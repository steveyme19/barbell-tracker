"""
The step 12 soak (docs/ble-plan.md step 12): a long pod session in the app with the phone locked,
checked against the pod's own USB stream, recorded at the same time.

    python check_soak.py data/soak-<stamp>/ data/<usb_clock>.json [--ring-s=S]
    python check_soak.py --selftest       # the outage rules on simulated drops and flaps (make_fake_pod)

The folder is what `node android/soak.mjs end` saved: the app's files (set CSVs, session.csv,
link.json, pod.json), its capture (pod-capture.csv), buzzes.json, vibrations.txt and soak.json. The
JSON is from `python usb_clock.py <json> --samples=<prefix>`: each boot's wall-clock origin and its
samples (<prefix>-b<k>.csv, next to the JSON).

Pass:
  - the capture replays through podlink.py to exactly what the app decoded (count and sums);
  - per epoch, compare_ble_usb.py against that boot's USB samples: every sample matched, every
    missing USB sample inside a counted gap, the pod's lost_overflow rise equal to its overflow
    gaps, no faults, no tail lost;
  - each woken pod's epoch placed within 0.2 s of the USB clock;
  - every outage came back by itself; one whose backlog fit the ring lost nothing; a sleep lost
    nothing; no index gap with the link up (each outage is listed with how long it was, the
    backlog the ring had to hold and what it lost). A gap belongs to the outage it showed up in:
    by the arrival of the frame that revealed it, not by its samples' time, so a flap at the edge
    of range doesn't take the blame for the long walk-away after it. The backlog runs from the
    last sample the phone had kept when the link went to the reconnect, so a short drop while the
    phone is still catching up from a long one may lose samples too;
  - every set buzzed with the screen off, and Android played every buzz (not ignored);
  - the app's process and its foreground service lived through the whole soak.
"""

import bisect
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

import compare_ble_usb as cbu
import podlink as pl
from log_serial import HEADER

ok = True


def check(good, msg):
    global ok
    print(("PASS  " if good else "FAIL  ") + msg)
    ok = ok and bool(good)


def replay(rows):
    """The capture through a fresh decoder, like make_fake_pod.replay, also keeping each sample's
    epoch, each boot's lost_overflow range (as log_ble.py's receiver does), the arrival of the frame
    that revealed each gap (None for one finish() records) and, per frame, (arrival, the last
    sample t the decoder had kept after it)."""
    samples, lost_by_boot, gap_arr, frames = [], {}, [], []
    s = pl.PodStream(lambda smp: samples.append((len(s.epochs) - 1, smp)), lambda k, i: None)
    for kind, arr, payload in rows:
        n0 = len(s.gaps)
        if kind == "frame":
            b = bytes.fromhex(payload)
            s.frame(b, arr)
            frames.append((arr, s.last_t))
            ep = s.ep
            if b[:1] == bytes([pl.T_STATUS]) and ep is not None and ep.status is not None and not s.ignoring:
                rng = lost_by_boot.setdefault(str(ep.boot_id), [ep.status["lost_overflow"]] * 2)
                rng[1] = ep.status["lost_overflow"]
        elif kind == "cmd":
            b = s.command()
            assert ("none" if b is None else b.hex()) == payload, f"replayed command differs from the app's {payload}"
        elif kind == "stop":
            assert s.stop_command().hex() == payload, "replayed STOP differs"
        elif kind == "call":
            if payload == "finish":
                s.finish()
            elif payload.startswith("stop_wait_ms="):
                s.stop_wait_ms()
            elif payload.startswith("stop_timed_out="):
                s.stop_timed_out()
        gap_arr += [arr if kind == "frame" else None] * (len(s.gaps) - n0)
    return samples, s, lost_by_boot, gap_arr, frames


