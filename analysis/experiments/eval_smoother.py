"""Score reps.py (current) vs the RTS smoother on fakes with known answers and on real files."""
import sys, io, contextlib
from pathlib import Path
import numpy as np
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent)); sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent / "demos"))
import reps as R
import smoother as S
from phone_lift import load_phyphox
with contextlib.redirect_stdout(io.StringIO()):
    sys.argv = ["x"]
    ns = {}
    exec(open(HERE.parent / "make_fake_reps.py").read().split('if "lifts" in sys.argv:')[0], ns)
    import bench_jcurve as B
FS = 400

def ease(n): return 0.5 - 0.5 * np.cos(np.pi * np.arange(n) / n)

def phone_fake(seed, kind, size):
    """No-pause phone-style set (starts at the bottom, goes up first), drift of a given shape."""
    r = np.random.default_rng(seed); ups = np.linspace(0.8, 1.6, 6); rom = 0.40
    hs = lambda d, s: s * rom / d * np.pi / 2 * np.sin(np.pi * np.arange(int(d * FS)) / int(d * FS))
    v = np.concatenate([np.zeros(int(1.5 * FS))] + [np.concatenate([hs(u, 1), hs(1.0, -1)]) for u in ups] + [np.zeros(2 * FS)])
    t = np.arange(len(v)) / FS; a = np.gradient(v, t); tm = np.clip(t - 1.5, 0, None); T = t[-1] - 3.5
    drift = {"wobble": size * np.sin(2 * np.pi * t / 9 + r.uniform(0, 6)) * (t > 1.5),
             "ramp": size * np.clip(tm / T, 0, 1) ** 2,
             "bump": size * np.sin(np.pi * np.clip(tm / T, 0, 1))}[kind]
    return t, 9.81 + a + 0.04 + drift + 0.03 * r.standard_normal(len(t)), np.full(6, 40.0), rom / ups

def paused_squat(seed, wander):
    r = np.random.default_rng(seed); rom = 0.45; depth = r.normal(0, 0.015, 5); z = [np.full(int(1.5 * FS), rom)]; ups = []
    for k in range(5):
        u = r.uniform(0.8, 1.6); ups.append(u); d = rom - depth[k]
        z += [rom - d * ease(int(1.1 * FS)), depth[k] + d * ease(int(u * FS)), np.full(FS, rom)]
    z.append(np.full(2 * FS, rom)); z = np.concatenate(z); t = np.arange(len(z)) / FS
    a = 9.81 + np.gradient(np.gradient(z, t), t) + 0.03 + wander * np.sin(2 * np.pi * t / 7 + r.uniform(0, 6)) + 0.03 * r.standard_normal(len(t))
    return t, a, (rom - depth) * 100, (rom - depth) / np.array(ups)

def lift_fake(kind, seed, wander):
    df = ns["fake_lift"](kind, wander=wander, seed=seed); r = np.random.default_rng(seed)
    depth = r.normal(0, 0.015, 6) if kind == "squat" else np.zeros(6)
    ups = np.linspace(0.7, 1.4, 6) * (1.4 if kind == "deadlift" else 1.0)
    return df.iloc[:, 0].to_numpy(), df.iloc[:, 3].to_numpy(), (0.45 - depth) * 100, (0.45 - depth) / ups

def bench_fake(seed, **kw):
    t, az, ay, truth = B.bench(seed=seed, **kw); n = 0.02 * np.random.default_rng(seed).standard_normal((2, len(t)))
    return t, np.sqrt((9.81 + az + n[0]) ** 2 + (ay + n[1]) ** 2), np.array([tr[0] for tr in truth]) * 100, np.array([tr[1] for tr in truth])

