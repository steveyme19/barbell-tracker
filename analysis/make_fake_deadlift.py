"""
Fake deadlift sets with an exact known answer (D44): one generator, named scenarios.

    python make_fake_deadlift.py                        # the guard set parity.mjs checks (GUARDS, raw and cal)
    python make_fake_deadlift.py --list                 # every scenario, what it pins, what it is
    python make_fake_deadlift.py [names] [--seeds=1,2,3] [--sensor=raw|cal|both] [--out=DIR] [--path] [--quiet]
                                                        # DIR/fake_deadlift_<name>_s<seed>_<sensor>.csv + _truth.json
                                                        # (+ _path.npz with --path); DIR defaults to data/
    python make_fake_deadlift.py --all                  # every scenario, seeds 1-3, raw and cal (the D44 suite)

The D44 design workflow built this suite (29 scenarios x 3 seeds x raw/cal = 174 runs) and scored every floor
fix on it; its scripts and results are kept in analysis/out/deadliftfix-2026-11-03/ (gitignored). The prep/*.py
named below are that workflow's measurements of the 11-02 recordings and videos. Pins in --list: RC1 a set-down's
still too late for a coarse stop (D44 rule 4, the stop into the window's end), RC2 a slow pull (its middle passed
the floor's flat test, its rise was under TA_MIN_MOVE: rules 2, 6), RC3 the landing closed at its last bounce
(rule 1), RC4 a still mid-rest that splits the window (rule 2), RC5 a rep that starts before liftoff (the truth's
definition), dip the bar-bend dip, hitch a stop before lockout (rule 9).

Built for the 11-02 gym deadlifts (389 lb, two sets, video truth): set 1 kept hands on the bar between reps
(one window, ROM read 6 to 11 cm high), set 2 let go between reps (each rep its own window between floor
stills: the app logged no set). Modelled on analysis/make_fake_bench.py and make_fake_motion.deadlift_creep,
whose machinery it imports: quintic knots (t, z, v, a) for the vertical path
(through), min-jerk steps, sways and rings as tracks with exact derivatives, landing(), R_POD.

Physics (world: x toward the lifter, y along the bar, z up; heights above the plates resting on the floor):
  - the PLATE path: floor rests, a shaped pull (velocity knots with smooth accelerations, from the 11-02 video:
    v/MCV rising to ~1.45 at 60% of the pull; "fast" peaks earlier; "grind" stalls), a hold at lockout with a
    1 mm sway, a lowering that starts slowly and speeds up into the floor (11-02: -1.0 to -1.2 m/s at the hit,
    still accelerating), the hit (landing(): stopped in 25 ms, ~80 m/s^2), the plates rattling on the floor in
    segments whose jolt runs match the measured ones (SETDOWN: where the last run starts is where span_velocity
    takes the bar to have landed) and a tail ring whose tau sets when the next still can start.
  - the BAR-BEND DIP (11-02 video, prep/dipshape.py): before liftoff the sleeve end (the pod) sinks d in two
    min-jerk steps, a slow pre-sink (PRESINK: ~28% of d over ~1.1 s; measured 1.5 to 3 mm over 0.65 to 1.3 s) then
    the fast phase (the rest of d over T, 0.33 to 0.6 s fitted, ending at the plates' departure: the pod's lowest
    point, v = 0, which the video puts 0.01 to 0.24 s before the plate's 3 mm liftoff), while the sleeve pitches by
    theta (about world x; theta = d / LEVER by default; measured 2.2 to 3.5 deg). The bend does NOT recover in the
    pull (rec "land", the default since D44 prep: the sleeve stays tilted through the top, the pod rides d under the
    plate, measured 0 to 10 mm, and relaxes 0.03 s after the set-down over 0.1 s); rec = s keeps the old variant
    (recovering over the first rec s of the pull, the *_rec scenarios). The pod's path = the plate path + the dip, so
    with "land" the pod's ROM is the plate's. Re-wedges in a hands-on rest are partial dips (no liftoff) plus the bar
    rolled 1.5 cm toward the shins.
  - HANDS: on the bar (the grip, hands-on rests) the floor contact vibrates (20.5 and 16.3 Hz, deadlift_creep's
    CONTACT_HZ; CONTACT_AMP 0.13 m/s^2 since D44 prep, plus bursts of 1.6 to 3.2 x that for 0.12 to 0.35 s every
    0.15 to 0.6 s: |a| 0.2 s std median ~0.12 with ~40% of the time over 0.15, as 11-02's hands-on rests and grips,
    median 0.09 to 0.15, 0 to 69% over 0.15, prep/grip.py; it was a steady 0.6, std 0.5) and the sleeve wobbles in the
    grip (gyro 2 to 6 deg/s); a quiet patch drops that to almost nothing. The hands go on before the bar starts to
    bend: a grip shorter than the dip (pre-sink + fast phase) + 0.1 s starts earlier, out of the rest off the bar.
    Off the bar: the floor is quiet (sensor noise only) or "fidget" (11-02 set 2's minute before the set: 8 to 30 Hz
    shakes, |a| 0.2 s std 0.02 to 0.1, gyro 1 to 7 deg/s, a few bumps), which loosens the noise-matched still limits.
  - ROLL: the sleeve turns back 40 deg per m as it rises (deadlift_creep) and every set-down leaves it at a new
    roll (11-02: -60, -3, -24 deg on set 1; -15, +24 on set 2), so an uncalibrated pod's |a| at rest moves
    (9.87 to 10.04 on 11-02, its roll-dependent offset).
  - SENSOR: the pod's 11-01 estimate (analysis/data/pod-calib-estimate-2026-11-01.json) as its true errors: accel
    bias (-0.1795, -0.041, +0.2809) m/s^2, scale (1.00459, 1.00446, 1.00307), rest noise, LSB 0.002393, clipped at
    +-8 g; gyro bias (0.770, -3.396, 2.197) deg/s, z scale 1.01, noise, LSB 0.035. THE SET-DOWN ARTEFACT (D44 prep,
    prep/setdown.py + clang_table.py): through every 11-02 set-down the pod's vertical integrates to -2.1 to -5.9 m/s
    from first contact to rest where physics says +|v_hit| (net -2.1..-5.9 instead of 0); the attitude isn't it
    (a fixed attitude from the rest after gives the same within 0.15 m/s), no samples are dropped, and no output
    sample comes within 2% of the +-8 g rail (axis peaks 46 to 74 m/s^2), so it isn't the output clip either. The
    loss sits in the jolts: the vertical's big samples are biased down (-30 to -47 m/s^2 lobes outweigh the up
    ones), 62-81% of each burst is over IMPACT_A, and with those blanked (span_velocity's JOLT_PAD mask) the rest
    of the burst keeps only -0.05 to -0.72 m/s. The mean along up in the ~0.5 s burst is 2.4 to 8.2 m/s^2 low (a
    rectification the 210 Hz samples can't explain: a sensor saturating on ringing it doesn't resolve is the
    likeliest cause, not proven). Modelled as an asymmetric rattle: during each rattle segment the world-vertical
    rattle's up half-waves read (1 - asym) and its down half-waves (1 + asym), i.e. -asym |rattle| added (SETDOWN's
    "asym", tuned per kind to 11-02's nets: prep/tune_asym.py); the *_clean scenarios turn it off. At 210 Hz (the 11-02 files'
    timestamps; the INFO odr says 208). "raw" is what the uncalibrated pod writes, "cal" the same draw corrected
    as reps.py --calib does with that file (calib_pod.apply: (raw - bias) / scale, accel and gyro).

The truth (each _truth.json): every rep from the POD's own vertical path (plate + dip): t_start = liftoff (the
pod's lowest point, v = 0: the dip bottoms out as the plates leave the floor; Quentin's rule, D44: a deadlift rep
starts at the pod's own turnaround), t_end = lockout (v = 0); rom = the pod's rise (with the bend held through the
pull, the plate's), mcv = rom / dur; also the plate's rom and mcv (rom_plate, mcv_plate), the dip, the stalls, the hit and
the still runs (the repo's reps.py, for the still delays after each hit). Vibration, sway, rings, fidget and
re-wedges ride on top of the path (all under 3 mm but the re-wedge's 4 mm) and are not in the truth. Every pull
is checked to rise all the way, every lowering to fall all the way, every dip to fall before its liftoff.
_path.npz: per sample t, the pod's path height and velocity (plate + dip), the plate height.
"""

import json
import sys
from pathlib import Path

import numpy as np

import make_fake_motion as M

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
FS = 210.0
G = M.G
SEEDS = (1, 2, 3)
POST = 17.0                     # s hands off after the last set-down: the app's SET_GAP (15 s) closes the set in the file
LEVER = 0.2                     # m: plate centre to the pod (the sleeve end): theta = d / LEVER
ROLL_PER_M = -40.0              # deg per m of plate height (deadlift_creep: the sleeve turns back as it rises)
CONTACT_HZ = M.CONTACT_HZ       # 20.5 Hz
CONTACT_AMP = 0.13              # m/s^2 up: hands on, plates on the floor, between its bursts (CONTACT_BURST)
CONTACT_BURST = dict(rest=dict(gap=(0.15, 0.6), dur=(0.12, 0.35), gain=(1.6, 3.2)),   # hands-on rests: 11-02 set 1, 39-69% over 0.15
                     grip=dict(gap=(0.6, 1.5), dur=(0.10, 0.25), gain=(1.6, 3.2)))   # the grip before a reset pull: set 2, 0-22%
