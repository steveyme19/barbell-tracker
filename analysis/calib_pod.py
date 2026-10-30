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
  2. Faces: lay it flat on each of its 6 faces, still 3 s each, any order. The USB-C end down
     (+x) can't stand on the plugged-in cable: stand it at the table's edge, that end face flat
     on the table and the plug hanging over the edge.
  3. Turns, each from rest back to rest, a whole number of turns about one axis:
     z: lid up, flush against a straight edge (a book), spin it 3 full turns, back flush.
     y: on a long side, flush against the edge, spin it 3 full turns the same way, back flush.
     x: on a long side, roll it along the table over its 4 long faces: one full turn or more.
        Stopping on each face is fine.
     Gravity checks that the roll (x) ended where it began. It can't see a spin about the
     vertical, so those (z, y) need the edge: 1 deg off over 3 turns is 0.1% of scale. A spin
     stopped short and pushed on a little (under 45 deg) is still the one spin; one turned back
     after it stopped is refused (which still was the flush one?): stop flush. Repeat any turn
     as you like: each axis takes the median.

Accelerometer (calib.py's fit, the phone's too): corrected = (raw - bias) / scale per axis. A
face's flattest still is used; if the fit's worst face is off by over RESID_MAX, that face wasn't
flat and the guide asks for it again.
Gyro: corrected = (raw - bias) / scale, the bias from the stills (the app re-estimates it at every
still anyway), the scale = the angle about the turn's own axis / (360 x the number of turns). A
turn counts when it's mostly about one axis (OFF_AXIS), within TURN_TOL of a whole number of
turns, its start and end gravity agree within GRAV_TOL, and (about the vertical) it wasn't turned
back after it stopped (NUDGE_MAX). Saves data/pod-calibration-YYYY-MM-DD-HHMM.json.
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
RESID_MAX = 0.05      # m/s^2: the accel fit's worst face; a face propped t deg adds about g sin^2(t) / 2
REST_MIN = 10.0       # s: the rest still (noise, gyro bias)
TURN_MIN = 300.0      # deg: less isn't a turn (moving to the next face is ~90)
TURN_TOL = 0.03       # a turn's angle must be within this of a whole number of turns (gyro scale is spec'd to a
                      # few %; a sloppy spin about the vertical, 340 deg read as 345 at +1.5%, must not pass)
OFF_AXIS = 0.15       # the other two axes may turn at most this x the main one
GRAV_TOL = 2.0        # deg: gravity before and after a turn must agree this well
BACK_NEAR = 45.0      # deg: a roll done face by face is one turn once gravity is back this near its start
NUDGE_MAX = 2.0       # deg: a spin about the vertical turned back more than this after it stopped is refused
FACES = ["+x", "-x", "+y", "-y", "+z", "-z"]
FACE_NAME = {"+z": "lid up", "-z": "lid down", "+x": "USB-C end down (at the table's edge, plug over it)",
             "-x": "USB-C end up", "+y": "5V/GND/3V3 side up", "-y": "D0-D6 side up"}


def _trapz(y, x, axis=0):
    return np.trapezoid(y, x, axis=axis) if hasattr(np, "trapezoid") else np.trapz(y, x, axis=axis)


def still_runs(t, acc, gyro):
    """[(i0, i1)] sample ranges where the pod lies still: blocks of BLOCK s whose accel and gyro
    wiggle stays under 4x the quietest block's (floored at 0.03 m/s^2 and 0.3 deg/s) and whose
    gyro sits near the rest reading (the slowest quiet block's), merged, at least STILL_MIN long."""
    edges = np.searchsorted(t, np.arange(t[0], t[-1], BLOCK))
    blocks = [(a, b) for a, b in zip(edges[:-1], edges[1:]) if b - a >= 3]
    if not blocks:
        return []
    a_sd = np.array([acc[a:b].std(axis=0).max() for a, b in blocks])
    g_sd = np.array([gyro[a:b].std(axis=0).max() for a, b in blocks])
    g_mean = np.array([gyro[a:b].mean(axis=0) for a, b in blocks])
    a_lim = max(0.03, 4 * np.percentile(a_sd, 5))
    g_lim = max(0.3, 4 * np.percentile(g_sd, 5))
    quiet = (a_sd < a_lim) & (g_sd < g_lim)
    rest = 0.0
    if np.any(quiet):
        # the rest reading: the quiet blocks near the slowest one (a steady turn, a turntable's, is quiet too
        # and may outlast the stills, so not the median of every quiet block)
        qm = g_mean[quiet]
        g0 = qm[np.argmin(np.linalg.norm(qm, axis=1))]
        rest = np.median(qm[np.abs(qm - g0).max(axis=1) < 3 * g_lim], axis=0)
    still = quiet & (np.abs(g_mean - rest).max(axis=1) < 3 * g_lim)
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


