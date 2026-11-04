"""
Roadmap 1.4's proof: a Bluetooth recording (log_ble.py) against a USB recording of the same pod
at the same time (log_serial.py). Same samples, same device timestamps, and nothing missing
except counted gaps, each sitting exactly where the USB timeline has those samples.

    python compare_ble_usb.py data/bar-xiao-....csv data/bar-pod-....csv [data/pod-link-....json]

Both files store t relative to their own first sample, so it first finds the one USB sample
that is a BLE sample's twin (same values, and the next 50 samples' values and time steps the
same, to print precision and 2 us), then matches every sample by time. Pass (ble-plan.md 5, T3/T4):
  - the USB log brackets the BLE file (only the STREAM_LIVE lead-in may predate it);
  - every BLE sample matches one USB sample: |dt| <= 3 us, |da| <= 6e-5 m/s^2, |dg| <= 6e-4 deg/s
    (USB prints 4 and 3 decimals);
  - every USB sample inside the BLE span is in the BLE file, or inside a counted index gap whose
    count equals the USB samples in that hole;
  - with the link JSON (strongly advised, without it any loss fails): the file holds every sample
    the receiver decoded, the stream ended with the pod's stream_stopped (no tail lost, no stop
    timeout), no faults, pkt_seq gaps 0, no unexplained gaps, and for each boot the pod's own
    lost_overflow counter rose by exactly the samples its overflow gaps count.

    python compare_ble_usb.py --exact data/pod-capture-A.csv data/pod-capture-B.csv

--exact: PodStream against PodStream, both sides unrounded (D45: say log_ble.py over Bluetooth and
log_ble.py --usb, run at the same time). Each argument is a run's capture, or its bar-pod CSV or
pod-link JSON (the capture beside it is used). Both captures are replayed through a fresh decoder
(podlink.py), every sample keyed by (boot_id, index), so there is nothing to align. Pass:
  - each capture replays (every logged command is the one the decoder builds) and, where its
    pod-link JSON is there, to exactly what its receiver decoded live (count and gaps);
  - the same pod (INFO serial), and samples in common;
  - per side and boot, the kept indices plus the counted index gaps cover one range exactly once:
    whatever a side lacks that the other has is a counted gap of its own;
  - every sample both kept is bit for bit the same, and per boot t differs by one constant (to
    1 ns: the same ticks, another tOffset);
  - no faults, no unexplained gaps, pkt_seq gaps 0, on both sides.
"""

import json
import sys
from pathlib import Path

import numpy as np

TOL_A, TOL_G, TOL_T = 6e-5, 6e-4, 3e-6
LEAD_IN = 600 + 50                 # STREAM_LIVE(600) samples from before the receiver started (+ slack)
EXACT_T = 1e-9                     # --exact: the per-boot t offset holds to float rounding
VALUES = ("ax", "ay", "az", "gx", "gy", "gz")


def load(path):
    d = np.loadtxt(path, delimiter=",", skiprows=1, usecols=(0, 1, 2, 3, 5, 6, 7), ndmin=2)
    return d[:, 0], d[:, 1:4], d[:, 4:7]


def align(ut, ua, ug, bt, ba, bg, w=50):
    """Offset that maps BLE t onto USB t, or None. BLE samples rounded to USB's print precision
    are looked up among the USB samples; a candidate counts only if the next w samples agree in
    values and time steps, and only a unique one is taken."""
    key = lambda a, g: tuple(np.round(a, 4)) + tuple(np.round(g, 3))
    where = {}
    for i in range(len(ut)):
        where.setdefault(key(ua[i], ug[i]), []).append(i)
    for k in range(len(bt) - w):
        good = [i for i in where.get(key(ba[k], bg[k]), [])
                if i + w <= len(ut)
                and np.abs(ua[i:i + w] - ba[k:k + w]).max() <= TOL_A
                and np.abs(ug[i:i + w] - bg[k:k + w]).max() <= TOL_G
                and np.abs(np.diff(ut[i:i + w]) - np.diff(bt[k:k + w])).max() <= 2e-6]
        if len(good) == 1:
            return ut[good[0]] - bt[k]
    return None


