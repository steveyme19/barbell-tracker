"""
The pod's calibration (roadmap 1.3, algorithm.md section 1): noise and gyro bias at rest, the
accelerometer's bias and scale from its 6 faces, and the gyro's scale per axis from whole turns.
Do it on the assembled pod: its flat faces are the calibration cube, and it catches the board
sitting crooked in the case.

    python calib_pod.py COM5                     # guided, live over USB; keeps the raw CSV + the result
    python calib_pod.py data/bar-xiao-....csv    # the same analysis on a recording
    python calib_pod.py --selftest               # a synthetic pod with known errors must come back out

The steps (the guide prints them and ticks them off as it sees them):
  1. Rest: leave it still on the table for 20 s.
  2. Faces: lay it on each of its 6 faces, still 3 s each, any order.
  3. Turns, each from rest back to rest, a whole number of turns about one axis:
     z: lid up, flush against a straight edge (a book), spin it 3 full turns on the table, back flush.
     x: on a long side, roll it along the table over its 4 long faces, one full turn (or more).
     y: on the lid, tumble it end over end over lid, end, bottom, end: one full turn.
     Gravity checks that a tumble (x, y) ended where it began. It can't see a spin about the
     vertical, so that one (z) needs the edge: 1 deg off over 3 turns is 0.1% of scale.

Accelerometer (calib.py's fit, the phone's too): corrected = (raw - bias) / scale per axis.
Gyro: corrected = (raw - bias) / scale, the bias from the stills (the app re-estimates it at every
still anyway), the scale = the integrated angle / (360 x the number of turns). A turn counts when
it's within TURN_TOL of a whole number of turns, mostly about one axis (OFF_AXIS), and its start
and end gravity agree within GRAV_TOL. Saves data/pod-calibration-YYYY-MM-DD-HHMM.json.
"""

import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

import calib

BLOCK = 0.25          # s: the still detector's blocks
STILL_MIN = 1.0       # s: shortest still that counts
FACE_MIN = 2.0        # s: a face's still must last this long
FACE_TILT = 10.0      # deg: a face is down when one axis is within this of vertical
REST_MIN = 10.0       # s: the rest still (noise, gyro bias)
TURN_MIN = 300.0      # deg: less isn't a turn (moving to the next face is ~90)
TURN_TOL = 0.03       # a turn's angle must be within this of a whole number of turns (gyro scale is spec'd to a
                      # few %; a sloppy spin about the vertical, 340 deg read as 345 at +1.5%, must not pass)
OFF_AXIS = 0.15       # the other two axes may turn at most this x the main one
GRAV_TOL = 2.0        # deg: gravity before and after a turn must agree this well
FACES = ["+x", "-x", "+y", "-y", "+z", "-z"]
FACE_NAME = {"+z": "lid up", "-z": "lid down", "+x": "far end up", "-x": "USB-C end up",
             "+y": "5V/GND/3V3 side up", "-y": "D0-D6 side up"}


def still_runs(t, acc, gyro):
    """[(i0, i1)] sample ranges where the pod lies still: blocks of BLOCK s whose accel and gyro
    wiggle stays under 4x the quietest block's (floored at 0.03 m/s^2 and 0.3 deg/s) and whose
    gyro sits near the recording's usual rest reading, merged, at least STILL_MIN long."""
    edges = np.searchsorted(t, np.arange(t[0], t[-1], BLOCK))
    blocks = [(a, b) for a, b in zip(edges[:-1], edges[1:]) if b - a >= 3]
    if not blocks:
        return []
    a_sd = np.array([acc[a:b].std(axis=0).max() for a, b in blocks])
    g_sd = np.array([gyro[a:b].std(axis=0).max() for a, b in blocks])
    g_mean = np.array([gyro[a:b].mean(axis=0) for a, b in blocks])
    a_lim = max(0.03, 4 * np.percentile(a_sd, 5))
    g_lim = max(0.3, 4 * np.percentile(g_sd, 5))
    rest = np.median(g_mean[(a_sd < a_lim) & (g_sd < g_lim)], axis=0) if np.any((a_sd < a_lim) & (g_sd < g_lim)) else 0.0
    still = (a_sd < a_lim) & (g_sd < g_lim) & (np.abs(g_mean - rest).max(axis=1) < 3 * g_lim)
    runs, k = [], 0
    while k < len(blocks):
        if not still[k]:
            k += 1
            continue
        j = k
        while j + 1 < len(blocks) and still[j + 1]:
            j += 1
        i0, i1 = blocks[k][0], blocks[j][1] - 1
        if t[i1] - t[i0] >= STILL_MIN:
            runs.append((i0, i1))
        k = j + 1
    return runs