def tilt_of(m):
    """How far (deg) the still reading m's nearest-vertical axis is from vertical."""
    k, _ = calib.orientation(m)
    return float(np.degrees(np.arccos(min(1.0, abs(m[k]) / np.linalg.norm(m)))))


def face_of(m):
    """'+z' and the like when one axis of the still reading m is within FACE_TILT of vertical."""
    k, s = calib.orientation(m)
    return (("+" if s > 0 else "-") + "xyz"[k]) if tilt_of(m) <= FACE_TILT else None


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
    # the faces: per face, its flattest still (a redo replaces a propped one), the longer on a tie
    for k in range(len(runs)):
        f = face_of(mean[k])
        if not f or dur[k] < FACE_MIN:
            continue
        old = out["faces"].get(f)
        tl = tilt_of(mean[k])
        if old is None or tl < old["tilt"] - 0.3 or (abs(tl - old["tilt"]) <= 0.3 and dur[k] > old["seconds"]):
            out["faces"][f] = {"mean": mean[k].tolist(), "seconds": dur[k], "tilt": tl}
    if len(out["faces"]) == 6:
        c = calib.fit_calibration([out["faces"][f]["mean"] for f in FACES])
        if c and c["residual"] > RESID_MAX:
            # the face least flat once corrected: that one gets done again
            post = {f: tilt_of((np.array(out["faces"][f]["mean"]) - c["bias"]) / c["scale"]) for f in FACES}
            c["redo"] = max(post, key=post.get)
        out["accel"] = c
    # the moves between stills: the gyro integrated with the bias of the stills either side
    moves = []
    for k in range(len(runs) - 1):
        a, b = runs[k][1], runs[k + 1][0]
        if b - a < 3:
            moves.append(None)
            continue
        w = gyro[a:b + 1] - (gmean[k] + gmean[k + 1]) / 2
        moves.append((w, t[a:b + 1], _trapz(w, t[a:b + 1])))
    k = 0
    while k < len(moves):
        if moves[k] is None:
            k += 1
            continue
        ang = moves[k][2].copy()
        ax = int(np.argmax(np.abs(ang)))
        sg = np.sign(ang[ax])
        level = abs(mean[k][ax]) < 0.5 * np.linalg.norm(mean[k])     # the turn's axis lies flat: gravity checks its end

        def same(j):
            return (j < len(moves) and moves[j] is not None and int(np.argmax(np.abs(moves[j][2]))) == ax
                    and np.sign(moves[j][2][ax]) == sg)
        m = k
        if level:
            # a roll done face by face: one turn once gravity is back near where it began
            while angle_between(mean[k], mean[m + 1]) > BACK_NEAR and same(m + 1):
                m += 1
                ang = ang + moves[m][2]
            if angle_between(mean[k], mean[m + 1]) > BACK_NEAR:
                k += 1                                        # it never came back: moving between faces
                continue
        else:
            # a spin about the vertical stopped short and pushed on (a nudge under BACK_NEAR, the same way,
            # on the same face) is the one spin; a bigger move is a spin of its own
            while same(m + 1) and abs(moves[m + 1][2][ax]) < BACK_NEAR:
                m += 1
                ang = ang + moves[m][2]
        if abs(ang[ax]) < TURN_MIN:
            k += 1
            continue
        # the angle about the turn's own axis (the main direction of its rate), not its share on ax
        u = np.linalg.eigh(sum(moves[j][0].T @ moves[j][0] for j in range(k, m + 1)))[1][:, -1]
        main = abs(sum(_trapz(moves[j][0] @ u, moves[j][1]) for j in range(k, m + 1)))
        n = int(round(main / 360.0))
        turn = {"axis": "xyz"[ax], "angle": float(sg * main), "n": n, "scale": main / (360.0 * n) if n else None,
                "off_axis": float(max(abs(ang[j]) for j in range(3) if j != ax) / abs(ang[ax])),
                "gravity_deg": angle_between(mean[k], mean[m + 1]), "pieces": m - k + 1, "at": float(t[runs[k][1]])}
        why = None
        if turn["off_axis"] > OFF_AXIS:
            why = f"not about one axis ({turn['off_axis']:.0%} on the others)"
        elif n < 1 or abs(turn["scale"] - 1) > TURN_TOL:
            why = f"{main:.0f} deg isn't a whole number of turns"
        elif turn["gravity_deg"] > GRAV_TOL:
            why = f"it didn't end where it began (gravity {turn['gravity_deg']:.1f} deg off)"
        elif not level:
            back, j = 0.0, m + 1
            while j < len(moves) and moves[j] is not None and np.abs(moves[j][2]).max() < BACK_NEAR:
                back += float(moves[j][2][ax])
                j += 1
            if abs(back) > NUDGE_MAX:
                why = f"it stopped, then turned {back:+.0f} deg back: which still was flush? Spin it again, stop flush"
        (out["rejected"] if why else out["turns"]).append(dict(turn, why=why) if why else turn)
        k = m + 1
    for k, axis in enumerate("xyz"):
        s = [x["scale"] for x in out["turns"] if x["axis"] == axis]
        if s:
            out["gyro"]["scale"][k] = float(np.median(s))
    return out


