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
"""

import json
import sys

import numpy as np

TOL_A, TOL_G, TOL_T = 6e-5, 6e-4, 3e-6
LEAD_IN = 600 + 50                 # STREAM_LIVE(600) samples from before the receiver started (+ slack)


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


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) < 2:
        sys.exit(__doc__)
    sys.exit(0 if compare(*args[:3]) else 1)
