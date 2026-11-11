"""
Test bench for PodLink v1 over CRP (docs/ble.md, D45): a simulated pod with its CRP engine per link,
a simulated radio link, a simulated USB cable and the clients' decoders (podlink.PodStream), run
through the scenarios of docs/ble-plan.md step 2 and D45's own.

    python make_fake_pod.py            # runs every scenario, prints PASS / FAIL per scenario

The pod models docs/ble.md 2 and 5 on each link (one struct per link, as firmware/pod/link.ino):
no session until a HELLO (which resets PUSH and the stream), the PUSH register, a spool of 4 for
the engine's own packets, pushes only with PUSH on, an empty spool, a free slot and room (a
TX-complete can land mid-pass, between the RX processing and sendPass, as the BLE task's does), a NOP
after 1 s of sending nothing (straight out only, else dropped), BUSY for a READ whose answer
wouldn't fit, reads built when the READ is processed, a WRITE's RESPONSE before the STATUS that
answers it, n_max = min(16, (MTU - 23) / 14). USB: text mode (the CSV) until a level 1 HELLO, a
2 KB byte spool with a 128 B reserve for the engine, 3 s of silence ends the session. The phone
(Bench) runs the connection order of docs/ble.md 7 with request / response, txn ids exactly as
CRPClient allocates them (crp.TxnAllocator), no retries; a PC (UsbHost) does the same over USB.

Every scenario checks, against the simulator's ground truth (never against the decoder's own
numbers):
  - accounting: per boot, the emitted sample indices plus the index gaps tile the expected
    range exactly once (no overlap, no hole), so no loss is ever silent
  - values: each emitted sample = the pod's raw counts x the pod's own scale factors (as float32
    in INFO), exactly
  - time: t strictly increasing; within a boot, t differences = tick differences / 32768; every
    gap's t0 / t1 = the t of the samples next to it
  - clock: each boot's wallBase = the minimum of arrival - ticks / 32.768 over its last 32 STATUS
  - every anomaly counter is 0 unless the scenario expects it, and the faults are exactly the
    expected ones; lower-bound gaps never exceed the true loss
Two scenarios are negative controls (crp-push-before-info, crp-write-retry): they pass by showing
the faults their wrong client order causes.
Then each decoder's whole input is replayed into a fresh decoder, twice (as packets, and as the
frames the adapter takes out of them), and must give identical samples, gaps, stats, faults,
events and command bytes. Those logs are the test vectors for the JS port (S3):
  data/fake_pod_packets.csv   scenario, step, kind, arrival_ms, payload
                              kind pkt: a CRP packet received (hex), fed through packet(); tx: a
                              packet sent (hex, for reading only); link: what the link did (for
                              reading only); cmd: command() must return payload (the CTRL bytes,
                              hex, or "none"); stop: stop_command(); call: payload is
                              stop_timed_out=<0|1>, stop_wait_ms=<ms> or finish
  data/fake_pod_frames.csv    the same runs through podlink.crp_frame: frame rows (feed payload,
                              hex) plus cmd / stop / call, so the frame path stays tested
  data/fake_pod_expected.csv  scenario, t, ax, ay, az, gx, gy, gz     (every emitted sample)
  data/fake_pod_gaps.csv      scenario, kind, n, idx, t0, t1, lower_bound
  data/fake_pod_stats.csv     scenario, every PodStream counter, faults (joined by |)
  data/fake_pod_events.csv    scenario, k, kind, info
A scenario with a PC on USB beside the phone writes the PC's decoder as "<scenario>/usb".
(all fake_*.csv: gitignored, regenerate any time)
"""

import math
import random
import struct
import sys
import traceback
from pathlib import Path

import crp
import podlink as pl

DATA = Path(__file__).parent / "data"
TPS = 32.768                      # ticks per ms
ANOMALIES = ("bad", "pre_info", "ignored", "stale", "stale_stops", "after_close", "dupes",
             "t_backwards", "pkt_seq_gaps", "pkt_seq_lost", "clock_clamped")
INDEX_KINDS = ("overflow", "resync", "unexplained", "stop-tail", "sleep-tail", "lost-tail")

# the pod's link rules (docs/ble.md 2.2, 2.5 and 5)
SLOTS = 3                         # BLE: notifications in flight, shared by every packet
SPOOL_N = 4                       # BLE: engine packets waiting for a slot
USB_SPOOL, USB_RESERVE = 2048, 128    # USB: the byte spool; a push needs 128 B spare
USB_RATE = 64                     # bytes per ms the PC takes off the pod's USB (the sim's choice)
RX_N, RX_MAX = 8, 64              # the pod's RX queue: 8 packets of up to 64 B
HEARTBEAT_MS = 1000               # the engine's NOP after 1 s of sending nothing; USB clients' NOP
LIVENESS_MS = 3000                # USB: 3 s without receiving ends the session (ignored on BLE)
MTU_EXCHANGE_MS = 50              # the pod's own MTU exchange, at connect + 50 ms
READ_ONLY = (pl.REG_PROTO, pl.REG_INFO, pl.REG_STATUS, pl.REG_CAL, pl.REG_DATA,
             pl.REG_LINK_KIND, pl.REG_LINK_MTU, pl.REG_LINK_N_MAX)
BENCH_CHARS = (b"s", b"o", b"w", b"i", b"h")
SWITCH = crp.frame_l1(crp.hello())[:4]      # 0A 00 01 0B: a level 1 HELLO switches USB to CRP

# the clients (docs/ble.md 2.2 and 5)
TXN_TIMEOUT_MS = 5000             # CRPClient txnTimeoutMs; txnRetries 0
BUSY_RETRY_MS, BUSY_FOR_MS = 200, 3000      # a BUSY read is retried every 200 ms for 3 s


def f32(x):
    """x as the float32 INFO carries it."""
    return struct.unpack("<f", struct.pack("<f", x))[0]


def raw_for(i):
    """Deterministic raw counts for sample i: gx gy gz ax ay az (int16)."""
    return (int(900 * math.sin(i / 37)), int(500 * math.cos(i / 53)), (i * 7) % 2000 - 1000,
            int(4000 * math.sin(i / 29)), int(3000 * math.cos(i / 41)), 4100 + (i % 11) - 5)


