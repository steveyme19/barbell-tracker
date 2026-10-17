"""
Experiment: drift removal with a Kalman filter run forward, then backward (RTS smoother).

Instead of "straight line between stills" + "polynomial through the lockouts", one model:
    height x, velocity v, accelerometer bias b (allowed to wander slowly),
    lockout height H and bottom height L (constants, unknown).
Measurements it gets fixed by:
    - still periods:         v = 0
    - lockouts (turnarounds): x = H      (bottoms too for bench / deadlift / machine)
    - stills at lockout or floor height (barbell lifts): x = H or x = L
The backward pass is the "back-calculating" part: every correction found later in the set
flows back to the samples before it.

Usage:  python experiments/smoother.py   (compares against reps.py on every file)
"""
import sys
from pathlib import Path
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import reps as R
from phone_lift import integrate

P = dict(
    q_a=1e-3,      # m^2/s^3: fast acceleration error (noise, rectification) into velocity
    q_b=1e-3,      # m^2/s^5: how fast the bias may wander (random walk)
    b0=0.1,        # m/s^2: starting bias uncertainty
    zupt_sd=0.01,  # m/s: how hard a still period pins velocity to 0
    zupt_hz=50,    # still-period updates per second
    h_sd=0.01,     # m: how much lockout (or bottom) height may vary rep to rep
    # adaptive stillness: after ADAPT_AFTER s without an anchor, also accept wobblier,
    # shorter pauses (hand shake at lockout), as weaker velocity = 0 hints
    adapt_after=2.0,   # s since the last anchor
    soft_std=0.2,      # m/s^2: wiggle allowed (normal stills: 0.15)
    soft_min=0.15,     # s: shortest soft pause (normal: 0.25)
    soft_hump=0.05,    # m/s: velocity hump allowed inside it
    soft_sd=0.03,      # m/s: how hard a soft pause pins velocity (normal: 0.01)
)


def height_marks(x, runs, lift, open_from=None):
    """Indices pinned to the lockout height (tops) and to the bottom height (bottoms)."""
    prof = R.LIFTS[lift]
    lows, highs = R.turning_points(x, R.MIN_ROM)
    tops, bottoms = list(highs), list(lows)
    in_still = np.zeros(len(x), bool)
    for s, e in runs:
        in_still[s:e + 1] = True
    # turnarounds inside a still are the still itself (handled below), drop them
    tops = [h for h in tops if not in_still[h]]
    bottoms = [b for b in bottoms if not in_still[b]]
    if open_from is not None:   # live tail: only turnarounds the bar has left by MIN_ROM
        tops = [h for h in tops if h < open_from or x[h] - x[h:].min() >= R.MIN_ROM]
        bottoms = [b for b in bottoms if b < open_from or x[b:].max() - x[b] >= R.MIN_ROM]
    turns = sorted(tops + bottoms)
    still_tops, still_bottoms = [], []
    if prof["ends"]:
        for s, e in runs:
            mid = (s + e) // 2
            nxt = [k for k in turns if k > e]
            prv = [k for k in turns if k < s]
            if nxt and nxt[0] in bottoms and x[mid] - x[nxt[0]] >= R.MIN_ROM:
                still_tops.append(mid)        # left it going down: it was at lockout
            elif nxt and nxt[0] in tops and x[nxt[0]] - x[mid] >= R.MIN_ROM:
                still_bottoms.append(mid)     # left it going up: floor / chest / stack
            elif prv and prv[-1] in bottoms and x[mid] - x[prv[-1]] >= R.MIN_ROM:
                still_tops.append(mid)        # arrived going up
            elif prv and prv[-1] in tops and x[prv[-1]] - x[mid] >= R.MIN_ROM:
                still_bottoms.append(mid)
    tops = sorted(tops + still_tops)
    bottoms = sorted(bottoms + still_bottoms) if prof["bottoms"] else []
    return tops, bottoms


def soft_stills(a_raw, a, t, g, fs, runs, p=P):
    """Adaptive thresholds: short or wobbly pauses, accepted only where the last real
    anchor is ADAPT_AFTER s or more back, and only if they pass the coasting and hump checks."""
    keep = (R.STILL_STD, R.MIN_STILL)
    R.STILL_STD, R.MIN_STILL = p["soft_std"], p["soft_min"]
    try:
        cand = R.find_still_runs(a_raw, g, fs)
    finally:
        R.STILL_STD, R.MIN_STILL = keep
    n = int(R.COAST_LOOK * fs)
    soft = []
    for s, e in cand:
        if any(s <= e2 and e >= s2 for s2, e2 in runs):
            continue
        prev = [e2 for s2, e2 in runs if e2 < s]
        if not prev or t[s] - t[prev[-1]] < p["adapt_after"]:
            continue
        before, after = R.speed_change(a, t, s - n, s), R.speed_change(a, t, e, e + n)
        if before * after < 0 and min(abs(before), abs(after)) > R.COAST_DV:
            continue                                   # speeding up then slowing down: a coast
        if R.velocity_hump(a, t, s, e) > p["soft_hump"]:
            continue
        soft.append((s, e))
    return soft


