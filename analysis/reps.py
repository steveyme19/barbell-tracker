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
Which heights repeat (step 3) depends on the lift. Squat, bench and deadlift come from the
name; otherwise the gyro decides machine or free weight (D21, detect_mount()).

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

from phone_lift import load_phyphox, read_phyphox, integrate

# ---- Settings ----
AXIS = "magnitude"      # tilt-proof vertical signal for a phone with no gyro
STILL_WINDOW = 0.2      # s: window for the stillness test
STILL_STD = 0.15        # m/s^2: max wiggle inside the window (hand-held pauses wiggle a bit)
STILL_DEV = 0.3         # m/s^2: max |average - g| inside the window
# Noise-matched stillness (D20): a sensor sitting on a weight stack is ~10x quieter at rest
# than a phone in your hand, and a smooth cable lowering wiggles about as much as a hand-held
# pause. So the limits shrink to a few times the wiggle of the longest rest in the recording.
NOISE_STD_K = 4.0       # wiggle limit = 4x the rest wiggle ...
NOISE_DEV_K = 5.0       # ... and |average - g| limit = 5x it,
STILL_STD_MIN = 0.06    # m/s^2   but never below these
STILL_DEV_MIN = 0.08    # m/s^2   (and never above STILL_STD / STILL_DEV)
MIN_STILL = 0.25        # s: a still period has to last at least this long
STILL_MAX_SPEED = 0.1   # m/s: faster than this during a "still" period = coasting, not still
LONG_STILL = 1.0        # s: stills at least this long are real (nothing coasts dead steady that long)
COAST_TRUST = 4.0       # s: past this long since the last anchor, drift is too big for the speed check
COAST_LOOK = 0.5        # s: how far before and after a "still" period to look for speeding up / slowing down
COAST_DV = 0.1          # m/s: speed change on both sides that marks a coast
STILL_MAX_HUMP = 0.12   # m/s: velocity can't rise and fall more than this inside a still period (D15)
MIN_ROM = 0.05          # m: smaller up/down moves are wiggles, not reps
V_ZERO = 1e-9           # m/s: "not moving up". Drift-corrected anchors are 0 up to roundoff (±1e-17)
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
    "machine":  dict(bottoms=True, ends=True),    # weight stack: comes to rest on the stack every rep (D19)
    "free":     dict(bottoms=False, ends=True),   # a free weight with no name we know (row, OHP, curl): lockouts only (D21)
}
LIFT = "phone"          # default; --lift=bench, or put the lift in the file name

# Machine or free weight, from how the sensor moves (D21). A weight stack runs on guide
# rods, so it goes straight up and down without turning. A bar tilts, rolls and drifts
# forward and back; a hand does all of that a lot.
MOUNT_MIN_MOVE = 1.0    # s: need this much motion between the stills to judge
MACHINE_ROT = 3.0       # deg/s: median turning speed while moving. Stack ~0.7, hand 10 to 25
MACHINE_SIDE = 0.35     # sideways / vertical acceleration while moving. Stack ~0.2, hand 0.36 to 2
NAMED_WINS = ("phone", "squat", "bench", "deadlift")   # these names say more than the sensor can