def csv_line(pod, i):
    """Sample i as the pod prints it on USB in text mode (t_us, then the scaled values)."""
    gx, gy, gz, ax, ay, az = pod.raw(i)
    t_us = (pod.ticks[i] * 15625 // 512) & 0xFFFFFFFF
    a, g = pod.accel_lsb, pod.gyro_lsb
    return f"{t_us},{ax * a:.4f},{ay * a:.4f},{az * a:.4f},{gx * g:.3f},{gy * g:.3f},{gz * g:.3f}\r\n".encode()


class Link:
    """One link of the pod: its CRP engine (docs/ble.md 2.2, 2.3), its transport (5) and its stream
    (3 to 6). BLE: 3 notification slots shared by every packet; the engine's packets go straight
    out or wait in a spool of 4, and pushes wait while it holds any. USB: text mode until a level 1
    HELLO, then CRP over a 2 KB byte spool (pushes need 128 B spare), read by the PC's UsbHost."""

    def __init__(self, pod, kind):
        self.pod, self.b, self.kind = pod, pod.b, kind
        self.connected = False            # BLE: a connection; USB: the port is open
        self.crp = kind == "ble"          # USB: False in text mode (docs/ble.md 2.5)
        # the engine and its transport
        self.session = False              # a HELLO came and nothing has ended it since
        self.push = 0                     # the PUSH register
        self.spool = []                   # BLE: packets; USB: [kind, bytearray] ("text", "pkt", "junk")
        self.spool_bytes = 0              # USB: bytes waiting
        self.spool_max = 0                # the most it ever held (packets on BLE, bytes on USB)
        self.inflight = 0                 # BLE: notifications sent minus TX-complete
        self.rx = []                      # packets received, processed by the next pass
        self.tx_drops = self.rx_drops = 0
        self.nop_drops = 0                # NOPs the engine sent that couldn't go straight out
        self.spool_holds = 0              # BLE: pushes held back by the spool alone (a slot was free)
        self.last_send = self.last_recv = 0.0
        self.mtu = 23
        self.mtu_at = None                # when the pod's own MTU exchange completes
        self.info_override = None         # test-only: the next INFO read answers this (a cached value)
        self.session_ends = []            # (wall, why, hold) per session that ended on a live link
        self.deframer = None              # USB: level 1, from the switch on
        self.textbuf = b""                # USB text mode: the last bytes, to find a split switch
        self.crp_since = None
        self.csv = 0                      # USB text mode: the next sample to print
        self.mode_log = []                # USB: (wall, "crp" / "text", why)
        self.next_log_wall = None         # USB CRP: the next 30 s LOG (the "#" lines)
        self.junk = []                    # test-only: bytes put on the USB wire before the next DATA pushes
        # the stream
        self.streaming = False
        self.cursor = 0
        self.cmd_seq = 0
        self.pkt_seq = 0
        self.resync = self.overflow = False
        self.lost = 0
        self.lost_mark = 0
        self.retry = None                 # a built DATA frame waiting until the link takes it
        self.status_due = []              # STATUS frames to send before any further DATA
        self.next_status_wall = None
        self.drain = None                 # {"head", "deadline", "kind"} during STOP / sleep
        self.cmd_log = []                 # (op, cursor, head) per STREAM command handled
        self.drain_heads = []             # (kind, head) per finished STOP / sleep drain
        self.rewind = 0                   # test-only fault: resend this many samples on STREAM_FROM

    # ---- sizing ----
    def mtu_eff(self):
        return min(self.mtu, 247)

    def max_pkt(self):
        """The longest packet the link takes: MTU - 3 on BLE, a level 1 packet on USB."""
        return self.mtu_eff() - 3 if self.kind == "ble" else crp.L1_MAX

    def n_max(self):
        if self.kind == "usb":
            return 16
        return min(16, max(0, (self.mtu_eff() - 23) // 14))    # MTU - 3 - 6 (the push header) - 14

    def interval(self):
        return 32 if self.kind == "ble" else 0

    def free(self):
        return USB_SPOOL - self.spool_bytes

    # ---- frames ----
    def status_frame(self, extra_flags=0, head=None):
        p = self.pod
        flags = extra_flags
        if self.streaming:
            flags |= pl.S_STREAMING
        if self.kind == "ble" and self.connected and self.n_max() < 4:
            flags |= pl.S_MTU_TOO_SMALL
        h = p.head if head is None else head
        return pl.build_status(flags, 3950, p.now_ticks(), h, self.lost, 0,
                               p.sleep_reason if extra_flags & pl.S_GOING_TO_SLEEP else 0,
                               self.n_max(), self.interval())

    def queue_status(self, extra_flags=0, head=None):
        self.status_due.append(self.status_frame(extra_flags, head))

    def build_data(self):
        """Build the next DATA frame from the cursor, or None (docs/ble.md 3 and 5)."""
        p = self.pod
        nm = self.n_max()
        if nm < 4:
            return None
        if p.head - self.cursor > p.ring_n:                             # sampling lapped us
            lo = p.head - p.ring_n
            self.lost += max(0, lo - max(self.cursor, self.lost_mark))
            self.lost_mark = max(self.lost_mark, lo)
            self.cursor = lo
            self.overflow = True
        end = self.drain["head"] if self.drain else p.head
        pending = end - self.cursor
        if pending <= 0:
            return None
        age_ticks = p.now_ticks() - p.ticks[self.cursor]
        n = min(nm, pending)
        # always start a new frame at an IMU restart, and keep dt under 60000 ticks
        for k in range(1, n):
            if (self.cursor + k) in p.restarts or p.ticks[self.cursor + k] - p.ticks[self.cursor] > 60000:
                n = k
                break
        cut_short = n < min(nm, pending)
        if n < nm and not (self.drain or cut_short or age_ticks >= 2621):
            return None                   # wait for a full frame
        flags = 0
        if self.resync:
            flags |= pl.F_RESYNC
        if self.overflow:
            flags |= pl.F_OVERFLOW
        if self.cursor in p.restarts:
            flags |= pl.F_IMU_RESTART
        if self.drain and self.cursor + n == self.drain["head"]:
            flags |= pl.F_FINAL
        smp = [(p.ticks[i], p.raw(i)) for i in range(self.cursor, self.cursor + n)]
        frame = pl.build_data(self.pkt_seq, self.cursor, p.ticks[self.cursor], flags, self.cmd_seq, smp)
        return {"bytes": frame, "n": n}

    # ---- the transport (docs/ble.md 5) ----
    def hvx(self, pkt, lost=False):
        """One notification (BLE): a slot, then the air."""
        self.inflight += 1
        self.b.pod_sent(pkt, lost)

    def usb_put(self, kind, data):
        self.spool.append([kind, bytearray(data)])
        self.spool_bytes += len(data)
        self.spool_max = max(self.spool_max, self.spool_bytes)

    def send(self, pkt):
        """The transport's send() for the engine's own packets (HELLO, RESPONSE, BYE, LOG, NOP):
        straight out if nothing waits and a slot is free, else into the spool, else dropped and
        counted. A NOP only ever goes straight out; if it can't, it's dropped and reported as sent,
        since anything queued already proves liveness. The engine counts the send when it worked."""
        nop = pkt == crp.nop()
        if self.kind == "ble":
            if len(pkt) > self.max_pkt():
                self.tx_drops += 1                # (discovery answers; nothing the bench asks)
                return
            if not self.spool and self.inflight < SLOTS:
                self.hvx(pkt)
            elif nop:
                self.nop_drops += 1
            elif len(self.spool) < SPOOL_N:
                self.spool.append(pkt)
                self.spool_max = max(self.spool_max, len(self.spool))
            else:
                self.tx_drops += 1
                return
        else:
            f = crp.frame_l1(pkt)
            if nop and self.spool_bytes:          # USB: straight out = into an empty spool
                self.nop_drops += 1
            elif self.free() >= len(f):
                self.usb_put("pkt", f)
            else:
                self.tx_drops += 1
                return
        self.last_send = self.b.now

    def push_frame(self, frame, is_data):
        """A STATUS or DATA push (docs/ble.md 5), True if the link took it. PUSH off: a STATUS counts
        as sent, DATA waits. Otherwise it goes only with nothing in the spool (BLE) or 128 B spare
        (USB), a free slot and room for it; else its frame waits and is resent byte-identical."""
        if not self.push:
            return not is_data
        pkt = crp.push(pl.REG_DATA if is_data else pl.REG_STATUS, frame)
        if self.kind == "ble":
            if self.spool or self.inflight >= SLOTS or len(pkt) > self.max_pkt():
                self.spool_holds += bool(self.spool) and self.inflight < SLOTS    # the spool rule decided
                return False
            if is_data and self.b.drop_next_data:
                self.b.drop_next_data = False
                self.hvx(pkt, lost=True)          # sent, then lost on the air (test-only fault)
            else:
                self.hvx(pkt)
        else:
            f = crp.frame_l1(pkt)
            if self.free() < len(f) + USB_RESERVE:
                return False
            if is_data and self.junk:
                self.usb_put("junk", self.junk.pop(0))     # test-only: bytes no packet owns
            self.usb_put("pkt", f)
        self.last_send = self.b.now
        return True

    def spool_drain(self):
        while self.kind == "ble" and self.spool and self.inflight < SLOTS:
            self.hvx(self.spool.pop(0))

    def receive(self, pkt):
        """One packet into the RX queue (BLE: one write is one packet)."""
        if len(self.rx) >= RX_N or len(pkt) > RX_MAX:
            self.rx_drops += 1
        else:
            self.rx.append(bytes(pkt))

    def receive_bytes(self, data):
        """USB bytes from the PC: in text mode only the switch counts, in CRP mode level 1."""
        if not self.crp:
            buf = self.textbuf + data
            k = buf.find(SWITCH)
            if k < 0:
                self.textbuf = buf[-3:]           # a switch split across reads
                return                            # text: the bench characters, not modelled
            self.to_crp()
            data = buf[k:]
        for pkt in self.deframer.feed(data):
            self.receive(pkt)

    # ---- the engine (the vendored crp_engine.c's rules) ----
    def process_all(self):
        while self.rx and self.connected and self.crp:
            self.process(self.rx.pop(0))

    def process(self, pkt):
        self.last_recv = self.b.now
        p = crp.parse(pkt)
        if p is None:
            return
        t = p["type"]
        if not self.session and t not in (crp.HELLO, crp.NOP):
            return                                # before a HELLO: everything but HELLO and NOP ignored
        if t == crp.HELLO:
            if p["ver"] < crp.VERSION:
                self.send(crp.bye(0, pl.DEVICE_ID))
                return
            if self.session:
                self.session_end("hello")         # a HELLO during a session starts a new one
            self.session, self.push = True, 0
            self.send(crp.hello(crp.VERSION, 1, 244 if self.kind == "ble" else 1024, crp.FLAG_PUSH, pl.DEVICE_ID))
            if self.kind == "usb":
                self.log_line("usb crp session")
                self.next_log_wall = self.b.now + 30000
        elif t == crp.BYE or (t == crp.END and p["ch"] == crp.SESSION_CHANNEL):
            self.session_end("bye" if t == crp.BYE else "end")
            self.session = False
            if self.kind == "usb":
                self.to_text("bye" if t == crp.BYE else "end")
        elif t == crp.READ:
            self.on_read(p)
        elif t == crp.WRITE and p["txn"]:
            self.on_write(p)

    def on_read(self, p):
        """A READ (docs/ble.md 2.3). INFO, STATUS and CAL are built now: a STATUS read's head is at
        least that of any DATA already sent. An answer that wouldn't fit gets BUSY (8 B)."""
        reg, st, val = p["reg"], crp.OK, b""
        if reg == pl.REG_INFO:
            val = self.info_override if self.info_override is not None else self.pod.info_frame()
        elif reg == pl.REG_STATUS:
            val = self.status_frame()
        elif reg == pl.REG_CAL:
            val = pl.build_cal()
        elif reg == pl.REG_PROTO:
            val = bytes([1])
        elif reg == pl.REG_PUSH:
            val = bytes([self.push])
        elif reg == pl.REG_LINK_KIND:
            val = bytes([1 if self.kind == "ble" else 2])
        elif reg == pl.REG_LINK_MTU:
            val = struct.pack("<H", self.mtu_eff() if self.kind == "ble" else 0)
        elif reg == pl.REG_LINK_N_MAX:
            val = bytes([self.n_max()])
        elif reg in (pl.REG_DATA, pl.REG_CTRL) or (reg == pl.REG_BENCH and self.kind == "usb"):
            st = crp.ACCESS_DENIED                # pushed only / write only
        else:
            st = crp.UNKNOWN_REG
        if 8 + len(val) > self.max_pkt():
            st, val = crp.BUSY, b""
        elif reg == pl.REG_INFO:
            self.info_override = None
        self.send(crp.response(p["txn"], st, reg, val))

    def on_write(self, p):
        reg, val = p["reg"], p["value"]
        if reg == pl.REG_CTRL:
            st = self.pod.ctrl(self, val)
        elif reg == pl.REG_PUSH:
            st = crp.OK if val in (b"\x00", b"\x01") else crp.INVALID_VALUE
            if st == crp.OK:
                self.push = val[0]
        elif reg == pl.REG_BENCH and self.kind == "usb":
            st = crp.OK if val in BENCH_CHARS else crp.INVALID_VALUE   # the bench itself: not modelled
        elif reg in READ_ONLY:
            st = crp.ACCESS_DENIED
        else:
            st = crp.UNKNOWN_REG
        self.send(crp.response(p["txn"], st, reg))   # before any STATUS answering it: pushes wait for the spool

    def log_line(self, msg):
        """A "#" line in CRP mode (USB only): a LOG packet, module "pod"."""
        self.send(crp.log(crp.LOG_INFO, int(self.b.now) & 0xFFFFFFFF, "pod", msg))

    # ---- sessions, modes, connections ----
    def end_stream(self):
        self.streaming = False
        self.retry = None
        self.status_due = []
        if self.drain and self.drain["kind"] == "sleep":
            self.drain = None
            self.b.pod_go_to_sleep_2()    # mid sleep-drain: sleep without the STATUS
        self.drain = None

    def session_end(self, why):
        """A session that ends on a live link ends its stream as a disconnect does (docs/ble.md 2.2):
        streaming off, queued STATUS and the built frame dropped, PUSH off. The hold stays."""
        self.session_ends.append((self.b.now, why, self.pod.hold))
        self.push = 0
        if self.kind == "ble" and why != "hello":
            self.spool = []                   # BYE / END empty the packet spool, as podcrp.c does (USB
                                              # keeps its bytes: no half packet before the text)
        self.end_stream()

    def on_connect(self):
        b = self.b
        self.connected = True
        self.mtu = b.mtu if b.native_mtu else 23
        self.mtu_at = None if b.native_mtu else b.now + b.mtu_delay()
        self.session, self.push = False, 0
        self.spool, self.rx, self.inflight = [], [], 0
        self.last_send = self.last_recv = b.now
        self.next_status_wall = b.now + 1000

    def on_disconnect(self):
        self.connected = False
        self.session, self.push = False, 0
        self.inflight = 0
        self.spool, self.rx = [], []
        self.next_status_wall = None
        self.end_stream()

    def on_open(self):
        """USB: the PC opened the port. Text mode, the CSV live from head."""
        self.connected = True
        self.crp = False
        self.textbuf = b""
        self.csv = self.pod.head

    def on_close(self):
        if self.crp:
            self.to_text("closed")
        self.connected = False
        self.spool, self.spool_bytes, self.rx = [], 0, []

    def to_crp(self):
        now = self.b.now
        self.crp = True
        self.crp_since = self.last_recv = self.last_send = now
        self.deframer = crp.Deframer()
        self.textbuf = b""
        self.session, self.push = False, 0
        self.next_status_wall = now + 1000
        self.mode_log.append((now, "crp", "hello"))

    def to_text(self, why):
        """USB back to text (docs/ble.md 2.5): the engine reset, the stream ended, the CSV live from
        head. Packets already in the spool still go out, the text after them."""
        if self.session:
            self.session_end(why)
        self.push = 0
        self.end_stream()
        self.session, self.crp = False, False
        self.rx, self.deframer = [], None
        self.next_status_wall = self.next_log_wall = None
        self.csv = self.pod.head
        self.mode_log.append((self.b.now, "text", why))

    def print_csv(self):
        """Text mode: one CSV line per new sample, as the pod prints it."""
        while self.csv < self.pod.head:
            line = csv_line(self.pod, self.csv)
            if self.free() < len(line):
                break
            self.usb_put("text", line)
            self.csv += 1

    # ---- the stream ----
    def start_stream(self, op, seq, cur):
        p = self.pod
        self.drain = None                 # a STREAM command ends a STOP drain, no STATUS
        self.retry = None                 # an unsent built frame belonged to the old stream
        if op == pl.OP_STREAM_FROM and self.rewind:
            cur = max(0, cur - self.rewind)
        self.cursor, self.cmd_seq = cur, seq
        self.cmd_log.append((op, cur, p.head))
        self.resync = True
        self.streaming = True
        if self.kind == "ble":
            p.hold = True                 # USB never touches the session hold
        self.queue_status()

    def send_pass(self):
        """sendPass: STATUS first, then up to 4 DATA frames."""
        while self.status_due:
            if not self.push_frame(self.status_due[0], is_data=False):
                return
            self.status_due.pop(0)
        if not (self.streaming and self.connected):
            return
        for _ in range(4):
            if self.retry is None:
                self.retry = self.build_data()
            if self.retry is None:
                break
            if not self.push_frame(self.retry["bytes"], is_data=True):
                break
            self.cursor += self.retry["n"]
            self.pkt_seq = (self.pkt_seq + 1) & 0xFFFF
            self.resync = self.overflow = False
            self.retry = None
        if self.drain and self.cursor >= self.drain["head"] and self.retry is None:
            self.finish_drain()

    def finish_drain(self):
        d, self.drain = self.drain, None
        self.drain_heads.append((d["kind"], d["head"]))
        self.retry = None
        self.streaming = False
        if d["kind"] == "stop":
            self.queue_status(pl.S_STREAM_STOPPED, head=d["head"])
        else:
            self.b.pod_go_to_sleep_2()

    # ---- one loop() pass for this link ----
    def step(self):
        """docs/ble.md 5 and the notes' pass order: the spool, the RX queue, the link's own work and
        sendPass, the spool again, then the engine's tick (the NOP; USB: liveness)."""
        b, pod, now = self.b, self.pod, self.b.now
        if self.kind == "usb" and not self.crp:
            return self.print_csv()
        self.spool_drain()
        self.process_all()
        if not (self.connected and self.crp) or b.pod is not pod:
            return
        if self.mtu_at is not None and now >= self.mtu_at:
            self.mtu_at = None                    # the pod's own MTU exchange, once
            if self.mtu != b.mtu:
                self.mtu = b.mtu
                self.queue_status()               # fresh STATUS: mtu_too_small cleared
        if self.next_status_wall is not None and now >= self.next_status_wall:
            self.next_status_wall += 1000
            self.queue_status()
        if self.next_log_wall is not None and now >= self.next_log_wall:
            self.next_log_wall += 30000
            self.log_line(f"batt 3.95 V head {pod.head}")
        if self.drain and (now >= self.drain["deadline"] or self.n_max() < 4 or not self.push):
            self.finish_drain()       # the cap: the rest is the phone's stop-tail / sleep-tail
            if b.pod is not pod:
                return
        if self.kind == "ble":
            b.tx_complete(self, now - 0.5)    # a slot can free mid-pass: the spool rule decides
        self.send_pass()
        self.spool_drain()
        if self.session and now - self.last_send >= HEARTBEAT_MS:
            self.send(crp.nop())
        if self.kind == "usb":
            if not self.session and now - self.crp_since >= LIVENESS_MS:
                self.to_text("no hello")
            elif now - self.last_recv >= LIVENESS_MS:
                self.to_text("silence")


class Pod:
    """The pod's side of docs/ble.md: sampling, the ring, CTRL ops, sleep; one Link per link."""

    def __init__(self, bench, boot_id, serial, wall_ms, rate_hz=211.3, ring_n=8192, tick_start=0,
                 drift_ppm=12.0, wake=0, accel_lsb=pl.ACCEL_LSB, gyro_lsb=pl.GYRO_LSB):
        self.b = bench
        self.boot_id, self.serial, self.ring_n, self.wake = boot_id, serial, ring_n, wake
        self.accel_lsb, self.gyro_lsb = accel_lsb, gyro_lsb
        self.boot_wall = wall_ms
        self.rate, self.tick_start, self.drift = rate_hz, tick_start, drift_ppm * 1e-6
        self.ticks = []                   # extended (unwrapped) ticks of every sample, by index
        self.head = 0
        self.next_sample_wall = wall_ms + 3.0
        self.paused_until = -1.0          # IMU restart: no samples until then
        self.skip_at = []                 # walls of single missed samples (a missed data-ready)
        self.restarts = set()             # first index after each IMU restart
        self.sampling = True
        self.sleeping = False             # in the sleep prologue: CTRL is answered BUSY
        self.sleep_reason = 0
        self.sleep_req = None             # a SLEEP op's reason: the prologue runs after its RESPONSE
        self.hold = False
        self.ble = Link(self, "ble")
        self.usb = Link(self, "usb")
        self.cmd_log, self.drain_heads = self.ble.cmd_log, self.ble.drain_heads    # the scenarios' BLE truth

    @property
    def lost(self):
        return self.ble.lost

    # ---- clock and samples ----
    def tick_at(self, wall):
        return self.tick_start + (wall - self.boot_wall) * TPS * (1 + self.drift)

    def now_ticks(self):
        return int(self.tick_at(self.b.now))

    def produce(self):
        while self.sampling and self.next_sample_wall <= self.b.now:
            w = self.next_sample_wall
            self.next_sample_wall += 1000.0 / self.rate
            if w < self.paused_until:
                continue
            if self.skip_at and w >= self.skip_at[0]:
                self.skip_at.pop(0)       # a missed data-ready: no sample, no index
                continue
            if self.paused_until > 0 and self.ticks and self.tick_at(w) - self.ticks[-1] > 0.9 * TPS * 1000:
                self.restarts.add(self.head)
            jitter = self.b.rng.uniform(-0.05, 0.05)        # ms, the polling firmware's spread
            self.ticks.append(int(self.tick_at(w + jitter)))
            self.head += 1

    def raw(self, i):
        return raw_for(i)

    # ---- frames ----
    def info_frame(self, proto=1, serial=None):
        return pl.build_info(self.boot_id, self.serial if serial is None else serial, self.ring_n,
                             wake=self.wake, accel_lsb=self.accel_lsb, gyro_lsb=self.gyro_lsb, proto=proto)

    # ---- the pod's loop, every ms ----
    def step(self):
        self.produce()
        for link in (self.ble, self.usb):
            if link.connected and self.b.pod is self:
                link.step()
        if self.sleep_req is not None and self.b.pod is self:
            reason, self.sleep_req = self.sleep_req, None
            self.b.pod_sleep(reason)

    def ctrl(self, link, b):
        """A WRITE to CTRL on this link (docs/ble.md 2.3 and 4). Returns the RESPONSE's status."""
        if not 1 <= len(b) <= 10:
            return crp.INVALID_VALUE          # podcrp.c checks the length before the pod's handler
        if self.sleeping or self.sleep_req is not None:
            return crp.BUSY                   # from the SLEEP op on (firmware: sleepCmd, then inPrologue)
        op = b[0]
        if op == pl.OP_STREAM_FROM and len(b) == 10:
            _, seq, boot, frm = struct.unpack("<BBII", b)
            if boot != self.boot_id:
                link.queue_status(pl.S_BOOT_MISMATCH)
                return crp.OK                 # taken: its real answer is the boot_mismatch STATUS
            lo = self.head - self.ring_n if self.head > self.ring_n else 0
            cur = min(max(frm, lo), self.head)
            link.overflow = False
            if frm < lo:
                link.lost += max(0, lo - max(frm, link.lost_mark))
                link.lost_mark = max(link.lost_mark, lo)
                link.overflow = True
            link.start_stream(op, seq, cur)
        elif op == pl.OP_STREAM_LIVE and len(b) == 4:
            _, seq, back = struct.unpack("<BBH", b)
            cur = self.head - min(back, self.head, self.ring_n)
            link.overflow = False
            link.start_stream(op, seq, cur)
        elif op == pl.OP_STOP and len(b) == 1:
            if link.kind == "ble":
                self.hold = False
            if not link.streaming:
                link.queue_status(pl.S_STREAM_STOPPED)
                link.drain_heads.append(("stop-idle", self.head))
                return crp.OK
            backlog = self.head - link.cursor
            nm = max(link.n_max(), 1)
            link.drain = {"head": self.head, "kind": "stop",
                          "deadline": self.b.now + 1500 + backlog / (60 * nm) * 1000}
        elif op == pl.OP_SLEEP and len(b) == 2 and b[1] <= 1:   # modes 0 and 1 only (docs/ble.md 4)
            self.sleep_req = 4                # loop() runs the prologue after this WRITE's RESPONSE
        else:
            return crp.INVALID_VALUE          # unknown op or wrong length: nothing changes
        return crp.OK


class Client:
    """A CRP client and its decoder: the phone on BLE (the Bench itself) or a PC on USB (UsbHost).
    Requests and answers as CRPClient does them: txn ids from crp.TxnAllocator, a 5 s timeout, no
    retries unless txn_retries says so (a retry resends the same packet, as CRPClient's does)."""
    heartbeat = None                      # BLE clients send no heartbeat (docs/ble.md 2.2)

    def init_client(self, bench):
        self.bench = bench
        self.stream = pl.PodStream(self.on_sample, self.on_event)
        self.samples, self.events = [], []
        self.emits = []                   # (epoch, idx) per emitted sample, in order
        self.ep_pod = {}                  # epoch -> the pod (boot) it decodes
        self.ep_gaps = []                 # (epoch, gap) in order
        self.wb_obs = {}                  # epoch -> [arrival - t64 / 32.768] per STATUS it used
        self.log = []                     # what the decoder saw and the client did, in order
        self.txns = crp.TxnAllocator()
        self.txn_retries = 0
        self.pending = {}                 # txn -> {"pkt", "deadline", "retries"}
        self.answers = {}                 # txn -> (status, value), or None: timed out or aborted
        self.status_reads = []            # (head, decoder nextIdx before it) per STATUS read answered
        self.busy = 0                     # BUSY answers to reads (each retried)
        self.mtu_read = None              # the MTU register, read after the BUSY retries gave up
        self.peer = None                  # the pod's HELLO, parsed
        self.refused = False              # the pod answered our HELLO with BYE
        self.session = False
        self.last_beat = 0.0
        self.mute = False                 # test-only: the client sends nothing at all (a stalled process)
        self.in_connect = False           # running the connection order: it writes the command itself
        self.stop_deadline = None
        self.held_seen = False
        self.status_blind_until = None    # test-only: a receiver that misses STATUS frames until then
        self.clock_step = 0.0             # added to every arrival time (a phone clock step)
        self.need_command = False         # the decoder asked for a command (a reboot seen in a STATUS)

    def on_sample(self, smp):
        self.samples.append(smp)
        self.emits.append((self.stream.ep, self.stream.ep.last_idx))

    def on_event(self, kind, info):
        if kind == "epoch":
            self.ep_pod[self.stream.ep] = self.bench.pod
        if kind == "gap":
            self.ep_gaps.append((self.stream.ep, info))
        if kind == "need-command":
            self.need_command = True      # answered by tick(), or by the connection order's command
            return
        self.events.append((kind, info))

    def wall(self):
        return round(self.bench.now + self.clock_step, 3)

    # ---- receiving ----
    def on_packet(self, pkt, arrival):
        """Every packet the pod sends, in arrival order: the capture row, the decoder (through the
        adapter), then the client's own session and transactions."""
        pkt = bytes(pkt)
        arr = round(arrival + self.clock_step, 3)
        self.log.append(("pkt", arr, pkt.hex()))
        p = crp.parse(pkt)
        f = pl.crp_frame(pkt)
        ep = self.stream.ep
        if f is not None and p["type"] == crp.RESPONSE and len(f) == 20 and f[0] == pl.T_STATUS and ep is not None:
            self.status_reads.append((pl.decode_frame(f)["head"], ep.next_idx))
        self.stream.packet(pkt, arr)
        ep = self.stream.ep
        if ep is not None and ep.held:
            self.held_seen = True
        if f is not None and f[:1] == bytes([pl.T_STATUS]) and ep is not None and ep.status is not None \
                and ep.ref32 is not None and not self.stream.ignoring and len(ep.win) \
                and ep.win[-1] == arr - ep.status_t64 / 32.768:
            self.wb_obs.setdefault(ep, []).append(arr - ep.status_t64 / 32.768)
        if p is None:
            return
        t = p["type"]
        if t == crp.RESPONSE:
            if p["txn"] in self.pending:
                del self.pending[p["txn"]]
                self.txns.free(p["txn"])
                self.answers[p["txn"]] = (p["status"], p["value"])
        elif t == crp.HELLO:
            self.peer = p
            self.session = True
            self.last_beat = self.bench.now       # CRPClient starts its heartbeat here
        elif t == crp.BYE or (t == crp.END and p["ch"] == crp.SESSION_CHANNEL):
            self.refused = self.peer is None
            self.end_session(reset=True)

    def end_session(self, reset):
        for txn in self.pending:
            self.txns.free(txn)
            self.answers[txn] = None
        self.pending.clear()
        if reset:
            self.txns.reset()
        self.session = False

    def link_down(self):
        self.end_session(reset=False)             # CRPClient frees the txns, keeps the pointer

    # ---- sending ----
    def tx(self, pkt):
        self.log.append(("tx", self.wall(), pkt.hex()))
        self.send_raw(pkt)

    def request(self, pkt_of):
        """Send pkt_of(txn) with the next txn id; the answer lands in answers[txn]."""
        txn = self.txns.alloc()
        pkt = pkt_of(txn)
        self.pending[txn] = {"pkt": pkt, "deadline": self.bench.now + TXN_TIMEOUT_MS, "retries": 0}
        self.tx(pkt)
        return txn

    def await_txn(self, txn):
        """Run until txn is answered: (status, value), or None (timed out, or the session ended)."""
        while txn in self.pending and self.link_up():
            self.bench.run(1)
        self.pending.pop(txn, None)
        return self.answers.pop(txn, None)

    def ctrl(self, b):
        return self.request(lambda txn: crp.write(txn, pl.REG_CTRL, b))

    def hello(self):
        """HELLO (CRPClient.sendHello's defaults), then wait for the pod's: its HELLO parsed, or None."""
        self.peer, self.refused = None, False
        self.tx(crp.hello())
        deadline = self.bench.now + TXN_TIMEOUT_MS
        while self.peer is None and not self.refused and self.bench.now < deadline and self.link_up():
            self.bench.run(1)
        return self.peer

    def read_frame(self, reg):
        """A READ with the BUSY retry (docs/ble.md 5): every 200 ms for 3 s, then the MTU register
        (10 B, fits at MTU 23) to say why. (status, value); (BUSY, b"") after giving up; None if
        it went unanswered."""
        t0 = self.bench.now
        while True:
            r = self.await_txn(self.request(lambda txn: crp.read(txn, reg)))
            if r is None or r[0] != crp.BUSY:
                return r
            self.busy += 1
            if self.bench.now - t0 + BUSY_RETRY_MS > BUSY_FOR_MS:
                break
            self.bench.run(BUSY_RETRY_MS)
        r = self.await_txn(self.request(lambda txn: crp.read(txn, pl.REG_LINK_MTU)))
        self.mtu_read = struct.unpack("<H", r[1])[0] if r and r[0] == crp.OK and len(r[1]) == 2 else None
        return crp.BUSY, b""

    def connection_order(self, sloppy=False, push_early=False, push_delay=0):
        """docs/ble.md 7, steps 2 to 5: HELLO (device_id BPOD), READ INFO, READ STATUS (each BUSY
        retried), WRITE PUSH = 1, then WRITE CTRL with command() if it gives one. False if the
        session gave up before the command.
        sloppy (test-only): a phone that skips the STATUS read and whose stack misses the STATUS
        frames of the first 1.5 s, so DATA arrives before the epoch's first STATUS and must be
        held (the decoder's defensive path: the pod itself always sends a STATUS first).
        push_early (test-only, a NEGATIVE control): PUSH = 1 right after HELLO, before the reads.
        push_delay: ms between the reads and PUSH = 1 (an app waiting on its user)."""
        self.in_connect = True
        try:
            peer = self.hello()
            if peer is None or peer["device_id"] != pl.DEVICE_ID:
                return False
            if push_early:
                r = self.await_txn(self.request(lambda txn: crp.write(txn, pl.REG_PUSH, b"\x01")))
                if r is None or r[0] != crp.OK:
                    return False
            r = self.read_frame(pl.REG_INFO)
            if r is None or r[0] != crp.OK:
                return False                      # MTU too small (mtu_read says what it is) or no answer
            if sloppy:
                self.status_blind_until = self.bench.now + 1530
            else:
                r = self.read_frame(pl.REG_STATUS)
                if r is None or r[0] != crp.OK:
                    return False
            if push_delay:
                self.bench.run(push_delay)
            if not push_early:
                r = self.await_txn(self.request(lambda txn: crp.write(txn, pl.REG_PUSH, b"\x01")))
                if r is None or r[0] != crp.OK:
                    return False
            # a reboot-recovery request from the STATUS read is answered by this command
            self.need_command = False
            self.command()
            return True
        finally:
            self.in_connect = False

    def reread(self):
        """After need-info (boot_mismatch): READ INFO and STATUS again, then the command."""
        self.in_connect = True
        try:
            self.read_frame(pl.REG_INFO)
            self.read_frame(pl.REG_STATUS)
            self.need_command = False
            self.command()
        finally:
            self.in_connect = False

    def bye(self):
        """CRPClient.close(): BYE (base 0, our device_id 0), every txn aborted, the ids from 1."""
        self.tx(crp.bye(0, 0))
        self.end_session(reset=True)

    # ---- the decoder's calls ----
    def command(self):
        b = self.stream.command()
        self.log.append(("cmd", None, "none" if b is None else b.hex()))
        if b is not None:
            self.ctrl(b)
        return b

    def stop(self):
        b = self.stream.stop_command()
        self.log.append(("stop", None, b.hex()))
        wait = self.stream.stop_wait_ms()
        self.log.append(("call", None, f"stop_wait_ms={wait!r}"))
        txn = self.ctrl(b)
        self.stop_deadline = self.bench.now + wait
        return txn

    def finish(self):
        self.log.append(("call", None, "finish"))
        self.stream.finish()

    # ---- every ms ----
    def tick(self):
        now = self.bench.now
        for txn in [t for t, e in self.pending.items() if now >= e["deadline"]]:
            e = self.pending[txn]
            if e["retries"] < self.txn_retries:
                e["retries"] += 1
                e["deadline"] = now + TXN_TIMEOUT_MS
                self.tx(e["pkt"])                 # the same packet, the same txn
            else:
                del self.pending[txn]
                self.txns.free(txn)
                self.answers[txn] = None
        if self.heartbeat and self.session and self.link_up() and now - self.last_beat >= self.heartbeat:
            self.last_beat = now
            if not self.mute:
                self.tx(crp.nop())
        if self.stop_deadline is not None:
            if self.stream.stop is None:
                self.stop_deadline = None         # stopped, or a STREAM command cancelled it
            elif now >= self.stop_deadline:
                r = self.stream.stop_timed_out()
                self.log.append(("call", None, f"stop_timed_out={int(r)}"))
                self.stop_deadline = None
        if not self.in_connect and self.need_command:
            self.need_command = False
            if self.link_up():
                self.command()

    # ---- crafted input (no pod, no radio) ----
    def feed_read(self, reg, frame, arrival=None):
        """A crafted OK RESPONSE carrying frame, straight into the decoder."""
        txn = self.txns.alloc()
        self.txns.free(txn)
        self.on_packet(crp.response(txn, crp.OK, reg, frame), self.bench.now if arrival is None else arrival)

    def feed_push(self, reg, frame, arrival=None):
        self.on_packet(crp.push(reg, frame), self.bench.now if arrival is None else arrival)


class Bench(Client):
    """One scenario: the pod, the radio, the phone (a Client) and, if attached, a PC on USB."""

    def __init__(self, name, seed=1, mtu=247, native_mtu=True, lat=(7.5, 30.0), mtu_after=None,
                 txn_retries=0, **pod_kw):
        self.name = name
        self.rng = random.Random(seed)
        self.now = 0.0
        self.mtu, self.native_mtu, self.lat = mtu, native_mtu, lat
        self.mtu_after = mtu_after        # ms after connect the pod's own exchange completes (None: 50 + a round trip)
        self.air = []                     # [deliver_ms, kind, packet] pod -> phone, FIFO
        self.up = []                      # (deliver_ms, packet) phone -> pod writes
        self.last_deliver = 0.0
        self.drop_next_data = False
        self.jam_until = None             # a jammed link: packets wait (BLE retransmits), in order
        self.rx_stall_until = None        # test-only: the phone's app gets no notifications until then
        self.stalled = []
        self.init_client(self)
        self.txn_retries = txn_retries
        self.pods = []                    # every boot, for the ground truth
        self.pod_kw = dict(pod_kw)
        self.serial = 0x5EED0001
        self.pod = self.new_pod(boot_id=0x1000 + seed, wake=0)
        self.connected = False
        self.usb = None
        self.note = None
        self.hello_push = []              # ground truth: the pod's PUSH as each of its HELLOs reaches the phone

    def new_pod(self, boot_id, wake, tick_start=None):
        kw = dict(self.pod_kw)
        if tick_start is not None:
            kw["tick_start"] = tick_start
        p = Pod(self, boot_id, self.serial, self.now, wake=wake, **kw)
        self.pods.append(p)
        return p

    def link_up(self):
        return self.connected

    def attach_usb(self):
        self.usb = UsbHost(self)
        return self.usb

    # ---- the radio ----
    def latency(self):
        return self.lat[0] + self.rng.uniform(0, self.lat[1])

    def mtu_delay(self):
        """The pod asks at connect + 50 ms; the phone answers a round trip later."""
        return self.mtu_after if self.mtu_after is not None else MTU_EXCHANGE_MS + self.latency() + self.latency()

    def pod_sent(self, pkt, lost=False):
        d = max(self.last_deliver, self.now + self.latency())
        self.last_deliver = d
        self.air.append([d, "lost" if lost else "pkt", pkt, False])     # False: no TX-complete yet

    def tx_complete(self, link, upto):
        """TX-complete for the notifications on the air due by upto that haven't had one. On the pod
        it comes from the BLE task, beside loop(), so a slot can free between the RX processing and
        sendPass: the passes call this there for the first half of their ms (the rest completes in
        deliver()). That is the moment a push could overtake a RESPONSE in the spool."""
        if self.jam_until is not None or not link.connected:
            return                        # a jammed link completes nothing; its end is deliver()'s
        for a in self.air:
            if a[0] > upto:
                break
            if not a[3]:
                a[3] = True
                link.inflight = max(0, link.inflight - 1)

    def send_raw(self, pkt):
        self.up.append((self.now + self.latency(), pkt))

    def deliver(self):
        if self.jam_until is not None and self.now < self.jam_until:
            return                        # nothing gets through, nothing completes
        if self.jam_until is not None:
            self.jam_until = None
            for a in self.air:            # the backlog goes out from now on, in order
                a[0] = max(a[0], self.now)
        if self.stalled and (self.rx_stall_until is None or self.now >= self.rx_stall_until):
            held, self.stalled = self.stalled, []
            for pkt in held:              # the app sees them all now, in order
                self.receive(pkt, self.now)
        while self.air and self.air[0][0] <= self.now:
            d, kind, pkt, done = self.air.pop(0)
            if not done and self.pod and self.pod.ble.connected:
                self.pod.ble.inflight = max(0, self.pod.ble.inflight - 1)   # TX-complete
            if kind == "pkt" and self.connected:
                if self.rx_stall_until is not None and self.now < self.rx_stall_until:
                    self.stalled.append(pkt)
                    continue
                self.receive(pkt, d)
        while self.up and self.up[0][0] <= self.now:
            _, pkt = self.up.pop(0)
            if self.pod and self.pod.ble.connected:
                self.pod.ble.receive(pkt)

    def receive(self, pkt, arrival):
        f = pl.crp_frame(pkt)
        if f and f[0] == pl.T_STATUS and self.status_blind_until is not None and self.now < self.status_blind_until:
            return                        # test-only fault (connect sloppy=True)
        if pkt[:1] == bytes([crp.HELLO]) and self.pod:
            self.hello_push.append(self.pod.ble.push)   # the phone hasn't written PUSH since the pod sent it
        self.on_packet(pkt, arrival)

    # ---- the phone ----
    def connect(self, info=None, sloppy=False, push_early=False, push_delay=0):
        """Connect and run the connection order (docs/ble.md 7). info (test-only): the INFO read
        answers this cached value once."""
        self.connected = True
        self.log.append(("link", self.wall(), "connect"))
        self.pod.ble.on_connect()
        self.run(15)                      # link up: TX notifications on
        if info is not None:
            self.pod.ble.info_override = info
        return self.connection_order(sloppy=sloppy, push_early=push_early, push_delay=push_delay)

    def disconnect(self):
        """Link loss: packets on the air are gone, the pod ends the session and the stream (keeps the hold)."""
        self.connected = False
        self.log.append(("link", self.wall(), "disconnect"))
        self.air = []
        self.up = []
        self.stalled = []
        self.jam_until = None
        self.link_down()
        if self.pod:
            self.pod.ble.on_disconnect()

    # ---- the pod sleeping and waking ----
    def pod_sleep(self, reason):
        """The sleep prologue (docs/ble.md 6): sampling stops, drain <= 1.5 s, STATUS, disconnect."""
        p = self.pod
        L = p.ble
        p.sampling = False
        p.sleeping = True
        p.sleep_reason = reason
        if L.streaming and L.connected:
            dl = self.now + 1500
            if L.drain and L.drain["kind"] == "stop":
                dl = min(dl, L.drain["deadline"])     # never past the STOP's cap: the phone's stop wait is sized on it
            L.retry = None                # a frame built for a STOP drain may carry FINAL there: rebuild it
            L.drain = {"head": p.head, "kind": "sleep", "deadline": dl}
        else:
            self.pod_go_to_sleep_2()

    def pod_go_to_sleep_2(self):
        p = self.pod
        if p is None:
            return
        self.pod = None                   # asleep from here on
        L = p.ble
        if L.connected:
            # STATUS going_to_sleep waits for the link, then up to 300 ms for nothing in flight or queued
            L.status_due.append(L.status_frame(pl.S_GOING_TO_SLEEP, head=p.head))
            end = self.now + 300
            while self.now < end and (L.status_due or L.inflight or L.spool or self.air):
                L.spool_drain()
                L.process_all()
                self.tx_complete(L, self.now + 0.5)     # this pass runs before deliver(now + 1)
                L.send_pass()
                L.spool_drain()
                self.now += 1.0
                self.pod = p
                self.deliver()
                self.pod = None
            self.connected = False
            self.log.append(("link", self.wall(), "disconnect by the pod before sleep"))   # no commas: a CSV field
            self.air = []
            self.up = []
            self.link_down()
            L.on_disconnect()

    def wake(self, boot_id):
        """A wake is a reboot: new boot_id (or the same one, to test the defensive check), ticks from 0."""
        self.pod = self.new_pod(boot_id=boot_id, wake=1, tick_start=0)

    # ---- time ----
    def run(self, ms):
        end = self.now + ms
        while self.now < end:
            self.now += 1.0
            if self.pod:
                self.pod.step()
            self.deliver()
            if self.usb:
                self.usb.wire()
            self.tick()
            if self.usb:
                self.usb.tick()


class UsbHost(Client):
    """A PC on the pod's USB (docs/ble.md 2.5), as log_ble.py --usb is: the port opens in
    text mode, a level 1 HELLO switches it to CRP, packets come out of a crp.Deframer in arrival
    order. Its own decoder and its own cursor on the pod, beside the phone's on BLE."""
    heartbeat = HEARTBEAT_MS              # USB clients send a NOP every 1 s

    def __init__(self, bench):
        self.init_client(bench)
        self.open = False
        self.deframer = crp.Deframer()
        self.wire_in, self.wire_out = [], []      # (deliver_ms, bytes) pod -> PC, PC -> pod
        self.text_rx = self.junk_rx = 0           # ground truth: bytes received that no packet owns
        self.unowned = 0                  # what the deframers left out of packets, over every port open
        self.junk_out = None              # test-only: bytes sent ahead of the next packet
        self.split_next = None            # test-only: the next packet goes in two writes, split here
        self.read_stall_until = None      # test-only: the PC's reader takes nothing until then (its writes go on)

    def link_up(self):
        return self.open

    def open_port(self):
        self.open = True
        self.deframer = crp.Deframer()
        self.log.append(("link", self.wall(), "port open"))
        if self.bench.pod:
            self.bench.pod.usb.on_open()

    def close_port(self):
        # count what this deframer dropped before the next open replaces it. A close in text mode
        # also leaves the last text byte pending: one byte is never a length, so it waits for good
        pod = self.bench.pod
        self.unowned += self.deframer.dropped + (self.deframer.pending if pod and not pod.usb.crp else 0)
        self.open = False
        self.log.append(("link", self.wall(), "port close"))
        self.link_down()
        self.wire_in, self.wire_out = [], []
        if self.bench.pod:
            self.bench.pod.usb.on_close()

    def connect(self):
        return self.connection_order()

    def send_raw(self, pkt):
        now = self.bench.now
        f = crp.frame_l1(pkt)
        if self.junk_out:
            self.wire_out.append((now + 1, self.junk_out))
            self.junk_out = None
        if self.split_next:
            k, self.split_next = self.split_next, None
            self.wire_out += [(now + 1, f[:k]), (now + 6, f[k:])]
        else:
            self.wire_out.append((now + 1, f))

    def wire(self):
        """The cable, every ms: up to USB_RATE bytes off the pod's spool (1 ms to the PC), the PC's
        writes to the pod, and the PC's deframer on what arrived."""
        pod, now = self.bench.pod, self.bench.now
        if not self.open:
            return
        if pod and pod.usb.connected:
            stalled = self.read_stall_until is not None and now < self.read_stall_until
            take, out, sp = 0 if stalled else USB_RATE, bytearray(), pod.usb.spool
            while take and sp:
                kind, data = sp[0]
                n = min(take, len(data))
                out += data[:n]
                del data[:n]
                take -= n
                pod.usb.spool_bytes -= n
                if kind == "text":
                    self.text_rx += n
                elif kind == "junk":
                    self.junk_rx += n
                if not data:
                    sp.pop(0)
            if out:
                self.wire_in.append((now + 1, bytes(out)))
            while self.wire_out and self.wire_out[0][0] <= now:
                pod.usb.receive_bytes(self.wire_out.pop(0)[1])
        while self.wire_in and self.wire_in[0][0] <= now:
            d, chunk = self.wire_in.pop(0)
            for pkt in self.deframer.feed(chunk):
                self.on_packet(pkt, d)


# ---------------------------------------------------------------------------------------------
# checks

def check(b, expect):
    """Compare a finished client (the Bench's phone or a UsbHost) with what the scenario expects.
    Returns failure strings."""
    st, out, fails = b.stream, b.samples, []
    t_of = {}                             # (epoch, idx) -> t

    # 1. t strictly increasing over the whole session
    for k in range(1, len(out)):
        if out[k]["t"] <= out[k - 1]["t"]:
            fails.append(f"t not increasing at sample {k}")
            break

    # 2. values against the pod's own scale (float32 in INFO); t spacing against its ticks
    prev = None
    for k, ((ep, idx), smp) in enumerate(zip(b.emits, out)):
        pod = b.ep_pod[ep]
        t_of[(ep, idx)] = smp["t"]
        gx, gy, gz, ax, ay, az = pod.raw(idx)
        a_l, g_l = f32(pod.accel_lsb), f32(pod.gyro_lsb)
        want = (ax * a_l, ay * a_l, az * a_l, gx * g_l, gy * g_l, gz * g_l)
        got = (smp["ax"], smp["ay"], smp["az"], smp["gx"], smp["gy"], smp["gz"])
        if want != got:
            fails.append(f"sample {k} (index {idx}): values differ from the pod's")
            break
        if prev and prev[0] is ep:
            dt_true = (pod.ticks[idx] - pod.ticks[prev[1]]) / 32768
            if abs((smp["t"] - prev[2]) - dt_true) > 1e-9:
                fails.append(f"sample {k}: dt {smp['t'] - prev[2]:.9f} s, ticks say {dt_true:.9f} s")
                break
        prev = (ep, idx, smp["t"])

    # 3. accounting: per epoch, emitted indices + index gaps tile [start, end) exactly once
    for n_ep, (ep, start, end) in enumerate(expect.get("ranges", [])):
        cover = [(i, i + 1, "sample") for e, i in b.emits if e is ep]
        cover += [(g["idx"], g["idx"] + g["n"], g["kind"]) for e, g in b.ep_gaps
                  if e is ep and g["kind"] in INDEX_KINDS and g["n"] > 0]
        cover.sort()
        at, bad = start, None
        for a, z, what in cover:
            if a != at:
                bad = f"at index {at}: next is {what} from {a}"
                break
            at = z
        if bad is None and at != end:
            bad = f"covered up to {at}, want {end}"
        if bad:
            fails.append(f"epoch {n_ep + 1} accounting [{start}, {end}): {bad}")
    if not expect.get("ranges") and not expect.get("no_ranges_ok"):
        fails.append("scenario lists no ranges (the accounting check would be vacuous)")

    # 4. gap t0 / t1 against the neighbouring samples
    for ep, g in b.ep_gaps:
        if g["kind"] in ("stop-tail", "sleep-tail", "lost-tail"):
            if ep.last_t is not None and g["t0"] is not None and (ep, g["idx"] - 1) in t_of \
                    and g["t0"] != t_of[(ep, g["idx"] - 1)]:
                fails.append(f"{g['kind']} t0 {g['t0']} != t of index {g['idx'] - 1}")
            continue
        right = g["idx"] + (g["n"] if g["kind"] in INDEX_KINDS else 0)
        if (ep, right) in t_of and g["t1"] != t_of[(ep, right)]:
            fails.append(f"{g['kind']} gap t1 {g['t1']} != t of index {right}")
        left = (ep, g["idx"] - 1)
        if left in t_of and g["t0"] != t_of[left]:
            fails.append(f"{g['kind']} gap t0 {g['t0']} != t of index {left[1]}")

    # 5. clock: wallBase = the min over the epoch's last 32 STATUS
    for ep, obs in b.wb_obs.items():
        if ep.wall_base is not None and obs and ep.wall_base != min(obs[-32:]):
            fails.append(f"wallBase {ep.wall_base} != min of its last 32 STATUS {min(obs[-32:])}")

    # 6. gaps: kind -> total n (int), or a predicate on the list of n; nothing else allowed
    want_gaps = expect.get("gaps", {})
    for kind in {g["kind"] for g in st.gaps} | set(want_gaps):
        got = [g["n"] for g in st.gaps if g["kind"] == kind]
        w = want_gaps.get(kind, None)
        if w is None:
            if got:
                fails.append(f"unexpected {kind} gap(s): {got}")
        elif callable(w):
            if not w(got):
                fails.append(f"{kind} gaps {got} not as expected")
        elif sum(got) != w:
            fails.append(f"{kind} gaps total {sum(got)}, want {w} ({got})")
    for g in st.gaps:
        if g.get("lower_bound") and "true_loss" in expect and g["n"] > expect["true_loss"]:
            fails.append(f"lower-bound {g['kind']} n={g['n']} > true loss {expect['true_loss']}")

    # 7. counters: anomalies must be 0 unless listed; faults exactly as listed
    want_s = expect.get("stats", {})
    for k in set(ANOMALIES) | set(want_s):
        v, w = st.stats[k], want_s.get(k, 0)
        if not (w(v) if callable(w) else v == w):
            fails.append(f"stats.{k} = {v}" + ("" if callable(w) else f", want {w}"))
    want_f = sorted(expect.get("faults", []))
    if sorted(st.faults) != want_f:
        fails.append(f"faults {sorted(st.faults)}, want {want_f}")

    for name, fn in expect.get("extra", {}).items():
        msg = fn()
        if msg:
            fails.append(f"{name}: {msg}")
    return fails


def frames_of(log):
    """The same run as frame rows, the decoder's input before D45: each packet's frame through the
    adapter (packets without one left out), tx and link rows dropped, the rest as they are."""
    out = []
    for kind, arr, payload in log:
        if kind == "pkt":
            f = pl.crp_frame(bytes.fromhex(payload))
            if f is not None:
                out.append(("frame", arr, f.hex()))
        elif kind in ("cmd", "stop", "call"):
            out.append((kind, arr, payload))
    return out


def received(c, since=float("-inf"), until=float("inf")):
    """(arrival, packet) of every packet client c received in (since, until]."""
    return [(a, bytes.fromhex(h)) for k, a, h in c.log if k == "pkt" and since < a <= until]


def frames_rx(c, typ):
    """Every frame of one type the decoder was fed (through the adapter)."""
    return [f for f in (pl.crp_frame(p) for _, p in received(c)) if f is not None and f[:1] == bytes([typ])]


# ---------------------------------------------------------------------------------------------
# scenarios (docs/ble-plan.md step 2 and section 0, then D45's)

def first_cmd(pod):
    return pod.cmd_log[0][1]


def last_drain(pod):
    return pod.drain_heads[-1][1]


def end_known(b, ep):
    """The end of an epoch the client can know after an abrupt end: the larger of the last head
    it heard and what it already received (docs/ble.md 7, a lower bound)."""
    got = [i for e, i in b.emits if e is ep]
    return max(ep.status["head"] if ep.status else 0, (max(got) + 1) if got else 0)


def rebase_error(b, pod1, pod2):
    """Decoder t of pod2's first emitted sample minus the true time since the session's first
    sample (from the simulator's walls). Bounded by the STATUS latency spread."""
    eps = list(b.ep_pod.items())
    e1 = next(e for e, p in eps if p is pod1)
    e2 = next(e for e, p in eps if p is pod2)
    i1 = next(i for e, i in b.emits if e is e1)
    k2, i2 = next((k, i) for k, (e, i) in enumerate(b.emits) if e is e2)
    wall = lambda p, i: p.boot_wall + (p.ticks[i] - p.tick_start) / (TPS * (1 + p.drift))
    t_true = (wall(pod2, i2) - wall(pod1, i1)) / 1000
    return b.samples[k2]["t"] - t_true


def run_until(b, cond, what, step=1, limit=60000):
    """Run step ms at a time until cond() holds. Past limit ms it raises, so a regression fails
    loudly instead of hanging the whole run."""
    end = b.now + limit
    while not cond():
        if b.now >= end:
            raise AssertionError(f"{b.name}: never saw {what} in {limit} ms")
        b.run(step)


def one_gap(got):
    """Gap expectation: exactly one gap of this kind, of at least one sample."""
    return len(got) == 1 and got[0] > 0


ANY = lambda v: True                      # a counter the scenario doesn't pin (it depends on the latencies)


def one_info(b):
    n = len(frames_rx(b, pl.T_INFO))
    return None if n == 1 else f"the decoder got {n} INFO frames, want 1"


def sc_clean():
    b = Bench("clean", seed=1)
    b.pods[0].ble.pkt_seq = 0xFFF0        # also crosses the 16-bit pkt_seq wrap
    b.run(3000); b.connect(); b.run(20000); b.stop(); b.run(3000); b.finish()
    p = b.pods[0]; ep = b.stream.epochs[0]
    crossed = lambda: None if p.ble.pkt_seq < 0xFFF0 else "pkt_seq never wrapped"
    return b, {"ranges": [(ep, first_cmd(p), last_drain(p))], "stats": {"stopped": 1},
               "extra": {"pkt_seq wrap": crossed}}


def sc_scale():
    b = Bench("scale-from-info", seed=18, gyro_lsb=4.375 * 4 / 1000)   # a +-500 dps pod
    b.run(3000); b.connect(); b.run(5000); b.stop(); b.run(3000); b.finish()
    p = b.pods[0]; ep = b.stream.epochs[0]
    return b, {"ranges": [(ep, first_cmd(p), last_drain(p))], "stats": {"stopped": 1}}


def sc_reconnect_rewind():
    b = Bench("reconnect-dupes", seed=2)
    b.run(3000); b.connect(); b.run(8000)
    b.disconnect(); b.run(4000)
    b.pods[0].ble.rewind = 50             # the pod resends 50 samples it already sent
    b.connect(); b.run(8000); b.stop(); b.run(3000); b.finish()
    p = b.pods[0]; ep = b.stream.epochs[0]
    return b, {"ranges": [(ep, first_cmd(p), last_drain(p))],
               "stats": {"dupes": 50, "reconnects": 1, "stopped": 1}}


def sc_overflow():
    b = Bench("overflow-reconnect", seed=3)
    b.run(3000); b.connect(); b.run(5000)
    b.disconnect(); b.run(60000)          # longer than the 38.8 s ring
    b.connect(); b.run(12000); b.stop(); b.run(3000); b.finish()
    p = b.pods[0]; ep = b.stream.epochs[0]
    return b, {"ranges": [(ep, first_cmd(p), last_drain(p))],
               "gaps": {"overflow": p.lost}, "stats": {"reconnects": 1, "stopped": 1}}


def sc_lap():
    b = Bench("overflow-mid-stream", seed=19)
    b.run(3000); b.connect(); b.run(5000)
    b.jam_until = b.now + 45000           # connected but jammed past the ring: the cursor is lapped
    b.run(46000); b.run(8000); b.stop(); b.run(3000); b.finish()
    p = b.pods[0]; ep = b.stream.epochs[0]
    lapped = lambda: None if any(g["kind"] == "overflow" for g in b.stream.gaps) else "no overflow gap"
    return b, {"ranges": [(ep, first_cmd(p), last_drain(p))],
               "gaps": {"overflow": p.lost}, "stats": {"stopped": 1}, "extra": {"lap": lapped}}


def sc_pkt_hole():
    b = Bench("pkt-seq-hole", seed=4)
    b.run(3000); b.connect(); b.run(5000)
    b.drop_next_data = True               # one DATA push lost on the air (test-only fault)
    b.run(5000); b.stop(); b.run(3000); b.finish()
    p = b.pods[0]; ep = b.stream.epochs[0]
    return b, {"ranges": [(ep, first_cmd(p), last_drain(p))],
               "gaps": {"unexplained": 16}, "stats": {"pkt_seq_gaps": 1, "pkt_seq_lost": 1, "stopped": 1},
               "faults": ["unexplained gap"]}


def sc_tick_wrap():
    b = Bench("tick-wrap", seed=5, tick_start=2**32 - 5 * 32768)
    b.run(1000); b.connect(); b.run(15000); b.stop(); b.run(3000); b.finish()
    p = b.pods[0]; ep = b.stream.epochs[0]
    wrapped = lambda: None if p.ticks[-1] > 2**32 else "the ticks never crossed 2^32"
    return b, {"ranges": [(ep, first_cmd(p), last_drain(p))], "stats": {"stopped": 1},
               "extra": {"wrap": wrapped}}


def sc_sleep_wake():
    b = Bench("sleep-wake-rebase", seed=6)
    b.run(3000); b.connect(); b.run(10000)
    b.pod_sleep(1)                        # idle sleep: drain, STATUS, disconnect
    b.run(7000)
    b.wake(boot_id=0x2006); b.run(3000)
    b.connect(); b.run(8000); b.stop(); b.run(3000); b.finish()
    p1, p2 = b.pods
    e1, e2 = b.stream.epochs
    spread = b.lat[1] + 1.0
    b.note = f"rebase error {rebase_error(b, p1, p2) * 1000:+.2f} ms (bound {spread:.0f} ms)"
    err = lambda: (None if abs(rebase_error(b, p1, p2)) * 1000 <= spread
                   else f"rebase error {rebase_error(b, p1, p2) * 1000:.2f} ms > {spread} ms")
    return b, {"ranges": [(e1, first_cmd(p1), p1.drain_heads[-1][1]), (e2, 0, last_drain(p2))],
               "stats": {"epochs": 2, "stopped": 2}, "extra": {"rebase": err,
               "sleep event": lambda: None if ("sleep", "idle") in b.events else "no sleep(idle) event"}}


def sc_sleep_cut():
    b = Bench("sleep-drain-cut", seed=7, mtu=79)          # 4 samples per frame: a slow drain
    b.run(3000); b.connect(); b.run(5000)
    b.disconnect(); b.run(30000)          # 30 s of backlog, still inside the ring
    b.connect(); b.run(200)
    b.pod_sleep(4)                        # sleep right away: the 1.5 s drain can't finish
    b.run(3000); b.finish()
    p = b.pods[0]; ep = b.stream.epochs[0]
    return b, {"ranges": [(ep, first_cmd(p), p.drain_heads[-1][1])], "gaps": {"sleep-tail": one_gap},
               "stats": {"reconnects": 1, "stopped": 1}}


def finals(b):
    """(first_idx, n) of every DATA frame with FINAL the client received."""
    out = []
    for f in frames_rx(b, pl.T_DATA):
        d = pl.decode_frame(f)
        if d and d["flags"] & pl.F_FINAL:
            out.append((d["first_idx"], d["n"]))
    return out


def sc_stop_then_sleep():
    # a STOP drain on a link slower than the cap assumes (40-80 ms: ~50 frames/s, not 60), and a
    # sleep (lid-down: STOP released the hold) 100 ms before its cap. The sleep keeps the STOP's
    # cap, so going_to_sleep lands inside the phone's stop wait (~1.2 s past the cap at 16 per
    # frame); a fresh 1.5 s drain would land after it: a false 'stop timed out'
    b = Bench("stop-then-sleep-at-cap", seed=41, lat=(40.0, 40.0))
    b.run(3000); b.connect(); b.run(4000)
    b.disconnect(); b.run(30000)
    b.connect(); b.run(300)
    b.stop()
    p = b.pods[0]
    run_until(b, lambda: p.ble.drain and p.ble.drain["kind"] == "stop", "the pod's STOP drain")
    b.run(round(p.ble.drain["deadline"] - b.now - 100))
    b.pod_sleep(2)
    b.run(4000); b.finish()
    ep = b.stream.epochs[0]
    return b, {"ranges": [(ep, first_cmd(p), p.drain_heads[-1][1])], "gaps": {"sleep-tail": one_gap},
               "stats": {"reconnects": 1, "stopped": 1},
               "extra": {"no FINAL on a capped drain": lambda: None if not finals(b) else f"FINAL frames {finals(b)}"}}


def sc_stop_final_then_sleep():
    # the STOP drain's last frame (FINAL) is built but waits for a free slot when the sleep comes:
    # it must be rebuilt, so the only FINAL frame ends at the final head. Whether that frame ever
    # waits is down to the latencies: seed 58 is one where it does (42 stopped doing it once
    # TX-complete could land mid-pass), and the loop below fails loudly if it ever stops
    b = Bench("stop-final-then-sleep", seed=58)
    b.run(3000); b.connect(); b.run(4000)
    b.disconnect(); b.run(6000)           # a backlog keeps all 3 slots busy to the drain's last frame
    b.connect(); b.run(300)
    b.stop()
    p = b.pods[0]
    run_until(b, lambda: p.ble.retry is not None and p.ble.retry["bytes"][12] & pl.F_FINAL,
              "a built FINAL frame waiting for a slot", limit=5000)
    b.pod_sleep(4)
    b.run(4000); b.finish()
    ep = b.stream.epochs[0]
    end = p.drain_heads[-1][1]
    ok = lambda: (None if [fi + n for fi, n in finals(b)] == [end]
                  else f"FINAL frames {finals(b)}, the sleep drain ended at {end}")
    return b, {"ranges": [(ep, first_cmd(p), end)], "stats": {"stopped": 1},
               "extra": {"one FINAL, at the final head": ok,
                         "sleep event": lambda: None if ("sleep", "command") in b.events else "no sleep(command) event"}}


def sc_reconnect_stop_before_data():
    # a same-boot reconnect on a link that only reaches MTU 60 (n_max 2: no DATA, but INFO and
    # STATUS fit, so the connection order completes). End right away: the pod caps the drain
    # (n_max < 4) and its stream_stopped comes before the new stream's first DATA. It answers this
    # STOP, so it closes the stream (no stale stop, no 'stop timed out')
    b = Bench("reconnect-stop-before-data", seed=43, native_mtu=False)
    b.run(3000); b.connect(); b.run(5000)
    b.disconnect(); b.run(2000)
    b.mtu = 60
    b.connect(); b.run(100)
    b.stop(); b.run(4000); b.finish()
    p = b.pods[0]; ep = b.stream.epochs[0]
    return b, {"ranges": [(ep, first_cmd(p), last_drain(p))],
               "gaps": {"stop-tail": one_gap},
               "stats": {"reconnects": 1, "stopped": 1, "stale_stops": 0}}


def sc_wake_drop_before_data():
    # the pod sleeps and wakes; the phone reconnects (INFO, STATUS, STREAM_FROM(new boot, 0)) but
    # the link drops before any DATA, and the session ends: the pickup it was owed is a counted
    # lower-bound tail from index 0, not silently missing
    b = Bench("wake-drop-before-data", seed=44)
    b.run(3000); b.connect(); b.run(5000)
    b.pod_sleep(1); b.run(3000)
    b.wake(boot_id=0x2044); b.run(4000)
    b.connect(); b.disconnect()
    b.run(1000); b.finish()
    p1 = b.pods[0]; e1, e2 = b.stream.epochs
    owed = e2.status["head"]
    return b, {"ranges": [(e1, first_cmd(p1), p1.drain_heads[-1][1]), (e2, 0, owed)],
               "gaps": {"lost-tail": lambda got: got == [owed]}, "stats": {"epochs": 2}}


def sc_wake_drop_then_reset():
    # the same, but the pod restarts again before the phone gets back: the new epoch closes the
    # owed one with its lower-bound tail, and the third boot streams from 0 as usual
    b = Bench("wake-drop-then-reset", seed=45)
    b.run(3000); b.connect(); b.run(5000)
    b.pod_sleep(1); b.run(3000)
    b.wake(boot_id=0x2045); b.run(4000)
    b.connect(); b.disconnect(); b.run(2000)
    b.wake(boot_id=0x3045); b.run(2000)
    b.connect(); b.run(6000); b.stop(); b.run(3000); b.finish()
    p1, p2, p3 = b.pods
    e1, e2, e3 = b.stream.epochs
    owed = e2.status["head"]
    return b, {"ranges": [(e1, first_cmd(p1), p1.drain_heads[-1][1]), (e2, 0, owed), (e3, 0, last_drain(p3))],
               "gaps": {"lost-tail": lambda got: got == [owed]}, "stats": {"epochs": 3, "stopped": 2}}


def sc_imu_restart():
    b = Bench("imu-restart-and-miss", seed=8, mtu=79)
    b.run(3000); b.connect(); b.run(3000)
    b.pods[0].skip_at = [b.now + 1000.3]  # one missed data-ready: an 'imu' hole of 1
    b.run(2000)
    b.pods[0].paused_until = b.now + 1100  # 50 x 20 ms timeouts + imuStart: ~1.1 s, no samples
    b.run(8000); b.stop(); b.run(4000); b.finish()
    p = b.pods[0]; ep = b.stream.epochs[0]
    one = lambda got: len(got) == 1 and 200 <= got[0] <= 250
    return b, {"ranges": [(ep, first_cmd(p), last_drain(p))],
               "gaps": {"imu-restart": one, "imu": 1}, "stats": {"stopped": 1}}


def sc_wake_pickup():
    b = Bench("wake-pickup-from-0", seed=9)
    b.run(3000); b.connect(); b.run(6000)
    b.pod_sleep(2); b.run(5000)
    b.wake(boot_id=0x3009); b.run(20000)  # head ~4200 < ring_n: lo must be 0, not a wrapped u32
    b.connect(); b.run(15000); b.stop(); b.run(3000); b.finish()
    p1, p2 = b.pods
    e1, e2 = b.stream.epochs
    spread = b.lat[1] + 1.0
    b.note = f"rebase error {rebase_error(b, p1, p2) * 1000:+.2f} ms"
    err = lambda: (None if abs(rebase_error(b, p1, p2)) * 1000 <= spread else "rebase error over bound")
    return b, {"ranges": [(e1, first_cmd(p1), p1.drain_heads[-1][1]), (e2, 0, last_drain(p2))],
               "stats": {"epochs": 2, "stopped": 2}, "extra": {"rebase": err}}


def sc_stale():
    b = Bench("stale-reanchor", seed=10)
    b.run(3000); b.connect(); b.run(6000)
    b.disconnect(); b.run(10000)
    b.connect(); b.run(150)               # catching up: frames are on the air all the time
    b.command()                           # the phone re-issues mid catch-up
    b.run(6000); b.stop(); b.run(3000); b.finish()
    p = b.pods[0]; ep = b.stream.epochs[0]
    return b, {"ranges": [(ep, first_cmd(p), last_drain(p))],
               "stats": {"stale": lambda v: v > 0, "reconnects": 1, "stopped": 1}}


def sc_status_race():
    b = Bench("periodic-status-race", seed=11)
    b.run(3000)
    b.connect()
    b.pods[0].ble.next_status_wall = b.now + 2   # a periodic STATUS goes out just before the answer
    b.run(10000); b.stop(); b.run(3000); b.finish()
    p = b.pods[0]; ep = b.stream.epochs[0]
    return b, {"ranges": [(ep, first_cmd(p), last_drain(p))], "stats": {"stopped": 1}}


def sc_pod_mtu():
    # Chrome on Android never asks for an MTU: the pod's own exchange (connect + 50 ms) completes a
    # round trip later. An INFO read before that gets BUSY (40 B doesn't fit MTU 23) and is retried;
    # a BUSY carries no frame, so the decoder sees exactly one INFO
    b = Bench("pod-mtu-exchange", seed=20, native_mtu=False)
    b.run(3000); b.connect(); b.run(8000); b.stop(); b.run(3000); b.finish()
    p = b.pods[0]; ep = b.stream.epochs[0]
    busy = lambda: None if b.busy >= 1 else "no BUSY read before the exchange"
    return b, {"ranges": [(ep, first_cmd(p), last_drain(p))], "stats": {"stopped": 1},
               "extra": {"BUSY before the exchange": busy, "one INFO": lambda: one_info(b)}}


def sc_mtu_60_stop():
    # MTU 60: INFO and STATUS fit, DATA doesn't (n_max 2): mtu_too_small, no samples, and STOP is
    # answered at once (the capped drain)
    b = Bench("mtu-60-stop", seed=12, mtu=60, native_mtu=False)
    b.run(3000); b.connect(); b.run(5000); b.stop(); b.run(4000); b.finish()
    flag = lambda: None if any(k == "mtu-too-small" for k, _ in b.events) else "no mtu-too-small"
    return b, {"no_ranges_ok": True, "stats": {"samples": 0, "stopped": 1}, "extra": {"mtu event": flag}}


def sc_stop_exact_tail():
    # a slow link (40-80 ms, ~50 frames/s of 4 samples) against a cap that assumes 60 frames/s
    b = Bench("stop-capped-tail", seed=21, mtu=79, lat=(40.0, 40.0))
    b.run(3000); b.connect(); b.run(4000)
    b.disconnect(); b.run(30000)
    b.connect(); b.run(300)
    b.stop()
    run_until(b, lambda: b.stream.stats["stopped"] >= 1, "the capped STOP's answer", step=100)
    b.lat = (7.5, 30.0)                   # the link speeds up again
    b.command(); b.run(5000)              # Start again: the counted tail must not come back
    b.stop()
    for _ in range(600):                  # until that STOP is answered (a backlog at n_max 4)
        if b.stream.stats["stopped"] >= 2:
            break
        b.run(100)
    b.finish()
    p = b.pods[0]; ep = b.stream.epochs[0]
    return b, {"ranges": [(ep, first_cmd(p), last_drain(p))],
               "gaps": {"stop-tail": one_gap},
               "stats": {"reconnects": 1, "stopped": 2}}


def sc_stop_then_start():
    b = Bench("stop-then-start-mid-drain", seed=22, mtu=79)
    b.run(3000); b.connect(); b.run(4000)
    b.disconnect(); b.run(20000)
    b.connect(); b.run(300)
    b.stop(); b.run(200)                  # the drain is still running...
    b.command()                           # ...and the user hits Start again
    b.run(15000); b.stop(); b.run(4000); b.finish()
    p = b.pods[0]; ep = b.stream.epochs[0]
    return b, {"ranges": [(ep, first_cmd(p), last_drain(p))],
               "stats": {"reconnects": 1, "stopped": 1, "stale": ANY}}


def sc_stop_then_start_late():
    b = Bench("stop-then-start-after-drain", seed=23)
    b.run(3000); b.connect(); b.run(6000)
    b.stop()
    run_until(b, lambda: b.pods[0].drain_heads, "the end of the STOP drain")   # stopped is queued...
    b.command()                           # ...and Start goes out before that STATUS arrives
    b.run(8000); b.stop(); b.run(3000); b.finish()
    p = b.pods[0]; ep = b.stream.epochs[0]
    return b, {"ranges": [(ep, first_cmd(p), last_drain(p))],
               "stats": {"stale_stops": lambda v: v <= 1, "stopped": lambda v: v in (1, 2),
                         "stale": ANY}}


def sc_stop_timeout_late():
    b = Bench("stop-timeout-late-reply", seed=13)
    b.run(3000); b.connect(); b.run(8000)
    b.jam_until = b.now + 6000            # a congested link: everything waits ~6 s
    b.run(300); b.stop(); b.run(4000)
    r = b.stream.stop_timed_out()         # the app calls it again: must say "already over"
    b.log.append(("call", None, f"stop_timed_out={int(r)}"))
    b.run(5000); b.finish()
    p = b.pods[0]; ep = b.stream.epochs[0]
    return b, {"ranges": [(ep, first_cmd(p), last_drain(p))],
               "stats": {"stopped": 1}, "faults": ["stop timed out"]}


def sc_stop_start_lost():
    b = Bench("stop-start-then-link-lost", seed=31)
    b.run(3000); b.connect(); b.run(5000)
    b.disconnect(); b.run(15000)
    b.connect(); b.run(300)               # catching up: the STATUS head is far ahead
    b.stop(); b.run(5)
    b.command()                           # Start right after Stop: the stop wait is cancelled
    b.run(600)
    b.disconnect()                        # the link dies mid catch-up; the session ends
    true_end = b.pod.head
    nxt = b.stream.ep.next_idx
    b.finish()                            # a lost-tail, not a stop-tail
    p = b.pods[0]; ep = b.stream.epochs[0]
    kinds = lambda: None if all(g["kind"] != "stop-tail" for g in b.stream.gaps) else "finish called it a stop-tail"
    return b, {"ranges": [(ep, first_cmd(p), end_known(b, ep))], "true_loss": true_end - nxt,
               "gaps": {"lost-tail": one_gap},
               "stats": {"stale": ANY, "stale_stops": lambda v: v <= 1, "reconnects": 1},
               "extra": {"kind": kinds}}


def sc_window_then_wake():
    b = Bench("link-degrades-then-wake", seed=32)
    b.run(3000); b.connect(); b.run(15000)
    b.lat = (60.0, 20.0)                  # the link gets slow: the last 32 STATUS all arrive late
    b.run(40000)
    b.pod_sleep(1); b.run(3000)
    b.lat = (7.5, 30.0)
    b.wake(boot_id=0x9032); b.run(2000)
    b.connect(); b.run(5000); b.stop(); b.run(3000); b.finish()
    p1, p2 = b.pods
    e1, e2 = b.stream.epochs
    return b, {"ranges": [(e1, first_cmd(p1), p1.drain_heads[-1][1]), (e2, 0, last_drain(p2))],
               "stats": {"epochs": 2, "stopped": 2}}


def sc_stop_timeout_finish():
    b = Bench("stop-timeout-gives-up", seed=24, mtu=79)
    b.run(3000); b.connect(); b.run(4000)
    b.disconnect(); b.run(20000)
    b.connect(); b.run(1500)              # catching up: STATUS heads run far ahead of the data
    b.jam_until = b.now + 10**9           # ...and then the link never comes back
    b.run(300); b.stop(); b.run(40000)
    true_end = b.pods[0].head
    nxt = b.stream.ep.next_idx
    b.finish()
    ep = b.stream.epochs[0]; p = b.pods[0]
    flagged = lambda: None if all(g.get("lower_bound") for g in b.stream.gaps if g["kind"] == "stop-tail") else "stop-tail not lower_bound"
    return b, {"ranges": [(ep, first_cmd(p), end_known(b, ep))], "true_loss": true_end - nxt,
               "gaps": {"stop-tail": one_gap},
               "stats": {"reconnects": 1}, "faults": ["stop timed out"], "extra": {"lower bound": flagged}}


def sc_lost_tail():
    b = Bench("reboot-while-away", seed=14)
    b.run(3000); b.connect(); b.run(8000)
    b.disconnect(); b.run(2000)
    true_end = b.pod.head
    nxt = b.stream.ep.next_idx
    b.pod = None                          # brown-out while disconnected: no goodbye STATUS
    b.wake(boot_id=0x4014); b.run(3000)
    b.connect(); b.run(6000); b.stop(); b.run(3000); b.finish()
    p1, p2 = b.pods
    e1, e2 = b.stream.epochs
    return b, {"ranges": [(e1, first_cmd(p1), end_known(b, e1)), (e2, 0, last_drain(p2))],
               "true_loss": true_end - nxt, "gaps": {"lost-tail": lambda got: len(got) <= 1},
               "stats": {"epochs": 2, "stopped": 1}}


def sc_lost_tail_catchup():
    b = Bench("reboot-mid-catch-up", seed=30, mtu=79)
    b.run(3000); b.connect(); b.run(4000)
    b.disconnect(); b.run(20000)
    b.connect(); b.run(1500)              # catching up: STATUS heads run far ahead of the data
    b.disconnect()
    true_end = b.pod.head
    nxt = b.stream.ep.next_idx
    b.pod = None                          # brown-out while away
    b.wake(boot_id=0x8030); b.run(2000)
    b.connect(); b.run(5000); b.stop(); b.run(3000); b.finish()
    p1, p2 = b.pods
    e1, e2 = b.stream.epochs
    flagged = lambda: None if all(g.get("lower_bound") for g in b.stream.gaps if g["kind"] == "lost-tail") else "lost-tail not lower_bound"
    return b, {"ranges": [(e1, first_cmd(p1), end_known(b, e1)), (e2, 0, last_drain(p2))],
               "true_loss": true_end - nxt, "gaps": {"lost-tail": one_gap},
               "stats": {"epochs": 2, "reconnects": 1, "stopped": 1}, "extra": {"lower bound": flagged}}


def sc_same_boot_id():
    b = Bench("reboot-same-boot-id", seed=15)
    b.run(3000); b.connect(); b.run(6000)
    boot = b.pods[0].boot_id
    b.disconnect(); b.run(1000)
    true_end = b.pod.head
    nxt = b.stream.ep.next_idx
    b.pod = None
    b.wake(boot_id=boot); b.run(2000)     # the RNG failed: same boot_id after a reboot
    b.connect(); b.run(6000); b.stop(); b.run(3000); b.finish()
    p1, p2 = b.pods
    e1, e2 = b.stream.epochs
    return b, {"ranges": [(e1, first_cmd(p1), end_known(b, e1)), (e2, 0, last_drain(p2))],
               "true_loss": true_end - nxt, "gaps": {"lost-tail": lambda got: len(got) <= 1},
               "stats": {"epochs": 2, "reconnects": 1, "stopped": 1, "stale": ANY},
               "faults": ["reboot without a new boot_id"]}


def sc_boot_mismatch():
    b = Bench("boot-mismatch-rollback", seed=16)
    b.run(3000); b.connect(); b.run(6000)
    old_info = b.pod.info_frame()
    b.disconnect()
    true_end = b.pod.head
    nxt = b.stream.ep.next_idx
    b.pod = None
    b.wake(boot_id=0x6016); b.run(15000)  # long enough that head and ticks pass the old ones
    b.connect(info=old_info)              # the INFO read returns the old (cached) value
    b.run(500)
    if any(k == "need-info" for k, _ in b.events):
        b.reread()                        # READ INFO and STATUS again, then the command
    b.run(6000); b.stop(); b.run(3000); b.finish()
    p1, p2 = b.pods
    e1, e2 = b.stream.epochs
    return b, {"ranges": [(e1, first_cmd(p1), end_known(b, e1)), (e2, 0, last_drain(p2))],
               "true_loss": true_end - nxt, "gaps": {"lost-tail": lambda got: len(got) <= 1},
               "stats": {"epochs": 2, "reconnects": 1, "stopped": 1, "ignored": ANY},
               "faults": ["boot mismatch"]}


def sc_other_pod():
    b = Bench("other-pod-and-bad-info", seed=25)
    b.run(3000); b.connect(); b.run(4000)
    b.disconnect(); b.run(500)
    # the phone briefly connects to a neighbour's pod, then reads an INFO from the future (proto 2)
    b.feed_read(pl.REG_INFO, b.pod.info_frame(serial=0xBEEF))
    b.feed_read(pl.REG_STATUS, b.pod.ble.status_frame())
    none1 = b.command()
    b.feed_read(pl.REG_INFO, b.pod.info_frame(proto=2))
    none2 = b.command()
    b.run(500); b.connect(); b.run(5000); b.stop(); b.run(3000); b.finish()
    p = b.pods[0]; ep = b.stream.epochs[0]
    refused = lambda: None if none1 is None and none2 is None else "command() did not refuse"
    return b, {"ranges": [(ep, first_cmd(p), last_drain(p))],
               "stats": {"ignored": 1, "reconnects": 1, "stopped": 1},
               "faults": ["another pod", "bad INFO (proto or boot_id)"], "extra": {"refused": refused}}


def sc_held_data():
    b = Bench("data-before-status", seed=26)
    b.run(3000); b.connect(sloppy=True); b.run(5000); b.stop(); b.run(3000); b.finish()
    held = lambda: None if b.held_seen else "no DATA was ever held"
    p = b.pods[0]; ep = b.stream.epochs[0]
    return b, {"ranges": [(ep, first_cmd(p), last_drain(p))], "stats": {"stopped": 1},
               "extra": {"held": held}}


def sc_clock_step():
    b = Bench("phone-clock-step", seed=27)
    b.run(3000); b.connect(); b.run(6000)
    b.pod_sleep(1); b.run(2000)
    b.clock_step = -60000.0               # the phone's clock jumps back a minute (NTP)
    b.wake(boot_id=0x7027); b.run(2000)
    b.connect(); b.run(5000); b.stop(); b.run(3000); b.finish()
    p1, p2 = b.pods
    e1, e2 = b.stream.epochs
    return b, {"ranges": [(e1, first_cmd(p1), p1.drain_heads[-1][1]), (e2, 0, last_drain(p2))],
               "stats": {"epochs": 2, "stopped": 2, "clock_clamped": 1}}


def sc_after_close():
    b = Bench("frames-after-close", seed=28)
    b.run(3000); b.connect(); b.run(5000); b.stop(); b.run(3000)
    last = frames_rx(b, pl.T_DATA)[-1]
    b.feed_push(pl.REG_DATA, last)        # a pod bug: a DATA frame after stream_stopped
    b.feed_read(pl.REG_CAL, pl.build_cal())               # CAL: ignored in v1, not 'bad'
    b.feed_push(pl.REG_DATA, bytes([pl.T_DATA, 2, 0, 0]))  # truncated DATA
    b.feed_push(pl.REG_DATA, bytes([0x55]) + bytes(19))    # unknown type
    # packets that carry no frame: the adapter gives nothing and nothing is counted
    info = b.pod.info_frame()
    for pkt in (crp.response(9, crp.BUSY, pl.REG_INFO), crp.response(9, crp.ACCESS_DENIED, pl.REG_STATUS, info[:20]),
                crp.response(9, crp.OK, pl.REG_CTRL), crp.response(9, crp.OK, pl.REG_DATA, last),
                crp.response(9, crp.OK, pl.REG_PUSH, b"\x01"), crp.nop(), crp.hello(crp.VERSION, 1, 244, 1, pl.DEVICE_ID),
                crp.log(crp.LOG_INFO, 5, "pod", "hi"), crp.write(7, pl.REG_DATA, last), crp.push(pl.REG_CTRL, b"\x03"),
                crp.push(pl.REG_INFO, info), crp.push(0x00020003, b"\x00\x00\x00\x00"),
                bytes([0x05, 0x00]) + struct.pack("<I", pl.REG_DATA) + last, crp.response(9, crp.OK, pl.REG_INFO)[:7],
                bytes([crp.WRITE, 0, 4, 0, 1])):
        b.on_packet(pkt, b.now)
    b.finish()
    p = b.pods[0]; ep = b.stream.epochs[0]
    return b, {"ranges": [(ep, first_cmd(p), last_drain(p))],
               "stats": {"after_close": 1, "bad": 2, "stopped": 1}}


def sc_round_tie():
    """A crafted 1024-tick hole: 1024 x 208 / 32768 = 6.5 exactly, so n = 6 (rounds .5 up)."""
    b = Bench("hole-rounding-tie", seed=29)
    b.feed_read(pl.REG_INFO, b.pod.info_frame(), 0.0)
    b.feed_push(pl.REG_STATUS, pl.build_status(0, 3900, 1000, 0, 0, 0, 0, 16, 32), 5.0)
    cmd = b.command()
    seq = cmd[1]
    p = b.pods[0]
    p.ticks = [1000, 1158, 1158 + 1024]   # the pod's truth for the 3 crafted samples
    smp = [(t, p.raw(i)) for i, t in enumerate(p.ticks)]
    b.feed_push(pl.REG_DATA, pl.build_data(0, 0, 1000, pl.F_RESYNC, seq, smp), 20.0)
    six = lambda got: got == [6]
    b.finish()
    return b, {"no_ranges_ok": True, "gaps": {"imu": six}}


# ---- D45: CRP ----

def sc_crp_busy_before_mtu():
    # Chrome on Android never asks for an MTU, and this phone answers the pod's own exchange late
    # (1.2 s after connect): the INFO read gets BUSY again and again (retried every 200 ms). BUSY
    # carries no frame, so the decoder sees one INFO and no reconnect
    b = Bench("crp-busy-before-mtu", seed=46, native_mtu=False, mtu_after=1200)
    b.run(3000); b.connect(); b.run(5000); b.stop(); b.run(3000); b.finish()
    p = b.pods[0]; ep = b.stream.epochs[0]
    busy = lambda: None if b.busy >= 4 else f"{b.busy} BUSY answers, want 4 or more"
    return b, {"ranges": [(ep, first_cmd(p), last_drain(p))], "stats": {"stopped": 1, "reconnects": 0},
               "extra": {"BUSY retried": busy, "one INFO": lambda: one_info(b)}}


def sc_mtu_23_forever():
    # an MTU that never leaves 23: every INFO read is BUSY for 3 s, then the phone reads the MTU
    # register (10 B, fits) and gives up: no PUSH, no command, nothing decoded
    b = Bench("mtu-23-forever", seed=47, mtu=23, native_mtu=False)
    b.run(3000)
    ok = b.connect()
    b.run(2000); b.finish()
    gave_up = lambda: None if ok is False and b.mtu_read == 23 else f"connect gave {ok}, the MTU read {b.mtu_read}"
    no_cmd = lambda: None if not any(k == "cmd" for k, _, _ in b.log) else "a STREAM command went out"
    no_push = lambda: (None if not any(bytes.fromhex(h)[:1] == bytes([crp.WRITE]) for k, _, h in b.log if k == "tx")
                       else "the phone wrote a register")
    busy = lambda: None if b.busy >= 10 else f"{b.busy} BUSY answers in 3 s, want 10 or more"
    return b, {"no_ranges_ok": True, "stats": {"samples": 0, "epochs": 0},
               "extra": {"gave up": gave_up, "no command": no_cmd, "no write": no_push, "BUSY retried": busy}}


def sc_crp_reread_under_backlog():
    # a slow link (MTU 79: 4 samples per frame) catching up after 15 s away, and a phone that
    # re-reads STATUS every 400 ms meanwhile. Each read is built when the pod processes it, and its
    # answer waits in the spool ahead of any further push, so its head is never below what the
    # phone already has: no false reboot
    b = Bench("crp-reread-under-backlog", seed=48, mtu=79)
    b.run(3000); b.connect(); b.run(4000)
    b.disconnect(); b.run(15000)
    b.connect()
    reads = []
    for _ in range(12):
        b.run(400)
        reads.append(b.request(lambda txn: crp.read(txn, pl.REG_STATUS)))
    b.run(20000); b.stop(); b.run(4000); b.finish()
    p = b.pods[0]; ep = b.stream.epochs[0]
    answered = lambda: (None if all(b.answers.get(t) and b.answers[t][0] == crp.OK for t in reads)
                        else "a STATUS read went unanswered")
    behind = [h - n for h, n in b.status_reads[-12:] if n is not None]
    under = lambda: None if sum(d > 500 for d in behind) >= 6 else f"reads not under a backlog: {behind}"
    return b, {"ranges": [(ep, first_cmd(p), last_drain(p))], "stats": {"reconnects": 1, "stopped": 1},
               "extra": {"answered": answered, "under backlog": under}}


def sc_crp_reread_live():
    # a client polling STATUS while the stream runs live (MTU 247, caught up: every read lands
    # within a frame or two of the data): 4 reads back to back every 200 ms for 15 s. 3 answers
    # take the slots and the 4th waits in the spool; a slot can free mid-pass (TX-complete comes
    # from the BLE task) and no push may take it while the spool holds that answer. If one did, a
    # DATA frame built after the read would arrive first, and the read's head would be below
    # nextIdx: a false reboot without a new boot_id (docs/ble.md 5 and 7)
    b = Bench("crp-reread-live", seed=59)
    b.run(3000); b.connect(); b.run(2000)
    reads = []
    for _ in range(75):
        b.run(200)
        reads += [b.request(lambda txn: crp.read(txn, pl.REG_STATUS)) for _ in range(4)]
    b.run(1000); b.stop(); b.run(3000); b.finish()
    p = b.pods[0]; ep = b.stream.epochs[0]
    answered = lambda: (None if all(b.answers.get(t) and b.answers[t][0] == crp.OK for t in reads)
                        else "a STATUS read went unanswered")
    behind = [h - n for h, n in b.status_reads if n is not None]
    live = lambda: (None if len(behind) >= 250 and max(behind) <= 32
                    else f"{len(behind)} reads during the stream, up to {max(behind, default=None)} samples behind")
    held = lambda: None if p.ble.spool_holds > 0 else "the spool never held a push back with a slot free"
    b.note = f"{p.ble.spool_holds} pushes held for the spool, reads up to {max(behind, default=0)} samples behind"
    return b, {"ranges": [(ep, first_cmd(p), last_drain(p))], "stats": {"stopped": 1},
               "extra": {"answered": answered, "live": live, "the spool rule decided": held}}


def sc_mtu_185_stream():
    # MTU 185 (iOS): n_max = (185 - 3 - 6 - 14) / 14 = 11, a 168 B frame in a 174 B push, MTU - 3 is
    # 182. Without the push's 6 B header the pod would say 12, build 182 B frames whose 188 B push
    # never fits, and stall DATA without mtu_too_small (docs/ble.md 5: no quiet degrading)
    b = Bench("mtu-185-stream", seed=60, mtu=185)
    b.run(3000); b.connect(); b.run(6000); b.stop(); b.run(3000); b.finish()
    p = b.pods[0]; ep = b.stream.epochs[0]
    ns = [pl.decode_frame(f)["n"] for f in frames_rx(b, pl.T_DATA)]
    npp = {pl.decode_frame(f)["n_per_packet"] for f in frames_rx(b, pl.T_STATUS)}
    eleven = lambda: (None if ns and max(ns) == 11 and ns.count(11) > len(ns) // 2 and npp == {11}
                      else f"DATA n up to {max(ns, default=None)}, STATUS n_per_packet {sorted(npp)}")
    if not p.drain_heads:                 # a stalled stream never finishes its STOP drain in time
        return b, {"no_ranges_ok": True, "extra": {"11 samples per frame": eleven,
                                                   "STOP drained": lambda: "the STOP drain never finished"}}
    return b, {"ranges": [(ep, first_cmd(p), last_drain(p))], "stats": {"stopped": 1},
               "extra": {"11 samples per frame": eleven}}


def sc_mtu_73_stop():
    # MTU 73, just under 79: n_max = (73 - 23) / 14 = 3, so mtu_too_small, no DATA, and the STOP is
    # answered at once (the capped drain). INFO and STATUS fit, so the connection order completes.
    # Without the push header in the formula the pod would say 4, build 70 B frames whose 76 B push
    # never fits MTU - 3 = 70, and neither flag the MTU nor cap the STOP
    b = Bench("mtu-73-stop", seed=61, mtu=73)
    b.run(3000); b.connect(); b.run(5000); b.stop(); b.run(4000); b.finish()
    flag = lambda: None if any(k == "mtu-too-small" for k, _ in b.events) else "no mtu-too-small"
    npp = {pl.decode_frame(f)["n_per_packet"] for f in frames_rx(b, pl.T_STATUS)}
    three = lambda: None if npp == {3} else f"STATUS n_per_packet {sorted(npp)}, want 3"
    no_data = lambda: None if not frames_rx(b, pl.T_DATA) else f"{len(frames_rx(b, pl.T_DATA))} DATA frames"
    cmd = lambda: None if any(k == "cmd" and h != "none" for k, _, h in b.log) else "the connection order never sent its command"
    return b, {"no_ranges_ok": True, "stats": {"samples": 0, "stopped": 1},
               "extra": {"mtu event": flag, "n_per_packet": three, "no DATA": no_data, "command sent": cmd}}


def sc_crp_push_before_info():
    # NEGATIVE control: a client that turns PUSH on right after HELLO, before its INFO and STATUS
    # reads (docs/ble.md 7 puts PUSH after them). With a slow MTU exchange the reads wait on BUSY
    # while STATUS pushes already flow: at the first connect they come before any INFO (pre_info);
    # after the pod sleeps and wakes, the woken pod's STATUS is read against the old epoch, its
    # small head looks like a reboot without a new boot_id, and that false epoch leaves a false
    # lost-tail (boot 2's samples all arrive, in the epoch its INFO opens). Expected: exactly these
    b = Bench("crp-push-before-info", seed=49, native_mtu=False, mtu_after=1200)
    b.run(3000); b.connect(push_early=True); b.run(6000)
    b.pod_sleep(1); b.run(3000)
    b.wake(boot_id=0x2049); b.run(3000)
    b.connect(push_early=True); b.run(6000); b.stop(); b.run(3000); b.finish()
    p1, p2 = b.pods
    eps = b.stream.epochs
    if len(eps) != 3:
        return b, {"no_ranges_ok": True, "extra": {"negative control": lambda: f"{len(eps)} epochs, want 3"}}
    e1, e2, e3 = eps
    owed = e2.status["head"]
    b.note = "negative control: the false reboot and pre_info, as expected"
    return b, {"ranges": [(e1, first_cmd(p1), p1.drain_heads[-1][1]), (e2, 0, owed), (e3, 0, last_drain(p2))],
               "gaps": {"lost-tail": lambda got: got == [owed]},
               "stats": {"pre_info": lambda v: v > 0, "epochs": 3, "stopped": 2},
               "faults": ["reboot without a new boot_id"]}


def sc_crp_hello_mid_stream():
    # the app restarts its CRP client on a live link: HELLO in the middle of a stream. The pod
    # starts a new session, which ends the stream as a disconnect would (PUSH off, the queued STATUS
    # and the built frame dropped, the hold kept); the phone runs the connection order again and
    # STREAM_FROM picks up where it was, losing nothing. PUSH is 0 at every HELLO (docs/ble.md 2.3):
    # the app waits 1.2 s before PUSH = 1 this time, so the pod's 1 s STATUS falls in between and
    # must not be pushed
    b = Bench("crp-hello-mid-stream", seed=50)
    b.run(3000); b.connect(); b.run(6000)
    b.connection_order(push_delay=1200)
    b.run(6000); b.stop(); b.run(3000); b.finish()
    p = b.pods[0]; ep = b.stream.epochs[0]
    ends = lambda: (None if [(w, h) for _, w, h in p.ble.session_ends] == [("hello", True)]
                    else f"session ends {p.ble.session_ends}, want one by HELLO with the hold kept")
    ops = lambda: (None if [op for op, _, _ in p.cmd_log] == [pl.OP_STREAM_LIVE, pl.OP_STREAM_FROM]
                   else f"commands {p.cmd_log}")

    def push_reset():
        # sampled as each HELLO reached the phone, and on the air: no push from the second HELLO
        # until the second PUSH = 1 is answered (by arrival position)
        at_hello = None if b.hello_push == [0, 0] else f"PUSH {b.hello_push} as the pod's HELLOs arrived, want [0, 0]"
        return "; ".join(m for m in (at_hello, pushes_after_hello()) if m) or None

    def pushes_after_hello():
        rx = [crp.parse(pkt) for _, pkt in received(b)]
        sent = [crp.parse(bytes.fromhex(h)) for k, _, h in b.log if k == "tx"]
        hellos = [k for k, q in enumerate(rx) if q and q["type"] == crp.HELLO]
        push_txns = [q["txn"] for q in sent if q["type"] == crp.WRITE and q["reg"] == pl.REG_PUSH]
        if len(hellos) != 2 or len(push_txns) != 2:
            return f"{len(hellos)} HELLOs from the pod, {len(push_txns)} PUSH writes, want 2 and 2"
        ans = [k for k, q in enumerate(rx) if k > hellos[1] and q and q["type"] == crp.RESPONSE and q["txn"] == push_txns[1]]
        if not ans:
            return "the second PUSH = 1 was never answered"
        early = [k for k in range(hellos[1] + 1, ans[0]) if rx[k] and rx[k]["type"] == crp.WRITE and rx[k]["txn"] == 0]
        return None if not early else f"{len(early)} pushes between the second HELLO and PUSH = 1"
    return b, {"ranges": [(ep, first_cmd(p), last_drain(p))], "stats": {"reconnects": 1, "stopped": 1},
               "extra": {"session end": ends, "STREAM_FROM after it": ops, "PUSH 0 from the HELLO on": push_reset}}


def sc_crp_heartbeat_stops():
    # the phone sends nothing at all for 40 s of streaming (Bluetooth clients send no heartbeat,
    # and a locked phone's WebView throttles its timers): the pod ignores liveness on Bluetooth and
    # streams on. Beside it a PC on USB whose heartbeat stops (its process stalls): 3 s without
    # receiving ends the USB session and the port goes back to text. The PC can only count up to
    # the last head it heard, and on USB (n_max 16, no interval) its DATA is already past every
    # STATUS head, so finish() has at most a lower-bound tail to record, never more than the truth
    b = Bench("crp-heartbeat-stops", seed=51)
    b.run(3000); b.connect()
    t_quiet = b.now
    pc = b.attach_usb(); pc.open_port(); b.run(400); pc.connect()
    b.run(5000)
    pc.mute = True
    t_mute = b.now
    b.run(6000)
    u = b.pod.usb
    pe = pc.stream.ep
    pc_true = b.pod.head - pe.next_idx
    pc.finish(); pc.close_port()
    b.run(30000)
    ble_live = b.pod.ble.session and b.pod.ble.push == 1 and b.pod.ble.streaming
    phone_tx = [a for k, a, _ in b.log if k == "tx" and a > t_quiet + 1000]
    b.stop(); b.run(3000); b.finish()
    p = b.pods[0]; ep = b.stream.epochs[0]
    to_text = [m for m in u.mode_log if m[1] == "text"]
    timed_out = lambda: (None if to_text and to_text[0][2] == "silence" and t_mute + 2000 <= to_text[0][0] <= t_mute + 3100
                         else f"USB modes {u.mode_log}, muted at {t_mute}")
    text_after = lambda: None if pc.text_rx > 0 and pc.unowned == pc.text_rx else \
        f"text {pc.text_rx} B, the deframer left out {pc.unowned}"
    ble = lambda: (None if ble_live and not phone_tx and not p.ble.session_ends
                   else f"BLE session on {ble_live}, phone sent at {phone_tx[:3]}, ends {p.ble.session_ends}")
    return b, {"ranges": [(ep, first_cmd(p), last_drain(p))], "stats": {"stopped": 1},
               "extra": {"BLE streams on with a silent phone": ble},
               "usb": {"ranges": [(pe, first_cmd(u), end_known(pc, pe))], "true_loss": pc_true,
                       "gaps": {"lost-tail": lambda got: len(got) <= 1},
                       "extra": {"3 s of silence ends the USB session": timed_out, "back to text": text_after}}}


def sc_crp_spool_reserve():
    # catch-up after 20 s away keeps all 3 slots busy with DATA. The phone then reads STATUS and
    # sends STOP back to back: both answers go ahead of the backlog (an engine packet waits in the
    # spool, and no push goes while it holds one), in order, and the stream_stopped STATUS follows
    # the STOP's answer
    b = Bench("crp-spool-reserve", seed=52)
    b.run(3000); b.connect(); b.run(4000)
    b.disconnect(); b.run(20000)
    b.connect(); b.run(300)
    t0 = b.now
    r = b.request(lambda txn: crp.read(txn, pl.REG_STATUS))
    s = b.stop()
    b.run(8000); b.finish()
    p = b.pods[0]; ep = b.stream.epochs[0]
    # order by arrival position, not by the ms: packets of one connection event share it
    rx = received(b, t0)
    pos, at, stopped = {}, {}, []
    for k, (a, pkt) in enumerate(rx):
        q, f = crp.parse(pkt), pl.crp_frame(pkt)
        if q and q["type"] == crp.RESPONSE and q["txn"] in (r, s) and q["txn"] not in pos:
            pos[q["txn"]], at[q["txn"]] = k, a
        if f and f[0] == pl.T_STATUS and f[1] & pl.S_STREAM_STOPPED:
            stopped.append(k)
    behind = b.status_reads[-1][0] - b.status_reads[-1][1] if b.status_reads else 0

    def order():
        if r not in pos or s not in pos:
            return "an answer is missing"
        if not (pos[r] < pos[s] and at[s] <= t0 + 200):
            return (f"READ answered at +{at[r] - t0:.0f} ms (packet {pos[r]}), STOP at +{at[s] - t0:.0f} ms "
                    f"(packet {pos[s]}): want in order, within 200 ms")
        if not stopped or stopped[0] <= pos[s]:
            return "no stream_stopped after the STOP's answer"
        if behind < 2000:
            return f"only {behind} samples behind: not a backlog"
        return None
    drops = lambda: None if p.ble.tx_drops == 0 else f"{p.ble.tx_drops} engine packets dropped"
    b.note = f"answers +{at.get(r, 0) - t0:.0f} / +{at.get(s, 0) - t0:.0f} ms, {behind} samples behind"
    return b, {"ranges": [(ep, first_cmd(p), last_drain(p))], "stats": {"reconnects": 1, "stopped": 1},
               "extra": {"answers ahead of the backlog, in order": order, "no TX drops": drops}}


def sc_crp_nop_jam():
    # NOPs, the pod's heartbeat, only ever go straight out. A quiet session first (PUSH stays 0 for
    # 3.5 s, an app waiting on its user): a NOP each second, nothing else. Then a stream and a link
    # jammed for 6 s: every second the pod has sent nothing and wants a NOP, but no slot is free,
    # so each is dropped instead of queued. After the jam no NOPs pile out
    b = Bench("crp-nop-jam", seed=53)
    b.run(3000)
    t_on = b.now
    b.connect(push_delay=3500)
    t_push = b.now
    b.run(3000)
    t_jam = b.now
    b.jam_until = b.now + 6000
    b.run(9000); b.stop(); b.run(3000); b.finish()
    p = b.pods[0]; ep = b.stream.epochs[0]
    nops = lambda a, z: sum(1 for _, pkt in received(b, a, z) if pkt == crp.nop())
    quiet = lambda: None if 3 <= nops(t_on, t_push) <= 4 else f"{nops(t_on, t_push)} NOPs in the quiet 3.5 s, want 3 or 4"
    jam = lambda: (None if nops(t_push, b.now) == 0 and p.ble.nop_drops >= 5 and p.ble.spool_max <= 1 and not p.ble.tx_drops
                   else f"{nops(t_push, b.now)} NOPs while pushing, {p.ble.nop_drops} dropped in the jam, "
                        f"spool max {p.ble.spool_max}, TX drops {p.ble.tx_drops}")
    return b, {"ranges": [(ep, first_cmd(p), last_drain(p))], "stats": {"stopped": 1},
               "extra": {"NOPs when quiet": quiet, "no NOP pile-up": jam}}


def sc_crp_write_retry():
    # NEGATIVE control: a client that retries transactions (CRPClient's default, txnRetries 2; the
    # pod's clients use 0). The phone's app gets no notifications for 7 s just after STOP, so the
    # STOP's answer comes after the 5 s transaction timeout and the client sends the same STOP
    # again. The engine runs a repeated WRITE again: two stream_stopped, the second moving nextIdx
    # past the stop head. Expected: stopped 2 (and the stop wait's own timeout, from the stall)
    b = Bench("crp-write-retry", seed=54, txn_retries=1)
    b.run(3000); b.connect(); b.run(5000)
    b.rx_stall_until = b.now + 7000
    b.stop(); b.run(9000); b.finish()
    p = b.pods[0]; ep = b.stream.epochs[0]
    twice = lambda: (None if [k for k, _ in p.drain_heads] == ["stop", "stop-idle"]
                     else f"the pod's stops {p.drain_heads}, want the STOP run twice")
    b.note = "negative control: two stream_stopped, as expected"
    return b, {"ranges": [(ep, first_cmd(p), p.drain_heads[0][1])], "stats": {"stopped": 2},
               "faults": ["stop timed out"], "extra": {"STOP ran twice": twice}}


def sc_crp_bye_reconnect():
    # the app ends the CRP session with BYE (CRPClient.close()) but the link stays up, and opens a
    # new one 3 s later: in between the pod has no session and sends nothing at all (no NOP, no
    # push); the new session's STREAM_FROM gets those 3 s back from the ring
    b = Bench("crp-bye-reconnect", seed=55)
    b.run(3000); b.connect(); b.run(6000)
    b.bye()
    t_bye = b.now
    b.run(3000)
    quiet = received(b, t_bye + 150)
    b.connection_order(); b.run(6000); b.stop(); b.run(3000); b.finish()
    p = b.pods[0]; ep = b.stream.epochs[0]
    silent = lambda: None if not quiet else f"{len(quiet)} packets without a session"
    ends = lambda: None if [(w, h) for _, w, h in p.ble.session_ends] == [("bye", True)] else f"{p.ble.session_ends}"
    return b, {"ranges": [(ep, first_cmd(p), last_drain(p))], "stats": {"reconnects": 1, "stopped": 1},
               "extra": {"silent without a session": silent, "session end": ends}}


def same_values(b, pc):
    """Indices both decoders kept must decode to the same values (one ring, two cursors)."""
    vals = lambda c: {i: tuple(s[k] for k in ("ax", "ay", "az", "gx", "gy", "gz")) for (_, i), s in zip(c.emits, c.samples)}
    a, z = vals(b), vals(pc)
    both = set(a) & set(z)
    if len(both) < 1000:
        return f"only {len(both)} indices on both links"
    bad = [i for i in both if a[i] != z[i]]
    return None if not bad else f"{len(bad)} indices differ"


def sc_crp_usb_link():
    # a PC on USB beside the phone on BLE (log_ble.py --usb). The port opens in text mode (the CSV),
    # a level 1 HELLO switches it to CRP, and the text before the pod's answer is dropped by the
    # deframer. Its own cursor: STREAM_LIVE, a port close (back to text, no STOP), a reopen whose
    # STREAM_FROM catches up from the ring, STOP, BYE (back to text). The phone's stream runs through
    # all of it. The reserve: during the catch-up the PC's reader stalls for 11 s (its heartbeat runs
    # on). DATA pushes fill the spool and the 1 s STATUS pushes take what's left, down to the 128 B
    # pushes leave (without it, under the 30 B a STATUS read's answer needs); a read near the end is
    # answered from the reserve
    b = Bench("crp-usb-link", seed=56)
    b.run(3000); b.connect()
    pc = b.attach_usb(); pc.open_port(); b.run(500)
    pc.connect()
    u = b.pod.usb
    b.run(8000)
    pc.close_port()
    b.run(5000)
    pc.open_port(); b.run(300); pc.connect()
    pc.read_stall_until = b.now + 11000
    b.run(10500)                          # past 9 STATUS ticks: 9 x 28 B > the 246 B of a DATA push
    stall_bytes = u.spool_bytes
    r = pc.request(lambda txn: crp.read(txn, pl.REG_STATUS))
    b.run(3000); pc.stop(); b.run(3000)
    pc.bye(); b.run(50); pc.close_port(); pc.finish()
    b.stop(); b.run(3000); b.finish()
    p = b.pods[0]; ep = b.stream.epochs[0]; pe = pc.stream.epochs[0]
    text = lambda: (None if pc.text_rx > 0 and pc.unowned == pc.text_rx
                    else f"text {pc.text_rx} B, the deframer left out {pc.unowned}")
    modes = lambda: (None if [(m, w) for _, m, w in u.mode_log] == [("crp", "hello"), ("text", "closed"), ("crp", "hello"), ("text", "bye")]
                     else f"USB modes {u.mode_log}")
    cursors = lambda: (None if [op for op, _, _ in u.cmd_log] == [pl.OP_STREAM_LIVE, pl.OP_STREAM_FROM]
                       and [op for op, _, _ in p.ble.cmd_log] == [pl.OP_STREAM_LIVE] else f"USB {u.cmd_log}, BLE {p.ble.cmd_log}")
    rd = pc.answers.get(r)
    # full to the reserve when the read came, and its answer went into the reserve
    reserve = lambda: (None if rd and rd[0] == crp.OK and not u.tx_drops and stall_bytes > USB_SPOOL - USB_RESERVE - 28
                       and u.spool_max > USB_SPOOL - USB_RESERVE
                       else f"the read {rd and rd[0]}, TX drops {u.tx_drops}, spool {stall_bytes} B at the read, "
                            f"max {u.spool_max} B")
    b.note = f"USB text dropped {pc.unowned} B, spool {stall_bytes} B at the read, max {u.spool_max} B"
    return b, {"ranges": [(ep, first_cmd(p), last_drain(p))], "stats": {"stopped": 1},
               "extra": {"the same values on both links": lambda: same_values(b, pc)},
               "usb": {"ranges": [(pe, first_cmd(u), last_drain(u))], "stats": {"reconnects": 1, "stopped": 1},
                       "extra": {"text before the HELLO": text, "back to text": modes, "own cursor": cursors,
                                 "the engine's reserve": reserve}}}


def sc_level1_resync():
    # level 1 framing on both ends, under planted garbage. The PC writes stray bytes, then its HELLO
    # in two writes (split inside the switch bytes): the pod still finds the switch. On the way
    # back, bytes no packet owns sit before five DATA pushes (a length of 0, 0xFFFF, 1025, a stray
    # "#" line, a lone newline): the PC's deframer drops exactly those and the text, and every
    # packet arrives (the wire splits them too: 64 B per ms)
    b = Bench("level1-resync", seed=57)
    b.run(3000)
    pc = b.attach_usb(); pc.open_port(); b.run(400)
    pc.junk_out = b"?\r\n"
    pc.split_next = 3
    pc.connect()
    u = b.pod.usb
    junk = [b"\x00\x00", b"\xff\xff", b"\x01\x04", b"# stray\r\n", b"\x0a"]
    u.junk = list(junk)
    b.run(8000); pc.stop(); b.run(3000); pc.bye(); b.run(50); pc.close_port(); pc.finish()
    pe = pc.stream.epochs[0]
    dropped = lambda: (None if not u.junk and pc.junk_rx == sum(map(len, junk)) and pc.text_rx > 0
                       and pc.unowned == pc.text_rx + pc.junk_rx
                       else f"left out {pc.unowned}, text {pc.text_rx}, junk {pc.junk_rx}, unsent {u.junk}")
    switched = lambda: None if u.mode_log[:1] and u.mode_log[0][1] == "crp" and not u.rx_drops else f"{u.mode_log}, RX drops {u.rx_drops}"
    return b, {"ble": False,
               "usb": {"ranges": [(pe, first_cmd(u), last_drain(u))], "stats": {"stopped": 1},
                       "extra": {"garbage dropped, every packet kept": dropped, "split switch found": switched}}}


SCENARIOS = [sc_clean, sc_scale, sc_reconnect_rewind, sc_overflow, sc_lap, sc_pkt_hole, sc_tick_wrap,
             sc_sleep_wake, sc_sleep_cut, sc_imu_restart, sc_wake_pickup, sc_stale, sc_status_race,
             sc_pod_mtu, sc_mtu_60_stop, sc_stop_exact_tail, sc_stop_then_start,
             sc_stop_then_start_late, sc_stop_timeout_late, sc_stop_start_lost, sc_window_then_wake,
             sc_stop_timeout_finish, sc_lost_tail,
             sc_lost_tail_catchup,
             sc_same_boot_id, sc_boot_mismatch, sc_other_pod, sc_held_data, sc_clock_step,
             sc_after_close, sc_round_tie, sc_stop_then_sleep, sc_stop_final_then_sleep,
             sc_reconnect_stop_before_data, sc_wake_drop_before_data, sc_wake_drop_then_reset,
             sc_crp_busy_before_mtu, sc_mtu_23_forever, sc_crp_reread_under_backlog, sc_crp_reread_live,
             sc_mtu_185_stream, sc_mtu_73_stop, sc_crp_push_before_info,
             sc_crp_hello_mid_stream, sc_crp_heartbeat_stops, sc_crp_spool_reserve, sc_crp_nop_jam,
             sc_crp_write_retry, sc_crp_bye_reconnect, sc_crp_usb_link, sc_level1_resync]


def runs_of(b, expect):
    """(vector name, client, expectation) per decoder a scenario checks: the phone's, then a PC's."""
    out = []
    if expect.get("ble", True):
        out.append((b.name, b, expect))
    if "usb" in expect:
        out.append((b.name + "/usb" if out else b.name, b.usb, expect["usb"]))
    return out


def run_all():
    """Run every scenario and write the vectors. Returns the number of failed scenarios."""
    DATA.mkdir(exist_ok=True)
    files = {k: open(DATA / f"fake_pod_{k}.csv", "w", newline="")
             for k in ("packets", "frames", "expected", "gaps", "stats", "events")}
    stat_keys = list(pl.PodStream(lambda s: None).stats)
    files["packets"].write("scenario,step,kind,arrival_ms,payload\n")
    files["frames"].write("scenario,step,kind,arrival_ms,payload\n")
    files["expected"].write("scenario,t,ax,ay,az,gx,gy,gz\n")
    files["gaps"].write("scenario,kind,n,idx,t0,t1,lower_bound\n")
    files["stats"].write("scenario," + ",".join(stat_keys) + ",faults\n")
    files["events"].write("scenario,k,kind,info\n")
    done = DATA / "fake_pod_runs.csv"     # written last, and only if every scenario ran: the vectors
    done.unlink(missing_ok=True)          # are whole (webapp/test/pod.mjs refuses them without it)
    n_fail, crashed, names = 0, 0, []
    for sc in SCENARIOS:
        try:
            b, expect = sc()
            runs = [(name, c, check(c, exp)) for name, c, exp in runs_of(b, expect)]
        except Exception:                 # one crashed scenario fails alone: the rest still get a verdict
            print(f"FAIL  {sc.__name__}: crashed")
            traceback.print_exc(file=sys.stdout)
            n_fail += 1
            crashed += 1
            continue
        sc_failed = False
        for name, c, fails in runs:
            names.append(name)
            logs = {"packets": c.log, "frames": frames_of(c.log)}
            rev = []
            for which, log in logs.items():
                try:                          # the replay must reproduce everything from the log alone
                    rs, rst, rev_w = pl.replay_rows(log)
                    if rs != c.samples:
                        fails.append(f"replay ({which}): samples differ")
                    if rst.gaps != c.stream.gaps or rst.stats != c.stream.stats or rst.faults != c.stream.faults:
                        fails.append(f"replay ({which}): gaps / stats / faults differ")
                    if [k for k, _ in rev_w if k != "need-command"] != [k for k, _ in c.events]:
                        fails.append(f"replay ({which}): events differ")
                    if which == "packets":
                        rev = rev_w
                except AssertionError as e:
                    fails.append(f"replay ({which}): {e}")
            sc_failed |= bool(fails)
            gaps = ", ".join(f"{g['kind']} {g['n']}" for g in c.stream.gaps) or "no gaps"
            note = f"  ({b.note})" if b.note and c is b else ""
            print(f"{'PASS' if not fails else 'FAIL'}  {name:28} {len(c.samples):6} samples, {gaps}{note}")
            for f in fails:
                print(f"      - {f}")
            for which, log in logs.items():
                for k, (kind, arr, payload) in enumerate(log):
                    files[which].write(f"{name},{k},{kind},{'' if arr is None else arr},{payload}\n")
            for s in c.samples:
                files["expected"].write(f"{name},{s['t']!r},{s['ax']!r},{s['ay']!r},{s['az']!r},"
                                        f"{s['gx']!r},{s['gy']!r},{s['gz']!r}\n")
            for g in c.stream.gaps:
                files["gaps"].write(f"{name},{g['kind']},{g['n']},{g['idx']},{g['t0']!r},{g['t1']!r},"
                                    f"{int(bool(g.get('lower_bound')))}\n")
            files["stats"].write(name + "," + ",".join(str(c.stream.stats[k]) for k in stat_keys)
                                 + "," + "|".join(c.stream.faults) + "\n")
            for k, (kind, info) in enumerate(rev):
                files["events"].write(f"{name},{k},{kind},{'' if info is None or isinstance(info, dict) else info}\n")
        n_fail += sc_failed
    for f in files.values():
        f.close()
    if not crashed:
        done.write_text("run\n" + "".join(f"{n}\n" for n in names))
    return n_fail


def main():
    n_fail = run_all()
    print(f"\n{len(SCENARIOS) - n_fail}/{len(SCENARIOS)} scenarios PASS. Vectors in data/fake_pod_*.csv")
    sys.exit(1 if n_fail else 0)


if __name__ == "__main__":
    main()