def accel_ok(res):
    return bool(res["accel"]) and res["accel"]["residual"] <= RESID_MAX


def checklist(res):
    """One line: what's done, what's left."""
    faces = " ".join(f"{f}{'ok' if f in res['faces'] else '--'}" for f in FACES)
    turns = " ".join(f"{a}{'ok' if res['gyro']['scale'][k] else '--'}" for k, a in enumerate("xyz"))
    line = f"rest {'ok' if res['rest'] else '--'} | faces {faces} | turns {turns}"
    if res["accel"] and not accel_ok(res):
        line += f" | redo {res['accel']['redo']} ({FACE_NAME[res['accel']['redo']]}), flat"
    return line


def done(res):
    return bool(res["rest"]) and accel_ok(res) and all(res["gyro"]["scale"])


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
        lines.append(f"accel fit: worst |corrected| - g over the faces {c['residual']:.3f} m/s^2"
                     + ("" if accel_ok(res) else f": a face wasn't flat, redo {c['redo']} ({FACE_NAME[c['redo']]})"))
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
            "made": datetime.now().isoformat(timespec="seconds"), "from": source, "complete": done(res),
            "accel": None if not res["accel"] else {"bias": res["accel"]["bias"], "scale": res["accel"]["scale"],
                                                    "residual": res["accel"]["residual"], "ok": accel_ok(res)},
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
    second on what it has, printing what's done and what's left, and any turn it couldn't use."""
    import log_serial as L
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M")
    csv = Path(__file__).parent / "data" / f"bar-xiao-{stamp}-calib.csv"
    print("The steps " + __doc__.split("The steps ")[1].split("Accelerometer")[0].strip(), "\n")
    print(f"Recording {port} to {csv.name}. Ctrl+C when you're done (it stops by itself once everything is in).")
    t_l, a_l, g_l = [], [], []
    t0 = last = None
    wrap, prev_line, finished_at, told, gone = 0, "", None, set(), None
    with open(csv, "w", newline="") as f:
        f.write(L.HEADER + "\n")
        try:
            for line in L.serial_lines(port):
                if line.startswith("#"):
                    continue
                s = L.parse(line)
                if s is None:
                    continue
                if last is not None and last - s[0] > 2 ** 31:   # micros() rolled over (a reset drops the port first)
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
                        prev_line = line_now
                    for x in res["rejected"]:                 # a refused turn ticks nothing: say so right away
                        if round(x["at"]) not in told:
                            told.add(round(x["at"]))
                            print(f"{tt:6.0f} s  turn about {x['axis']} not used: {x['why']}. Do it again.")
                    if set(res["faces"]) == set(FACES) - {"+x"} and "+x" not in told:
                        told.add("+x")
                        print(f"{tt:6.0f} s  left: the USB-C end down. The plug sticks out of that face, so stand it at the"
                              " table's edge, the end face flat on the table and the plug hanging over, 3 s")
                    if done(res) and finished_at is None:
                        finished_at = tt
                        print("         everything is in: stopping in 3 s")
                    if finished_at is not None and tt - finished_at > 3:
                        break
        except KeyboardInterrupt:
            print()
        except OSError as e:                                  # pyserial's SerialException: no port, or it went away
            gone = e
    if not t_l:
        csv.unlink()                                          # only the header: nothing to keep
    if len(t_l) < 100:
        sys.exit(f"No samples{f' ({gone})' if gone else ''}: is the pod on this port and awake (lying lid-down with "
                 "the port closed it sleeps: pick it up), and the Arduino Serial Monitor closed?")
    if gone:
        print(f"\n{port} went away at {t_l[-1]:.0f} s ({gone}): the result from what came in")
    res = analyze(np.array(t_l), np.array(a_l), np.array(g_l))
    print("\n" + summary(res))
    out = save(res, csv.name)
    print(f"\nRaw recording: {csv}\nCalibration: {out}" + ("" if done(res) else "  (incomplete: see what's left above)"))


