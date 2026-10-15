"""
Make a FAKE phyphox-style CSV of a phone lift with a KNOWN height.

Why bother? Before trusting phone_lift.py on real data, test it on data
where you know the right answer. If it can't recover the height from
clean fake data, the bug is in the code, not the sensor. This is called
a known-answer test and you'll use the same trick for every algorithm
in this project.

Usage:
    python make_fake_lift.py            # phone held perfectly flat
    python make_fake_lift.py 10         # phone tilts up to 10 deg mid-lift

Writes data/fake_lift_tilt<N>.csv
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

# ---- The "truth" we're simulating ----
HEIGHT = 0.74        # m, floor to table
LIFT_TIME = 1.2      # s, how long the lift takes
STILL_BEFORE = 2.0   # s, phone sitting on the floor
STILL_AFTER = 2.0    # s, phone sitting on the table
FS = 200             # Hz, roughly what a phone accelerometer gives you
G = 9.81             # m/s^2

# ---- Sensor imperfections ----
BIAS = 0.05          # m/s^2, constant offset on the z axis
NOISE = 0.03         # m/s^2, random noise (standard deviation)

tilt_deg = float(sys.argv[1]) if len(sys.argv) > 1 else 0.0

rng = np.random.default_rng(seed=1)  # fixed seed = same fake data every run

# Timestamps. Real phones don't sample perfectly evenly, so add +/-10% jitter.
total_time = STILL_BEFORE + LIFT_TIME + STILL_AFTER
n = int(total_time * FS)
dt = (1 / FS) * (1 + 0.2 * (rng.random(n) - 0.5))
t = np.cumsum(dt) - dt[0]

# Fraction of the way through the lift: 0 before, 0..1 during, 1 after.
s = np.clip((t - STILL_BEFORE) / LIFT_TIME, 0, 1)

# Minimum-jerk motion: the smoothest way to go from rest to rest.
# Human reaching/lifting looks a lot like this.
#   position     x(s) = H * (10 s^3 - 15 s^4 + 6 s^5)
#   acceleration a(s) = H / T^2 * (60 s - 180 s^2 + 120 s^3)
# (a is the second derivative of x with respect to time, T = LIFT_TIME)
a_true = HEIGHT / LIFT_TIME**2 * (60 * s - 180 * s**2 + 120 * s**3)
a_true[(s <= 0) | (s >= 1)] = 0.0

# Tilt: phone rocks up to tilt_deg in the middle of the lift, flat at both ends.
theta = np.radians(tilt_deg) * np.sin(np.pi * s)

# What the accelerometer actually reports (more on this in docs/01-phone-lift.md):
# it feels the support force, so at rest it reads +g, not 0.
# The vertical "specific force" is (g + a). Tilting the phone splits it
# between the z and y axes.
f_vertical = G + a_true
az = f_vertical * np.cos(theta) + BIAS + NOISE * rng.standard_normal(n)
ay = f_vertical * np.sin(theta) + NOISE * rng.standard_normal(n)
ax = NOISE * rng.standard_normal(n)

df = pd.DataFrame({
    "Time (s)": t,
    "Acceleration x (m/s^2)": ax,
    "Acceleration y (m/s^2)": ay,
    "Acceleration z (m/s^2)": az,
    "Absolute acceleration (m/s^2)": np.sqrt(ax**2 + ay**2 + az**2),
})

# Save next to this script in data/, no matter where you run it from
out_path = Path(__file__).parent / "data" / f"fake_lift_tilt{tilt_deg:g}.csv"
df.to_csv(out_path, index=False)
print(f"Wrote data/{out_path.name}  ({n} samples, true height {HEIGHT} m, tilt {tilt_deg:g} deg)")
