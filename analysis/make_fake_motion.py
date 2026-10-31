"""
Fake IMU recordings with known truth for orientation and bar path (roadmap 0.3, algorithm.md
sections 2, 11, 12; checked by orientation.py --selftest).

    python make_fake_motion.py            # every scenario -> data/fake_motion_<name>.csv (+ _truth.csv)
    python make_fake_motion.py roll       # just one

Exact physics, then sensor noise: the device moves along min-jerk translations and turns about
one body axis at a time (which makes the gyro's body rate exact: theta_dot times that axis). The
accelerometer reads the specific force in the sensor frame, R^T (a + g up); the gyro reads the body
rate plus a constant bias and white noise. The mount is skewed on purpose (rolled 25 degrees about
the bar), so no sensor axis points up. World frame: x forward, y along the bar, z up. Sensor x
lies along the bar (a mount that forces it, algorithm.md section 11); every scenario starts with
the 2 s roll gesture anyway, so the bar axis can also be found from the gyro.

Scenarios (the roadmap 0.3 tests, plus a bench-style J-curve):
    flat      three 40 cm lifts, the device's attitude fixed
    tilt      the same lifts, the bar tilting 25 degrees (one end up) and back during each
    roll      the same lifts, the device rolling a full turn around the bar during each (sleeve spin)
    slide     slid 50 cm forward along a table and back, flat
    diagonal  lifted 30 cm up and 20 cm forward onto a table, and back down
    jcurve    three bench reps: 40 cm up with a 10 cm forward sweep, and back
    tiltroll  the bar tilted 20 degrees, two lifts spinning a full turn about the tilted bar, leveled
    early     the roll gesture before the first still (ending 10 degrees off where it began), then lifts

Gym scenarios (GYM, not in SCENARIOS: orientation.py --selftest runs every SCENARIOS entry against
the mount, noise and roll gesture above, and a loaded bar has none of those):
    squat_tng a whole touch-and-go squat set as the pod on Adapter A sees it (the 10-31 gym day):
              racked, unrack, walkout, 6 reps (one with a 0.85 s lockout pause), walk-in, a clanging
              rerack, racked. Writes data/fake_motion_squat_tng.csv, its per-sample _truth.csv and
              _truth.json with every rep's concentric start/end, ROM and MCV from the exact path
"""

import sys
from pathlib import Path

import numpy as np

FS = 208.0                       # Hz, the pod's IMU rate (INFO odr)
G = 9.80665                      # m/s^2
ACC_NOISE = 0.012                # m/s^2, white (the pod's rest wiggle is ~0.01)
GYRO_NOISE = 0.07                # deg/s, white
GYRO_BIAS = np.array([0.4, -0.3, 0.2])   # deg/s, constant: orientation.py takes it from the stills
MOUNT_ROLL = 25.0                # deg: the device sits rolled about the bar, no axis points up
HEADER = ('"Time (s)","Acceleration x (m/s^2)","Acceleration y (m/s^2)","Acceleration z (m/s^2)",'
          '"Absolute acceleration (m/s^2)","Gyro x (deg/s)","Gyro y (deg/s)","Gyro z (deg/s)"')
OUT = Path(__file__).parent / "data"


def min_jerk(tau):
    """s, ds/dtau, d2s/dtau2 of the min-jerk profile 10t^3 - 15t^4 + 6t^5 on [0, 1]."""
    return (10 * tau**3 - 15 * tau**4 + 6 * tau**5, 30 * tau**2 - 60 * tau**3 + 30 * tau**4,
            60 * tau - 180 * tau**2 + 120 * tau**3)


def rot(axis, angle):
    """Rotation matrix about a unit axis (Rodrigues)."""
    a = np.asarray(axis, float) / np.linalg.norm(axis)
    k = np.array([[0, -a[2], a[1]], [a[2], 0, -a[0]], [-a[1], a[0], 0]])
    return np.eye(3) + np.sin(angle) * k + (1 - np.cos(angle)) * k @ k


# the mount: sensor x along the bar (world y), sensor y backward, z up, then rolled about the bar
R_MOUNT = rot([0, 1, 0], np.radians(MOUNT_ROLL)) @ np.array([[0, -1, 0], [1, 0, 0], [0, 0, 1]], float)


