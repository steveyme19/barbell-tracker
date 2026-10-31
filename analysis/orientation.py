"""
Orientation, world-frame acceleration, bar path and bar tilt from accel + gyro (roadmap 0.3;
algorithm.md sections 2, 4, 11 and 12; decisions D3 and D6). Not in the rep pipeline yet: reps.py
still finds reps on the tilt-proof magnitude (D9) until real bar data (phase 2) says orientation
does better (the trade is algorithm.md section 4's table).

    python orientation.py <recording.csv> [--bar=x|y|z|roll] [--plot]
    python orientation.py --selftest       # make_fake_motion's scenarios against their truth

1. Gyro bias: the gyro's mean over the still periods (the pod's reads ~0.3 deg/s at rest).
2. Orientation: 6-axis Madgwick (the reference IMU update, Madgwick 2010), started from the first
   still's gravity. The accelerometer's pull (beta) is strong while still, so the tilt converges,
   and weak while moving: there sideways acceleration tips the accel vector and would tip the
   estimate with it (algorithm.md section 11, "What tricks Madgwick").
3. World acceleration: the accel rotated into the world frame, minus g (the stills' mean |a|).
4. The bar axis, in the sensor frame: a mount axis (--bar=x|y|z) or the roll gesture (--bar=roll,
   the default): rolling the bar turns the device about the bar, so the gyro's principal direction
   over the strongest-turning 2 s is the bar axis. Its sign is arbitrary (which side of the bar the
   device is on is a setting, not a measurement).
5. Bar path: up and forward (forward = up x bar axis, made horizontal) integrated from one still to
   the next, velocity zero at both ends (the straight-line drift removed), positions carried
   across the stills. Bar tilt: asin(bar axis . up), no integration.
"""

import sys
from pathlib import Path

import numpy as np

import reps
from reps import still_mask, quat_from_to, quat_to_mat, madgwick   # D33's filter, in reps.py since D40

# Madgwick's gains and gate (BETA_STILL, BETA_MOVE, GATE_DEG) live in reps.py since D40, with the filter.
ROLL_WINDOW = 2.0        # s: the roll gesture's length
ROLL_ONE_AXIS = 0.9      # its turning must be at least this share about one axis ...
ROLL_MIN_RATE = 20.0     # deg/s: ... and this fast (rms), or it isn't a roll gesture
STILL_ACC = 0.1          # m/s^2: a still's mean world acceleration, every axis (a steady push isn't still)
STILL_VEC_STD = 0.15     # m/s^2: the accel vector's wiggle in a still window (reps.STILL_STD, but per vector:
                         # |a| alone can't see horizontal motion, D9)
STILL_GYRO = 2.0         # deg/s: the gyro's turning rate in a still window (about its median, the bias)


def find_stills(acc, gyr_deg, fs):
    """(start, end) of every still period at least reps.MIN_STILL long: the accel vector quiet (all
    three axes, so a slide along a table isn't "still" the way |a| would call it), |a| near g, and
    the gyro not turning. A sample counts once its whole centered window is quiet (no padding out:
    the bias and the strong Madgwick pull use these samples, so they must really be still)."""
    n = max(int(reps.STILL_WINDOW * fs), 2)
    mag = np.linalg.norm(acc, axis=1)
    g0 = np.median(mag)
    std = np.sqrt(sum(reps.rolling(acc[:, k], n)[1] ** 2 for k in range(3)))
    mean_mag, _ = reps.rolling(mag, n)
    turn, _ = reps.rolling(np.linalg.norm(gyr_deg - np.median(gyr_deg, axis=0), axis=1), n)
    still = (std < STILL_VEC_STD) & (np.abs(mean_mag - g0) < reps.STILL_DEV) & (turn < STILL_GYRO)
    runs, i = [], 0
    while i < len(still):
        if still[i]:
            j = i
            while j < len(still) and still[j]:
                j += 1
            if (j - i) / fs >= reps.MIN_STILL:
                runs.append((i, j - 1))
            i = j
        else:
            i += 1
    return runs


