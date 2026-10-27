"""
RPE v1 (roadmap 5.4, analytics.md section 5): your own RIR-velocity line per exercise, fit on your
sets to failure and checked on sessions it never saw.

    python rpe.py data/session-....json [more.json]     # app exports (D30): fit, validate, predict
    python rpe.py --selftest                            # a synthetic lifter whose line is known

A calibration set is one taken to failure: "Your RPE" 10 in the app (or a toFailure mark, if a
set has one). Its last completed rep had 0 reps in reserve, so rep k of N had N - k: one set labels
every rep in it. The attempt that failed (safeties set, the bar lowered onto them) isn't a rep, and
a set with a rep much shorter than its others (a grinder that sank and split in two) isn't labeled:
either would shift every label by one.

Per exercise (its typed name, grouped like consistency.py and the LV profile), RIR = a + b x MCV by
least squares over the labeled reps is the predictor. Your MCV at RIR 0 and the MCV each rep in
reserve adds (v0's two numbers, D18) come from the same reps fit the other way, MCV on RIR: the
labels are exact and the noise is in MCV, so the predictor inverted would read them off (errors in
variables). Validation is leave one session out: fit on the other sessions, predict this one's
labeled reps, mean absolute error in reps, near failure (RIR 0 to 3) and further. Never a rep from
a set the fit saw: reps in one set are near copies, which flatters the model. A session is checked
only when the others alone give a line; the report names any that aren't.

A normal set: its last rep's MCV -> RIR -> RPE = 10 - RIR on the 5..10 scale you call on, a range
of +/- the held-out error (at least half a point; +/- 1 until there is one), next to your own call
when you typed one. One line across loads? Each load's mean residual within the sessions that took
2+ loads to failure: across days a load's residual is also that day's offset. Deadlift is low
confidence (section 5).
"""

import sys
from pathlib import Path

import numpy as np

import consistency as C
import sets as S

MIN_SETS = 2          # calibration sets per exercise before a line is fit
NEAR = 3              # "near failure": RIR this or less, where the research expects ~1 rep of error
FAIL_FRAC = 0.8       # a set to failure's last "rep" rising less than this x the rep before: the attempt that failed
SPLIT_FRAC = 0.8      # a set to failure with a rep under this x its median ROM isn't labeled: a split grinder


def is_calibration(meta):
    """A set taken to failure: a toFailure mark, or "Your RPE" 10."""
    if meta.get("toFailure"):
        return True
    try:
        return float(meta.get("rpe")) >= 10
    except (TypeError, ValueError):
        return False


def load_num(ld):
    """The load as a number, as the app's LV profile reads it (Number(load) > 0): "225" and "225.0"
    are one load; missing, empty or not a number is None."""
    try:
        v = float(ld)
    except (TypeError, ValueError):
        return None
    return v if np.isfinite(v) and v > 0 else None


def completed_reps(r):
    """A set to failure's reps, without the attempt that failed. With safeties set (section 5, step
    1) the set ends with the bar lowered onto them, not at lockout, so the leveling (which takes a
    barbell set's last still for a lockout, reps.py level_heights) lifts that attempt's top to
    lockout height and it reads as a full rep. On the plain heights (drift taken out between stills,
    no leveling) it rises short: it goes when it rose under FAIL_FRAC x the rep before and the bar
    came to rest below where the set started (a deadlift always ends on the floor)."""
    reps = S.closed_set_reps(r["reps"], r["lift"])
    if len(reps) < 2:
        return reps
    v, _ = C.R.velocity_between_anchors(r["a"], r["t"], r["fs"], r["runs"])
    x = C.R.integrate(v, r["t"])
    rise = [x[p["end"]] - x[p["start"]] for p in reps]
    low = r["lift"] == "deadlift" or x[r["runs"][-1][0]] - x[r["runs"][0][1]] < -0.5 * np.median(rise)
    if low and rise[-1] < FAIL_FRAC * rise[-2]:
        reps = reps[:-1]
    return reps


