"""
RPE v1 (roadmap 5.4, analytics.md section 5): your own RIR-velocity line per exercise, fit on your
sets to failure and checked on sessions it never saw.

    python rpe.py data/session-....json [more.json]     # app exports (D30): fit, validate, predict
    python rpe.py --selftest                            # a synthetic lifter whose line is known

A calibration set is one taken to failure: "Your RPE" 10 in the app (or a toFailure mark, if a
set has one). Its last rep had 0 reps in reserve, so rep k of N had N - k: one set labels every
rep in it. Per exercise (its typed name, grouped like consistency.py and the LV profile):
RIR = a + b x MCV by least squares over the labeled reps, reported the way v0 (D18) is set: your
MCV at RIR 0 and the MCV each rep in reserve adds. Validation is leave one session out: fit on the
other sessions, predict this one's labeled reps, mean absolute error in reps, near failure (RIR 0
to 3) and further. Never a rep from a set the fit saw: reps in one set are near copies, which
flatters the model. A normal set: its last rep's MCV -> RIR -> RPE = 10 - RIR, a range of +/- the
validation error (at least half a point), clamped to 5..10, next to your own call when you typed
one. Deadlift is low confidence (analytics.md section 5).
"""

import sys
from pathlib import Path

import numpy as np

import consistency as C
import sets as S

MIN_SETS = 2          # calibration sets per exercise before a line is fit
NEAR = 3              # "near failure": RIR this or less, where the research expects ~1 rep of error


def is_calibration(meta):
    """A set taken to failure: a toFailure mark, or "Your RPE" 10."""
    if meta.get("toFailure"):
        return True
    try:
        return float(meta.get("rpe")) >= 10
    except (TypeError, ValueError):
        return False


def collect(sessions):
    """Per exercise: the profile, calibration reps [(session id, set key, load, rir, mcv)] and
    normal sets [(session id, meta, rep MCVs)]. Reps after the closed-set filter (D31)."""
    out = {}
    for rec, sets in sessions:
        for meta, r in sets:
            mcvs = [rep["mcv"] for rep in S.closed_set_reps(r["reps"], r["lift"])]
            if not mcvs:
                continue
            d = out.setdefault(C.exercise_key(meta, r), {"lift": r["lift"], "cal": [], "normal": []})
            if is_calibration(meta):
                key = (rec["id"], meta.get("index", 0))
                d["cal"] += [(rec["id"], key, meta.get("load"), len(mcvs) - k, v) for k, v in enumerate(mcvs, 1)]
            else:
                d["normal"].append((rec["id"], meta, mcvs))
    return out


def fit(rows):
    """RIR = a + b x MCV over labeled reps [(.., rir, mcv)]. None with fewer than MIN_SETS sets, or
    when it doesn't slope up (a faster rep has more in reserve)."""
    if len({r[1] for r in rows}) < MIN_SETS:
        return None
    x = np.array([r[4] for r in rows], float)
    y = np.array([r[3] for r in rows], float)
    if np.ptp(x) <= 0:
        return None
    b, a = np.polyfit(x, y, 1)
    if b <= 0:
        return None
    return {"a": float(a), "b": float(b), "v1rm": float(-a / b), "dv": float(1 / b), "n": len(rows)}


def rir_of(line, mcv):
    return line["a"] + line["b"] * mcv


def validate(rows):
    """Leave one session out: the absolute errors (near failure, further) of every labeled rep,
    each predicted by a line fit without its session. A session whose removal leaves too little
    to fit is skipped."""
    near, far = [], []
    for sid in dict.fromkeys(r[0] for r in rows):
        line = fit([r for r in rows if r[0] != sid])
        if line is None:
            continue
        for r in rows:
            if r[0] == sid:
                (near if r[3] <= NEAR else far).append(abs(rir_of(line, r[4]) - r[3]))
    return near, far


def by_load(rows, line):
    """The fit's mean residual (reps) per load: does one line work across loads ([3] in section 5)?"""
    per = {}
    for r in rows:
        per.setdefault(r[2], []).append(r[3] - rir_of(line, r[4]))
    return {ld: float(np.mean(v)) for ld, v in per.items()}


def half(x):
    return round(x * 2) / 2


