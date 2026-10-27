"""
Make a FAKE whole-workout recording with known sets, reps and rests. Known-answer test
for sets.py (and the app's session mode).

Four squat sets of 5 reps. Around each set: unrack (bar up 6 cm off the hooks), a 2 s
walkout (the bar bobs ~1 cm with each step), a still at lockout, the reps, a still,
walk back in, rerack. Rests of 90, 120 and 150 s with plate-loading bumps in them,
and in the second rest an unrack + rerack with no reps (must not count as a set).
Set 3 has a 10 s breather at lockout after rep 3 (must stay one set).

Usage:
    python make_fake_session.py          # writes data/fake_session_squat.csv, prints the truth
    python make_fake_session.py joined   # real 402 Hz app sets joined by 60 s of real
                                         # still noise: data/fake_session_joined.csv
    python make_fake_session.py handled  # the same 4 squat sets, then 4 s after the last
                                         # rerack the phone is picked up off the bar (up 0.5 m)
                                         # and held while End is tapped: must stay 4 x 5 reps
                                         # (D31): data/fake_session_squat_handled.csv
"""

import sys

from pathlib import Path

import numpy as np
import pandas as pd

FS = 400                     # Hz
G = 9.81
ROM = 0.55                   # m, squat depth
RACK_LIFT = 0.06             # m, unrack: bar goes up this much off the hooks
UPS = [0.8, 0.85, 0.9, 1.0, 1.15]                # s per concentric, slowing with fatigue
RESTS = [90, 120, 150]       # s, from the last rep of a set to the first rep of the next
NOISE = 0.03                 # m/s^2

if "joined" in sys.argv:
    # Real sets from the app, each followed by 60 s of real still noise: 1 s from the
    # middle of the file's longest still period, tiled. (Not the file's last second:
    # that's often the hand reaching for Stop.)
    import reps as R
    data = Path(__file__).parent / "data"
    parts, t_end = [], 0.0
    for name in ["bar-squat-2026-10-16-2131.csv", "bar-phone-2026-10-16-2206.csv"]:
        df = pd.read_csv(data / name)
        fs = 1 / np.median(np.diff(df.iloc[:, 0]))
        t, a_raw = R.load_phyphox(data / name, R.AXIS)
        s, e = max(R.analyze(t, a_raw, R.lift_from_name(name))["runs"], key=lambda r: r[1] - r[0])
        mid = (s + e) // 2
        still = df.iloc[mid - int(fs) // 2:mid + int(fs) // 2]
        gap = pd.concat([still] * 60, ignore_index=True)
        chunk = pd.concat([df, gap], ignore_index=True)
        chunk.iloc[:, 0] = t_end + np.arange(len(chunk)) / fs
        t_end = chunk.iloc[-1, 0] + 1 / fs
        parts.append(chunk)
        print(f"  {name}: starts at {parts[-1].iloc[0, 0]:.1f} s")
    out = data / "fake_session_joined.csv"
    pd.concat(parts, ignore_index=True).to_csv(out, index=False)
    print(f"Wrote data/{out.name}: {t_end:.0f} s. Lifts: squat, phone")
    sys.exit()

r = np.random.default_rng(7)
ease = lambda n: 0.5 - 0.5 * np.cos(np.pi * np.arange(n) / n)
z, truth = [], []            # height pieces; truth per set


def hold(sec, h):
    z.append(np.full(int(sec * FS), h))


def move(sec, h0, h1):
    z.append(h0 + (h1 - h0) * ease(int(sec * FS)))


def walk(sec, h):
    n = int(sec * FS)
    tt = np.arange(n) / FS
    z.append(h + 0.01 * np.sin(2 * np.pi * 1.5 * tt) * np.sin(np.pi * tt / sec))   # steps, fade in/out


def bumps(h):
    """Sliding a plate on: two quick jolts, no net height change."""
    for _ in range(2):
        n = int(0.15 * FS)
        z.append(h + 0.003 * np.sin(2 * np.pi * np.arange(n) / n))
        hold(0.6, h)


def now():
    return sum(len(p) for p in z) / FS


hold(5, 0)                                   # bar on the hooks, session starts
for k in range(4):
    top = RACK_LIFT
    move(0.6, 0, top)                        # unrack
    walk(2.0, top)                           # walkout
    hold(1.5, top)                           # settle at lockout
    reps = []
    for j, up in enumerate(UPS):
        move(1.1, top, top - ROM)            # down
        t0 = now()
        move(up, top - ROM, top)             # up: the concentric
        reps.append((t0, now()))
        hold(0.25, top)
        if k == 2 and j == 2:
            hold(10, top)                    # breather inside the set
    hold(1.0, top)
    walk(2.0, top)                           # walk back in
    move(0.5, top, 0)                        # rerack
    truth.append(reps)
    if k < 3:
        # rest: from the last rep to the next set's first rep is RESTS[k]
        spent = now() - reps[-1][1]
        bumps(0)                             # change plates
        if k == 1:                           # unrack, step, rerack, no reps: not a set
            hold(20, 0); move(0.6, 0, RACK_LIFT); walk(1.5, RACK_LIFT); move(0.5, RACK_LIFT, 0); hold(20, 0)
        lead = 0.6 + 2.0 + 1.5 + 1.1         # unrack + walkout + settle + first descent
        hold(RESTS[k] - spent - (now() - reps[-1][1] - spent) - lead, 0)
if "handled" in sys.argv:
    hold(4, 0)                               # racked, then picked up before the set closes
    move(0.6, 0, 0.5)                        # off the bar to look at it
    walk(3.0, 0.5)                           # held in the hand (End tapped while it moves)
else:
    hold(8, 0)                               # end of session

zz = np.concatenate(z)
t = np.arange(len(zz)) / FS
az = np.gradient(np.gradient(zz, t), t)
wander = 0.04 * np.sin(2 * np.pi * t / 47) + 0.02 * np.sin(2 * np.pi * t / 7.3)    # slow bias drift
sig = G + az + 0.03 + wander + NOISE * r.standard_normal(len(t))
zeros = np.zeros_like(t)
out = Path(__file__).parent / "data" / ("fake_session_squat_handled.csv" if "handled" in sys.argv else "fake_session_squat.csv")
pd.DataFrame({"Time (s)": t, "Acceleration x (m/s^2)": zeros, "Acceleration y (m/s^2)": zeros,
              "Acceleration z (m/s^2)": sig, "Absolute acceleration (m/s^2)": sig}).to_csv(out, index=False)

print(f"Wrote data/{out.name}: {t[-1]:.0f} s, 4 sets x 5 squat reps, ROM {ROM * 100:.0f} cm")
for k, reps in enumerate(truth, 1):
    rest = f"rest before {reps[0][0] - truth[k - 2][-1][1]:.1f} s" if k > 1 else ""
    print(f"  set {k}: first rep at {reps[0][0]:.2f} s, last rep ends {reps[-1][1]:.2f} s {rest}")
print("True MCV per rep:", " ".join(f"{ROM / u:.2f}" for u in UPS), "m/s")
