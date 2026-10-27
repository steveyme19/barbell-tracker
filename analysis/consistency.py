"""
Per-rep shape features and consistency (roadmap 5.2, analytics.md sections 1 and 2): how each rep
compares with your own usual rep, in real units, speed kept out of it (velocity loss is its own number).

    python consistency.py data/bar-squat-....csv                # one set: each rep against the set's median rep
    python consistency.py data/session-....json [more.json]     # app exports (D30): rep to rep, set to set,
                                                                # and session to session per exercise
    python consistency.py ... --floor-depth=1.0 --floor-tempo=5 --floor-profile=0.05   # the noise floor (3.5)
    python consistency.py --selftest                            # deliberate faults must stand out

Features per rep (section 1): ROM, the concentric's duration, the velocity profile (v at every 5% of
the concentric's height, divided by its MCV: shape, not speed) and the sticking point (D34).
Deviations (section 2): depth = ROM minus the reference's (cm), tempo = duration against the
reference's (%), profile = RMS difference of the normalized profiles. The reference: the set's
median rep (rep to rep), the session's median rep of that exercise (set to set), the median of the
last 4 earlier sessions of that exercise within 10% of the load (session to session). An exercise
is its typed name (as the app's LV profile matches it), not the tracking profile.

Until the Smith-machine noise floor exists (roadmap 3.5) there's nothing to hold a deviation
against: everything is shown, marked "no floor". With --floor-*, only deviations over twice the
floor are flagged (section 2's start).
"""

import sys
from pathlib import Path

import numpy as np

import reps as R

STEPS = np.linspace(0, 1, 21)          # every 5% of the concentric's height
LOAD_BAND = 0.10                       # session to session: sets within this of the same load
BASELINE = 4                           # ... over the last this many sessions of the lift


def profile(x, v, rep):
    """v at every 5% of the concentric's height, over its MCV. Height made monotone first (a
    running max), so the small wobbles near the ends can't fold the curve back."""
    s, e = rep["start"], rep["end"]
    h = np.maximum.accumulate(x[s:e + 1] - x[s])
    if h[-1] <= 0:
        return None
    frac = h / h[-1]
    keep = np.concatenate([[True], np.diff(frac) > 0])     # strictly increasing for interp
    return np.interp(STEPS, frac[keep], v[s:e + 1][keep]) / rep["mcv"]


ECC_STOP = 0.03                        # the eccentric ends where its speed is under this x its peak (D32's idea)


def eccentric(v, x, t, rep, before, after, lowers_first):
    """The lowering's duration: in the gap before the concentric when the lift lowers first (squat,
    bench), after it when it rises first (deadlift, a stack). The gap splits into runs of moving
    down (faster than ECC_STOP x its peak downward speed); the lowering is the one nearest the rep
    that takes the bar down at least half the rep's ROM, else the one holding the peak. Drift in a
    pause at the top, a wobble in a bottom pause or box sit, a settle at lockout, a walkout's bobs
    and the pod handled after the last rep (often faster than the lowering) are runs of their own,
    apart from it and short of that drop, so none can stand in for the lowering or stretch it. It's
    timed at its own threshold, ECC_STOP x its own peak. None if there's no descent."""
    if lowers_first:
        lo, hi = before, rep["start"]
    else:
        lo, hi = rep["end"], after
    if hi - lo < 3:
        return None
    seg = v[lo:hi + 1]
    if seg.min() >= 0:
        return None
    edges = np.flatnonzero(np.diff(np.concatenate([[0], (seg < ECC_STOP * seg.min()).astype(np.int8), [0]])))
    runs = list(zip(edges[::2], edges[1::2] - 1))           # (first, last) sample of each run, in time order
    far = [(a, b) for a, b in runs if x[lo + a] - x[lo + b] >= 0.5 * rep["rom"]]
    if far:
        a, b = far[-1] if lowers_first else far[0]
        k = a + int(np.argmin(seg[a:b + 1]))                # the lowering's own peak speed
    else:
        k = int(np.argmin(seg))
    moving = seg < ECC_STOP * seg[k]                        # seg[k] < 0: "faster down than 3% of the peak"
    i = j = k
    while i > 0 and moving[i - 1]:
        i -= 1
    while j + 1 < len(moving) and moving[j + 1]:
        j += 1
    return float(t[lo + j] - t[lo + i])


