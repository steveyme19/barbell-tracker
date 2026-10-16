"""
Bench J-curve: how much does horizontal bar motion hurt the vertical numbers?

Fake bench sets (bar comes down and toward the belly, presses back up and back),
run through the real rep pipeline two ways:
  - the magnitude trick the phone uses: sqrt(vertical^2 + horizontal^2)
  - orientation tracking (what the bar device will do) with 1 or 3 degrees of error

Usage (from the analysis folder):
    python demos/bench_jcurve.py
    python demos/bench_jcurve.py --save   # also write data/fake_bench_grinder.csv for the parity test
"""
import sys
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import reps as R
FS, G = 400, 9.81

def smooth(n):           # 0 -> 1 with zero velocity at both ends (cosine ramp)
    s = np.arange(n) / n; return 0.5 - 0.5 * np.cos(np.pi * s)

def bench(reps=5, rom=0.40, jcurve=0.10, down=1.2, up=1.0, slow_last=None, pause_chest=0.0, shake=0.0, seed=1):
    """Vertical and horizontal bar position. Bar comes down and forward (toward the belly), goes back up and back."""
    rng = np.random.default_rng(seed)
    z = [np.full(int(1.5 * FS), rom)]; y = [np.zeros(int(1.5 * FS))]; truth = []
    for k in range(reps):
        u = slow_last if (slow_last and k == reps - 1) else up
        nd, nu = int(down * FS), int(u * FS)
        z.append(rom - rom * smooth(nd)); y.append(jcurve * smooth(nd))
        if pause_chest: z.append(np.zeros(int(pause_chest * FS))); y.append(np.full(int(pause_chest * FS), jcurve))
        z.append(rom * smooth(nu)); y.append(jcurve - jcurve * smooth(nu)); truth.append((rom, rom / u))
    z.append(np.full(int(2 * FS), rom)); y.append(np.zeros(int(2 * FS)))
    z, y = np.concatenate(z), np.concatenate(y); t = np.arange(len(z)) / FS
    az, ay = np.gradient(np.gradient(z, t), t), np.gradient(np.gradient(y, t), t)
    if shake:   # sideways jitter (hand tremor / bar rattle), only while moving
        moving = np.abs(np.gradient(z, t)) > 0.01
        ay = ay + shake * np.sqrt(2) * np.sin(2 * np.pi * 8 * t + rng.uniform(0, 6)) * moving
    return t, az, ay, truth

def run(t, signal, truth):
    t0 = t - t[0]; fs = 1 / np.median(np.diff(t0)); a_raw = signal
    g = np.median(a_raw[t0 < 0.3]); runs = R.find_still_runs(a_raw, g, fs)
    g = np.mean(np.concatenate([a_raw[s:e+1] for s, e in runs])); runs = R.find_still_runs(a_raw, g, fs)
    a = a_raw - g; runs = R.confirm_still_runs(a, t0, fs, runs)
    v, w = R.velocity_between_anchors(a, t0, fs, runs); v = R.level_heights(a, t0, v, w, "bench")
    x = R.integrate(v, t0); reps = R.find_reps(x, v, t0, w)
    if len(reps) != len(truth): return f"{len(reps)} reps found (expected {len(truth)})"
    rom_err = max(abs(r['rom'] - tr[0]) for r, tr in zip(reps, truth)) * 1000
    mcv_err = max(abs(r['mcv'] - tr[1]) / tr[1] for r, tr in zip(reps, truth)) * 100
    return f"worst ROM error {rom_err:5.1f} mm, worst MCV error {mcv_err:4.1f}%"

rng = np.random.default_rng(0)
cases = [
    ("touch-and-go, 10 cm J-curve", dict()),
    ("paused on chest, 10 cm J-curve", dict(pause_chest=0.6)),
    ("15 cm J-curve, last rep a 2.5 s grinder", dict(jcurve=0.15, slow_last=2.5)),
    ("10 cm J + 1 m/s^2 sideways shake", dict(shake=1.0)),
    ("10 cm J + 2 m/s^2 sideways shake", dict(shake=2.0)),
]
for name, kw in cases:
    t, az, ay, truth = bench(**kw)
    noise = 0.02 * rng.standard_normal((2, len(t)))
    fz, fy = G + az + noise[0], ay + noise[1]          # specific force in the bar's vertical / horizontal
    print(name)
    print(f"   magnitude trick        : {run(t, np.sqrt(fz**2 + fy**2), truth)}")
    for deg in (1, 3):
        th = np.radians(deg)                             # orientation filter off by this much, in the plane of the J
        print(f"   orientation, {deg} deg off: {run(t, fz * np.cos(th) + fy * np.sin(th), truth)}")

if "--save" in sys.argv:
    import pandas as pd
    t, az, ay, _ = bench(jcurve=0.15, slow_last=2.5)
    n = 0.02 * np.random.default_rng(0).standard_normal((2, len(t)))
    fz, fy = G + az + n[0], ay + n[1]
    out = Path(__file__).resolve().parent.parent / "data" / "fake_bench_grinder.csv"
    pd.DataFrame({"Time (s)": t, "Acceleration x (m/s^2)": 0 * t, "Acceleration y (m/s^2)": fy,
                  "Acceleration z (m/s^2)": fz, "Absolute acceleration (m/s^2)": np.sqrt(fy**2 + fz**2)}).to_csv(out, index=False)
    print(f"Wrote {out.name}")
