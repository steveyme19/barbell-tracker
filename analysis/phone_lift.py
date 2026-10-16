"""
Step 1: measure the height of a fake "rep" from phone accelerometer data.

You lift your phone (held flat, screen up) from the floor onto a table.
We integrate vertical acceleration twice to get displacement, and use the
fact that the phone is still at the start AND end to cancel drift.

Read docs/01-phone-lift.md first. It explains every step below.

Usage:
    python phone_lift.py data/lift_01.csv 0.74
                         ^ phyphox export  ^ true height in meters (optional)

The export can be the CSV, the Excel file (.xlsx), or the zip phyphox makes.
"""

import sys
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

# ---- Settings you might tweak ----
AXIS = "magnitude"      # "magnitude" (tilt-proof, recommended) or "z" (screen axis only)
MIN_STILL_SECONDS = 0.3 # warn if the phone sat still for less than this at the start
MOTION_THRESHOLD = 0.3  # m/s^2: smoothed |accel| above this counts as "moving"
SMOOTH_SECONDS = 0.05   # moving-average window used only for motion detection
PAD_SECONDS = 0.15      # extra still time kept on each side of the motion
UP_FRACTION = 0.05      # "moving up" = velocity above 5% of peak (for mean velocity)


def read_phyphox(path):
    """Read a phyphox export into a table. Works with the CSV, the Excel file
    (first sheet is "Raw Data"), or the zip phyphox makes ("Raw Data.csv" inside)."""
    suffix = Path(path).suffix.lower()
    if suffix == ".xlsx":
        return pd.read_excel(path, sheet_name=0)
    if suffix == ".zip":
        with zipfile.ZipFile(path) as z:
            with z.open("Raw Data.csv") as f:
                return pd.read_csv(f)
    return pd.read_csv(path)


def load_phyphox(path, axis):
    """Returns time (s) and the vertical acceleration signal (m/s^2).

    axis = "x", "y" or "z": one phone axis.
    axis = "magnitude": total acceleration sqrt(x^2 + y^2 + z^2). Without a gyro
    this is the better choice for hand-held lifts, because it doesn't care how
    the phone is tilted. It assumes the motion is mostly up and down.
    """
    df = read_phyphox(path)
    t = df.iloc[:, 0].to_numpy()  # first column is always time
    cols = {}
    for name in ("x", "y", "z"):
        matches = [c for c in df.columns if f"Acceleration {name}" in c]
        if matches:
            cols[name] = df[matches[0]].to_numpy()
    if axis == "magnitude":
        if len(cols) < 3:
            sys.exit(f"Need x, y and z columns for magnitude. Found: {list(df.columns)}")
        return t, np.sqrt(cols["x"] ** 2 + cols["y"] ** 2 + cols["z"] ** 2)
    if axis not in cols:
        sys.exit(f"Couldn't find an 'Acceleration {axis}' column. Found: {list(df.columns)}")
    return t, cols[axis]


def integrate(y, t):
    """Running integral of y over t using the trapezoid rule. Starts at 0.

    Between two samples, the area under the curve is about
    (average of the two heights) * (time between them).
    We add those slices up one after another (cumsum).
    Uses the real timestamps, so uneven sampling is handled correctly.
    """
    out = np.zeros_like(y)
    out[1:] = np.cumsum(0.5 * (y[1:] + y[:-1]) * np.diff(t))
    return out


