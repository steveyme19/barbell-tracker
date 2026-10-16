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
    python reps.py data/reps_pause.zip --until=8 # only the first 8 s (what the live app saw then)
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
STILL_MAX_HUMP = 0.045  # m/s: velocity can't rise and fall more than this inside a still period
MIN_ROM = 0.05          # m: smaller up/down moves are wiggles, not reps
LONG_WINDOW = 4.0       # s: flag reps inside longer windows (more drift)
BIG_CORRECTION = 0.15   # m/s: flag windows whose drift correction was this big
LEVEL_MAX_DEG = 3       # highest power of t in the velocity drift curve (lockouts only)
LEVEL_MAX_DEG_BOTH = 5  # same, when bottoms are leveled too (twice the points, so more bend is safe)
LEVEL_FLAG = 0.02       # m: flag a rep whose lockout misses the common height by this much

# Which heights repeat every rep. Lockouts always do (that's what "locked out" means).
#   bottoms: the bottom is a hard stop too (chest, floor), not a depth you choose
#   ends:    the still before the set and after it is at lockout or floor height
LIFTS = {
    "phone":    dict(bottoms=False, ends=False),  # hand-held: you don't hold still at the turnaround height
    "squat":    dict(bottoms=False, ends=True),   # depth varies; starts and ends standing
    "bench":    dict(bottoms=True, ends=True),    # touches the chest; starts and ends at lockout
    "deadlift": dict(bottoms=True, ends=True),    # touches the floor; starts and ends on the floor
}
LIFT = "phone"          # default; --lift=bench, or put the lift in the file name


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


