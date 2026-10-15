"""
Demo: why you should NOT force velocity = 0 at the bottom of a rep.

Fake squat rep: still at the top, down 0.6 m in 1.0 s, up 0.6 m in 0.8 s, still.
The accelerometer has a constant bias b (the drift source). We correct two ways:

  A) Top to top: force v = 0 only where the bar is actually still (both ends).
  B) Same, PLUS force v = 0 at the bottom, found as the zero crossing of the
     drifting velocity.

Then compare the mean concentric velocity (MCV) each one gives to the truth.

Usage (from the analysis folder):
    python demos/bottom_anchor.py
"""

import numpy as np
import matplotlib.pyplot as plt

BIAS = 0.05  # m/s^2, try 0.2 too
USE_TRUE_BOTTOM = False  # True = anchor at the real bottom time (you'd need outside evidence for this)

# ---- Build the true motion ----
fs = 400
t = np.arange(0, 2.6, 1 / fs)
A = 0.6 * np.pi / 2 / 1.0  # peak speed going down (sets ROM to 0.6 m)
B = 0.6 * np.pi / 2 / 0.8  # peak speed going up
v_true = np.zeros_like(t)
down = (t >= 0.3) & (t < 1.3)
up = (t >= 1.3) & (t < 2.1)  # this is the true concentric
v_true[down] = -A * np.sin(np.pi * (t[down] - 0.3) / 1.0)
v_true[up] = B * np.sin(np.pi * (t[up] - 1.3) / 0.8)
a_true = np.gradient(v_true, t)


def integrate(y, t):
    """Trapezoid running integral, same as phone_lift.py."""
    out = np.zeros_like(y)
    out[1:] = np.cumsum(0.5 * (y[1:] + y[:-1]) * np.diff(t))
    return out


# ---- What the sensor gives you: truth + bias, integrated ----
v_raw = integrate(a_true + BIAS, t)

# A) Top-to-top: one straight line through the whole rep (step 0.1's trick)
v_A = v_raw - v_raw[-1] * t / t[-1]

# B) Also anchor at the "bottom" = where the DRIFTING velocity changes direction.
#    Note: v_raw is already 0 there by definition, so this constraint adds no new
#    information about the drift. It only changes how the end error gets spread out.
middle = np.where((t > 0.8) & (t < 1.8))[0]
ic = middle[np.argmin(np.abs(v_raw[middle]))]
if USE_TRUE_BOTTOM:
    ic = np.argmin(np.abs(t - 1.3))  # cheating: we only know this because we built the fake rep

# Piecewise correction: one straight line from the start to the anchor,
# another from the anchor to the end, so v = 0 at all three points.
v_B = v_raw.copy()
v_B[:ic] -= v_raw[ic] * t[:ic] / t[ic]
v_B[ic:] -= v_raw[ic] + (v_raw[-1] - v_raw[ic]) * (t[ic:] - t[ic]) / (t[-1] - t[ic])

print(f"Bias {BIAS} m/s^2, anchor at {'TRUE bottom' if USE_TRUE_BOTTOM else 'drifting zero crossing'}")
print(f"True MCV:              {v_true[up].mean():.3f} m/s")
print(f"A) top-to-top:         {v_A[up].mean():.3f} m/s")
print(f"B) + bottom anchor:    {v_B[up].mean():.3f} m/s")
print(f"Anchor is off from the real bottom by {(t[ic] - 1.3) * 1000:.0f} ms")
print(f"Raw velocity at the anchor: {v_raw[ic]:+.3f} m/s "
      f"(the drift that had built up: {BIAS * t[ic]:.3f} m/s)")

plt.plot(t, v_true, "k", lw=3, alpha=0.3, label="true")
plt.plot(t, v_raw, ":", label="raw (drifting)")
plt.plot(t, v_A, label="A) top-to-top")
plt.plot(t, v_B, "--", label="B) + bottom anchor")
plt.axvline(t[ic], color="gray", lw=0.5)
plt.axhline(0, color="k", lw=0.5)
plt.xlabel("time (s)")
plt.ylabel("velocity (m/s)")
plt.title(f"Constant bias {BIAS} m/s²")
plt.legend()
plt.tight_layout()
plt.show()