PRESINK = (0.28, 1.1)           # the dip's slow pre-sink: fraction of d, s (11-02: 1.5-3 mm of 6.5-10 over 0.65-1.3 s)
STALL_V = -0.02                 # m/s: a stall may sink this much (11-02 set 2 rep 3: v down to -0.005..-0.014)

# the pod's errors: its 11-01 estimate taken as the truth (raw = scale * true + bias + noise; cal undoes it)
ACC_BIAS = np.array([-0.1795, -0.041, 0.2809])
ACC_SCALE = np.array([1.00459, 1.00446, 1.00307])
ACC_NOISE = np.array([0.0092, 0.0115, 0.0091])      # m/s^2: the estimate's lid-down rest (11-02 set 1's floor: 0.0088)
GYRO_BIAS = np.array([0.7703, -3.3963, 2.1971])
GYRO_SCALE = np.array([1.0, 1.0, 1.01])
GYRO_NOISE = np.array([0.073, 0.051, 0.039])
CAL = dict(accel=dict(bias=ACC_BIAS.tolist(), scale=ACC_SCALE.tolist()),
           gyro=dict(bias=GYRO_BIAS.tolist(), scale=[None, None, 1.01]))

# pull shapes: (fraction of the pull's time, velocity relative to the peak); accelerations from central differences
SHAPES = {
    # 11-02 video, the pod's own concentric (dip bottom to top, prep/pullshape.py), the 4 clean pulls' mean (set 1 reps
    # 1, 3, set 2 reps 1, 2): v / peak at tenths .11 .33 .60 .82 .94 .97 .96 .91 .69, peak 1.45-1.61 x MCV at 55-75%
    # (the first suite's plate-based shape was a little slower off the floor)
    "dl": [(0.1, 0.11), (0.2, 0.33), (0.3, 0.60), (0.4, 0.82), (0.5, 0.94), (0.6, 0.97), (0.7, 0.96), (0.8, 0.91), (0.9, 0.69)],
    # 11-02 set 1 rep 2: a lockout hitch, v ~0.004 m/s at 85-88% of the pull 1.2-1.6 cm under the top, then the top
    # (the knots at 5%, from the video; peak 2.0 x MCV at 60%)
    "hitch": [(0.05, 0.08), (0.10, 0.18), (0.15, 0.32), (0.20, 0.44), (0.25, 0.54), (0.30, 0.62), (0.35, 0.67), (0.40, 0.70),
              (0.45, 0.77), (0.50, 0.89), (0.55, 0.98), (0.60, 1.0), (0.65, 0.85), (0.70, 0.69), (0.75, 0.51), (0.80, 0.22),
              (0.84, 0.03), (0.88, 0.03), (0.92, 0.14), (0.96, 0.18)],
    # a light, fast pull: peaks at 45%, decelerates over the second half
    "fast": [(0.1, 0.35), (0.2, 0.70), (0.3, 0.92), (0.45, 1.0), (0.6, 0.94), (0.75, 0.78), (0.9, 0.45)],
    # the grind, 11-02 set 2 rep 3 as measured (3.17 s pod concentric, prep/pullshape.py): peak at 40%, a first stall at
    # 64-69% of the time at 0.78 ROM (v ~0), up again, a second at 85-89% at 0.89 ROM sinking a little (v to -0.005..-0.014),
    # then a fast finish
    "grind": [(0.05, 0.14), (0.10, 0.24), (0.15, 0.47), (0.20, 0.64), (0.25, 0.82), (0.30, 0.92), (0.35, 0.95), (0.40, 1.0),
              (0.45, 0.97), (0.50, 0.88), (0.55, 0.61), (0.60, 0.27), (0.64, 0.02), (0.69, 0.02), (0.74, 0.36), (0.79, 0.55),
              (0.84, 0.05), (0.865, -0.025), (0.89, -0.025), (0.93, 0.62), (0.96, 0.92)],
    # the first suite's guess at that grind (one 1.1 s stall at 2-3 cm/s, ~80% up): kept for reset_slow3_stall1
    "grind_long": [(0.05, 0.45), (0.12, 0.92), (0.20, 1.0), (0.31, 1.0), (0.38, 0.72), (0.42, 0.13), (0.46, 0.055),
                   (0.71, 0.055), (0.78, 0.13), (0.86, 0.40), (0.93, 0.34), (0.97, 0.16)],
    # a slow single with no stall: an even grind
    "slow": [(0.1, 0.40), (0.2, 0.75), (0.35, 0.95), (0.5, 1.0), (0.65, 0.92), (0.8, 0.72), (0.92, 0.40)],
}
LOWER = [(0.15, 0.15), (0.40, 0.36), (0.65, 0.48), (0.85, 0.72)]   # (fraction of time, of the hit speed): slow, then let go

ASYM_K = dict(clang=0.25, clang_mid=0.36, clang_mid2=0.28, clang_one=0.41, clang_late=0.33)   # tuned: prep/tune_asym.py

# set-downs: the hit (landing(): stopped in `stop` s), then the plates rattle on the floor: segments (s after the hit, s,
# m/s^2 of vertical rattle at 23 Hz, with fore-aft, along-bar and the sleeve's rattle in proportion), then a decaying
# tail (s after, m/s^2, tau) that sets when the next still can start. Measured on the real set-downs (the algorithm's
# |vertical| over IMPACT_A dilated by JOLT_PAD, the `hit` mask span_velocity closes a floor span on, s after the first
# jolt): 11-02 set 1 [0,.10][.15,.22][.25,.38] / [0,.16][.18,.45] / [0,.45]; set 2 [0,.09][.10,.41] / [0,.10][.12,.42] /
# [0,.10][.11,.24][.27,.33][.34,.41][.43,.48]; 10-31 one run each, 0.26 to 0.38 s. Where the LAST run starts is where
# span_velocity takes the bar to have landed. |a - g| per 0.1 s after 11-02's hits ~65 69 60 30 15 then 1.6, 0.4;
# the next still 0.67, 1.01, 0.74 s after (set 2's loose limits); the gyro 200 to 480 deg/s for 0.4 s (rms per axis
# x 75-141, y 35-60, z = bar 23-61)
# "asym": the set-down artefact (the docstring): the vertical rattle reads -asym |rattle| more, tuned so the fakes'
# vertical loses what 11-02's lost (prep/setdown.py on the fakes vs the real: net through the set-down set 1 -2.7 / -3.7 /
# -5.9 m/s, set 2 -2.7 / -2.1 / -3.2). Only the 11-02 kinds carry it; short/long/long_one (the synthetic isolation
# controls) and the touches don't
SETDOWN = {
    "clang": dict(stop=0.025, rattle=[(0.0, 0.08, 55.0), (0.135, 0.40, 35.0)], tail=(0.40, 12.0, 0.07), asym=ASYM_K["clang"]),
    "clang_mid": dict(stop=0.025, rattle=[(0.0, 0.08, 60.0), (0.155, 0.21, 40.0), (0.265, 0.37, 35.0)],          # set 1 r1
                      tail=(0.37, 12.0, 0.07), asym=ASYM_K["clang_mid"]),
    "clang_mid2": dict(stop=0.025, rattle=[(0.0, 0.13, 60.0), (0.19, 0.43, 40.0)], tail=(0.43, 12.0, 0.07),     # set 1 r2
                       asym=ASYM_K["clang_mid2"]),
    "clang_one": dict(stop=0.025, rattle=[(0.0, 0.43, 45.0)], tail=(0.43, 12.0, 0.07), asym=ASYM_K["clang_one"]), # set 1 r3
    "clang_late": dict(stop=0.025, rattle=[(0.0, 0.09, 55.0), (0.125, 0.23, 40.0), (0.28, 0.32, 32.0), (0.355, 0.40, 30.0),
                                           (0.445, 0.47, 28.0)], tail=(0.47, 10.0, 0.06), asym=ASYM_K["clang_late"]),   # set 2 r3
    "short": dict(stop=0.025, rattle=[(0.0, 0.08, 55.0), (0.135, 0.20, 32.0)], tail=(0.20, 8.0, 0.04)),         # still ~0.4 s
    "long": dict(stop=0.025, rattle=[(0.0, 0.08, 55.0), (0.135, 0.40, 35.0)], tail=(0.40, 15.0, 0.16)),         # still ~1.2 s
    "long_one": dict(stop=0.025, rattle=[(0.0, 0.43, 45.0)], tail=(0.43, 15.0, 0.16)),                         # both: one run
    "touch": dict(stop=0.025, rattle=[(0.0, 0.03, 25.0)], tail=None),                                           # touch-and-go
    "touch_soft": dict(stop=0.05, rattle=[(0.0, 0.03, 3.0)], tail=None),                                        # under IMPACT_A
}
for _k in ("clang", "clang_mid", "clang_mid2", "clang_one", "clang_late"):   # the *_clean controls' kinds
    SETDOWN[_k + "_clean"] = dict(SETDOWN[_k], asym=0.0)
