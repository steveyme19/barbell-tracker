"""
Make a FAKE phyphox-style recording of a set of phone "reps" with known ROM.
Known-answer test for reps.py.

Each rep: up (the concentric), optional pause at the top, back down.

Usage:
    python make_fake_reps.py            # 5 reps, 1 s pause at the top
    python make_fake_reps.py nopause    # 5 reps, no pause
    python make_fake_reps.py drift      # 5 reps, no pause, bias that creeps up during the set
    python make_fake_reps.py lifts      # no-pause bench, squat and deadlift sets, wobbly drift

Writes data/fake_reps_pause.csv, fake_reps_nopause.csv, fake_reps_drift.csv, or
fake_bench.csv + fake_squat.csv + fake_deadlift.csv
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPS = 5
ROM = 0.40                                  # m
UP_TIMES = [0.9, 1.0, 1.1, 1.3, 1.6]        # s, each rep slower (fatigue)
DOWN_TIME = 1.0                             # s
DRIFT = "drift" in sys.argv
PAUSE = 0.0 if ("nopause" in sys.argv or DRIFT) else 1.0
FS = 400                                    # Hz, like your phone
G = 9.81
BIAS = 0.05                                 # m/s^2 on z
BIAS_CREEP = 0.06 if DRIFT else 0.0         # m/s^2 extra bias by the end of the set
NOISE = 0.03                                # m/s^2
TILT_DEG = 6                                # hand wobble, comes and goes

rng = np.random.default_rng(2)


def fake_lift(kind, wander=0.05, seed=3, reps=6, rom=0.45):
    """A no-pause barbell set as a bar-mounted sensor would see it (vertical only).
    Bench and squat start and end at lockout and go down first; deadlift starts and
    ends on the floor. Squat depth varies a little rep to rep. The sensor bias wobbles
    (period 7 s), which is the case a smooth drift curve struggles with."""
    r = np.random.default_rng(seed)
    ups = np.linspace(0.7, 1.4, reps) * (1.4 if kind == "deadlift" else 1.0)
    ease = lambda n: 0.5 - 0.5 * np.cos(np.pi * np.arange(n) / n)
    top_first = kind != "deadlift"
    depth = r.normal(0, 0.015, reps) if kind == "squat" else np.zeros(reps)
    z = [np.full(int(1.5 * FS), rom if top_first else 0.0)]
    for k, u in enumerate(ups):
        lo, d = depth[k], rom - depth[k]
        if top_first:
            z += [rom - d * ease(int(1.1 * FS)), lo + d * ease(int(u * FS))]
        else:
            z += [lo + d * ease(int(u * FS)), rom - d * ease(int(1.1 * FS))]
    z.append(np.full(int(2 * FS), rom if top_first else 0.0))
    z = np.concatenate(z)
    tt = np.arange(len(z)) / FS
    az = np.gradient(np.gradient(z, tt), tt)
    sig = G + az + 0.03 + wander * np.sin(2 * np.pi * tt / 7 + r.uniform(0, 6)) + NOISE * r.standard_normal(len(tt))
    zeros = np.zeros_like(tt)
    return pd.DataFrame({"Time (s)": tt, "Acceleration x (m/s^2)": zeros, "Acceleration y (m/s^2)": zeros,
                         "Acceleration z (m/s^2)": sig, "Absolute acceleration (m/s^2)": sig})


if "lifts" in sys.argv:
    for kind in ("bench", "squat", "deadlift"):
        fake_lift(kind).to_csv(Path(__file__).parent / "data" / f"fake_{kind}.csv", index=False)
        print(f"Wrote data/fake_{kind}.csv")
    sys.exit()


def sticking_set(seed=5, rom=0.40):
    """A paused bench set for the sticking point (analytics.md section 4, roadmap 5.3): 2 light reps
    (a smooth press, no dip), then 3 heavy ones whose speed dips to 40% of the peak partway up and
    picks up again. Pauses on the chest and at lockout anchor every rep. Returns the recording and,
    per rep, the dip's true place as a fraction of the ROM (None for the light reps)."""
    r = np.random.default_rng(seed)
    ease = lambda n: 0.5 - 0.5 * np.cos(np.pi * np.arange(n) / n)
    z, truth = [np.full(int(1.5 * FS), rom)], []
    for k, (up, dip_at) in enumerate([(0.8, None), (0.9, None), (1.8, 0.45), (2.1, 0.45), (2.4, 0.5)]):
        z += [rom - rom * ease(int(1.1 * FS)), np.zeros(int(0.5 * FS))]          # down, pause on the chest
        n = int(up * FS)
        u = np.arange(n) / n
        v = np.sin(np.pi * u)
        if dip_at is not None:
            v = v * (1 - 0.6 * np.exp(-((u - dip_at) / 0.07) ** 2))
        x = np.concatenate([[0.0], np.cumsum(v)[:-1]])
        x *= rom / (x[-1] + v[-1])
        z += [x, np.full(int(1.0 * FS), rom)]                                     # up, pause at lockout
        if dip_at is None:
            truth.append(None)
        else:
            near = np.abs(u - dip_at) < 0.15                                      # the dip, not the ends
            i = np.flatnonzero(near)[np.argmin(v[near])]
            truth.append(float(x[i] / rom))
    z.append(np.full(int(2 * FS), rom))
    z = np.concatenate(z)
    tt = np.arange(len(z)) / FS
    az = np.gradient(np.gradient(z, tt), tt)
    sig = G + az + 0.03 + NOISE * r.standard_normal(len(tt))
    zeros = np.zeros_like(tt)
    df = pd.DataFrame({"Time (s)": tt, "Acceleration x (m/s^2)": zeros, "Acceleration y (m/s^2)": zeros,
                       "Acceleration z (m/s^2)": sig, "Absolute acceleration (m/s^2)": sig})
    return df, truth


