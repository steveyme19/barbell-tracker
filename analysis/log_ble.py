"""
Record the pod over Bluetooth: roadmap 1.4's receiver (docs/ble.md; docs/ble-plan.md step 7).
The laptop does what the phone will: every frame goes through PodStream (podlink.py, the
reference decoder), a dropout resumes from the pod's 38 s ring with STREAM_FROM, and nothing is
ever filled in: a sample that can't come back is a counted, located gap.

    python log_ble.py                                    # Ctrl+C stops (with the STOP handshake)
    python log_ble.py --seconds=600 --name=proof
    python log_ble.py --seconds=420 --outage=3@40 --outage=60@300   # drop the link: 3 s at 40 s, ...
    python log_ble.py --seconds=300 --sleep=15 --until-wake=15      # step 9: sleep, pickup, new epoch

--outage=S@T disconnects on purpose T s into the run and reconnects S s later (the outage
matrix: the pod's side is the same as a real dropout, docs/ble.md 5). Times count from the start.
--sleep=T[:mode] sends the SLEEP op T s in (mode 0 = wake on motion, 1 = IMU off). After any
sleep the receiver keeps trying to reconnect, so a pickup lands in a new epoch;
--until-wake=S ends the run S s after the first sample from a pod that rebooted (woke).
Check a sleep run with check_sleep.py.

Saves, in data/ (P2), and never over an earlier run (a -2, -3 suffix in the same minute):
  bar-pod-<stamp>[-name].csv       the samples, same columns as log_serial.py and the app
  pod-capture-<stamp>[-name].csv   every frame and command as it happens (make_fake_pod.replay())
  pod-link-<stamp>[-name].json     gaps, faults, stats, how it ended, the pod's loss counter per boot
Compare a run with a simultaneous USB log: compare_ble_usb.py. Needs bleak (requirements.txt);
Bluetooth headphones off (they starve the laptop's scanning, D28).
"""

import asyncio
import json
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

from bleak import BleakClient, BleakScanner

import podlink as pl

HEADER = ('"Time (s)","Acceleration x (m/s^2)","Acceleration y (m/s^2)","Acceleration z (m/s^2)",'
          '"Absolute acceleration (m/s^2)","Gyro x (deg/s)","Gyro y (deg/s)","Gyro z (deg/s)"')
BACKOFF = (0.5, 1, 2, 4)           # s between reconnect tries, then every 5 s (ble-plan.md 3)
FIRST_SAMPLE_S = 10                # no sample this long after a connect: the run fails, like the app
MTU_FAULT_S = 3                    # mtu_too_small still set this long after connect: a fault (ble.md 5)


def now_ms():
    return time.time() * 1000      # the arrival clock: wall time, like the browser's Date.now()


def new_path(folder, prefix, tag, ext):
    """data/<prefix>-<tag>.<ext>, or -2, -3... if a run in the same minute already used it."""
    p = folder / f"{prefix}-{tag}.{ext}"
    k = 2
    while p.exists():
        p = folder / f"{prefix}-{tag}-{k}.{ext}"
        k += 1
    return p


