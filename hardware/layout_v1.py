"""Draws hardware/layout_v1.png (the v1 pod + adapter sketch). Run: python hardware/layout_v1.py"""
import numpy as np, matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, Circle, Polygon

INK, MUTED, CASE, BATT, PCB, MAG, STEEL, ACC, STRAP = "#1f2328", "#6b7280", "#dbe4ee", "#f2c46b", "#3f8f5a", "#9aa3ad", "#c9ced4", "#e86f00", "#4c78a8"
plt.rcParams.update({"font.size": 10, "font.family": "DejaVu Sans"})
fig, axs = plt.subplots(1, 3, figsize=(15.5, 6.2), gridspec_kw={"width_ratios": [1.2, 1.15, 1.0]})

def dim(ax, x0, y0, x1, y1, text, off=(0, 0), rot=0):
    ax.annotate("", (x0, y0), (x1, y1), arrowprops=dict(arrowstyle="<->", color=MUTED, lw=0.9))
    ax.text((x0 + x1) / 2 + off[0], (y0 + y1) / 2 + off[1], text, color=MUTED, ha="center", va="center", fontsize=9, rotation=rot)

def note(ax, xy, xytext, text, color=INK, ha="center"):
    ax.annotate(text, xy, xytext, fontsize=8.5, color=color, ha=ha, va="center",
                arrowprops=dict(arrowstyle="-", color=color, lw=0.8))

# ---------- 1: the pod, side section ----------
ax = axs[0]
W, H = 39, 18
ax.add_patch(Rectangle((0, 0), W, H - 2, fc=CASE, ec=INK, lw=1.2))
ax.add_patch(Rectangle((2, 2), W - 4, H - 4, fc="white", ec="none"))
ax.add_patch(Rectangle((0, H - 2), W, 2, fc=CASE, ec=INK, lw=1.2, hatch="//"))
ax.add_patch(Rectangle((3, 2.4), 33, 8, fc=BATT, ec=INK, lw=1))
ax.text(19.5, 6.4, "LiPo 802030", ha="center", va="center", fontsize=9)
ax.add_patch(Rectangle((2, 10.9), 21, 1.2, fc=PCB, ec=INK, lw=1))
ax.add_patch(Rectangle((2, 12.1), 7, 3.2, fc="#b9c0c7", ec=INK, lw=0.8))
ax.add_patch(Rectangle((14, 12.1), 2.5, 1.0, fc=ACC, ec=INK, lw=0.6))
ax.add_patch(Rectangle((27, 10.5), 5, 2.2, fc="#d9d9d9", ec=INK, lw=0.6))
for xi in (9.5, 29.5):
    ax.add_patch(Rectangle((xi - 2, 0), 4, 2.0, fc="#c8a24a", ec=INK, lw=0.6))
note(ax, (15.2, 12.8), (15, 27), "IMU (measure its spot on the board)", ACC)
note(ax, (8, 11.5), (-3, 23.5), "XIAO nRF52840 Sense\n21 × 17.5 mm", PCB)
note(ax, (5.5, 14.5), (-6, 15.5), "USB-C\n(slot in wall)", MUTED, ha="right")
note(ax, (29.5, 12.2), (44, 13), "JST-PH", MUTED, ha="left")
note(ax, (32, 17), (44, 20.5), "lid, M2 screws,\nX/Y/Z arrows embossed", MUTED, ha="left")
note(ax, (31, 6), (44, 6), "8 × 20 × 30 mm\n+ protection PCB", "#a07a1f", ha="left")
note(ax, (29.5, 1), (30, -5), "2 × M3 heat-set inserts, 20 mm apart:\nthe one interface every adapter screws to", INK)
dim(ax, 0, -10.5, W, -10.5, "≈ 39 × 27 mm footprint", off=(0, -2.2))
dim(ax, -1.5, 0, -1.5, H, "≈ 18", off=(-2.3, 0), rot=90)
ax.set_title("1. The pod (electronics), side section\nflat square faces, so it doubles as the calibration cube", fontsize=10.5, loc="left")
ax.set_xlim(-14, 60); ax.set_ylim(-16, 31); ax.set_aspect("equal"); ax.axis("off")

