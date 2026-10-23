"""
Sample timing of a recording: rate, gaps and dt jitter. The regression check for every pod
firmware change (docs/ble-plan.md): 209-212 Hz, 0 gaps, dt std <= 0.05 ms (fail above 0.1 ms).

    python dt_stats.py data/bar-xiao-2026-10-21-1200-desk.csv [more.csv ...]

A gap is dt > 1.5 x the median dt, the same rule as log_serial.py.
"""

import sys

import numpy as np


def stats(path):
    t = np.loadtxt(path, delimiter=",", skiprows=1, usecols=0)
    dt = np.diff(t)
    med = np.median(dt)
    gaps = int((dt > 1.5 * med).sum())
    return {"n": len(t), "seconds": t[-1] - t[0], "rate": (len(t) - 1) / (t[-1] - t[0]),
            "gaps": gaps, "dt_ms": med * 1000, "std_ms": dt.std() * 1000,
            "min_ms": dt.min() * 1000, "max_ms": dt.max() * 1000}


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    bad = 0
    for path in sys.argv[1:]:
        s = stats(path)
        verdict = "PASS" if s["gaps"] == 0 and s["std_ms"] <= 0.1 and 209 <= s["rate"] <= 212.5 else "FAIL"
        bad += verdict == "FAIL"
        print(f"{verdict}  {path}: {s['n']} samples over {s['seconds']:.1f} s, {s['rate']:.2f} Hz, "
              f"{s['gaps']} gaps, dt {s['dt_ms']:.3f} ms (std {s['std_ms']:.4f}, "
              f"min {s['min_ms']:.3f}, max {s['max_ms']:.3f})")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