A_BIAS, A_SCALE = np.array([0.08, -0.05, 0.12]), np.array([1.012, 0.994, 1.006])   # the synthetic pod's errors
G_BIAS, G_SCALE = np.array([0.6, -0.4, 0.25]), np.array([1.008, 0.993, 1.015])
X, Y, Z = [1, 0, 0], [0, 1, 0], [0, 0, 1]


def main_plan(hold, turn):
    """The self-test's session (see selftest)."""
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


def synthetic(seed=4, plan=main_plan, drift=0.0):
    """The self-test's pod: (t, acc, gyro) at 208 Hz, with A_BIAS/A_SCALE and G_BIAS/G_SCALE
    planted (the gyro bias drifting by drift deg/s every second) and white noise like the pod's,
    moved by plan(hold, turn)."""
    from make_fake_motion import min_jerk, rot
    FS, G = 208.0, calib.G
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

    def turn(axis, deg, sec, steady=False):
        """A min-jerk turn about a body axis (exact body rate), or a steady one (a turntable),
        then R moves on."""
        nonlocal R
        n = int(sec * FS)
        tau = np.arange(n) / n
        s, ds = (tau, np.ones(n)) if steady else min_jerk(tau)[:2]
        e = np.asarray(axis, float) / np.linalg.norm(axis)
        Rs = np.array([R @ rot(e, np.radians(deg) * x) for x in s])
        t_all.append(t_now[0] + np.arange(n) / FS)
        f_all.append(np.einsum("nji,j->ni", Rs, np.array([0, 0, G])))
        w_all.append(np.outer(deg * ds / sec, e))
        R = R @ rot(e, np.radians(deg))
        t_now[0] += n / FS

    plan(hold, turn)
    t = np.concatenate(t_all)
    f = np.concatenate(f_all)
    w = np.concatenate(w_all)
    acc = f * A_SCALE + A_BIAS + rng.normal(0, 0.012, f.shape)
    gyro = w * G_SCALE + G_BIAS + drift * t[:, None] + rng.normal(0, 0.07, w.shape)
    return t, acc, gyro


