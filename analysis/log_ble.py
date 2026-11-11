"""
Record the pod over Bluetooth or USB: roadmap 1.4's receiver (docs/ble.md; docs/ble-plan.md step 7),
on CRP since D45 (docs/ble.md 2, analysis/crp.py). The laptop does what the phone does: the
connection order of docs/ble.md 7 through crp.Session, every packet the pod sends through PodStream
(podlink.py, the reference decoder) in arrival order, a dropout resumed from the pod's 38 s ring
with STREAM_FROM, and nothing ever filled in: a sample that can't come back is a counted, located gap.

    python log_ble.py                                    # Ctrl+C stops (with the STOP handshake)
    python log_ble.py --seconds=600 --name=proof
    python log_ble.py --seconds=420 --outage=3@40 --outage=60@300   # drop the link: 3 s at 40 s, ...
    python log_ble.py --seconds=300 --sleep=15 --until-wake=15      # step 9: sleep, pickup, new epoch
    python log_ble.py --usb=COM5 --seconds=600 --name=usbcrp        # the same over the pod's USB
    python log_ble.py --selftest                         # log_ble_sim.py: make_fake_pod's pod, no hardware

Bluetooth: the first pod that advertises Ansync's CRP service (5090) with a BarPod- name, or
--address=. Every connect and reconnect runs the connection order: HELLO (device_id "BPOD"), READ
INFO, READ STATUS (a BUSY is retried every 200 ms for 3 s, then the MTU register says why), WRITE
PUSH = 1, WRITE CTRL with the decoder's command.
--usb=COMx: the same over the USB port (docs/ble.md 2.5). The HELLO switches the pod from its CSV
to CRP, and the session sends a NOP every 1 s. The pod going back to text (its BYE or END, or the
CSV arriving where packets should) ends the capture; a port that goes quiet or away (a reset) is
reopened like a Bluetooth reconnect.
--outage=S@T drops the link on purpose T s into the run and reconnects S s later (the outage
matrix: the pod's side is the same as a real dropout, docs/ble.md 5). Times count from the start.
--sleep=T[:mode] sends the SLEEP op T s in (mode 0 = wake on motion, 1 = IMU off). After any
sleep the receiver keeps trying to reconnect, so a pickup lands in a new epoch;
--until-wake=S ends the run S s after the first sample from a pod that rebooted (woke).
--bench=c@T (with --usb): writes the bench character c to the BENCH register T s in, say w@30
(the dry run of a sleep, then a reset; check it with check_sleep.py --no-pickup).
Check a sleep run with check_sleep.py.

Saves, in data/ (P2), and never over an earlier run (a -2, -3 suffix in the same minute):
  bar-pod-<stamp>[-name].csv       the samples, same columns as log_serial.py and the app
  pod-capture-<stamp>[-name].csv   every packet and decoder call as it happens (docs/ble.md 7,
                                   Captures: pkt, tx, cmd, stop, call; plus ctrl and link rows,
                                   for reading): podlink.replay_rows replays it
  pod-link-<stamp>[-name].json     gaps, faults, stats, how it ended, the pod's loss counter per boot
Compare a run with a simultaneous USB log: compare_ble_usb.py (--exact: two of these runs, say one
over Bluetooth and one over USB, against each other). Bluetooth needs bleak (requirements.txt) and
the Bluetooth headphones off (they starve the laptop's scanning, D28); --usb needs pyserial.
"""

import asyncio
import itertools
import json
import signal
import struct
import sys
import time
from datetime import datetime
from pathlib import Path

import crp
import podlink as pl

HEADER = ('"Time (s)","Acceleration x (m/s^2)","Acceleration y (m/s^2)","Acceleration z (m/s^2)",'
          '"Absolute acceleration (m/s^2)","Gyro x (deg/s)","Gyro y (deg/s)","Gyro z (deg/s)"')