def face_of(m):
    """'+z' and the like when one axis of the still reading m is within FACE_TILT of vertical."""
    k, s = calib.orientation(m)
    tilt = np.degrees(np.arccos(min(1.0, abs(m[k]) / np.linalg.norm(m))))
    return (("+" if s > 0 else "-") + "xyz"[k]) if tilt <= FACE_TILT else None


def angle_between(a, b):
    c = np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b))
    return float(np.degrees(np.arccos(np.clip(c, -1.0, 1.0))))


def analyze(t, acc, gyro):
    """Everything from one recording: the rest, the faces, the turns, and the fits."""
    runs = still_runs(t, acc, gyro)
    out = {"stills": len(runs), "rest": None, "faces": {}, "turns": [], "rejected": [], "accel": None,
           "gyro": {"bias": None, "scale": [None, None, None]}}
    if not runs:
        return out
    mean = [acc[a:b + 1].mean(axis=0) for a, b in runs]
    gmean = [gyro[a:b + 1].mean(axis=0) for a, b in runs]
    dur = [t[b] - t[a] for a, b in runs]
    # the rest: the longest still, if it's long enough for noise and bias
    r = int(np.argmax(dur))
    if dur[r] >= REST_MIN:
        a, b = runs[r]
        out["rest"] = {"seconds": dur[r], "accel_noise": acc[a:b + 1].std(axis=0).tolist(),
                       "gyro_noise": gyro[a:b + 1].std(axis=0).tolist(), "gyro_bias": gmean[r].tolist()}
    # the gyro's bias: the median over every still (it drifts a little, so spread is shown too)
    out["gyro"]["bias"] = np.median(gmean, axis=0).tolist()
    out["gyro"]["bias_spread"] = (np.max(gmean, axis=0) - np.min(gmean, axis=0)).tolist()
    # the faces: per face, its longest still
    for k, (a, b) in enumerate(runs):
        f = face_of(mean[k])
        if f and dur[k] >= FACE_MIN and (f not in out["faces"] or dur[k] > out["faces"][f]["seconds"]):
            out["faces"][f] = {"mean": mean[k].tolist(), "seconds": dur[k]}
    if len(out["faces"]) == 6:
        out["accel"] = calib.fit_calibration([out["faces"][f]["mean"] for f in FACES])
    # the turns: between two stills, the gyro integrated with the bias of the stills either side
    for k in range(len(runs) - 1):
        a, b = runs[k][1], runs[k + 1][0]
        if b - a < 3:
            continue
        bias = (gmean[k] + gmean[k + 1]) / 2
        w = gyro[a:b + 1] - bias
        ang = np.trapezoid(w, t[a:b + 1], axis=0) if hasattr(np, "trapezoid") else np.trapz(w, t[a:b + 1], axis=0)
        ax = int(np.argmax(np.abs(ang)))
        main = abs(ang[ax])
        if main < TURN_MIN:
            continue
        n = int(round(main / 360.0))
        turn = {"axis": "xyz"[ax], "angle": float(ang[ax]), "n": n, "scale": main / (360.0 * n) if n else None,
                "off_axis": float(max(abs(ang[j]) for j in range(3) if j != ax) / main),
                "gravity_deg": angle_between(mean[k], mean[k + 1]), "at": float(t[a])}
        why = None
        if n < 1 or abs(turn["scale"] - 1) > TURN_TOL:
            why = f"{main:.0f} deg isn't a whole number of turns"
        elif turn["off_axis"] > OFF_AXIS:
            why = f"not about one axis ({turn['off_axis']:.0%} on the others)"
        elif turn["gravity_deg"] > GRAV_TOL:
            why = f"it didn't end where it began (gravity {turn['gravity_deg']:.1f} deg off)"
        (out["rejected"] if why else out["turns"]).append(dict(turn, why=why) if why else turn)
    for k, axis in enumerate("xyz"):
        s = [x["scale"] for x in out["turns"] if x["axis"] == axis]
        if s:
            out["gyro"]["scale"][k] = float(np.median(s))
    return out