def runs(idx):
    """Sorted integer array -> [(first, count)] of consecutive runs."""
    out = []
    for i in idx:
        if out and i == out[-1][0] + out[-1][1]:
            out[-1][1] += 1
        else:
            out.append([int(i), 1])
    return [tuple(r) for r in out]


def compare(usb_path, ble_path, link_path=None):
    ok = True

    def check(good, msg):
        nonlocal ok
        print(("PASS  " if good else "FAIL  ") + msg)
        ok = ok and bool(good)

    ut, ua, ug = load(usb_path)
    bt, ba, bg = load(ble_path)
    print(f"USB {len(ut)} samples over {ut[-1] - ut[0]:.1f} s; BLE {len(bt)} samples over {bt[-1] - bt[0]:.1f} s")
    off = align(ut, ua, ug, bt, ba, bg)
    if off is None:
        check(False, "no BLE sample has a USB twin: not the same pod and time?")
        return False
    target = bt + off
    lo, hi = max(ut[0], target[0]), min(ut[-1], target[-1])
    inb = (target >= lo - TOL_T) & (target <= hi + TOL_T)
    print(f"overlap {hi - lo:.1f} s ({inb.sum()} BLE samples); BLE t + {off:.6f} s = USB t")
    before = int((target < ut[0] - TOL_T).sum())
    after = int((target > ut[-1] + TOL_T).sum())
    check(before <= LEAD_IN and after == 0,
          f"the USB log brackets the BLE file: {before} BLE samples before it (lead-in, at most {LEAD_IN}), {after} after it")

    # each BLE sample in the overlap -> its USB twin by time
    j = np.clip(np.searchsorted(ut, target), 1, len(ut) - 1)
    j = np.where(np.abs(ut[j - 1] - target) < np.abs(ut[j] - target), j - 1, j)
    dt = np.abs(ut[j] - target)
    hit = inb & (dt <= TOL_T)
    da = np.abs(ua[j] - ba).max(1)
    dg = np.abs(ug[j] - bg).max(1)
    extra = int((inb & ~hit).sum())
    check(extra == 0 and len(set(j[hit])) == int(hit.sum()),
          f"{int(hit.sum())} BLE samples matched one USB sample each, {extra} without a twin")
    check(da[hit].max() <= TOL_A and dg[hit].max() <= TOL_G and dt[hit].max() <= TOL_T,
          f"worst |da| {da[hit].max():.2e} m/s^2, |dg| {dg[hit].max():.2e} deg/s, |dt| {dt[hit].max() * 1e6:.2f} us")

    # USB samples inside the BLE span that BLE doesn't have: they must be counted gaps
    span = np.where((ut >= target[0] - TOL_T) & (ut <= target[-1] + TOL_T))[0]
    seen = np.zeros(len(ut), bool)
    seen[j[hit]] = True
    missing = runs(span[~seen[span]])
    holes = [(ut[i] - off, n) for i, n in missing]            # in BLE time: (t of the first missing, n)
    print(f"USB samples missing from BLE: {sum(n for _, n in missing)} in {len(missing)} run(s)")

    if link_path:
        link = json.load(open(link_path))
        check(link["samples"] == len(bt), f"the BLE file holds every sample the receiver decoded ({len(bt)} of {link['samples']})")
        tails = [g for g in link["gaps"] if g["kind"].endswith("-tail")]
        check(link["stats"]["stopped"] >= 1 and not tails and "stop timed out" not in link["faults"]
              and link.get("ended", "done") in ("done", "quit"),
              f"ended cleanly: {link.get('ended', '?')}, stopped {link['stats']['stopped']}, tail gaps {tails}")
        gaps = [g for g in link["gaps"] if not g["kind"].startswith("imu") and not g["kind"].endswith("-tail")]
        used = set()
        for t_first, n in holes:
            g = next((k for k, g in enumerate(gaps) if k not in used and g["t0"] is not None and g["t1"] is not None
                      and g["t0"] < t_first < g["t1"] and g["n"] == n), None)
            check(g is not None, f"the {n} samples missing at t {t_first:.3f} s are a counted gap "
                                 f"({gaps[g]['kind']}, n {gaps[g]['n']})" if g is not None else
                                 f"the {n} samples missing at t {t_first:.3f} s match no counted gap")
            if g is not None:
                used.add(g)
        inside = [g for k, g in enumerate(gaps) if k not in used and g.get("t1") is not None and g["t1"] <= target[-1] - off]
        check(not inside, f"every counted gap inside the span is a USB hole too (left over: {inside})")
        st = link["stats"]
        check(not link["faults"] and st["pkt_seq_gaps"] == 0 and not any(g["kind"] == "unexplained" for g in link["gaps"]),
              f"faults {link['faults']}, pkt_seq gaps {st['pkt_seq_gaps']}, reconnects {st['reconnects']}")
        by_boot = link.get("lost_overflow_by_boot")
        if by_boot is None:                          # an older JSON: one range for the whole run
            by_boot = {"all": link["pod_lost_overflow"]}
        for boot, (a, b) in by_boot.items():
            over = sum(g["n"] for g in gaps if g["kind"] == "overflow" and (boot == "all" or str(g["boot_id"]) == boot))
            check(a is not None and b is not None and b - a == over,
                  f"boot {boot}: the pod's lost_overflow rose {None if a is None or b is None else b - a}, its overflow gaps count {over}")
    else:
        check(not missing, "no samples missing (no link JSON to explain any)")
    print("PASS" if ok else "FAIL")
    return ok


