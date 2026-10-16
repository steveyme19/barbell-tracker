"""
Make a FAKE phyphox-style recording of a set of phone "reps" with known ROM.
Known-answer test for reps.py.

Each rep: up (the concentric), optional pause at the top, back down.

Usage:
    python make_fake_reps.py            # 5 reps, 1 s pause at the top
    python make_fake_reps.py nopause    # 5 reps, no pause

Writes data/fake_reps_pause.csv or data/fake_reps_nopause.csv
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPS = 5
ROM = 0.40                                  # m
UP_TIMES = [0.9, 1.0, 1.1, 1.3, 1.6]        # s, each rep slower (fatigue)
DOWN_TIME = 1.0                             # s
PAUSE = 0.0 if "nopause" in sys.argv else 1.0
FS = 400                                    # Hz, like your phone
G = 9.81
BIAS = 0.05                                 # m/s^2 on z
NOISE = 0.03                                # m/s^2
TILT_DEG = 6                                # hand wobble, comes and goes

rng = np.random.default_rng(2)


def half_sine(dur, dist, sign):
    """Velocity for moving `dist` in `dur` seconds, smooth start and stop."""
    n = int(round(dur * FS))
    s = np.arange(n) / n
    return sign * dist / dur * np.pi / 2 * np.sin(np.pi * s)


# Velocity, phase by phase. Start and end resting.
parts = [np.zeros(int(1.5 * FS))]
true_mcv = []
for k in range(REPS):
    parts.append(half_sine(UP_TIMES[k], ROM, +1))
    true_mcv.append(ROM / UP_TIMES[k])
    if PAUSE:
        parts.append(np.zeros(int(PAUSE * FS)))
    parts.append(half_sine(DOWN_TIME, ROM, -1))
parts.append(np.zeros(int(2.0 * FS)))
v = np.concatenate(parts)
t = np.arange(len(v)) / FS
a = np.gradient(v, t)

# Sensor: hand tilt wobble splits gravity between z and y, plus bias and noise.
moving = np.convolve((np.abs(v) > 0).astype(float), np.ones(FS // 2) / (FS // 2), mode="same")
theta = np.radians(TILT_DEG) * np.sin(2 * np.pi * t / 2.3) * np.clip(moving, 0, 1)
f_vert = G + a
az = f_vert * np.cos(theta) + BIAS + NOISE * rng.standard_normal(len(t))
ay = f_vert * np.sin(theta) + NOISE * rng.standard_normal(len(t))
ax = NOISE * rng.standard_normal(len(t))

df = pd.DataFrame({
    "Time (s)": t,
    "Acceleration x (m/s^2)": ax,
    "Acceleration y (m/s^2)": ay,
    "Acceleration z (m/s^2)": az,
    "Absolute acceleration (m/s^2)": np.sqrt(ax**2 + ay**2 + az**2),
})
name = "fake_reps_pause.csv" if PAUSE else "fake_reps_nopause.csv"
out = Path(__file__).parent / "data" / name
df.to_csv(out, index=False)
print(f"Wrote data/{name}: {REPS} reps, ROM {ROM * 100:.0f} cm, pause {PAUSE} s")
print("True MCV per rep:", " ".join(f"{m:.2f}" for m in true_mcv), "m/s")