class Receiver:
    def __init__(self, csv_path, cap_path):
        self.f = open(csv_path, "x", newline="")
        self.f.write(HEADER + "\n")
        self.cap = open(cap_path, "x", newline="")       # written as it happens: a crash keeps it
        self.cap.write("scenario,step,kind,arrival_ms,payload\n")
        self.step = 0
        self.stream = pl.PodStream(self.on_sample, self.on_event)
        self.client = None
        self.dev = None
        self.linked = False            # connected and set up
        self.lost_link = asyncio.Event()
        self.stopped = asyncio.Event()
        self.n = 0
        self.pending = []              # commands the decoder asked for (need-command / need-info)
        self.mtu_small_since = None
        self.connected_at = None
        self.n_at_connect = 0
        self.lost_by_boot = {}         # boot_id -> [first, last] lost_overflow the pod reported
        self.ended = "running"         # how the run ended, for the JSON
        self.t0 = time.time()
        self.sleeps = []               # per going_to_sleep STATUS: reason, when, the disconnect after it
        self.epochs = []               # per epoch: boot_id, reboot, samples before it, when
        self.woke_at = None            # time of the first sample of a rebooted (woken) pod

    # ---- the decoder's side ----
    def on_sample(self, s):
        if self.woke_at is None and self.epochs and self.epochs[-1]["reboot"]:
            self.woke_at = time.time()
        self.n += 1
        a = (s["ax"] ** 2 + s["ay"] ** 2 + s["az"] ** 2) ** 0.5
        self.f.write(f"{s['t']:.6f},{s['ax']:.6f},{s['ay']:.6f},{s['az']:.6f},{a:.6f},"
                     f"{s['gx']:.5f},{s['gy']:.5f},{s['gz']:.5f}\n")

    def on_event(self, kind, info):
        if kind == "stopped":
            self.stopped.set()
        elif kind in ("need-command", "need-info"):
            self.pending.append(kind)
        elif kind == "mtu-too-small":
            if self.mtu_small_since is None:
                self.mtu_small_since = time.time()
        elif kind in ("fault", "gap", "epoch", "sleep"):
            print(f"      {kind}: {info}", flush=True)
        if kind == "sleep":
            self.sleeps.append({"reason": info, "at_s": round(time.time() - self.t0, 3), "samples": self.n,
                                "disconnect_after_ms": None})
        elif kind == "epoch":
            self.epochs.append({"boot_id": info["boot_id"], "reboot": info["reboot"], "samples_before": self.n,
                                "at_s": round(time.time() - self.t0, 3)})

    def log(self, kind, arrival, payload):
        self.cap.write(f"capture,{self.step},{kind},{arrival},{payload}\n")
        self.step += 1

    def frame(self, data):
        t = now_ms()
        b = bytes(data)
        self.log("frame", t, b.hex())
        self.stream.frame(b, t)
        ep = self.stream.ep
        if b[:1] == bytes([pl.T_STATUS]) and ep is not None and ep.status is not None and not self.stream.ignoring:
            lost = ep.status["lost_overflow"]
            rng = self.lost_by_boot.setdefault(str(ep.boot_id), [lost, lost])
            rng[1] = lost
            if not ep.status["flags"] & pl.S_MTU_TOO_SMALL:
                self.mtu_small_since = None

    # ---- the link ----
    def on_disconnect(self, _client):
        self.linked = False
        self.lost_link.set()
        sl = self.sleeps[-1] if self.sleeps else None
        if sl and sl["disconnect_after_ms"] is None:       # the pod's own disconnect after going_to_sleep
            sl["disconnect_after_ms"] = round((time.time() - self.t0 - sl["at_s"]) * 1000)

    async def command(self):
        b = self.stream.command()
        self.log("cmd", "", "none" if b is None else b.hex())
        if b is None:
            raise RuntimeError(f"the decoder won't stream: {self.stream.faults[-1:]}")
        await self.client.write_gatt_char(pl.CTRL_UUID, b, response=True)

    async def connect(self, address):
        """Find the pod, connect and run the connection order (docs/ble.md 7). One try."""
        if address:
            self.dev = await BleakScanner.find_device_by_address(address, timeout=10)
        else:
            self.dev = await BleakScanner.find_device_by_filter(
                lambda d, adv: pl.SERVICE in [u.lower() for u in adv.service_uuids], timeout=10)
        if self.dev is None:
            raise RuntimeError("no BarPod found")
        self.lost_link.clear()
        self.client = BleakClient(self.dev, disconnected_callback=self.on_disconnect)
        await self.client.connect()
        self.frame(await self.client.read_gatt_char(pl.INFO_UUID))
        self.frame(await self.client.read_gatt_char(pl.STATUS_UUID))
        await self.client.start_notify(pl.DATA_UUID, lambda _s, d: self.frame(d))   # STATUS too (D28)
        self.pending.clear()           # the connection order's own command answers any request
        await self.command()
        self.linked = not self.lost_link.is_set()
        self.connected_at = time.time()
        self.n_at_connect = self.n
        return self.dev.address

    async def sleep_command(self, mode):
        b = bytes([pl.OP_SLEEP, mode])
        self.log("ctrl", "", b.hex())   # not "cmd": replay() checks those against command()
        await self.client.write_gatt_char(pl.CTRL_UUID, b, response=True)

    async def drop(self):
        self.linked = False
        try:
            await self.client.disconnect()
        except Exception:
            pass

    async def reconnect(self, address, give_up):
        """Back to streaming after a dropout: the same PodStream, so STREAM_FROM(next index)."""
        for k in range(10000):
            await asyncio.sleep(BACKOFF[k] if k < len(BACKOFF) else 5)
            if give_up():
                return False
            try:
                await self.connect(address)
                print(f"      reconnected (try {k + 1})", flush=True)
                return True
            except Exception as e:      # radio off, out of range, WinRT object closed: try again
                print(f"      reconnect try {k + 1}: {type(e).__name__}: {e}", flush=True)
                await self.drop()
        return False

    async def stop(self):
        """STOP and wait for stream_stopped (everything up to its head arrived, or the tail is
        counted). A link that drops meanwhile ends the wait: that is not a stop timeout
        (docs/ble.md 7), finish() counts the tail."""
        if self.linked and self.client and self.client.is_connected:
            self.stopped.clear()
            b = self.stream.stop_command()
            self.log("stop", "", b.hex())
            wait = self.stream.stop_wait_ms()
            try:
                await self.client.write_gatt_char(pl.CTRL_UUID, b, response=True)
                waits = [asyncio.create_task(self.stopped.wait()), asyncio.create_task(self.lost_link.wait())]
                await asyncio.wait(waits, timeout=wait / 1000, return_when=asyncio.FIRST_COMPLETED)
                for t in waits:
                    t.cancel()
                if not self.stopped.is_set() and not self.lost_link.is_set():
                    self.log("call", "", f"stop_timed_out={int(self.stream.stop_timed_out())}")
            except Exception:
                pass                    # the link went: finish() counts the tail
        self.stream.finish()
        self.log("call", "", "finish")
        await self.drop()