for _k in ("clang", "clang_mid", "clang_mid2", "clang_late"):   # the *_one controls: one jolt run, the kind's own artefact
    SETDOWN[_k + "_1"] = dict(SETDOWN["clang_one"], asym=SETDOWN[_k]["asym"])
RATTLE_HZ = dict(z=23.0, x=26.0, y=41.0, roll=29.0, tilt=37.0, yaw=31.0)
RATTLE_PER = dict(x=0.55, y=0.18, roll=1.2, tilt=2.8, yaw=1.4)    # m/s^2 per m/s^2 of z; deg/s per m/s^2 of z
TAIL_W = 25.0                                                        # deg/s of tilt ring per 12 m/s^2 of tail


def w2(hz):
    return (2 * np.pi * hz) ** 2


def ring(t, t0, amp, hz, tau):
    """M.ring with its own decay tau: amp e^(-s/tau) (1 - e^(-s/onset))^3 sin(2 pi hz s), s = t - t0."""
    s = np.maximum(t - t0, 0)
    e, o = np.exp(-s / tau), np.exp(-s / M.RING_ONSET)
    decay = (e, -e / tau, e / tau**2)
    onset = (1 - o, o / M.RING_ONSET, -o / M.RING_ONSET**2)
    env = M.mul(decay, M.mul(onset, M.mul(onset, onset)))
    return tuple(k * (t >= t0) for k in M.mul(env, M.sine(t, t0, amp, hz)))


def seg_disp(T, v0, a0, v1, a1):
    """Displacement of the quintic with cubic-Hermite velocity (v0, a0) -> (v1, a1) over T."""
    return T * ((v0 + v1) / 2 + (a0 - a1) * T / 12)


def smooth_knots(ts, vs, a_ends=(0.0, 0.0)):
    """Accelerations at interior knots from central differences of (ts, vs); the ends get a_ends."""
    acs = [a_ends[0]] + [(vs[i + 1] - vs[i - 1]) / (ts[i + 1] - ts[i - 1]) for i in range(1, len(ts) - 1)] + [a_ends[1]]
    return acs


def pull_knots(t_l, tc, rom, shape):
    """Knots after (t_l, 0, 0, 0) up to the lockout (t_l + tc, rom, 0, 0) through SHAPES[shape]; returns knots, peak."""
    rel = SHAPES[shape]
    ts = [t_l] + [t_l + f * tc for f, _ in rel] + [t_l + tc]
    vs = [0.0] + [u for _, u in rel] + [0.0]
    acs = smooth_knots(ts, vs)
    zs = [0.0]
    for i in range(len(ts) - 1):
        zs.append(zs[-1] + seg_disp(ts[i + 1] - ts[i], vs[i], acs[i], vs[i + 1], acs[i + 1]))
    k = rom / zs[-1]
    return [(ts[i], k * zs[i], k * vs[i], k * acs[i]) for i in range(1, len(ts))]


def lower_knots(t_h, rom, T, v_hit, a_hit, p_hit):
    """Knots after the hold's end (t_h, rom, 0, 0) to the hit (t_h + T, p_hit, v_hit, a_hit): interior speeds
    k * rel * v_hit (LOWER), k solved so the path lands on p_hit."""
    ts = [t_h] + [t_h + f * T for f, _ in LOWER] + [t_h + T]

    def path(k):
        vs = [0.0] + [k * u * v_hit for _, u in LOWER] + [v_hit]
        acs = smooth_knots(ts, vs, (0.0, a_hit))
        zs = [rom]
        for i in range(len(ts) - 1):
            zs.append(zs[-1] + seg_disp(ts[i + 1] - ts[i], vs[i], acs[i], vs[i + 1], acs[i + 1]))
        return vs, acs, zs
    d0, d1 = path(0.0)[2][-1], path(1.0)[2][-1]
    k = (p_hit - d0) / (d1 - d0)
    vs, acs, zs = path(k)
    assert abs(zs[-1] - p_hit) < 1e-12
    return [(ts[i], zs[i], vs[i], acs[i]) for i in range(1, len(ts))]


def rep(tc, rom, shape="dl", before=("off", 3.0, 0.8), dip=(0.008, 0.45), theta=None, rec="land", hold=0.5, lower=1.4,
        v_hit=-1.05, a_hit=-1.5, setdown="clang", droll=0.0, rewedges=(), quiet=None, presink=PRESINK, dip_steps=None):
    """One deadlift rep and how it starts. tc: liftoff to lockout s (the pod's dip bottom to its top); rom: the PLATE's
    rise m (the truth's rom is the pod's: the same with the bend held, + d when it recovers in the pull); shape: SHAPES;
    before: what comes between the previous set-down (or the start of the file) and this liftoff: ("off", s hands off,
    s of grip) | ("on", s hands on) | ("tng", s on the floor after a touch); dip: (d m, T s) the bend before liftoff
    (d 0: none), T its fast phase; presink: (fraction of d, s) the slow pre-sink before it; dip_steps: [(fraction of d,
    s), ...] explicit sequential min-jerk steps ending at the liftoff instead (fractions sum to 1); theta: its pitch
    deg (default d / LEVER); rec: "land" (the bend persists to the set-down: the default, as 11-02) or s the dip takes
    to recover after liftoff (the old model); hold: s at lockout; lower: s from the hold to the hit, hitting at v_hit
    m/s still speeding up at a_hit m/s^2; setdown: SETDOWN; droll: deg the sleeve ends up turned by this set-down;
    rewedges: s after the previous set-down (hands on); quiet: (s after the previous set-down, s long): a quiet patch
    in a hands-on rest."""
    d, T = dip
    if dip_steps is None:
        dip_steps = [(presink[0], presink[1]), (1.0 - presink[0], T)] if presink and presink[0] > 0 else [(1.0, T)]
    assert abs(sum(f for f, _ in dip_steps) - 1.0) < 1e-9, dip_steps
    return dict(tc=tc, rom=rom, shape=shape, before=before, dip=dip, dip_steps=[list(x) for x in dip_steps], theta=theta,
                rec=rec, hold=hold, lower=lower, v_hit=v_hit, a_hit=a_hit, setdown=setdown, droll=droll,
                rewedges=list(rewedges), quiet=quiet)


def fidget(t, rng, spans, level):
    """The platform and the bar disturbed with nobody lifting (11-02 set 2's half minute before the set): a hum (two
    steady 9-22 Hz tones, ~0.05 m/s^2 std at level 0.07; 11-02 set 2's floor reads 0.09-0.10, reps.py's still noise 0.055), shakes of
    0.4 to 2.5 s at 8 to 30 Hz (level x 0.4 to 1.6 m/s^2 up, 0.6 x that fore-aft), now and then a bump (x 4), and a
    slow wobble of the sleeve (0.1 to 0.5 deg at 0.5 to 2 Hz: gyro 0.3 to 6 deg/s)."""
    n = len(t)
    z, x, tilt, yaw = M.track(n), M.track(n), M.track(n), M.track(n)
    for a, b in spans:
        # the hum between the shakes (D44 prep: 11-02 set 2's floor reads |a| 0.2 s std 0.09-0.10 throughout, reps.py's still noise
        # 0.055): two steady tones, 0.9 and 0.55 x level, ~0.05 m/s^2 std at the default level
        h1, h2 = rng.uniform(9.0, 22.0), rng.uniform(9.0, 22.0)
        rp = min(0.2, (b - a) / 3)
        z = M.add(z, M.sway(t, a, b, 0.9 * level / w2(h1), hz=h1, ramp=rp, phase=rng.uniform(0, 6.28)),
                  M.sway(t, a, b, 0.55 * level / w2(h2), hz=h2, ramp=rp, phase=rng.uniform(0, 6.28)))
        tt = a + rng.uniform(0.0, 0.3)
        while tt < b - 0.4:
            e = min(b, tt + rng.uniform(0.4, 2.5))
            hz = rng.uniform(8, 30)
            A = level * rng.uniform(0.4, 1.6) * (4.0 if rng.uniform() < 0.2 else 1.0)
            rp = min(0.1, (e - tt) / 2)
            ph = rng.uniform(0, 2 * np.pi)
            z = M.add(z, M.sway(t, tt, e, A / w2(hz), hz=hz, ramp=rp, phase=ph))
            x = M.add(x, M.sway(t, tt, e, 0.6 * A / w2(1.1 * hz), hz=1.1 * hz, ramp=rp, phase=ph + 1.0))
            whz = rng.uniform(0.5, 2.0)
            rw = min((e - tt) / 2, 0.25)
            tilt = M.add(tilt, M.sway(t, tt, e, rng.uniform(0.1, 0.5), hz=whz, ramp=rw, phase=rng.uniform(0, 6.28)))
            yaw = M.add(yaw, M.sway(t, tt, e, rng.uniform(0.1, 0.4), hz=whz * 1.3, ramp=rw, phase=rng.uniform(0, 6.28)))
            tt = e + rng.uniform(0.0, 0.8)
    return z, x, tilt, yaw