BACKOFF = (0.5, 1, 2, 4)           # s between reconnect tries, then every 5 s (ble-plan.md 3)
FIRST_SAMPLE_S = 10                # no sample this long after a connect: the run fails, like the app
MTU_FAULT_S = 3                    # mtu_too_small still set this long after connect: a fault (ble.md 5)
BUSY_RETRY_S, BUSY_FOR_S = 0.2, 3.0    # a BUSY read is retried every 200 ms for 3 s (ble.md 5)
NAME_PREFIX = "BarPod-"            # every CRP device shares the service: the name tells a pod (ble.md 2.4)
USB_QUIET_S = 3.0                  # USB: nothing from the pod this long, the port is reopened (it sends every 1 s)
USB_TEXT_B = 64                    # USB: this much text where packets should be = the pod went back to its CSV


def new_path(folder, prefix, tag, ext):
    """data/<prefix>-<tag>.<ext>, or -2, -3... if a run in the same minute already used it."""
    p = folder / f"{prefix}-{tag}.{ext}"
    k = 2
    while p.exists():
        p = folder / f"{prefix}-{tag}-{k}.{ext}"
        k += 1
    return p


def is_pod(dev, adv):
    """A BarPod's advertisement: Ansync's CRP service plus our name prefix."""
    name = adv.local_name or dev.name or ""
    return crp.SERVICE_UUID in [u.lower() for u in adv.service_uuids] and name.startswith(NAME_PREFIX)


async def read_retry(session, reg, clock):
    """(status, value, BUSY answers) of a READ with the BUSY retry (docs/ble.md 5): every 200 ms
    for 3 s. A status that's still BUSY after that is returned as it is. ble_check.py uses it too."""
    t0, busy = clock(), 0
    while True:
        st, val = await session.read(reg)
        if st != crp.BUSY or clock() - t0 + BUSY_RETRY_S > BUSY_FOR_S:
            return st, val, busy + (st == crp.BUSY)
        busy += 1
        await asyncio.sleep(BUSY_RETRY_S)


# ---- where the pod is: a fresh, unopened link per connect ----
class BleTarget:
    """The pod on Bluetooth: --address, or the first BarPod found. Reconnects find the same one
    again by address (WinRT closes the scanner's device object at a disconnect)."""
    kind = "ble"

    def __init__(self, address=None):
        self.address = address
        self.name = None
        self.hint = True                # the no-pod hint until the first connect: after that a miss is
                                        # mostly the pod asleep, and the hint's PowerShell query slows each try

    async def link(self):
        from bleak import BleakScanner          # only here: --usb and --selftest run without bleak
        if self.address:
            dev = await BleakScanner.find_device_by_address(self.address, timeout=10)
        else:
            dev = await BleakScanner.find_device_by_filter(is_pod, timeout=10)
        if dev is None:
            raise RuntimeError("no BarPod found" + (crp.no_pod_hint() if self.hint else ""))
        self.address, self.name, self.hint = dev.address, dev.name or self.name, False
        return crp.BleakLink(dev)

    def __str__(self):
        return f"{self.name or 'BarPod'} {self.address}"


class UsbTarget:
    """The pod's USB port. Opening it finds the pod in text mode; the HELLO switches it to CRP."""
    kind = "usb"

    def __init__(self, port):
        self.port = port

    async def link(self):
        return crp.SerialLink(self.port)

    def __str__(self):
        return f"the pod's USB on {self.port}"


class Tapped:
    """The link as the Session sees it: every packet sent becomes a tx row first, and a link that
    drops tells the receiver before the Session (which then fails its open transactions)."""

    def __init__(self, link, rx):
        self.link, self.rx = link, rx
        self.heartbeat = getattr(link, "heartbeat", None)
        self.on_packet = self.on_down = None          # the Session's, set when it's made
        link.on_packet = lambda pkt, ms: self.on_packet(pkt, ms)
        link.on_down = self._down

    def _down(self):
        self.rx.on_disconnect(self.link)
        if self.on_down:
            self.on_down()

    def send(self, pkt):
        self.rx.log("tx", self.rx.now_ms(), bytes(pkt).hex())
        return self.link.send(pkt)