# ---- --exact: PodStream against PodStream ----
def capture_of(path):
    """A run's capture from any of its files: pod-capture-<tag>.csv itself, or bar-pod-<tag>.csv /
    pod-link-<tag>.json beside it (log_ble.py's names). Anything else is taken as a capture."""
    p = Path(path)
    for pre, ext in (("bar-pod-", ".csv"), ("pod-link-", ".json")):
        if p.name.startswith(pre) and p.suffix == ext:
            return p.with_name("pod-capture-" + p.stem[len(pre):] + ".csv")
    return p


def replay_keyed(path):
    """A capture through a fresh decoder, by podlink.replay_rows' rules (pkt and frame rows fed,
    cmd rows checked against command(), stop and call rows replayed, the rest skipped), each
    sample keyed by (boot_id, index). Returns ({key: sample}, the decoder, keys seen twice)."""
    import podlink as pl
    keyed, twice = {}, [0]
    s = None

    def on_sample(smp):
        k = (s.ep.boot_id, s.ep.last_idx)          # last_idx is set before on_sample is called
        twice[0] += k in keyed
        keyed[k] = smp
    s = pl.PodStream(on_sample)
    for kind, arr, payload in pl.read_capture(path):
        if kind == "pkt":
            s.packet(bytes.fromhex(payload), arr)
        elif kind == "frame":
            s.frame(bytes.fromhex(payload), arr)
        elif kind == "cmd":
            b = s.command()
            if ("none" if b is None else b.hex()) != payload:
                raise ValueError(f"replayed command {'none' if b is None else b.hex()} != logged {payload}")
        elif kind == "stop":
            s.stop_command()
        elif kind == "call":
            if payload == "finish":
                s.finish()
            elif payload.startswith("stop_timed_out="):
                s.stop_timed_out()
    return keyed, s, twice[0]


def tiling(keyed, s):
    """Per boot: the kept indices plus the index gaps (every kind but imu holes) must cover one
    range exactly once. Returns ({boot: (lo, hi)}, [problems])."""
    spans, bad = {}, []
    for boot in sorted({b for b, _ in keyed} | {g["boot_id"] for g in s.gaps if not g["kind"].startswith("imu")}):
        cover = [(i, i + 1, "sample") for b, i in keyed if b == boot]
        cover += [(g["idx"], g["idx"] + g["n"], g["kind"]) for g in s.gaps
                  if g["boot_id"] == boot and not g["kind"].startswith("imu") and g["n"] > 0]
        cover.sort()
        if not cover:
            continue
        at = cover[0][0]
        for a, z, what in cover:
            if a != at:
                bad.append(f"boot {boot}: at index {at} the next is {what} from {a}"
                           + (" (a silent hole)" if a > at else " (an overlap)"))
                break
            at = z
        spans[boot] = (cover[0][0], at)
    return spans, bad


