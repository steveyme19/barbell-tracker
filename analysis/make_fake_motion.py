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


if __name__ == "__main__":
    for name in (sys.argv[1:] or list(SCENARIOS)):
        print(f"wrote {write(name)}")
