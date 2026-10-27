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
Deviations (section 2): depth = ROM minus the reference's (cm), tempo = the lowering's duration
against the reference's (%), per gap between two reps (D35: which side of a rep its lowering is on
the motion around a set can't always say), profile = RMS difference of the normalized profiles.
The reference: the set's
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


ECC_STOP = 0.03       # a lowering's ends: where its speed is under this x its peak (D32's idea)
ECC_PIECE = 0.10      # x ROM: moving down less than this is a wobble, a settle, a sag, never a lowering's piece
ECC_RISE = 0.02       # x ROM: pieces with no more rise than this between them are one lowering, however long the stop


def lowering(v, x, t, lo, hi, rom):
    """The lowering's duration in the gap v[lo:hi + 1] between two reps (one's top to the next's
    start). The gap's runs of moving down (faster than ECC_STOP x its peak downward speed) that drop
    ECC_PIECE x rom or more are pieces, and pieces with no rise between them (ECC_RISE x rom) are one
    lowering, however long the stop partway down. The lowering is the biggest drop, timed at
    ECC_STOP x its own peak; with no piece, the run holding the gap's peak speed. Drift in a pause at
    the top, a wobble in a bottom pause or box sit and a settle at lockout are too small to be
    pieces, and a 1 1/4 rep's quarter is kept apart by its rise. None if the bar never moves down."""
    if hi - lo < 3:
        return None
    seg = v[lo:hi + 1]
    if seg.min() >= 0:
        return None
    edges = np.flatnonzero(np.diff(np.concatenate([[0], (seg < ECC_STOP * seg.min()).astype(np.int8), [0]])))
    pieces = []
    for a, b in zip(edges[::2], edges[1::2] - 1):
        if x[lo + a] - x[lo + b] < ECC_PIECE * rom:
            continue
        if pieces and x[lo + pieces[-1][1]:lo + a + 1].max() - x[lo + pieces[-1][1]] <= ECC_RISE * rom:
            pieces[-1] = (pieces[-1][0], b)
        else:
            pieces.append((a, b))
    if pieces:
        a, b = max(pieces, key=lambda p: x[lo + p[0]] - x[lo + p[1]])
    else:
        a = b = int(np.argmin(seg))
    k = a + int(np.argmin(seg[a:b + 1]))                    # the lowering's own peak speed
    moving = seg < ECC_STOP * seg[k]                        # seg[k] < 0: "faster down than 3% of the peak"
    i, j = a, b
    while i > 0 and moving[i - 1]:
        i -= 1
    while j + 1 < len(moving) and moving[j + 1]:
        j += 1
    return float(t[lo + j] - t[lo + i])


def set_features(r):
    """One analyze() result's features, after the closed-set filter (a saved set starts with the
    unrack and can end with the sensor handled, D31: sets.closed_set_reps): per rep, and per gap
    between two reps the lowering's duration (tempo). Per gap, not per rep: whether a rep's lowering
    is the one before it (a squat) or after it (a deadlift) the motion around the set can't always
    say (a Romanian deadlift tracked as a deadlift, a pin squat, a row's hinge from the hang, the bar
    set down after the set: four reviews, a new counter-example each), and the lowering between two
    reps is the same event either way. The ends, before the first rep and after the last, aren't
    used: the unrack, a walkout, the bar set down, the pod handled, a cut-off rep."""
    import sets as S
    reps = S.closed_set_reps(r["reps"], r["lift"])
    feats = []
    for rep in reps:
        p = profile(r["x"], r["v"], rep)
        if p is not None:
            feats.append({"rom": rep["rom"], "dur": rep["dur"], "mcv": rep["mcv"], "profile": p,
                          "sticking": (rep.get("sticking") or {}).get("pos")})
    ecc = [lowering(r["v"], r["x"], r["t"], p["end"], q["start"], min(p["rom"], q["rom"])) for p, q in zip(reps, reps[1:])]
    return {"reps": feats, "ecc": ecc}


def median_ecc(es):
    e = [x for x in es if x]
    return float(np.median(e)) if e else None


def median_rep(fs):
    """A set's usual rep: the median ROM and profile of its reps, the median of its lowerings."""
    return {"rom": float(np.median([f["rom"] for f in fs["reps"]])), "ecc": median_ecc(fs["ecc"]),
            "profile": np.median(np.array([f["profile"] for f in fs["reps"]]), axis=0)}


