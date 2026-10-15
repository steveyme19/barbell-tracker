"""
Demo: why a velocity THRESHOLD for rep boundaries wrecks slow reps.

A common way to decide when a rep starts and ends is "the bar is moving once
velocity goes above some threshold, like 0.05 m/s". We can't see how commercial
devices do it (closed algorithms), but this demo shows what that choice does.

Each fake concentric covers 0.40 m with a smooth (half-sine) velocity curve. We
compute MCV two ways:
  - true: over the whole concentric (start to stop)
  - thresholded: only over the part where velocity > THRESHOLD

Usage (from the analysis folder):
    python demos/threshold_bias.py
"""

import numpy as np

ROM = 0.40         # m
THRESHOLD = 0.05   # m/s, try 0.10

print(f"Threshold {THRESHOLD} m/s, ROM {ROM} m\n")
print(" true MCV | concentric time | thresholded MCV | error | ROM it sees")
for mcv_true in [1.0, 0.5, 0.3, 0.2, 0.15, 0.10]:
    T = ROM / mcv_true             # concentric duration
    v_peak = mcv_true * np.pi / 2  # half-sine: mean = peak * 2/pi
    t = np.linspace(0, T, 200001)
    v = v_peak * np.sin(np.pi * t / T)

    above = v > THRESHOLD
    t_in = t[above]
    dist = np.trapezoid(v[above], t_in)    # distance covered while "moving"
    dur = t_in[-1] - t_in[0]               # time spent "moving"
    mcv_thr = dist / dur

    err = (mcv_thr / mcv_true - 1) * 100
    print(f"  {mcv_true:5.2f}  |     {T:4.1f} s      |      {mcv_thr:5.3f}      | {err:+4.0f}% |"
          f"  {dist * 100:4.1f} cm")

print("""
Notice: the error in m/s is about the same at every speed (~0.02 m/s here).
A fixed offset is nothing on a 1.0 m/s rep and huge on a 0.10 m/s grinder.
That's why this project finds rep boundaries from stillness and zero crossings
(algorithm.md section 5 to 8), never from a velocity threshold.""")