def confirm_stills(t, a_w, runs, fs):
    """The still periods the path may anchor on (v = 0): the world acceleration ~0 on every axis (a
    steady push reads quiet but isn't still), and no axis calling it a coast at steady speed
    (reps.confirm_still_runs: from the last anchor, or by speeding up into it and slowing out)."""
    flat = [(s, e) for s, e in runs if np.abs(a_w[s:e + 1].mean(axis=0)).max() < STILL_ACC]
    if not flat:
        return []
    keep = set(flat)
    for k in range(3):
        keep &= set(reps.confirm_still_runs(a_w[:, k], t, fs, flat))
    return [r for r in flat if r in keep]


def bar_axis_from_roll(t, gyr_rad, window=ROLL_WINDOW):
    """The gyro's principal direction over the strongest-turning `window` seconds (the roll gesture)."""
    fs = 1 / np.median(np.diff(t))
    n = max(int(window * fs), 2)
    power = np.convolve((gyr_rad ** 2).sum(1), np.ones(n), mode="valid")
    i = int(np.argmax(power))
    w = gyr_rad[i:i + n]
    vals, vecs = np.linalg.eigh(w.T @ w)
    # a roll turns the device about one axis, hard: anything else (handling, a lift) is no gesture
    rms = np.degrees(np.sqrt(vals.sum() / len(w)))
    if vals[-1] / vals.sum() < ROLL_ONE_AXIS or rms < ROLL_MIN_RATE:
        raise ValueError(f"no roll gesture found (the strongest-turning {window:.0f} s turns {rms:.0f} deg/s, "
                         f"{vals[-1] / vals.sum() * 100:.0f}% about one axis): pass --bar=x|y|z")
    axis = vecs[:, -1]
    k = int(np.argmax(np.abs(axis)))
    return axis * np.sign(axis[k]), (t[i], t[min(i + n, len(t) - 1)])   # sign: largest component positive