def collect(sessions):
    """Per exercise: the profile, calibration reps [(session id, set key, load, rir, mcv)], normal
    sets [(session id, meta, rep MCVs)] and sets to failure left unlabeled [(session id, meta, why)].
    Reps after the closed-set filter (D31)."""
    out = {}
    for rec, sets in sessions:
        for meta, r in sets:
            cal = is_calibration(meta)
            reps = completed_reps(r) if cal else S.closed_set_reps(r["reps"], r["lift"])
            if not reps:
                continue
            d = out.setdefault(C.exercise_key(meta, r), {"lift": r["lift"], "cal": [], "normal": [], "unlabeled": []})
            if not cal:
                d["normal"].append((rec["id"], meta, [p["mcv"] for p in reps]))
                continue
            roms = [p["rom"] for p in reps]
            if min(roms) < SPLIT_FRAC * float(np.median(roms)):
                d["unlabeled"].append((rec["id"], meta, "a rep much shorter than the others (a grinder that sank and split?)"))
                continue
            key, n = (rec["id"], meta.get("index", 0)), len(reps)
            d["cal"] += [(rec["id"], key, load_num(meta.get("load")), n - k, p["mcv"]) for k, p in enumerate(reps, 1)]
    return out


def fit(rows):
    """Over labeled reps [(.., rir, mcv)]: the predictor RIR = a + b x MCV, and the same reps fit
    the other way, MCV = v1rm + dv x RIR, to report. None with fewer than MIN_SETS sets, or when it
    doesn't slope up (a faster rep has more in reserve)."""
    if len({r[1] for r in rows}) < MIN_SETS:
        return None
    x = np.array([r[4] for r in rows], float)
    y = np.array([r[3] for r in rows], float)
    if np.ptp(x) <= 0 or np.ptp(y) <= 0:
        return None
    b, a = np.polyfit(x, y, 1)
    if b <= 0:
        return None
    dv, v1rm = np.polyfit(y, x, 1)
    return {"a": float(a), "b": float(b), "v1rm": float(v1rm), "dv": float(dv), "n": len(rows)}


def rir_of(line, mcv):
    return line["a"] + line["b"] * mcv


def validate(rows):
    """Leave one session out: (abs errors near failure, further, sessions not checked). Each labeled
    rep is predicted by a line fit without its session; a session whose removal leaves no line
    (under MIN_SETS sets, or no slope) can't be checked, and is returned so the report says so."""
    near, far, skipped = [], [], []
    for sid in dict.fromkeys(r[0] for r in rows):
        line = fit([r for r in rows if r[0] != sid])
        if line is None:
            skipped.append(sid)
            continue
        for r in rows:
            if r[0] == sid:
                (near if r[3] <= NEAR else far).append(abs(rir_of(line, r[4]) - r[3]))
    return near, far, skipped


def by_load(rows, line):
    """One line across loads ([3] in section 5)? Each load's mean residual (reps) within the sessions
    that took 2+ loads to failure, each session's own mean residual taken off first: a load done on
    one day alone would read that day's offset as a load effect. None when no session took 2+ known
    loads to failure (load and day can't be told apart yet)."""
    known = [(r, r[3] - rir_of(line, r[4])) for r in rows if r[2] is not None]
    day, loads = {}, {}
    for r, e in known:
        day.setdefault(r[0], []).append(e)
        loads.setdefault(r[0], set()).add(r[2])
    per = {}
    for r, e in known:
        if len(loads[r[0]]) >= 2:
            per.setdefault(r[2], []).append(e - np.mean(day[r[0]]))
    return {ld: float(np.mean(v)) for ld, v in per.items()} if len(per) >= 2 else None


def half(x):
    return round(x * 2) / 2


def rpe_range(line, mcv, err):
    """The set's RPE from its last rep, on the 5..10 scale you call on (v0's RIR >= 0), and its range
    +/- err (at least half a point), the ends to half points: (low, high, the estimate)."""
    rpe = min(10.0, max(5.0, 10 - rir_of(line, mcv)))
    w = max(0.5, err)
    clamp = lambda x: min(10.0, max(5.0, half(x)))
    return clamp(rpe - w), clamp(rpe + w), rpe