def moving_average(y, n):
    """Smooth y by averaging each point with its n neighbors."""
    n = max(int(n), 1)
    return np.convolve(y, np.ones(n) / n, mode="same")


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    csv_path = Path(sys.argv[1])
    true_height = float(sys.argv[2]) if len(sys.argv) > 2 else None

    t, a_raw = load_phyphox(csv_path, AXIS)
    t = t - t[0]
    fs = 1 / np.median(np.diff(t))
    print(f"\n{len(t)} samples, about {fs:.0f} Hz, {t[-1]:.2f} s long")

    # --- 1. Gravity + sensor bias from the still period at the start ---
    # Sitting still, the accelerometer reads +g (plus whatever bias it has).
    # Averaging many samples beats down the noise.
    # Find where motion starts (rough guess from the first 0.3 s), then
    # average everything before that, minus a small safety margin.
    baseline = np.median(a_raw[t < 0.3])
    rough = moving_average(np.abs(a_raw - baseline), SMOOTH_SECONDS * fs)
    if not (rough > MOTION_THRESHOLD).any():
        sys.exit("Never saw motion. Lower MOTION_THRESHOLD or check the axis.")
    onset = t[np.argmax(rough > MOTION_THRESHOLD)]
    still = t < max(onset - 0.1, 0.1)
    if onset < MIN_STILL_SECONDS:
        print(f"WARNING: only {onset:.2f} s of stillness at the start. Wait longer before lifting.")
    g_est = a_raw[still].mean()
    noise = a_raw[still].std()
    print(f"Still period: reads {g_est:.3f} m/s^2 (noise std {noise:.3f})")

    a = a_raw - g_est  # what's left is real acceleration, up = positive

    # --- 2. Find when the phone is actually moving ---
    smooth = moving_average(np.abs(a), SMOOTH_SECONDS * fs)
    moving_idx = np.where(smooth > MOTION_THRESHOLD)[0]
    if len(moving_idx) == 0:
        sys.exit("Never saw motion. Lower MOTION_THRESHOLD or check the axis.")
    pad = int(PAD_SECONDS * fs)
    i0 = max(moving_idx[0] - pad, 0)
    i1 = min(moving_idx[-1] + pad, len(t) - 1)
    if t[-1] - t[i1] < 0.3:
        print("WARNING: recording ends right after the motion. Let it sit still longer.")
    print(f"Motion window: {t[i0]:.2f} s to {t[i1]:.2f} s ({t[i1] - t[i0]:.2f} s)")

    # Only integrate inside the window. Less time integrating = less drift.
    tw = t[i0:i1 + 1] - t[i0]
    aw = a[i0:i1 + 1]

    # --- 3. Acceleration -> velocity ---
    v_raw = integrate(aw, tw)

    # --- 4. Zero-velocity correction ---
    # Phone is still at both ends, so TRUE velocity is 0 at tw[0] and tw[-1].
    # v_raw starts at 0 by construction, but ends at some nonzero value.
    # That leftover is pure error. If it came from a constant accel error b,
    # the velocity error grows in a straight line: b * t.
    # So: b = leftover / duration, and subtract that straight line.
    drift_rate = v_raw[-1] / tw[-1]  # this IS b, in m/s^2
    v = v_raw - drift_rate * tw

    # --- 5. Velocity -> position ---
    x = integrate(v, tw)
    x_raw = integrate(v_raw, tw)  # for comparison: no correction
    height = x[-1]
    peak = x.max()
    # If the phone went up and came back down (a "rep"), the end height is ~0
    # and the number that matters is the peak.
    came_back = abs(height) < 0.5 * peak
    measured = peak if came_back else height

    # --- 6. Lifting metrics (preview of the real thing) ---
    v_peak = v.max()
    up = np.where(v > UP_FRACTION * v_peak)[0]
    j0, j1 = up[0], up[-1]
    up_time = tw[j1] - tw[j0]
    # Mean velocity over an interval = distance / time. No averaging of samples needed.
    v_mean = (x[j1] - x[j0]) / up_time

    print(f"\nVelocity at end before correction: {v_raw[-1]:+.3f} m/s"
          f"  -> implied accel error {drift_rate:+.4f} m/s^2")
    print(f"Height, no correction:   {x_raw[-1]:.3f} m")
    print(f"Height, with correction: {height:.3f} m")
    print(f"Peak height:             {peak:.3f} m")
    if came_back:
        print("Phone came back down (up-and-down rep), so the PEAK is the measurement.")
    if true_height is not None:
        err = measured - true_height
        print(f"True height:             {true_height:.3f} m"
              f"  -> error {err * 100:+.1f} cm ({err / true_height * 100:+.1f}%)")
    print(f"Peak velocity: {v_peak:.2f} m/s")
    print(f"Mean velocity while moving up: {v_mean:.2f} m/s over {up_time:.2f} s")

    # --- 7. Plots ---
    tw_abs = tw + t[i0]  # put window times back on the full-recording clock
    fig, (ax1, ax2, ax3) = plt.subplots(3, 1, sharex=True, figsize=(9, 8))

    ax1.plot(t, a, lw=0.8)
    ax1.axvspan(t[i0], t[i1], color="orange", alpha=0.2, label="motion window")
    ax1.set_ylabel("accel (m/s²)")
    ax1.set_title(csv_path.name)
    ax1.legend()

    ax2.plot(tw_abs, v_raw, "--", label="raw (drifts)")
    ax2.plot(tw_abs, v, label="zero-velocity corrected")
    ax2.axhline(0, color="k", lw=0.5)
    ax2.set_ylabel("velocity (m/s)")
    ax2.legend()

    ax3.plot(tw_abs, x_raw, "--", label="raw")
    ax3.plot(tw_abs, x, label="corrected")
    if true_height is not None:
        ax3.axhline(true_height, color="g", ls=":", label="true height")
    ax3.set_ylabel("height (m)")
    ax3.set_xlabel("time (s)")
    ax3.legend()

    fig.tight_layout()
    out_dir = Path(__file__).parent / "out"
    out_dir.mkdir(exist_ok=True)
    png = out_dir / (csv_path.stem + ".png")
    fig.savefig(png, dpi=120)
    print(f"\nPlot saved to {png}")
    plt.show()


if __name__ == "__main__":
    main()