def hands(t, rng, spans, amp):
    """Hands on the bar, plates on the floor: the floor contact's 20.5 Hz (amp m/s^2 up) and a 16.3 Hz partner, fore-aft
    too, and the sleeve wobbling in the grip (0.15 to 0.3 deg at 1.5 to 3 Hz). spans: (a, b, gain)."""
    n = len(t)
    z, x, tilt, yaw = M.track(n), M.track(n), M.track(n), M.track(n)
    for a, b, gain, kind in spans:
        if b - a < 0.05:
            continue
        rp = min(0.15, (b - a) / 2)
        A = amp * gain
        ph = rng.uniform(0, 2 * np.pi)
        z = M.add(z, M.sway(t, a, b, A / w2(CONTACT_HZ), hz=CONTACT_HZ, ramp=rp, phase=ph),
                  M.sway(t, a, b, 0.6 * A / w2(16.3), hz=16.3, ramp=rp, phase=ph + 2.0))
        x = M.add(x, M.sway(t, a, b, 0.8 * A / w2(1.05 * CONTACT_HZ), hz=1.05 * CONTACT_HZ, ramp=rp, phase=ph + 0.7))
        cb = CONTACT_BURST[kind]
        tb = a + rng.uniform(*cb["gap"])                 # the hands shifting on the bar: bursts (prep/grip.py)
        while tb < b - 0.15:
            eb = min(b, tb + rng.uniform(*cb["dur"]))
            Ab = A * rng.uniform(*cb["gain"])
            hb = rng.uniform(14.0, 26.0)
            z = M.add(z, M.sway(t, tb, eb, Ab / w2(hb), hz=hb, ramp=min(0.04, (eb - tb) / 3), phase=rng.uniform(0, 6.28)))
            tb = eb + rng.uniform(*cb["gap"])
        whz = rng.uniform(1.5, 3.0)
        g2 = max(gain, 0.1)
        tilt = M.add(tilt, M.sway(t, a, b, g2 * rng.uniform(0.15, 0.3), hz=whz, ramp=rp, phase=rng.uniform(0, 6.28)))
        yaw = M.add(yaw, M.sway(t, a, b, g2 * rng.uniform(0.1, 0.25), hz=1.2 * whz, ramp=rp, phase=rng.uniform(0, 6.28)))
    return z, x, tilt, yaw


