"""
Check a log_ble.py run in which the pod went to sleep and was picked up again (docs/ble-plan.md
step 9). The pod's side of it is the sleep prologue in docs/ble.md 6.

    python check_sleep.py data/pod-link-....json [--reason=command] [--tail-ok=N] [--no-pickup]

--no-pickup: the pod was reset, not picked up (bench command w: the prologue, then a reset), so
the new epoch has no motion to look for.

Pass:
  - the pod said it was going to sleep, with the expected reason;
  - nothing was lost to the sleep: no sleep-tail gap (or at most --tail-ok samples), and no other
    index gap either;
  - the disconnect came at once after the going_to_sleep STATUS (under 1 s: a deliberate one,
    not the 4 s supervision timeout);
  - after it, a new epoch from a rebooted pod with a new boot_id (the wake), no faults, and the
    run ended with the STOP handshake;
  - t strictly increasing over the whole file, across the sleep;
  - the first 2 s of the new epoch hold motion (the pickup that woke it reached the phone).
"""

import json
import sys
from pathlib import Path

import numpy as np

DISCONNECT_MS = 1000               # deliberate disconnect: well under the 4 s supervision timeout
MOTION_GYRO = 30.0                 # deg/s: a pickup turns the pod far more than sensor noise
MOTION_ACC = 1.5                   # m/s^2 away from g


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        sys.exit(__doc__)
    reason = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--reason=")), "command")
    tail_ok = next((int(a.split("=", 1)[1]) for a in sys.argv if a.startswith("--tail-ok=")), 0)
    pickup = "--no-pickup" not in sys.argv
    link_path = Path(args[0])
    link = json.loads(link_path.read_text())
    csv_path = link_path.with_name("bar-pod-" + link_path.stem[len("pod-link-"):] + ".csv")
    d = np.loadtxt(csv_path, delimiter=",", skiprows=1, ndmin=2)
    if d.shape[1] < 8:                     # header only: no samples at all
        d = np.zeros((0, 8))
    t, acc, gyr = d[:, 0], d[:, 1:4], d[:, 5:8]
    ok = True

    def check(cond, text):
        nonlocal ok
        ok &= bool(cond)
        print(f"{'PASS' if cond else 'FAIL'}  {text}")

    sleeps = link.get("sleeps", [])
    check(len(sleeps) == 1 and sleeps[0]["reason"] == reason,
          f"going_to_sleep with reason {reason!r}: {[s['reason'] for s in sleeps]}")
    tails = [g for g in link["gaps"] if g["kind"] == "sleep-tail"]
    others = [g for g in link["gaps"] if g["kind"] != "sleep-tail"]
    tail = sum(g["n"] for g in tails)
    check(tail <= tail_ok, f"sleep-tail {tail} samples (allowed {tail_ok})")
    check(not others, f"no other gaps: {[(g['kind'], g['n']) for g in others]}")
    if sleeps:
        ms = sleeps[0]["disconnect_after_ms"]
        check(ms is not None and ms < DISCONNECT_MS, f"disconnect {ms} ms after the going_to_sleep STATUS")
    eps = link.get("epochs", [])
    after = [e for e in eps if sleeps and e["at_s"] > sleeps[0]["at_s"]]
    woke = after[0] if after else None
    check(woke is not None and woke["reboot"] and all(woke["boot_id"] != e["boot_id"] for e in eps if e is not woke),
          f"a new epoch after the sleep from a rebooted pod with a new boot_id: {eps}")
    check(not link["faults"], f"no faults: {link['faults']}")
    # "quit" = Ctrl+C, which still runs the STOP handshake; stopped counts the sleep and that STOP
    check(link["ended"] in ("done", "quit") and link["stats"]["stopped"] >= 2,
          f"ended {link['ended']!r}, stopped {link['stats']['stopped']} (the sleep and the final STOP)")
    if len(t) < 2:
        check(False, f"only {len(t)} samples in {csv_path.name}")
    else:
        dt = np.diff(t)
        check(dt.min() > 0, f"t strictly increasing over {len(t)} samples (min dt {dt.min() * 1000:.3f} ms)")
    if woke is not None and sleeps:
        k = woke["samples_before"]
        print(f"      asleep from {sleeps[0]['at_s']:.1f} s to the reconnect at {woke['at_s']:.1f} s; "
              f"{len(t) - k} samples after the wake")
        if k < len(t) and not pickup:
            print("      (no pickup to look for: --no-pickup)")
        elif k < len(t):
            w = t[k:] < t[k] + 2.0
            g = np.abs(gyr[k:][w]).max()
            a = np.abs(np.linalg.norm(acc[k:][w], axis=1) - 9.80665).max()
            check(g > MOTION_GYRO or a > MOTION_ACC,
                  f"the first 2 s after the wake hold the pickup: max |gyro| {g:.0f} deg/s, max ||a| - g| {a:.2f} m/s^2")
        else:
            check(False, "no samples after the wake")
    print("PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
