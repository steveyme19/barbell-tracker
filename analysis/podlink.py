"""
PodLink v1: the reference decoder for the pod's Bluetooth stream (decision P1).

Implements docs/ble.md rule for rule. The JS PodStream in webapp/sources.js is a line-for-line
port of PodStream below, and make_fake_pod.py checks both against the same test vectors.
Standard library only.

    from podlink import PodStream
    s = PodStream(on_sample)          # on_sample({t, ax, ay, az, gx, gy, gz})
    s.packet(crp_pkt, arrival_ms)     # every CRP packet the pod sends, in arrival order (D45)
    s.frame(raw_bytes, arrival_ms)    # or every frame of any type (captures from before D45)
    cmd = s.command()                 # bytes to write to the CTRL register (STREAM_LIVE / STREAM_FROM)

Since D45 the frames travel inside CRP (docs/ble.md 2, analysis/crp.py): crp_frame() takes a
frame out of a packet, and PodStream itself never sees CRP.

The frame builders (build_data, build_status, build_info, build_cal) are the pod's side, used by
the simulator in make_fake_pod.py.

    python podlink.py --replay-all        # every committed capture still decodes the same (P2)
    python podlink.py --write-manifest    # pin the captures the manifest doesn't have yet
"""

import math
import struct
from collections import deque

import crp

# ---- the pod's CRP registers (docs/ble.md 2.3) ----
REG_PROTO, REG_INFO, REG_STATUS, REG_CAL = 0x00010000, 0x00010001, 0x00010002, 0x00010003
REG_DATA, REG_CTRL, REG_PUSH = 0x00010004, 0x00010005, 0x00010006
REG_LINK_KIND, REG_LINK_MTU, REG_LINK_N_MAX = 0x00030001, 0x00030002, 0x00030003
REG_BENCH = 0x00040001                     # USB only
DEVICE_ID = 0x444F5042                     # the pod's HELLO device_id, "BPOD" (42 50 4F 44 on the wire)
FRAME_READS = (REG_INFO, REG_STATUS, REG_CAL)   # an OK RESPONSE to a READ of these carries a frame
FRAME_PUSHES = (REG_DATA, REG_STATUS)           # so does a push (WRITE txn 0) to these

# ---- frame constants (docs/ble.md 3 and 4) ----
T_DATA, T_STATUS, T_INFO, T_CAL = 0x01, 0x10, 0x20, 0x30
F_RESYNC, F_OVERFLOW, F_IMU_RESTART, F_FINAL = 0x01, 0x02, 0x04, 0x08
S_USB, S_CHARGING, S_BATT_LOW, S_STREAMING = 0x01, 0x02, 0x04, 0x08
S_GOING_TO_SLEEP, S_STREAM_STOPPED, S_MTU_TOO_SMALL, S_BOOT_MISMATCH = 0x10, 0x20, 0x40, 0x80
OP_STREAM_FROM, OP_STREAM_LIVE, OP_STOP, OP_SLEEP = 0x01, 0x02, 0x03, 0x10

ACCEL_LSB = 0.061 * 4 / 1000 * 9.80665     # m/s^2 per count at +-8 g (= 0.0023928226)
GYRO_LSB = 4.375 * 8 / 1000                # deg/s per count at +-1000 deg/s (= 0.035)
TICK_HZ = 32768
LIVE_BACK = 600                            # STREAM_LIVE lead-in for a fresh session (~2.8 s)
SLEEP_REASONS = {0: "none", 1: "idle", 2: "lid-down", 3: "low battery", 4: "command", 5: "IMU lost"}

DATA_HDR = struct.Struct("<BBHIIBB")       # type n pkt_seq first_idx t0_ticks flags cmd_seq
DATA_SMP = struct.Struct("<H6h")           # dt_ticks gx gy gz ax ay az
STATUS_FMT = struct.Struct("<BBHIIHHBBH")  # 20 B
INFO_FMT = struct.Struct("<BBHIIffHHBBHI")  # 32 B


def int32(x):
    """The u32 difference x as a signed 32-bit number (JS: x | 0)."""
    x &= 0xFFFFFFFF
    return x - 0x100000000 if x >= 0x80000000 else x