# ---------- 2: sleeve-end adapter ----------
ax = axs[1]
ax.add_patch(Rectangle((-42, -25), 42, 50, fc=STEEL, ec=INK, lw=1.2))
ax.text(-21, 0, "sleeve\nØ50 mm", ha="center", va="center", fontsize=9)
ax.plot([-48, 44], [0, 0], ls="-.", color=MUTED, lw=0.8); ax.text(44, -2.5, "bar axis", color=MUTED, fontsize=8, ha="right")
ax.add_patch(Rectangle((0, -25), 7, 50, fc=CASE, ec=INK, lw=1.2))
for yc in (-15, 15):
    ax.add_patch(Rectangle((0, yc - 8), 4.5, 16, fc=MAG, ec=INK, lw=0.8))
ax.add_patch(Rectangle((7, -19.5), 18, 39, fc=CASE, ec=INK, lw=1.2))
ax.text(16, -10, "pod", ha="center", va="center", rotation=90, fontsize=9)
ax.add_patch(Circle((19, 0), 1.4, fc=ACC, ec=INK, lw=0.6))
note(ax, (19, 0), (35, 13), "IMU centered\non the axis", ACC)
note(ax, (2, -18), (-12, -33), "pot magnets Ø16 (M3),\nrecessed in the puck", MUTED)
note(ax, (5.5, 24), (-12, 33), "printed puck Ø50 × 7", MUTED)
dim(ax, 0, -29, 25, -29, "", off=(0, 0)); ax.text(27, -29, "sticks out ≈ 25 mm", color=MUTED, fontsize=9, va="center")
ax.set_title("2. Adapter A: sleeve end, on the axis\nno lever arm. Needs a steel end face\n(or a slip-on cup). Check J-hook clearance.", fontsize=10.5, loc="left")
ax.set_xlim(-48, 50); ax.set_ylim(-40, 40); ax.set_aspect("equal"); ax.axis("off")

# ---------- 3: shaft adapter, end view ----------
ax = axs[2]
R = 14.25
ax.add_patch(Circle((0, 0), R, fc=STEEL, ec=INK, lw=1.2)); ax.text(0, -2, "shaft\nØ28–29", ha="center", va="center", fontsize=8.5)
vy = R * np.sqrt(2)                                   # 90° V-notch: vertex this far above the shaft center
yb, yt = 9.0, vy + 4.5                                # saddle bottom / top
half = vy - yb                                        # notch half-width at the saddle bottom
ax.add_patch(Polygon([(-21, yb), (-half, yb), (0, vy), (half, yb), (21, yb), (21, yt), (-21, yt)], closed=True, fc=CASE, ec=INK, lw=1.2))
ax.add_patch(Rectangle((-19.5, yt), 39, 18, fc=CASE, ec=INK, lw=1.2)); ax.text(9, yt + 9, "pod", ha="center", va="center", fontsize=9)
imu = (-4, yt + 12.5)
ax.add_patch(Circle(imu, 1.4, fc=ACC, ec=INK, lw=0.6))
ax.annotate("", (0, 0), imu, arrowprops=dict(arrowstyle="<->", color=ACC, lw=0.9))
ax.text(-21, yt + 2.5, "r ≈ 30–35 mm\nlever arm (§3)", color=ACC, fontsize=8.5, ha="center")
th = np.linspace(np.radians(-200), np.radians(20), 80)
ax.plot((R + 2) * np.cos(th), (R + 2) * np.sin(th), color=STRAP, lw=3)
ax.plot([(R + 2) * np.cos(th[0]), -19], [(R + 2) * np.sin(th[0]), yb + 1], color=STRAP, lw=3)
ax.plot([(R + 2) * np.cos(th[-1]), 19], [(R + 2) * np.sin(th[-1]), yb + 1], color=STRAP, lw=3)
note(ax, (12, -11), (16, -24), "strap / hose clamp\n+ TPU pad (no spinning)", STRAP)
note(ax, (half * 0.6, (vy + yb) / 2 + 1), (24, yb + 2), "90° V:\nlines up\nthe bar axis", MUTED, ha="left")
ax.set_title("3. Adapter B: shaft, inside the collar\nless whip, but off axis", fontsize=10.5, loc="left")
ax.set_xlim(-30, 36); ax.set_ylim(-30, 48); ax.set_aspect("equal"); ax.axis("off")

fig.suptitle("Bar Tracker hardware v1: one pod, two bar adapters  (sizes are first guesses until you measure the parts)", fontsize=12, x=0.01, ha="left")
fig.tight_layout(rect=(0, 0, 1, 0.94))
fig.savefig(__import__("pathlib").Path(__file__).with_suffix(".png"), dpi=140)
print("ok")