def selftest():
    """A synthetic pod at 208 Hz with known errors, through the analysis: accel bias (0.08,
    -0.05, 0.12) m/s^2 and scale (+1.2, -0.6, +0.6)%, gyro bias (0.6, -0.4, 0.25) deg/s and
    scale (+0.8, -0.7, +1.5)%, white noise like the pod's (0.012 m/s^2, 0.07 deg/s).
    The main session rests 25 s, leans 25 deg for 30 s (longer than any face: it must not be one),
    visits the 6 faces (two of those moves are whole turns about x and y by themselves), then
    turns 3 times about z, once about x and once about y; a 340 deg spin (345 read: not a whole
    turn), a tumble 4 deg short (a whole turn to 1.8%, but gravity says it didn't end where it
    began) and a turn about a tilted axis must be refused. Every error must come back out.
    Then the cases a real hand makes: a roll done a quarter at a time with 2 s on each face; a
    spin stopped 15 deg short then pushed on flush (one spin), and one 15 deg over then turned back
    (refused: which still was flush?); a whole turn about an axis 6 deg off the board's (its own
    axis's angle, not its share); a face propped 8 deg (a redo asked for, then a flat one used);
    three spins, the first 16 deg short (a whole number to 1.5%, so it's used: the median puts it
    right); a gyro warming up, its bias drifting 0.6 deg/s a minute (each move's bias from the
    stills either side of it); a steady turn like a turntable's (quiet, but not still).
    Last, each tolerance just inside and just outside: faces at 8 and 12 deg, stills of 2.5 and
    1.9 s (measured on 0.25 s blocks: 2.5 and 1.75), turns 11% and 20% off-axis, gravity 1.5 and
    2.6 deg off."""
    ok = True

    def check(good, msg):
        nonlocal ok
        ok &= bool(good)
        print(f"{'PASS' if good else 'FAIL'}  {msg}")

    t, acc, gyro = synthetic()
    res = analyze(t, acc, gyro)
    print(summary(res))
    check(res["rest"] and res["rest"]["seconds"] > 20 and abs(np.mean(res["rest"]["gyro_noise"]) - 0.07) < 0.01,
          f"the rest: {res['rest']['seconds']:.0f} s, gyro noise {np.mean(res['rest']['gyro_noise']):.3f} deg/s (0.07)"
          if res["rest"] else "the rest: none")
    check(np.allclose(res["gyro"]["bias"], G_BIAS, atol=0.02), f"gyro bias {np.round(res['gyro']['bias'], 3)} ({G_BIAS})")
    c = res["accel"]
    check(c and accel_ok(res) and np.allclose(c["bias"], A_BIAS, atol=0.01) and np.allclose(c["scale"], A_SCALE, atol=0.001),
          f"accel bias {np.round(c['bias'], 3)}, scale {np.round(c['scale'], 4)} ({A_BIAS}, {A_SCALE})" if c else "accel: no fit")
    gs = res["gyro"]["scale"]
    check(all(gs) and np.allclose(gs, G_SCALE, atol=0.002), f"gyro scale {np.round(gs, 4) if all(gs) else gs} ({G_SCALE})")
    check(len(res["rejected"]) == 3 and any("whole number" in x["why"] for x in res["rejected"])
          and any("didn't end" in x["why"] for x in res["rejected"]) and any("one axis" in x["why"] for x in res["rejected"]),
          f"the 340 deg spin, the tumble 4 deg short and the tilted turn refused: {[x['why'] for x in res['rejected']]}")
    used = [x["axis"] + str(x["n"]) for x in res["turns"]]
    check(used == ["x1", "y1", "z3", "x1", "y1"], f"turns used, the face moves' whole turns included: {used}")
    check(done(res), "the session is complete")

    def quarters(hold, turn):                             # a roll a quarter at a time, 2 s on each face
        hold(12)
        turn(X, 90, 1.5)
        hold(3)                                           # on a long side: x lies flat
        for _ in range(4):
            turn(X, 90, 1.0)
            hold(2.0)
    r = analyze(*synthetic(6, quarters))
    gx = r["gyro"]["scale"][0]
    check(gx and abs(gx - G_SCALE[0]) < 0.002 and [x["pieces"] for x in r["turns"]] == [4],
          f"a roll a quarter at a time (2 s on each face): x scale {gx} ({G_SCALE[0]}), pieces {[x['pieces'] for x in r['turns']]}")

    def short_then_on(hold, turn):                        # a spin 15 deg short, a look, pushed on flush
        hold(12)
        turn(Z, 3 * 360 - 15, 6.0)
        hold(2.0)
        turn(Z, 15, 0.6)
        hold(3)
        turn(Z, -90, 1.0)                                 # then turned a quarter back to read it: not a nudge
        hold(3)
    r = analyze(*synthetic(7, short_then_on))
    gz = r["gyro"]["scale"][2]
    check(gz and abs(gz - G_SCALE[2]) < 0.002 and not r["rejected"],
          f"a spin stopped short then pushed on flush counts (a quarter turn back after it isn't a nudge): z scale {gz} "
          f"({G_SCALE[2]}), refused {[x['why'] for x in r['rejected']]}")

    def over_then_back(hold, turn):                       # a spin 15 deg over, a look, turned back
        hold(12)
        turn(Z, 3 * 360 + 15, 6.0)
        hold(2.0)
        turn(Z, -15, 0.6)
        hold(3)
    r = analyze(*synthetic(8, over_then_back))
    check(not r["gyro"]["scale"][2] and any("turned" in x["why"] and "back" in x["why"] for x in r["rejected"]),
          f"a spin over then turned back is refused: {[x['why'] for x in r['rejected']]}")

    def tilted_turn(hold, turn):                          # a whole tumble about an axis 6 deg off y
        hold(12)
        turn(np.array([np.sin(np.radians(6)), np.cos(np.radians(6)), 0]), 360, 4.0)
        hold(3)
    r = analyze(*synthetic(9, tilted_turn))
    gy = r["gyro"]["scale"][1]
    check(gy and abs(gy - G_SCALE[1]) < 0.002, f"a whole turn about an axis 6 deg off y: y scale {gy} ({G_SCALE[1]}; "
          f"its share on y would read {G_SCALE[1] * np.cos(np.radians(6)):.4f})")

    def propped(hold, turn):                              # +y propped 8 deg, then done again flat
        hold(12)
        for axis, deg in ((X, 180), (X, 90), (Z, 90), (Z, 180)):
            turn(axis, deg, 1.5)                          # -z, -y, then the two x faces
            hold(4)
        turn(Z, -82, 1.5)                                 # onto +y, 8 deg short of flat
        hold(4)
    t1, a1, g1 = synthetic(10, propped)
    r = analyze(t1, a1, g1)
    redo = (r["accel"] or {}).get("redo")
    check(r["accel"] and not accel_ok(r) and redo == "+y" and "redo +y" in checklist(r),
          f"a face propped 8 deg: the fit's worst face {r['accel']['residual']:.3f} m/s^2, redo {redo}" if r["accel"] else "propped: no fit")

    def propped_then_flat(hold, turn):
        propped(hold, turn)
        turn(Z, -8, 0.8)                                  # now flat
        hold(3)
    r = analyze(*synthetic(10, propped_then_flat))
    check(accel_ok(r) and np.allclose(r["accel"]["scale"], A_SCALE, atol=0.001),
          f"then done flat, it's used: residual {r['accel']['residual']:.3f}, scale {np.round(r['accel']['scale'], 4)}"
          if r["accel"] else "then flat: no fit")

    def three(hold, turn):                                # three spins, the first 16 deg short
        hold(12)
        for deg in (3 * 360 - 16, 3 * 360, 3 * 360):
            turn(Z, deg, 6.0)
            hold(3)
    r = analyze(*synthetic(11, three))
    gz = r["gyro"]["scale"][2]
    check(gz and abs(gz - G_SCALE[2]) < 0.002 and len(r["turns"]) == 3,
          f"three spins, the first 16 deg short: z scale {gz} ({G_SCALE[2]}) from {len(r['turns'])} turns")

    def warming(hold, turn):                              # 2 minutes still while the bias drifts, then a spin
        hold(120)
        turn(Z, 20, 1.0)
        hold(3)
        turn(Z, 3 * 360, 6.0)
        hold(3)
    r = analyze(*synthetic(12, warming, drift=0.01))
    gz = r["gyro"]["scale"][2]
    check(gz and abs(gz - G_SCALE[2]) < 0.002, f"a drifting gyro bias (0.6 deg/s a minute): z scale {gz} ({G_SCALE[2]})")

    def turntable(hold, turn):                            # a steady 60 deg/s, 3 turns
        hold(12)
        turn(Z, 3 * 360, 18.0, steady=True)
        hold(3)
    r = analyze(*synthetic(13, turntable))
    gz = r["gyro"]["scale"][2]
    check(gz and abs(gz - G_SCALE[2]) < 0.002, f"a steady turn (quiet, but not still): z scale {gz} ({G_SCALE[2]})")

    def edges(hold, turn):                                # each tolerance just inside and just outside
        hold(25)
        turn(Y, 98, 1.5)
        hold(4)                                           # -x, 8 deg off vertical: a face
        turn(Y, 184, 1.5)
        hold(4)                                           # +x, 12 deg off: not a face
        turn(Y, 78, 1.5)
        hold(3)                                           # lid up
        turn(X, 90, 1.5)
        hold(2.5)                                         # +y, 2.5 s: a face
        turn(X, 180, 1.5)
        hold(1.9)                                         # -y, 1.9 s: too short
        turn(X, 90, 1.5)
        hold(3)                                           # lid up
        for off in (0.11, 0.20):                          # whole turns, 11% / 20% on the other axes
            e = np.array([1, off * G_SCALE[0] / G_SCALE[1], 0])
            turn(e / np.linalg.norm(e), 360, 4.0)
            hold(3)
        turn(Y, 358.5, 4.0)
        hold(3)                                           # ends 1.5 deg off: used
        turn(Y, 357.4, 4.0)
        hold(3)                                           # ends 2.6 deg off: refused
    e = analyze(*synthetic(5, edges))
    fs = sorted(e["faces"])
    check("-x" in fs and "+x" not in fs and "+y" in fs and "-y" not in fs, f"edges: faces {fs} (-x at 8 deg and +y at 2.5 s in)")
    seq = [x.get("why") for x in sorted(e["turns"] + e["rejected"], key=lambda x: x["at"])]
    want = [None, None, None, "one axis", None, "didn't end"]   # the first two: the face moves, whole rolls about y and x
    check(len(seq) == 6 and all((w is None and y is None) or (w and y and w in y) for y, w in zip(seq, want)),
          f"edges: 11% off-axis and 1.5 deg used, 20% and 2.6 deg refused: {seq}")
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