def crp_frame(pkt):
    """The PodLink frame a CRP packet carries, or None (docs/ble.md 7, Feeding the decoder):
    an OK RESPONSE to a READ of INFO, STATUS or CAL gives its value; a push (WRITE txn 0) to DATA
    or STATUS gives its value; anything else (HELLO, NOP, LOG, BUSY, the answer to a write, a
    client's WRITE, a short or unknown packet) gives None. Pure: nothing is counted here, so a
    malformed frame inside a good packet still reaches the decoder's own checks (`bad`)."""
    b = bytes(pkt)
    if len(b) >= 8 and b[0] == crp.RESPONSE and b[2] == crp.OK:      # 0C txn status ch reg:u32 value
        if struct.unpack_from("<I", b, 4)[0] in FRAME_READS:
            return b[8:]
        return None
    if len(b) >= 6 and b[0] == crp.WRITE and b[1] == 0:               # 04 00 reg:u32 value
        if struct.unpack_from("<I", b, 2)[0] in FRAME_PUSHES:
            return b[6:]
    return None


def decode_frame(b):
    """bytes -> dict, or None if the frame is malformed (wrong length for its type, unknown type)."""
    if not b:
        return None
    typ = b[0]
    if typ == T_DATA:
        if len(b) < 14 + 14 or (len(b) - 14) % 14:
            return None
        _, n, seq, first, t0, flags, cmd_seq = DATA_HDR.unpack_from(b, 0)
        if not 1 <= n <= 16 or len(b) != 14 + 14 * n:
            return None
        smp = [DATA_SMP.unpack_from(b, 14 + 14 * k) for k in range(n)]
        return {"type": typ, "n": n, "pkt_seq": seq, "first_idx": first, "t0": t0,
                "flags": flags, "cmd_seq": cmd_seq, "samples": smp}
    if typ == T_STATUS:
        if len(b) != 20:
            return None
        (_, flags, mv, ticks_now, head, lost, missed, reason, npp, interval) = STATUS_FMT.unpack(b)
        return {"type": typ, "flags": flags, "battery_mv": mv, "ticks_now": ticks_now, "head": head,
                "lost_overflow": lost, "missed_drdy": missed, "sleep_reason": reason,
                "n_per_packet": npp, "interval": interval}
    if typ == T_INFO:
        if len(b) != 32:
            return None
        (_, proto, fw, boot_id, serial, alsb, glsb, odr, ring_n, wake, _r1, tick_hz, _r2) = INFO_FMT.unpack(b)
        return {"type": typ, "proto": proto, "fw_build": fw, "boot_id": boot_id, "serial": serial,
                "accel_lsb": alsb, "gyro_lsb": glsb, "odr": odr, "ring_n": ring_n, "wake": wake,
                "tick_hz": tick_hz}
    if typ == T_CAL:
        return {"type": typ} if len(b) == 64 else None
    return None


class Epoch:
    """Everything that belongs to one boot of the pod (docs/ble.md section 7, Epochs)."""

    def __init__(self, info, is_reboot):
        self.boot_id = info["boot_id"]
        self.odr = info["odr"] or 208
        self.ring_n = info["ring_n"]
        ok = lambda v: isinstance(v, float) and math.isfinite(v) and v > 0
        self.accel_lsb = info["accel_lsb"] if ok(info["accel_lsb"]) else ACCEL_LSB
        self.gyro_lsb = info["gyro_lsb"] if ok(info["gyro_lsb"]) else GYRO_LSB
        self.is_reboot = is_reboot          # not the session's first epoch: resume from index 0
        self.ref32 = self.ref64 = None      # tick unwrap reference, set by the first STATUS
        self.win = deque(maxlen=32)         # wallBase candidates from the last 32 STATUS
        self.wall_base = None
        self.t_offset = None                # set when this epoch emits its first sample
        self.first_t = None                 # t of that first sample
        self.next_idx = None
        self.last_idx = None                # index / ticks64 / t of the last kept sample
        self.last_ticks64 = None
        self.last_t = None
        self.pkt_prev = None
        self.held = []                      # DATA before the first STATUS
        self.open = False                   # a stream is open (section 7, Streams)
        self.status = None                  # the latest STATUS (decoded) and its ticks64
        self.status_t64 = None
        self.open_seq = None                # cmd_seq of the command that opened the stream
        self.snap = None                    # state at the last reconnect INFO (boot_mismatch undo)

    def ticks64(self, ticks):
        return self.ref64 + int32(ticks - self.ref32)