def rolling(y, n):
    """Centered rolling mean and standard deviation over n samples."""
    s = pd.Series(y)
    r = s.rolling(n, center=True, min_periods=max(n // 2, 1))
    return r.mean().to_numpy(), r.std().fillna(0).to_numpy()


def find_still_runs(a_raw, g, fs, std_max=STILL_STD, dev_max=STILL_DEV):
    """(start, end) sample indices of every still period at least MIN_STILL long."""
    n = int(STILL_WINDOW * fs)
    mean, std = rolling(a_raw, n)
    still = (std < std_max) & (np.abs(mean - g) < dev_max)
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


def still_limits(a_raw, fs, runs):
    """Noise-matched limits: median wiggle inside the longest still, times NOISE_*_K,
    clamped between the *_MIN floors and the default limits."""
    n = int(STILL_WINDOW * fs)
    _, std = rolling(a_raw, n)
    s, e = max(runs, key=lambda r: r[1] - r[0])
    noise = float(np.median(std[s:e + 1]))
    std_max = min(STILL_STD, max(STILL_STD_MIN, NOISE_STD_K * noise))
    dev_max = min(STILL_DEV, max(STILL_DEV_MIN, NOISE_DEV_K * noise))
    return std_max, dev_max, noise


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


def find_reps(x, v, t, windows, bottom_ref=None):
    """One rep per bottom-to-top move. Boundaries come from v = 0, never a threshold.

    bottom_ref: the usual bottom height when the lift has one (bench chest, deadlift floor,
    machine stack). A rep may then start from a stop near that height even if the move
    began lower, which cuts a setup move off the first rep (taking up the cable before
    rep 1 lifts the stack a few cm first)."""
    lows, highs = turning_points(x, MIN_ROM)
    reps = []
    for lo in lows:
        later = [h for h in highs if h > lo]
        if not later:
            continue
        hi = later[0]
        seg = np.arange(lo, hi + 1)
        # start: last moment the bar wasn't moving up while still near the bottom
        base = x[lo] if bottom_ref is None else max(x[lo], bottom_ref)
        cand = seg[(v[seg] <= V_ZERO) & (x[seg] <= base + MIN_ROM)]
        start = cand[-1] if len(cand) else lo
        # end: first moment it stops moving up once it's near the top
        cand = seg[(seg > start) & (v[seg] <= V_ZERO) & (x[seg] >= x[hi] - MIN_ROM)]
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


def usual_bottom(x, lift):
    """Median height of the turnaround bottoms, for lifts whose bottom repeats; else None."""
    if not LIFTS[lift]["bottoms"]:
        return None
    lows, _ = turning_points(x, MIN_ROM)
    return float(np.median(x[lows])) if len(lows) >= 3 else None


def detect_mount(acc, gyro, runs, fs):
    """'machine' (weight stack, Smith, sled on rails) or 'free' (bar, dumbbell, hand), from
    the motion after the first still (D21). None if there's no gyro data or too little
    motion to judge yet.

    rot:  median turning speed while moving (deg/s), after taking off the gyro's offset
          (its average during the stills). Median, so a bump or setup move doesn't count.
    side: rms sideways acceleration / rms vertical acceleration while moving. Smoothed over
          STILL_WINDOW first: a stack rattles on its rods but can't travel sideways.

    How still the sensor is at rest doesn't tell them apart: a racked bar or a phone put
    down on a bench is as quiet as a stack (bar-phone-2123/2206)."""
    if acc is None or gyro is None or not np.any(gyro):
        return None
    still = np.zeros(len(acc), bool)
    for s, e in runs:
        still[s:e + 1] = True
    moving = ~still
    moving[:runs[0][1] + 1] = False         # before the first still: putting the sensor on
    if moving.sum() < MOUNT_MIN_MOVE * fs:
        return None
    turn = np.linalg.norm(gyro - gyro[still].mean(axis=0), axis=1)
    rot = float(np.median(turn[moving]))
    up = acc[still].mean(axis=0)            # gravity as the sensor sees it at rest
    g = np.linalg.norm(up)
    up = up / g
    n = int(STILL_WINDOW * fs)
    a = np.column_stack([rolling(acc[:, k], n)[0] for k in range(3)])
    along = a @ up
    sideways = np.linalg.norm(a - np.outer(along, up), axis=1)
    side = float(np.sqrt(np.mean(sideways[moving] ** 2) / np.mean((along[moving] - g) ** 2)))
    kind = "machine" if rot < MACHINE_ROT and side < MACHINE_SIDE else "free"
    return dict(kind=kind, rot=rot, side=side)


def choose_lift(named, mount):
    """The profile a set is tracked with (D21). Squat, bench, deadlift and phone by name
    win, since they say more than the sensor can (a Smith squat rides on rods, but you
    still pick the depth). Anything else (machine from an unknown exercise name, free, no
    name) goes by what the sensor saw, or by the name if the sensor couldn't tell."""
    if named in NAMED_WINS:
        return named
    if mount:
        return mount["kind"]
    return named or "free"


def load_imu(path, n=None):
    """3-axis acceleration and gyro (n x 3 arrays, first n rows) from a recording, or None
    for whichever the file doesn't have."""
    df = read_phyphox(path)
    def cols(prefix):
        names = [next((c for c in df.columns if c.startswith(f"{prefix} {k}")), None) for k in "xyz"]
        return None if None in names else df[names].to_numpy(float)[:n]
    return cols("Acceleration"), cols("Gyro")


def lift_from_name(name):
    """'bar-bench-2026-11-03-1830.csv' -> 'bench'. The word right after 'bar-' wins (session
    files end in the exercise name, e.g. bar-machine-...-set02-lat-pulldown.csv); else any
    lift name in it; else the default LIFT."""
    lower = Path(name).name.lower()
    if lower.startswith("bar-") and lower[4:].split("-", 1)[0] in LIFTS:
        return lower[4:].split("-", 1)[0]
    for lift in LIFTS:
        if lift in lower:
            return lift
    return LIFT


def lift_for_exercise(exercise):
    """Typed exercise name -> tracking profile (D19). Squat, bench and deadlift by name
    (front squat, pause bench...); "phone" for a hand-held test; anything else "machine"
    (lat pulldown, cable row, leg curl...), which only counts when the sensor can't tell
    machine from free weight (choose_lift, D21)."""
    lower = exercise.lower()
    for lift in ("phone", "deadlift", "bench", "squat"):
        if lift in lower:
            return lift
    return "machine"


def analyze(t, a_raw, lift=LIFT, acc=None, gyro=None):
    """The whole per-set pipeline (same as analyze() in webapp/reps.js). t in seconds
    (any start), a_raw = vertical signal in m/s^2 incl. gravity. lift = the profile the
    name or picker gave (choose_lift() has the last word). acc, gyro: n x 3 arrays, for
    telling a machine from a free weight (D21); without them the name decides. Raises
    ValueError if there's no still period to anchor on."""
    t = t - t[0]
    fs = 1 / np.median(np.diff(t))

    # Gravity: rough guess from the first 0.3 s, then refine using every still period.
    g = np.median(a_raw[t < 0.3])
    runs = find_still_runs(a_raw, g, fs)
    if not runs:
        raise ValueError("No still periods found. Start and end the recording with the phone still.")
    g = np.mean(np.concatenate([a_raw[s:e + 1] for s, e in runs]))
    runs = find_still_runs(a_raw, g, fs)
    # Noise-matched limits (D20). Only when they're tighter than the defaults (a sensor on
    # a stack or a bar, not a hand), then gravity again from the stills that survive them.
    std_max, dev_max, noise = still_limits(a_raw, fs, runs)
    if std_max < STILL_STD or dev_max < STILL_DEV:
        runs = find_still_runs(a_raw, g, fs, std_max, dev_max)
        if not runs:
            raise ValueError("No still periods found. Start and end the recording with the phone still.")
        g = np.mean(np.concatenate([a_raw[s:e + 1] for s, e in runs]))
        runs = find_still_runs(a_raw, g, fs, std_max, dev_max)
        if not runs:
            raise ValueError("No still periods found. Start and end the recording with the phone still.")
    a = a_raw - g
    n_candidates = len(runs)
    runs = confirm_still_runs(a, t, fs, runs)
    # Machine or free weight (D21). Stills don't depend on the lift, so this can wait till here.
    mount = detect_mount(acc, gyro, runs, fs)
    lift = choose_lift(lift, mount)

    v, windows = velocity_between_anchors(a, t, fs, runs)
    v = level_heights(a, t, v, windows, lift)
    x = integrate(v, t)
    reps = find_reps(x, v, t, windows, usual_bottom(x, lift))
    return dict(t=t, fs=fs, g=g, a=a, runs=runs, v=v, x=x, windows=windows, reps=reps,
                ignored=n_candidates - len(runs), lift=lift, mount=mount, noise=noise)


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
    acc, gyro = load_imu(path, len(t))
    try:
        r = analyze(t, a_raw, lift, acc, gyro)
    except ValueError as err:
        sys.exit(str(err))
    t, fs, g, a, runs, v, x, windows, reps = (r[k] for k in ("t", "fs", "g", "a", "runs", "v", "x", "windows", "reps"))
    if r["ignored"] and not as_json:
        print(f"(ignored {r['ignored']} 'still' periods that were really steady motion)")

    if as_json:
        import json
        print(json.dumps({
            "fs": float(fs), "g": float(g), "runs": [[int(s), int(e)] for s, e in runs],
            "lift": r["lift"], "mount": r["mount"],
            "reps": [{"start": int(r["start"]), "end": int(r["end"]), "rom": float(r["rom"]),
                      "dur": float(r["dur"]), "mcv": float(r["mcv"]), "peak": float(r["peak"]),
                      "leveled": r["leveled"], "flags": r["flags"]} for r in reps],
        }))
        return

    print(f"{path.name}: {t[-1]:.1f} s at {fs:.0f} Hz, g = {g:.3f} m/s^2, rest wiggle {r['noise']:.3f} m/s^2")
    m = r["mount"]
    seen = f"sensor says {m['kind']}, turning {m['rot']:.1f} deg/s, sideways {m['side']:.2f}" if m else "no gyro, went by the name"
    print(f"Tracked as {r['lift']} ({seen})")
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