def rts(a, t, runs, tops, bottoms, p=P, soft=()):
    """Forward Kalman filter + backward RTS pass. Returns smoothed velocity."""
    n = len(t)
    i_start = runs[0][0]
    fs = 1 / np.median(np.diff(t))
    zstep = max(1, int(round(fs / p["zupt_hz"])))
    zupt = np.zeros(n, bool)
    for s, e in runs:
        zupt[s:e + 1:zstep] = True
    zsoft = np.zeros(n, bool)
    for s, e in soft:
        zsoft[s:e + 1:zstep] = True
    top_set, bot_set = set(tops), set(bottoms)

    # state [x, v, b, H, L]
    m = np.array([0.0, 0.0, 0.0, 0.0, 0.0])
    Pm = np.diag([1e-8, 1e-6, p["b0"] ** 2, 1.0, 1.0])
    ms = np.zeros((n, 5)); Ps = np.zeros((n, 5, 5))
    mp = np.zeros((n, 5)); Pp = np.zeros((n, 5, 5)); Fs = np.zeros((n, 5, 5))
    ms[i_start], Ps[i_start] = m, Pm
    I5 = np.eye(5)

    def update(m, Pm, Hrow, sd):
        S = Hrow @ Pm @ Hrow + sd ** 2
        K = Pm @ Hrow / S
        m = m + K * (0.0 - Hrow @ m)
        Pm = (I5 - np.outer(K, Hrow)) @ Pm
        return m, Pm

    hv = np.array([0, 1.0, 0, 0, 0]); hx_top = np.array([1.0, 0, 0, -1.0, 0]); hx_bot = np.array([1.0, 0, 0, 0, -1.0])
    for k in range(i_start + 1, n):
        dt = t[k] - t[k - 1]
        u = 0.5 * (a[k] + a[k - 1])                    # trapezoid, like integrate()
        F = np.array([[1, dt, -0.5 * dt * dt, 0, 0],
                      [0, 1, -dt, 0, 0],
                      [0, 0, 1, 0, 0],
                      [0, 0, 0, 1, 0],
                      [0, 0, 0, 0, 1.0]])
        Q = np.zeros((5, 5))
        Q[:2, :2] = p["q_a"] * np.array([[dt ** 3 / 3, dt ** 2 / 2], [dt ** 2 / 2, dt]])
        Q[2, 2] = p["q_b"] * dt
        m = F @ m + np.array([0.5 * u * dt * dt, u * dt, 0, 0, 0])
        Pm = F @ Pm @ F.T + Q
        mp[k], Pp[k], Fs[k] = m, Pm, F
        if zupt[k]:
            m, Pm = update(m, Pm, hv, p["zupt_sd"])
        elif zsoft[k]:
            m, Pm = update(m, Pm, hv, p["soft_sd"])
        if k in top_set:
            m, Pm = update(m, Pm, hx_top, p["h_sd"])
        if k in bot_set:
            m, Pm = update(m, Pm, hx_bot, p["h_sd"])
        ms[k], Ps[k] = m, Pm
    # backward pass
    sm = ms.copy()
    for k in range(n - 2, i_start - 1, -1):
        C = Ps[k] @ Fs[k + 1].T @ np.linalg.inv(Pp[k + 1])
        sm[k] = ms[k] + C @ (sm[k + 1] - mp[k + 1])
    v = sm[:, 1].copy()
    v[:i_start] = 0.0
    for s, e in runs:          # stills are still: exactly 0, like the current pipeline
        v[s:e + 1] = 0.0
    return v


def analyze_rts(t, a_raw, lift="phone", p=P, adaptive=True):
    """Same front end as reps.analyze (gravity, stills, first-pass drift fix), then RTS."""
    first = R.analyze(t, a_raw, lift)
    t, a, runs, windows = first["t"], first["a"], first["runs"], first["windows"]
    open_from = next((w["i0"] for w in windows if not w["anchored"]), None)
    tops, bottoms = height_marks(first["x"], runs, lift, open_from)
    soft = soft_stills(a_raw, a, t, first["g"], first["fs"], runs, p) if adaptive else []
    v = rts(a, t, runs, tops, bottoms, p, soft)
    x = integrate(v, t)
    reps = R.find_reps(x, v, t, windows)
    return dict(first=first, t=t, v=v, x=x, reps=reps, tops=tops, bottoms=bottoms, soft=soft)
