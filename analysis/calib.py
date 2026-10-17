"""
Accelerometer calibration from 6 still positions (algorithm.md section 1, roadmap 1.3).
Reference for webapp/calib.js.

Lay the sensor on each face: each axis once pointing up and once pointing down. For axis
k, with m_up and m_down its average readings in those two positions:
    scale_k = (m_up - m_down) / (2 g)        (1.0 = perfect)
    bias_k  = (m_up + m_down) / 2            (m/s^2)
    corrected = (raw - bias) / scale
Why it matters: a scale error goes straight into velocity and ROM (2% high reads 2% fast),
and the drift fix can't see it. Gyro scale needs a known rotation (a full turn), not this.

Usage:
    python calib.py "[[0.1,0.0,9.86],[0.1,0.0,-9.70],[9.83,0.0,0.05],[-9.79,0.0,0.05],[0.1,9.81,0.0],[0.1,-9.81,0.0]]"
    python calib.py "<same>" --json
"""

import json
import sys

G = 9.80     # m/s^2, local gravity (West Lafayette ~9.801)


def orientation(m):
    """(axis, sign) of the axis closest to vertical in a still reading m = [x, y, z]."""
    k = max(range(3), key=lambda i: abs(m[i]))
    return k, 1 if m[k] > 0 else -1


def fit_calibration(means, g=G):
    """means: 6 still readings [x, y, z], one per face (any order). Returns dict(bias,
    scale, residual) or None if a face is missing. residual = worst |corrected magnitude - g|
    over the 6 positions (how well the simple per-axis model fits)."""
    up, down = [None] * 3, [None] * 3
    for m in means:
        k, s = orientation(m)
        (up if s > 0 else down)[k] = m[k]
    if None in up or None in down:
        return None
    scale = [(up[k] - down[k]) / (2 * g) for k in range(3)]
    bias = [(up[k] + down[k]) / 2 for k in range(3)]
    res = 0.0
    for m in means:
        c = [(m[k] - bias[k]) / scale[k] for k in range(3)]
        res = max(res, abs(sum(x * x for x in c) ** 0.5 - g))
    return dict(bias=bias, scale=scale, residual=res)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        sys.exit(__doc__)
    c = fit_calibration(json.loads(args[0]))
    if "--json" in sys.argv:
        print(json.dumps(c))
        return
    if c is None:
        sys.exit("Need all 6 faces: each axis once up and once down.")
    for k, ax in enumerate("xyz"):
        print(f"{ax}: bias {c['bias'][k]:+.3f} m/s^2, scale {(c['scale'][k] - 1) * 100:+.2f}%")
    print(f"worst |a| error after: {c['residual']:.3f} m/s^2")


if __name__ == "__main__":
    main()