def checklist(res):
    """One line: what's done, what's left."""
    faces = " ".join(f"{f}{'ok' if f in res['faces'] else '--'}" for f in FACES)
    turns = " ".join(f"{a}{'ok' if res['gyro']['scale'][k] else '--'}" for k, a in enumerate("xyz"))
    rest = f"rest {'ok' if res['rest'] else '--'}"
    return f"{rest} | faces {faces} | turns {turns}"


def done(res):
    return bool(res["rest"]) and len(res["faces"]) == 6 and all(res["gyro"]["scale"])


def summary(res):
    lines = [checklist(res)]
    if res["rest"]:
        rn = res["rest"]
        lines.append(f"rest {rn['seconds']:.0f} s: accel noise {', '.join(f'{x:.4f}' for x in rn['accel_noise'])} m/s^2, "
                     f"gyro noise {', '.join(f'{x:.3f}' for x in rn['gyro_noise'])} deg/s")
    b = res["gyro"]["bias"]
    if b:
        lines.append(f"gyro bias {', '.join(f'{x:+.3f}' for x in b)} deg/s (spread over the stills "
                     f"{', '.join(f'{x:.3f}' for x in res['gyro']['bias_spread'])})")
    if res["accel"]:
        c = res["accel"]
        for k, ax in enumerate("xyz"):
            lines.append(f"accel {ax}: bias {c['bias'][k]:+.3f} m/s^2, scale {(c['scale'][k] - 1) * 100:+.2f}%")
        lines.append(f"accel fit: worst |corrected| - g over the faces {c['residual']:.3f} m/s^2")
    for k, ax in enumerate("xyz"):
        s = res["gyro"]["scale"][k]
        if s:
            n = [x for x in res["turns"] if x["axis"] == ax]
            spread = (max(x["scale"] for x in n) - min(x["scale"] for x in n)) * 100
            lines.append(f"gyro {ax}: scale {(s - 1) * 100:+.2f}% from {len(n)} turn(s), spread {spread:.2f}%")
    for x in res["rejected"]:
        lines.append(f"turn about {x['axis']} at {x['at']:.0f} s not used: {x['why']}")
    return "\n".join(lines)


def result_json(res, source):
    return {"what": "the pod's IMU calibration (roadmap 1.3): corrected = (raw - bias) / scale, per axis",
            "made": datetime.now().isoformat(timespec="seconds"), "from": source,
            "accel": None if not res["accel"] else {"bias": res["accel"]["bias"], "scale": res["accel"]["scale"],
                                                    "residual": res["accel"]["residual"]},
            "gyro": {"bias": res["gyro"]["bias"], "scale": res["gyro"]["scale"], "turns": res["turns"]},
            "rest": res["rest"], "faces": res["faces"], "rejected": res["rejected"]}


def save(res, source):
    out = Path(__file__).parent / "data" / f"pod-calibration-{datetime.now():%Y-%m-%d-%H%M}.json"
    out.write_text(json.dumps(result_json(res, source), indent=1), encoding="utf-8")
    return out


def load_csv(path):
    import reps as R
    t, _ = R.load_phyphox(Path(path), R.AXIS)
    acc, gyro = R.load_imu(Path(path), len(t))
    if acc is None or gyro is None:
        sys.exit(f"{path}: no 3-axis accel and gyro columns")
    return np.asarray(t, float), np.asarray(acc, float), np.asarray(gyro, float)