class PodStream:
    """The phone's decoder. Feed every frame in arrival order; samples come out in index order."""

    def __init__(self, on_sample, on_event=None):
        self.on_sample = on_sample
        self.on_event = on_event or (lambda kind, info: None)
        self.serial = None                  # the session's pod
        self.ignoring = False               # until the next good INFO: another pod, a bad INFO
                                            # or a boot_mismatch (section 7, Input checks)
        self.need_info = False              # boot_mismatch: re-read INFO before any command
        self.epochs = []
        self.ep = None                      # the newest epoch
        self.emitter = None                 # the most recent epoch that emitted a sample
        self.last_t = None                  # last emitted t, over all epochs
        self.cmd_seq = 0
        self.cmd = None                     # the current STREAM command
        self.stop = None                    # a pending STOP: {"head", "timed_out"}
        self.stale_due = 0                  # STOPs a later STREAM command overtook: their stream_stopped
                                            # may still come, and closes nothing (section 7, Stop)
        self.gaps = []
        self.faults = []
        self.stats = {"samples": 0, "bad": 0, "pre_info": 0, "ignored": 0, "stale": 0,
                      "stale_stops": 0, "after_close": 0, "dupes": 0, "t_backwards": 0,
                      "pkt_seq_gaps": 0, "pkt_seq_lost": 0, "reconnects": 0, "epochs": 0,
                      "clock_clamped": 0, "stopped": 0}

    # ---- helpers ----
    def _fault(self, what):
        self.faults.append(what)
        self.on_event("fault", what)

    def _gap(self, ep, kind, n, idx, t0, t1, lower_bound=False):
        g = {"kind": kind, "n": n, "idx": idx, "t0": t0, "t1": t1, "boot_id": ep.boot_id}
        if lower_bound:
            g["lower_bound"] = True
        self.gaps.append(g)
        self.on_event("gap", g)
        if kind == "unexplained":
            self._fault("unexplained gap")

    def _t_of(self, ep, t64):
        """t of a tick in this epoch, or None if the epoch hasn't emitted (no tOffset yet)."""
        return None if ep.t_offset is None else ep.t_offset + t64 / TICK_HZ

    def _t0_left(self, ep, t1):
        """t0 of a gap with no earlier kept sample in the epoch (section 7, Gaps)."""
        if ep.last_t is not None:
            return ep.last_t
        return self.last_t if self.last_t is not None else t1

    def _owed(self, ep, kind):
        """A woken (reboot) epoch that never streamed still owes everything from index 0 up to the
        last head the phone heard: a lower-bound tail with no time (section 7, Closing)."""
        if ep.is_reboot and not ep.open and ep.next_idx is None and ep.status is not None and ep.status["head"] > 0:
            self._gap(ep, kind, ep.status["head"], 0, None, None, lower_bound=True)
            ep.next_idx = ep.status["head"]

    def _new_epoch(self, info, is_reboot):
        old = self.ep
        if old is not None and old.open:
            # the pod slept or reset while disconnected: count what we know, a lower bound
            self._close(old, old.status["head"] if old.status else old.next_idx, old.status_t64,
                        "lost-tail", lower_bound=True)
        elif old is not None:
            self._owed(old, "lost-tail")
        ep = Epoch(info, is_reboot)
        self.epochs.append(ep)
        self.ep = ep
        self.stop = None
        self.stale_due = 0
        self.stats["epochs"] += 1
        self.on_event("epoch", {"boot_id": ep.boot_id, "reboot": is_reboot})
        return ep

    def _close(self, ep, head, t64, kind, lower_bound=False):
        """Close the epoch's open stream; samples nextIdx..head never arrived (a tail gap)."""
        if ep.open and ep.next_idx is not None and head is not None and head > ep.next_idx:
            if ep.last_t is None:
                t0 = t1 = None
            else:
                t0 = ep.last_t
                t1 = self._t_of(ep, t64) if t64 is not None else None
            self._gap(ep, kind, head - ep.next_idx, ep.next_idx, t0, t1, lower_bound)
        if head is not None and (ep.next_idx is None or head > ep.next_idx):
            ep.next_idx = head
        ep.open = False

    # ---- the entry points ----
    def packet(self, pkt, arrival_ms):
        """A CRP packet from the pod (D45): its frame, if it carries one, goes to frame()."""
        f = crp_frame(pkt)
        if f is not None:
            self.frame(f, arrival_ms)

    def frame(self, b, arrival_ms):
        f = decode_frame(bytes(b))
        if f is None:
            self.stats["bad"] += 1
            return
        if f["type"] == T_CAL:
            return                          # ignored in v1
        if f["type"] == T_INFO:
            return self._info(f)
        if self.ep is None:
            self.stats["pre_info"] += 1
            return
        if self.ignoring:
            self.stats["ignored"] += 1
            return
        if f["type"] == T_STATUS:
            return self._status(f, arrival_ms)
        ep = self.ep
        if ep.ref32 is None:                # DATA before this epoch's first STATUS: hold it
            ep.held.append(f)
            return
        self._data(ep, f)

    def _info(self, f):
        if f["proto"] != 1 or f["boot_id"] == 0:
            self.ignoring = True            # its STATUS and DATA would be just as suspect
            self._fault("bad INFO (proto or boot_id)")
            return
        if self.serial is None:
            self.serial = f["serial"]
        if f["serial"] != self.serial:
            self.ignoring = True
            self._fault("another pod")
            return
        self.ignoring = False
        self.need_info = False
        ep = self.ep
        if ep is not None and f["boot_id"] == ep.boot_id:
            # same boot: the epoch continues. Keep what a boot_mismatch would have to roll back.
            # Nothing the pod queued for the old link can still come (it drops it at the disconnect).
            self.stats["reconnects"] += 1
            self.stale_due = 0
            ep.snap = (ep.status, ep.status_t64, ep.ref32, ep.ref64, list(ep.win), ep.wall_base)
            return
        self._new_epoch(f, is_reboot=ep is not None)

    def _status(self, f, arrival_ms):
        ep = self.ep
        if f["flags"] & S_BOOT_MISMATCH:
            # STREAM_FROM had the wrong boot_id: our last INFO was stale. Everything since it
            # came from another boot, so undo it and ignore the link until INFO is read again.
            self._fault("boot mismatch")
            if ep.snap is not None:
                ep.status, ep.status_t64, ep.ref32, ep.ref64, win, ep.wall_base = ep.snap
                ep.win = deque(win, maxlen=32)
            self.ignoring = self.need_info = True
            self.on_event("need-info", None)
            return
        if ep.ref32 is not None:
            t64 = ep.ticks64(f["ticks_now"])
            rebooted = ((ep.next_idx is not None and f["head"] < ep.next_idx) or
                        (ep.last_ticks64 is not None and t64 < ep.last_ticks64 - 2 * TICK_HZ))
            if rebooted:
                # the pod rebooted but kept its boot_id: a new epoch, starting with this STATUS
                self._fault("reboot without a new boot_id")
                info = {"boot_id": ep.boot_id, "odr": ep.odr, "ring_n": ep.ring_n,
                        "accel_lsb": ep.accel_lsb, "gyro_lsb": ep.gyro_lsb}
                ep = self._new_epoch(info, is_reboot=True)
                self.on_event("need-command", None)
        if ep.ref32 is None:
            ep.ref32 = ep.ref64 = f["ticks_now"]
        else:
            ep.ref64 += int32(f["ticks_now"] - ep.ref32)
            ep.ref32 = f["ticks_now"]
        t64 = ep.ref64
        ep.win.append(arrival_ms - t64 / 32.768)
        ep.wall_base = min(ep.win)
        ep.status, ep.status_t64 = f, t64

        if f["flags"] & S_GOING_TO_SLEEP:
            # the ring dies with the sleep: whatever didn't arrive is lost
            self._close(ep, f["head"], t64, "sleep-tail")
            self.stop = None
            self.stats["stopped"] += 1
            self.on_event("stopped", "sleep")
            self.on_event("sleep", SLEEP_REASONS.get(f["sleep_reason"], "?"))
        elif f["flags"] & S_STREAM_STOPPED:
            cmd = self.cmd
            if cmd is not None and not cmd["started"] and ep.open and ep.open_seq != cmd["seq"] and self.stale_due > 0:
                # the end of a stream the phone already replaced (STOP then Start): the new
                # STREAM_FROM fetches those samples again, so record nothing. Only an overtaken STOP
                # gets this answer: after a reconnect, a STOP the phone sends on the new link can be
                # answered before the new stream's first DATA, and that one closes the stream.
                self.stale_due -= 1
                self.stats["stale_stops"] += 1
            else:
                self._close(ep, f["head"], t64, "stop-tail")
                self.stop = None
                self.stats["stopped"] += 1
                self.on_event("stopped", "stop")
        if f["flags"] & S_MTU_TOO_SMALL:
            self.on_event("mtu-too-small", None)

        held, ep.held = ep.held, []
        for d in held:
            self._data(ep, d)

    def _data(self, ep, f):
        cmd = self.cmd
        if cmd is None or f["cmd_seq"] != cmd["seq"]:
            self.stats["stale"] += 1        # a stream the phone already replaced
            return
        first_frame = not cmd["started"]
        if first_frame:
            cmd["started"] = True
            self.stale_due = 0              # DATA and STATUS come in order (D28): no stale stop is left
            ep.snap = None                  # this boot answered: nothing left to roll back
            had_idx = ep.next_idx is not None
            cmd["resync_skip"] = cmd["op"] == OP_STREAM_LIVE and had_idx
            if cmd["op"] == OP_STREAM_FROM:
                ep.next_idx = cmd["from"]
            elif not had_idx:
                ep.next_idx = f["first_idx"]
            ep.open, ep.open_seq = True, cmd["seq"]
        elif not ep.open:
            # frames of a stream that already closed: everything below its head was counted
            # (stop-tail / sleep-tail), so these can only be samples already given up on
            self.stats["after_close"] += 1
            return

        # pkt_seq continuity (per epoch, mod 2^16)
        if f["flags"] & F_RESYNC or ep.pkt_prev is None:
            ep.pkt_prev = f["pkt_seq"]
        else:
            if f["pkt_seq"] != (ep.pkt_prev + 1) & 0xFFFF:
                self.stats["pkt_seq_gaps"] += 1
                self.stats["pkt_seq_lost"] += (f["pkt_seq"] - ep.pkt_prev - 1) & 0xFFFF
            ep.pkt_prev = f["pkt_seq"]

        hole = 1.5 * TICK_HZ / ep.odr
        for k, (dt, gx, gy, gz, ax, ay, az) in enumerate(f["samples"]):
            idx = f["first_idx"] + k
            if idx < ep.next_idx:
                self.stats["dupes"] += 1
                continue
            t64 = ep.ticks64((f["t0"] + dt) & 0xFFFFFFFF)
            if ep.t_offset is None:
                self._set_offset(ep, t64)
                ep.first_t = ep.t_offset + t64 / TICK_HZ
            t = ep.t_offset + t64 / TICK_HZ
            if self.last_t is not None and t <= self.last_t:
                # time went backwards inside a boot: a pod bug. Count it, never emit it.
                self.stats["t_backwards"] += 1
                if self.stats["t_backwards"] == 1:
                    self._fault("time went backwards")
                ep.next_idx = idx + 1
                continue
            if idx > ep.next_idx:
                if f["flags"] & F_OVERFLOW:
                    kind = "overflow"
                elif first_frame and k == 0 and f["flags"] & F_RESYNC and cmd["resync_skip"]:
                    kind = "resync"
                else:
                    kind = "unexplained"
                self._gap(ep, kind, idx - ep.next_idx, ep.next_idx, self._t0_left(ep, t), t)
            elif ep.last_idx is not None and idx == ep.last_idx + 1 and t64 - ep.last_ticks64 > hole:
                kind = "imu-restart" if k == 0 and f["flags"] & F_IMU_RESTART else "imu"
                dtk = t64 - ep.last_ticks64
                # floor(x + 0.5): rounds .5 up like JS Math.round (Python's round() goes to even)
                self._gap(ep, kind, math.floor(dtk * ep.odr / TICK_HZ + 0.5) - 1, idx, ep.last_t, t)
            s = {"t": t, "ax": ax * ep.accel_lsb, "ay": ay * ep.accel_lsb, "az": az * ep.accel_lsb,
                 "gx": gx * ep.gyro_lsb, "gy": gy * ep.gyro_lsb, "gz": gz * ep.gyro_lsb}
            ep.next_idx = idx + 1
            ep.last_idx, ep.last_ticks64, ep.last_t = idx, t64, t
            self.last_t = t
            self.emitter = ep
            self.stats["samples"] += 1
            self.on_sample(s)

    def _set_offset(self, ep, t64):
        """tOffset for an epoch's first emitted sample (section 7, Clock)."""
        prev = self.emitter
        if prev is None:
            ep.t_offset = -t64 / TICK_HZ                     # t = 0 at the very first sample
            return
        ep.t_offset = prev.t_offset + (ep.wall_base - prev.wall_base) / 1000
        if ep.t_offset + t64 / TICK_HZ <= self.last_t + 0.001:
            ep.t_offset = self.last_t + 0.001 - t64 / TICK_HZ
            self.stats["clock_clamped"] += 1
            self.on_event("clock-clamped", None)

    # ---- what the phone writes ----
    def command(self):
        """The STREAM command to write now (section 7, What to send), with the next cmd_seq.
        None while the phone must not stream (another pod, a bad INFO, INFO to re-read)."""
        if self.ignoring or self.need_info:
            return None
        ep = self.ep
        if self.stop is not None:
            self.stale_due += 1             # its stream_stopped may still come: the old stream's end
        self.stop = None                    # a STREAM command cancels a pending STOP wait
        self.cmd_seq = (self.cmd_seq + 1) & 0xFF
        if ep is not None and ep.next_idx is not None:
            self.cmd = {"op": OP_STREAM_FROM, "seq": self.cmd_seq, "from": ep.next_idx, "started": False}
            return struct.pack("<BBII", OP_STREAM_FROM, self.cmd_seq, ep.boot_id, ep.next_idx)
        if ep is not None and ep.is_reboot:
            self.cmd = {"op": OP_STREAM_FROM, "seq": self.cmd_seq, "from": 0, "started": False}
            return struct.pack("<BBII", OP_STREAM_FROM, self.cmd_seq, ep.boot_id, 0)
        self.cmd = {"op": OP_STREAM_LIVE, "seq": self.cmd_seq, "back": LIVE_BACK, "started": False}
        return struct.pack("<BBH", OP_STREAM_LIVE, self.cmd_seq, LIVE_BACK)

    def stop_command(self):
        """STOP. Starts the stop wait (section 7, Stop): resolve on the 'stopped' event."""
        st = self.ep.status if self.ep else None
        self.stop = {"head": st["head"] if st else None, "timed_out": False}
        return bytes([OP_STOP])

    def stop_wait_ms(self):
        """How long to wait for the stream_stopped STATUS: 1 s more than the pod's own cap,
        with the latest head up to 1 s stale (+1.2 s of samples)."""
        ep = self.ep
        st = ep.status if ep else None
        if st is None or st["n_per_packet"] < 4 or ep.next_idx is None:
            return 2500.0
        behind = max(0, st["head"] - ep.next_idx) + 1.2 * ep.odr
        return 2500.0 + behind / (60 * st["n_per_packet"]) * 1000

    def stop_timed_out(self):
        """No stream_stopped in time. Raise the fault and keep the stream open: a late
        stream_stopped still closes it exactly; finish() gives up on it. False if the wait was
        already over (stopped, or a STREAM command cancelled it)."""
        if self.stop is None or self.stop["timed_out"]:
            return False
        self.stop["timed_out"] = True
        self._fault("stop timed out")
        return True

    def finish(self):
        """The session is over (the app ends it or tears the link down). A stream still open
        gets a lower-bound tail up to the last head the phone heard."""
        ep = self.ep
        kind = "stop-tail" if self.stop is not None else "lost-tail"
        if ep is not None and ep.open:
            self._close(ep, ep.status["head"] if ep.status else None, ep.status_t64, kind, lower_bound=True)
        elif ep is not None:
            self._owed(ep, kind)
        self.stop = None

    def wall_at(self, t):
        """Arrival-clock ms of sample time t, using the latest wallBase of the epoch t came from
        (the newest epoch whose first sample is at or before t)."""
        eps = [e for e in self.epochs if e.first_t is not None]
        ep = next((e for e in reversed(eps) if e.first_t <= t), eps[0])
        return ep.wall_base + (t - ep.t_offset) * 1000


