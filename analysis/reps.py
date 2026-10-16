"""
Step 0.2: split a recording of several reps into individual reps.

For each rep (each upward move of at least MIN_ROM) it reports range of motion,
concentric time, mean concentric velocity (MCV) and peak velocity.

How it works (docs/algorithm.md sections 5 to 8):
  1. Find the still periods. These are the drift anchors.
  2. Integrate acceleration between each pair of still periods, then remove the
     straight-line drift so velocity is zero at both anchors.
  3. No-pause sets (3+ lockouts between two still periods): also remove curved
     drift by assuming every lockout is at the same height (section 6b).
  4. Integrate velocity to height and find the turnarounds (bottoms and tops).
  5. Each bottom-to-top stretch is one rep.

Usage:
    python reps.py data/reps_pause.zip
    python reps.py data/reps_pause.zip --json    # results only, for the JS parity test
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
LONG_STILL = 1.0        # s: stills at least this long are real (nothing coasts dead steady that long)
COAST_TRUST = 4.0       # s: past this long since the last anchor, drift is too big for the speed check
COAST_LOOK = 0.5        # s: how far before and after a "still" period to look for speeding up / slowing down
COAST_DV = 0.1          # m/s: speed change on both sides that marks a coast
MIN_ROM = 0.05          # m: smaller up/down moves are wiggles, not reps
LONG_WINDOW = 4.0       # s: flag reps inside longer windows (more drift)
BIG_CORRECTION = 0.15   # m/s: flag windows whose drift correction was this big
LEVEL_MIN_TOPS = 3      # lockouts needed inside one window before we level them
LEVEL_MAX_DEG = 3       # highest power of t in the velocity drift curve
LEVEL_FLAG = 0.02       # m: flag a rep whose lockout misses the common height by this much


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


def speed_change(a, t, i, j):
    """Change in velocity from sample i to sample j (clipped to the recording)."""
    i, j = max(i, 0), min(j, len(t) - 1)
    if j <= i:
        return 0.0
    return integrate(a[i:j + 1], t[i:j + 1] - t[i])[-1]


def confirm_still_runs(a, t, fs, runs):
    """Throw out "still" periods that are really the phone coasting at steady speed.

    An accelerometer can't tell resting from moving at constant velocity: both
    read zero acceleration. Two checks, depending on how much drift there could be:

    - Last anchor was recent (< COAST_TRUST): integrate from it. Real stillness
      shows up as ~0 velocity, coasting as a clear speed.
    - Last anchor was long ago: drift alone can be bigger than a coast, so look
      locally instead. A coast is entered by speeding up and left by slowing down
      (or the reverse on the way down). A real stop is entered and left the same
      way (stop at the top, then go down) or from/to rest.

    Stills of LONG_STILL or more are always real.
    """
    n = int(COAST_LOOK * fs)
    confirmed = [runs[0]]               # the recording starts at rest
    for s, e in runs[1:]:
        if t[e] - t[s] >= LONG_STILL:
            confirmed.append((s, e))
            continue
        i0 = confirmed[-1][1]
        if t[s] - t[i0] < COAST_TRUST:
            tw = t[i0:e + 1] - t[i0]
            v_raw = integrate(a[i0:e + 1], tw)
            still = np.abs(v_raw[s - i0:]).mean() < STILL_MAX_SPEED
        else:
            before = speed_change(a, t, s - n, s)
            after = speed_change(a, t, e, e + n)
            still = not (before * after < 0 and min(abs(before), abs(after)) > COAST_DV)
        if still:
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


def level_tops(a, t, v, windows):
    """No-pause sets: remove curved drift by assuming every lockout is at the same height.

    The straight-line fix only knows velocity is 0 at both ends of a window. With
    3+ lockouts inside a window we know more: the bar got back to the same height
    H each time (H unknown). So model the velocity drift as a smooth curve

        e(tau) = d1*tau + d2*tau^2 + d3*tau^3     (tau = time / window length)

    with e = raw end velocity at tau = 1 (velocity is 0 at the end still), and pick
    d2, d3 and H by least squares so every lockout lands at H. With d2 = d3 = 0 this
    is exactly the straight-line fix. Fewer lockouts = fewer powers (always one
    more lockout than unknowns, so the fit can't be forced through every point).
    """
    x = integrate(v, t)
    _, highs = turning_points(x, MIN_ROM)
    for w in windows:
        i0, i1 = w["i0"], w["i1"]
        tops = [h for h in highs if i0 < h < i1]
        if not w["anchored"] or len(tops) < LEVEL_MIN_TOPS:
            continue
        deg = min(LEVEL_MAX_DEG, len(tops) - 1)
        tw = t[i0:i1 + 1] - t[i0]
        T = tw[-1]
        v_raw = integrate(a[i0:i1 + 1], tw)
        x_raw = integrate(v_raw, tw)
        e_end = v_raw[-1]
        # Position drift is the integral of e: T * sum d_k tau^(k+1)/(k+1).
        # Substituting d1 = e_end - d2 - d3 leaves unknowns d2..d_deg and H.
        rows, rhs = [], []
        for h in tops:
            tau = tw[h - i0] / T
            rows.append([T * (tau ** (k + 1) / (k + 1) - tau ** 2 / 2) for k in range(2, deg + 1)] + [1.0])
            rhs.append(x_raw[h - i0] - T * e_end * tau ** 2 / 2)
        sol = np.linalg.lstsq(np.array(rows), np.array(rhs), rcond=None)[0]
        d = [e_end - sol[:-1].sum()] + list(sol[:-1])           # d1, d2, ..., d_deg
        tau = tw / T
        v[i0:i1 + 1] = v_raw - sum(dk * tau ** k for k, dk in enumerate(d, start=1))
        w["leveled"] = True
        w["top_height"] = sol[-1]           # H, relative to the height at i0
    return v


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
                if w.get("leveled"):
                    if abs(x[hi] - (x[w["i0"]] + w["top_height"])) > LEVEL_FLAG:
                        flags.append("uneven lockout")
                elif w["dur"] > LONG_WINDOW:
                    flags.append(f"long window {w['dur']:.1f}s")
                if w["anchored"] and abs(w["raw_end"]) > BIG_CORRECTION:
                    flags.append("big drift correction")
        leveled = any(w.get("leveled") and w["i0"] <= end and w["i1"] >= start for w in windows)
        reps.append(dict(start=start, end=end, rom=rom, dur=dur, mcv=rom / dur,
                         peak=v[start:end + 1].max(), leveled=leveled, flags=sorted(set(flags))))
    return reps


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        sys.exit(__doc__)
    path = Path(args[0])
    as_json = "--json" in sys.argv
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
    runs = confirm_still_runs(a, t, fs, runs)
    if len(runs) < n_candidates and not as_json:
        print(f"(ignored {n_candidates - len(runs)} 'still' periods that were really steady motion)")

    v, windows = velocity_between_anchors(a, t, fs, runs)
    v = level_tops(a, t, v, windows)
    x = integrate(v, t)
    reps = find_reps(x, v, t, windows)

    if as_json:
        import json
        print(json.dumps({
            "fs": float(fs), "g": float(g), "runs": [[int(s), int(e)] for s, e in runs],
            "reps": [{"start": int(r["start"]), "end": int(r["end"]), "rom": float(r["rom"]),
                      "dur": float(r["dur"]), "mcv": float(r["mcv"]), "peak": float(r["peak"]),
                      "leveled": r["leveled"], "flags": r["flags"]} for r in reps],
        }))
        return

    print(f"{path.name}: {t[-1]:.1f} s at {fs:.0f} Hz, g = {g:.3f} m/s^2")
    n_lev = sum(1 for w in windows if w.get("leveled"))
    print(f"{len(runs)} still periods, {len(windows)} integration windows "
          f"(longest {max(w['dur'] for w in windows):.1f} s, {n_lev} leveled)\n" if windows else "")
    print(" rep |  ROM   | up time |  MCV   | peak v | flags  (L = lockouts leveled)")
    for k, r in enumerate(reps, 1):
        print(f" {k:3d} | {r['rom'] * 100:5.1f}cm | {r['dur']:5.2f} s | {r['mcv']:.2f}   | "
              f"{r['peak']:.2f}   | {'L ' if r['leveled'] else ''}{', '.join(r['flags'])}")
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
