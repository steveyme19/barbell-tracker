"""
Session mode: split one long recording (a whole workout) into sets, time the rest
between them, and run the rep algorithm (reps.py) on each set.

How it works (docs/algorithm.md section 12):
  1. Cut time into 0.25 s blocks. A block is "active" if |a| wiggles more than
     ACTIVE_STD inside it (same idea as the stillness test, coarser).
  2. Active blocks closer together than SET_GAP (15 s of quiet) belong to one motion
     segment. A segment closes once SET_GAP of quiet has gone by.
  3. Each segment, plus LEAD s of still before it and TRAIL s after, goes through
     reps.analyze(). No reps = not a set (loading plates, bumping the bar).
  4. Rack moves: unracking lifts the bar a few cm, which can count as a tiny "rep".
     Reps at the start or end of a set under RACK_FRAC of the set's median ROM go, and
     (except phone tests) anything under RACK_MIN_ROM. Handling the sensor before the set
     closes (unclipping the pod, picking up the phone) is the opposite: one huge end "rep",
     over RACK_MAX_ROM or RACK_MAX_FRAC x the other reps' median, and it goes too. A closed
     set's rep with no end anchor goes as well: only motion running up to the cut makes one
     (D31). The live view keeps both, it needs the rep in progress.
  5. Rest = first rep of a set minus the last rep of the set before.

Blocks and segments are defined so a live, sample-by-sample version (webapp/sets.js)
finds exactly the same sets. The parity test checks that.

Also: rpe_estimate(), a rough v0 RPE from the last rep's speed (analytics.md section 5).

Usage:
    python sets.py data/fake_session.csv                 # lift from the file name
    python sets.py data/fake_session.csv --lifts=squat,bench   # per set; the last one repeats
                                                         # (machine/free: the gyro decides, D21)
    python sets.py data/fake_session.csv --json          # for the parity test
"""

import sys
from pathlib import Path

import numpy as np

import reps as R

# ---- Settings ----
BLOCK = 0.25        # s: activity is decided per block of this length
ACTIVE_STD = 0.15   # m/s^2: block std of |a| above this = moving (reps.STILL_STD)
SET_GAP = 15.0      # s: this much quiet ends a set. Shorter pauses stay inside the set
LEAD = 2.0          # s: still before the first active block handed to the rep algorithm
TRAIL = 2.0         # s: still after the last active block
RACK_FRAC = 0.5     # reps at either end of a set shorter than this x median ROM = rack moves
RACK_MIN_ROM = 0.12 # m: a rep at either end shorter than this is a rack move or a stack bump
                    # (an unrack before any real rep, unrack + rerack alone, setting the pin).
                    # Everything but hand-held phone tests (D19)
RACK_MAX_ROM = {"squat": 1.0, "bench": 0.8, "deadlift": 1.0}   # m: no real rep of these goes
                    # higher. An end "rep" above it is the sensor being handled (D31)
RACK_MAX_FRAC = 2.0 # an end rep over this x the median of the set's OTHER reps is handling too
                    # (the only test for machine, free and phone: a stack or a clean can move ~1 m)

# RPE v0 (uncalibrated, analytics.md section 5): RIR = (last rep MCV - v1RM) / DV_PER_REP.
# v1RM per lift from the research in section 5. DV_PER_REP is a rough starting guess
# (velocity gained per rep left in the tank) until your own failure sets replace it
# (Model v1). Phone tests get no RPE.
V1RM = {"squat": 0.23, "bench": 0.10, "deadlift": 0.14}
DV_PER_REP = {"squat": 0.04, "bench": 0.035, "deadlift": 0.04}


def block_activity(t, a_raw):
    """Per block: active or not, and the index of its first sample. Blocks are
    [k*BLOCK, (k+1)*BLOCK) from the first sample. Blocks with < 3 samples are quiet."""
    k = np.floor((t - t[0]) / BLOCK).astype(int)
    nb = k[-1] + 1
    starts = np.searchsorted(k, np.arange(nb + 1))
    active = np.zeros(nb, bool)
    for b in range(nb):
        seg = a_raw[starts[b]:starts[b + 1]]
        if len(seg) >= 3:
            active[b] = seg.std() > ACTIVE_STD
    return active, starts


def find_segments(active):
    """(first, last) active block of each motion segment."""
    gap = int(round(SET_GAP / BLOCK))
    segs, start, last = [], None, None
    for b, act in enumerate(active):
        if act:
            if start is None:
                start = b
            last = b
        elif start is not None and b - last >= gap:
            segs.append((start, last))
            start = None
    if start is not None:
        segs.append((start, last))
    return segs


def drop_rack_moves(reps, lift):
    """The ends of a set that aren't reps. Too big first: handling the sensor (one huge end
    'rep'). Then too small: unrack/rerack bumps. The small floor comes from what's left, so a
    2 m pickup can't lift the median and push a real single under it."""
    if not reps:
        return reps
    cap = RACK_MAX_ROM.get(lift, np.inf)

    def big(r, others):
        return r["rom"] > cap or (len(others) > 0 and r["rom"] > RACK_MAX_FRAC * np.median([o["rom"] for o in others]))

    while reps and big(reps[-1], reps[:-1]):
        reps = reps[:-1]
    while reps and big(reps[0], reps[1:]):
        reps = reps[1:]
    if not reps:
        return reps
    floor = RACK_MIN_ROM if lift != "phone" else 0.0       # bar or stack, not hand-held
    if len(reps) >= 2:
        floor = max(floor, RACK_FRAC * np.median([r["rom"] for r in reps]))
    while reps and reps[0]["rom"] < floor:
        reps = reps[1:]
    while reps and reps[-1]["rom"] < floor:
        reps = reps[:-1]
    return reps