def deadlift(spec, seed=1):
    """A whole deadlift set as the pod sees it: t, raw accel (m/s^2, with g, sensor frame), raw gyro (deg/s), the
    truth. spec: reps (list of rep()), floor ("quiet" | "fidget"), fidget (m/s^2 level), lead (s quiet at the very
    start before a fidget), roll0 (deg at rest before the set; 11-02: set 1 -160, set 2 112), contact (m/s^2),
    post (s hands off after the last set-down)."""
    rng = np.random.default_rng(seed)
    rows = spec["reps"]
    floor = spec.get("floor", "quiet")
    amp_c = spec.get("contact", CONTACT_AMP)
    zk = [(0.0, 0.0, 0, 0)]
    hands_on, hands_off, hits, dips, rolls, holds, rewedges, quiets = [], [], [], [], [], [], [], []
    out = []
    t_prev = 0.0                                       # the previous set-down's end (0: the start of the file)
    for k, r in enumerate(rows):
        b = r["before"]
        if b[0] == "off":
            t_l = t_prev + b[1] + b[2]
            need = sum(s_ for _, s_ in r["dip_steps"]) + 0.1 if r["dip"][0] else 0.0
            t_grip = t_l - max(b[2], need)            # the hands are on before the bar starts to bend (the pre-sink)
            assert t_grip > t_prev + 0.5, f"rep {k + 1}: no time off the bar before the grip"
            hands_off.append((t_prev, t_grip))
            # the grip: calm on a fidgety floor (11-02 set 2's, and set 1 rep 1's: 0-22% over 0.15), busy on a quiet one
            # (no 11-02 data: the hands-on rests' bursts, so a quiet floor's stills stay the floor's and its limits tight)
            hands_on.append([t_grip, t_l, 1.0, "grip" if spec.get("grip", "calm" if floor == "fidget" else "busy") == "calm" else "rest"])
        elif b[0] == "on":
            assert k > 0, "rep 1 starts from the floor, hands off"
            t_l = t_prev + b[1]
            spans = [[t_prev, t_l, 1.0, "rest"]]
            if r["quiet"]:
                q0, ql = t_prev + r["quiet"][0], r["quiet"][1]
                spans = [[t_prev, q0, 1.0, "rest"], [q0, q0 + ql, 0.04, "rest"], [q0 + ql, t_l, 1.0, "rest"]]
            hands_on += spans
            if r["quiet"]:
                quiets.append((t_prev + r["quiet"][0], t_prev + r["quiet"][0] + r["quiet"][1]))
            for tw in r["rewedges"]:
                rewedges.append(t_prev + tw)
        elif b[0] == "tng":
            assert k > 0
            t_l = t_prev + b[1]
            hands_on.append([t_prev, t_l, 0.5, "rest"])
        else:
            raise ValueError(b)
        d, T = r["dip"]
        if d:
            Ttot = sum(s_ for _, s_ in r["dip_steps"])
            assert Ttot < t_l - t_prev - 0.05, f"rep {k + 1}: the dip starts before the bar is on the floor"
            theta = r["theta"] if r["theta"] is not None else float(np.degrees(d / LEVER))
            downs, te = [], t_l                        # sequential steps ending at the liftoff
            for f_, s_ in reversed(r["dip_steps"]):
                downs.insert(0, (te - s_, s_, -f_ * d))
                te -= s_
            dips.append(dict(rep=k + 1, d=d, T=Ttot, t_l=t_l, rec=r["rec"], theta=theta, downs=downs))
        zk.append((t_l, 0.0, 0.0, 0.0))                # liftoff
        up = pull_knots(t_l, r["tc"], r["rom"], r["shape"])
        t_top = t_l + r["tc"]
        zk += up
        hold = r["hold"]
        zk.append((t_top + hold, r["rom"], 0.0, 0.0))
        holds.append((t_top, t_top + hold))
        sd = SETDOWN[r["setdown"]]
        v_hit = r["v_hit"]
        p_hit = -v_hit * sd["stop"] / 2
        t_hit = t_top + hold + r["lower"]
        zk += lower_knots(t_top + hold, r["rom"], r["lower"], v_hit, r["a_hit"], p_hit)
        zk.append((t_hit + sd["stop"], 0.0, 0.0, 0.0))
        hits.append(dict(rep=k + 1, t=t_hit, v=v_hit, stop=sd["stop"], kind=r["setdown"]))
        if r["droll"]:
            rolls.append((t_top + hold, r["lower"] + 0.3, r["droll"]))
        out.append(dict(rep=k + 1, t_liftoff=t_l, t_start=t_l, t_end=t_top, rom_plate=r["rom"], shape=r["shape"],
                        before=list(b), hold=hold, lower=r["lower"], t_hit=t_hit, v_hit=v_hit, setdown=r["setdown"],
                        droll=r["droll"]))
        t_prev = t_hit + sd["stop"]
    t_last = t_prev
    total = t_last + spec.get("post", POST)
    hands_off.append((t_last, total))
    n = int(round(total * FS)) + 1
    t = np.arange(n) / FS
    zp = M.through(t, zk)
    for h in hits:
        on = (t >= h["t"]) & (t < h["t"] + h["stop"])
        land = M.landing(t, h["t"], h["v"], h["stop"])
        zp = tuple(np.where(on, land[i], zp[i]) for i in range(3))

    # the dips (and the re-wedges: partial dips with no liftoff), and their pitch
    dmoves = []
    pitch = M.track(n)
    for dp in dips:
        if dp["rec"] == "land":
            th = next(h["t"] for h in hits if h["rep"] == dp["rep"])
            up_ = (th + 0.03, 0.1, dp["d"])
        else:
            up_ = (dp["t_l"], dp["rec"], dp["d"])
        dp["up"] = up_
        dmoves += dp["downs"] + [up_]
        c = dp["theta"] / dp["d"]                     # deg per m of dip: the sleeve pitches with it
        pitch = M.add(pitch, M.steps(t, [(a, T_, dd * c) for a, T_, dd in dp["downs"] + [up_]]))
    dip_tr = M.steps(t, dmoves)
    wmoves, wx, wroll = [], [], []
    for tw in rewedges:
        dw = 0.004
        wmoves += [(tw, 0.3, -dw), (tw + 0.35, 0.3, dw)]
        wx += [(tw, 0.4, 0.015)]                       # rolled 1.5 cm toward the shins
        wroll += [(tw, 0.4, float(np.degrees(0.015 / 0.225)))]
    rw_tr = M.steps(t, wmoves)
    pitch = M.add(pitch, M.steps(t, [(a, T_, d_ / LEVER * 180 / np.pi) for a, T_, d_ in wmoves]))
    path = M.add(zp, dip_tr)                           # the pod's own path (the truth's)

    # what rides on top: hands, fidget, the holds' sway, the set-downs
    hz_, hx_, htilt, hyaw = hands(t, rng, hands_on, amp_c)
    if floor == "fidget":
        lead = spec.get("lead", 2.0)
        spans = [(lead if a == 0 else a + 0.6, b) for a, b in hands_off[:-1]]   # not the post: the set closes quietly
        fz, fx, ftilt, fyaw = fidget(t, rng, [(a, b) for a, b in spans if b - a > 0.6], spec.get("fidget", 0.07))
    else:
        fz, fx, ftilt, fyaw = M.track(n), M.track(n), M.track(n), M.track(n)
    for tb, db, A in spec.get("blips", ()):           # a bump on the bar or the platform (0.5 m/s^2 at 15 Hz ...)
        fz = M.add(fz, M.sway(t, tb, tb + db, A / w2(15.0), hz=15.0, ramp=min(0.1, db / 2)))
        ftilt = M.add(ftilt, M.sway(t, tb, tb + db, 0.3, hz=2.0, ramp=min(0.1, db / 2)))
    # a loaded bar is never smooth while it's held: the hold sways (1 mm at 2.7 Hz, deadlift_creep) and trembles, the
    # pull shakes with the effort (0.15 mm at 8.5 Hz: |a| 0.2 s std ~0.3, as 11-02's pulls), the lowering trembles
    # (0.3 mm at 5.5 Hz). Without them a slow stretch (a grind's stall, a lowering's slow start) passes the |a| still
    # test, which 11-02's never did
    pulls = [(r["t_start"], r["t_end"]) for r in out]
    lowers = [(r["t_end"] + r["hold"], r["t_hit"]) for r in out]
    rmp = lambda a, b, x: min(x, (b - a) / 2)
    sway_z = M.add(M.track(n), *[M.sway(t, a, b, 0.001, ramp=(b - a) / 2) for a, b in holds if b > a],
                   *[M.sway(t, a, b, 0.0003, hz=6.0, ramp=rmp(a, b, 0.1)) for a, b in holds if b > a],
                   *[M.sway(t, a, b, 0.00015, hz=8.5, ramp=rmp(a, b, 0.15)) for a, b in pulls],
                   *[M.sway(t, a, b, 0.0003, hz=5.5, ramp=rmp(a, b, 0.15)) for a, b in lowers])
    sway_x = M.add(M.track(n), *[M.sway(t, a, b, 0.0015, ramp=(b - a) / 2, phase=0.0) for a, b in holds if b > a],
                   *[M.sway(t, a, b, 0.0001, hz=7.0, ramp=rmp(a, b, 0.15), phase=1.0) for a, b in pulls],
                   *[M.sway(t, a, b, 0.0003, hz=4.8, ramp=rmp(a, b, 0.15), phase=1.0) for a, b in lowers])
    trem_tilt = M.add(M.track(n), *[M.sway(t, a, b, 0.1, hz=6.0, ramp=rmp(a, b, 0.15), phase=0.3) for a, b in pulls + lowers])
    rz, rx, ry = M.track(n), M.track(n), M.track(n)
    rroll, rtilt, ryaw = M.track(n), M.track(n), M.track(n)
    asym_z = np.zeros(n)
    deg_per = lambda hz: 1 / (2 * np.pi * hz)
    for h in hits:
        sd = SETDOWN[h["kind"]]
        sc = (abs(h["v"]) / 1.05) ** 0.3               # a harder hit rings harder, not much longer (11-02: 1.0 to 1.5 m/s)
        for q, (on, off, A) in enumerate(sd["rattle"]):
            a0, a1, A = h["t"] + on, h["t"] + off, A * sc
            rp = min(0.02, (a1 - a0) / 3)               # a faster envelope would add its own spikes (env'' x amp)
            ph = 0.9 * q
            sw = lambda hz, amp, dph=0.0: M.sway(t, a0, a1, amp, hz=hz, ramp=rp, phase=ph + dph)
            seg = sw(RATTLE_HZ["z"], A / w2(RATTLE_HZ["z"]))
            rz = M.add(rz, seg)
            if sd.get("asym"):                          # the set-down artefact (the docstring): what the pod reads, not
                asym_z -= sd["asym"] * np.abs(seg[2])   # the bar's motion (no position or velocity track)
            rx = M.add(rx, sw(RATTLE_HZ["x"], RATTLE_PER["x"] * A / w2(RATTLE_HZ["x"]), 0.7))
            ry = M.add(ry, sw(RATTLE_HZ["y"], RATTLE_PER["y"] * A / w2(RATTLE_HZ["y"]), 1.3))
            rroll = M.add(rroll, sw(RATTLE_HZ["roll"], RATTLE_PER["roll"] * A * deg_per(RATTLE_HZ["roll"]), 0.4))
            rtilt = M.add(rtilt, sw(RATTLE_HZ["tilt"], RATTLE_PER["tilt"] * A * deg_per(RATTLE_HZ["tilt"]), 2.1))
            ryaw = M.add(ryaw, sw(RATTLE_HZ["yaw"], RATTLE_PER["yaw"] * A * deg_per(RATTLE_HZ["yaw"]), 1.7))
        if sd["tail"]:
            dt, A, tau = sd["tail"]
            rz = M.add(rz, ring(t, h["t"] + dt, A / w2(18.0), 18.0, tau))
            rx = M.add(rx, ring(t, h["t"] + dt, 0.4 * A / w2(21.0), 21.0, tau))
            rtilt = M.add(rtilt, ring(t, h["t"] + dt, TAIL_W * A / 12 * deg_per(15.0), 15.0, tau))
    lin = lambda c: (c * zp[0], c * zp[1], c * zp[2])
    pos = [M.add(lin(-0.08), M.steps(t, wx), hx_, fx, sway_x, rx), ry, M.add(path, rw_tr, hz_, fz, sway_z, rz)]

    # attitude (deg): roll about the bar (the start, -40 deg per m of height, a step per set-down, the re-wedges' roll,
    # the bounces), tilt about world x (3 deg per m, the dips' pitch, the wobbles, the rattle), yaw (-2 deg per m)
    roll = M.add(lin(ROLL_PER_M), M.steps(t, rolls), M.steps(t, wroll), rroll)
    roll = (roll[0] + spec.get("roll0", 112.0), roll[1], roll[2])
    tilt = M.add(lin(3.0), pitch, htilt, ftilt, rtilt, trem_tilt)
    yaw = M.add(lin(-2.0), hyaw, fyaw, ryaw)
    phi, phi_rate = -np.radians(roll[0]), -np.radians(roll[1])
    Ry, Rt, Rp = M.rots([0, 0, 1], np.radians(yaw[0])), M.rots([1, 0, 0], np.radians(tilt[0])), M.rots([0, 0, 1], phi)
    R = Ry @ Rt @ M.R_POD @ Rp
    w_world = np.einsum("nji,j->ni", Rt, [0, 0, 1.0]) * np.radians(yaw[1])[:, None] + np.outer(np.radians(tilt[1]), [1, 0, 0])
    w_b = np.einsum("nji,nj->ni", Rp, w_world @ M.R_POD) + np.outer(phi_rate, [0, 0, 1])
    acc_w = np.column_stack([p[2] for p in pos])
    acc_w[:, 2] += asym_z                              # the set-down artefact, world up (what the pod reads)
    f_s = np.einsum("nji,nj->ni", R, acc_w + np.array([0, 0, G]))
    raw = ACC_SCALE * f_s + ACC_BIAS + rng.normal(0, 1, f_s.shape) * ACC_NOISE
    raw = np.clip(np.round(raw / M.POD_ACC_LSB) * M.POD_ACC_LSB, -M.POD_ACC_MAX, M.POD_ACC_MAX)
    gyro = GYRO_SCALE * np.degrees(w_b) + GYRO_BIAS + rng.normal(0, 1, w_b.shape) * GYRO_NOISE
    gyro = np.clip(np.round(gyro / M.POD_GYRO_LSB) * M.POD_GYRO_LSB, -M.POD_GYRO_MAX, M.POD_GYRO_MAX)

    # the truth, checked: the pod up all the way through each pull, the plates down all the way through each lowering,
    # the pod down through each dip before its liftoff
    dip_at = lambda ts: M.steps(np.asarray(ts, float), dmoves) if dmoves else (np.zeros(len(ts)),) * 3
    for r, spec_r in zip(out, rows):
        tf = np.linspace(r["t_start"], r["t_end"], 4001)
        vf = M.through(tf, zk)[1] + dip_at(tf)[1]
        v_floor = STALL_V if spec_r["shape"] == "grind" else 0.0
        assert (vf[1:-1] > v_floor).all(), f"rep {r['rep']}: the pod doesn't go up all the way"
        td = np.linspace(r["t_end"] + r["hold"], r["t_hit"], 4001)
        assert (M.through(td, zk)[1][1:-1] < 0).all(), f"rep {r['rep']}: the lowering doesn't go down all the way"
        dp = next((x for x in dips if x["rep"] == r["rep"]), None)
        z_lo = float(dip_at([r["t_start"]])[0][0])
        z_hi = float(r["rom_plate"] + dip_at([r["t_end"]])[0][0])
        if dp:
            tdp = np.linspace(dp["t_l"] - dp["T"], dp["t_l"], 2001)
            assert (dip_at(tdp)[1][1:-1] <= 0).all() and (M.through(tdp, zk)[1] == 0).all(), f"rep {r['rep']}: the dip"
            assert float(dip_at([dp["t_l"]])[0][0]) == min(float(dip_at([x])[0][0]) for x in tdp), f"rep {r['rep']}: the dip's bottom"
            r["dip"] = dict(d=dp["d"], T=dp["T"], t_start=dp["t_l"] - dp["T"], theta_deg=dp["theta"], rec=dp["rec"],
                            steps=[list(map(float, x)) for x in dp["downs"]], up=list(map(float, dp["up"])))
        r.update(rom=z_hi - z_lo, dur=r["t_end"] - r["t_start"], peak_v=float(vf.max()), t_peak=float(tf[np.argmax(vf)]),
                 z_bottom=z_lo, z_top=z_hi)
        r["mcv"] = r["rom"] / r["dur"]
        r["mcv_plate"] = r["rom_plate"] / r["dur"]
        if spec_r["shape"] in ("grind", "grind_long", "hitch"):   # the stalls / the hitch: where the pull runs under 2 cm/s
            slow = (vf < 0.02) & (tf > tf[0] + 0.2 * r["dur"]) & (tf < tf[-1] - 0.03 * r["dur"])
            zf = M.through(tf, zk)[0]
            r["stalls"] = []
            for a_, b_ in ((g[0], g[-1]) for g in np.split(np.flatnonzero(slow), np.flatnonzero(np.diff(np.flatnonzero(slow)) > 1) + 1) if len(g)):
                r["stalls"].append(dict(t0=float(tf[a_]), t1=float(tf[b_]), dur=float(tf[b_] - tf[a_]), z=float(zf[a_:b_ + 1].mean()),
                                        frac=float(zf[a_:b_ + 1].mean() / r["rom_plate"]), below_top_m=float(r["rom_plate"] - zf[a_:b_ + 1].mean()),
                                        v_min=float(vf[a_:b_ + 1].min())))
    a_mag = np.linalg.norm(acc_w + [0, 0, G], axis=1)
    w_mag = np.linalg.norm(np.degrees(w_b), axis=1)
    for h in hits:
        m = (t >= h["t"] - 0.05) & (t < h["t"] + 0.8)
        h.update(peak_abs_a_minus_g=float(np.abs(a_mag[m] - G).max()), peak_gyro=float(w_mag[m].max()),
                 jolt_s=float(np.ptp(t[m][np.abs(a_mag[m] - G) > 20.0])) if (np.abs(a_mag[m] - G) > 20.0).any() else 0.0,
                 clipped=int((np.abs(raw[m]) >= M.POD_ACC_MAX - 1e-9).any(axis=1).sum()),
                 asym=float(SETDOWN[h["kind"]].get("asym", 0.0)),
                 artefact_dv=float(np.sum(asym_z[(t >= h["t"]) & (t < h["t"] + 1.0)]) / FS))
    info = dict(hands_on=[[round(a, 4), round(b, 4), g, k] for a, b, g, k in hands_on], hands_off=hands_off, holds=holds,
                rewedges=rewedges, quiet_patches=quiets, dips=dips, hits=hits, roll_steps=rolls,
                roll0=spec.get("roll0", 112.0), floor=floor,
                clipped_samples=int((np.abs(raw) >= M.POD_ACC_MAX - 1e-9).any(axis=1).sum()))
    return t, raw, gyro, dict(reps=out, info=info, total=total, z_knots=[list(map(float, k)) for k in zk],
                              dip_moves=[list(map(float, m)) for m in dmoves],
                              path=dict(z=path[0], v=path[1], plate=zp[0]))