def lowers_first(r, reps):
    """Whether each concentric comes after its lowering (the set starts at lockout). By name for
    the big three; a stack or a free weight can go either way (a curl or an OHP rises first, an RDL
    or a lunge lowers first), so for those the data says: did the bar come down most of the first
    rep's ROM to its start?"""
    if r["lift"] in ("squat", "bench"):
        return True
    if r["lift"] == "deadlift":
        return False
    s0 = reps[0]["start"]
    return bool(r["x"][:s0 + 1].max() - r["x"][s0] > 0.5 * reps[0]["rom"])


def rep_features(r):
    """The features of every rep of one analyze() result, after the closed-set filter (a saved set
    starts with the unrack and can end with the sensor handled, D31: sets.closed_set_reps)."""
    import sets as S
    out = []
    reps = S.closed_set_reps(r["reps"], r["lift"])
    if not reps:
        return out
    first = lowers_first(r, reps)
    for k, rep in enumerate(reps):
        p = profile(r["x"], r["v"], rep)
        if p is None:
            continue
        before = reps[k - 1]["end"] if k else 0
        after = reps[k + 1]["start"] if k + 1 < len(reps) else len(r["v"]) - 1
        out.append({"rom": rep["rom"], "dur": rep["dur"], "mcv": rep["mcv"], "profile": p,
                    "ecc": eccentric(r["v"], r["x"], r["t"], rep, before, after, first),
                    "sticking": (rep.get("sticking") or {}).get("pos")})
    return out


def median_ecc(feats):
    e = [f["ecc"] for f in feats if f.get("ecc")]
    return float(np.median(e)) if e else None


def median_rep(feats):
    return {"rom": float(np.median([f["rom"] for f in feats])), "ecc": median_ecc(feats),
            "profile": np.median(np.array([f["profile"] for f in feats]), axis=0)}


def deviation(f, ref):
    """Depth (cm), tempo (%), profile (RMS of the normalized profiles) of one rep against a reference.
    Tempo is the eccentric's duration: the concentric's is speed, which fatigue is supposed to cut
    (analytics.md section 2: velocity loss reports that, consistency keeps it out)."""
    tempo = (f["ecc"] / ref["ecc"] - 1) * 100 if f.get("ecc") and ref.get("ecc") else float("nan")
    return {"depth_cm": (f["rom"] - ref["rom"]) * 100, "tempo_pct": tempo,
            "profile": float(np.sqrt(np.mean((f["profile"] - ref["profile"]) ** 2)))}


def flags(d, floor):
    """Which deviations are over 2x the noise floor; None when there's no floor yet."""
    if floor is None:
        return None
    return [k for k, lim in (("depth_cm", floor["depth"]), ("tempo_pct", floor["tempo"]), ("profile", floor["profile"]))
            if abs(d[k]) > 2 * lim]


def fmt(d, fl):
    mark = " [no floor]" if fl is None else (f" <- {', '.join(fl)}" if fl else "")
    tempo = "  --" if np.isnan(d["tempo_pct"]) else f"{d['tempo_pct']:+5.0f}%"
    return f"depth {d['depth_cm']:+5.1f} cm, tempo {tempo}, profile {d['profile']:.3f}{mark}"


def analyze_csv(path):
    t, a_raw = R.load_phyphox(path, R.AXIS)
    acc, gyro = R.load_imu(path, len(t))
    return R.analyze(t, a_raw, R.lift_from_name(path.name), acc, gyro)


