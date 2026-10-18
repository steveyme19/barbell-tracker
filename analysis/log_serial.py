"""
Record the XIAO's IMU stream (firmware/imu_serial) into a CSV the rest of the code reads.

Usage (close the Arduino Serial Monitor first, it holds the port):
    python log_serial.py COM5                  # Ctrl+C to stop
    python log_serial.py COM5 --name=desk      # adds "-desk" to the file name
    python log_serial.py COM5 --seconds=30     # stops by itself after 30 s

Saves data/bar-xiao-YYYY-MM-DD-HHMM[-name].csv with the same columns as the app's Save
CSV. Time comes from the XIAO's own clock (D4). Every 2 s it prints the real sample rate,
gaps, the wiggle of |a| (the rest noise, D20), the gyro offset and which axis points up.

Needs pyserial once:  pip install pyserial
"""

import sys
from datetime import datetime
from pathlib import Path

import numpy as np

HEADER = ('"Time (s)","Acceleration x (m/s^2)","Acceleration y (m/s^2)","Acceleration z (m/s^2)",'
          '"Absolute acceleration (m/s^2)","Gyro x (deg/s)","Gyro y (deg/s)","Gyro z (deg/s)"')
REPORT_EVERY = 2.0   # s of device time between printouts


def parse(line):
    """'123456,0.01,...' -> (t_us, ax, ay, az, gx, gy, gz), or None for messages/garbage."""
    parts = line.strip().split(",")
    if line.startswith("#") or len(parts) != 7:
        return None
    try:
        return (int(parts[0]),) + tuple(float(p) for p in parts[1:])
    except ValueError:
        return None


def report(rows, t_now):
    """One status line from the samples since the last report."""
    a = np.array([r[1:4] for r in rows])
    gy = np.array([r[4:7] for r in rows])
    t = np.array([r[0] for r in rows])
    mag = np.linalg.norm(a, axis=1)
    dt = np.diff(t)
    gaps = int((dt > 1.5 * np.median(dt)).sum()) if len(dt) > 2 else 0
    rate = (len(t) - 1) / (t[-1] - t[0]) if len(t) > 1 and t[-1] > t[0] else 0.0
    mean = a.mean(axis=0)
    k = int(np.argmax(np.abs(mean)))
    up = ("+" if mean[k] > 0 else "-") + "xyz"[k]
    off = gy.mean(axis=0)
    print(f"{t_now:6.1f} s | {rate:6.1f} Hz | gaps {gaps} | |a| {mag.mean():.3f} wiggle {mag.std():.4f} m/s^2 | "
          f"gyro offset {off[0]:+.2f} {off[1]:+.2f} {off[2]:+.2f} deg/s | up {up}")
    return gaps


def record(lines, out, seconds=None):
    """Write samples from an iterable of text lines to the CSV file `out`. Returns
    (samples, gaps, duration). Stops at the end of `lines` or after `seconds`."""
    n = gaps = 0
    t0_us = last_us = None
    wrap = 0                 # micros() rolls over every 71.6 minutes
    window, t_rep = [], 0.0
    t = 0.0
    with open(out, "w", newline="") as f:
        f.write(HEADER + "\n")
        for line in lines:
            if line.startswith("#"):
                print(line.strip())
                continue
            s = parse(line)
            if s is None:
                continue
            t_us = s[0]
            if last_us is not None and t_us < last_us:
                wrap += 2 ** 32
            last_us = t_us
            if t0_us is None:
                t0_us = t_us + wrap
            t = (t_us + wrap - t0_us) / 1e6
            ax, ay, az, gx, gy, gz = s[1:]
            f.write(f"{t:.6f},{ax:.4f},{ay:.4f},{az:.4f},{np.sqrt(ax*ax + ay*ay + az*az):.4f},"
                    f"{gx:.3f},{gy:.3f},{gz:.3f}\n")
            n += 1
            window.append((t,) + s[1:])
            if t - t_rep >= REPORT_EVERY:
                gaps += report(window, t)
                window, t_rep = [], t
            if seconds is not None and t >= seconds:
                break
    return n, gaps, t


def serial_lines(port):
    """Text lines from the XIAO, forever (Ctrl+C stops)."""
    import serial    # pip install pyserial
    with serial.Serial(port, 115200, timeout=1) as ser:
        ser.reset_input_buffer()
        ser.readline()                      # first line may be cut in half
        while True:
            raw = ser.readline()
            if raw:
                yield raw.decode("ascii", errors="ignore")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        sys.exit(__doc__)
    name = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--name=")), "")
    seconds = next((float(a.split("=", 1)[1]) for a in sys.argv if a.startswith("--seconds=")), None)
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M")
    out = Path(__file__).parent / "data" / f"bar-xiao-{stamp}{'-' + name if name else ''}.csv"
    print(f"Recording {args[0]} to {out.name}. Ctrl+C to stop.")
    try:
        n, gaps, dur = record(serial_lines(args[0]), out, seconds)
    except KeyboardInterrupt:
        n = gaps = dur = None
    if n is None:                           # stopped with Ctrl+C: count what got saved
        n = sum(1 for _ in open(out)) - 1
        print()
    print(f"Saved {n} samples to {out}" + (f" ({dur:.1f} s, {gaps} gaps)" if dur else ""))


if __name__ == "__main__":
    main()