def build(segments, fs=FS, seed=1):
    """segments: ("still", T) | ("move", T, dp_world, body_axis or None, dtheta_deg).
    A move translates by dp (min-jerk) and turns about a fixed body axis by dtheta (min-jerk), at
    once. Returns t, accel (sensor, with g), gyro (deg/s, sensor), and the truth: position (world),
    the rotation sensor -> world per sample."""
    rng = np.random.default_rng(seed)
    total = sum(s[1] for s in segments)
    n = int(round(total * fs)) + 1
    t = np.arange(n) / fs
    pos, acc_w = np.zeros((n, 3)), np.zeros((n, 3))
    R = np.zeros((n, 3, 3))
    w_b = np.zeros((n, 3))
    p0, R0, t0 = np.zeros(3), R_MOUNT.copy(), 0.0
    for seg in segments:
        T = seg[1]
        m = (t >= t0 - 1e-9) & (t < t0 + T - 1e-9) if seg is not segments[-1] else (t >= t0 - 1e-9)
        tau = np.clip((t[m] - t0) / T, 0, 1)
        if seg[0] == "still":
            pos[m], R[m] = p0, R0
        else:
            _, _, dp, axis, dth = seg
            s, ds, dds = min_jerk(tau)
            dp = np.asarray(dp, float)
            pos[m] = p0 + np.outer(s, dp)
            acc_w[m] = np.outer(dds / T**2, dp)
            if axis is not None and dth:
                th = np.radians(dth) * s
                R[m] = np.array([R0 @ rot(axis, a) for a in th])
                w_b[m] = np.outer(np.radians(dth) * ds / T, np.asarray(axis, float) / np.linalg.norm(axis))
            else:
                R[m] = R0
            p0 = p0 + dp
            if axis is not None and dth:
                R0 = R0 @ rot(axis, np.radians(dth))
        t0 += T
    f_w = acc_w + np.array([0, 0, G])                       # specific force, world
    acc = np.einsum("nji,nj->ni", R, f_w)                  # R^T f: into the sensor frame
    acc += rng.normal(0, ACC_NOISE, acc.shape)
    gyro = np.degrees(w_b) + GYRO_BIAS + rng.normal(0, GYRO_NOISE, w_b.shape)
    return t, acc, gyro, {"pos": pos, "R": R}


BAR = [1, 0, 0]                  # the bar axis, sensor frame (the mount puts sensor x along it)
FWD_BODY = R_MOUNT.T @ np.array([1.0, 0, 0])   # world forward, seen from the mounted sensor


def roll_gesture():
    """Rolling the bar back and forth in the hooks, ~2 s (algorithm.md section 11)."""
    return [("move", 0.5, [0, 0, 0], BAR, 40), ("move", 0.7, [0, 0, 0], BAR, -80), ("move", 0.5, [0, 0, 0], BAR, 40)]


def lifts(n, up, fwd=0.0, axis=None, dth=0.0, t_up=1.2, t_down=1.4, pause=1.5):
    seg = []
    for _ in range(n):
        seg += [("move", t_up, [fwd, 0, up], axis, dth), ("still", pause),
                ("move", t_down, [-fwd, 0, -up], axis, -dth if axis is not None and dth not in (360, -360) else dth),
                ("still", pause)]
    return seg


SCENARIOS = {
    "flat": lambda: [("still", 2.0)] + roll_gesture() + [("still", 1.5)] + lifts(3, 0.40),
    "tilt": lambda: [("still", 2.0)] + roll_gesture() + [("still", 1.5)] + lifts(3, 0.40, axis=FWD_BODY, dth=25),
    "roll": lambda: [("still", 2.0)] + roll_gesture() + [("still", 1.5)] + lifts(3, 0.40, axis=BAR, dth=360),
    "slide": lambda: [("still", 2.0)] + roll_gesture() + [("still", 1.5), ("move", 1.6, [0.5, 0, 0], None, 0),
                                                           ("still", 1.5), ("move", 1.6, [-0.5, 0, 0], None, 0), ("still", 1.5)],
    "diagonal": lambda: [("still", 2.0)] + roll_gesture() + [("still", 1.5), ("move", 1.5, [0.2, 0, 0.3], None, 0),
                                                              ("still", 1.5), ("move", 1.5, [-0.2, 0, -0.3], None, 0), ("still", 1.5)],
    "jcurve": lambda: [("still", 2.0)] + roll_gesture() + [("still", 1.5)] + lifts(3, 0.40, fwd=0.10, t_up=1.0, t_down=1.3, pause=1.0),
    # two body axes composed: the bar tilted 20 degrees, then spun a full turn about itself (the tilted
    # bar) during each lift, then leveled. A body/world mix-up in the gyro update shows here, not above
    "tiltroll": lambda: [("still", 2.0)] + roll_gesture() + [("still", 1.5), ("move", 0.6, [0, 0, 0], FWD_BODY, 20), ("still", 1.2)]
    + lifts(2, 0.40, axis=BAR, dth=360) + [("move", 0.6, [0, 0, 0], FWD_BODY, -20), ("still", 1.5)],
    # moving from the first sample: the bar rolled right after Start (a net 10 degrees), then still
    "early": lambda: [("move", 0.5, [0, 0, 0], BAR, 40), ("move", 0.7, [0, 0, 0], BAR, -80), ("move", 0.5, [0, 0, 0], BAR, 50),
                      ("still", 2.0)] + lifts(3, 0.40, pause=1.0),
}