def closed_set_reps(reps, lift):
    """A closed set's reps: the anchored ones, with rack moves and handling off the ends. A
    closed set always has TRAIL s of still after its last active block, so a rep with no end
    anchor only happens when motion ran up to the cut: the sensor handled, or End tapped mid-move."""
    return drop_rack_moves([r for r in reps if "no end anchor" not in r["flags"]], lift)


def rpe_estimate(lift, last_mcv):
    """(low, high) RPE range from the last rep's MCV, or None (phone test, unknown lift).
    Uncalibrated v0: treat it as a hint, especially far from failure."""
    if lift not in V1RM:
        return None
    rir = max(0.0, (last_mcv - V1RM[lift]) / DV_PER_REP[lift])
    rpe = 10 - rir
    clamp = lambda x: min(10.0, max(5.0, round(x * 2) / 2))    # RPE 5 to 10, half steps
    return (clamp(rpe - 1), clamp(rpe + 1))


def set_summary(lift, reps, t_abs):
    """Numbers for the set card and session.csv. t_abs = absolute times of the samples."""
    mcvs = np.array([r["mcv"] for r in reps])
    roms = np.array([r["rom"] for r in reps])
    return dict(
        lift=lift, n_reps=len(reps),
        start_t=float(t_abs[reps[0]["start"]]), end_t=float(t_abs[reps[-1]["end"]]),
        best_mcv=float(mcvs.max()), last_mcv=float(mcvs[-1]),
        vel_loss=float(1 - mcvs[-1] / mcvs.max()), avg_rom=float(roms.mean()),
        rpe_est=rpe_estimate(lift, float(mcvs[-1])),
    )


def find_sets(t, a_raw, lifts, acc=None, gyro=None):
    """All sets in a session recording. lifts: list, one per set found (the last one
    repeats), from the typed names. acc, gyro (n x 3) let each set be checked for machine
    vs free weight (D21). Returns a list of dicts: sample range, rep results, summary, the
    profile used, rest before."""
    t = t - t[0]
    active, starts = block_activity(t, a_raw)
    sets = []
    for b0, b1 in find_segments(active):
        i0 = int(np.searchsorted(t, b0 * BLOCK - LEAD))                       # first t >= ...
        i1 = int(np.searchsorted(t, (b1 + 1) * BLOCK + TRAIL)) - 1           # last t < ...
        named = lifts[min(len(sets), len(lifts) - 1)]
        cut = lambda arr: None if arr is None else arr[i0:i1 + 1]
        try:
            r = R.analyze(t[i0:i1 + 1], a_raw[i0:i1 + 1], named, cut(acc), cut(gyro))
        except ValueError:
            continue
        lift = r["lift"]                     # the name, or what the sensor saw (D21)
        reps = closed_set_reps(r["reps"], lift)
        if not reps:
            continue                         # plates, a bump, the sensor handled: not a set
        s = dict(i0=i0, i1=i1, reps=reps, mount=r["mount"], **set_summary(lift, reps, t[i0:i1 + 1]))
        s["rest_before"] = s["start_t"] - sets[-1]["end_t"] if sets else None
        sets.append(s)
    return sets


def fmt_time(sec):
    """125.4 -> '2:05'."""
    sec = int(round(sec))
    return f"{sec // 60}:{sec % 60:02d}"


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        sys.exit(__doc__)
    path = Path(args[0])
    lifts = next((a.split("=", 1)[1].split(",") for a in sys.argv if a.startswith("--lifts=")), None)
    lifts = lifts or [R.lift_from_name(path.name)]
    t, a_raw = R.load_phyphox(path, R.AXIS)
    sets = find_sets(t, a_raw, lifts, *R.load_imu(path))

    if "--json" in sys.argv:
        import json
        print(json.dumps([{
            "i0": s["i0"], "i1": s["i1"], "lift": s["lift"], "mount": s["mount"], "start_t": s["start_t"], "end_t": s["end_t"],
            "rest_before": s["rest_before"], "rpe_est": s["rpe_est"],
            "reps": [{"rom": float(r["rom"]), "mcv": float(r["mcv"]), "dur": float(r["dur"])} for r in s["reps"]],
        } for s in sets]))
        return

    print(f"{path.name}: {t[-1] - t[0]:.0f} s, {len(sets)} sets\n")
    print(" set | lift     | start  | rest   | reps | best MCV | last MCV | loss | RPE est")
    for k, s in enumerate(sets, 1):
        rest = fmt_time(s["rest_before"]) if s["rest_before"] is not None else "--"
        rpe = "--" if not s["rpe_est"] else (f"<={s['rpe_est'][1]:g}" if s["rpe_est"][0] == 5 else f"{s['rpe_est'][0]:g}-{s['rpe_est'][1]:g}")
        print(f" {k:3d} | {s['lift']:8s} | {s['start_t']:5.0f}s | {rest:>6s} | {s['n_reps']:4d} | "
              f"{s['best_mcv']:8.2f} | {s['last_mcv']:8.2f} | {s['vel_loss'] * 100:3.0f}% | {rpe}")


if __name__ == "__main__":
    main()