class Receiver:
    def __init__(self, csv_path, cap_path, target, clock=time.time, say=None):
        self.f = open(csv_path, "x", newline="")
        self.f.write(HEADER + "\n")
        self.cap = open(cap_path, "x", newline="")       # written as it happens: a crash keeps it
        self.cap.write("scenario,step,kind,arrival_ms,payload\n")
        self.step = 0
        self.target = target
        self.clock = clock             # s; time.time on the bench, the simulator's in the selftest
        self.say = say or (lambda msg: print(msg, flush=True))
        self.stream = pl.PodStream(self.on_sample, self.on_event)
        self.link = None               # this connection's link and CRP session (new ones per connect)
        self.tapped = None
        self.session = None
        self.linked = False            # connected and set up
        self.lost_link = asyncio.Event()
        self.stopped = asyncio.Event()
        self.n = 0
        self.pending = []              # commands the decoder asked for (need-command / need-info)
        self.mtu_small_since = None
        self.connected_at = None
        self.n_at_connect = 0
        self.busy = 0                  # BUSY answers to reads (each retried)
        self.last_rx = None            # clock of the last packet from the pod
        self.text_mark = 0             # USB: the deframer's dropped bytes when the pod's HELLO came
        self.lost_by_boot = {}         # boot_id -> [first, last] lost_overflow the pod reported
        self.ended = "running"         # how the run ended, for the JSON
        self.t0 = self.clock()
        self.sleeps = []               # per going_to_sleep STATUS: reason, when, the disconnect after it
        self.epochs = []               # per epoch: boot_id, reboot, samples before it, when
        self.woke_at = None            # time of the first sample of a rebooted (woken) pod

    def now_ms(self):
        return round(self.clock() * 1000, 3)

    # ---- the decoder's side ----
    def on_sample(self, s):
        if self.woke_at is None and self.epochs and self.epochs[-1]["reboot"]:
            self.woke_at = self.clock()
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
                self.mtu_small_since = self.clock()
        elif kind in ("fault", "gap", "epoch", "sleep"):
            self.say(f"      {kind}: {info}")
        if kind == "sleep":
            self.sleeps.append({"reason": info, "at_s": round(self.clock() - self.t0, 3), "samples": self.n,
                                "disconnect_after_ms": None})
        elif kind == "epoch":
            self.epochs.append({"boot_id": info["boot_id"], "reboot": info["reboot"], "samples_before": self.n,
                                "at_s": round(self.clock() - self.t0, 3)})

    def log(self, kind, arrival, payload):
        self.cap.write(f"capture,{self.step},{kind},{arrival},{payload}\n")
        self.step += 1

    def packet(self, pkt, arrival):
        """The Session's tap: every packet the pod sends, first, in arrival order (docs/ble.md 7).
        Logged whole (a pkt row), then its frame, if it carries one, into the decoder."""
        self.last_rx = self.clock()
        self.log("pkt", arrival, pkt.hex())
        self.stream.packet(pkt, arrival)
        if pkt[:1] == bytes([crp.HELLO]):
            self.text_mark = getattr(getattr(self.link, "deframer", None), "dropped", 0)
        elif pkt[:1] == bytes([crp.LOG]):          # USB in CRP mode: the pod's "#" lines (ble.md 2.5)
            p = crp.parse(pkt)
            if p is not None:
                self.say(f"      pod: {p['msg'].decode(errors='replace')}")
        f = pl.crp_frame(pkt)
        ep = self.stream.ep
        if f is not None and f[:1] == bytes([pl.T_STATUS]) and ep is not None and ep.status is not None \
                and not self.stream.ignoring:
            lost = ep.status["lost_overflow"]
            rng = self.lost_by_boot.setdefault(str(ep.boot_id), [lost, lost])
            rng[1] = lost
            if not ep.status["flags"] & pl.S_MTU_TOO_SMALL:
                self.mtu_small_since = None

    # ---- the link ----
    def on_disconnect(self, link):
        if link is not self.link:
            return                     # a late callback from an earlier connection
        self.linked = False
        self.lost_link.set()
        self.log("link", self.now_ms(), "disconnect")
        sl = self.sleeps[-1] if self.sleeps else None
        if sl and sl["disconnect_after_ms"] is None:       # the pod's own disconnect after going_to_sleep
            sl["disconnect_after_ms"] = round((self.clock() - self.t0 - sl["at_s"]) * 1000)

    def pod_left(self):
        """Why the session ended with the link still up, or None: the pod's BYE or END (the Session
        saw it), and on USB the CSV arriving where packets should (the pod went back to text,
        docs/ble.md 2.5). On USB, 3 s without a packet is "quiet": the pod sends something every
        1 s (STATUS, or its NOP), so the port gets reopened."""
        s = self.session
        if s is not None and not s.active and s.ended in ("bye", "end"):
            return f"its {s.ended.upper()}"
        if self.target.kind != "usb":
            return None
        d = getattr(self.link, "deframer", None)
        if d is not None and d.dropped - self.text_mark > USB_TEXT_B:
            return f"{d.dropped - self.text_mark} B of text: back to its CSV"
        if self.last_rx is not None and self.clock() - self.last_rx > USB_QUIET_S:
            return "quiet"
        return None

    async def read_frame(self, reg):
        """A READ with the BUSY retry (docs/ble.md 5): every 200 ms for 3 s, then the MTU register
        (10 B, fits at MTU 23) says why. Returns the value; anything but OK raises."""
        st, val, busy = await read_retry(self.session, reg, self.clock)
        self.busy += busy
        if st == crp.BUSY:
            st, v = await self.session.read(pl.REG_LINK_MTU)
            mtu = struct.unpack("<H", v)[0] if st == crp.OK and len(v) == 2 else None
            raise RuntimeError(f"the link's MTU is too small (BUSY for {BUSY_FOR_S:g} s; the pod says MTU {mtu})")
        if st != crp.OK:
            raise RuntimeError(f"READ 0x{reg:08x}: {crp.STATUS_NAMES.get(st, st)}")
        return val

    async def command(self):
        b = self.stream.command()
        self.log("cmd", "", "none" if b is None else b.hex())
        if b is None:
            raise RuntimeError(f"the decoder won't stream: {self.stream.faults[-1:]}")
        st = await self.session.write(pl.REG_CTRL, b)
        if st != crp.OK:               # BUSY: the pod is in its sleep prologue (the link drops next)
            self.say(f"      the pod answered the command {crp.STATUS_NAMES.get(st, st)}")

    async def release(self):
        """Close the last connection's link if a dropout left it open. A SerialLink whose port
        vanished (the pod reset) still holds its handle, and Windows may not open the port again
        while it's held; closing a dead bleak client is a no-op."""
        link, self.link = self.link, None              # a late callback from it is ignored from here
        if link is not None:
            try:
                await link.close()
            except Exception:
                pass

    async def connect(self):
        """Bring the link up and run the connection order (docs/ble.md 7). One try."""
        await self.release()
        link = await self.target.link()
        self.lost_link.clear()
        self.link = link
        self.tapped = Tapped(link, self)
        self.session = crp.Session(self.tapped, tap=self.packet)   # before open: nothing goes untapped
        await link.open()
        self.log("link", self.now_ms(), "connect")
        peer = await self.session.hello()
        if peer["device_id"] != pl.DEVICE_ID:
            raise RuntimeError(f"not a BarPod: HELLO device_id 0x{peer['device_id']:08x}")
        await self.read_frame(pl.REG_INFO)
        await self.read_frame(pl.REG_STATUS)
        st = await self.session.write(pl.REG_PUSH, b"\x01")
        if st != crp.OK:
            raise RuntimeError(f"PUSH = 1: {crp.STATUS_NAMES.get(st, st)}")
        self.pending.clear()           # the connection order's own command answers any request
        await self.command()
        self.linked = not self.lost_link.is_set()
        self.connected_at = self.clock()
        self.n_at_connect = self.n

    async def bench_write(self, what, reg, value, row):
        """A write the bench asked for, not the decoder: logged as a ctrl row (not "cmd":
        replay_rows checks those against command()). The pod may sleep or reset before its answer
        arrives, so a link that goes meanwhile is only said."""
        self.log("ctrl", "", row)
        try:
            st = await self.session.write(reg, value)
            if st != crp.OK:
                self.say(f"      the pod answered {what} {crp.STATUS_NAMES.get(st, st)}")
        except crp.CrpError as e:
            self.say(f"      {what}: {e}")

    async def sleep_command(self, mode):
        b = bytes([pl.OP_SLEEP, mode])
        await self.bench_write("SLEEP", pl.REG_CTRL, b, b.hex())

    async def bench_command(self, c):
        """USB only: the BENCH register (docs/ble.md 2.3), the bench characters over CRP: w is the
        dry run of a sleep, then a reset (check_sleep.py --no-pickup)."""
        await self.bench_write(f"BENCH {c}", pl.REG_BENCH, c.encode(), f"bench {c}")

    async def drop(self, bye=False):
        """Take the link down. bye: end the CRP session first (a run's end; USB goes back to text)."""
        self.linked = False
        s, link = self.session, self.link
        self.log("link", self.now_ms(), "drop")
        try:
            if bye and s is not None:
                await s.close()
                await asyncio.sleep(0.1)   # let the BYE out first (a serial write returns before it's sent)
            if link is not None:
                await link.close()
        except Exception:
            pass
        if self.tapped is not None and self.tapped.on_down:
            self.tapped.on_down()          # the Session's: a closed SerialLink reports nothing, so its heartbeat would run on

    async def reconnect(self, give_up):
        """Back to streaming after a dropout: the same PodStream, so STREAM_FROM(next index)."""
        for k in itertools.count():         # until give_up(): Ctrl+C, --seconds or --until-wake
            await asyncio.sleep(BACKOFF[k] if k < len(BACKOFF) else 5)
            if give_up():
                return False
            try:
                await self.connect()
                self.say(f"      reconnected (try {k + 1})")
                return True
            except Exception as e:      # radio off, out of range, WinRT object closed, no port: try again
                self.say(f"      reconnect try {k + 1}: {type(e).__name__}: {e}")
                await self.drop()

    async def stop(self):
        """STOP and wait for stream_stopped (everything up to its head arrived, or the tail is
        counted). A link that drops meanwhile ends the wait: that is not a stop timeout
        (docs/ble.md 7), finish() counts the tail. Then BYE, and the link goes down."""
        s = self.session
        if self.linked and s is not None and s.active:
            self.stopped.clear()
            b = self.stream.stop_command()
            self.log("stop", "", b.hex())
            wait = self.stream.stop_wait_ms()
            self.log("call", "", f"stop_wait_ms={wait!r}")
            w = asyncio.ensure_future(s.write(pl.REG_CTRL, b))
            waits = [asyncio.ensure_future(self.stopped.wait()), asyncio.ensure_future(self.lost_link.wait())]
            await asyncio.wait(waits, timeout=wait / 1000, return_when=asyncio.FIRST_COMPLETED)
            for t in waits:
                t.cancel()
            if not self.stopped.is_set() and not self.lost_link.is_set():
                self.log("call", "", f"stop_timed_out={int(self.stream.stop_timed_out())}")
            if not w.done():
                w.cancel()
            elif not w.cancelled() and w.exception() is None and w.result() != crp.OK:
                self.say(f"      the pod answered STOP {crp.STATUS_NAMES.get(w.result(), w.result())}")
        self.log("call", "", "finish")
        self.stream.finish()
        await self.drop(bye=self.linked and s is not None and s.active)

    # ---- the link JSON ----
    def summary(self):
        s = self.stream
        ep = s.ep
        return {"samples": self.n, "ended": self.ended, "gaps": s.gaps, "faults": s.faults, "stats": s.stats,
                "lost_overflow_by_boot": self.lost_by_boot, "sleeps": self.sleeps, "epochs": self.epochs,
                "last_idx": ep.last_idx if ep else None, "next_idx": ep.next_idx if ep else None,
                "lost_counted": sum(g["n"] for g in s.gaps if not g["kind"].startswith("imu")),
                "transport": f"{self.target.kind}: {self.target}"}

    def close(self, link_path):
        self.f.close()
        self.cap.close()
        Path(link_path).write_text(json.dumps(self.summary(), indent=1))