def outages(s, gap_arr, frames, events):
    """link.json's outages (a "lost" or "asleep", then the "back" after it), each with its index
    gaps and the backlog its ring had to hold. A gap goes to the outage it showed up in: its frame
    arrived in (the previous outage's end or 3 s before this one began, this one's back + 2 s]
    (a sleep-tail comes just before "asleep", a reconnect's overflow with the first DATA after
    "back"). Returns (outages, stray gaps: index gaps outside every outage)."""
    arr_l = [a for a, _ in frames]
    outs = []
    for k, e in enumerate(events):
        if e["kind"] in ("lost", "asleep"):
            back = next((b["at"] for b in events[k + 1:] if b["kind"] == "back"), None)
            outs.append({"kind": e["kind"], "reason": e.get("reason"), "at": e["at"], "back": back})
    idx = [(g, a) for g, a in zip(s.gaps, gap_arr) if not g["kind"].startswith("imu") and a is not None]
    taken = set()
    prev_end = float("-inf")
    for o in outs:
        end = (o["back"] if o["back"] is not None else float("inf")) + 2000
        start = max(prev_end, o["at"] - 3000)
        o["gaps"] = [g for g, a in idx if start < a <= end]
        taken.update(id(g) for g in o["gaps"])
        prev_end = end
        j = bisect.bisect_right(arr_l, o["at"]) - 1          # the last frame before "lost"
        kept = frames[j][1] if j >= 0 else None
        o["backlog_s"] = (o["back"] - s.wall_at(kept)) / 1000 if o["back"] is not None and kept is not None else None
    return outs, [g for g, _ in idx if id(g) not in taken]


def judge(o, ring_s):
    """(good, why) for one outage from outages()."""
    n = sum(g["n"] for g in o["gaps"])
    if o["back"] is None:
        return False, "never came back"
    if o["kind"] == "asleep":
        return n == 0, "a sleep drains the ring first: nothing may be lost"
    if o["backlog_s"] is not None and o["backlog_s"] < ring_s - 2:
        return n == 0, f"the ring ({ring_s:.1f} s) held the backlog: nothing may be lost"
    return True, ("longer than the ring: what it lost must be counted, which compare_ble_usb checked" if n
                  else "the ring held it")


def usb_boots(clock_path):
    """The USB boots, reopens of one boot merged (bases within 50 ms): [(base_s, [csv paths])]."""
    j = json.loads(Path(clock_path).read_text())
    out = []
    for b in j["boots"]:
        path = Path(clock_path).parent / b["samples_csv"] if b.get("samples_csv") else None
        if out and abs(b["base_s"] - out[-1][0]) <= 0.05:
            out[-1][1].append(path)
        else:
            out.append((b["base_s"], [path]))
    return out, j["wrap_s"]


