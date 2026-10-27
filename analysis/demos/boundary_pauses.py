"""
Demo: rep boundaries and pauses that aren't stills (D32, algorithm.md section 8).

A short hold at lockout (or on the chest) that's too short or too shaky to count as a still isn't
an anchor, so the velocity there is left-over drift: true 0, read a hair above 0. A boundary at
"v = 0" then runs through the whole pause and the rep's duration takes it in: MCV too low. The fix
(V_STOP_FRAC) looks for the boundary where v <= 3% of the rep's own peak and then walks on while v
keeps falling: all the way to v = 0 when it gets there (the same boundary as before, no threshold
bias), or to where the pause's drift floor begins. Compare a plain threshold at 3% of the peak:
always +1 to +6% on clean reps (it cuts the tails, demos/threshold_bias.py's problem, scale-free).

Part 1, known velocity curves (no still detection, like threshold_bias.py): a 0.40 m concentric,
smooth, with a mid-rep sticking dip, or with a lockout stall (15% of the peak for the last 3 cm),
then a 0.4 s pause; the drift d rides on the rep and the pause. MCV error against the truth.
Part 2, every file the parity test reads: what changes, and what the bar did between the two
boundaries (moving: the walk cut real motion; sitting still: the old rule swallowed a pause).

Usage (from the analysis folder):
    python demos/boundary_pauses.py
"""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import reps as R

FS = 208.0
ROM = 0.40


def rep_curve(mcv, kind, d):
    T = ROM / mcv
    n = int(T * FS)
    u = np.arange(n) / n
    shape = np.sin(np.pi * u)
    if kind == "dip":
        shape *= 1 - 0.7 * np.exp(-((u - 0.5) / 0.08) ** 2)
    v_up = shape * ROM / (shape.sum() / FS)
    if kind == "stall":
        x_up = np.cumsum(v_up) / FS
        k = np.searchsorted(x_up, ROM - 0.03)
        tail = np.full(int(0.03 / (0.15 * v_up.max()) * FS), 0.15 * v_up.max())
        v_up = np.concatenate([v_up[:k], tail, np.linspace(0.15 * v_up.max(), 0, int(0.15 * FS))])
        v_up *= ROM / (v_up.sum() / FS)
    truth = ROM / (len(v_up) / FS)
    pause, still = np.zeros(int(0.4 * FS)), np.zeros(int(0.5 * FS))
    nd = int(1.0 * FS)
    down = -np.sin(np.pi * np.arange(nd) / nd) * ROM * np.pi / 2
    v = np.concatenate([still, v_up, pause, down, still])
    v[len(still):len(still) + len(v_up) + len(pause)] += d          # the left-over drift
    x = np.concatenate([[0.0], np.cumsum(v[:-1]) / FS])
    return np.arange(len(v)) / FS, x, v, truth


def threshold_only(x, v, t, f):
    """A plain threshold at f x peak (no walk), for comparison."""
    lows, highs = R.turning_points(x, R.MIN_ROM)
    lo = lows[0]
    hi = next(h for h in highs if h > lo)
    seg = np.arange(lo, hi + 1)
    vz = f * v[seg].max()
    start = seg[(v[seg] <= vz) & (x[seg] <= x[lo] + R.MIN_ROM)][-1]
    end = seg[(seg > start) & (v[seg] <= vz) & (x[seg] >= x[hi] - R.MIN_ROM)][0]
    return (x[end] - x[start]) / (t[end] - t[start])


print("Part 1: MCV error vs truth on known curves (v = 0 | 3% threshold | 3% + walk, D32)\n")
for kind in ("smooth", "dip", "stall"):
    for d in (0.0, 0.005, 0.015):
        print(f"{kind}, left-over drift +{d} m/s")
        for mcv in (1.0, 0.5, 0.3, 0.2, 0.15, 0.10):
            t, x, v, truth = rep_curve(mcv, kind, d)
            old = R.find_reps(x, v, t, [], None, 0.0)[0]["mcv"]
            new = R.find_reps(x, v, t, [], None, R.V_STOP_FRAC)[0]["mcv"]
            thr = threshold_only(x, v, t, R.V_STOP_FRAC)
            err = lambda m: f"{(m - truth) / truth * 100:+6.1f}%"
            print(f"  MCV {mcv:.2f}: v=0 {err(old)} | threshold {err(thr)} | walk {err(new)}")

print("\nPart 2: the parity test's files, per rep (only where the walk moves a boundary)\n")
data = Path(__file__).resolve().parent.parent / "data"
files = ["reps_pause.zip", "bar-squat-2026-10-16-2131.csv", "bar-machine-2026-10-17-1334-set01-lat-raise.csv",
         "bar-deadlift-2026-10-23-1857-set01.csv", "bar-squat-2026-10-16-2134-browser.csv", "bar-2026-10-16-1844.csv"]
for f in files:
    p = data / f
    if not p.exists():
        print(f"skip {f} (not found)")
        continue
    t, a_raw = R.load_phyphox(p, R.AXIS)
    acc, gyro = R.load_imu(p, len(t))
    r = R.analyze(t, a_raw, R.lift_from_name(p.name), acc, gyro)
    if r["lift"] == "phone":
        print(f"{f}: lift 'phone' (a hand-held test): the walk is off for it, nothing changes")
        continue
    if r["fs"] < R.WALK_MIN_FS:
        print(f"{f}: {r['fs']:.0f} Hz, under WALK_MIN_FS: the walk is off for it, nothing changes "
              f"(with it on, reps 1-2 read +51% and +34%, against the same squats at 402 Hz)")
        continue
    x, v = r["x"], r["v"]
    old = R.find_reps(x, v, t, r["windows"], R.usual_bottom(x, r["lift"]), 0.0)
    print(f"{f} ({1 / np.median(np.diff(t)):.0f} Hz, {r['lift']}):")
    for k, (a, b) in enumerate(zip(old, r["reps"]), 1):
        moves = [(side, i, j) for side, i, j in (("start", b["start"], a["start"]), ("end", b["end"], a["end"])) if i != j]
        if not moves:
            continue
        what = "; ".join(f"{side} {abs(t[j] - t[i]):.2f} s, the bar moved {abs(x[max(i, j)] - x[min(i, j)]) * 1000:.1f} mm there"
                         for side, i, j in moves)
        print(f"  rep {k}: MCV {a['mcv']:.3f} -> {b['mcv']:.3f} ({(b['mcv'] - a['mcv']) / a['mcv'] * 100:+.0f}%): {what}")