async def run(rx, seconds, outages, quit_flag, sleeps=(), until_wake=None, benches=()):
    clock = rx.clock
    t0 = clock()
    el = lambda: clock() - t0
    woke_done = lambda: until_wake is not None and rx.woke_at is not None and clock() - rx.woke_at >= until_wake
    give_up = lambda: quit_flag() or (seconds is not None and el() >= seconds) or woke_done()
    usb = rx.target.kind == "usb"
    await rx.connect()
    rx.say(f"Streaming from {rx.target}. Ctrl+C stops.")
    outages = sorted(outages, key=lambda o: o[1])
    todo = sorted([(at, f"SLEEP op, mode {mode}", rx.sleep_command, mode) for at, mode in sleeps]
                  + [(at, f"BENCH {c}", rx.bench_command, c) for at, c in benches], key=lambda w: w[0])
    t_print = 0
    while not give_up():
        await asyncio.sleep(0.25)
        rx.cap.flush()
        rx.f.flush()
        if rx.linked and rx.n == rx.n_at_connect and clock() - rx.connected_at > FIRST_SAMPLE_S:
            raise RuntimeError(f"no sample {FIRST_SAMPLE_S} s after connecting "
                               f"({'MTU too small' if rx.mtu_small_since else 'no data'})")
        if rx.mtu_small_since and clock() - rx.mtu_small_since > MTU_FAULT_S and "MTU too small" not in rx.stream.faults:
            rx.stream.faults.append("MTU too small")
            rx.say("      fault: MTU too small (still set 3 s after connect)")
        why = rx.pod_left() if rx.linked else None
        if why and usb and why != "quiet":           # the pod is back in text mode: the capture ends
            rx.linked = False
            rx.ended = f"the pod ended the session ({why})"
            rx.say(f"  {el():6.1f} s: {rx.ended}")
            return
        if why:                        # USB quiet, or a Bluetooth session ended with the link up
            rx.say(f"  {el():6.1f} s: the session went ({why}), reconnecting")
            await rx.drop()
        while rx.pending and rx.linked:          # the decoder asked (a reboot, a stale INFO)
            kind = rx.pending.pop(0)
            try:
                if kind == "need-info":
                    await rx.read_frame(pl.REG_INFO)
                    await rx.read_frame(pl.REG_STATUS)
                await rx.command()
            except crp.CrpError:
                if rx.linked:
                    raise
                break                  # the link went meanwhile: the reconnect's own order answers it
        if todo and el() >= todo[0][0] and rx.linked:
            _, what, send, value = todo.pop(0)
            rx.say(f"  {el():6.1f} s: {what}")
            await send(value)
            continue
        if outages and el() >= outages[0][1]:
            dur, at = outages.pop(0)
            rx.say(f"  {el():6.1f} s: outage {dur:g} s (dropping the link on purpose)")
            await rx.drop()
            t_end = clock() + dur
            while clock() < t_end and not quit_flag():
                await asyncio.sleep(0.25)
            if not await rx.reconnect(give_up):
                break
            continue
        if not rx.linked:              # the link went (it said so, or we dropped it above)
            asleep = rx.sleeps and rx.sleeps[-1]["samples"] == rx.n
            rx.say(f"  {el():6.1f} s: " + ("the pod went to sleep, reconnecting when it wakes" if asleep
                                           else "link lost, reconnecting"))
            if not await rx.reconnect(give_up):
                break
        if el() - t_print >= 10:
            t_print = el()
            st = rx.stream.ep.status if rx.stream.ep else None
            idx_gaps = [g for g in rx.stream.gaps if not g["kind"].startswith("imu")]
            rx.say(f"  {el():6.1f} s: {rx.n} samples, gaps {len(rx.stream.gaps)} "
                   f"(lost {sum(g['n'] for g in idx_gaps)}), faults {len(rx.stream.faults)}, "
                   f"pod lost_overflow {st['lost_overflow'] if st else '?'}")