def integrate_path(t, a, runs):
    """Position along one world direction: a integrated from the middle of one still to the middle of
    the next with v = 0 at both (the straight-line velocity drift taken out). The middles, not the
    edges: a smooth move starts and ends so gently that a still's edge sits a little inside it."""
    v = np.zeros_like(a)
    x = np.zeros_like(a)
    pos = 0.0
    mids = [(s + e) // 2 for s, e in runs]
    edges = list(zip(mids[:-1], mids[1:]))
    last = 0
    for s, e in edges:
        x[last:s + 1] = pos
        seg_t = t[s:e + 1]
        vv = np.concatenate([[0.0], np.cumsum(0.5 * (a[s + 1:e + 1] + a[s:e]) * np.diff(seg_t))])
        vv -= vv[-1] * (seg_t - seg_t[0]) / (seg_t[-1] - seg_t[0])     # v back to 0 at the next still
        xx = np.concatenate([[0.0], np.cumsum(0.5 * (vv[1:] + vv[:-1]) * np.diff(seg_t))])
        v[s:e + 1], x[s:e + 1] = vv, pos + xx
        pos += xx[-1]
        last = e
    x[last:] = pos
    return x, v


def analyze(t, acc, gyr_deg, bar="roll"):
    """Everything above, for one recording: a dict of per-sample arrays and the bar axis."""
    fs = 1 / np.median(np.diff(t))
    mag = np.linalg.norm(acc, axis=1)
    runs = find_stills(acc, gyr_deg, fs)
    if len(runs) < 2:
        raise ValueError("needs at least two still periods (before and after the motion)")
    still = still_mask(len(t), runs)
    g = float(np.mean(mag[still]))
    bias = np.median(gyr_deg[still], axis=0)
    gyr = np.radians(gyr_deg - bias)
    s0, e0 = runs[0]
    q0 = quat_from_to(acc[s0:e0 + 1].mean(axis=0), np.array([0.0, 0.0, 1.0]))
    trusted = still_mask(len(t), [(s, e) for s, e in runs if t[e] - t[s] >= reps.LONG_STILL])
    # from the first still (where gravity says the attitude) forward, and backward to the start:
    # seeding sample 0 with it put any turn before that still on top of it. Backward = time and the
    # gyro negated (the trapezoid step is symmetric)
    q = np.zeros((len(t), 4))
    q[s0:] = madgwick(t[s0:], acc[s0:], gyr[s0:], still[s0:], q0, trusted[s0:])
    if s0:
        b = slice(s0, None, -1)
        q[:s0 + 1] = madgwick(-t[b], acc[b], -gyr[b], still[b], q0, trusted[b])[::-1]
    R = quat_to_mat(q)
    a_w = np.einsum("nij,nj->ni", R, acc) - np.array([0.0, 0.0, g])
    candidates, runs = runs, confirm_stills(t, a_w, runs, fs)
    if len(runs) < 2:
        raise ValueError("needs at least two confirmed still periods (before and after the motion)")
    if bar == "roll":
        bar_s, roll_span = bar_axis_from_roll(t, gyr)
    else:
        bar_s, roll_span = np.eye(3)["xyz".index(bar)], None
    up_s = R[:, 2, :]                                      # world up, seen from the sensor
    bar_w = np.einsum("nij,j->ni", R, bar_s)
    tilt = np.degrees(np.arcsin(np.clip(bar_w[:, 2], -1, 1)))
    bar_h = bar_w.copy()
    bar_h[:, 2] = 0
    bar_h /= np.linalg.norm(bar_h, axis=1, keepdims=True)
    fwd = np.cross(np.array([0.0, 0.0, 1.0]), bar_h)       # forward = up x bar axis (horizontal)
    a_up, a_fwd = a_w[:, 2], (a_w * fwd).sum(1)
    z, vz = integrate_path(t, a_up, runs)
    x, vx = integrate_path(t, a_fwd, runs)
    return {"t": t, "q": q, "up_s": up_s, "a_world": a_w, "a_up": a_up, "a_fwd": a_fwd, "up": z, "fwd": x,
            "v_up": vz, "v_fwd": vx, "tilt_deg": tilt, "bar_axis": bar_s, "roll_span": roll_span, "runs": runs,
            "candidates": candidates, "gyro_bias": bias, "g": g}


def angle_deg(u, v):
    u = u / np.linalg.norm(u, axis=-1, keepdims=True)
    v = v / np.linalg.norm(v, axis=-1, keepdims=True)
    return np.degrees(np.arccos(np.clip((u * v).sum(-1), -1, 1)))


def selftest():
    """Every make_fake_motion scenario against its truth. Pass limits (roadmap 0.3, algorithm.md
    section 11's targets): attitude within 1 degree while moving; the bar axis from the roll
    gesture within 2 degrees; vertical moves within 5 mm (the tilt and roll scenarios must match the
    flat one); forward moves within 1 cm; bar tilt within 0.5 degree."""
    import make_fake_motion as m

    ok = True

    def check(good, msg):
        nonlocal ok
        print(("PASS  " if good else "FAIL  ") + msg)
        ok = ok and bool(good)

    flat_rom = None
    for name in m.SCENARIOS:
        t, acc, gyr, truth = m.scenario(name)
        r = analyze(t, acc, gyr)
        up_true = truth["R"][:, 2, :]
        att = angle_deg(r["up_s"], up_true)
        axis_err = min(angle_deg(r["bar_axis"], np.array(m.BAR, float)), angle_deg(-r["bar_axis"], np.array(m.BAR, float)))
        # the moves: displacement between consecutive stills, estimated against true
        mids = [(s + e) // 2 for s, e in r["runs"]]
        dz = np.diff(r["up"][mids]) - np.diff(truth["pos"][mids, 2])
        sign = np.sign(np.dot(np.cross([0, 0, 1], (truth["R"][0] @ r["bar_axis"]) * [1, 1, 0]), [1, 0, 0])) or 1
        dx = np.diff(r["fwd"][mids]) * sign - np.diff(truth["pos"][mids, 0])
        bar_w = np.einsum("nij,j->ni", truth["R"], np.array(m.BAR, float))
        tilt_true = np.degrees(np.arcsin(np.clip(bar_w[:, 2], -1, 1)))
        tilt_err = np.abs(r["tilt_deg"] - tilt_true * (1 if np.dot(r["bar_axis"], m.BAR) > 0 else -1))
        moving = ~still_mask(len(t), r["runs"])
        rom = np.diff(truth["pos"][mids, 2])
        # every true still found: a dropped one hides a move (the comparison then spans two moves
        # whose net is 0, and passes). True stills: maximal runs of "still" segments, 0.6 s or more
        want, run = 0, 0.0
        for seg in m.SCENARIOS[name]() + [("move", 0)]:
            if seg[0] == "still":
                run += seg[1]
            else:
                want += run >= 0.6
                run = 0.0
        good = (att[moving].max() < 1.0 and axis_err < 2.0 and np.abs(dz).max() < 0.005 and np.abs(dx).max() < 0.01
                and tilt_err.max() < 0.5 and len(r["runs"]) >= want)
        check(good, f"{name:8s}: {len(r['runs'])} of {want} stills, attitude worst {att[moving].max():.2f} deg moving, bar axis {axis_err:.2f} deg off, "
                    f"vertical moves worst {np.abs(dz).max() * 1000:.1f} mm, forward worst {np.abs(dx).max() * 1000:.1f} mm, "
                    f"tilt worst {tilt_err.max():.2f} deg, gyro bias {np.round(r['gyro_bias'], 2)} deg/s")
        if name == "flat":
            flat_rom = np.diff(r["up"][mids])
        elif name in ("tilt", "roll") and flat_rom is not None:
            mine = np.diff(r["up"][mids])
            check(len(mine) == len(flat_rom) and np.abs(mine - flat_rom).max() < 0.005,
                  f"{name:8s}: matches the flat lift: worst {np.abs(mine - flat_rom).max() * 1000:.1f} mm apart")
    print("PASS" if ok else "FAIL")
    return ok


def main():
    if "--selftest" in sys.argv:
        sys.exit(0 if selftest() else 1)
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        sys.exit(__doc__)
    bar = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--bar=")), "roll")
    t, _ = reps.load_phyphox(Path(args[0]), "magnitude")
    acc, gyr = reps.load_imu(Path(args[0]), len(t))
    if gyr is None:
        sys.exit("this recording has no gyro (a phyphox file?): orientation needs one")
    r = analyze(t, acc, gyr, bar)
    print(f"{Path(args[0]).name}: {len(t)} samples, {len(r['runs'])} still periods, g {r['g']:.4f} m/s^2, "
          f"gyro bias {np.round(r['gyro_bias'], 3)} deg/s")
    span = f" (the roll gesture at {r['roll_span'][0]:.1f}-{r['roll_span'][1]:.1f} s)" if r["roll_span"] else ""
    print(f"bar axis (sensor frame) {np.round(r['bar_axis'], 3)}{span}")
    mids = [(s + e) // 2 for s, e in r["runs"]]
    for k in range(len(mids) - 1):
        print(f"  still {k + 1} -> {k + 2}: up {(r['up'][mids[k + 1]] - r['up'][mids[k]]) * 100:+.1f} cm, "
              f"forward {(r['fwd'][mids[k + 1]] - r['fwd'][mids[k]]) * 100:+.1f} cm, "
              f"tilt {r['tilt_deg'][mids[k + 1]]:+.1f} deg")
    if "--plot" in sys.argv:
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(1, 2, figsize=(11, 4))
        ax[0].plot(r["fwd"] * 100, r["up"] * 100)
        ax[0].set(xlabel="forward (cm)", ylabel="up (cm)", title="bar path", aspect="equal")
        ax[1].plot(t, r["tilt_deg"])
        ax[1].set(xlabel="t (s)", ylabel="bar tilt (deg)")
        plt.tight_layout()
        plt.show()


if __name__ == "__main__":
    main()
