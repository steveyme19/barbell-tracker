"""Walk through the XIAO axis map: 5 short recordings, one per orientation, and say which
axis points up in each. Saves them with log_serial.py (kept in data/, P2).

    python axis_map.py COM8
"""
import subprocess
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
POSES = [  # (file name tag, what to do)
    ("usb-up", "stand it on end, USB-C end UP"),
    ("usb-down", "stand it on end, USB-C end DOWN"),
    ("d0d6-up", "on its long edge, D0-D6 pin edge UP"),
    ("5v-up", "on its long edge, 5V/GND/3V3 pin edge UP"),
    ("chip-down", "flat, chip side DOWN"),
]


def up_axis(csv):
    # mean acceleration while still: gravity reads +9.8 on the axis pointing up
    a = np.genfromtxt(csv, delimiter=",", skip_header=1, usecols=(1, 2, 3)).mean(axis=0)
    k = int(np.argmax(np.abs(a)))
    return f"{'+-'[int(a[k] < 0)]}{'xyz'[k]}", a


port = sys.argv[1] if len(sys.argv) > 1 else "COM8"
for tag, what in POSES:
    done = sorted((HERE / "data").glob(f"bar-xiao-*-axis-{tag}.csv"))
    if done:   # already recorded (e.g. a rerun after a crash): reuse it
        up, a = up_axis(done[-1])
        print(f"  {tag}: up is {up}   (mean a = {a[0]:+.2f} {a[1]:+.2f} {a[2]:+.2f} m/s^2)  -> {done[-1].name} (already done)")
        continue
    input(f"\nBoard: {what}. Hold it still, then press Enter...")
    before = set((HERE / "data").glob("bar-xiao-*.csv"))
    subprocess.run([sys.executable, str(HERE / "log_serial.py"), port, f"--name=axis-{tag}", "--seconds=6"],
                   check=True, stdout=subprocess.DEVNULL)
    csv = max(set((HERE / "data").glob("bar-xiao-*.csv")) - before, key=lambda p: p.stat().st_mtime)
    up, a = up_axis(csv)
    print(f"  {tag}: up is {up}   (mean a = {a[0]:+.2f} {a[1]:+.2f} {a[2]:+.2f} m/s^2)  -> {csv.name}")
print("\nDone. Tell Claude 'axis map done'.")
