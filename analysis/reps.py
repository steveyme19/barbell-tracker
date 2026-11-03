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
    python reps.py data/bar-....csv --calib=data/pod-calibration-....json
                                                 # a pod recording from before the app applied one (D42);
                                                 # refused when its pod.json / link.json say it's corrected
"""

import json
import math
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
V_STOP_FRAC = 0.03      # a rep's start and end are looked for where v is at or under this x the rep's
                        # own peak, then walked on while v keeps falling: to v = 0 when it gets there
                        # (the same boundary as before), or to where a pause too short or shaky to be a
                        # still begins, its leftover drift reading a hair above 0 (it used to be swallowed
                        # into the rep: MCV up to half too low, D32). Bar and stack lifts; 0 = off
STICK_SMOOTH = 0.05     # s: the concentric's velocity is averaged over this before looking for its dip
STICK_PROM = 0.10       # a sticking dip sits at least this x the rep's peak below the lower of the
                        # peaks before and after it (provisional: tune on real heavy reps, roadmap 5.3)
STICK_RANGE = (0.10, 0.90)   # ... and between these fractions of the ROM (every rep rises and falls at its ends)
WALK_MIN_FS = 150.0     # Hz: the walk needs v smooth near zero. Chrome's 60 Hz with its jittery
                        # timestamps wobbles there, and it stopped short (the same squats, D32)
HITCH_V = 0.1           # D44: a stop near the top that the bar moves on up out of, faster than this x the rep's peak,
                        # was a hitch (or a grind's stall): 11-02's lockout hitch moved on at 0.22 to 0.23 x its peak,
                        # the grind's second stall at 0.89 to 0.91, make_fake_deadlift.py's hitch at 0.14 to 0.18; at 0.06
                        # a 10-31 lockout's settle (0.068) broke its live guard, and a 10-23 one moves on at 0.12
HITCH_ROM = 0.008       # m: ... if that move rose this much (from its stop, and above the end): the hitch 17 to 18 mm,
                        # the stall 48, the fake's 12 (raw, from its drift floor); the false rises past a top 3.3 to 5.0 mm
                        # (10-31 rep 3's settle, the 10-23 lockout). HITCH_V turns away the settle (0.068); the 10-23
                        # lockout moves on at 0.12 and only this stops it (4 to 5 mm; at 4 mm its rep 1 read -8%)
LONG_WINDOW = 4.0       # s: flag reps inside longer windows (more drift)
BIG_CORRECTION = 0.15   # m/s: flag windows whose drift correction was this big
LEVEL_MAX_DEG = 3       # highest power of t in the velocity drift curve (lockouts only)
LEVEL_MAX_DEG_BOTH = 5  # same, when bottoms are leveled too (twice the points, so more bend is safe)
LEVEL_FLAG = 0.02       # m: flag a rep whose lockout misses the common height by this much

# Real bars (D40, the 2026-10-31 gym sets). A loaded barbell often never sits still between unrack and
# rerack: touch-and-go squats, a squatter's 2.7 Hz sway at lockout (0.3 to 0.6 m/s^2, 10x the racked
# noise), a deadlift whose floor rests ring at 20 Hz. Then one window runs from the rack still to the
# rerack still, the straight-line fix spreads the rerack clang over every rep, and reps come out metres
# long. Such a window is redone rep by rep (turnaround_velocity): a coarse pass finds the reps, then each
# span between two of the lift's resting turnarounds (lockouts; the floor for a deadlift) gets §6's
# straight-line fix on its own. Why that isn't D2's mistake (§7): the anchor times come from the coarse
# pass (drift high-passed away), not from the drifting velocity they correct; a pause is anchored on its
# own average velocity, real evidence the bar is stopped; and a touch-and-go anchor is a lockout, never
# a squat's bottom (a top turns at 2 to 5 m/s^2, a bottom at 5 to 13, so a timing error there costs 2 to
# 3x less).
TA_BROKEN_V = 1.0       # m/s: velocity left at the window's end. Stills, the fakes and every older real
                        # window: 0.68 at most. The gym sets on |a|: 3.5 to 8.1 (impacts and rectification,
                        # not sensor bias). On the vertical they end at 0.08 to 0.81, so there a bar lift's
                        # window is redone because of its impact, and this is a backstop for gross failures
TA_BROKEN_TAIL = 3.0    # m/s: the same for a window with no end still yet (live, mid-set), which can end
                        # mid-rep: the bar's own speed (up to 1.4 on file) plus TA_BROKEN_V and a margin
TA_MIN_WIN = 1.0        # s: a shorter window is never redone (a jolt's pad would cover it)
TA_BAR = ("squat", "bench", "deadlift", "free")   # bar lifts: an impact redoes their window too, and
                        # they get the gyro's vertical (below)
TA_RACKED = ("squat", "bench")   # these end at the rack: the first impact after the first lockout closes the
                        # set (an impact, not any jolt: a hard chest bounce reads 20+ m/s^2 too, D43), and
                        # the set's turnarounds are the coarse height's up to it. Not "free": a row or a
                        # clean typed by name touches the floor between reps
IMPACT_A = 20.0         # m/s^2: |a - g| over this is a jolt (bar on the hooks, plates on the floor).
                        # Squat reps peaked at 12.8, reracks 22 to 57, deadlift landings 67 to 76 (and
                        # its liftoffs 15 to 20, not jolts: the bar doesn't hit anything)
IMPACT_W = 100.0        # deg/s: an impact (it redoes a bar lift's window) is a jolt with the gyro (offset
IMPACT_NEAR = 0.05      # s     off) over IMPACT_W within IMPACT_NEAR of it. Reracks 250 to 330, set-downs
                        # 300 to 1130; the ham curl's 22 m/s^2 stack bump stayed under 60
IMPACT_PAD = 0.1        # s: blanked around each impact in the coarse pass (on the floor any jolt; else an
                        # impact, not any jolt: a chest bounce is the bar's own motion, D43)
JOLT_PAD = 0.02         # s: blanked around each impact when a span is integrated again (only the clang,
                        # not the lift)
TA_SIGMA = 0.75         # s: the coarse pass removes drift slower than this (Gaussian high-pass). Reps
                        # take 1.4 to 4 s; this keeps their turnarounds and loses the slow ones' size
TA_SIGMA_LIFT = {"bench": 1.75}  # s: ... longer on the bench (D43): a paused rep (1 s on the chest, a 2.4 s
                        # grind) is slower than a squat, and 0.75 left 12 to 19 cm of its 36 cm: no turnaround
                        # at a grind's top, and long holds failed TA_NEAR_FRAC (synthetics, 11-01: 1.25 to 2
                        # fix both; 1.75 also keeps a 4 to 8 s hold, and lost no count where 1.5 lost one).
                        # Not squat or deadlift: 1.5 there broke the 10-31 squat (+8.4%) and the deadlift
                        # (6 for 5)
TA_MIN_MOVE = 0.15      # m: a coarse turnaround moves at least this much (the high-pass halves a slow rep)
TA_HOOK_MOVE = 0.15     # m: on a racked lift a move off the hooks smaller than this is the unrack or the rerack
                        # (4 to 6 cm), not a rep (D43). The bench synthetics read the same from 0.15 to 0.25
                        # (unracks up to 14 cm stay out); at 0.12, sets.py's RACK_MIN_ROM, 8 unracks became reps
TA_REST = {"deadlift": "bottom", "machine": "bottom"}   # where the bar rests between reps; others: "top"
TA_FLAT_WIN = 0.37      # s: "flat" = acceleration averaged over this (jolts blanked) stays within
TA_FLAT_A = 0.5         # m/s^2  TA_FLAT_A of 0. The window nulls the 2.7 Hz sway of a bar on the back and
                        # the floor's 20 Hz ringing. Standing, held lockouts and the floor read up to
                        # 0.5 (a heavy bar whips at 1 to 5 Hz); touch-and-go tops average -1.75 to -4.9
TA_PAUSE = 0.25         # s: a flat stretch this long next to a turnaround can be a pause (standing before
                        # the first rep, a held lockout, the floor) ...
TA_REACH = 0.5          # s: ... if it reaches to within this of the coarse turnaround (whose zero crossing
                        # wanders in a pause: up to 0.4 s out of it on these sets) ...
TA_FLOOR_REACH = 1.5    # s: ... or, on the floor, ends within this before it. With no set-down jolt to restart
                        # the coarse pass (rep 1, pulled after a long setup) the high-pass bends the height
                        # down into the pull, and the low lands late, at a hitch just off the floor: 0.59 s
                        # after the floor's end on 10-31 (rep 1 read +31% from the hitch). 1.0 until D44: a slow
                        # pull after a 1.2 s sink into the bar-bend dip lands it 1.17 to 1.19 s after the still,
                        # and the set lost its last rep (D44's held-out synthetics: 1.3 to 2 recover 33 of 608) ...
TA_COAST_DV = 0.35      # m/s: ... isn't a coast by §5's local test (speed changes of opposite sign over
                        # COAST_LOOK before and after, both over this). Coasting through a concentric or a
                        # descent changes 0.45 to 1.7 m/s either side; the walkout's bob into standing 0.15
                        # to 0.32, which the stills' 0.1 would call a coast ...
TA_NEAR_FRAC = 0.4      # ... and is at the turnaround's height: within this x the bigger neighbouring move,
                        # on the coarse height. Real pauses on file read 0.06 to 0.37 (the high-pass
                        # leaks ~10 cm into a long pause), coasts 0.40 to 0.53: a thin margin
TA_HOLD_V = 0.03        # m/s: ... and the coarse height doesn't move through it faster than this (D43). Between
                        # two reps that's a must: a lockout held there reads 0.021 on 10-31's squat and 0.028
                        # (p99) on the bench synthetics, a bar easing down out of lockout 0.019 to 0.47. A hold
                        # turned away costs little (the coarse zero crossing anchors it), a drift taken for one
                        # a lot: lower read better at every step to 0.02, which turns the squat's hold away.
                        # At the set's ends the walkout, walk-in and rerack leak into the coarse pass (0.05 to
                        # 0.15 on 10-31), so there it only lets a run before the first rep or after the last
                        # skip the height test
TA_EDGE = 0.25          # s: the set starts and ends at rest: the first pause's anchor averages the last
                        # 2 x this of it (just before rep 1), the last pause's its first 2 x this (just
                        # after the last rep, before the walk-in); others all of it, but a lockout between
                        # reps that ends TA_START_DROP lower than it started (the descent began in it) its first
                        # 2 x this, and the first one that ends that far under its coarse top the 2 x this up to it
TA_START_DROP = 0.005   # m: ... on the coarse height (D43). Any drop at all fired on 164 holds of 90 bench runs,
                        # 22 of them under 1 mm; on every hold, an 8 s breath read -63% (the margin loses none)
TA_FLOOR_FLAT_A = 0.2   # m/s^2: on the floor a rest is flat to this, not TA_FLAT_A (D44). A slow pull's middle
                        # averages under 0.5 for over a second (11-02 set 1 rep 3, calibrated: the rest ran
                        # 1.9 s past liftoff into it, and its zero put +0.22 m/s on the rep; set 2's rep 2 read 7 cm,
                        # calibrated its rep 3 11 cm). A narrow window: only 0.19 to 0.23 pass everything (under, the
                        # 10-31 deadlift's live view; over, 11-02 set 2's calibrated rep 3 and deadlift_creep's live view)
LAND_GAP = 0.3          # s: a set-down is one landing from its first contact (D44): a jolt run that ends within
                        # this of the next one's onset is the same set-down. A heavy one bounces in 1 to 5 runs
                        # (jolts padded by JOLT_PAD) over 0.38 to 0.48 s, 9 to 57 ms apart, and closing at the last
                        # one put +0.01 to 0.05 m/s (and up to 19 cm) on the pull that landed (11-02 set 1 +0.04 to
                        # 0.05 and 10 cm, set 2 +0.01 to 0.02, its grind 19 cm). Not longer: a jolt in the descent
                        # within it becomes the landing (10-31 rep 3 has a 15 m/s^2 spike 0.75 s before its first
                        # contact; at 0.8 a slightly harder one read ROM -15 cm)
TA_SET_EDGE = 3.0       # s: a window still this close to the first (last) anchor is part of the set (one
                        # rep's time); the gym walkouts took 4.3 to 7.3 s from the rack still

# The vertical (D40). On a bar the gyro tracks which way is up, so the acceleration along up replaces
# |a| - g for bar lifts: |a| rectifies a clang and the sway into fake upward acceleration, and an
# uncalibrated pod's |a| at rest moves with the sleeve's roll (9.67 to 10.03 on 10-31). Madgwick (D33's
# filter) is pulled onto gravity at the stills and at quasi-stills, the gyro alone in between.
VERTICAL_FS = (150.0, 300.0)   # Hz: only at the pod's rate, a stand-in for "the source is the pod".
                        # Browsers (60 Hz) and the phone's native sensors (402 Hz, hand-held tests) stay on
                        # |a| (D9): a hand-held phone isn't on a bar
QS_AVG = 0.1            # s: accel and gyro averaged over this for the quasi-still test (two periods of
                        # the floor's 20 Hz ring cancel)
QS_DEV = 0.15           # m/s^2: | |mean accel| - g | under this ...
QS_GYRO = 3.0           # deg/s: ... and the mean turning rate (bias off) under this, NOT counting the turn
                        # about the bar axis: an Olympic sleeve keeps spinning on its bearing after a set-down
                        # or a rerack (5 to 25 deg/s here) while the bar rests, and an on-axis pod feels no
                        # acceleration from it ...
BAR_AXIS = 2            # the pod axis along the bar (Adapter A: z, on the sleeve end face, D37/O3)
QS_MIN = 0.25           # s: ... this long = quasi-still: gravity is the only acceleration (resting OR
                        # coasting, both fine for attitude), so Madgwick may pull onto it. Never a velocity anchor
BETA_STILL = 0.2        # Madgwick gain while still (rad/s of correction): converge on gravity (D33)
BETA_MOVE = 0.0         # while moving: the gyro alone. Madgwick's step is beta times a *normalized*
                        # gradient, so once the true error is below the accel noise it still pushes at
                        # full strength in a noise-picked direction: 0.005 added 0.45 deg over a 1.2 s
                        # sleeve spin that the bias-corrected gyro alone tracked to 0.04 deg
GATE_DEG = 3.0          # a still's accel must point within this of the estimated up to pull on it
                        # (a steady push reads quiet but tips the accel; a real rest agrees) ...
SN_FLOOR = 0.05         # the gradient's norm, floored: it's about 2x the tilt error in rad, so under ~1.4 deg
                        # the pull is proportional (tau ~62 ms), not a full beta step in a noise-picked
                        # direction. Without it the vertical was chaotic in roundoff: 1e-15 m/s^2 of input
                        # moved it 0.07 (D40's review), so JS and Python couldn't agree on a live cut

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


# ---- attitude (D33's Madgwick, here since D40 so analyze() can use it; orientation.py imports it) ----

def still_mask(n, runs):
    """Per-sample bool: inside one of the (start, end) runs."""
    m = np.zeros(n, bool)
    for s, e in runs:
        m[s:e + 1] = True
    return m


def quat_from_to(a, b):
    """The unit quaternion (w, x, y, z) that turns unit vector a onto unit vector b."""
    a, b = a / np.linalg.norm(a), b / np.linalg.norm(b)
    c = np.dot(a, b)
    if c < -0.999999:                                    # opposite: any axis at right angles
        ax = np.cross(a, [1, 0, 0]) if abs(a[0]) < 0.9 else np.cross(a, [0, 1, 0])
        return np.concatenate([[0.0], ax / np.linalg.norm(ax)])
    q = np.concatenate([[1 + c], np.cross(a, b)])
    return q / np.linalg.norm(q)


def quat_to_mat(q):
    """Rotation matrices (n, 3, 3), sensor -> world, from quaternions (n, 4) (w, x, y, z)."""
    w, x, y, z = q.T
    return np.stack([
        np.stack([1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)], -1),
        np.stack([2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)], -1),
        np.stack([2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)], -1)], -2)


def madgwick(t, acc, gyr_rad, still, q0, trusted=None):
    """Madgwick's IMU update (MadgwickAHRS.c, MadgwickAHRSupdateIMU) per sample. q maps sensor to
    world: v_world = q v_sensor q*. still: per-sample bool, BETA_STILL there (if the accel agrees
    with the estimate within GATE_DEG, or the sample is `trusted`: a still of LONG_STILL or more,
    so a gyro-scale error left after a spin can't lock the correction out), BETA_MOVE elsewhere. The
    step from sample i-1 to i turns by the mean of their two gyro readings (the trapezoid): the end
    reading alone lags the attitude by half a sample, 1.4 degrees in a 560 deg/s sleeve spin.
    math.sqrt, not ** 0.5: Math.sqrt in reps.js rounds the same way (parity)."""
    n = len(t)
    out = np.zeros((n, 4))
    q0_, q1, q2, q3 = (float(x) for x in q0)
    out[0] = q0
    cos_gate = math.cos(math.radians(GATE_DEG))
    for i in range(1, n):
        dt = t[i] - t[i - 1]
        gx, gy, gz = (0.5 * (float(gyr_rad[i][k]) + float(gyr_rad[i - 1][k])) for k in range(3))
        ax, ay, az = (float(x) for x in acc[i])
        beta = BETA_MOVE
        if still[i]:
            # the estimated up in the sensor frame (R^T z), against the measured accel's direction
            ux, uy, uz = 2 * (q1 * q3 - q0_ * q2), 2 * (q0_ * q1 + q2 * q3), 1 - 2 * (q1 * q1 + q2 * q2)
            na = math.sqrt(ax * ax + ay * ay + az * az)
            if (trusted is not None and trusted[i]) or (na > 0 and (ux * ax + uy * ay + uz * az) / na > cos_gate):
                beta = BETA_STILL
        qd0 = 0.5 * (-q1 * gx - q2 * gy - q3 * gz)
        qd1 = 0.5 * (q0_ * gx + q2 * gz - q3 * gy)
        qd2 = 0.5 * (q0_ * gy - q1 * gz + q3 * gx)
        qd3 = 0.5 * (q0_ * gz + q1 * gy - q2 * gx)
        norm = math.sqrt(ax * ax + ay * ay + az * az)
        if norm > 0:
            ax, ay, az = ax / norm, ay / norm, az / norm
            _2q0, _2q1, _2q2, _2q3 = 2 * q0_, 2 * q1, 2 * q2, 2 * q3
            _4q0, _4q1, _4q2 = 4 * q0_, 4 * q1, 4 * q2
            _8q1, _8q2 = 8 * q1, 8 * q2
            q0q0, q1q1, q2q2, q3q3 = q0_ * q0_, q1 * q1, q2 * q2, q3 * q3
            s0 = _4q0 * q2q2 + _2q2 * ax + _4q0 * q1q1 - _2q1 * ay
            s1 = _4q1 * q3q3 - _2q3 * ax + 4 * q0q0 * q1 - _2q0 * ay - _4q1 + _8q1 * q1q1 + _8q1 * q2q2 + _4q1 * az
            s2 = 4 * q0q0 * q2 + _2q0 * ax + _4q2 * q3q3 - _2q3 * ay - _4q2 + _8q2 * q1q1 + _8q2 * q2q2 + _4q2 * az
            s3 = 4 * q1q1 * q3 - _2q1 * ax + 4 * q2q2 * q3 - _2q2 * ay
            sn = max(math.sqrt(s0 * s0 + s1 * s1 + s2 * s2 + s3 * s3), SN_FLOOR)
            if sn > 0:
                qd0 -= beta * s0 / sn
                qd1 -= beta * s1 / sn
                qd2 -= beta * s2 / sn
                qd3 -= beta * s3 / sn
        q0_, q1, q2, q3 = q0_ + qd0 * dt, q1 + qd1 * dt, q2 + qd2 * dt, q3 + qd3 * dt
        qn = math.sqrt(q0_ * q0_ + q1 * q1 + q2 * q2 + q3 * q3)
        q0_, q1, q2, q3 = q0_ / qn, q1 / qn, q2 / qn, q3 / qn
        out[i] = (q0_, q1, q2, q3)
    return out


def quasi_stills(acc, gyr, g, fs):
    """Per-sample mask: the QS_AVG-averaged accel is g long and the averaged gyro (bias off) barely
    turns about anything but the bar, for QS_MIN or more. Tolerates the ring and whip that fail the
    real still test. Also returns the averaged accel (what the attitude is pulled toward)."""
    n = max(int(QS_AVG * fs), 1)
    am = np.column_stack([rolling(acc[:, k], n)[0] for k in range(3)])
    wm = np.column_stack([rolling(gyr[:, k], n)[0] for k in range(3) if k != BAR_AXIS])
    ok = (np.abs(np.linalg.norm(am, axis=1) - g) < QS_DEV) & (np.linalg.norm(wm, axis=1) < QS_GYRO)
    mask = np.zeros(len(ok), bool)
    for s, e in runs_of(ok):
        if (e + 1 - s) / fs >= QS_MIN:
            mask[s:e + 1] = True
    return mask, am


def vertical_accel(t, acc, gyro, runs, fs):
    """Acceleration along up (gravity off, m/s^2) from the gyro-tracked attitude: Madgwick pulled onto
    gravity in the stills and the quasi-stills, the gyro alone in between, run forward from the first
    still and backward before it. Every pull is "trusted": a clang can tip the estimate past GATE_DEG
    (5 to 7 deg on the 10-31 deadlift), and the averaged accel it pulls toward has the ring averaged out."""
    still = still_mask(len(t), runs)
    g = float(np.mean(np.linalg.norm(acc[still], axis=1)))
    w_deg = gyro - np.median(gyro[still], axis=0)             # gyro offset from the stills
    qs, am = quasi_stills(acc, w_deg, g, fs)
    pull = still | qs
    s0, e0 = runs[0]
    q0 = quat_from_to(acc[s0:e0 + 1].mean(axis=0), np.array([0.0, 0.0, 1.0]))
    w = np.radians(w_deg)
    q = np.zeros((len(t), 4))
    q[s0:] = madgwick(t[s0:], am[s0:], w[s0:], pull[s0:], q0, pull[s0:])
    if s0:
        b = slice(s0, None, -1)
        q[:s0 + 1] = madgwick(-t[b], am[b], -w[b], pull[b], q0, pull[b])[::-1]
    up = quat_to_mat(q)[:, 2, :]                            # world up in the sensor frame
    return (acc * up).sum(axis=1) - g


def velocity_between_anchors(a, t, fs, runs, floor=False):
    """Velocity with drift removed between every pair of still periods.

    Still periods stay at exactly 0. Motion after the last still period has no
    anchor at its end, so it's integrated without correction and flagged. On the
    floor (floor: a lift whose bar rests at the bottom) less the last still's bias.
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
        # On the floor less the last still's own average (D44): at rest the bar's acceleration is 0, so what the
        # still reads is the sensor's offset at that attitude, and the bar is pulled from where it rested (11-02: -0.031
        # m/s^2 raw before set 2 rep 3, the tail drifted -0.03 and showed the 45 cm grind as 26 cm; +0.003 before set 1,
        # and 11 s of hands on the bar with no still drifted into a 25 cm "rep"). Not off the rack: the unrack turns the
        # bar, and a squat's rack still made its walkout a 16 cm rise (10-31 set 1)
        bias = a[runs[-1][0]:last + 1].mean() if floor else 0.0
        v[last:] = integrate(tail - bias, tw)
        windows.append(dict(i0=last, i1=len(t) - 1, dur=tw[-1], raw_end=np.nan, anchored=False, bias=bias))
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
    hooks = (lift or LIFT) in TA_RACKED     # squat, bench: on a pod the stills before and after are the hooks
    x = integrate(v, t)
    lows, highs = turning_points(x, MIN_ROM)
    for w in windows:
        if w.get("turns"):
            continue                    # redone rep by rep (turnaround_velocity)
        i0, i1 = w["i0"], w["i1"]
        tops = [h for h in highs if i0 < h < i1]
        bottoms = [b for b in lows if i0 < b < i1]
        if not w["anchored"]:
            # only turnarounds the bar has already moved away from
            tops = [h for h in tops if x[h] - x[h:i1 + 1].min() >= MIN_ROM]
            bottoms = [b for b in bottoms if x[b:i1 + 1].max() - x[b] >= MIN_ROM]
        if hooks and prof["bottoms"]:
            # bench: the chest is a rep's depth under the lockout before it. A low less than TA_HOOK_MOVE
            # under that is the hooks (a soft rerack lowers the bar 4 to 6 cm onto them), not the chest (D43)
            bottoms = [b for b in bottoms if x[max([h for h in highs if h < b], default=i0)] - x[b] >= TA_HOOK_MOVE]
        turns = sorted(tops + bottoms)
        if prof["ends"] and turns:
            # The bar left the start still going down: it started at lockout. Going up:
            # it started at the bottom (deadlift from the floor). Same for the end still.
            # A racked lift going up from its still by less than a rep (TA_HOOK_MOVE) left the hooks, not
            # the chest: the unrack, 4 to 6 cm (and the rerack back down onto them, D43)
            rep = TA_HOOK_MOVE if hooks else MIN_ROM
            if turns[0] in bottoms and x[i0] - x[turns[0]] >= MIN_ROM:
                tops = [i0] + tops
            elif turns[0] in tops and x[turns[0]] - x[i0] >= rep:
                bottoms = [i0] + bottoms
            if w["anchored"]:
                if turns[-1] in bottoms and x[i1] - x[turns[-1]] >= MIN_ROM:
                    tops = tops + [i1]
                elif turns[-1] in tops and x[turns[-1]] - x[i1] >= rep:
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


# ---- turnaround anchors (D40): a window with no still between its reps, redone rep by rep ----

def gauss_smooth(y, fs, sigma):
    """Gaussian moving average (sigma in s), ends padded with the end values."""
    n = int(4 * sigma * fs)
    k = np.exp(-0.5 * (np.arange(-n, n + 1) / (sigma * fs)) ** 2)
    yp = np.concatenate([np.full(n, y[0]), y, np.full(n, y[-1])])
    return np.convolve(yp, k / k.sum(), mode="same")[n:len(yp) - n]


def dilate(mask, n):
    """mask widened by n samples each way."""
    out = np.zeros(len(mask), bool)
    for i in np.where(mask)[0]:
        out[max(i - n, 0):i + n + 1] = True
    return out


def impact_mask(a, gyro, runs, fs):
    """Per sample: a jolt (|a| over IMPACT_A) with the gyro (offset off) over IMPACT_W within
    IMPACT_NEAR of it: the bar hit something (the hooks, the floor), not a fast rep."""
    if gyro is None or not np.any(gyro):
        return np.abs(a) > 25.0                         # no gyro to confirm it: a harder jolt alone
    jolt = np.abs(a) > IMPACT_A
    still = still_mask(len(a), runs)
    wn = np.linalg.norm(gyro - gyro[still].mean(axis=0), axis=1)
    return jolt & dilate(wn > IMPACT_W, int(IMPACT_NEAR * fs))


def broken_window(v, w):
    """The still-to-still fix can't be trusted: the velocity left at the window's end (raw, or after
    the tail with no end still) is far beyond any sensor bias."""
    if w["anchored"]:
        return abs(w["raw_end"]) > TA_BROKEN_V
    return abs(v[w["i1"]]) > TA_BROKEN_TAIL


def runs_of(mask):
    """(start, end) of every run of True in mask."""
    d = np.diff(np.concatenate([[0], np.asarray(mask).astype(int), [0]]))
    return list(zip(np.where(d == 1)[0], np.where(d == -1)[0] - 1))


def confirmed_at(x, k, min_move):
    """Where turning_points confirmed the turnaround at k: the first sample after it that's min_move
    away from it (len(x) if none is)."""
    d = np.abs(x[k:] - x[k]) >= min_move
    return k + int(np.argmax(d)) if d.any() else len(x)


def find_pause(a, t, xc, k, lo, hi, fs, flat, floor, first, last):
    """The pause at the coarse turnaround k (lo, hi: the turnarounds either side; xc: coarse height),
    as (start, end) samples, or None. A flat run between lo and hi, TA_PAUSE or longer, reaching to
    within TA_REACH of k, is a pause (standing before the first rep, a held lockout, the floor) if the
    bar isn't moving through it. Flat is also a coast, the old lesson of §5, so two checks: §5's local
    one (speed changes of opposite sign before and after, both over TA_COAST_DV), and height: a pause
    is at the turnaround's height, within TA_NEAR_FRAC of the bigger neighbouring move (a slow
    concentric's flat middle, or a grinder's flat finish, is well below the top), read at the run's middle
    (on the floor at its sample nearest k, D44). Which run: at a
    lockout the nearest, on the floor the longest (a deadlift's hitch just off the floor passes both
    checks and sits nearer, but it's brief). In a pause the coarse velocity's zero crossing k can land
    anywhere, even outside it; on the floor it can land at that hitch, so there the run may end up to
    TA_FLOOR_REACH before k. At a lockout (not the floor) the coarse height is a velocity check too
    (TA_HOLD_V): a bar easing down out of lockout at a steady speed reads flat, but its height falls through
    the run. Between two reps (k neither first nor last: the window's first and last coarse lockout),
    whose up and down cancel in the coarse pass's average, a run must pass it. Before the first rep (first,
    the run's middle before k) or after the last (last, its middle after k) a run that passes it needs no
    height check: there's no rep on that side to coast through, and the high-pass bends a long hold there
    well down from the top (a chest pause shrinks the move it is measured against, D43)"""
    look, reach = int(COAST_LOOK * fs), int(TA_REACH * fs)
    back = int(TA_FLOOR_REACH * fs) if floor else reach      # how far before k the run may end
    move = max(abs(xc[k] - xc[lo]), abs(xc[k] - xc[hi]))
    best = None
    for rs, re in runs_of(flat[lo:hi + 1]):
        rs, re = rs + lo, re + lo
        if re - rs < TA_PAUSE * fs or not (rs - reach <= k <= re + back):
            continue
        into, out = speed_change(a, t, rs - look, rs), speed_change(a, t, re, re + look)
        if into * out < 0 and min(abs(into), abs(out)) > TA_COAST_DV:
            continue
        # On the floor the height is read where the run is nearest k (D44): the high-pass (TA_SIGMA) bends a long
        # floor rest up by its middle, 0.17 to 0.37 m for 4 s. 11-02 set 1 raw's rests between reps read +0.28 m against
        # the 0.18 allowed and +0.17 against 0.15, so they never anchored, and the suite's rep after such a rest
        # (held3*, reset_slow3*) read -22 to -40%
        mid = (rs + re) // 2 if not floor else min(max(k, rs), re)
        slow = abs(xc[re] - xc[rs]) <= TA_HOLD_V * (t[re] - t[rs])
        outer = not floor and slow and ((first and mid <= k) or (last and mid >= k))
        if not outer and abs(xc[mid] - xc[k]) > TA_NEAR_FRAC * move:
            continue
        if not (floor or first or last or slow):
            continue
        score = -(re - rs) if floor else (0 if rs <= k <= re else min(abs(rs - k), abs(re - k)))
        if best is None or score < best[0]:
            best = (score, (rs, re))
    return None if best is None else best[1]


def span_velocity(vr, tw, s, e, vs, ve, hit=None):
    """Velocity over samples s..e: the raw velocity vr less a straight line through vs at s and ve at
    e (each anchor's raw velocity, §6's fix for one span). hit (floor lifts only): jolt samples. The
    last jolt in the span's second half is the bar landing: the floor stops it, so v = 0 from there
    on, and the span before it closes on height instead (the bar is back on the floor it left from),
    since its speed at the landing isn't known."""
    tt = tw[s:e + 1] - tw[s]
    v = vr[s:e + 1] - vs
    if hit is not None:
        late = np.where(hit[s:e + 1] & (tt > tt[-1] / 2))[0]
        if len(late):
            land = late[-1]
            while land > 0 and hit[s + land - 1]:      # back to the onset of that last jolt
                land -= 1
            while True:                                 # back to the first contact (D44), in the second half too
                prev = np.where(hit[s:s + land] & (tt[land] - tt[:land] <= LAND_GAP) & (tt[:land] > tt[-1] / 2))[0]
                if not len(prev):
                    break
                land = prev[0]
                while land > 0 and hit[s + land - 1]:
                    land -= 1
            if tt[land] > 0:
                x = integrate(v[:land + 1], tt[:land + 1])
                v[:land + 1] -= 2 * x[-1] / tt[land] ** 2 * tt[:land + 1]   # height back to 0 at the landing
            else:
                v[:] = 0.0                              # the span starts in that jolt: on the floor
            v[land + 1:] = 0.0
            return v
    return v - (ve - vs) * tt / tt[-1]


def turnaround_velocity(a, t, fs, w, lift):
    """Velocity over a redone window, rep by rep:
      1. coarse pass: the rep structure only (this velocity isn't kept). Impacts blanked (on the floor
         every jolt; else w["impacts"], the jolts the gyro confirms, never a chest bounce, D43), an
         impact the bar comes to rest after stops it (on the floor also one whose flat run reaches the
         window's end, D44), drift slower than TA_SIGMA (TA_SIGMA_LIFT) high-passed away; then the
         height's turnarounds of TA_MIN_MOVE or more
      2. the lift's resting turnarounds (TA_REST: lockouts, or the floor) are the anchors: a pause
         (find_pause) anchors on its average velocity, which averages out a heavy bar's whip; a
         touch-and-go turn on the coarse zero crossing. A squat or bench set ends at the first impact
         after its first lockout (the bar hit the rack), and its turnarounds are the coarse height's
         up to there. With no end still yet, a floor lift's last rest anchors on all of it (there's
         no walk-in after a deadlift), and a turnaround confirmed only in the window's last TA_SIGMA
         waits, but for the floor after the last rep once the bar has landed. On the floor the window's
         start still is the floor when nothing anchors it before the first top (D44)
      3. one raw velocity over the window (impacts blanked), less a straight line between each pair of
         neighbouring anchors (§6, span by span). Before the first anchor and after the last it's 0:
         the unrack, walkout, walk-in and rerack aren't reps. No end still yet (live, or a still the
         test missed) and not racked: after the last anchor the raw velocity goes on from it, flagged,
         like any unanchored tail (the rep in progress).
    Returns v for samples i0..i1; w gets the anchors, and w["rests"] = (first anchor, last anchor,
    closed: nothing after the last one can be a rep)."""
    i0, i1 = w["i0"], w["i1"]
    tw = t[i0:i1 + 1] - t[i0]
    aw = a[i0:i1 + 1]
    floor = TA_REST.get(lift, "top") == "bottom"
    # What the bar hit. On the floor every jolt: a set-down that barely turns the bar still lands it (D41).
    # Elsewhere only the jolts the gyro confirms: a hard chest bounce is the bar's own motion (D43)
    imp = np.abs(aw) > IMPACT_A if floor else w["impacts"]
    # 1. coarse pass (n: samples in TA_FLAT_WIN)
    blank = dilate(imp, int(IMPACT_PAD * fs))
    am = np.where(blank, 0.0, aw)
    n = max(1, int(TA_FLAT_WIN * fs))
    fm = np.abs(rolling(am, n)[0])
    flat = fm < TA_FLAT_A
    floor_flat = fm < TA_FLOOR_FLAT_A                   # a floor rest's test (D44)
    # A stop: a blanked jolt the bar comes to rest after, a flat run of TA_PAUSE from within n of its end. On the floor
    # (D44) a flat run into the window's end is one however short: a closed window's end still goes on from there, and
    # live nothing since the landing says the bar moved on. A heavy single's still begins 0.35 s after its set-down: no
    # stop, the blanked landing left -0.8 m/s in the coarse velocity, the high-pass spread it over the descent, and the
    # height's one turnaround was the landing (0 reps; live, the rep vanished until 0.25 s of the still was in)
    to_end = len(aw) - 1 if floor else -1
    stops = [je for js, je in runs_of(blank)
             if any(rs <= n and (re - rs >= TA_PAUSE * fs or je + re == to_end) for rs, re in runs_of(flat[je:]))]
    vc = np.zeros_like(aw)
    for s, e in zip([0] + stops, stops + [len(aw) - 1]):
        vc[s:e + 1] = integrate(am[s:e + 1], tw[s:e + 1] - tw[s])
    sigma = TA_SIGMA_LIFT.get(lift, TA_SIGMA)
    vc = vc - gauss_smooth(vc, fs, sigma)
    xc = integrate(vc, tw)
    lows, highs = turning_points(xc, TA_MIN_MOVE)
    # A squat or bench set ends at the first impact after its first lockout (the bar hit the rack): its
    # turnarounds are the coarse height's up to there (rack: the last sample before it). A rerack fumble
    # lifted above lockout after the knock would otherwise be the last rep's top, and go with everything
    # after the impact (D43)
    rack = len(tw) - 1
    racked = False
    if lift in TA_RACKED:
        hits = np.where(imp)[0]
        hits = hits[hits > highs[0]] if highs else hits
        if len(hits):
            rack = hits[0] - 1
            lows, highs = turning_points(xc[:rack + 1], TA_MIN_MOVE)
            racked = True                               # no rep in progress after it, live or not
    lows_all, highs_all = list(lows), list(highs)       # before the open window's wait (below)
    if floor and not w["anchored"]:
        # No end still yet (live): the high-pass bends the height near the buffer's end, and fakes a low
        # in the descent just before the plates land. A turnaround it confirms only in the last TA_SIGMA
        # isn't one yet; the rep stays in the open tail until it is. Except the last low once the bar has
        # landed (a stop, a jolt the bar then rests after, between the high before it and TA_REACH after
        # it): that's the floor after the last rep, and nothing after it may ever confirm it (End tapped
        # soon after the set-down took the last rep's anchor away)
        cut = len(xc) - 1 - int(sigma * fs)
        reach = int(TA_REACH * fs)
        before = lambda k: max([h for h in highs if h < k], default=-1)
        landed = lambda k: k == lows[-1] and any(before(k) < je <= k + reach for je in stops)
        lows = [k for k in lows if confirmed_at(xc, k, TA_MIN_MOVE) <= cut or landed(k)]
        highs = [k for k in highs if confirmed_at(xc, k, TA_MIN_MOVE) <= cut]
    turns = sorted(lows + highs)
    # 2. anchors: (sample, first and last sample of the stretch whose average velocity is the zero)
    # A turnaround the wait holds back is still a rest's neighbour (D44): its height is real, only its
    # confirmation is late. Measured against the buffer's end instead, the rest before the last rep fails the
    # height test while the set-down rings, and the rep goes for a moment (make_fake_deadlift.py held3_quiet
    # raw: the count 3, then 2 for 1.25 s at the set's close)
    nb = sorted(lows_all + highs_all) if floor and not w["anchored"] else turns
    rests = []
    resting = lows if floor else highs
    for k in resting:
        j = nb.index(k)
        lo = nb[j - 1] if j > 0 else 0
        hi = nb[j + 1] if j + 1 < len(nb) else rack
        rests.append((k, find_pause(am, tw, xc, k, lo, hi, fs, floor_flat if floor else flat, floor,
                                    k == resting[0], k == resting[-1])))
    closed = w["anchored"] or racked
    edge = int(TA_EDGE * fs)
    anchors = []
    ks = []                                             # each anchor's coarse turnaround
    for j, (k, p) in enumerate(rests):
        ks.append(k)
        if p is None:
            if floor:
                # On the floor the turn is the touch (D44): the last jolt since the top before it, within TA_FLOOR_REACH
                # before the coarse low, anchors at its coarse blank's last sample (where a coarse stop restarts). The
                # blank took the stop that jolt carried, so the low lags it into the pull: tng5's by 0.13 to 0.22 s,
                # where the bar already rose at 0.37 to 0.41 m/s, and that zero took 20 cm off each rep (-27 to -51%).
                # The anchor inherits IMPACT_PAD's tuning: tng5 reads right from 0.1 to 0.125 only
                prev = max([h for h in highs_all if h < k] + [k - int(TA_FLOOR_REACH * fs), 0])
                bj = np.where(blank[prev:k + 1])[0]
                if len(bj):
                    k = prev + bj[-1]
            anchors.append((k, k, k))                   # touch-and-go
            continue
        ps, pe = p
        if j == 0 and len(rests) > 1:
            ps = max(ps, pe - 2 * edge)                 # standing before the first rep: its end ...
            top = p[0] + int(np.argmax(xc[p[0]:pe + 1]))
            if not floor and xc[pe] < xc[top] - TA_START_DROP:
                # ... unless the descent began in it (eased into, it reads flat): the 2 x TA_EDGE up to its coarse
                # top. The anchor stays where it was (the set starts there, D43)
                anchors.append(((ps + pe) // 2, max(p[0], top - 2 * edge), top))
                continue
        elif j == len(rests) - 1 and j > 0 and floor and not closed:
            pass                                        # open, on the floor: all of it (a slow break creeps
                                                        # out of its end, and the walk-in rule is a squat's)
        elif j > 0 and (j == len(rests) - 1 or (not floor and xc[pe] < xc[ps] - TA_START_DROP)):
            pe = min(pe, ps + 2 * edge)                 # after the last rep: its start (then the walk-in); between
                                                        # reps too if it ends lower than it starts (the descent
                                                        # began in it: a slow start still reads flat, D43)
        anchors.append(((ps + pe) // 2, ps, pe))
    # The window's own stills anchor the set's ends too when they're part of it (the hold before the
    # first rep, the bottom a row starts from): within TA_SET_EDGE of the first or last anchor, and
    # not behind a rerack. Further away it's the rack, with a walkout in between. A turnaround within
    # TA_REACH of the still is that same rest, and the still is the better anchor (the coarse pass is
    # least sure near the window's ends).
    # On the floor (D44) a window's stills are the floor further out than TA_SET_EDGE too (that's the rack's
    # walkout, and a deadlift has none). The start still anchors when nothing else anchors the floor before the
    # first coarse top (no low before that top, or only ones the live wait holds back; its held-back tops count as
    # tops): the bar left the floor from it.
    # It also takes over a rest whose coarse low is within TA_FLOOR_REACH of it (the low lags the floor rest by
    # up to that, D41): a still that ends in the dip or the pull leaves the coarse pass nothing before the pull,
    # and its "pause" is the slow pull's flat middle. (The end still needs no rule of its own: a set-down's flat
    # run into the window's end is a coarse stop, above, and the floor after it a rest.)
    end = len(tw) - 1
    # (a window with no coarse top holds no rep: a bump in the setup isn't anchored, and its rise doesn't move the floor)
    bare = floor and len(highs_all) > 0 and (not anchors or anchors[0][0] > highs_all[0])
    if (anchors and anchors[0][0] <= TA_SET_EDGE * fs) or bare:
        anchors = [(0, 0, 0)] + [an for an, k in zip(anchors, ks)
                                 if an[0] > TA_REACH * fs and not (floor and k <= TA_FLOOR_REACH * fs)]
    if anchors and w["anchored"] and not racked and end - anchors[-1][0] <= TA_SET_EDGE * fs:
        anchors = [an for an in anchors if an[0] < end - TA_REACH * fs] + [(end, end, end)]
    w["anchors"] = [i0 + k for k, _, _ in anchors]
    w["rests"] = (i0 + anchors[0][0], i0 + anchors[-1][0], closed) if anchors else None
    v = np.zeros_like(aw)
    if not anchors:
        return v
    # 3. spans
    hit = dilate(imp, int(JOLT_PAD * fs))
    vr = integrate(np.where(hit, 0.0, aw), tw)
    zero = [vr[s0:s1 + 1].mean() for _, s0, s1 in anchors]
    for (s, _, _), (e, _, _), vs, ve in zip(anchors[:-1], anchors[1:], zero[:-1], zero[1:]):
        if e > s:
            v[s:e + 1] = span_velocity(vr, tw, s, e, vs, ve, hit if floor else None)
    if not closed:
        s = anchors[-1][0]
        v[s:] = vr[s:] - zero[-1]
        # Still on the floor rest the window starts from (no top before it): less that still's bias too (D44, as the
        # tail). After a rep the set-down can turn the pod (11-02 set 1: -0.1 m/s^2 after rep 2's), and a floor rest's
        # own reading is too rough (hands on the bar) to stand in for it
        if floor and not any(h < s for h in highs_all):
            v[s:] -= w.get("bias", 0.0) * (tw[s:] - tw[s])
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


def past_hitches(x, v, t, lo, end, hi, vz):
    """A stop near the top that the bar moves on up out of (a hitch at lockout, a grind's second stall) isn't the rep's
    end (D44): the next stop is, found and walked as find_reps finds the end (v at or under vz within MIN_ROM of the top,
    then on while v keeps falling). Moving on: v back over HITCH_V x the rep's peak, and the move rising HITCH_ROM from
    its stop and above the end. A pause's leftover drift (a hair above 0) and its tremble stay under the first, a
    lockout's settle under both, a descent and a rise (the bar going down and up again) under the second. A stop is v = 0:
    what v reads under 0 there, within the stop's own band (down to -HITCH_V x the peak), is drift, and the rise counts
    from that floor (make_fake_deadlift.py held3_hitch raw: the stop read -0.016 m/s and its 12.7 mm rise 7.0 mm, so the
    rep ended at the hitch, +18%). Further down, the bar went down: no credit"""
    pk = v[lo]
    for i in range(lo, hi + 1):
        pk = max(pk, v[i])
    thr = HITCH_V * pk
    stop = end                               # the latest stop near the top: the end, or a blip after it
    while stop < hi:
        k = stop + 1
        while k <= hi and v[k] <= thr:
            k += 1
        if k > hi:
            break                            # never moved on again: the end stands
        e = k + 1                            # the next stop near the top
        while e <= hi and not (v[e] <= vz and x[e] >= x[hi] - MIN_ROM):
            e += 1
        e = min(e, hi)
        while e < hi and v[e] > V_ZERO and v[e + 1] < v[e]:
            e += 1
        fl = min(0.0, min(v[stop:k]))         # the stop's drift floor (above)
        if fl < -thr:
            fl = 0.0
        if (x[e] - x[stop] - fl * (t[e] - t[stop]) >= HITCH_ROM
                and x[e] - x[end] - fl * (t[e] - t[end]) >= HITCH_ROM):
            end = e
        stop = e
    return end


def find_reps(x, v, t, windows, bottom_ref=None, stop_frac=0.0, hitch=False):
    """One rep per bottom-to-top move. Boundaries come from v = 0, never a threshold position:
    stop_frac (V_STOP_FRAC) only says where to start looking, then the boundary is walked to v = 0, or
    to the start of a stop whose drift keeps v just above it (D32).

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
        for w in windows:
            # A closed set's rep tops out by its last anchor. v = 0 after it (the walk-in and rerack aren't
            # reps), so the rerack's drop isn't in the height, and the next move up (the pod pulled off the
            # sleeve) would carry the zigzag's top on into itself: the rep then ends past the set and goes (D43)
            r = w.get("rests")
            if w.get("turns") and r and r[2] and lo < r[1] < hi and x[lo:r[1] + 1].max() - x[lo] >= MIN_ROM:
                hi = lo + int(np.argmax(x[lo:r[1] + 1]))
        seg = np.arange(lo, hi + 1)
        vz = max(V_ZERO, stop_frac * v[seg].max())
        # start: last moment the bar wasn't moving up while still near the bottom
        base = x[lo] if bottom_ref is None else max(x[lo], bottom_ref)
        cand = seg[(v[seg] <= vz) & (x[seg] <= base + MIN_ROM)]
        start = cand[-1] if len(cand) else lo
        if len(cand):                        # back to v = 0, or to where a stop's drift floor ends
            while start > lo and v[start] > V_ZERO and v[start - 1] < v[start]:
                start -= 1
        # end: first moment it stops moving up once it's near the top
        cand = seg[(seg > start) & (v[seg] <= vz) & (x[seg] >= x[hi] - MIN_ROM)]
        end = cand[0] if len(cand) else hi
        if len(cand):                        # on to v = 0, or to where a pause's drift floor begins
            while end < hi and v[end] > V_ZERO and v[end + 1] < v[end]:
                end += 1
            if hitch and x[end] - x[start] >= MIN_ROM:   # on past a hitch or a stall to the top (D44), once
                end = past_hitches(x, v, t, lo, end, hi, vz)   # it's a rep (a smaller rise isn't one yet)
        if end <= start:
            continue                    # a flat stretch (turnaround anchors zero the walkout and rerack)
        rom = x[end] - x[start]
        dur = t[end] - t[start]
        flags = []
        skip = False
        for w in windows:
            if w["i0"] <= end and w["i1"] >= start:
                if w.get("turns"):
                    # its reps lie between the first and the last resting turnaround: before is the
                    # unrack and walkout, after is the walk-in and rerack (unless it's still going)
                    first, last, closed = w["rests"] or (w["i1"], w["i0"], True)
                    if start < first or (end > last and closed):
                        skip = True
                    flags.append("turnaround anchors")
                    if end > last and not closed:
                        flags.append("no end anchor")
                    continue
                if not w["anchored"]:
                    flags.append("no end anchor")
                if w.get("leveled"):
                    if "top_height" in w and abs(x[hi] - (x[w["i0"]] + w["top_height"])) > LEVEL_FLAG:
                        flags.append("uneven lockout")
                elif w["dur"] > LONG_WINDOW:
                    flags.append(f"long window {w['dur']:.1f}s")
                if w["anchored"] and abs(w["raw_end"]) > BIG_CORRECTION:
                    flags.append("big drift correction")
        if skip:
            continue
        leveled = any(w.get("leveled") and w["i0"] <= end and w["i1"] >= start for w in windows)
        reps.append(dict(start=start, end=end, rom=rom, dur=dur, mcv=rom / dur,
                         peak=v[start:end + 1].max(), leveled=leveled, flags=sorted(set(flags))))
    return reps


def sticking_point(x, v, t, rep):
    """Where a hard concentric sticks (analytics.md section 4): velocity rises, dips, rises again. The
    deepest dip with a rise before and after it, STICK_PROM x the rep's peak below the lower of those
    two peaks, inside STICK_RANGE of the ROM; None for a rep that doesn't dip (light ones don't).
    {pos: fraction of the ROM, v: m/s at the dip, depth: m/s, t: s into the rep}. The smoothing is a
    centered mean from running sums, the same in reps.js to the last bit."""
    s, e = rep["start"], rep["end"]
    n = e - s + 1
    if n < 5 or rep["rom"] <= 0:
        return None
    fs = (n - 1) / (t[e] - t[s])
    h = max(1, int(STICK_SMOOTH * fs)) // 2               # int() truncates, like Math.floor (not round: 10.5 at 210 Hz)
    c = np.concatenate([[0.0], np.cumsum(v[s:e + 1])])
    lo = np.maximum(np.arange(n) - h, 0)
    hi = np.minimum(np.arange(n) + h, n - 1)
    vv = (c[hi + 1] - c[lo]) / (hi - lo + 1)
    peak = vv.max()
    left = np.maximum.accumulate(vv)                      # the highest before (and at) each sample
    right = np.maximum.accumulate(vv[::-1])[::-1]         # ... and after
    best = None
    for i in range(1, n - 1):
        if not (vv[i] < vv[i - 1] and vv[i] <= vv[i + 1]):
            continue
        pos = (x[s + i] - x[s]) / rep["rom"]
        if not (STICK_RANGE[0] <= pos <= STICK_RANGE[1]):
            continue
        depth = min(left[i - 1], right[i + 1]) - vv[i]
        if depth >= STICK_PROM * peak and (best is None or depth > best[1]):
            best = (i, depth, pos)
    if best is None:
        return None
    i, depth, pos = best
    return dict(pos=float(pos), v=float(vv[i]), depth=float(depth), t=float(t[s + i] - t[s]))


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
    # A bar lift from the pod: the acceleration along up instead of |a| - g (D40)
    bar = lift in TA_BAR and acc is not None and gyro is not None and np.any(gyro)
    if bar and VERTICAL_FS[0] <= fs <= VERTICAL_FS[1]:
        a = vertical_accel(t, acc, gyro, runs, fs)

    v, windows = velocity_between_anchors(a, t, fs, runs, TA_REST.get(lift, "top") == "bottom")
    # Windows with no still between their reps get turnaround anchors (D40): a broken window, or a bar
    # lift's window holding an impact (the bar hit the hooks or the floor inside it). Not a hand-held
    # phone test, nor a browser's 60 Hz
    real = lift != "phone" and fs >= WALK_MIN_FS
    hits = impact_mask(a, gyro, runs, fs) if real else np.zeros(len(a), bool)
    turns = [w for w in windows if real and w["dur"] >= TA_MIN_WIN
             and (broken_window(v, w) or (lift in TA_BAR and hits[w["i0"]:w["i1"] + 1].any()))]
    for w in turns:
        w["turns"] = True
        w["impacts"] = hits[w["i0"]:w["i1"] + 1]       # the bar hit something: a jolt the gyro confirms
    v = level_heights(a, t, v, windows, lift)
    for w in turns:
        v[w["i0"]:w["i1"] + 1] = turnaround_velocity(a, t, fs, w, lift)
    x = integrate(v, t)
    # the walk to a pause's start (D32): bar and stack lifts at a real sensor rate, not hand-held tests
    walk = lift != "phone" and fs >= WALK_MIN_FS
    # past a hitch or a stall to the top (D44): a bar lift off the floor (the deadlift), where a heavy pull hitches
    # before lockout. Not a stack: no stack recording hitches, and a squeeze or a shrug at the top stops, then rises
    # 8 mm too (a synthetic 3.8 cm shrug read -30%)
    hitch = walk and TA_REST.get(lift) == "bottom" and lift in TA_BAR
    reps = find_reps(x, v, t, windows, usual_bottom(x, lift), V_STOP_FRAC if walk else 0.0, hitch)
    for r in reps:
        r["sticking"] = sticking_point(x, v, t, r)       # analytics.md section 4
    return dict(t=t, fs=fs, g=g, a=a, runs=runs, v=v, x=x, windows=windows, reps=reps,
                ignored=n_candidates - len(runs), lift=lift, mount=mount, noise=noise)


def applied_calibration(csv):
    """What the app already corrected this recording's samples with (D42), as (file, what), or None. It says
    so next to the CSV: the app's session folder in pod.json / link.json ("calibration"; pod-2.json,
    link-2.json, ... for a resumed session's later parts: any of them counts, a CSV doesn't say its part) or
    calibration.json (the phone's 6-face one, D23, written only when it was applied); sessions.py --csv in
    <session id>-calibration.json, with each CSV's part."""
    import re
    csv = Path(csv)
    for p in sorted(csv.parent.glob("*.json")):
        app = re.fullmatch(r"(pod|link|calibration)(-\d+)?\.json", p.name)
        export = p.name.startswith("session-") and p.name.endswith("-calibration.json")
        if not (app or export):
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(d, dict):
            continue
        if app and app.group(1) == "calibration":
            return p.name, "the phone's 6-face calibration (D23)"
        if app:
            c = d.get("calibration")
        else:
            sets, parts = d.get("sets") or {}, d.get("parts") or {}
            if csv.name not in sets:
                continue                                  # another session's
            part = sets[csv.name]
            if part is None:                              # a set stored before the app said its part: any corrected part
                c = next((x["calibration"] for x in parts.values() if isinstance(x, dict) and x.get("calibration")), None)
            else:
                x = parts.get(str(part))
                c = x.get("calibration") if isinstance(x, dict) else None
        if c:
            if isinstance(c, dict) and "accel" in c:
                pod = f"pod 0x{c['pod']:08x}" if isinstance(c.get("pod"), int) else "any pod"
                return p.name, f"the pod calibration {c.get('from', '?')} ({pod}, made {c.get('made', '?')})"
            return p.name, "the phone's 6-face calibration (D23)"
    return None


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
    calib = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--calib=")), None)
    if calib:
        done = applied_calibration(path)
        if done:
            sys.exit(f"{path.name}: --calib refused, {done[0]} next to it says the app already corrected these samples "
                     f"with {done[1]}: {Path(calib).name} on top would correct them twice")
        # the samples as the app corrects them now (D42), and |a| from those
        import calib_pod
        acc, gyro = calib_pod.apply(json.loads(Path(calib).read_text(encoding="utf-8")), acc, gyro)
        a_raw = np.sqrt(acc[:, 0] ** 2 + acc[:, 1] ** 2 + acc[:, 2] ** 2)
    try:
        r = analyze(t, a_raw, lift, acc, gyro)
    except ValueError as err:
        sys.exit(str(err))
    t, fs, g, a, runs, v, x, windows, reps = (r[k] for k in ("t", "fs", "g", "a", "runs", "v", "x", "windows", "reps"))
    if r["ignored"] and not as_json:
        print(f"(ignored {r['ignored']} 'still' periods that were really steady motion)")

    if as_json:
        print(json.dumps({
            "fs": float(fs), "g": float(g), "runs": [[int(s), int(e)] for s, e in runs],
            "lift": r["lift"], "mount": r["mount"],
            "reps": [{"start": int(r["start"]), "end": int(r["end"]), "rom": float(r["rom"]),
                      "dur": float(r["dur"]), "mcv": float(r["mcv"]), "peak": float(r["peak"]),
                      "leveled": r["leveled"], "flags": r["flags"], "sticking": r["sticking"]} for r in reps],
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