async def run(rx, address, seconds, outages, quit_flag, sleeps=(), until_wake=None):
    t0 = time.time()
    el = lambda: time.time() - t0
    woke_done = lambda: until_wake is not None and rx.woke_at is not None and time.time() - rx.woke_at >= until_wake
    give_up = lambda: quit_flag() or (seconds is not None and el() >= seconds) or woke_done()
    addr = await rx.connect(address)
    print(f"Streaming from {rx.dev.name or 'BarPod'} {addr}. Ctrl+C stops.", flush=True)
    outages = sorted(outages, key=lambda o: o[1])
    sleeps = sorted(sleeps)
    t_print = 0
    while not give_up():
        await asyncio.sleep(0.25)
        rx.cap.flush()
        rx.f.flush()
        if rx.linked and rx.n == rx.n_at_connect and time.time() - rx.connected_at > FIRST_SAMPLE_S:
            raise RuntimeError(f"no sample {FIRST_SAMPLE_S} s after connecting "
                               f"({'MTU too small' if rx.mtu_small_since else 'no data'})")
        if rx.mtu_small_since and time.time() - rx.mtu_small_since > MTU_FAULT_S and "MTU too small" not in rx.stream.faults:
            rx.stream.faults.append("MTU too small")
            print("      fault: MTU too small (still set 3 s after connect)", flush=True)
        while rx.pending and rx.linked:          # the decoder asked (a reboot, a stale INFO)
            kind = rx.pending.pop(0)
            if kind == "need-info":
                rx.frame(await rx.client.read_gatt_char(pl.INFO_UUID))
                rx.frame(await rx.client.read_gatt_char(pl.STATUS_UUID))
            await rx.command()
        if sleeps and el() >= sleeps[0][0] and rx.linked:
            at, mode = sleeps.pop(0)
            print(f"  {el():6.1f} s: SLEEP op, mode {mode}", flush=True)
            await rx.sleep_command(mode)
            continue
        if outages and el() >= outages[0][1]:
            dur, at = outages.pop(0)
            print(f"  {el():6.1f} s: outage {dur:g} s (disconnecting on purpose)", flush=True)
            await rx.drop()
            t_end = time.time() + dur
            while time.time() < t_end and not quit_flag():
                await asyncio.sleep(0.25)
            rx.lost_link.clear()
            if not await rx.reconnect(addr, give_up):
                break
            continue
        if rx.lost_link.is_set() and not rx.linked:
            asleep = rx.sleeps and rx.sleeps[-1]["samples"] == rx.n
            print(f"  {el():6.1f} s: " + ("the pod went to sleep, reconnecting when it wakes" if asleep
                                          else "link lost, reconnecting"), flush=True)
            if not await rx.reconnect(addr, give_up):
                break
        if el() - t_print >= 10:
            t_print = el()
            st = rx.stream.ep.status if rx.stream.ep else None
            idx_gaps = [g for g in rx.stream.gaps if not g["kind"].startswith("imu")]
            print(f"  {el():6.1f} s: {rx.n} samples, gaps {len(rx.stream.gaps)} "
                  f"(lost {sum(g['n'] for g in idx_gaps)}), faults {len(rx.stream.faults)}, "
                  f"pod lost_overflow {st['lost_overflow'] if st else '?'}", flush=True)


