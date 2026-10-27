"""
Per-rep shape features and consistency (roadmap 5.2, analytics.md sections 1 and 2): how each rep
compares with your own usual rep, in real units, speed kept out of it (velocity loss is its own number).

    python consistency.py data/bar-squat-....csv                # one set: each rep against the set's median rep
    python consistency.py data/session-....json [more.json]     # app exports (D30): rep to rep, set to set,
                                                                # and session to session per lift
    python consistency.py ... --floor-depth=1.0 --floor-tempo=5 --floor-profile=0.05   # the noise floor (3.5)
    python consistency.py --selftest                            # deliberate faults must stand out

Features per rep (section 1): ROM, the concentric's duration, the velocity profile (v at every 5% of
the concentric's height, divided by its MCV: shape, not speed) and the sticking point (D34).
Deviations (section 2): depth = ROM minus the reference's (cm), tempo = duration against the
reference's (%), profile = RMS difference of the normalized profiles. The reference: the set's
median rep (rep to rep), the session's median rep of that lift (set to set), the median of the
last 4 earlier sessions of that lift within 10% of the load (session to session).

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


def eccentric(v, t, rep, before, after, top_first):
    """The lowering's duration: before the concentric for squat and bench (top first), after it for
    a deadlift. From the bottom, walk away while the bar moves down faster than ECC_STOP x the
    descent's peak speed, so drift in a pause at the top can't stretch it. None if there's no descent."""
    if top_first:
        lo, hi = before, rep["start"]
    else:
        lo, hi = rep["end"], after
    if hi - lo < 3:
        return None
    vmin = v[lo:hi + 1].min()
    if vmin >= 0:
        return None
    moving = v[lo:hi + 1] < ECC_STOP * vmin                 # vmin < 0: "faster down than 3% of the peak"
    # the one unbroken run of it at the bottom: a walkout's bobs before the descent (each a moment of
    # moving down, with stops between) must not join it
    i = int(np.flatnonzero(moving)[-1] if top_first else np.flatnonzero(moving)[0])
    j = i
    step = -1 if top_first else 1
    while 0 <= j + step < len(moving) and moving[j + step]:
        j += step
    return float(abs(t[lo + i] - t[lo + j]))