def card_stills(t, a_raw):
    """The still runs where the closed card looks: the session segment holding the set (sets.find_sets' cut, LEAD
    before its first active block, TRAIL after its last), then reps.analyze()'s first steps: runs (s, file time),
    noise, limits, the segment. A diagnostic (when the still after each set-down begins), not the truth."""
    import reps as R
    import sets as SS
    tt = t - t[0]
    active, _ = SS.block_activity(tt, a_raw)
    segs = SS.find_segments(active)
    b0, b1 = max(segs, key=lambda s: s[1] - s[0])      # the set's (the longest)
    i0 = int(np.searchsorted(tt, b0 * SS.BLOCK - SS.LEAD))
    i1 = int(np.searchsorted(tt, (b1 + 1) * SS.BLOCK + SS.TRAIL)) - 1
    t0 = float(t[i0])
    t, a_raw = t[i0:i1 + 1], a_raw[i0:i1 + 1]
    t = t - t[0]
    fs = 1 / np.median(np.diff(t))
    g = np.median(a_raw[t < 0.3])
    runs = R.find_still_runs(a_raw, g, fs)
    g = np.mean(np.concatenate([a_raw[s:e + 1] for s, e in runs]))
    runs = R.find_still_runs(a_raw, g, fs)
    std_max, dev_max, noise = R.still_limits(a_raw, fs, runs)
    if std_max < R.STILL_STD or dev_max < R.STILL_DEV:
        runs = R.find_still_runs(a_raw, g, fs, std_max, dev_max)
        g = np.mean(np.concatenate([a_raw[s:e + 1] for s, e in runs]))
        runs = R.find_still_runs(a_raw, g, fs, std_max, dev_max)
    runs = R.confirm_still_runs(a_raw - g, t, fs, runs)
    return dict(runs=[[round(float(t[s]) + t0, 3), round(float(t[e]) + t0, 3)] for s, e in runs], noise=float(noise),
                std_max=float(std_max), dev_max=float(dev_max), g=float(g), segment=[round(t0, 3), round(t0 + float(t[-1]), 3)],
                segments=len(segs))


# ---- the scenarios ----
S = {}


def scen(name, pins, note, **spec):
    S[name] = dict(pins=list(pins), note=note, spec=spec)


# 11-02 set 2 (hands off between reps, the app logged no set), from the video truth (analysis/out/deadliftfix-2026-11-03/d44/truth/, pod time): the pod's
# concentric 1.80 / 2.00 / 3.17 s, 430.0 / 424.6 / 451.0 mm (rep 3 the double-stall grind); dips 9.3 / 6.5 / 7.0 mm at
# pitch 3.5 / 2.4 / 2.4 deg, shapes fitted (prep/dipshape.py: rep 1 sank 2 mm, then 6 mm fast 0.6-0.3 s before the bottom,
# then 1.3 mm slowly; reps 2-3 1.5 / 2.75 mm over 1.3 / 1.2 s, then the fast phase over 0.45 / 0.55 s); top to set-down
# 1.54 / 1.84 / 2.41 s, hits at 0.91 / 0.94 / 1.11 m/s (video); set-down to the next dip bottom 4.1 / 3.6 s (a ~0.8 s grip
# in it); rolls 112 -> 97 -> ~100 -> 121
SET2 = [rep(1.80, 0.430, "dl", ("off", 14.0, 0.85), (0.0093, 0.3), theta=3.5, hold=0.25, lower=1.29, v_hit=-0.91, droll=-15.0,
            dip_steps=[(0.22, 1.0), (0.64, 0.3), (0.14, 0.5)]),
        rep(2.00, 0.4246, "dl", ("off", 3.3, 0.8), (0.0065, 0.45), theta=2.36, hold=0.3, lower=1.54, v_hit=-0.94, droll=5.0,
            presink=(0.23, 1.3)),
        rep(3.17, 0.451, "grind", ("off", 2.8, 0.8), (0.0070, 0.55), theta=2.38, hold=0.3, lower=2.11, v_hit=-1.11, droll=19.0,
            setdown="clang_late", presink=(0.39, 1.2))]
scen("reset_slow3", ["RC1", "RC2"], "11-02 set 2: hands off between reps (a quiet floor still each time: every rep its own "
     "window), pods' concentric 1.80/2.00/3.17 s (rep 3 the double-stall grind), dips 9.3/6.5/7.0 mm held through the "
     "pull, bouncing set-downs with the set-down artefact, the still 0.6-0.8 s after the hit, set 2's fidgety floor "
     "(loose still limits)", reps=SET2, floor="fidget", roll0=112.0)
scen("reset_slow3_quiet", ["RC1", "RC2"], "reset_slow3 on a quiet floor (tight noise-matched still limits, as 11-02 set 1)",
     reps=SET2, floor="quiet", roll0=112.0)
scen("reset_slow3_late", ["RC2"], "reset_slow3 whose set-downs ring on for ~1.2 s (the still starts late enough for the "
     "coarse pass's landing stop): RC1 taken out", reps=[dict(r, setdown="long") for r in SET2], floor="fidget", roll0=112.0)
scen("reset_slow3_one", ["RC1", "RC2"], "reset_slow3 whose set-downs rattle in one jolt run (10-31's kind: span_velocity's "
     "landing is the true hit), the still still 0.6-0.7 s after", reps=[dict(r, setdown=r["setdown"] + "_1") for r in SET2], floor="fidget",
     roll0=112.0)
scen("reset_slow3_clean", ["RC1", "RC2"], "reset_slow3 without the set-down artefact (the vertical keeps the stop's impulse "
     "through the rattle: the old sensor model)", reps=[dict(r, setdown=r["setdown"] + "_clean") for r in SET2], floor="fidget", roll0=112.0)
scen("reset_slow3_rec", ["dip"], "reset_slow3 with the old bend model: the dip recovers over the first 0.3 s of the pull (the pod "
     "rises d more than the plate)", reps=[dict(r, rec=0.3) for r in SET2], floor="fidget", roll0=112.0)