def rpe_range(line, mcv, err):
    """The set's RPE from its last rep, (low, high) to half points: +/- err, at least half a point,
    clamped to 5..10 (v0's range)."""
    rpe = 10 - rir_of(line, mcv)
    w = max(0.5, half(err))
    clamp = lambda x: min(10.0, max(5.0, half(x)))
    return clamp(rpe - w), clamp(rpe + w), rpe


def load_key(ld):
    try:
        return float(ld)
    except (TypeError, ValueError):
        return float("inf")


def report(sessions):
    """Prints the fit, its validation and the predictions per exercise; returns them for the self-test."""
    out = {}
    for ex, d in collect(sessions).items():
        cal = d["cal"]
        sets_n, ses_n = len({r[1] for r in cal}), len({r[0] for r in cal})
        low = " (deadlift: low confidence)" if d["lift"] == "deadlift" else ""
        print(f"== {ex}{low}: {sets_n} set(s) to failure in {ses_n} session(s), {len(cal)} labeled reps")
        line = fit(cal)
        res = out[ex] = {"line": line, "near": [], "far": [], "sets": [], "diffs": []}
        if line is None:
            print(f"   no line yet: needs {MIN_SETS}+ sets to failure (Your RPE 10) whose reps slow down")
            continue
        v0 = f"; v0 guesses {S.V1RM[d['lift']]:.2f} and {S.DV_PER_REP[d['lift']]:.3f}" if d["lift"] in S.V1RM else ""
        print(f"   RIR = (MCV - {line['v1rm']:.3f}) / {line['dv']:.3f}: your MCV at failure {line['v1rm']:.3f} m/s, "
              f"{line['dv']:.3f} m/s per rep in reserve{v0}")
        near, far = res["near"], res["far"] = validate(cal)
        err = float(np.mean(near + far)) if near + far else None
        if err is None:
            print("   not validated: needs sets to failure in 2+ sessions (a line is never checked on its own sets)")
        else:
            nf = f"near failure (RIR 0-{NEAR}) {np.mean(near):.2f} (n {len(near)})" if near else "none near failure"
            ff = f", further {np.mean(far):.2f} (n {len(far)})" if far else ""
            print(f"   leave one session out: mean abs error {err:.2f} reps; {nf}{ff}")
        bl = by_load(cal, line)
        if len(bl) >= 2:
            print("   one line across loads? mean residual by load: " +
                  ", ".join(f"{ld if ld not in (None, '') else '?'} lb {v:+.1f}" for ld, v in sorted(bl.items(), key=lambda x: load_key(x[0]))))
        for sid, meta, mcvs in d["normal"]:
            lo, hi, mid = rpe_range(line, mcvs[-1], 1.0 if err is None else err)
            try:
                you = float(meta.get("rpe"))
            except (TypeError, ValueError):
                you = None
            res["sets"].append((sid, meta, lo, hi, mid))
            mine = f" (you: {you:g})" if you is not None else ""
            if you is not None:
                res["diffs"].append(mid - you)
            print(f"   {sid} set {meta.get('index', 0) + 1} ({meta.get('load') or '?'} lb, {len(mcvs)} reps): last rep "
                  f"{mcvs[-1]:.2f} m/s -> RPE {lo:g} to {hi:g}{mine}")
        if res["diffs"]:
            print(f"   your RPE calls against the model: {len(res['diffs'])} set(s), the model minus you "
                  f"{np.mean(res['diffs']):+.1f} on average")
    return out