def rep_features(r):
    """The features of every rep of one analyze() result, after the closed-set filter (a saved set
    starts with the unrack and can end with the sensor handled, D31: sets.closed_set_reps)."""
    import sets as S
    out = []
    reps = S.closed_set_reps(r["reps"], r["lift"])
    top_first = r["lift"] != "deadlift"
    for k, rep in enumerate(reps):
        p = profile(r["x"], r["v"], rep)
        if p is None:
            continue
        before = reps[k - 1]["end"] if k else 0
        after = reps[k + 1]["start"] if k + 1 < len(reps) else len(r["v"]) - 1
        out.append({"rom": rep["rom"], "dur": rep["dur"], "mcv": rep["mcv"], "profile": p,
                    "ecc": eccentric(r["v"], r["t"], rep, before, after, top_first),
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
    """[(session record, [(set meta, analyze result)])] from app exports, oldest first."""
    import sessions as S
    out = []
    for p in paths:
        for s in S.load(p):
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


def baseline(history, lift, load):
    """The last BASELINE earlier sessions with this lift within LOAD_BAND of the load: each one's
    median of its matching sets, then the median over those sessions. (None, 0) if there are none."""
    per = {}
    for sid, l, ld, ref in history:
        if l == lift and abs(ld - load) <= LOAD_BAND * load:
            per.setdefault(sid, []).append(ref)
    sids = list(per)[-BASELINE:]                             # history is in session order
    if not sids:
        return None, 0
    return stack([stack(per[s]) for s in sids]), len(sids)


def report(sessions, floor):
    history = []                                             # (session id, lift, load, set median rep), in order
    for rec, sets in sessions:
        print(f"== {rec['id']}")
        by_lift = {}
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
            by_lift.setdefault(r["lift"], []).append((meta, ref))
        for lift, refs in by_lift.items():
            if len(refs) >= 2:
                sess = stack([x for _, x in refs])
                print(f"  {lift}: set to set, each set's median rep against the session's")
                for meta, x in refs:
                    d = deviation(x, sess)
                    print(f"    set {meta.get('index', 0) + 1}: {fmt(d, flags(d, floor))}")
            for meta, x in refs:
                try:
                    load = float(meta.get("load"))
                except (TypeError, ValueError):
                    continue
                base, n = baseline(history, lift, load)
                if base is not None:
                    d = deviation(x, base)
                    print(f"  {lift} set {meta.get('index', 0) + 1} at {load:g} lb against {n} earlier session(s) at "
                          f"similar load: {fmt(d, flags(d, floor))}")
        for lift, refs in by_lift.items():
            for meta, x in refs:
                try:
                    history.append((rec["id"], lift, float(meta.get("load")), x))
                except (TypeError, ValueError):
                    pass


def selftest():
    """Synthetic sets (FS 400 Hz, noise like make_fake_reps): 5 clean squats, then the deliberate
    faults of analytics.md section 2, one at a time in rep 3: depth cut 5 cm, a hitch mid-rep, a
    slower descent. Each must stand out on its own measure. And fatigue, a slower concentric of the
    same shape, must not count as inconsistent: it's speed (velocity loss), not tempo or shape."""
    FS, G, ROM = 400, 9.81, 0.55
    ease = lambda n: 0.5 - 0.5 * np.cos(np.pi * np.arange(n) / n)

    def squat_set(fault, seed):
        rng = np.random.default_rng(seed)
        z = [np.full(int(2 * FS), ROM)]
        for k in range(5):
            depth = ROM - (0.05 if fault == "depth" and k == 2 else 0.0)
            up = 1.0 * (1.35 if fault == "fatigue" and k == 2 else 1.0)
            down = 1.1 * (1.35 if fault == "tempo" and k == 2 else 1.0)
            z += [ROM - depth * ease(int(down * FS))]
            n = int(up * FS)
            u = np.arange(n) / n
            vv = np.sin(np.pi * u)
            if fault == "hitch" and k == 2:
                vv = vv * (1 - 0.7 * np.exp(-((u - 0.45) / 0.06) ** 2))
            x = np.concatenate([[0.0], np.cumsum(vv)[:-1]])
            z += [(ROM - depth) + depth * x / (x[-1] + vv[-1]), np.full(int(1.0 * FS), ROM)]
        z.append(np.full(int(2 * FS), ROM))
        z = np.concatenate(z)
        t = np.arange(len(z)) / FS
        a = G + np.gradient(np.gradient(z, t), t) + 0.03 * rng.standard_normal(len(t))
        return R.analyze(t, a, "squat")

    ok = True
    for fault, key in (("depth", "depth_cm"), ("hitch", "profile"), ("tempo", "tempo_pct")):
        feats = rep_features(squat_set(fault, 3))
        ref = median_rep(feats)
        devs = [deviation(f, ref) for f in feats]
        worst = max(range(len(devs)), key=lambda k: abs(devs[k][key]))
        others = [abs(d[key]) for k, d in enumerate(devs) if k != 2]
        clear = abs(devs[2][key]) > 3 * max(others + [1e-9])
        good = len(feats) == 5 and worst == 2 and clear
        ok &= good
        print(f"{'PASS' if good else 'FAIL'}  {fault}: rep 3 {key} {devs[2][key]:+.3f}, the other reps at most {max(others):.3f}")
    # fatigue: rep 3's concentric 35% slower, same shape: not a tempo or shape deviation
    feats = rep_features(squat_set("fatigue", 3))
    devs = [deviation(f, median_rep(feats)) for f in feats]
    good = len(feats) == 5 and abs(devs[2]["tempo_pct"]) < 3 and devs[2]["profile"] < 0.05
    ok &= good
    print(f"{'PASS' if good else 'FAIL'}  fatigue (a 35% slower concentric) isn't inconsistency: rep 3 tempo "
          f"{devs[2]['tempo_pct']:+.1f}%, profile {devs[2]['profile']:.3f}")
    print("PASS" if ok else "FAIL")
    return ok


def main():
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