async def session(rx, address, seconds, outages, quit_flag, sleeps=(), until_wake=None):
    try:
        await run(rx, address, seconds, outages, quit_flag, sleeps, until_wake)
        rx.ended = "quit" if quit_flag() else "done"
    except Exception as e:
        rx.ended = f"error: {type(e).__name__}: {e}"
        print(f"  {rx.ended}", flush=True)
    finally:
        await rx.stop()                # STOP (releases the pod's hold), then the tail is counted


def main():
    name = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--name=")), "")
    seconds = next((float(a.split("=", 1)[1]) for a in sys.argv if a.startswith("--seconds=")), None)
    address = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--address=")), None)
    outages = []
    for a in sys.argv:
        if a.startswith("--outage="):
            d, at = a.split("=", 1)[1].split("@")
            outages.append((float(d), float(at)))
    sleeps = []
    for a in sys.argv:
        if a.startswith("--sleep="):
            at, _, mode = a.split("=", 1)[1].partition(":")
            sleeps.append((float(at), int(mode or 0)))
    until_wake = next((float(a.split("=", 1)[1]) for a in sys.argv if a.startswith("--until-wake=")), None)
    tag = datetime.now().strftime("%Y-%m-%d-%H%M") + (f"-{name}" if name else "")
    data = Path(__file__).parent / "data"
    out_csv = new_path(data, "bar-pod", tag, "csv")
    tag = out_csv.stem[len("bar-pod-"):]                  # with the -2 suffix, if it got one
    cap = data / f"pod-capture-{tag}.csv"
    link_path = data / f"pod-link-{tag}.json"
    rx = Receiver(out_csv, cap)

    quit_now = [False]
    def on_int(*_):                    # first Ctrl+C: STOP and save; a second one kills it
        quit_now[0] = True
        signal.signal(signal.SIGINT, signal.default_int_handler)
    signal.signal(signal.SIGINT, on_int)
    try:
        asyncio.run(session(rx, address, seconds, outages, lambda: quit_now[0], sleeps, until_wake))
    finally:
        rx.f.close()
        rx.cap.close()
        s = rx.stream
        ep = s.ep
        link = {"samples": rx.n, "ended": rx.ended, "gaps": s.gaps, "faults": s.faults, "stats": s.stats,
                "lost_overflow_by_boot": rx.lost_by_boot, "sleeps": rx.sleeps, "epochs": rx.epochs,
                "last_idx": ep.last_idx if ep else None, "next_idx": ep.next_idx if ep else None,
                "lost_counted": sum(g["n"] for g in s.gaps if not g["kind"].startswith("imu"))}
        link_path.write_text(json.dumps(link, indent=1))
        print(f"Saved {rx.n} samples to {out_csv.name}, the capture to {cap.name}, link to {link_path.name} ({rx.ended})")
        print(f"gaps {[(g['kind'], g['n']) for g in s.gaps]}, faults {s.faults}, pkt_seq gaps {s.stats['pkt_seq_gaps']}, "
              f"reconnects {s.stats['reconnects']}, stopped {s.stats['stopped']}, pod lost_overflow {rx.lost_by_boot}")


if __name__ == "__main__":
    main()