def selftest():
    """A synthetic squatter with a known line, MCV = 0.25 + 0.045 x RIR m/s, a day-to-day offset per
    session (+/-0.015 m/s) and rep noise (0.012 m/s), through the whole pipeline (raw signal ->
    analyze -> closed-set filter). 4 sessions, each one set to failure (65/75/85/75%: 12, 9, 5, 9
    reps) and two normal sets stopped 2 and 4 reps short, called 0.5 RPE too high. The fit must find
    the line, the held-out error must stay near the noise and can't beat the fit's own residuals
    (it would if a set the fit saw leaked into its check), the true RPE must sit in most ranges, the
    calls' bias must show. And one set to failure is no line."""
    FS, G, ROM = 400, 9.81, 0.55
    V1, DV = 0.25, 0.045
    ease = lambda n: 0.5 - 0.5 * np.cos(np.pi * np.arange(n) / n)
    rng = np.random.default_rng(11)

    def squat_set(mcvs):
        z = [np.full(int(2 * FS), ROM)]
        for v in mcvs:
            n = int(ROM / v * FS)                       # the concentric's duration for this MCV
            u = np.arange(n) / n
            vv = np.sin(np.pi * u)
            x = np.concatenate([[0.0], np.cumsum(vv)[:-1]])
            z += [ROM - ROM * ease(int(1.1 * FS)), ROM * x / (x[-1] + vv[-1]), np.full(int(1.0 * FS), ROM)]
        z.append(np.full(int(2 * FS), ROM))
        z = np.concatenate(z)
        t = np.arange(len(z)) / FS
        a = G + np.gradient(np.gradient(z, t), t) + 0.03 * rng.standard_normal(len(t))
        return C.R.analyze(t, a, "squat")

    def reps_to(rir_last, n, day):
        """n reps ending rir_last in reserve: rep k has rir_last + n - k."""
        return [V1 + DV * (rir_last + n - k) + day + rng.normal(0, 0.012) for k in range(1, n + 1)]

    sessions, truth = [], {}
    for s, (load, n_fail) in enumerate([(225, 12), (255, 9), (295, 5), (255, 9)]):
        day = rng.uniform(-0.015, 0.015)
        sid = f"s{s + 1}"
        sets = [({"index": 0, "exercise": "Squat", "load": load, "rpe": "10"}, squat_set(reps_to(0, n_fail, day)))]
        for k, (rir, n) in enumerate([(2, 5), (4, 5)], 1):
            sets.append(({"index": k, "exercise": "Squat", "load": load, "rpe": str(10 - rir + 0.5)}, squat_set(reps_to(rir, n, day))))
            truth[(sid, k)] = 10 - rir
        sessions.append(({"id": sid, "startedAtMs": s}, sets))
    # a second exercise with one set to failure: no line
    sessions[0][1].append(({"index": 3, "exercise": "Pause squat", "load": 185, "rpe": "10"}, squat_set(reps_to(0, 6, 0.0))))

    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        out = report(sessions)
    sq, line = out.get("squat", {}), out.get("squat", {}).get("line")
    ok = True

    def check(good, msg):
        nonlocal ok
        ok &= bool(good)
        print(f"{'PASS' if good else 'FAIL'}  {msg}")

    check(line and abs(line["v1rm"] - V1) < 0.02 and abs(line["dv"] / DV - 1) < 0.15 and line["n"] == 35,
          f"the line: MCV at failure {line['v1rm']:.3f} (true {V1}), {line['dv']:.4f} per rep (true {DV}), "
          f"{line['n']} labeled reps (35 made)" if line else "the line: none")
    if line:
        held = float(np.mean(sq["near"] + sq["far"]))
        cal = collect(sessions)["squat"]["cal"]
        own = float(np.mean([abs(rir_of(line, r[4]) - r[3]) for r in cal]))
        check(len(sq["near"] + sq["far"]) == 35 and held < 0.75 and held > own,
              f"leave one session out: {held:.2f} reps over {len(sq['near'] + sq['far'])} reps, under 0.75 and over the "
              f"fit's own {own:.2f} (an in-sample check would read {own:.2f})")
        inside = [lo <= truth[(sid, m['index'])] <= hi for sid, m, lo, hi, _ in sq["sets"]]
        check(len(inside) == 8 and sum(inside) >= 6, f"normal sets: the true RPE inside the range in {sum(inside)} of {len(inside)}")
        bias = float(np.mean(sq["diffs"])) if sq["diffs"] else float("nan")
        check(abs(bias + 0.5) < 0.35, f"your calls 0.5 too high: the model minus you reads {bias:+.2f}")
    other = out.get("pause squat", {})
    check(other and other["line"] is None, "one set to failure (Pause squat): no line")
    print("PASS" if ok else "FAIL")
    return ok


def main():
    sys.stdout.reconfigure(errors="replace")      # typed exercise names on a cp1252 console
    if "--selftest" in sys.argv:
        sys.exit(0 if selftest() else 1)
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args or not all(a.endswith(".json") for a in args):
        sys.exit(__doc__)
    report(C.sessions_from_exports([Path(a) for a in args]))


if __name__ == "__main__":
    main()