def scenario(name, seed=1):
    return build(SCENARIOS[name](), seed=seed)


def write(name, seed=1):
    t, acc, gyro, truth = scenario(name, seed)
    OUT.mkdir(exist_ok=True)
    a = np.linalg.norm(acc, axis=1)
    rows = np.column_stack([t, acc, a, gyro])
    np.savetxt(OUT / f"fake_motion_{name}.csv", rows, delimiter=",", header=HEADER, comments="", fmt="%.6f")
    up = truth["R"][:, 2, :]                                # world up in the sensor frame, per sample
    bar_w = truth["R"] @ np.array(BAR, float)
    tilt = np.degrees(np.arcsin(np.clip(bar_w[:, 2], -1, 1)))
    np.savetxt(OUT / f"fake_motion_{name}_truth.csv", np.column_stack([t, truth["pos"], up, tilt]), delimiter=",",
               header="t,x_fwd,y_bar,z_up,up_sx,up_sy,up_sz,tilt_deg", comments="", fmt="%.6f")
    return OUT / f"fake_motion_{name}.csv"


# ---- Gym scenarios: a loaded bar, the pod on its sleeve end (Adapter A) ----
# Exact physics again, but a touch-and-go rep never stops: its turnarounds have v = 0 for an
# instant and a big acceleration (the bounce). So paths are quintics through knots (t, p, v, a),
# continuous up to the acceleration, and every motion is a sum of "tracks": (value, rate, rate of
# the rate) arrays with exact derivatives. The pod's mount: sensor x up (at roll 0), y forward, z
# along the bar, so gravity lies in the x-y plane and the sleeve's roll turns it about z.

R_POD = np.array([[0, 1, 0], [0, 0, 1], [1, 0, 0]], float)   # columns: sensor x, y, z in the world (up, fwd, bar)
BAR_POD = [0, 0, 1]                                # the bar axis, pod sensor frame
POD_ACC_NOISE = np.array([0.02, 0.02, 0.007])      # m/s^2 per axis: a racked bar's rest wiggle (10-31 gym)
POD_GYRO_NOISE = 0.05                              # deg/s (same)
POD_GYRO_BIAS = np.array([0.74, -3.36, 2.18])      # deg/s: the pod's own, from the 10-31 racked stills
POD_ACC_OFFSET = np.array([-0.18, -0.055, 0.0])    # m/s^2: uncalibrated (calib_pod.py, D37, not applied yet)
POD_ACC_SCALE = 9.849 / G                          # so |a| at rest is the day-1 fit: 9.849 - 0.179 cos(roll) - 0.055 sin(roll)
POD_ACC_LSB, POD_ACC_MAX = 0.002393, 8 * G         # m/s^2: the LSM6DS3 at +-8 g (the recordings' step)
POD_GYRO_LSB, POD_GYRO_MAX = 0.035, 1000.0         # deg/s: ... at +-1000 deg/s
SWAY_HZ = 2.7                                      # standing under the bar isn't still (10-31: ~2.7 Hz)
RING_TAU, RING_ONSET = 0.25, 0.008                 # s: a clang's ringing decays (e-fold), and how fast it starts:
                                                   # under 2 samples, but not under one (a 3 ms onset left 49 mm/s
                                                   # in the integrated accel after the knock, 8 ms leaves ~1)


def track(n):
    return np.zeros(n), np.zeros(n), np.zeros(n)


def add(*tracks):
    return tuple(sum(k[i] for k in tracks) for i in range(3))


def mul(f, g):
    """The product of two tracks, with its derivatives (the product rule, twice)."""
    return f[0] * g[0], f[1] * g[0] + f[0] * g[1], f[2] * g[0] + 2 * f[1] * g[1] + f[0] * g[2]