def deviation(f, ref):
    """Depth (cm), tempo (%), profile (RMS of the normalized profiles) against a reference. Tempo is
    the eccentric's duration (a set's median lowering against the reference's; a rep has none): the
    concentric's is speed, which fatigue is supposed to cut (analytics.md section 2: velocity loss
    reports that, consistency keeps it out)."""
    tempo = (f["ecc"] / ref["ecc"] - 1) * 100 if f.get("ecc") and ref.get("ecc") else float("nan")
    return {"depth_cm": (f["rom"] - ref["rom"]) * 100, "tempo_pct": tempo,
            "profile": float(np.sqrt(np.mean((f["profile"] - ref["profile"]) ** 2)))}


def tempos(fs, ref):
    """Each gap's lowering against the reference's (%), NaN where a gap has none."""
    return [(e / ref["ecc"] - 1) * 100 if e and ref.get("ecc") else float("nan") for e in fs["ecc"]]


def flags(d, floor):
    """Which deviations are over 2x the noise floor; None when there's no floor yet."""
    if floor is None:
        return None
    return [k for k, lim in (("depth_cm", floor["depth"]), ("tempo_pct", floor["tempo"]), ("profile", floor["profile"]))
            if abs(d[k]) > 2 * lim]


def fmt(d, fl, tempo=True):
    mark = " [no floor]" if fl is None else (f" <- {', '.join(fl)}" if fl else "")
    tp = "" if not tempo else (", tempo   --" if np.isnan(d["tempo_pct"]) else f", tempo {d['tempo_pct']:+5.0f}%")
    return f"depth {d['depth_cm']:+5.1f} cm{tp}, profile {d['profile']:.3f}{mark}"


def gap_line(fs, ref, floor):
    """The set's lowerings between reps, each against their median."""
    parts = []
    for k, p in enumerate(tempos(fs, ref), 1):
        flag = " <-" if floor is not None and not np.isnan(p) and abs(p) > 2 * floor["tempo"] else ""
        parts.append(f"{k}-{k + 1} " + ("--" if np.isnan(p) else f"{p:+.0f}%") + flag)
    mark = " [no floor]" if floor is None else ""
    return f"lowerings between reps, against their median {ref['ecc']:.2f} s: {', '.join(parts)}{mark}"


def analyze_csv(path):
    t, a_raw = R.load_phyphox(path, R.AXIS)
    acc, gyro = R.load_imu(path, len(t))
    return R.analyze(t, a_raw, R.lift_from_name(path.name), acc, gyro)