# ---- the pod's side: frame builders (used by make_fake_pod.py's simulator) ----
def build_data(pkt_seq, first_idx, t0_ticks, flags, cmd_seq, samples):
    """samples: [(ticks, (gx, gy, gz, ax, ay, az)), ...], consecutive indices from first_idx."""
    out = bytearray(DATA_HDR.pack(T_DATA, len(samples), pkt_seq & 0xFFFF, first_idx & 0xFFFFFFFF,
                                  t0_ticks & 0xFFFFFFFF, flags, cmd_seq & 0xFF))
    for ticks, raw in samples:
        out += DATA_SMP.pack((ticks - t0_ticks) & 0xFFFFFFFF, *raw)
    return bytes(out)


def build_status(flags, mv, ticks_now, head, lost, missed, reason, npp, interval):
    return STATUS_FMT.pack(T_STATUS, flags, mv, ticks_now & 0xFFFFFFFF, head & 0xFFFFFFFF,
                           min(lost, 0xFFFF), min(missed, 0xFFFF), reason, npp, interval)


def build_info(boot_id, serial, ring_n, wake=0, fw=1, odr=208, accel_lsb=ACCEL_LSB, gyro_lsb=GYRO_LSB, proto=1):
    return INFO_FMT.pack(T_INFO, proto, fw, boot_id, serial, accel_lsb, gyro_lsb, odr, ring_n, wake, 0, TICK_HZ, 0)