def quintic(t, t0, t1, start, end):
    """The quintic through (p, v, a) at t0 and at t1, evaluated on t: a track."""
    T = t1 - t0
    (p0, v0, a0), (p1, v1, a1) = start, end
    # p(tau) = sum c_k tau^k, tau = (t - t0) / T: rows are p, p', p'' at tau = 0, then at tau = 1
    M = np.array([[1, 0, 0, 0, 0, 0], [0, 1, 0, 0, 0, 0], [0, 0, 2, 0, 0, 0],
                  [1, 1, 1, 1, 1, 1], [0, 1, 2, 3, 4, 5], [0, 0, 2, 6, 12, 20]], float)
    c = np.linalg.solve(M, [p0, v0 * T, a0 * T**2, p1, v1 * T, a1 * T**2])
    tau = (t - t0) / T
    k = np.arange(6)
    return (np.polyval(c[::-1], tau), np.polyval((c * k)[1:][::-1], tau) / T,
            np.polyval((c * k * (k - 1))[2:][::-1], tau) / T**2)


def through(t, knots):
    """A path through knots [(t, p, v, a)]: a quintic between each pair, held at the first knot's p
    before it and the last's after it (so the end knots need v = a = 0, or something else after)."""
    p, v, a = track(len(t))
    p[:] = knots[0][1]
    for k0, k1 in zip(knots[:-1], knots[1:]):
        m = (t >= k0[0]) & (t < k1[0])
        p[m], v[m], a[m] = quintic(t[m], k0[0], k1[0], k0[1:], k1[1:])
    p[t >= knots[-1][0]] = knots[-1][1]
    return p, v, a


def steps(t, moves):
    """Sum of min-jerk steps [(t0, T, delta)]: a track."""
    out = track(len(t))
    for t0, T, d in moves:
        s, ds, dds = min_jerk(np.clip((t - t0) / T, 0, 1))
        inside = (t > t0) & (t < t0 + T)
        out = add(out, (d * s, d * ds / T * inside, d * dds / T**2 * inside))
    return out


def sine(t, t0, amp, hz, phase=0.0):
    w = 2 * np.pi * hz
    x = w * (t - t0) + phase
    return amp * np.sin(x), amp * w * np.cos(x), -amp * w * w * np.sin(x)


def sway(t, t0, t1, amp, hz=SWAY_HZ, ramp=0.25, phase=np.pi):
    """amp * sin at hz from t0 to t1, faded in and out over `ramp` s (min-jerk), so it starts and
    stops with no jump. phase pi: it starts downward (a soft stop's top stays the top)."""
    s0, d0, dd0 = min_jerk(np.clip((t - t0) / ramp, 0, 1))
    s1, d1, dd1 = min_jerk(np.clip((t - (t1 - ramp)) / ramp, 0, 1))
    on = (t > t0) & (t < t1)
    env = ((s0 - s1) * on, (d0 - d1) / ramp * on, (dd0 - dd1) / ramp**2 * on)
    return mul(env, sine(t, t0, amp, hz, phase))


def ring(t, t0, amp, hz):
    """A clang's ringing from t0: amp e^(-s/RING_TAU) (1 - e^(-s/RING_ONSET))^3 sin(2 pi hz s), s = t - t0.
    The cubed onset starts it with no jump in the acceleration or its rate (a jerk that jumps at t0
    leaves velocity in the sampled accel: 14 mm/s from the knock with a squared onset); no net motion."""
    s = np.maximum(t - t0, 0)
    e, o = np.exp(-s / RING_TAU), np.exp(-s / RING_ONSET)
    decay = (e, -e / RING_TAU, e / RING_TAU**2)
    onset = (1 - o, o / RING_ONSET, -o / RING_ONSET**2)
    env = mul(decay, mul(onset, mul(onset, onset)))
    return tuple(k * (t >= t0) for k in mul(env, sine(t, t0, amp, hz)))


def landing(t, t_hit, v_hit, T):
    """Height above the hooks from t_hit on: the bar comes down at v_hit (< 0) and a sin^2 pulse of
    upward acceleration (peak -2 v_hit / T) stops it in T s, -v_hit T / 2 lower. 0 after that."""
    p, v, a = track(len(t))
    s = t - t_hit
    m = (s >= 0) & (s < T)
    A = -2 * v_hit / T
    sm = s[m]
    p[m] = -v_hit * T / 2 + v_hit * sm + A * (sm**2 / 4 + T**2 / (8 * np.pi**2) * (np.cos(2 * np.pi * sm / T) - 1))
    v[m] = v_hit + A * (sm / 2 - T / (4 * np.pi) * np.sin(2 * np.pi * sm / T))
    a[m] = A * np.sin(np.pi * sm / T) ** 2
    return p, v, a