def sessions_from_exports(paths):
    """[(session record, [(set meta, analyze result)])] from app exports, oldest first. A session in
    more than one export (Export all is cumulative) counts once, the first copy, as the app's Import
    does: a second copy would be its own baseline."""
    import sessions as S
    out, seen = [], set()
    for p in paths:
        for s in S.load(p):
            if s["record"]["id"] in seen:
                continue
            seen.add(s["record"]["id"])
            sets = []
            by_index = {x["index"]: x for x in s["record"]["sets"]}
            for st in s["sets"]:
                meta = by_index.get(st["index"], {})
                lift = meta.get("lift") or "free"
                a = np.sqrt(st["ax"].astype(float) ** 2 + st["ay"].astype(float) ** 2 + st["az"].astype(float) ** 2)
                acc = np.column_stack([st["ax"], st["ay"], st["az"]]).astype(float)
                gyro = np.column_stack([st["gx"], st["gy"], st["gz"]]).astype(float)
                try:
                    r = R.analyze(st["t"].astype(float), a, lift, acc, gyro)
                except ValueError:
                    continue
                sets.append((meta, r))
            out.append((s["record"], sets))
    return sorted(out, key=lambda x: x[0]["startedAtMs"])


def stack(refs):
    """The median of several median reps."""
    return {"rom": float(np.median([x["rom"] for x in refs])), "ecc": median_ecc(refs),
            "profile": np.median(np.array([x["profile"] for x in refs]), axis=0)}


def exercise_key(meta, r):
    """What sets are compared by: the typed exercise name, matched the way the app's LV profile
    matches it (sameName: trimmed, any case), else the profile in brackets. Not the profile alone:
    every unnamed exercise is "machine" or "free" (D21), and a curl isn't an OHP."""
    return (meta.get("exercise") or "").strip().lower() or f"({r['lift']})"


def baseline(history, ex, load):
    """The last BASELINE earlier sessions with this exercise within LOAD_BAND of the load: each one's
    median of its matching sets, then the median over those sessions. (None, 0) if there are none."""
    per = {}
    for sid, e, ld, ref in history:
        if e == ex and abs(ld - load) <= LOAD_BAND * load:
            per.setdefault(sid, []).append(ref)
    sids = list(per)[-BASELINE:]                             # history is in session order
    if not sids:
        return None, 0
    return stack([stack(per[s]) for s in sids]), len(sids)


def report(sessions, floor):
    history = []                                             # (session id, exercise, load, set median rep), in order
    for rec, sets in sessions:
        print(f"== {rec['id']}")
        by_ex = {}
        for meta, r in sets:
            feats = rep_features(r)
            if len(feats) < 2:
                continue
            ref = median_rep(feats)
            name = meta.get("exercise") or r["lift"]
            print(f"  set {meta.get('index', 0) + 1} ({name}, {meta.get('load') or '?'} lb): rep to rep, against its median rep")
            for k, f in enumerate(feats, 1):
                d = deviation(f, ref)
                stick = f" | sticks at {f['sticking'] * 100:.0f}%" if f["sticking"] is not None else ""
                print(f"    rep {k}: {fmt(d, flags(d, floor))}{stick}")
            by_ex.setdefault(exercise_key(meta, r), []).append((meta, ref))
        for ex, refs in by_ex.items():
            if len(refs) >= 2:
                sess = stack([x for _, x in refs])
                print(f"  {ex}: set to set, each set's median rep against the session's")
                for meta, x in refs:
                    d = deviation(x, sess)
                    print(f"    set {meta.get('index', 0) + 1}: {fmt(d, flags(d, floor))}")
            for meta, x in refs:
                try:
                    load = float(meta.get("load"))
                except (TypeError, ValueError):
                    continue
                base, n = baseline(history, ex, load)
                if base is not None:
                    d = deviation(x, base)
                    print(f"  {ex} set {meta.get('index', 0) + 1} at {load:g} lb against {n} earlier session(s) at "
                          f"similar load: {fmt(d, flags(d, floor))}")
        for ex, refs in by_ex.items():
            for meta, x in refs:
                try:
                    history.append((rec["id"], ex, float(meta.get("load")), x))
                except (TypeError, ValueError):
                    pass