def build_cal():
    return bytes([T_CAL, 0]) + bytes(62)


# ---- replaying captures (the make_fake_pod.py vector format, P2) ----
MANIFEST = "data/pod-capture-manifest.json"   # relative to analysis/


def replay_rows(rows, on_sample=None):
    """Feed capture rows (kind, arrival_ms, payload) into a fresh decoder, checking every logged
    command against the one the decoder builds. Returns (samples, stream, events) or raises.
    pkt rows (a received CRP packet) go through packet(), frame rows (captures from before D45)
    straight to frame(); tx rows (packets sent) and link rows are for reading only.
    on_sample(stream, sample), if given, sees each sample as it comes out (stream.ep is its epoch)."""
    samples, events = [], []

    def keep(smp):
        samples.append(smp)
        if on_sample:
            on_sample(s, smp)
    s = PodStream(keep, lambda k, i: events.append((k, i)))
    for kind, arr, payload in rows:
        if kind == "pkt":
            s.packet(bytes.fromhex(payload), arr)
        elif kind == "frame":
            s.frame(bytes.fromhex(payload), arr)
        elif kind == "cmd":
            b = s.command()
            got = "none" if b is None else b.hex()
            if got != payload:
                raise AssertionError(f"replayed command {got} != logged {payload}")
        elif kind == "stop":
            if s.stop_command().hex() != payload:
                raise AssertionError("replayed STOP bytes differ")
        elif kind == "call":
            if payload == "finish":
                s.finish()
            elif payload.startswith("stop_wait_ms="):
                got = s.stop_wait_ms()
                if repr(got) != payload.split("=", 1)[1]:
                    raise AssertionError(f"replayed stop_wait_ms {got!r} != {payload}")
            elif payload.startswith("stop_timed_out="):
                got = int(s.stop_timed_out())
                if str(got) != payload.split("=", 1)[1]:
                    raise AssertionError(f"replayed stop_timed_out {got} != {payload}")
    return samples, s, events