def of_depth(z, z_top, c1, c2):
    """c1 u + c2 u^2 with u = z_top - z (how far below standing): a track that follows the rep."""
    u = (z_top - z[0], -z[1], -z[2])
    return (c1 * u[0] + c2 * u[0]**2, (c1 + 2 * c2 * u[0]) * u[1], 2 * c2 * u[1]**2 + (c1 + 2 * c2 * u[0]) * u[2])


def rots(axis, angles):
    """Rotation matrices (n, 3, 3) about one fixed axis (rot(), for an array of angles)."""
    a = np.asarray(axis, float)
    k = np.array([[0, -a[2], a[1]], [a[2], 0, -a[0]], [-a[1], a[0], 0]])
    return np.eye(3) + np.sin(angles)[:, None, None] * k + (1 - np.cos(angles))[:, None, None] * (k @ k)


STAND = 0.11                     # m: standing height above the hooks (the unrack lifts the bar this much)
BOUNCE = 0.1                     # s: the bounce out of the bottom runs this long either side of it, at a
                                 # steady jerk, from a quarter of its peak acceleration to the peak and back
# one row per rep: eccentric s, concentric s, depth below standing m, top short of standing m, the
# bounce's peak acceleration m/s^2 (at the bottom), top acceleration m/s^2 (0 = a soft stop), lockout
# pause after it s. 10-31: touch-and-go tops 5 to 8 cm short of standing, 1.5 s (135 lb) to 3 s (360 lb)
# a rep, ROM 55 to 65 cm; |a| spiking to 17-22 at a 135 bottom and dipping to ~3 at its top, the 360
# swinging only 7.5 to 12; the 360 had one 0.85 s lockout pause
SQUAT_TNG_REPS = [
    (0.85, 0.70, 0.67, 0.06, 10.0, -6.5, 0.0),
    (0.75, 0.75, 0.68, 0.05, 11.0, -6.0, 0.0),
    (0.75, 0.85, 0.64, 0.01, 10.0, 0.0, 0.85),    # all the way up, then the pause
    (0.80, 0.95, 0.63, 0.07, 8.0, -4.0, 0.0),
    (0.75, 1.10, 0.65, 0.08, 6.0, -3.0, 0.0),
    (0.80, 1.60, 0.64, 0.00, 4.0, 0.0, 0.0),      # the grinder, all the way up
]


