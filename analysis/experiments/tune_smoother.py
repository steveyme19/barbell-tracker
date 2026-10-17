import sys, itertools, numpy as np
from pathlib import Path
HERE = Path(__file__).resolve().parent; sys.path.insert(0, str(HERE))
import eval_smoother as E, smoother as S, reps as R
from phone_lift import load_phyphox
seeds = range(4)
cases = []
for kind, size in [("wobble", 0.03), ("wobble", 0.1), ("ramp", 0.2)]:
    cases += [("phone", E.phone_fake(s, kind, size)) for s in seeds]
for kind in ["bench", "squat", "deadlift"]:
    cases += [(kind, E.lift_fake(kind, s, 0.05)) for s in seeds]
cases += [("squat", E.paused_squat(s, 0.05)) for s in seeds]
shake = [("bench", E.bench_fake(s + 1, shake=2.0)) for s in seeds]
real = []
for f in ["bar-2026-10-16-1920.csv", "bar-squat-2026-10-16-2131.csv", "bar-squat-2026-10-16-2134-browser.csv", "bar-2026-10-16-1835.csv"]:
    real.append(load_phyphox(E.HERE.parent / "data" / f, "magnitude"))
def worst(lift, d, p):
    t, a, rt, mt = d
    try: reps = S.analyze_rts(t, a, lift, p)["reps"]
    except ValueError: return np.nan
    if len(reps) != len(rt): return np.nan
    return np.abs(np.array([q["rom"] for q in reps]) * 100 - rt).max()
print("q_a     q_b     h_sd   | fakes worst-ROM mean | shake | real spreads 1920 2131 2134 1835")
for q_a, q_b, h_sd in itertools.product([5e-5, 2e-4, 1e-3], [3e-4, 1e-3, 3e-3, 1e-2], [0.005, 0.01, 0.02]):
    p = dict(S.P, q_a=q_a, q_b=q_b, h_sd=h_sd)
    fk = np.nanmean([worst(l, d, p) for l, d in cases]); sh = np.nanmean([worst(l, d, p) for l, d in shake])
    sp = []
    for t, a in real:
        reps = S.analyze_rts(t, a, "squat", p)["reps"]; sp.append(np.std([q["rom"] for q in reps], ddof=1) * 100)
    print(f"{q_a:.0e}  {q_b:.0e}  {h_sd:.3f}  |  {fk:6.2f}               | {sh:5.2f} | {'  '.join(f'{x:4.1f}' for x in sp)}", flush=True)