def score(t, a, rom_true, mcv_true, lift, p=None):
    out = {}
    try:
        old = R.analyze(t, a, lift)["reps"]
    except ValueError:
        old = []
    try:
        new = S.analyze_rts(t, a, lift, p or S.P)["reps"]
    except ValueError:
        new = []
    for name, reps in (("old", old), ("new", new)):
        if len(reps) != len(rom_true):
            out[name] = (np.nan, np.nan, len(reps))
            continue
        rom = np.array([q["rom"] for q in reps]) * 100; mcv = np.array([q["mcv"] for q in reps])
        out[name] = (np.abs(rom - rom_true).max(), (np.abs(mcv - mcv_true) / mcv_true).max() * 100, len(reps))
    return out

def fake_suite(p=None, seeds=range(6)):
    cases = []
    for kind, size in [("wobble", 0.03), ("wobble", 0.1), ("ramp", 0.2), ("bump", 0.2)]:
        cases.append((f"phone no-pause, {kind} {size}", "phone", [phone_fake(s, kind, size) for s in seeds]))
    for kind in ["bench", "squat", "deadlift"]:
        for w in (0.02, 0.05):
            cases.append((f"{kind} no-pause, wobble {w}", kind, [lift_fake(kind, s, w) for s in seeds]))
    cases.append(("squat paused at top, wobble 0.05", "squat", [paused_squat(s, 0.05) for s in seeds]))
    cases.append(("bench J-curve + 2 m/s2 shake", "bench", [bench_fake(s + 1, shake=2.0) for s in seeds]))
    cases.append(("bench, 2.5 s grinder last", "bench", [bench_fake(s + 1, jcurve=0.15, slow_last=2.5) for s in seeds]))
    rows = []
    for name, lift, sets in cases:
        res = [score(t, a, rt, mt, lift, p) for t, a, rt, mt in sets]
        o = np.array([r["old"][:2] for r in res]); nn = np.array([r["new"][:2] for r in res])
        miss_o = sum(np.isnan(r["old"][0]) for r in res); miss_n = sum(np.isnan(r["new"][0]) for r in res)
        rows.append((name, np.nanmean(o[:, 0]), np.nanmean(nn[:, 0]), np.nanmean(o[:, 1]), np.nanmean(nn[:, 1]), miss_o, miss_n))
    return rows

if __name__ == "__main__":
    print(f"{'fake set (6 seeds each)':38s} {'worst ROM err cm':>18s}   {'worst MCV err %':>17s}  rep-count misses")
    print(f"{'':38s} {'now':>8s} {'RTS':>8s}   {'now':>8s} {'RTS':>8s}")
    for name, ro, rn, mo, mn, xo, xn in fake_suite():
        print(f"{name:38s} {ro:8.2f} {rn:8.2f}   {mo:8.2f} {mn:8.2f}   {xo}/{xn}")
    print("\nreal recordings (no truth): ROM spread (std, cm) and mean |MCV change|")
    D = HERE.parent / "data"
    for f in ["reps_pause.zip", "reps_nopause.zip", "bar-2026-10-16-1835.csv", "bar-2026-10-16-1844.csv", "bar-2026-10-16-1845.csv",
              "bar-2026-10-16-1920.csv", "bar-squat-2026-10-16-2131.csv", "bar-squat-2026-10-16-2134-browser.csv"]:
        lift = "squat" if ("18" in f or "19" in f or "squat" in f) and f.startswith("bar") else R.lift_from_name(f)
        t, a = load_phyphox(D / f, "magnitude")
        o = R.analyze(t, a, lift)["reps"]; n = S.analyze_rts(t, a, lift)["reps"]
        ro = np.array([q["rom"] for q in o]) * 100; rn = np.array([q["rom"] for q in n]) * 100
        dm = np.mean(np.abs(np.array([q["mcv"] for q in o]) - np.array([q["mcv"] for q in n]))) if len(o) == len(n) else np.nan
        print(f"  {f:38s} {lift:6s} reps {len(o)}/{len(n)}  spread now {ro.std(ddof=1):4.1f}  RTS {rn.std(ddof=1):4.1f}   |dMCV| {dm:.3f}")