scen("reset_slow3_hitch", ["hitch"], "reset_slow3 with rep 2 hitched at lockout (11-02 set 1 rep 2's: v ~0.004 m/s for ~0.12 s "
     "1.3 cm under the top)", reps=[SET2[0], dict(SET2[1], tc=2.20, shape="hitch"), SET2[2]], floor="fidget", roll0=112.0)
scen("reset_slow3_stall1", ["RC2"], "reset_slow3 whose rep 3 is the old grind guess (one 1.1 s stall at 2-3 cm/s, 80% up, 2.97 s)",
     reps=SET2[:2] + [dict(SET2[2], tc=2.97, shape="grind_long")], floor="fidget", roll0=112.0)
scen("reset_short_end", ["RC1"], "reset style, medium pulls (MCV ~0.33), each set-down's still only ~0.4 s after the hit "
     "(synthetic set-downs: no artefact)",
     reps=[rep(1.35, 0.45, "dl", ("off", 6.0, 0.8), (0.008, 0.4), hold=0.5, lower=1.2, setdown="short", droll=-10.0),
           rep(1.40, 0.45, "dl", ("off", 3.0, 0.8), (0.008, 0.4), hold=0.5, lower=1.2, setdown="short", droll=8.0),
           rep(1.50, 0.45, "dl", ("off", 3.0, 0.8), (0.008, 0.4), hold=0.5, lower=1.2, setdown="short", droll=-6.0)],
     floor="quiet", roll0=60.0)
for _d, _T, _nm in ((0.0, 0.4, "dip0"), (0.015, 0.6, "dip15")):
    scen(f"reset3_{_nm}", ["dip"], f"reset_slow3 with {'no bend dip (the control)' if not _d else 'a 15 mm dip, its fast phase over 0.6 s'}",
         reps=[dict(r, dip=(_d, _T), dip_steps=([(PRESINK[0], PRESINK[1]), (1 - PRESINK[0], _T)] if _d else None)) for r in SET2],
         floor="fidget", roll0=112.0)

# a long sink into the dip before each pull (D44's held-out physics, TA_FLOOR_REACH): a 1.2 s pre-sink and the fast phase
# put the coarse low 1.17 to 1.19 s after the still, past the old 1.0 s reach, and the set lost its last rep
_SINK = dict(dip=(0.0075, 0.45), presink=(0.25, 1.2), lower=1.5, v_hit=-1.0)
for _h, _nm in ((0.3, ""), (0.02, "_hold0")):
    scen(f"reset_sink12{_nm}", ["reach"], f"reset style, 3 pulls of 1.8/2.0/2.3 s, each after a 1.2 s pre-sink into a 7.5 mm "
         f"dip, lockout hold {_h} s", floor="fidget", roll0=112.0,
         reps=[rep(1.8, 0.43, "dl", ("off", 14.0, 0.85), hold=_h, droll=8.0, **_SINK),
               rep(2.0, 0.43, "dl", ("off", 3.3, 0.8), hold=_h, droll=8.0, **_SINK),
               rep(2.3, 0.43, "dl", ("off", 3.3, 0.8), hold=_h, droll=-12.0, **_SINK)])

# 11-02 set 1 (hands on between reps: one window): a bump at 1 s (so the set's segment starts there and its long quiet
# rest at the pre-set roll sets g, as on 11-02), quiet to 12 s, fidgety to 20 s, a 1.7 s grip; from the video truth: the
# pod's concentric 1.77 / 2.20 / 2.33 s, 434.5 / 442.7 / 451.9 mm; dips 8.3 / 9.1 / 10.0 mm at pitch 2.2 / 2.25 / 2.36 deg
# (pre-sinks 1.5 / 3.0 / 2.5 mm over 1.2 / 1.1 / 0.65 s, fast phases 0.53 / 0.33 / 0.48 s); top to set-down 1.32 / 1.03 /
# 2.08 s, hits at 1.00 / 1.25 / 1.19 m/s; set-down to the next dip bottom 2.55 / 4.45 s, hands on, with re-wedges; rolls
# -160 -> 134 (raw |a| there 9.951 against 10.038 at -160: just over the 0.08 still limit, as 11-02's 9.952 against 10.034:
# no still there raw, one calibrated) -> 134 -> 113; the set-downs' jolt runs as measured (last run onset 0.25, 0.18, 0 s)
SET1 = [rep(1.77, 0.4345, "dl", ("off", 20.0, 1.7), (0.0083, 0.53), theta=2.2, hold=0.2, lower=1.12, v_hit=-1.0, droll=-66.0,
            setdown="clang_mid", presink=(0.18, 1.2)),
        rep(2.20, 0.4427, "dl", ("on", 2.55), (0.0091, 0.33), theta=2.25, hold=0.15, lower=0.88, v_hit=-1.25, droll=0.0,
            rewedges=(0.4,), setdown="clang_mid2", presink=(0.33, 1.1)),
        rep(2.33, 0.4519, "dl", ("on", 4.45), (0.0100, 0.48), theta=2.36, hold=0.3, lower=1.78, v_hit=-1.19, droll=-21.0,
            rewedges=(1.0, 2.6), setdown="clang_one", presink=(0.25, 0.65))]
B1 = [(1.0, 0.6, 0.6), (12.4, 0.4, 1.5), (16.5, 0.5, 0.8)]
scen("held3", ["RC3", "RC5"], "11-02 set 1: hands on the bar between reps (contact vibration, no still: one window), 2.55 and "
     "4.45 s floor rests with re-wedges, the pod's concentric 1.77/2.20/2.33 s, dips 8.3-10 mm held through the pull, "
     "set-downs at 1.0-1.25 m/s with 11-02's jolt runs and the set-down artefact, the sleeve's roll 66 deg off its pre-set "
     "rest from rep 1 on", reps=SET1, floor="fidget", lead=12.0, roll0=-160.0, blips=B1)
scen("held3_quiet", ["RC4"], "held3 with a 0.75 s quiet patch (hands still, contact x0.04) 2.5 s into the 4.45 s rest before rep 3: "
     "a still there (11-02 set 1 calibrated: 59.83-60.57, 2.6 s after the set-down) splits the window", floor="fidget", lead=12.0,
     roll0=-160.0, blips=B1, reps=SET1[:2] + [dict(SET1[2], quiet=(2.5, 0.75), rewedges=(1.0,))])
for _d, _T, _nm in ((0.0, 0.45, "dip0"), (0.015, 0.6, "dip15")):
    scen(f"held3_{_nm}", ["dip"], f"held3 with {'no bend dip (the control)' if not _d else 'a 15 mm dip, its fast phase over 0.6 s'}",
         reps=[dict(r, dip=(_d, _T), dip_steps=([(PRESINK[0], PRESINK[1]), (1 - PRESINK[0], _T)] if _d else None)) for r in SET1],
         floor="fidget", lead=12.0, roll0=-160.0, blips=B1)
scen("held3_rec", ["dip"], "held3 with the old bend model: the dip recovers over the first 0.3 s of the pull (the pod rises d "
     "more than the plate; the default holds the bend to the set-down)", reps=[dict(r, rec=0.3) for r in SET1],
     floor="fidget", lead=12.0, roll0=-160.0, blips=B1)
scen("held3_one", ["RC1"], "held3 whose set-downs rattle in one jolt run (span_velocity's landing is the true hit): the control "
     "for the bouncing set-downs", reps=[dict(r, setdown=r["setdown"] if r["setdown"] == "clang_one" else r["setdown"] + "_1") for r in SET1],
     floor="fidget", lead=12.0, roll0=-160.0, blips=B1)
scen("held3_clean", ["RC1"], "held3 without the set-down artefact (the old sensor model)",
     reps=[dict(r, setdown=r["setdown"] + "_clean") for r in SET1], floor="fidget", lead=12.0, roll0=-160.0, blips=B1)
scen("held3_hitch", ["hitch"], "held3 with 11-02 set 1 rep 2's lockout hitch (v ~0.004 m/s at 85-88% of the pull, 1.3 cm under "
     "the top, then the top)", reps=[SET1[0], dict(SET1[1], shape="hitch"), SET1[2]], floor="fidget", lead=12.0, roll0=-160.0, blips=B1)

# fast and touch-and-go controls
scen("reset_fast5", [], "reset style, 5 fast pulls (MCV 0.80 -> 0.50, ROM 0.50), 3 s off the bar + 0.7 s grip between, "
     "set-downs at 1.1 m/s", floor="quiet", roll0=30.0,
     reps=[rep(0.5 / m, 0.50, "fast", ("off", 6.0 if k == 0 else 3.0, 0.7), (0.007, 0.35), hold=0.4, lower=1.0,
               v_hit=-1.1, droll=(-8.0, 6.0, -12.0, 9.0, -4.0)[k]) for k, m in enumerate((0.80, 0.72, 0.65, 0.57, 0.50))])
scen("reset_fast5_one", [], "reset_fast5 whose set-downs rattle in one jolt run (10-31's kind): the control for the late "
     "landing", floor="quiet", roll0=30.0,
     reps=[rep(0.5 / m, 0.50, "fast", ("off", 6.0 if k == 0 else 3.0, 0.7), (0.007, 0.35), hold=0.4, lower=1.0,
               v_hit=-1.1, setdown="clang_1", droll=(-8.0, 6.0, -12.0, 9.0, -4.0)[k])
           for k, m in enumerate((0.80, 0.72, 0.65, 0.57, 0.50))])
