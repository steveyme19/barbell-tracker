"""
Demo: fusing an IMU with a laser distance sensor (complementary filter).

Fake set: 3 squat reps, NO pause at lockout, last rep a slow grinder. That's the
IMU's worst case: no stillness between reps, so drift has the whole set to grow.

Three estimates of bar velocity:
  A) IMU only: integrate, zero-velocity correction only at the start and end of the set
  B) laser only: smooth the measured height, then differentiate
  C) fused: IMU for fast changes, laser for the slow truth

The laser numbers are ASSUMPTIONS. The TF-Luna spec says +/-6 cm accuracy and 1 cm
resolution but doesn't split that into random noise vs. systematic error. Measure
your own sensor (bench test) and put the real numbers here.

Usage (from the analysis folder):
    python demos/imu_laser_fusion.py
"""

import numpy as np
import matplotlib.pyplot as plt

# ---- Knobs ----
LASER_NOISE = 0.01    # m, random noise per reading (assumed, try 0.03)
LASER_SCALE = 1.00    # 1.02 = laser reads 2% long (systematic error, try it)
LASER_HZ = 100        # readings per second
CROSSOVER_HZ = 1.0    # below this, trust the laser; above it, trust the IMU (try 0.3, 3)

FS = 200              # IMU sample rate
dt = 1 / FS
ROM = 0.55            # m
rng = np.random.default_rng(3)

# ---- Build the true motion: (phase, duration in s) ----
phases = [("still", 1.0), ("down", 1.1), ("up", 0.9), ("down", 1.1), ("up", 1.4),
          ("down", 1.2), ("up", 3.7), ("still", 1.0)]
t_parts, v_parts, concentrics = [], [], []
t0 = 0.0
for kind, dur in phases:
    n = int(round(dur * FS))
    tt = t0 + np.arange(n) * dt
    if kind == "still":
        vv = np.zeros(n)
    else:
        v_peak = ROM / dur * np.pi / 2        # half-sine covering ROM
        sign = 1 if kind == "up" else -1
        vv = sign * v_peak * np.sin(np.pi * (tt - t0) / dur)
        if kind == "up":
            concentrics.append((t0, t0 + dur))
    t_parts.append(tt)
    v_parts.append(vv)
    t0 += dur
t = np.concatenate(t_parts)
v_true = np.concatenate(v_parts)


def integrate(y):
    out = np.zeros_like(y)
    out[1:] = np.cumsum(0.5 * (y[1:] + y[:-1]) * dt)
    return out


x_true = integrate(v_true)
a_true = np.gradient(v_true, dt)

# ---- Fake IMU: 1% scale error, constant bias, slowly wandering bias, noise ----
a_imu = (1.01 * a_true + 0.04 + 0.03 * np.sin(2 * np.pi * t / 15)
         + 0.03 * rng.standard_normal(len(t)))

# ---- Fake laser: offset, scale error, noise, 1 cm steps, and a 0.4 s dropout ----
step = FS // LASER_HZ
laser_idx = np.arange(0, len(t), step)
z = LASER_SCALE * x_true[laser_idx] + 0.03 + LASER_NOISE * rng.standard_normal(len(laser_idx))
z = np.round(z * 100) / 100                     # 1 cm resolution
dropout = (t[laser_idx] > 7.0) & (t[laser_idx] < 7.4)  # beam blocked
z[dropout] = np.nan

# ---- A) IMU only: the best you can do with stillness only at the set ends ----
i0, i1 = int(0.9 * FS), len(t) - int(0.9 * FS)
v_A = np.zeros_like(t)
seg = integrate(a_imu[i0:i1])
ts = t[i0:i1] - t[i0]
v_A[i0:i1] = seg - seg[-1] * ts / ts[-1]       # straight-line drift correction

# ---- B) laser only: fill the dropout, smooth, differentiate ----
tz = t[laser_idx]
ok = ~np.isnan(z)
z_filled = np.interp(tz, tz[ok], z[ok])
z_smooth = np.convolve(z_filled, np.ones(11) / 11, mode="same")
v_B = np.interp(t, tz, np.gradient(z_smooth, tz))

# ---- C) fused: third-order complementary filter ----
# State: height xh, velocity vh, accel bias bh. Every step, predict with the IMU.
# When a laser reading arrives, nudge all three toward it in proportion to the error.
#   k1, k2, k3 all come from one knob (w = crossover in rad/s). These values put all
#   three of the filter's error-decay rates at w, so it settles smoothly with no
#   overshoot. The bias state lets the laser *learn* the IMU's bias.
w = 2 * np.pi * CROSSOVER_HZ
k1, k2, k3 = 3 * w, 3 * w**2, w**3
laser_at = dict(zip(laser_idx, z))
xh, vh, bh = z_filled[0], 0.0, 0.0
v_C = np.zeros_like(t)
for i in range(len(t)):
    zi = laser_at.get(i, np.nan)
    e = 0.0 if np.isnan(zi) else (zi - xh) * step  # x step: readings come every `step` samples
    bh += -k3 * e * dt
    vh += (a_imu[i] - bh + k2 * e) * dt
    xh += (vh + k1 * e) * dt
    v_C[i] = vh


# ---- Score each method per rep ----
def score(name, v_est):
    cells = []
    for c0, c1 in concentrics:
        m = (t >= c0) & (t < c1)
        mcv_t, mcv_e = v_true[m].mean(), v_est[m].mean()
        pk_t, pk_e = v_true[m].max(), v_est[m].max()
        cells.append(f"MCV {mcv_t:.2f}->{mcv_e:.2f} ({(mcv_e / mcv_t - 1) * 100:+3.0f}%), "
                     f"peak {(pk_e / pk_t - 1) * 100:+4.0f}%")
    print(f"{name:14s} " + " | ".join(cells))


print(f"laser noise {LASER_NOISE * 100:.0f} cm, laser scale {LASER_SCALE}, "
      f"crossover {CROSSOVER_HZ} Hz\n")
score("A) IMU only", v_A)
score("B) laser only", v_B)
score("C) fused", v_C)

plt.plot(t, v_true, "k", lw=3, alpha=0.3, label="true")
plt.plot(t, v_A, label="A) IMU only")
plt.plot(t, v_B, alpha=0.6, label="B) laser only")
plt.plot(t, v_C, label="C) fused")
plt.axhline(0, color="k", lw=0.5)
plt.xlabel("time (s)")
plt.ylabel("velocity (m/s)")
plt.title("3 reps, no pauses, last rep is a grinder")
plt.legend()
plt.tight_layout()
plt.show()