async def session(rx, seconds, outages, quit_flag, sleeps=(), until_wake=None, benches=()):
    try:
        await run(rx, seconds, outages, quit_flag, sleeps, until_wake, benches)
        if rx.ended == "running":
            rx.ended = "quit" if quit_flag() else "done"
    except Exception as e:
        rx.ended = f"error: {type(e).__name__}: {e}"
        rx.say(f"  {rx.ended}")
    finally:
        await rx.stop()                # STOP (releases the pod's hold), then the tail is counted


FLAGS = ("name", "seconds", "address", "usb", "outage", "sleep", "until-wake", "bench")


def args(name):
    """Every value of a --name=value flag, in order."""
    return [a.split("=", 1)[1] for a in sys.argv if a.startswith(f"--{name}=")]


def arg(name, conv=str, default=None):
    return next((conv(v) for v in args(name)), default)


def main():
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        return 0
    if "--selftest" in sys.argv:
        import log_ble_sim
        return 0 if log_ble_sim.selftest() else 1
    stray = [a for a in sys.argv[1:] if not a.startswith(tuple(f"--{k}=" for k in FLAGS))]
    if stray:                          # a typo would run on a default: --usb COM5 records over Bluetooth
        sys.exit(f"unknown argument(s) {' '.join(stray)}: every flag is --name=value (see --help)")
    name = arg("name", default="")
    seconds = arg("seconds", float)
    address = arg("address")
    port = arg("usb")
    if port == "":                     # --usb= with nothing after it would fall to Bluetooth
        sys.exit("--usb=COMx: no port given")
    if port and address:
        sys.exit("--usb and --address: pick one link")
    outages = [(float(d), float(at)) for d, at in (v.split("@") for v in args("outage"))]
    sleeps = [(float(at), int(mode or 0)) for at, _, mode in (v.partition(":") for v in args("sleep"))]
    benches = [(float(at), c) for c, _, at in (v.partition("@") for v in args("bench"))]
    if benches and not (port and all(len(c) == 1 for _, c in benches)):
        sys.exit("--bench=c@T: one bench character, and only with --usb")
    until_wake = arg("until-wake", float)
    tag = datetime.now().strftime("%Y-%m-%d-%H%M") + (f"-{name}" if name else "")
    data = Path(__file__).parent / "data"
    out_csv = new_path(data, "bar-pod", tag, "csv")
    tag = out_csv.stem[len("bar-pod-"):]                  # with the -2 suffix, if it got one
    cap = data / f"pod-capture-{tag}.csv"
    link_path = data / f"pod-link-{tag}.json"
    rx = Receiver(out_csv, cap, UsbTarget(port) if port else BleTarget(address))

    quit_now = [False]
    def on_int(*_):                    # first Ctrl+C: STOP and save; a second one kills it
        quit_now[0] = True
        signal.signal(signal.SIGINT, signal.default_int_handler)
    signal.signal(signal.SIGINT, on_int)
    try:
        asyncio.run(session(rx, seconds, outages, lambda: quit_now[0], sleeps, until_wake, benches))
    finally:
        rx.close(link_path)
        s = rx.stream
        print(f"Saved {rx.n} samples to {out_csv.name}, the capture to {cap.name}, link to {link_path.name} ({rx.ended})")
        print(f"gaps {[(g['kind'], g['n']) for g in s.gaps]}, faults {s.faults}, pkt_seq gaps {s.stats['pkt_seq_gaps']}, "
              f"reconnects {s.stats['reconnects']}, stopped {s.stats['stopped']}, pod lost_overflow {rx.lost_by_boot}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