def live(port):
    """Guided: records like log_serial.py (the raw CSV is kept, P2) and reruns the analysis every
    second on what it has, printing what's done and what's left."""
    import log_serial as L
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M")
    csv = Path(__file__).parent / "data" / f"bar-xiao-{stamp}-calib.csv"
    print("The steps " + __doc__.split("The steps ")[1].split("Accelerometer")[0].strip(), "\n")
    print(f"Recording {port} to {csv.name}. Ctrl+C when you're done (it stops by itself once everything is in).")
    t_l, a_l, g_l = [], [], []
    t0 = last = None
    wrap, prev_line, finished_at, told = 0, "", None, set()
    res = None
    with open(csv, "w", newline="") as f:
        f.write(L.HEADER + "\n")
        try:
            for line in L.serial_lines(port):
                if line.startswith("#"):
                    continue
                s = L.parse(line)
                if s is None:
                    continue
                if last is not None and s[0] < last:
                    wrap += 2 ** 32
                last = s[0]
                t0 = s[0] + wrap if t0 is None else t0
                tt = (s[0] + wrap - t0) / 1e6
                ax, ay, az, gx, gy, gz = s[1:]
                f.write(f"{tt:.6f},{ax:.4f},{ay:.4f},{az:.4f},{np.sqrt(ax * ax + ay * ay + az * az):.4f},"
                        f"{gx:.3f},{gy:.3f},{gz:.3f}\n")
                t_l.append(tt)
                a_l.append((ax, ay, az))
                g_l.append((gx, gy, gz))
                if len(t_l) % 208 == 0 and tt > 2:
                    res = analyze(np.array(t_l), np.array(a_l), np.array(g_l))
                    line_now = checklist(res)
                    if line_now != prev_line:
                        print(f"{tt:6.0f} s  {line_now}")
                        for x in res["rejected"]:
                            if round(x["at"]) not in told:
                                told.add(round(x["at"]))
                                print(f"         turn about {x['axis']} not used: {x['why']}")
                        prev_line = line_now
                    if done(res) and finished_at is None:
                        finished_at = tt
                        print("         everything is in: stopping in 3 s")
                    if finished_at is not None and tt - finished_at > 3:
                        break
        except KeyboardInterrupt:
            print()
    if len(t_l) < 100:
        sys.exit("No samples: is the pod on this port, and the Arduino Serial Monitor closed?")
    res = analyze(np.array(t_l), np.array(a_l), np.array(g_l))
    print("\n" + summary(res))
    out = save(res, csv.name)
    print(f"\nRaw recording: {csv}\nCalibration: {out}" + ("" if done(res) else "  (incomplete: see what's left above)"))


A_BIAS, A_SCALE = np.array([0.08, -0.05, 0.12]), np.array([1.012, 0.994, 1.006])   # the synthetic pod's errors
G_BIAS, G_SCALE = np.array([0.6, -0.4, 0.25]), np.array([1.008, 0.993, 1.015])


def synthetic(seed=4):
    """The self-test's pod (see selftest): (t, acc, gyro) at 208 Hz, with A_BIAS/A_SCALE and
    G_BIAS/G_SCALE planted and white noise like the pod's."""
    from make_fake_motion import min_jerk, rot
    FS, G = 208.0, calib.G
    a_bias, a_scale, g_bias, g_scale = A_BIAS, A_SCALE, G_BIAS, G_SCALE
    rng = np.random.default_rng(seed)
    R = np.eye(3)                                         # sensor -> world, z up to start
    t_all, f_all, w_all = [], [], []
    t_now = [0.0]

    def hold(sec):
        n = int(sec * FS)
        t_all.append(t_now[0] + np.arange(n) / FS)
        f_all.append(np.repeat((R.T @ np.array([0, 0, G]))[None], n, axis=0))
        w_all.append(np.zeros((n, 3)))
        t_now[0] += n / FS

    def turn(axis, deg, sec):
        """A min-jerk turn about a body axis (exact body rate), then R moves on."""
        nonlocal R
        n = int(sec * FS)
        tau = np.arange(n) / n
        s, ds, _ = min_jerk(tau)
        e = np.asarray(axis, float)
        Rs = np.array([R @ rot(e, np.radians(deg) * x) for x in s])
        t_all.append(t_now[0] + np.arange(n) / FS)
        f_all.append(np.einsum("nji,j->ni", Rs, np.array([0, 0, G])))
        w_all.append(np.outer(deg * ds / sec, e))
        R = R @ rot(e, np.radians(deg))
        t_now[0] += n / FS

    X, Y, Z = [1, 0, 0], [0, 1, 0], [0, 0, 1]
    hold(25)                                              # rest, lid up (+z)
    turn(X, 25, 1.0)                                      # leaning on the edge, 25 deg: not a face
    hold(30)
    turn(X, -25, 1.0)
    hold(3)
    for axis, deg in ((X, 180), (X, 90), (X, 180), (X, 90), (Y, 90), (Y, 180)):
        turn(axis, deg, 1.5)                              # to the next face
        hold(4)
    turn(Y, 90, 1.5)                                      # back lid up
    hold(3)
    turn(Z, 3 * 360, 6.0)                                 # 3 spins on the table
    hold(3)
    turn(Z, 340, 2.0)                                     # not a whole turn: refused
    hold(3)
    turn(X, 90, 1.5)                                      # onto a long side
    hold(3)
    turn(X, 360, 4.0)                                     # a roll over the 4 long faces
    hold(3)
    turn(X, -90, 1.5)                                     # lid up again
    hold(3)
    turn(Y, 360, 4.0)                                     # end over end
    hold(3)
    turn(Y, 356, 4.0)                                     # ends 4 deg off (a whole turn to 1.8%): refused by gravity
    hold(3)
    tilted = np.array([1, 0.35, 0]) / np.linalg.norm([1, 0.35, 0])
    turn(tilted, 383, 4.0)                                # x turns 362 deg but y 126: not about one axis
    hold(3)
    t = np.concatenate(t_all)
    f = np.concatenate(f_all)
    w = np.concatenate(w_all)
    acc = f * a_scale + a_bias + rng.normal(0, 0.012, f.shape)
    gyro = w * g_scale + g_bias + rng.normal(0, 0.07, w.shape)
    return t, acc, gyro


