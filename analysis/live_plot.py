"""Live scrolling plot of the XIAO's IMU (imu_serial over USB): the last 10 s of accel
and gyro. Nothing is saved; use log_serial.py to record.

    python live_plot.py          (finds the XIAO's port)
    python live_plot.py COM14
"""
import sys
import threading
from collections import deque

import matplotlib.pyplot as plt
import serial
import serial.tools.list_ports as lp
from matplotlib.animation import FuncAnimation

WINDOW_S = 10   # seconds shown
N = 2600        # samples kept (~12 s at 210 Hz)

port = sys.argv[1] if len(sys.argv) > 1 else next(p.device for p in lp.comports() if p.vid == 0x2886)
buf = deque(maxlen=N)   # (t, ax, ay, az, |a|, gx, gy, gz)


def reader():
    # Serial reading runs on its own thread so the plot redraws never drop samples.
    s = serial.Serial(port, 115200, timeout=1)
    while True:
        ln = s.readline().decode(errors="ignore").strip()
        if not ln or not ln[0].isdigit():
            continue   # '#' message lines
        try:
            t, ax, ay, az, gx, gy, gz = (float(v) for v in ln.split(","))
        except ValueError:
            continue   # half a line at startup
        buf.append((t / 1e6, ax, ay, az, (ax * ax + ay * ay + az * az) ** 0.5, gx, gy, gz))


threading.Thread(target=reader, daemon=True).start()

plt.style.use("dark_background")
fig, (top, bot) = plt.subplots(2, 1, sharex=True, figsize=(10, 6))
fig.canvas.manager.set_window_title(f"XIAO IMU live ({port})")
colors = ["#ff6b6b", "#51cf66", "#4dabf7"]
acc = [top.plot([], [], c, lw=1, label=k)[0] for c, k in zip(colors, ["ax", "ay", "az"])]
acc.append(top.plot([], [], "#dddddd", lw=1.5, label="|a|")[0])
gyr = [bot.plot([], [], c, lw=1, label=k)[0] for c, k in zip(colors, ["gx", "gy", "gz"])]
top.set_ylabel("accel (m/s²)")
bot.set_ylabel("gyro (deg/s)")
bot.set_xlabel("time (s)")
for ax in (top, bot):
    ax.grid(alpha=0.2)
    ax.legend(loc="upper left", ncol=4, fontsize=8)
readout = top.set_title("waiting for data...", fontsize=9, loc="right")


def autoscale(ax, lines, floor):
    # Fit the y range to what's on screen, but never tighter than +-floor.
    ys = [y for ln in lines for y in ln.get_ydata()]
    lo, hi = min(ys + [-floor]), max(ys + [floor])
    pad = 0.1 * (hi - lo)
    ax.set_ylim(lo - pad, hi + pad)


def update(_):
    if len(buf) < 2:
        return
    d = list(buf)
    t_end = d[-1][0]
    d = [r for r in d if r[0] > t_end - WINDOW_S]
    t = [r[0] - t_end for r in d]   # seconds before now
    for k, ln in enumerate(acc):
        ln.set_data(t, [r[1 + k] for r in d])
    for k, ln in enumerate(gyr):
        ln.set_data(t, [r[5 + k] for r in d])
    top.set_xlim(-WINDOW_S, 0)
    autoscale(top, acc, 12)
    autoscale(bot, gyr, 20)
    r = d[-1]
    readout.set_text(f"a {r[1]:+.2f} {r[2]:+.2f} {r[3]:+.2f}  |a| {r[4]:.2f}   "
                     f"gyro {r[5]:+.1f} {r[6]:+.1f} {r[7]:+.1f}")


anim = FuncAnimation(fig, update, interval=40, cache_frame_data=False)   # ~25 fps
plt.tight_layout()
plt.show()
