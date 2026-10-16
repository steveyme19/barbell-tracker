"""
Step 0.2: split a recording of several reps into individual reps.

For each rep (each upward move of at least MIN_ROM) it reports range of motion,
concentric time, mean concentric velocity (MCV) and peak velocity.

How it works (docs/algorithm.md sections 5 to 8):
  1. Find the still periods. These are the drift anchors.
  2. Integrate acceleration between each pair of still periods, then remove the
     straight-line drift so velocity is zero at both anchors.
  3. Integrate velocity to height and find the turnarounds (bottoms and tops).
  4. Each bottom-to-top stretch is one rep.

Usage:
    python reps.py data/reps_pause.zip
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

from phone_lift import load_phyphox, integrate

# ---- Settings ----
AXIS = "magnitude"      # tilt-proof vertical signal for a phone with no gyro
STILL_WINDOW = 0.2      # s: window for the stillness test
STILL_STD = 0.15        # m/s^2: max wiggle inside the window (hand-held pauses wiggle a bit)
STILL_DEV = 0.3         # m/s^2: max |average - g| inside the window
MIN_STILL = 0.25        # s: a still period has to last at least this long
STILL_MAX_SPEED = 0.1   # m/s: faster than this during a "still" period = coasting, not still
MIN_ROM = 0.05          # m: smaller up/down moves are wiggles, not reps
LONG_WINDOW = 4.0       # s: flag reps inside longer windows (more drift)
BIG_CORRECTION = 0.15   # m/s: flag windows whose drift correction was this big


def rolling(y, n):
    """Centered rolling mean and standard deviation over n samples."""
    s = pd.Series(y)
    r = s.rolling(n, center=True, min_periods=max(n // 2, 1))
    return r.mean().to_numpy(), r.std().fillna(0).to_numpy()


def find_still_runs(a_raw, g, fs):
    """(start, end) sample indices of every still period at least MIN_STILL long."""
    n = int(STILL_WINDOW * fs)
    mean, std = rolling(a_raw, n)
    still = (std < STILL_STD) & (np.abs(mean - g) < STILL_DEV)
    runs, i = [], 0
    while i < len(still):
        if still[i]:
            j = i
            while j < len(still) and still[j]:
                j += 1
            if (j - i) / fs >= MIN_STILL:
                # A sample only counts as still once its whole window is quiet,
                # so the quiet stretch really starts/ends half a window further out.
                runs.append((max(i - n // 2, 0), min(j - 1 + n // 2, len(still) - 1)))
            i = j
        else:
            i += 1
    return runs


def confirm_still_runs(a, t, runs):
    """Throw out "still" periods that are really the phone coasting at steady speed.

    An accelerometer can't tell resting from moving at constant velocity: both
    read zero acceleration. So check: integrating from the last confirmed still
    period, is the phone actually going (almost) nowhere during this one? Over a
    few seconds drift is small, so real stillness shows up as ~0 velocity and
    coasting shows up as a clear speed.
    """
    confirmed = [runs[0]]               # the recording starts at rest
    for s, e in runs[1:]:
        i0 = confirmed[-1][1]
        tw = t[i0:e + 1] - t[i0]
        v_raw = integrate(a[i0:e + 1], tw)
        if np.abs(v_raw[s - i0:]).mean() < STILL_MAX_SPEED:
            confirmed.append((s, e))
    return confirmed


def velocity_between_anchors(a, t, fs, runs):
    """Velocity with drift removed between every pair of still periods.

    Still periods stay at exactly 0. Motion after the last still period has no
    anchor at its end, so it's integrated without correction and flagged.
    """
    v = np.zeros_like(a)
    windows = []
    for (_, end0), (start1, _) in zip(runs[:-1], runs[1:]):
        i0, i1 = end0, start1
        if i1 - i0 < 2:
            continue
        tw = t[i0:i1 + 1] - t[i0]
        v_raw = integrate(a[i0:i1 + 1], tw)
        v[i0:i1 + 1] = v_raw - v_raw[-1] * tw / tw[-1]   # straight-line drift removal
        windows.append(dict(i0=i0, i1=i1, dur=tw[-1], raw_end=v_raw[-1], anchored=True))
    last = runs[-1][1]
    tail = a[last:]
    if len(tail) > 0.2 * fs and np.abs(tail).max() > 0.5:   # still moving when recording stopped
        tw = t[last:] - t[last]
        v[last:] = integrate(tail, tw)
        windows.append(dict(i0=last, i1=len(t) - 1, dur=tw[-1], raw_end=np.nan, anchored=False))
    return v, windows


def turning_points(x, min_move):
    """Lows and highs where the height reverses by at least min_move ("zigzag")."""
    lows, highs = [], []
    trend = 0          # 0 unknown, +1 going up, -1 going down
    lo = hi = 0        # lowest / highest point since the last turn
    for i in range(1, len(x)):
        if x[i] < x[lo]:
            lo = i
        if x[i] > x[hi]:
            hi = i
        if trend <= 0 and x[i] - x[lo] >= min_move:    # rose far enough: lo was a bottom
            lows.append(lo)
            trend, hi = 1, i
        elif trend >= 0 and x[hi] - x[i] >= min_move:  # fell far enough: hi was a top
            highs.append(hi)
            trend, lo = -1, i
    if trend == 1:
        highs.append(hi)
    elif trend == -1:
        lows.append(lo)
    return lows, highs


def find_reps(x, v, t, windows):
    """One rep per bottom-to-top move. Boundaries come from v = 0, never a threshold."""
    lows, highs = turning_points(x, MIN_ROM)
    reps = []
    for lo in lows:
        later = [h for h in highs if h > lo]
        if not later:
            continue
        hi = later[0]
        seg = np.arange(lo, hi + 1)
        # start: last moment the bar wasn't moving up while still near the bottom
        cand = seg[(v[seg] <= 0) & (x[seg] <= x[lo] + MIN_ROM)]
        start = cand[-1] if len(cand) else lo
        # end: first moment it stops moving up once it's near the top
        cand = seg[(seg > start) & (v[seg] <= 0) & (x[seg] >= x[hi] - MIN_ROM)]
        end = cand[0] if len(cand) else hi
        rom = x[end] - x[start]
        dur = t[end] - t[start]
        flags = []
        for w in windows:
            if w["i0"] <= end and w["i1"] >= start:
                if not w["anchored"]:
                    flags.append("no end anchor")
                if w["dur"] > LONG_WINDOW:
                    flags.append(f"long window {w['dur']:.1f}s")
                if w["anchored"] and abs(w["raw_end"]) > BIG_CORRECTION:
                    flags.append("big drift correction")
        reps.append(dict(start=start, end=end, rom=rom, dur=dur, mcv=rom / dur,
                         peak=v[start:end + 1].max(), flags=sorted(set(flags))))
    return reps


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    path = Path(sys.argv[1])
    t, a_raw = load_phyphox(path, AXIS)
    t = t - t[0]
    fs = 1 / np.median(np.diff(t))

    # Gravity: rough guess from the first 0.3 s, then refine using every still period.
    g = np.median(a_raw[t < 0.3])
    runs = find_still_runs(a_raw, g, fs)
    if not runs:
        sys.exit("No still periods found. Start and end the recording with the phone still.")
    g = np.mean(np.concatenate([a_raw[s:e + 1] for s, e in runs]))
    runs = find_still_runs(a_raw, g, fs)
    a = a_raw - g
    n_candidates = len(runs)
    runs = confirm_still_runs(a, t, runs)
    if len(runs) < n_candidates:
        print(f"(ignored {n_candidates - len(runs)} 'still' periods that were really steady motion)")

    v, windows = velocity_between_anchors(a, t, fs, runs)
    x = integrate(v, t)
    reps = find_reps(x, v, t, windows)

    print(f"{path.name}: {t[-1]:.1f} s at {fs:.0f} Hz, g = {g:.3f} m/s^2")
    print(f"{len(runs)} still periods, {len(windows)} integration windows "
          f"(longest {max(w['dur'] for w in windows):.1f} s)\n" if windows else "")
    print(" rep |  ROM   | up time |  MCV   | peak v | flags")
    for k, r in enumerate(reps, 1):
        print(f" {k:3d} | {r['rom'] * 100:5.1f}cm | {r['dur']:5.2f} s | {r['mcv']:.2f}   | "
              f"{r['peak']:.2f}   | {', '.join(r['flags'])}")
    if reps:
        roms = np.array([r["rom"] for r in reps]) * 100
        mcvs = np.array([r["mcv"] for r in reps])
        print(f"\nROM {roms.mean():.1f} cm average, spread (std) {roms.std():.1f} cm")
        print(f"Velocity loss, best rep to last rep: {(1 - mcvs[-1] / mcvs.max()) * 100:.0f}%")

    # ---- Plot ----
    fig, (p1, p2, p3) = plt.subplots(3, 1, sharex=True, figsize=(11, 8))
    p1.plot(t, a, lw=0.6)
    for s, e in runs:
        p1.axvspan(t[s], t[e], color="green", alpha=0.15)
    p1.set_ylabel("accel (m/s²)")
    p1.set_title(f"{path.name}  (green = still periods used as anchors)")
    p2.plot(t, v)
    p2.axhline(0, color="k", lw=0.5)
    p2.set_ylabel("velocity (m/s)")
    p3.plot(t, x * 100)
    for k, r in enumerate(reps, 1):
        p3.axvspan(t[r["start"]], t[r["end"]], color="orange", alpha=0.25)
        p3.annotate(f"{k}: {r['rom'] * 100:.0f} cm\n{r['mcv']:.2f} m/s",
                    (t[r["end"]], x[r["end"]] * 100), fontsize=8, ha="right", va="bottom")
    p3.set_ylabel("height (cm)")
    p3.set_xlabel("time (s)")
    fig.tight_layout()
    out_dir = Path(__file__).parent / "out"
    out_dir.mkdir(exist_ok=True)
    png = out_dir / (path.stem + "_reps.png")
    fig.savefig(png, dpi=110)
    print(f"Plot saved to {png}")
    plt.show()


if __name__ == "__main__":
    main()