def sessions_from_exports(paths):
    """[(session record, [(set meta, analyze result)])] from app exports, oldest first. A session in
    more than one export (Export all is cumulative) counts once, or it would be its own baseline:
    the copy from the newest export by its own "exported" time, not by the files' names or order
    (a one-session export is named by the session, and can be made any day), since a past session's
    names, loads, RPE and sets can be edited after an older export."""
    import sessions as S
    out, seen = [], set()
    for p in sorted(paths, key=S.exported_at, reverse=True):
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
    return {"rom": float(np.median([x["rom"] for x in refs])), "ecc": median_ecc([x["ecc"] for x in refs]),
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
            fs = set_features(r)
            if len(fs["reps"]) < 2:
                continue
            ref = median_rep(fs)
            name = meta.get("exercise") or r["lift"]
            print(f"  set {meta.get('index', 0) + 1} ({name}, {meta.get('load') or '?'} lb): rep to rep, against its median rep")
            for k, f in enumerate(fs["reps"], 1):
                d = deviation(f, ref)
                stick = f" | sticks at {f['sticking'] * 100:.0f}%" if f["sticking"] is not None else ""
                print(f"    rep {k}: {fmt(d, flags(d, floor), tempo=False)}{stick}")
            if ref["ecc"]:
                print(f"    {gap_line(fs, ref, floor)}")
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
    Then the slower descent again, its lowering found and measured to its true size between the
    right two reps: next to a box sit's wobble or a deadlift's settle at lockout, split by a long
    stop partway down, on lifts that lower first (squat, an RDL, a Romanian deadlift) or rise first
    (a deadlift, a stack, a pin squat, a row with a hinge, a curl from a high rack), with the bar set
    down after the set, the pod handled, or the recording cut mid-rep. Then lowering() on exact
    numbers, each threshold moved either way failing a case. Last, a session in several exports
    counts once (its newest copy, by its own time), and sets are grouped by exercise name, not by
    the profile every unnamed exercise shares."""
    import contextlib
    import io
    FS, G, ROM = 400, 9.81, 0.55
    ease = lambda n: 0.5 - 0.5 * np.cos(np.pi * np.arange(n) / n)

    def dip(base, d, dur):
        """Down d and back up over dur (peak speed pi d / dur): a moment of moving down that isn't
        the lowering."""
        n = int(dur * FS)
        return base - d / 2 * (1 - np.cos(2 * np.pi * np.arange(n) / n))

    def lift_set(fault, seed, lift="squat", top=True, box=False, settle=False, handle=False, rom=ROM,
                 hinge=0.0, stop=0.0, tremble=False, after=None, cut=False, raw=False):
        """5 reps, rep 3 carrying the fault. top: starts standing and lowers first (a squat, an
        RDL); else starts at the bottom and rises first (a deadlift, a stack, a curl). box: a 1.5 s
        box sit at the bottom with 3 wobble dips (-0.03 to -0.07 m/s); settle: a 1 mm dip as the bar
        settles at lockout (-0.04 m/s); handle: after the set the pod is picked up 4 cm and dropped
        back, faster (-1.0 m/s) than any lowering; hinge: a rises-first set starts this much higher
        and comes down to the first rep's start (a row from the hang, a curl from a high rack);
        stop: rep 3's lowering stops this long at 40% of the way down (tremble: shaking 0.4 mm at
        8 Hz, not a still: a dead-still stop of 0.5 s or more is a still to reps.py, whose leveling
        then bends that rep, an open reps.py issue); after: the bar is then taken
        to this height (set down, reracked, the pod unclipped); cut: the recording ends halfway up
        rep 5. raw: (t, a) instead of the analysis."""
        rng = np.random.default_rng(seed)
        z = [np.full(int(2 * FS), rom if top else hinge)]
        if hinge and not top:
            z += [hinge * (1 - ease(int(0.8 * FS))), np.full(int(0.5 * FS), 0.0)]
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
            if cut and k == 4:
                rise = [rise[0][:n // 2]]
            if settle:
                rise.append(dip(rom, 0.001, 0.08))
            lower = [rom - depth * ease(int(down * FS))]
            if stop and k == 2:
                mid = rom - 0.4 * depth
                shake = 0.0004 * np.sin(2 * np.pi * 8 * np.arange(int(stop * FS)) / FS) if tremble else 0.0
                lower = [rom - 0.4 * depth * ease(int(0.4 * down * FS)), mid + shake + np.zeros(int(stop * FS)),
                         mid - 0.6 * depth * ease(int(0.6 * down * FS))]
            if box:
                b = np.full(int(0.2 * FS), low)
                lower += [np.full(int(0.25 * FS), low), dip(low, 0.002, 0.2), b, dip(low, 0.003, 0.2), b,
                          dip(low, 0.0045, 0.2), np.full(int(0.25 * FS), low)]
            if cut and k == 4:
                z += (lower + rise) if top else rise
                break
            if top:
                z += lower + rise + [np.full(int(1.0 * FS), rom)]
            else:
                z += rise + [np.full(int(0.8 * FS), rom)] + lower + [np.full(int(1.0 * FS), low)]
        if not cut:
            end = rom if top else 0.0
            if handle:
                z += [end + 0.04 * ease(int(0.3 * FS)), np.full(int(0.3 * FS), end + 0.04), end + 0.04 - 0.04 * ease(int(0.06 * FS))]
            if after is not None:
                z += [end + (after - end) * ease(int(1.2 * FS))]
                end = after
            z.append(np.full(int(2 * FS), end))
        z = np.concatenate(z)
        t = np.arange(len(z)) / FS
        a = G + np.gradient(np.gradient(z, t), t) + 0.03 * rng.standard_normal(len(t))
        return (t, a) if raw else R.analyze(t, a, lift)

    def stands_out(name, r, key, want=None, tol=0.0, at=2, n=5):
        """The planted fault (rep `at` for depth and profile, the lowering between reps `at` + 1 and
        `at` + 2 for tempo) is the worst by 3x, and when it has a known size (a 35% slower descent, a
        5 cm cut), measures it: a blip timed instead of the lowering can still be worst."""
        fs = set_features(r)
        if len(fs["reps"]) != n:
            print(f"FAIL  {name}: {len(fs['reps'])} reps, not {n}")
            return False
        ref = median_rep(fs)
        vals = tempos(fs, ref) if key == "tempo_pct" else [deviation(f, ref)[key] for f in fs["reps"]]
        mag = [abs(np.nan_to_num(v, nan=np.inf)) for v in vals]   # a gap with no lowering fails
        others = [m for k, m in enumerate(mag) if k != at]
        good = (int(np.argmax(mag)) == at and mag[at] > 3 * max(others + [1e-9])
                and (want is None or abs(vals[at] - want) <= tol))
        where = f"the lowering between reps {at + 1} and {at + 2}" if key == "tempo_pct" else f"rep {at + 1}"
        print(f"{'PASS' if good else 'FAIL'}  {name}: {where} {key} {vals[at]:+.3f}, the others at most {max(others):.3f}")
        return good

    ok = True
    for fault, key, want, tol, at in (("depth", "depth_cm", -5.0, 0.5, 2), ("hitch", "profile", None, 0, 2),
                                      ("tempo", "tempo_pct", 35, 5, 1)):
        ok &= stands_out(fault, lift_set(fault, 3), key, want, tol, at)
    # fatigue: rep 3's concentric 35% slower, same shape: not a tempo or shape deviation
    fs = set_features(lift_set("fatigue", 3))
    ref = median_rep(fs)
    worst = max(abs(p) for p in tempos(fs, ref))
    prof = deviation(fs["reps"][2], ref)["profile"]
    good = len(fs["reps"]) == 5 and worst < 3 and prof < 0.05
    ok &= good
    print(f"{'PASS' if good else 'FAIL'}  fatigue (a 35% slower concentric) isn't inconsistency: lowerings at most "
          f"{worst:.1f}% off, rep 3's profile {prof:.3f}")
    # the slower descent, between the right two reps whatever is around it (four reviews' cases)
    T = lambda name, r, at, want=35, n=5: stands_out(f"tempo, {name}", r, "tempo_pct", want, 5, at, n)
    ok &= T("a squat with a box sit's wobble", lift_set("tempo", 4, box=True), 1)
    ok &= T("a deadlift with a settle at lockout", lift_set("tempo", 5, "deadlift", top=False, settle=True), 2)
    ok &= T("a stack, the pod handled after it", lift_set("tempo", 6, "machine", top=False, handle=True), 2)
    ok &= T("an unnamed free weight lowered first (an RDL)", lift_set("tempo", 7, "free"), 1)
    ok &= T("a Romanian deadlift (tracked as a deadlift, lowers first)", lift_set("tempo", 11, "deadlift"), 1)
    ok &= T("a pin squat (tracked as a squat, rises first)", lift_set("tempo", 12, "squat", top=False), 2)
    ok &= T("a bent-over row whose hinge from the hang comes down to rep 1's start",
            lift_set("tempo", 13, "free", top=False, hinge=0.28, rom=0.30), 2)
    # (the rerack is a 6th "rep": a 0.35 m lift onto the hooks next to 0.40 m curls, which the rack
    # filter can't tell apart, an open D31 issue; the lowerings stay between the right reps)
    ok &= T("a curl from a high rack, reracked", lift_set("tempo", 18, "free", top=False, hinge=0.35, rom=0.40, after=0.35), 2, n=6)
    ok &= T("a Romanian deadlift set down on the floor after the set", lift_set("tempo", 16, "deadlift", after=-0.125 * ROM), 1)
    ok &= T("a lunge, the pod unclipped and held at the waist", lift_set("tempo", 17, "free", after=0.1 * ROM), 1)
    ok &= T("a lunge cut off halfway up rep 5 (End tapped mid-move)", lift_set("tempo", 19, "free", cut=True), 1, n=4)
    ok &= T("a squat whose slower descent stops 0.1 s partway", lift_set("tempo", 14, stop=0.1), 1, ((1.35 * 1.1 + 0.1) / 1.1 - 1) * 100)
    ok &= T("a squat whose slower descent stops 1.0 s partway, shaking", lift_set("tempo", 20, stop=1.0, tremble=True), 1,
            ((1.35 * 1.1 + 1.0) / 1.1 - 1) * 100)

    # lowering()'s parts on exact speeds, each threshold moved either way failing a case. Each answer
    # is a sine-speed move of T at 3% of its own peak: T (1 - 2 asin(0.03) / pi), the 0.03 by hand
    # so a moved ECC_STOP can't move the answer with it. ROM 0.5 m.
    def vel(drop, dur):
        n = int(dur * FS)
        return -np.pi * drop / (2 * dur) * np.sin(np.pi * np.arange(n) / n)
    hold = lambda d: np.zeros(int(d * FS))
    own = lambda T: T * (1 - 2 * np.arcsin(0.03) / np.pi)

    def known(name, parts, want):
        v = np.concatenate(parts)
        t = np.arange(len(v)) / FS
        x = np.concatenate([[0.0], np.cumsum(v)[:-1] / FS])
        got = lowering(v, x, t, 0, len(v) - 1, 0.5)
        good = got is not None and abs(got - want) < 0.01
        print(f"{'PASS' if good else 'FAIL'}  lowering's known answer, {name}: {'none' if got is None else f'{got:.3f}'} s, "
              f"want {want:.3f} s")
        return good

    split = lambda between: [hold(0.5), vel(0.15, 0.6)] + between + [vel(0.15, 0.6), hold(0.5)]
    ok &= known("the slow 0.6 ROM lowering, not a faster 0.4 ROM blip before it",
                [hold(0.5), vel(0.2, 0.2), -vel(0.2, 0.5), hold(0.5), vel(0.3, 2.0), hold(0.5)], own(2.0))
    ok &= known("two pieces with a 1 s stop between, partway down", split([hold(1.0)]), 2.2 - (0.6 - own(0.6)))
    ok &= known("two pieces with a 0.015 ROM bob up between: one lowering", split([-vel(0.0075, 0.1)]), 1.3 - (0.6 - own(0.6)))
    ok &= known("two pieces with a 0.03 ROM rise between: two", split([-vel(0.015, 0.1)]), own(0.6))
    ok &= known("a 1 1/4 squat's descent, not its quarter", [hold(0.5), vel(0.5, 1.2), hold(0.05), -vel(0.125, 0.2),
                                                           vel(0.125, 0.4), hold(0.1)], own(1.2))
    ok &= known("a 0.07 ROM settle at the bottom isn't a piece", [hold(0.5), vel(0.3, 1.0), hold(0.2), vel(0.035, 0.3), hold(0.3)],
                own(1.0))
    ok &= known("a 0.15 ROM sink after a 0.2 s stop is one", [hold(0.5), vel(0.3, 1.0), hold(0.2), vel(0.075, 0.25), hold(0.5)],
                1.45 - (1.0 - own(1.0)) / 2 - (0.25 - own(0.25)) / 2)

    # a session in several exports: the newest copy by its own time, not by the files' names, order or dates
    import base64
    import json
    import os
    import shutil
    import tempfile
    t_a, a_a = lift_set(None, 15, raw=True)
    col = lambda arr, typ: {"type": typ, "b64": base64.b64encode(np.asarray(arr, "<f8" if typ == "f64" else "<f4").tobytes()).decode()}
    def entry(sid, ms, name):
        st = {"index": 0, "n": len(t_a), "tAbs": col(t_a, "f64"),
              "cols": {"t": col(t_a, "f64"), "ax": col(0 * a_a, "f32"), "ay": col(0 * a_a, "f32"), "az": col(a_a, "f32"),
                       "gx": col(0 * a_a, "f32"), "gy": col(0 * a_a, "f32"), "gz": col(0 * a_a, "f32")}}
        return {"record": {"id": sid, "startedAtMs": ms, "sets": [{"index": 0, "exercise": name, "lift": "squat", "load": 225}]},
                "sets": [st]}
    tmp = Path(tempfile.mkdtemp(prefix="consistency-selftest-"))
    try:
        # a one-session export is named by the session's folder and can be made any day, so names
        # sort in neither time order; nor do the files' own times (a copy, a re-download)
        mon, tue, wed = (tmp / "bartracker-export-2026-10-20.json", tmp / "session-2026-10-19-180000.json",
                         tmp / "bartracker-export-2026-10-22.json")
        for p, when, ses in ((mon, "2026-10-20T10:00:00.000Z", [entry("A", 1, "")]),
                             (tue, "2026-10-21T10:00:00.000Z", [entry("A", 1, "Squat")]),
                             (wed, "2026-10-22T10:00:00.000Z", [entry("A", 1, "Back squat"), entry("B", 2, "Back squat")])):
            p.write_text(json.dumps({"format": "bartracker-sessions", "version": 1, "exported": when, "sessions": ses}), encoding="utf-8")
        os.utime(mon, (2e9, 2e9))
        orders = [sessions_from_exports(o) for o in ([mon, tue, wed], [wed, tue, mon])]
        good = all([s[0]["id"] for s in got] == ["A", "B"] and got[0][1][0][0]["exercise"] == "Back squat" for got in orders)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    ok &= good
    print(f"{'PASS' if good else 'FAIL'}  a session in three exports counts once, its newest copy by the export's own time "
          f"(not the names, the order or the file dates)")
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