def exact(a_path, b_path):
    ok = True

    def check(good, msg):
        nonlocal ok
        print(("PASS  " if good else "FAIL  ") + msg)
        ok = ok and bool(good)

    sides = []
    for path in (a_path, b_path):
        cap = capture_of(path)
        try:
            keyed, s, twice = replay_keyed(cap)
        except (OSError, ValueError, KeyError) as e:
            check(False, f"{cap.name} doesn't replay: {e}")
            return False
        spans, bad = tiling(keyed, s)
        print(f"{cap.name}: {len(keyed)} samples, {len(s.epochs)} epoch(s), "
              f"gaps {[(g['kind'], g['n']) for g in s.gaps]}, spans {spans}")
        check(not twice, f"{cap.name}: every (boot, index) kept once ({twice} twice: a reboot without a new boot_id?)")
        link = cap.with_name("pod-link-" + cap.stem[len("pod-capture-"):] + ".json")
        if cap.name.startswith("pod-capture-") and link.exists():
            j = json.loads(link.read_text())
            check(j["samples"] == len(keyed) and j["gaps"] == json.loads(json.dumps(s.gaps)),
                  f"{cap.name}: the replay is what the receiver decoded live ({len(keyed)} samples, "
                  f"{link.name} says {j['samples']}; gaps {'equal' if j['gaps'] == json.loads(json.dumps(s.gaps)) else 'differ'})")
        check(not bad, f"{cap.name}: kept indices + counted gaps cover each boot exactly once" + (f": {bad}" if bad else ""))
        check(not s.faults and s.stats["pkt_seq_gaps"] == 0 and not any(g["kind"] == "unexplained" for g in s.gaps),
              f"{cap.name}: faults {s.faults}, pkt_seq gaps {s.stats['pkt_seq_gaps']}, "
              f"reconnects {s.stats['reconnects']}, stopped {s.stats['stopped']}")
        sides.append((cap, keyed, s, spans))

    (ca, ka, sa, spa), (cb, kb, sb, spb) = sides
    check(sa.serial is not None and sa.serial == sb.serial,
          f"the same pod: serial {sa.serial and hex(sa.serial)} and {sb.serial and hex(sb.serial)}")
    both = sorted(set(ka) & set(kb))
    check(len(both) > 0, f"{len(both)} samples in both, over {len({b for b, _ in both})} boot(s)")
    diff = [k for k in both if tuple(ka[k][v] for v in VALUES) != tuple(kb[k][v] for v in VALUES)]
    check(not diff, f"every sample in both is bit for bit the same ({len(diff)} differ"
                    + (f", first at boot {diff[0][0]} index {diff[0][1]})" if diff else ")"))
    for boot in sorted({b for b, _ in both}):
        dts = [kb[k]["t"] - ka[k]["t"] for k in both if k[0] == boot]
        dev = max(abs(d - dts[0]) for d in dts)
        check(dev <= EXACT_T, f"boot {boot}: t({cb.name}) = t({ca.name}) + {dts[0]:.9f} s over {len(dts)} samples "
                              f"(worst deviation {dev * 1e9:.3f} ns)")
        # what one side lacks inside both spans: by the tiling check, its own counted gaps
        lo = max(spa[boot][0], spb[boot][0])
        hi = min(spa[boot][1], spb[boot][1])
        for name, mine, other, s in ((ca.name, ka, kb, sa), (cb.name, kb, ka, sb)):
            lack = [i for i in range(lo, hi) if (boot, i) not in mine and (boot, i) in other]
            if lack:
                kinds = sorted({g["kind"] for g in s.gaps if g["boot_id"] == boot and g["idx"] <= lack[-1]
                                and g["idx"] + g["n"] > lack[0] and not g["kind"].startswith("imu")})
                print(f"      boot {boot}: {name} lacks {len(lack)} samples the other has, counted as {kinds}")
    print("PASS" if ok else "FAIL")
    return ok


if __name__ == "__main__":
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        sys.exit(0)
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) < 2:
        sys.exit(__doc__)
    if "--exact" in sys.argv:
        sys.exit(0 if exact(*args[:2]) else 1)
    sys.exit(0 if compare(*args[:3]) else 1)