def selftest():
    """Synthetic sets (FS 400 Hz, noise like make_fake_reps): 5 clean squats, then the deliberate
    faults of analytics.md section 2, one at a time in rep 3: depth cut 5 cm, a hitch mid-rep, a
    slower descent. Each must stand out on its own measure. And fatigue, a slower concentric of the
    same shape, must not count as inconsistent: it's speed (velocity loss), not tempo or shape.
    Then the slower descent again where a moment of moving down that isn't the lowering sits next to
    it (a box sit's wobble, a deadlift's settle at lockout), and on lifts that rise first (a deadlift,
    a stack) or lower first with no name to say so (an RDL): rep 3 must stand out every time. Last,
    sets are grouped by exercise name, not by the profile every unnamed exercise shares."""
    import contextlib
    import io
    FS, G, ROM = 400, 9.81, 0.55
    ease = lambda n: 0.5 - 0.5 * np.cos(np.pi * np.arange(n) / n)

    def dip(base, d, dur):
        """Down d and back up over dur (peak speed pi d / dur): a moment of moving down that isn't
        the lowering."""
        n = int(dur * FS)
        return base - d / 2 * (1 - np.cos(2 * np.pi * np.arange(n) / n))

    def lift_set(fault, seed, lift="squat", top=True, box=False, settle=False, handle=False, rom=ROM):
        """5 reps, rep 3 carrying the fault. top: starts standing and lowers first (a squat, an
        RDL); else starts at the bottom and rises first (a deadlift, a stack, a curl). box: a 1.5 s
        box sit at the bottom with 3 wobble dips (-0.03 to -0.07 m/s); settle: a 1 mm dip as the bar
        settles at lockout (-0.04 m/s); handle: after the set the pod is picked up 4 cm and dropped
        back, faster (-1.0 m/s) than any lowering."""
        rng = np.random.default_rng(seed)
        z = [np.full(int(2 * FS), rom if top else 0.0)]
        for k in range(5):
            depth = rom - (0.05 if fault == "depth" and k == 2 else 0.0)
            up = 1.0 * (1.35 if fault == "fatigue" and k == 2 else 1.0)
            down = 1.1 * (1.35 if fault == "tempo" and k == 2 else 1.0)
            n = int(up * FS)
            u = np.arange(n) / n
            vv = np.sin(np.pi * u)
            if fault == "hitch" and k == 2:
                vv = vv * (1 - 0.7 * np.exp(-((u - 0.45) / 0.06) ** 2))
            x = np.concatenate([[0.0], np.cumsum(vv)[:-1]])
            low = rom - depth
            rise = [low + depth * x / (x[-1] + vv[-1])]
            if settle:
                rise.append(dip(rom, 0.001, 0.08))
            lower = [rom - depth * ease(int(down * FS))]
            if box:
                b = np.full(int(0.2 * FS), low)
                lower += [np.full(int(0.25 * FS), low), dip(low, 0.002, 0.2), b, dip(low, 0.003, 0.2), b,
                          dip(low, 0.0045, 0.2), np.full(int(0.25 * FS), low)]
            if top:
                z += lower + rise + [np.full(int(1.0 * FS), rom)]
            else:
                z += rise + [np.full(int(0.8 * FS), rom)] + lower + [np.full(int(1.0 * FS), low)]
        end = rom if top else 0.0
        if handle:
            z += [end + 0.04 * ease(int(0.3 * FS)), np.full(int(0.3 * FS), end + 0.04), end + 0.04 - 0.04 * ease(int(0.06 * FS))]
        z.append(np.full(int(2 * FS), end))
        z = np.concatenate(z)
        t = np.arange(len(z)) / FS
        a = G + np.gradient(np.gradient(z, t), t) + 0.03 * rng.standard_normal(len(t))
        return R.analyze(t, a, lift)

    def stands_out(name, r, key, want=None, tol=0.0):
        """Rep 3 is the worst by 3x, and when the fault has a known size (a 35% slower descent, a
        5 cm cut), measures it: a blip timed instead of the lowering can still be worst."""
        feats = rep_features(r)
        if len(feats) != 5:
            print(f"FAIL  {name}: {len(feats)} reps, not 5")
            return False
        ref = median_rep(feats)
        devs = [deviation(f, ref) for f in feats]
        worst = max(range(len(devs)), key=lambda k: abs(np.nan_to_num(devs[k][key], nan=np.inf)))
        others = [abs(np.nan_to_num(d[key], nan=np.inf)) for k, d in enumerate(devs) if k != 2]   # a rep with no tempo fails
        good = (worst == 2 and abs(devs[2][key]) > 3 * max(others + [1e-9])
                and (want is None or abs(devs[2][key] - want) <= tol))
        print(f"{'PASS' if good else 'FAIL'}  {name}: rep 3 {key} {devs[2][key]:+.3f}, the other reps at most {max(others):.3f}")
        return good

    ok = True
    for fault, key, want, tol in (("depth", "depth_cm", -5.0, 0.5), ("hitch", "profile", None, 0), ("tempo", "tempo_pct", 35, 5)):
        ok &= stands_out(fault, lift_set(fault, 3), key, want, tol)
    # fatigue: rep 3's concentric 35% slower, same shape: not a tempo or shape deviation
    feats = rep_features(lift_set("fatigue", 3))
    devs = [deviation(f, median_rep(feats)) for f in feats]
    good = len(feats) == 5 and abs(devs[2]["tempo_pct"]) < 3 and devs[2]["profile"] < 0.05
    ok &= good
    print(f"{'PASS' if good else 'FAIL'}  fatigue (a 35% slower concentric) isn't inconsistency: rep 3 tempo "
          f"{devs[2]['tempo_pct']:+.1f}%, profile {devs[2]['profile']:.3f}")
    # the slower descent, found where the old run choice timed a blip (the night review)
    ok &= stands_out("tempo, squat with a box sit's wobble", lift_set("tempo", 4, box=True), "tempo_pct", 35, 5)
    ok &= stands_out("tempo, deadlift with a settle at lockout", lift_set("tempo", 5, "deadlift", top=False, settle=True),
                     "tempo_pct", 35, 5)
    ok &= stands_out("tempo, a stack (rises first, by the data), the pod handled after it",
                     lift_set("tempo", 6, "machine", top=False, handle=True), "tempo_pct", 35, 5)
    ok &= stands_out("tempo, a free weight lowered first (an RDL, by the data)", lift_set("tempo", 7, "free"), "tempo_pct", 35, 5)
    # by exercise: a curl and an OHP share the "free" profile and the load, but aren't one exercise
    curl = lambda s: lift_set(None, s, "free", top=False, rom=0.35)
    ohp = lambda s: lift_set(None, s, "free", top=False, rom=0.50)
    sessions = [({"id": "s1"}, [({"index": 0, "exercise": "Barbell curl", "load": 95}, curl(8))]),
                ({"id": "s2"}, [({"index": 0, "exercise": "OHP", "load": 95}, ohp(9)),
                                ({"index": 1, "exercise": " barbell Curl ", "load": 95}, curl(10))])]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        report(sessions, None)
    out = buf.getvalue()
    good = ("set to set" not in out and "ohp set" not in out
            and "barbell curl set 2 at 95 lb against 1 earlier session(s)" in out)
    ok &= good
    print(f"{'PASS' if good else 'FAIL'}  by exercise: the OHP isn't held against the curls (same profile and load); "
          f"the curl is, by name in any case")
    print("PASS" if ok else "FAIL")
    return ok


def main():
    sys.stdout.reconfigure(errors="replace")      # typed exercise names on a cp1252 console
    if "--selftest" in sys.argv:
        sys.exit(0 if selftest() else 1)
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        sys.exit(__doc__)
    fl = {k: next((float(a.split("=", 1)[1]) for a in sys.argv if a.startswith(f"--floor-{k}=")), None) for k in ("depth", "tempo", "profile")}
    floor = fl if all(v is not None for v in fl.values()) else None
    if floor is None:
        print("No noise floor yet (roadmap 3.5, a Smith-machine set): every deviation is shown, none judged.\n")
    if args[0].endswith(".json"):
        report(sessions_from_exports([Path(a) for a in args]), floor)
    else:
        r = analyze_csv(Path(args[0]))
        report([({"id": Path(args[0]).name, "startedAtMs": 0}, [({"index": 0, "exercise": r["lift"]}, r)])], floor)


if __name__ == "__main__":
    main()