def vib_time(line, year):
    """'10-25 23:34:05.025 | effect | finished | ...' -> wall ms (local time)."""
    stamp = line.strip()[:18]
    return datetime.strptime(f"{year}-{stamp}", "%Y-%m-%d %H:%M:%S.%f").timestamp() * 1000


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) < 2:
        sys.exit(__doc__)
    folder, clock = Path(args[0]), Path(args[1])
    ring_s = next((float(a.split("=", 1)[1]) for a in sys.argv if a.startswith("--ring-s=")), None)
    soak = json.loads((folder / "soak.json").read_text())
    link = json.loads((folder / "link.json").read_text())
    print(f"soak {soak['stamp']}: {(soak['endedAt'] - soak['startedAt']) / 60000:.1f} min, "
          f"battery {soak['battery']}% -> {soak['batteryEnd']}%")

    # 1. the capture against what the app decoded
    rows = [(r["kind"], float(r["arrival_ms"]) if r["arrival_ms"] else None, r["payload"])
            for r in csv.DictReader(open(folder / "pod-capture.csv"))]
    samples, s, lost_by_boot, gap_arr, frames = replay(rows)
    if ring_s is None:                           # the pod's own ring, from its INFO (8192 samples ~ 39 s)
        ring_s = s.epochs[0].ring_n / s.epochs[0].odr
    keys = ["t", "ax", "ay", "az", "gx", "gy", "gz"]
    sums = {k: 0.0 for k in keys}
    for _, smp in samples:
        for k in keys:
            sums[k] += smp[k]
    d = soak["digest"]
    check(len(samples) == d["n"] and all(sums[k] == d["sum"][k] for k in keys),
          f"podlink.py replays the app's capture to the same {len(samples)} samples (app {d['n']}), sums equal bit for bit")

    # 2. per epoch, against the USB truth
    boots, wrap_s = usb_boots(clock)
    print(f"{len(s.epochs)} epochs in the app, {len(boots)} boots on USB")
    tmp = folder / "per-epoch"
    tmp.mkdir(exist_ok=True)
    placed = []                                  # (epoch, its USB boot's base_s)
    for e, ep in enumerate(s.epochs):
        mine = [smp for k, smp in samples if k == e]
        if not mine:
            print(f"      epoch {e + 1}: no samples")
            continue
        ble = tmp / f"ble-e{e + 1}.csv"
        with open(ble, "w", newline="") as f:
            f.write(HEADER + "\n")
            for m in mine:
                a = (m["ax"] ** 2 + m["ay"] ** 2 + m["az"] ** 2) ** 0.5
                f.write(f"{m['t']:.6f},{m['ax']:.6f},{m['ay']:.6f},{m['az']:.6f},{a:.6f},{m['gx']:.5f},{m['gy']:.5f},{m['gz']:.5f}\n")
        gaps = [g for g in s.gaps if g["boot_id"] == ep.boot_id]
        lj = tmp / f"link-e{e + 1}.json"
        lj.write_text(json.dumps({"samples": len(mine), "ended": "done", "gaps": gaps, "faults": s.faults, "stats": s.stats,
                                  "lost_overflow_by_boot": {str(ep.boot_id): lost_by_boot.get(str(ep.boot_id), [None, None])}}))
        # its USB boot: the one a BLE sample has a twin in
        bt, ba, bg = cbu.load(ble)
        hit = None
        for base, paths in boots:
            merged = tmp / f"usb-{base:.3f}.csv"
            if not merged.exists():
                with open(merged, "w", newline="") as f:
                    for k, p in enumerate(paths):
                        lines = open(p).read().splitlines()
                        f.write("\n".join(lines if k == 0 else lines[1:]) + "\n")
            ut, ua, ug = cbu.load(merged)
            if cbu.align(ut, ua, ug, bt, ba, bg) is not None:
                hit = (base, merged)
                break
        if hit is None:
            check(False, f"epoch {e + 1} (boot {ep.boot_id}): no USB boot has its samples (was usb_clock.py running?)")
            continue
        print(f"---- epoch {e + 1} (boot {ep.boot_id}{', a woken pod' if ep.is_reboot else ''}) against USB {hit[1].name}")
        good = cbu.compare(str(hit[1]), str(ble), str(lj))
        check(good, f"epoch {e + 1}: compare_ble_usb.py")
        placed.append((ep, hit[0]))

    # 3. each new epoch's placement against the USB clock (docs/ble-plan.md step 10)
    for (e1, b1), (e2, b2) in zip(placed, placed[1:]):
        if e2.t_offset is None or e1.t_offset is None:
            continue
        at = e2.t_offset - e1.t_offset
        mod = b2 - b1
        wraps = round((at - mod) / wrap_s)
        truth = mod + wraps * wrap_s
        check(abs(at - truth) <= 0.2, f"boot {e2.boot_id} placed {at:.3f} s after boot {e1.boot_id}; the USB clock says "
                                      f"{truth:.3f} s: error {(at - truth) * 1000:.1f} ms")

    # 4. the outages: each came back by itself; what each lost
    t0 = soak["startedAt"]
    outs, stray = outages(s, gap_arr, frames, link["linkEvents"])
    for o in outs:
        good, why = judge(o, ring_s)
        n = sum(g["n"] for g in o["gaps"])
        dur = "never back" if o["back"] is None else f"back {(o['back'] - o['at']) / 1000:.1f} s later"
        backlog = "" if o["backlog_s"] is None else f", backlog {o['backlog_s']:.1f} s"
        check(good, f"{o['kind']}{' (' + o['reason'] + ')' if o['reason'] else ''} at {(o['at'] - t0) / 1000:.0f} s: "
                    f"{dur}{backlog}, lost {n} ({', '.join(g['kind'] + ' ' + str(g['n']) for g in o['gaps']) or 'nothing'}) [{why}]")
    check(not stray, f"no index gap with the link up ({', '.join(g['kind'] + ' ' + str(g['n']) for g in stray) or 'none'})")

    # 5. sets and buzzes, screen off; Android played them
    sets = []                                    # the app writes session.csv with the first set
    if (folder / "session.csv").exists():
        with open(folder / "session.csv") as f:
            sets = list(csv.DictReader(f))
    buzzes = json.loads((folder / "buzzes.json").read_text())
    done = [b for b in buzzes if b["pattern"] == [100, 80, 100] and t0 <= b["at"] <= soak["endedAt"]]   # "a set finished"
    check(len(done) == len(sets) and all(b["hidden"] for b in done),
          f"{len(sets)} sets, {len(done)} 'set finished' buzzes, {sum(b['hidden'] for b in done)} of them with the screen off")
    year = datetime.fromtimestamp(t0 / 1000).year
    vib = [(vib_time(l, year), l.split("|")[2].strip()) for l in (folder / "vibrations.txt").read_text().splitlines() if "|" in l]
    played = []
    for b in [b for b in buzzes if t0 <= b["at"] <= soak["endedAt"]]:
        v = min(vib, key=lambda x: abs(x[0] - b["at"]), default=None)
        played.append(v[1] if v and abs(v[0] - b["at"]) < 3000 else "not in Android's list")
    check(played and all(p == "finished" for p in played),
          f"Android on the {len(played)} buzzes: {', '.join(f'{played.count(p)} {p}' for p in sorted(set(played)))}"
          if played else "no buzzes to check (no sets?)")

    # 6. the app itself
    check(soak["pidEnd"] == soak["pid"] and soak["fgsAtEnd"],
          f"the app's process lived through it (pid {soak['pid']} -> {soak['pidEnd']}), foreground service at the end {soak['fgsAtEnd']}")
    print("PASS" if ok else "FAIL")
    return ok