scen("tng5", [], "touch-and-go: 5 pulls (MCV 0.60 -> 0.40, ROM 0.50), each lowered in 0.8 s onto a 0.12 s touch at 0.4 m/s "
     "(a jolt, gyro x0.25), hands on; a dip before rep 1 only", floor="quiet", roll0=-30.0,
     reps=[rep(0.5 / m, 0.50, "fast", ("off", 6.0, 1.0) if k == 0 else ("tng", 0.12), (0.008, 0.4) if k == 0 else (0.0, 0.3),
               hold=0.3, lower=0.8, setdown="touch" if k < 4 else "clang", v_hit=-0.40 if k < 4 else -1.0,
               a_hit=2.0 if k < 4 else -1.5, droll=(-3.0, 2.0, -2.0, 3.0, -6.0)[k])
           for k, m in enumerate((0.60, 0.55, 0.50, 0.45, 0.40))])
scen("tng5_soft", [], "tng5 with softer touches (0.25 m/s stopped in 50 ms: 10 m/s^2 peak, under IMPACT_A: not a jolt)",
     floor="quiet", roll0=-30.0,
     reps=[dict(r, v_hit=-0.25, a_hit=1.5, setdown="touch_soft") if r["setdown"] == "touch" else r for r in S["tng5"]["spec"]["reps"]])
scen("single_heavy", ["RC2"], "one slow single (MCV 0.15: 3.0 s, ROM 0.45, no stall) with a 10 mm dip, its still ~0.4 s after "
     "the set-down", floor="quiet", roll0=112.0,
     reps=[rep(3.0, 0.45, "slow", ("off", 6.0, 1.0), (0.010, 0.45), hold=0.8, lower=1.8, setdown="short", droll=-20.0)])
# the slow-pull sweep: reset style, 2 reps each, set-downs that ring on (late stills: the landing stop fires, RC1 out)
for _m in (0.12, 0.15, 0.20, 0.25, 0.35):
    scen(f"sweep_v{round(_m * 100):03d}", ["RC2"], f"slow-pull sweep: 2 reset-style pulls at MCV {_m} (ROM 0.45, {0.45 / _m:.1f} s), "
         "late stills after set-downs that rattle in one jolt run (~1.2 s: neither RC1 nor the late landing)", floor="quiet",
         roll0=112.0, reps=[rep(0.45 / _m, 0.45, "dl", ("off", 6.0 if k == 0 else 3.5, 0.8), (0.009, 0.4), hold=0.5, lower=1.5,
                                setdown="long_one", droll=(-12.0, 10.0)[k]) for k in range(2)])
SCENARIOS = S

# The guard set parity.mjs checks (D44), raw and calibrated: each D44 rule reverted alone fails one of these or a real
# 11-02 set (decisions.md D44 has the table)
GUARDS = [("held3_quiet", 1),            # the floor's flat test (2 of 3), held-back neighbours (live: a dip)
          ("single_heavy", 1),           # the start still and the stop into the window's end (0 reps without either)
          ("held3_hitch", 1),            # past a hitch and its drift floor (+18%), the floor height test (+15%)
          ("tng5", 1),                   # the touch (reps 2 to 5 -27 to -51%)
          ("sweep_v025", 1),             # the open tail's bias (live: a dip)
          ("held3_one", 1),              # the floor height test (rep 2 -40%)
          ("reset_fast5", 1),            # first contact (-16 to -35%)
          ("reset_sink12_hold0", 4)]     # TA_FLOOR_REACH 1.5 (at 1.0 the last rep is lost)


def paths(name, seed, sensor, out=None):
    d = Path(out) if out else DATA
    stem = f"fake_deadlift_{name}_s{seed}_{sensor}"
    return d / f"{stem}.csv", d / f"{stem}_truth.json", d / f"{stem}_path.npz"


def write(name, seed=1, sensors=("raw",), out=None, path=False):
    """The scenario's CSV (the pod's columns) and _truth.json per sensor, and with path its _path.npz."""
    sc = SCENARIOS[name]
    t, raw, gyro, truth = deadlift(sc["spec"], seed)
    res = []
    rnd = lambda o: round(o, 6) if isinstance(o, float) else [rnd(v) for v in o] if isinstance(o, (list, tuple)) else \
        {k: rnd(v) for k, v in o.items()} if isinstance(o, dict) else o
    for sensor in sensors:
        if sensor == "raw":
            acc, gy = raw, gyro
        else:                                          # calib_pod.apply(CAL, ...): what reps.py --calib does
            acc = (raw - ACC_BIAS) / ACC_SCALE
            gy = (gyro - GYRO_BIAS) / GYRO_SCALE
        csv, tj, npz = paths(name, seed, sensor, out)
        csv.parent.mkdir(parents=True, exist_ok=True)
        a = np.linalg.norm(acc, axis=1)
        np.savetxt(csv, np.column_stack([t, acc, a, gy]), delimiter=",", header=M.HEADER, comments="", fmt="%.6f")
        if path:
            np.savez_compressed(npz, t=t.astype(np.float64), z=truth["path"]["z"].astype(np.float32),
                                v=truth["path"]["v"].astype(np.float32), plate=truth["path"]["plate"].astype(np.float32))
        hs = card_stills(t, a)
        st = [r[0] for r in hs["runs"]]
        for h in truth["info"]["hits"]:
            nxt = [s for s in st if s > h["t"]]
            h[f"still_after_{sensor}"] = round(nxt[0] - h["t"], 3) if nxt else None
        d = dict(scenario=name, pins=sc["pins"], note=sc["note"], seed=seed, sensor=sensor, lift="deadlift", fs=FS,
                 samples=len(t),
                 truth_def="exact path; a rep: liftoff (the pod's lowest point: the plates leave the floor as the bend dip "
                           "bottoms out, v = 0) to lockout (v = 0), on the POD's path (plate + dip); rom = its rise, mcv = rom "
                           "/ dur; rom_plate / mcv_plate: the plates'. Vibration, sway, rings, fidget, re-wedges not in it",
                 reps=truth["reps"], info=truth["info"], stills=hs, total=truth["total"], z_knots=truth["z_knots"],
                 dip_moves=truth["dip_moves"],
                 sensor_model=dict(acc_bias=ACC_BIAS.tolist(), acc_scale=ACC_SCALE.tolist(), acc_noise=ACC_NOISE.tolist(),
                                   acc_lsb=M.POD_ACC_LSB, acc_max=M.POD_ACC_MAX, gyro_bias=GYRO_BIAS.tolist(),
                                   gyro_scale=GYRO_SCALE.tolist(), gyro_noise=GYRO_NOISE.tolist(), gyro_lsb=M.POD_GYRO_LSB,
                                   calibrated=sensor == "cal", calibration=CAL))
        tj.write_text(json.dumps(rnd(d), indent=1))
        res.append((csv, tj, npz))
    return res


def describe(tj):
    tr = json.loads(Path(tj).read_text())
    r = tr["reps"]
    hs = tr["stills"]
    still = [h.get(f"still_after_{tr['sensor']}") for h in tr["info"]["hits"]]
    return (f"{Path(tj).name.replace('_truth.json', '')}: {len(r)} reps, ROM {' '.join(f'{x['rom'] * 100:.1f}' for x in r)} cm "
            f"(plate {' '.join(f'{x['rom_plate'] * 100:.1f}' for x in r)}), MCV {' '.join(f'{x['mcv']:.3f}' for x in r)}; "
            f"hits |a-g| {' '.join(f'{h['peak_abs_a_minus_g']:.0f}' for h in tr['info']['hits'])} jolt "
            f"{' '.join(f'{h['jolt_s']:.2f}' for h in tr['info']['hits'])} s, still after "
            f"{' '.join('-' if s is None else f'{s:.2f}' for s in still)} s; noise {hs['noise']:.4f} "
            f"(limits {hs['std_max']:.3f}/{hs['dev_max']:.3f}), {len(hs['runs'])} stills in segment {hs['segment']} (of {hs['segments']}); "
            f"clipped {tr['info']['clipped_samples']}")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if "--list" in sys.argv:
        for nm, sc in SCENARIOS.items():
            print(f"{nm:20s} [{','.join(sc['pins']) or '-':8s}] {sc['note']}")
        sys.exit(0)
    seeds = next((list(map(int, a.split("=", 1)[1].split(","))) for a in sys.argv if a.startswith("--seeds=")), list(SEEDS))
    sensor = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--sensor=")), "both")
    out = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--out=")), None)
    sensors = ("raw", "cal") if sensor == "both" else (sensor,)
    if args or "--all" in sys.argv:
        jobs = [(nm, sd) for nm in (args or list(SCENARIOS)) for sd in seeds]
    else:
        jobs = list(GUARDS)                            # the default: parity.mjs's guard set, each at its own seed
        if any(a.startswith("--seeds=") for a in sys.argv):
            print("note: --seeds needs names or --all; the guard set is written at its own seeds", file=sys.stderr)
    for nm, sd in jobs:
        for csv, tj, npz in write(nm, sd, sensors, out, "--path" in sys.argv):
            if "--quiet" not in sys.argv:
                print(describe(tj), flush=True)