def squat_tng(seed=1, fs=FS):
    """The set, start to end: t, accel, gyro (as the pod writes them) and the truth: position,
    velocity, the rotation sensor -> world per sample, the reps and when everything happens."""
    rng = np.random.default_rng(seed)
    ev = dict(racked=(0.0, 2.0), unrack=(2.0, 2.9), walkout=(2.9, 4.5), stand_before=(4.5, 5.6))
    # the vertical: knots through the unrack, the walkout's 3 steps (each bobs 1.2 cm), the reps
    z_knots = [(0.0, 0.0, 0, 0), (2.0, 0.0, 0, 0), (2.9, STAND, 0, 0)]
    for k in range(3):
        t0 = 2.9 + k * 1.6 / 3
        z_knots += [(t0 + 0.8 / 3, STAND - 0.012, 0, 0), (t0 + 1.6 / 3, STAND, 0, 0)]
    t_now = 5.6
    z_knots.append((t_now, STAND, 0, 0))
    reps, pauses = [], []
    for k, (te, tc, depth, short, a_bot, a_top, pause) in enumerate(SQUAT_TNG_REPS):
        t_bot, t_top = t_now + te, t_now + te + tc
        z_bot, z_top = STAND - depth, STAND - short
        # the bounce: a steady jerk from a_bot / 4 at t_bot - BOUNCE to a_bot at the bottom and back,
        # which fixes the speed and height at its edges (a quintic through them stays that cubic)
        a_edge = a_bot / 4
        v_edge, z_edge = BOUNCE * (a_edge + a_bot) / 2, z_bot + BOUNCE**2 * (2 * a_bot + a_edge) / 6
        z_knots += [(t_bot - BOUNCE, z_edge, -v_edge, a_edge), (t_bot, z_bot, 0, a_bot),
                    (t_bot + BOUNCE, z_edge, v_edge, a_edge), (t_top, z_top, 0, a_top)]
        reps.append(dict(rep=k + 1, t_start=t_bot, t_end=t_top, ecc_start=t_now, z_bottom=z_bot, z_top=z_top,
                         short_of_standing=short, pause_after=pause))
        if pause:
            assert a_top == 0, "a pause needs a soft stop at the top"
            z_knots.append((t_top + pause, z_top, 0, 0))
            pauses.append((t_top, t_top + pause))
        t_now = t_top + pause
    assert SQUAT_TNG_REPS[-1][5] == 0, "the set ends standing (a soft stop)"
    t_last = t_now
    ev.update(reps=(5.6, t_last), stand_after=(t_last, t_last + 1.1), walkin=(t_last + 1.1, t_last + 2.7),
              lean=(t_last + 2.7, t_last + 3.2), rerack=(t_last + 3.2, t_last + 3.8))
    ev["knock"] = ev["walkin"][1] - 0.1            # the bar touches the uprights (10-31: 1.65 s before the hooks)
    # onto the hooks, then it rattles in them: 10-31's 135 rerack hit 4 times in 0.8 s (|a - g| peaks
    # 30, 30, 34, 49; the |a| it rectified added 6.8 m/s to the old integration, the knock 1.0)
    ev["clangs"] = tuple(ev["rerack"][1] + d for d in (0.0, 0.12, 0.42, 0.80))
    total = ev["clangs"][-1] + 3.5                 # the ringing (~1.5 s), then ~2 s racked
    n = int(round(total * fs)) + 1
    t = np.arange(n) / fs
    z_knots.append((ev["walkin"][0], STAND, 0, 0))
    w0, w1 = ev["walkin"]
    for k in range(3):
        t0 = w0 + k * (w1 - w0) / 3
        z_knots += [(t0 + (w1 - w0) / 6, STAND - 0.012, 0, 0), (t0 + (w1 - w0) / 3, STAND, 0, 0)]
    v_hit, t_stop = -0.30, 0.02                    # m/s onto the hooks, stopped in 20 ms
    z_knots += [(ev["rerack"][0], STAND, 0, 0), (ev["rerack"][1], -v_hit * t_stop / 2, v_hit, 0)]
    z = through(t, z_knots)
    after = t >= ev["rerack"][1]
    land = landing(t, ev["rerack"][1], v_hit, t_stop)
    z = tuple(np.where(after, land[i], z[i]) for i in range(3))

    # forward: 3 steps back (38 cm), the squat's bar path (5 cm forward at the bottom), 3 steps in
    o0, o1 = ev["walkout"]
    x = add(steps(t, [(o0 + k * (o1 - o0) / 3, (o1 - o0) / 3, -0.38 / 3) for k in range(3)]
                  + [(w0 + k * (w1 - w0) / 3, (w1 - w0) / 3, 0.38 / 3) for k in range(3)]),
            of_depth(z, STAND, 0.04, 0.05))
    # along the bar: the weight shifting side to side with each step
    y = steps(t, [(o0, 0.53, 0.012), (o0 + 0.53, 0.53, -0.02), (o0 + 1.07, 0.53, 0.011), (w0, 0.53, -0.013),
                  (w0 + 0.53, 0.53, 0.021), (w0 + 1.07, 0.53, -0.011)])
    # standing sway: up and down 2 mm (|a| std ~0.4 m/s^2), fore and aft 3 mm, lighter while leaning in
    stands = [ev["stand_before"], ev["stand_after"]] + pauses
    sway_z = add(*[sway(t, a, b, 0.002) for a, b in stands], sway(t, *ev["lean"], 0.0007))
    sway_x = add(*[sway(t, a, b, 0.003, phase=0.0) for a, b in stands], sway(t, *ev["lean"], 0.001, phase=0.0))
    # the clangs ring: up/down, fore/aft and along the bar (displacement = acceleration amplitude / w^2)
    bursts = [(ev["knock"], 0.7)] + list(zip(ev["clangs"], (1.0, 0.9, 1.0, 1.4)))   # (when, how hard)
    w2 = lambda hz: (2 * np.pi * hz) ** 2
    ring_z = add(*[ring(t, tb, s * 22 / w2(23), 23) for tb, s in bursts])
    ring_x = add(*[ring(t, tb, s * 18 / w2(26), 26) for tb, s in bursts])
    ring_y = add(*[ring(t, tb, s * 6 / w2(41), 41) for tb, s in bursts])
    pos = [add(x, sway_x, ring_x), add(y, ring_y), add(z, sway_z, ring_z)]

    # attitude, in degrees. Roll about the bar: up to ~35 deg through the set (10-31: 5 -> -8 -> 31 -> 14
    # -> 30 -> 39), plus 8 deg per m of depth. Tilt (one end up) and yaw (about vertical): a few degrees
    # with the steps and the depth, the sway's wobble, and the clangs (yaw rings hardest, like gyro x on 10-31).
    # The slow roll and yaw steps are timed to SQUAT_TNG_REPS as it stands (reps 5.6 to 17.1 s, the pause at 10.25)
    deg_per = lambda hz: 1 / (2 * np.pi * hz)      # angle amplitude per deg/s of ringing rate
    roll = add(steps(t, [(2.0, 2.2, -11), (4.4, 1.2, 4), (5.6, 4.6, 26), (10.5, 4.0, -10), (15.0, 3.0, 14),
                         (w0, w1 - w0, 2), (ev["rerack"][0], 0.6, 6)]),
               of_depth(z, STAND, 8.0, 0.0), add(*[ring(t, tb, s * 50 * deg_per(29), 29) for tb, s in bursts]))
    roll = (roll[0] + 5.0, roll[1], roll[2])       # racked at 5 deg
    tilt = add(steps(t, [(o0, 0.53, 0.6), (o0 + 0.53, 0.53, -1.0), (o0 + 1.07, 0.53, 0.4), (w0, 0.53, -0.5),
                         (w0 + 0.53, 0.53, 0.8), (w0 + 1.07, 0.53, -0.3)]),
               of_depth(z, STAND, 2.5, 0.0), *[sway(t, a, b, 0.12, phase=0.5) for a, b in stands],
               *[ring(t, tb, s * 110 * deg_per(37), 37) for tb, s in bursts])
    yaw = add(steps(t, [(o0, 0.53, 1.5), (o0 + 0.53, 0.53, -2.5), (o0 + 1.07, 0.53, 1.0), (5.6, 4.6, 0.8),
                        (10.5, 6.6, -0.8), (w0, 0.53, -1.2), (w0 + 0.53, 0.53, 2.0), (w0 + 1.07, 0.53, -0.8)]),
              *[sway(t, a, b, 0.10, phase=2.0) for a, b in stands],
              *[ring(t, tb, s * 260 * deg_per(31), 31) for tb, s in bursts])
    # R = Rz_world(yaw) Rx_world(tilt) R_POD Rz_sensor(phi), phi = -roll, so up in the sensor frame is
    # (cos roll, sin roll, 0). Body rate, exact: Rphi^T R_POD^T (Rtilt^T yaw' Z + tilt' X) + phi' z
    phi, phi_rate = -np.radians(roll[0]), -np.radians(roll[1])
    Ry, Rt, Rp = rots([0, 0, 1], np.radians(yaw[0])), rots([1, 0, 0], np.radians(tilt[0])), rots([0, 0, 1], phi)
    R = Ry @ Rt @ R_POD @ Rp
    w_world = np.einsum("nji,j->ni", Rt, [0, 0, 1.0]) * np.radians(yaw[1])[:, None] + np.outer(np.radians(tilt[1]), [1, 0, 0])
    w_b = np.einsum("nji,nj->ni", Rp, w_world @ R_POD) + np.outer(phi_rate, [0, 0, 1])

    acc_w = np.column_stack([p[2] for p in pos])
    f_s = np.einsum("nji,nj->ni", R, acc_w + np.array([0, 0, G]))       # R^T (a + g up)
    acc = POD_ACC_SCALE * f_s + POD_ACC_OFFSET + rng.normal(0, 1, f_s.shape) * POD_ACC_NOISE
    acc = np.clip(np.round(acc / POD_ACC_LSB) * POD_ACC_LSB, -POD_ACC_MAX, POD_ACC_MAX)
    gyro = np.degrees(w_b) + POD_GYRO_BIAS + rng.normal(0, POD_GYRO_NOISE, w_b.shape)
    gyro = np.clip(np.round(gyro / POD_GYRO_LSB) * POD_GYRO_LSB, -POD_GYRO_MAX, POD_GYRO_MAX)

    # the truth per rep, from the exact path: bottom to top turnaround (v = 0 at both), checked to go
    # up all the way (a knot change that makes a rep reverse mid-way fails here, not in a test)
    v_up = pos[2][1]
    for r in reps:
        tf = np.linspace(r["t_start"], r["t_end"], 4001)
        vf = through(tf, z_knots)[1]
        assert (vf[1:-1] > 0).all(), f"rep {r['rep']} doesn't go up all the way"
        te = np.linspace(r["ecc_start"], r["t_start"], 4001)
        assert (through(te, z_knots)[1][1:-1] < 0).all(), f"rep {r['rep']}'s descent doesn't go down all the way"
        r.update(rom=r["z_top"] - r["z_bottom"], dur=r["t_end"] - r["t_start"], peak_v=float(vf.max()),
                 t_peak=float(tf[np.argmax(vf)]))
        r["mcv"] = r["rom"] / r["dur"]
    pos_w = np.column_stack([p[0] for p in pos])
    return t, acc, gyro, {"pos": pos_w, "v_up": v_up, "R": R, "reps": reps, "events": ev}