def selftest():
    """A synthetic pod at 208 Hz with known errors, through the analysis: accel bias (0.08,
    -0.05, 0.12) m/s^2 and scale (+1.2, -0.6, +0.6)%, gyro bias (0.6, -0.4, 0.25) deg/s and
    scale (+0.8, -0.7, +1.5)%, white noise like the pod's (0.012 m/s^2, 0.07 deg/s). It rests
    25 s, leans 25 deg for 30 s (longer than any face: it must not be one), visits the 6 faces,
    then turns 3 times about z, once about x and once about y; a 340
    deg spin (345 read: not a whole turn) and a tumble 4 deg short (a whole turn to 1.8%, but
    gravity says it didn't end where it began) and a turn about a tilted axis must be refused.
    Every error must come back out."""
    t, acc, gyro = synthetic()
    a_bias, a_scale, g_bias, g_scale = A_BIAS, A_SCALE, G_BIAS, G_SCALE
    res = analyze(t, acc, gyro)
    print(summary(res))
    ok = True

    def check(good, msg):
        nonlocal ok
        ok &= bool(good)
        print(f"{'PASS' if good else 'FAIL'}  {msg}")

    check(res["rest"] and res["rest"]["seconds"] > 20 and abs(np.mean(res["rest"]["gyro_noise"]) - 0.07) < 0.01,
          f"the rest: {res['rest']['seconds']:.0f} s, gyro noise {np.mean(res['rest']['gyro_noise']):.3f} deg/s (0.07)"
          if res["rest"] else "the rest: none")
    check(np.allclose(res["gyro"]["bias"], g_bias, atol=0.02), f"gyro bias {np.round(res['gyro']['bias'], 3)} ({g_bias})")
    c = res["accel"]
    check(c and np.allclose(c["bias"], a_bias, atol=0.01) and np.allclose(c["scale"], a_scale, atol=0.001),
          f"accel bias {np.round(c['bias'], 3)}, scale {np.round(c['scale'], 4)} ({a_bias}, {a_scale})" if c else "accel: no fit")
    gs = res["gyro"]["scale"]
    check(all(gs) and np.allclose(gs, g_scale, atol=0.002), f"gyro scale {np.round(gs, 4) if all(gs) else gs} ({g_scale})")
    check(len(res["rejected"]) == 3 and any("whole number" in x["why"] for x in res["rejected"])
          and any("didn't end" in x["why"] for x in res["rejected"]) and any("one axis" in x["why"] for x in res["rejected"]),
          f"the 340 deg spin, the tumble 4 deg short and the tilted turn refused: {[x['why'] for x in res['rejected']]}")
    check(len(res["turns"]) == 3, f"3 turns used ({[x['axis'] + str(x['n']) for x in res['turns']]})")
    print("PASS" if ok else "FAIL")
    return ok


def main():
    sys.stdout.reconfigure(errors="replace")
    if "--selftest" in sys.argv:
        sys.exit(0 if selftest() else 1)
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        sys.exit(__doc__)
    if args[0].lower().endswith(".csv"):
        t, acc, gyro = load_csv(args[0])
        res = analyze(t, acc, gyro)
        print(summary(res))
        print(f"\nCalibration: {save(res, Path(args[0]).name)}" + ("" if done(res) else "  (incomplete)"))
    else:
        live(args[0])


if __name__ == "__main__":
    main()