def selftest():
    """outages() and judge() on simulated links (make_fake_pod's bench: the pod, its ring, a radio
    with latency), the app's linkEvents made up the way PodNativeSource writes them: "lost" when
    the link is noticed gone (4 s, the supervision timeout), "back" once the reconnect's command is out."""
    import make_fake_pod as m

    def scenario(name, steps, seed):
        b = m.Bench(name, seed=seed)
        b.run(3000)
        b.connect()
        ev = []
        for kind, ms in steps:
            if kind == "up":
                b.run(ms)
            else:                                # down for ms: lost after 4 s, then the reconnect
                b.disconnect()
                b.run(min(4000, ms))
                ev.append({"kind": "lost", "at": b.now})
                b.run(max(0, ms - 4000))
                b.connect()
                ev.append({"kind": "back", "at": b.now})
        b.run(15000)
        b.stop()
        b.run(3000)
        b.finish()
        _, s, _, gap_arr, frames = replay(b.log)
        outs, stray = outages(s, gap_arr, frames, ev)
        ring_s = s.epochs[0].ring_n / s.epochs[0].odr
        return outs, stray, ring_s, sum(p.lost for p in b.pods)

    cases = [
        # (name, steps, the samples each outage must be blamed for: None = all the pod lost)
        ("20 s outage", [("up", 20000), ("down", 20000)], [0]),
        ("90 s outage", [("up", 20000), ("down", 90000)], [None]),
        ("a flap on the way out: 10 s, 300 ms up, then 90 s", [("up", 20000), ("down", 10000), ("up", 300), ("down", 90000)], [0, None]),
        ("a flap on the way back: 90 s, 1 s up while catching up, 6 s", [("up", 20000), ("down", 90000), ("up", 1000), ("down", 6000)], None),
    ]
    for k, (name, steps, want) in enumerate(cases):
        outs, stray, ring_s, pod_lost = scenario(name, steps, seed=40 + k)
        got = [sum(g["n"] for g in o["gaps"]) for o in outs]
        verdicts = [judge(o, ring_s) for o in outs]
        exp = [pod_lost if w is None else w for w in want] if want else None
        good = (not stray and sum(got) == pod_lost and all(v[0] for v in verdicts) and (exp is None or got == exp))
        check(good, f"{name}: the pod lost {pod_lost}, blamed {got}, backlogs "
                    f"{[None if o['backlog_s'] is None else round(o['backlog_s'], 1) for o in outs]} s, "
                    f"verdicts {['ok' if v[0] else 'FAIL: ' + v[1] for v in verdicts]}, stray {len(stray)}")
    print("PASS" if ok else "FAIL")
    return ok


if __name__ == "__main__":
    sys.exit(0 if (selftest() if "--selftest" in sys.argv else main()) else 1)