GYM = {"squat_tng": squat_tng}


def write_gym(name, seed=1):
    """data/fake_motion_<name>.csv (the pod's columns), _truth.csv per sample (the columns above plus
    v_up and the roll), and _truth.json: the reps and when everything happens."""
    import json
    t, acc, gyro, truth = GYM[name](seed)
    OUT.mkdir(exist_ok=True)
    rows = np.column_stack([t, acc, np.linalg.norm(acc, axis=1), gyro])
    np.savetxt(OUT / f"fake_motion_{name}.csv", rows, delimiter=",", header=HEADER, comments="", fmt="%.6f")
    up = truth["R"][:, 2, :]
    bar_w = truth["R"] @ np.array(BAR_POD, float)
    tilt = np.degrees(np.arcsin(np.clip(bar_w[:, 2], -1, 1)))
    roll = np.degrees(np.arctan2(up[:, 1], up[:, 0]))              # the sleeve's roll, as gravity shows it
    np.savetxt(OUT / f"fake_motion_{name}_truth.csv", np.column_stack([t, truth["pos"], up, tilt, truth["v_up"], roll]),
               delimiter=",", header="t,x_fwd,y_bar,z_up,up_sx,up_sy,up_sz,tilt_deg,v_up,roll_deg", comments="", fmt="%.6f")
    fs = 1 / (t[1] - t[0])
    keys = ("rep", "t_start", "t_end", "dur", "rom", "mcv", "peak_v", "t_peak", "short_of_standing", "pause_after")
    out = dict(scenario=name, lift="squat", fs=fs, seed=seed, samples=len(t),
               note="exact path: a rep is its bottom turnaround to its top one (v = 0 at both, continuous time); "
                    "i_start/i_end are the nearest samples. Heights are above the hooks; rom = z_top - z_bottom, "
                    "mcv = rom / dur",
               reps=[dict({k: r[k] for k in keys}, i_start=int(round(r["t_start"] * fs)), i_end=int(round(r["t_end"] * fs)),
                          z_bottom=r["z_bottom"], z_top=r["z_top"]) for r in truth["reps"]],
               events=truth["events"], stand_height=STAND, roll_deg_range=[float(roll.min()), float(roll.max())],
               sensor=dict(acc_offset=POD_ACC_OFFSET.tolist(), acc_scale=POD_ACC_SCALE, acc_noise=POD_ACC_NOISE.tolist(),
                           gyro_bias=POD_GYRO_BIAS.tolist(), gyro_noise=POD_GYRO_NOISE, acc_lsb=POD_ACC_LSB,
                           gyro_lsb=POD_GYRO_LSB))
    rnd = lambda o: round(o, 6) if isinstance(o, float) else [rnd(v) for v in o] if isinstance(o, (list, tuple)) else \
        {k: rnd(v) for k, v in o.items()} if isinstance(o, dict) else o
    (OUT / f"fake_motion_{name}_truth.json").write_text(json.dumps(rnd(out), indent=1))
    return OUT / f"fake_motion_{name}.csv", truth["reps"]


if __name__ == "__main__":
    for name in (sys.argv[1:] or list(SCENARIOS) + list(GYM)):
        if name in GYM:
            path, reps = write_gym(name)
            roms = " ".join(f"{r['rom'] * 100:.0f}" for r in reps)
            mcvs = " ".join(f"{r['mcv']:.2f}" for r in reps)
            print(f"wrote {path}: {len(reps)} reps, ROM {roms} cm, MCV {mcvs} m/s")
        else:
            print(f"wrote {write(name)}")