def velocity_hump(a, t, s, e):
    """How much velocity rises and falls inside samples s..e, after removing a straight
    line (a steady sensor bias makes a straight line, real motion makes a hump)."""
    tw = t[s:e + 1] - t[s]
    if len(tw) < 3:
        return 0.0
    v = integrate(a[s:e + 1], tw)
    v = v - np.polyval(np.polyfit(tw, v, 1), tw)
    return v.max() - v.min()


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

    Stills of LONG_STILL or more are real, unless velocity inside them rises and
    falls (a slow, smooth grinder can look still for a second or more; a coast is
    never perfectly steady).
    """
    n = int(COAST_LOOK * fs)
    confirmed = [runs[0]]               # the recording starts at rest
    for s, e in runs[1:]:
        if velocity_hump(a, t, s, e) > STILL_MAX_HUMP:
            continue
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


def level_heights(a, t, v, windows, lift=None):
    """No-pause sets: remove curved drift using heights that repeat every rep.

    The straight-line fix only knows velocity is 0 at both ends of a window. Inside a
    no-pause set we know more: every lockout is at the same height H (H unknown), and
    for bench and deadlift every bottom is too (chest, floor). For squat, bench and
    deadlift, the still before and after the set is also at lockout (or floor) height.
    So model the velocity drift as a smooth curve

        e(tau) = d1*tau + d2*tau^2 + ...     (tau = time / window length)

    with e = raw end velocity at tau = 1 (velocity is 0 at the end still), and pick the
    d's and the repeated heights by least squares. With only d1 this is exactly the
    straight-line fix. Fewer points = fewer powers (always at least one more point
    than unknowns, so the fit can't be forced through every point).

    Still moving when the data ends (live, mid-set): there's no end still, so d1 is
    fitted too, from 2+ lockouts. That's provisional until the next still.
    """
    prof = LIFTS[lift or LIFT]
    x = integrate(v, t)
    lows, highs = turning_points(x, MIN_ROM)
    for w in windows:
        i0, i1 = w["i0"], w["i1"]
        tops = [h for h in highs if i0 < h < i1]
        bottoms = [b for b in lows if i0 < b < i1]
        if not w["anchored"]:
            # only turnarounds the bar has already moved away from
            tops = [h for h in tops if x[h] - x[h:i1 + 1].min() >= MIN_ROM]
            bottoms = [b for b in bottoms if x[b:i1 + 1].max() - x[b] >= MIN_ROM]
        turns = sorted(tops + bottoms)
        if prof["ends"] and turns:
            # The bar left the start still going down: it started at lockout. Going up:
            # it started at the bottom (deadlift from the floor). Same for the end still.
            if turns[0] in bottoms and x[i0] - x[turns[0]] >= MIN_ROM:
                tops = [i0] + tops
            elif turns[0] in tops and x[turns[0]] - x[i0] >= MIN_ROM:
                bottoms = [i0] + bottoms
            if w["anchored"]:
                if turns[-1] in bottoms and x[i1] - x[turns[-1]] >= MIN_ROM:
                    tops = tops + [i1]
                elif turns[-1] in tops and x[turns[-1]] - x[i1] >= MIN_ROM:
                    bottoms = bottoms + [i1]
        groups = [tops] if len(tops) >= 2 else []
        if prof["bottoms"] and len(bottoms) >= 2:
            groups.append(bottoms)
        n_rows = sum(len(g) for g in groups)
        n_h = len(groups)
        min_rows = n_h + (2 if w["anchored"] else 1)
        if w["anchored"] and prof["ends"]:
            # A barbell rep between two stills at lockout: same height at both ends is
            # enough for one extra bend (an exact fit, no spare point to check it with).
            min_rows = n_h + 1
        if n_rows < min_rows:
            continue
        max_deg = LEVEL_MAX_DEG_BOTH if n_h == 2 else LEVEL_MAX_DEG
        tw = t[i0:i1 + 1] - t[i0]
        T = tw[-1]
        v_raw = integrate(a[i0:i1 + 1], tw)
        x_raw = integrate(v_raw, tw)
        rows, rhs = [], []
        if w["anchored"]:
            deg = min(max_deg, max(2, n_rows - n_h))
            e_end = v_raw[-1]
            # Position drift is the integral of e: T * sum d_k tau^(k+1)/(k+1).
            # Substituting d1 = e_end - d2 - ... leaves unknowns d2..d_deg and the heights.
            for gi, g in enumerate(groups):
                for h in g:
                    tau = tw[h - i0] / T
                    rows.append([T * (tau ** (k + 1) / (k + 1) - tau ** 2 / 2) for k in range(2, deg + 1)]
                                + [1.0 if j == gi else 0.0 for j in range(n_h)])
                    rhs.append(x_raw[h - i0] - T * e_end * tau ** 2 / 2)
            sol = np.linalg.lstsq(np.array(rows), np.array(rhs), rcond=None)[0]
            d = [e_end - sol[:deg - 1].sum()] + list(sol[:deg - 1])     # d1, d2, ..., d_deg
        else:
            deg = max(1, min(max_deg, n_rows - n_h - 1))               # unknowns d1..d_deg and the heights
            for gi, g in enumerate(groups):
                for h in g:
                    tau = tw[h - i0] / T
                    rows.append([T * tau ** (k + 1) / (k + 1) for k in range(1, deg + 1)]
                                + [1.0 if j == gi else 0.0 for j in range(n_h)])
                    rhs.append(x_raw[h - i0])
            sol = np.linalg.lstsq(np.array(rows), np.array(rhs), rcond=None)[0]
            d = list(sol[:deg])
        tau = tw / T
        v[i0:i1 + 1] = v_raw - sum(dk * tau ** k for k, dk in enumerate(d, start=1))
        w["leveled"] = True
        if groups[0] is tops:
            w["top_height"] = sol[len(sol) - n_h]       # lockout height, relative to the height at i0
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
                    if "top_height" in w and abs(x[hi] - (x[w["i0"]] + w["top_height"])) > LEVEL_FLAG:
                        flags.append("uneven lockout")
                elif w["dur"] > LONG_WINDOW:
                    flags.append(f"long window {w['dur']:.1f}s")
                if w["anchored"] and abs(w["raw_end"]) > BIG_CORRECTION:
                    flags.append("big drift correction")
        leveled = any(w.get("leveled") and w["i0"] <= end and w["i1"] >= start for w in windows)
        reps.append(dict(start=start, end=end, rom=rom, dur=dur, mcv=rom / dur,
                         peak=v[start:end + 1].max(), leveled=leveled, flags=sorted(set(flags))))
    return reps


def lift_from_name(name):
    """'bar-bench-2026-11-03-1830.csv' -> 'bench'. Anything else -> the default LIFT."""
    for lift in LIFTS:
        if lift in name.lower():
            return lift
    return LIFT


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        sys.exit(__doc__)
    path = Path(args[0])
    as_json = "--json" in sys.argv
    until = next((float(a.split("=", 1)[1]) for a in sys.argv if a.startswith("--until=")), None)
    lift = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--lift=")), None) or lift_from_name(path.name)
    t, a_raw = load_phyphox(path, AXIS)
    if until is not None:
        keep = (t - t[0]) < until
        t, a_raw = t[keep], a_raw[keep]
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
    v = level_heights(a, t, v, windows, lift)
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