if "sticking" in sys.argv:
    df, truth = sticking_set()
    out = Path(__file__).parent / "data" / "fake_bench_sticking.csv"
    df.to_csv(out, index=False)
    (Path(__file__).parent / "data" / "fake_bench_sticking_truth.json").write_text(__import__("json").dumps(truth))
    print(f"Wrote data/{out.name}: 5 paused bench reps, sticking dips at {[None if p is None else round(p, 3) for p in truth]} of the ROM")
    sys.exit()


def half_sine(dur, dist, sign):
    """Velocity for moving `dist` in `dur` seconds, smooth start and stop."""
    n = int(round(dur * FS))
    s = np.arange(n) / n
    return sign * dist / dur * np.pi / 2 * np.sin(np.pi * s)


# Velocity, phase by phase. Start and end resting.
parts = [np.zeros(int(1.5 * FS))]
true_mcv = []
for k in range(REPS):
    parts.append(half_sine(UP_TIMES[k], ROM, +1))
    true_mcv.append(ROM / UP_TIMES[k])
    if PAUSE:
        parts.append(np.zeros(int(PAUSE * FS)))
    parts.append(half_sine(DOWN_TIME, ROM, -1))
parts.append(np.zeros(int(2.0 * FS)))
v = np.concatenate(parts)
t = np.arange(len(v)) / FS
a = np.gradient(v, t)

# Sensor: hand tilt wobble splits gravity between z and y, plus bias and noise.
moving = np.convolve((np.abs(v) > 0).astype(float), np.ones(FS // 2) / (FS // 2), mode="same")
theta = np.radians(TILT_DEG) * np.sin(2 * np.pi * t / 2.3) * np.clip(moving, 0, 1)
f_vert = G + a
creep = BIAS_CREEP * np.clip((t - 1.5) / (t[-1] - 3.5), 0, 1) ** 2   # curved, not a straight line
az = f_vert * np.cos(theta) + BIAS + creep + NOISE * rng.standard_normal(len(t))
ay = f_vert * np.sin(theta) + NOISE * rng.standard_normal(len(t))
ax = NOISE * rng.standard_normal(len(t))

df = pd.DataFrame({
    "Time (s)": t,
    "Acceleration x (m/s^2)": ax,
    "Acceleration y (m/s^2)": ay,
    "Acceleration z (m/s^2)": az,
    "Absolute acceleration (m/s^2)": np.sqrt(ax**2 + ay**2 + az**2),
})
name = "fake_reps_drift.csv" if DRIFT else ("fake_reps_pause.csv" if PAUSE else "fake_reps_nopause.csv")
out = Path(__file__).parent / "data" / name
df.to_csv(out, index=False)
print(f"Wrote data/{name}: {REPS} reps, ROM {ROM * 100:.0f} cm, pause {PAUSE} s")
print("True MCV per rep:", " ".join(f"{m:.2f}" for m in true_mcv), "m/s")