def read_capture(path):
    """A capture CSV (scenario,step,kind,arrival_ms,payload) -> [(kind, arrival_ms, payload)]."""
    import csv
    with open(path, newline="") as f:
        return [(r["kind"], float(r["arrival_ms"]) if r["arrival_ms"] else None, r["payload"])
                for r in csv.DictReader(f)]


def capture_digest(rows):
    """What a capture decodes to, compactly: a hash of every sample (repr floats, so bit exact)
    plus the gaps, stats, faults and event counts in full."""
    import hashlib
    samples, s, events = replay_rows(rows)
    h = hashlib.sha256()
    for smp in samples:
        h.update(repr(tuple(smp[k] for k in ("t", "ax", "ay", "az", "gx", "gy", "gz"))).encode())
    kinds = {}
    for k, _ in events:
        kinds[k] = kinds.get(k, 0) + 1
    return {"rows": len(rows), "samples": len(samples), "sha256": h.hexdigest(),
            "first_t": samples[0]["t"] if samples else None, "last_t": samples[-1]["t"] if samples else None,
            "gaps": s.gaps, "stats": s.stats, "faults": s.faults, "events": kinds}


def replay_all(write=False):
    """Every committed pod-capture-*.csv against the manifest (P2: old recordings keep decoding
    the same). write=True first pins the captures the manifest doesn't have yet, from the decoder
    as it is now. A capture already pinned is only ever checked: to change a pin on purpose, delete
    its entry from the manifest first."""
    import glob
    import json
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, MANIFEST)
    want = {}
    if os.path.exists(path):
        with open(path) as f:
            want = json.load(f)
    got = {}
    for p in sorted(glob.glob(os.path.join(here, "data", "pod-capture-*.csv"))):
        try:                        # through JSON once, so a digest compares equal to the manifest's copy
            got[os.path.basename(p)] = json.loads(json.dumps(capture_digest(read_capture(p))))
        except Exception as e:      # one capture that no longer replays is that capture's FAIL
            got[os.path.basename(p)] = {"error": f"doesn't replay: {type(e).__name__}: {e}"}
    if write:
        new = {n: d for n, d in got.items() if n not in want and "error" not in d}
        want.update(new)
        with open(path, "w", newline="\n") as f:
            json.dump(want, f, indent=1, sort_keys=True)
            f.write("\n")
        for n in sorted(new):
            print(f"NEW   {n}")
        print(f"wrote {MANIFEST}: {len(new)} new, {len(want)} captures")
    ok = True
    for name in sorted(set(want) | set(got)):
        if name not in got:
            print(f"FAIL  {name}: in the manifest, file missing")
        elif "error" in got[name]:
            print(f"FAIL  {name}: {got[name]['error']}")
        elif name not in want:
            print(f"FAIL  {name}: not in the manifest")
        elif got[name] != want[name]:
            diff = [k for k in sorted(set(want[name]) | set(got[name])) if got[name].get(k) != want[name].get(k)]
            print(f"FAIL  {name}: {', '.join(diff)} differ")
        else:
            print(f"PASS  {name}: {got[name]['samples']} samples, {len(got[name]['gaps'])} gaps")
            continue
        ok = False
    print(f"replay {'PASS' if ok else 'FAIL'}: {len(got)} captures")
    return ok


if __name__ == "__main__":
    import sys
    if "--replay-all" in sys.argv or "--write-manifest" in sys.argv:
        sys.exit(0 if replay_all(write="--write-manifest" in sys.argv) else 1)
    sys.exit("usage: python podlink.py --replay-all | --write-manifest")