def report(sessions):
    """Prints the fit, its check and the predictions per exercise; returns them for the self-test."""
    out = {}
    for ex, d in collect(sessions).items():
        cal = d["cal"]
        sets_n, ses_n = len({r[1] for r in cal}), len({r[0] for r in cal})
        low = " (deadlift: low confidence)" if d["lift"] == "deadlift" else ""
        print(f"== {ex}{low}: {sets_n} set(s) to failure in {ses_n} session(s), {len(cal)} labeled reps")
        for sid, meta, why in d["unlabeled"]:
            print(f"   {sid} set {meta.get('index', 0) + 1}, to failure, not labeled: {why}")
        line = fit(cal)
        res = out[ex] = {"line": line, "near": [], "far": [], "skipped": [], "sets": [], "diffs": [], "loads": None,
                         "unlabeled": d["unlabeled"], "err": None}
        if line is None:
            print(f"   no line yet: needs {MIN_SETS}+ sets to failure (Your RPE 10) whose reps slow down")
            continue
        v0 = f"; v0 guesses {S.V1RM[d['lift']]:.2f} and {S.DV_PER_REP[d['lift']]:.3f}" if d["lift"] in S.V1RM else ""
        print(f"   your MCV at failure {line['v1rm']:.3f} m/s, {line['dv']:.3f} m/s per rep in reserve{v0}; "
              f"it predicts RIR = {line['a']:.2f} + {line['b']:.2f} x MCV")
        near, far, skipped = validate(cal)
        res.update(near=near, far=far, skipped=skipped)
        err = res["err"] = float(np.mean(near + far)) if near + far else None
        if err is None:
            print(f"   not checked yet: a session is checked on a line from the others alone, so it needs {MIN_SETS}+ "
                  f"sets to failure outside it (one set per session: {MIN_SETS + 1}+ sessions); ranges +/- 1 until then")
        else:
            nf = f"near failure (RIR 0-{NEAR}) {np.mean(near):.2f} (n {len(near)})" if near else "none near failure"
            ff = f", further {np.mean(far):.2f} (n {len(far)})" if far else ""
            print(f"   leave one session out: mean abs error {err:.2f} reps over {len(near + far)} of {len(cal)}; {nf}{ff}")
            if skipped:
                print(f"   not checked: {', '.join(map(str, skipped))} (the other sessions alone give no line)")
        bl = res["loads"] = by_load(cal, line)
        if bl:
            print("   one line across loads? mean residual by load, within a session: " +
                  ", ".join(f"{ld:g} lb {v:+.1f}" for ld, v in sorted(bl.items())))
        elif len({r[2] for r in cal if r[2] is not None}) >= 2:
            print("   one line across loads? can't tell yet: no session took 2+ loads to failure, so a load's residual "
                  "is also its day's offset")
        for sid, meta, mcvs in d["normal"]:
            lo, hi, mid = rpe_range(line, mcvs[-1], 1.0 if err is None else err)
            try:
                you = float(meta.get("rpe"))
            except (TypeError, ValueError):
                you = None
            res["sets"].append((sid, meta, lo, hi, mid))
            if you is not None:
                res["diffs"].append(mid - you)
            mine = f" (you: {you:g})" if you is not None else ""
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
    reps), session 2's ending with an attempt that failed onto the safeties, session 3 with a second
    one at another load; normal sets stopped 1, 2 and 4 reps short, called 0.5 RPE too high, and a
    warm-up called 5. The fit must find the line, the held-out error must be leave one session out
    exactly (recomputed here: a rep, a set or a half set the fit saw leaking in fails it), the true
    RPE must sit in most ranges, the calls' bias must show. A grinder that sank and split must not be
    labeled; one set to failure is no line; a session the others can't check is named. And the
    pieces on exact numbers: the reported pair against noisy MCV, a range's width, loads by number,
    one line across loads within a day only."""
    FS, G, ROM = 400, 9.81, 0.55
    V1, DV = 0.25, 0.045
    ease = lambda n: 0.5 - 0.5 * np.cos(np.pi * np.arange(n) / n)
    rng = np.random.default_rng(11)

    def squat_set(mcvs, failed=False, sag=0.0):
        """Reps at these MCVs from lockout. failed: then an attempt up 40% of the ROM, a stall, and
        the bar lowered onto safeties 3 cm under the bottom. sag: the last rep stalls at 0.25 m, sinks
        this much and grinds on to lockout."""
        z = [np.full(int(2 * FS), ROM)]
        for j, v in enumerate(mcvs):
            z.append(ROM - ROM * ease(int(1.1 * FS)))
            if sag and j == len(mcvs) - 1:
                lo = 0.25 - sag
                z += [0.25 * ease(int(0.25 / v * FS)), np.full(int(0.3 * FS), 0.25), 0.25 - sag * ease(int(0.4 * FS)),
                      lo + (ROM - lo) * ease(int((ROM - lo) / v * FS))]
            else:
                n = int(ROM / v * FS)                       # the concentric's duration for this MCV
                u = np.arange(n) / n
                vv = np.sin(np.pi * u)
                x = np.concatenate([[0.0], np.cumsum(vv)[:-1]])
                z.append(ROM * x / (x[-1] + vv[-1]))
            z.append(np.full(int(1.0 * FS), ROM))
        end = ROM
        if failed:
            z += [ROM - ROM * ease(int(1.1 * FS)), 0.4 * ROM * ease(int(0.8 * FS)), np.full(int(0.3 * FS), 0.4 * ROM),
                  0.4 * ROM - (0.4 * ROM + 0.03) * ease(int(0.6 * FS))]
            end = -0.03
        z.append(np.full(int(2 * FS), end))
        z = np.concatenate(z)
        t = np.arange(len(z)) / FS
        a = G + np.gradient(np.gradient(z, t), t) + 0.03 * rng.standard_normal(len(t))
        return C.R.analyze(t, a, "squat")

    def reps_to(rir_last, n, day):
        """n reps ending rir_last in reserve: rep k has rir_last + n - k."""
        return [V1 + DV * (rir_last + n - k) + day + rng.normal(0, 0.012) for k in range(1, n + 1)]

    sessions, truth, days = [], {}, []
    for s, (load, n_fail) in enumerate([(225, 12), (255, 9), (295, 5), (255, 9)]):
        day = rng.uniform(-0.015, 0.015)
        days.append(day)
        sid = f"s{s + 1}"
        sets = [({"index": 0, "exercise": "Squat", "load": str(load), "rpe": "10"}, squat_set(reps_to(0, n_fail, day), failed=s == 1))]
        for k, (rir, n) in enumerate([(2, 5), (4, 5)], 1):
            sets.append(({"index": k, "exercise": "Squat", "load": str(load), "rpe": str(10 - rir + 0.5)}, squat_set(reps_to(rir, n, day))))
            truth[(sid, k)] = 10 - rir
        sessions.append(({"id": sid, "startedAtMs": s}, sets))
    # session 3: a second set to failure at another load (sets and sessions differ; loads share a day),
    # a set stopped 1 short called 9.5, a warm-up called 5; session 4: a split grinder to failure
    sessions[2][1].append(({"index": 3, "exercise": "Squat", "load": "255.0", "rpe": "10"}, squat_set(reps_to(0, 9, days[2]))))
    sessions[2][1].append(({"index": 4, "exercise": "Squat", "load": "255", "rpe": "9.5"}, squat_set(reps_to(1, 4, days[2]))))
    truth[("s3", 4)] = 9
    sessions[0][1].append(({"index": 5, "exercise": "Squat", "load": "135", "rpe": "5"}, squat_set(reps_to(8, 5, days[0]))))
    truth[("s1", 5)] = 2
    sessions[3][1].append(({"index": 6, "exercise": "Squat", "load": "275", "rpe": "10"},
                           squat_set(reps_to(0, 4, days[3]), sag=0.06)))
    # another exercise with one set to failure: no line; and one whose session a the others can't check
    sessions[0][1].append(({"index": 7, "exercise": "Pause squat", "load": "185", "rpe": "10"}, squat_set(reps_to(0, 6, 0.0))))
    for sid, n in (("s1", 8), ("s1", 6), ("s2", 7)):
        rec = next(x for x in sessions if x[0]["id"] == sid)
        rec[1].append(({"index": 10 + n, "exercise": "Box squat", "load": str(150 + 10 * n), "rpe": "10"}, squat_set(reps_to(0, n, 0.0))))

    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        out = report(sessions)
    sq = out.get("squat", {})
    line = sq.get("line")
    ok = True

    def check(good, msg):
        nonlocal ok
        ok &= bool(good)
        print(f"{'PASS' if good else 'FAIL'}  {msg}")

    made = 12 + 9 + 5 + 9 + 9                                   # the reps of the sets to failure, the split grinder's aside
    check(line and abs(line["v1rm"] - V1) < 0.02 and abs(line["dv"] / DV - 1) < 0.15 and line["n"] == made,
          f"the line: MCV at failure {line['v1rm']:.3f} (true {V1}), {line['dv']:.4f} per rep (true {DV}), {line['n']} "
          f"labeled reps ({made} made; the failed attempt on the safeties isn't one)" if line else "the line: none")
    check(len(sq.get("unlabeled", [])) == 1 and sq["unlabeled"][0][0] == "s4",
          f"a set to failure whose grinder sank 6 cm and split isn't labeled: {[u[0] for u in sq.get('unlabeled', [])]}")
    if line:
        cal = collect(sessions)["squat"]["cal"]
        held = float(np.mean(sq["near"] + sq["far"]))
        own = float(np.mean([abs(rir_of(line, r[4]) - r[3]) for r in cal]))
        ref = []
        for s_out in dict.fromkeys(r[0] for r in cal):                # leave one session out, recomputed
            tr = [r for r in cal if r[0] != s_out]
            b, a = np.polyfit([r[4] for r in tr], [r[3] for r in tr], 1)
            ref += [abs(a + b * r[4] - r[3]) for r in cal if r[0] == s_out]
        same = len(ref) == len(sq["near"] + sq["far"]) and np.allclose(sorted(ref), sorted(sq["near"] + sq["far"]), atol=1e-9)
        check(same and not sq["skipped"] and held < 0.75 and held > own,
              f"leave one session out: {held:.2f} reps over {len(ref)} reps, the same as recomputed, under 0.75 and over "
              f"the fit's own {own:.2f}")
        inside = [lo <= truth[(sid, m['index'])] <= hi or (truth[(sid, m['index'])] < 5 and lo == 5)
                  for sid, m, lo, hi, _ in sq["sets"]]
        check(len(inside) == 10 and sum(inside) >= 8, f"normal sets: the true RPE inside the range in {sum(inside)} of {len(inside)}")
        calls = [float(m["rpe"]) for _, m, *_ in sq["sets"]]
        want = float(np.mean([min(10, max(5, truth[(sid, m["index"])])) - c for (sid, m, *_), c in zip(sq["sets"], calls)]))
        bias = float(np.mean(sq["diffs"])) if sq["diffs"] else float("nan")
        check(abs(bias - want) < 0.3, f"your calls, 0.5 too high and a warm-up called 5: the model minus you reads {bias:+.2f} "
              f"(the truth on the 5..10 scale minus you: {want:+.2f})")
        bl = sq["loads"]
        check(bl is not None and sorted(bl) == [255.0, 295.0] and max(abs(v) for v in bl.values()) < 0.4,
              f"one line across loads, within session 3 (255 and 295 lb, \"255.0\" read as 255): {bl}")
    other = out.get("pause squat", {})
    check(other and other["line"] is None, "one set to failure (Pause squat): no line")
    box = out.get("box squat", {})
    check(box and box["line"] is not None and box["skipped"] == ["s1"] and len(box["near"] + box["far"]) == 7,
          f"Box squat: s1 (2 sets) can't be checked by s2's one set alone, and is named: skipped {box.get('skipped')}, "
          f"{len(box.get('near', []) + box.get('far', []))} reps checked")

    # the pieces, on exact numbers
    r2 = np.random.default_rng(3)
    rows = [(f"d{s}", (f"d{s}", 0), 225.0, rir, V1 + DV * rir + r2.normal(0, 0.04)) for s in range(40) for rir in range(8)]
    ln = fit(rows)
    check(abs(ln["v1rm"] - V1) < 0.01 and abs(ln["dv"] - DV) < 0.003,
          f"the reported pair under 0.04 m/s of MCV noise: {ln['v1rm']:.3f} and {ln['dv']:.4f} (true {V1}, {DV}; "
          f"the predictor inverted reads {-ln['a'] / ln['b']:.3f} and {1 / ln['b']:.4f})")
    lo, hi, _ = rpe_range({"a": 0.0, "b": 1.0}, 2.74, 0.74)
    check((lo, hi) == (6.5, 8.0), f"a range is +/- the held-out error: RPE 7.26, error 0.74 -> {lo:g} to {hi:g} (6.5 to 8)")
    lo, hi, mid = rpe_range({"a": 0.0, "b": 1.0}, 7.0, 0.5)
    check((lo, hi, mid) == (5.0, 5.5, 5.0), f"a warm-up (RPE 3) on the 5..10 scale: {lo:g} to {hi:g}, estimate {mid:g}")
    check([load_num(x) for x in ("225", "225.0", 225, "", None, "0", "x")] == [225.0, 225.0, 225.0, None, None, None, None],
          "loads by number: \"225\", \"225.0\", 225 are one; empty, missing, 0 and text are none")
    flat = {"a": -V1 / DV, "b": 1 / DV}
    at = lambda sid, ld, off, eff: [(sid, (sid, ld), ld, rir, V1 + DV * rir + off - DV * eff) for rir in range(6)]
    days_only = at("p", 225.0, 0.02, 0) + at("q", 255.0, -0.02, 0) + at("r", 295.0, 0.0, 0)
    shared = at("p", 225.0, 0.02, 0.3) + at("p", 255.0, 0.02, -0.3) + at("q", 225.0, -0.02, 0.3)
    got_a, got_b = by_load(days_only, flat), by_load(shared, flat)
    check(got_a is None and got_b is not None and abs(got_b[225.0] - 0.3) < 0.01 and abs(got_b[255.0] + 0.3) < 0.01,
          f"one line across loads: day offsets alone (each load on its own day) say nothing ({got_a}); a real "
          f"+/-0.3-rep load effect within a day shows ({got_b})")
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
