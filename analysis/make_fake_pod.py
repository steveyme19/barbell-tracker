"""
Test bench for PodLink v1 (docs/ble.md): a simulated pod, a simulated radio link and the
phone's decoder (podlink.PodStream), run through the scenarios of docs/ble-plan.md step 2.

    python make_fake_pod.py            # runs every scenario, prints PASS / FAIL per scenario

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
Then the decoder's whole input (frames, commands, STOP, timer calls) is replayed into a fresh
decoder, which must give identical samples, gaps, stats, faults, events and command bytes. That
log is the test vector set for the JS port (step 3):
  data/fake_pod_frames.csv    scenario, step, kind, arrival_ms, payload
                              kind frame: feed payload (hex); cmd: command() must return payload
                              (hex, or "none"); stop: stop_command(); call: payload is
                              stop_timed_out=<0|1>, stop_wait_ms=<ms> or finish
  data/fake_pod_expected.csv  scenario, t, ax, ay, az, gx, gy, gz     (every emitted sample)
  data/fake_pod_gaps.csv      scenario, kind, n, idx, t0, t1, lower_bound
  data/fake_pod_stats.csv     scenario, every PodStream counter, faults (joined by |)
  data/fake_pod_events.csv    scenario, k, kind, info
(all fake_*.csv: gitignored, regenerate any time)
"""

import math
import random
import struct
import sys
from pathlib import Path

import podlink as pl

DATA = Path(__file__).parent / "data"
TPS = 32.768                      # ticks per ms
ANOMALIES = ("bad", "pre_info", "ignored", "stale", "stale_stops", "after_close", "dupes",
             "t_backwards", "pkt_seq_gaps", "pkt_seq_lost", "clock_clamped")
INDEX_KINDS = ("overflow", "resync", "unexplained", "stop-tail", "sleep-tail", "lost-tail")


def f32(x):
    """x as the float32 INFO carries it."""
    return struct.unpack("<f", struct.pack("<f", x))[0]


def raw_for(i):
    """Deterministic raw counts for sample i: gx gy gz ax ay az (int16)."""
    return (int(900 * math.sin(i / 37)), int(500 * math.cos(i / 53)), (i * 7) % 2000 - 1000,
            int(4000 * math.sin(i / 29)), int(3000 * math.cos(i / 41)), 4100 + (i % 11) - 5)


class Pod:
    """The pod's side of docs/ble.md: sampling, the ring, the sender, CTRL ops, sleep."""

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
        self.sleeping = False             # in the sleep prologue: CTRL is ignored
        # link / stream state
        self.connected = False
        self.mtu = 23
        self.data_on = False              # the phone subscribed to DATA (STATUS rides on it too)
        self.streaming = False
        self.hold = False
        self.cursor = 0
        self.cmd_seq = 0
        self.pkt_seq = 0
        self.resync = self.overflow = False
        self.lost = 0
        self.lost_mark = 0
        self.inflight = 0
        self.retry = None                 # a built DATA frame waiting for a free slot
        self.status_due = []              # STATUS frames to send before any further DATA
        self.next_status_wall = None
        self.drain = None                 # {"head", "deadline", "kind"} during STOP / sleep
        self.sleep_reason = 0
        self.connect_wall = 0.0
        self.cmd_log = []                 # (op, cursor, head) per STREAM command handled
        self.drain_heads = []             # (kind, head) per finished STOP / sleep drain
        self.status_heads = []            # head of every STATUS this boot built
        self.rewind = 0                   # test-only fault: resend this many samples on STREAM_FROM

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
    def n_max(self):
        me = min(self.mtu, 247)
        return min(16, max(0, (me - 3 - 14) // 14))

    def status_frame(self, extra_flags=0, head=None):
        flags = extra_flags
        if self.streaming:
            flags |= pl.S_STREAMING
        if self.connected and self.n_max() < 4:
            flags |= pl.S_MTU_TOO_SMALL
        h = self.head if head is None else head
        self.status_heads.append(h)
        return pl.build_status(flags, 3950, self.now_ticks(), h, self.lost, 0,
                               self.sleep_reason if extra_flags & pl.S_GOING_TO_SLEEP else 0,
                               self.n_max(), 32)

    def info_frame(self, boot_id=None, proto=1, serial=None):
        return pl.build_info(self.boot_id if boot_id is None else boot_id,
                             self.serial if serial is None else serial, self.ring_n,
                             wake=self.wake, accel_lsb=self.accel_lsb, gyro_lsb=self.gyro_lsb, proto=proto)

    def queue_status(self, extra_flags=0, head=None):
        self.status_due.append(self.status_frame(extra_flags, head))

    def notify(self, frame, is_data):
        """True if a notify slot was free (docs/ble.md 5: 3 in flight, never block). STATUS and
        DATA both go out on the DATA characteristic, so they arrive in the order sent."""
        if not self.data_on:
            return not is_data            # STATUS only updates the readable value, counts as sent
        if self.inflight >= 3:
            return False
        self.inflight += 1
        if self.b.drop_next_data and is_data:
            self.b.drop_next_data = False
            self.b.pod_sent(frame, lost=True)     # sent, then lost on the air (test-only fault)
            return True
        self.b.pod_sent(frame)
        return True

    def tx_complete(self):
        self.inflight = max(0, self.inflight - 1)

    def build_data(self):
        """Build the next DATA frame from the cursor, or None (docs/ble.md 3 and 5)."""
        nm = self.n_max()
        if nm < 4:
            return None
        if self.head - self.cursor > self.ring_n:                       # sampling lapped us
            lo = self.head - self.ring_n
            self.lost += max(0, lo - max(self.cursor, self.lost_mark))
            self.lost_mark = max(self.lost_mark, lo)
            self.cursor = lo
            self.overflow = True
        end = self.drain["head"] if self.drain else self.head
        pending = end - self.cursor
        if pending <= 0:
            return None
        age_ticks = self.now_ticks() - self.ticks[self.cursor]
        n = min(nm, pending)
        # always start a new frame at an IMU restart, and keep dt under 60000 ticks
        for k in range(1, n):
            if (self.cursor + k) in self.restarts or self.ticks[self.cursor + k] - self.ticks[self.cursor] > 60000:
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
        if self.cursor in self.restarts:
            flags |= pl.F_IMU_RESTART
        if self.drain and self.cursor + n == self.drain["head"]:
            flags |= pl.F_FINAL
        smp = [(self.ticks[i], self.raw(i)) for i in range(self.cursor, self.cursor + n)]
        frame = pl.build_data(self.pkt_seq, self.cursor, self.ticks[self.cursor], flags, self.cmd_seq, smp)
        return {"bytes": frame, "n": n}

    def send_pass(self):
        """One pass of loop(): STATUS first, then up to 4 DATA frames."""
        while self.status_due:
            if not self.notify(self.status_due[0], is_data=False):
                return
            self.status_due.pop(0)
        if not (self.streaming and self.connected):
            return
        for _ in range(4):
            if self.retry is None:
                self.retry = self.build_data()
            if self.retry is None:
                break
            if not self.notify(self.retry["bytes"], is_data=True):
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

    # ---- the pod's loop, every ms ----
    def step(self):
        self.produce()
        if self.connected:
            if not self.mtu_done and self.b.now - self.connect_wall >= 1000:
                self.mtu_done = True                      # the pod's own MTU exchange, once
                if self.mtu != self.b.mtu:
                    self.mtu = self.b.mtu
                    self.queue_status()                   # fresh STATUS: mtu_too_small cleared
            if self.next_status_wall is not None and self.b.now >= self.next_status_wall:
                self.next_status_wall += 1000
                self.queue_status()
            if self.drain and (self.b.now >= self.drain["deadline"] or self.n_max() < 4 or not self.data_on):
                self.finish_drain()       # the cap: the rest is the phone's stop-tail / sleep-tail
                if self.b.pod is not self:
                    return
            self.send_pass()

    # ---- connection ----
    def on_connect(self):
        self.connected = True
        self.connect_wall = self.b.now
        self.mtu = self.b.mtu if self.b.native_mtu else 23
        self.mtu_done = self.b.native_mtu
        self.data_on = False
        self.next_status_wall = self.b.now + 1000

    def on_disconnect(self):
        self.connected = False
        self.streaming = False
        self.inflight = 0
        self.retry = None
        self.status_due = []
        self.next_status_wall = None
        if self.drain and self.drain["kind"] == "sleep":
            self.drain = None
            self.b.pod_go_to_sleep_2()    # a disconnect mid sleep-drain: sleep without the STATUS
        self.drain = None

    def ctrl(self, b):
        if self.sleeping:
            return                        # the sleep prologue ignores CTRL
        op = b[0]
        if op in (pl.OP_STREAM_FROM, pl.OP_STREAM_LIVE):
            if op == pl.OP_STREAM_FROM:
                if len(b) != 10:
                    return
                _, seq, boot, frm = struct.unpack("<BBII", b)
                if boot != self.boot_id:
                    self.queue_status(pl.S_BOOT_MISMATCH)
                    return
                lo = self.head - self.ring_n if self.head > self.ring_n else 0
                cur = min(max(frm, lo), self.head)
                self.overflow = False
                if frm < lo:
                    self.lost += max(0, lo - max(frm, self.lost_mark))
                    self.lost_mark = max(self.lost_mark, lo)
                    self.overflow = True
            else:
                if len(b) != 4:
                    return
                _, seq, back = struct.unpack("<BBH", b)
                cur = self.head - min(back, self.head, self.ring_n)
                self.overflow = False
            self.drain = None                 # a STREAM command ends a STOP drain, no STATUS
            self.retry = None                 # an unsent built frame belonged to the old stream
            if op == pl.OP_STREAM_FROM and self.rewind:
                cur = max(0, cur - self.rewind)
            self.cursor, self.cmd_seq = cur, seq
            self.cmd_log.append((op, cur, self.head))
            self.resync = True
            self.streaming = self.hold = True
            self.queue_status()
        elif op == pl.OP_STOP:
            self.hold = False
            if not self.streaming:
                self.queue_status(pl.S_STREAM_STOPPED)
                self.drain_heads.append(("stop-idle", self.head))
                return
            backlog = self.head - self.cursor
            nm = max(self.n_max(), 1)
            self.drain = {"head": self.head, "kind": "stop",
                          "deadline": self.b.now + 1500 + backlog / (60 * nm) * 1000}
        elif op == pl.OP_SLEEP and len(b) == 2:
            self.b.pod_sleep(4)


class Bench:
    """One scenario: the pod, the link, the phone and the log."""

    def __init__(self, name, seed=1, mtu=247, native_mtu=True, lat=(7.5, 30.0), **pod_kw):
        self.name = name
        self.rng = random.Random(seed)
        self.now = 0.0
        self.mtu, self.native_mtu, self.lat = mtu, native_mtu, lat
        self.air = []                     # [deliver_ms, kind, frame] pod -> phone, FIFO
        self.up = []                      # (deliver_ms, bytes) phone -> pod CTRL writes
        self.last_deliver = 0.0
        self.drop_next_data = False
        self.jam_until = None             # a jammed link: frames wait (BLE retransmits), in order
        self.clock_step = 0.0             # added to every arrival time (a phone clock step)
        self.log = []                     # what the decoder saw, in order (for the replay)
        self.samples, self.events = [], []
        self.emits = []                   # (epoch, idx) per emitted sample, in order
        self.ep_pod = {}                  # epoch -> the pod (boot) it decodes
        self.ep_gaps = []                 # (epoch, gap) in order
        self.wb_obs = {}                  # epoch -> [arrival - t64 / 32.768] per STATUS it used
        self.stream = pl.PodStream(self.on_sample, self.on_event)
        self.pods = []                    # every boot, for the ground truth
        self.pod_kw = dict(pod_kw)
        self.serial = 0x5EED0001
        self.pod = self.new_pod(boot_id=0x1000 + seed, wake=0)
        self.connected = False
        self.stop_deadline = None
        self.note = None
        self.held_seen = False
        self.status_blind_until = None   # test-only: a receiver that misses STATUS frames until then

    def new_pod(self, boot_id, wake, tick_start=None):
        kw = dict(self.pod_kw)
        if tick_start is not None:
            kw["tick_start"] = tick_start
        p = Pod(self, boot_id, self.serial, self.now, wake=wake, **kw)
        self.pods.append(p)
        return p

    def on_sample(self, smp):
        self.samples.append(smp)
        self.emits.append((self.stream.ep, self.stream.ep.last_idx))

    def on_event(self, kind, info):
        if kind == "epoch":
            self.ep_pod[self.stream.ep] = self.pod
        if kind == "gap":
            self.ep_gaps.append((self.stream.ep, info))
        self.events.append((kind, info))

    # ---- the radio ----
    def latency(self):
        return self.lat[0] + self.rng.uniform(0, self.lat[1])

    def pod_sent(self, frame, lost=False):
        d = max(self.last_deliver, self.now + self.latency())
        self.last_deliver = d
        self.air.append([d, "lost" if lost else "frame", frame])

    def deliver(self):
        if self.jam_until is not None and self.now < self.jam_until:
            return                        # nothing gets through, nothing completes
        if self.jam_until is not None:
            self.jam_until = None
            for a in self.air:            # the backlog goes out from now on, in order
                a[0] = max(a[0], self.now)
        while self.air and self.air[0][0] <= self.now:
            d, kind, frame = self.air.pop(0)
            if self.pod and self.pod.connected:
                self.pod.tx_complete()
            if kind == "frame" and self.connected:
                if frame[0] == pl.T_STATUS and self.status_blind_until is not None and self.now < self.status_blind_until:
                    continue                  # test-only fault (connect sloppy=True)
                self.feed(frame, d)
        while self.up and self.up[0][0] <= self.now:
            _, b = self.up.pop(0)
            if self.pod and self.pod.connected:
                self.pod.ctrl(b)

    def feed(self, frame, arrival):
        arr = round(arrival + self.clock_step, 3)
        self.log.append(("frame", arr, frame.hex()))
        self.stream.frame(frame, arr)
        if self.stream.ep is not None and self.stream.ep.held:
            self.held_seen = True
        ep = self.stream.ep
        if frame[0] == pl.T_STATUS and ep is not None and ep.status is not None and ep.ref32 is not None \
                and not self.stream.ignoring and len(ep.win) and ep.win[-1] == arr - ep.status_t64 / 32.768:
            self.wb_obs.setdefault(ep, []).append(arr - ep.status_t64 / 32.768)

    # ---- the phone ----
    def write(self, b):
        self.up.append((self.now + self.latency(), b))

    def command(self):
        b = self.stream.command()
        self.log.append(("cmd", None, "none" if b is None else b.hex()))
        if b is not None:
            self.write(b)
        return b

    def connect(self, info=None, sloppy=False):
        """Connect and run the connection order (docs/ble.md 7): read INFO, read STATUS,
        subscribe to DATA (it carries STATUS too), write the STREAM command if command() gives one.
        sloppy (test-only): a phone that skips the STATUS read and whose stack misses the STATUS
        frames of the first 1.5 s, so DATA arrives before the epoch's first STATUS and must be
        held (the decoder's defensive path: the pod itself always sends a STATUS first)."""
        self.connected = True
        self.pod.on_connect()
        self.run(15)
        self.feed(info or self.pod.info_frame(), self.now)
        if not sloppy:
            status = self.pod.status_frame()  # a read: built now, arrives a radio latency later
            self.run(round(self.latency()))
            self.feed(status, self.now)
            self.run(15)
        else:
            self.status_blind_until = self.now + 1530
        self.run(15)
        self.pod.data_on = True
        # a reboot-recovery request from the STATUS read is answered by this command
        self.events = [e for e in self.events if e[0] != "need-command"]
        self.command()

    def disconnect(self):
        """Link loss: frames on the air are gone, the pod stops streaming (keeps the hold)."""
        self.connected = False
        self.air = []
        self.up = []
        self.jam_until = None
        if self.pod:
            self.pod.on_disconnect()

    def stop(self):
        b = self.stream.stop_command()
        self.log.append(("stop", None, b.hex()))
        wait = self.stream.stop_wait_ms()
        self.log.append(("call", None, f"stop_wait_ms={wait!r}"))
        self.write(b)
        self.stop_deadline = self.now + wait

    def finish(self):
        self.log.append(("call", None, "finish"))
        self.stream.finish()

    def check_stop_timer(self):
        if self.stop_deadline is not None:
            if self.stream.stop is None:
                self.stop_deadline = None             # stopped, or a STREAM command cancelled it
            elif self.now >= self.stop_deadline:
                r = self.stream.stop_timed_out()
                self.log.append(("call", None, f"stop_timed_out={int(r)}"))
                self.stop_deadline = None

    # ---- the pod sleeping and waking ----
    def pod_sleep(self, reason):
        """The sleep prologue (docs/ble.md 6): sampling stops, drain <= 1.5 s, STATUS, disconnect."""
        p = self.pod
        p.sampling = False
        p.sleeping = True
        p.sleep_reason = reason
        if p.streaming and p.connected:
            p.drain = {"head": p.head, "kind": "sleep", "deadline": self.now + 1500}
        else:
            self.pod_go_to_sleep_2()

    def pod_go_to_sleep_2(self):
        p = self.pod
        if p is None:
            return
        self.pod = None                   # asleep from here on
        if p.connected:
            # STATUS going_to_sleep waits for a free slot, then up to 300 ms for zero in flight
            p.status_due.append(p.status_frame(pl.S_GOING_TO_SLEEP, head=p.head))
            end = self.now + 300
            while self.now < end and (p.status_due or p.inflight or self.air):
                p.send_pass()
                self.now += 1.0
                self.pod = p
                self.deliver()
                self.pod = None
            self.connected = False
            self.air = []
            self.up = []
            p.connected = False

    def wake(self, boot_id, tick_start=0, **kw):
        """A wake is a reboot: new boot_id (or the same one, to test the defensive check)."""
        self.pod_kw.update(kw)
        self.pod = self.new_pod(boot_id=boot_id, wake=1, tick_start=tick_start)

    # ---- time ----
    def run(self, ms):
        end = self.now + ms
        while self.now < end:
            self.now += 1.0
            if self.pod:
                self.pod.step()
            self.deliver()
            self.check_stop_timer()
            if any(k == "need-command" for k, _ in self.events):
                self.events = [e for e in self.events if e[0] != "need-command"]
                if self.connected:
                    self.command()


# ---------------------------------------------------------------------------------------------
# checks

def check(b, expect):
    """Compare a finished bench with what the scenario expects. Returns failure strings."""
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
        left = (ep, g["idx"] - 1) if g["kind"] in INDEX_KINDS else (ep, g["idx"] - 1)
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


def replay(log):
    """Feed a scenario's log into a fresh decoder. Returns (samples, stream, events) or raises."""
    samples, events = [], []
    s = pl.PodStream(samples.append, lambda k, i: events.append((k, i)))
    for kind, arr, payload in log:
        if kind == "frame":
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


# ---------------------------------------------------------------------------------------------
# scenarios (docs/ble-plan.md step 2 and section 0)

def first_cmd(pod, k=0):
    return pod.cmd_log[k][1]


def last_drain(pod):
    return pod.drain_heads[-1][1]


def end_known(b, ep):
    """The end of an epoch the phone can know after an abrupt end: the larger of the last head
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


def sc_clean():
    b = Bench("clean", seed=1)
    b.pods[0].pkt_seq = 0xFFF0            # also crosses the 16-bit pkt_seq wrap
    b.run(3000); b.connect(); b.run(20000); b.stop(); b.run(3000); b.finish()
    p = b.pods[0]; ep = b.stream.epochs[0]
    crossed = lambda: None if p.pkt_seq < 0xFFF0 else "pkt_seq never wrapped"
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
    b.pods[0].rewind = 50                 # the pod resends 50 samples it already sent
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
    no_resync = lambda: None if any(g["kind"] == "overflow" for g in b.stream.gaps) else "no overflow gap"
    return b, {"ranges": [(ep, first_cmd(p), last_drain(p))],
               "gaps": {"overflow": p.lost}, "stats": {"stopped": 1}, "extra": {"lap": no_resync}}


def sc_pkt_hole():
    b = Bench("pkt-seq-hole", seed=4)
    b.run(3000); b.connect(); b.run(5000)
    b.drop_next_data = True               # one DATA frame lost on the air (test-only fault)
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
    b = Bench("sleep-drain-cut", seed=7, mtu=73)          # 4 samples per frame: a slow drain
    b.run(3000); b.connect(); b.run(5000)
    b.disconnect(); b.run(30000)          # 30 s of backlog, still inside the ring
    b.connect(); b.run(200)
    b.pod_sleep(4)                        # sleep right away: the 1.5 s drain can't finish
    b.run(3000); b.finish()
    p = b.pods[0]; ep = b.stream.epochs[0]
    tail = lambda got: len(got) == 1 and got[0] > 0
    return b, {"ranges": [(ep, first_cmd(p), p.drain_heads[-1][1])], "gaps": {"sleep-tail": tail},
               "stats": {"reconnects": 1, "stopped": 1}}


def sc_imu_restart():
    b = Bench("imu-restart-and-miss", seed=8, mtu=73)
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
    b.pods[0].next_status_wall = b.now + 2   # a periodic STATUS goes out just before the answer
    b.run(10000); b.stop(); b.run(3000); b.finish()
    p = b.pods[0]; ep = b.stream.epochs[0]
    return b, {"ranges": [(ep, first_cmd(p), last_drain(p))], "stats": {"stopped": 1}}


def sc_pod_mtu():
    b = Bench("pod-mtu-exchange", seed=20, native_mtu=False)   # Chrome on Android: the pod asks
    b.run(3000); b.connect(); b.run(8000); b.stop(); b.run(3000); b.finish()
    p = b.pods[0]; ep = b.stream.epochs[0]
    seen = lambda: None if any(k == "mtu-too-small" for k, _ in b.events) else "no mtu-too-small before the exchange"
    return b, {"ranges": [(ep, first_cmd(p), last_drain(p))], "stats": {"stopped": 1}, "extra": {"mtu": seen}}


def sc_mtu_small_stop():
    b = Bench("mtu-too-small-stop", seed=12, mtu=23, native_mtu=False)
    b.run(3000); b.connect(); b.run(5000); b.stop(); b.run(4000); b.finish()
    flag = lambda: None if any(k == "mtu-too-small" for k, _ in b.events) else "no mtu-too-small"
    return b, {"no_ranges_ok": True, "stats": {"samples": 0, "stopped": 1}, "extra": {"mtu event": flag}}


def sc_stop_exact_tail():
    # a slow link (40-80 ms, ~50 frames/s of 4 samples) against a cap that assumes 60 frames/s
    b = Bench("stop-capped-tail", seed=21, mtu=73, lat=(40.0, 40.0))
    b.run(3000); b.connect(); b.run(4000)
    b.disconnect(); b.run(30000)
    b.connect(); b.run(300)
    b.stop()
    while b.stream.stats["stopped"] < 1:  # until the capped stop is answered
        b.run(100)
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
               "gaps": {"stop-tail": lambda got: len(got) == 1 and got[0] > 0},
               "stats": {"reconnects": 1, "stopped": 2}}


def sc_stop_then_start():
    b = Bench("stop-then-start-mid-drain", seed=22, mtu=73)
    b.run(3000); b.connect(); b.run(4000)
    b.disconnect(); b.run(20000)
    b.connect(); b.run(300)
    b.stop(); b.run(200)                  # the drain is still running...
    b.command()                           # ...and the user hits Start again
    b.run(15000); b.stop(); b.run(4000); b.finish()
    p = b.pods[0]; ep = b.stream.epochs[0]
    return b, {"ranges": [(ep, first_cmd(p), last_drain(p))],
               "stats": {"reconnects": 1, "stopped": 1, "stale": lambda v: v >= 0}}


def sc_stop_then_start_late():
    b = Bench("stop-then-start-after-drain", seed=23)
    b.run(3000); b.connect(); b.run(6000)
    b.stop()
    while not b.pods[0].drain_heads:      # the pod has finished the drain and queued stopped...
        b.run(1)
    b.command()                           # ...and Start goes out before that STATUS arrives
    b.run(8000); b.stop(); b.run(3000); b.finish()
    p = b.pods[0]; ep = b.stream.epochs[0]
    return b, {"ranges": [(ep, first_cmd(p), last_drain(p))],
               "stats": {"stale_stops": lambda v: v <= 1, "stopped": lambda v: v in (1, 2),
                         "stale": lambda v: v >= 0}}


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
               "gaps": {"lost-tail": lambda got: len(got) == 1 and got[0] > 0},
               "stats": {"stale": lambda v: v >= 0, "stale_stops": lambda v: v <= 1, "reconnects": 1},
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
    b = Bench("stop-timeout-gives-up", seed=24, mtu=73)
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
               "gaps": {"stop-tail": lambda got: len(got) == 1 and got[0] > 0},
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
    b = Bench("reboot-mid-catch-up", seed=30, mtu=73)
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
               "true_loss": true_end - nxt, "gaps": {"lost-tail": lambda got: len(got) == 1 and got[0] > 0},
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
               "stats": {"epochs": 2, "reconnects": 1, "stopped": 1, "stale": lambda v: v >= 0},
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
        b.feed(b.pod.info_frame(), b.now); b.run(15)
        status = b.pod.status_frame(); b.run(20); b.feed(status, b.now); b.run(15)
        b.command()
    b.run(6000); b.stop(); b.run(3000); b.finish()
    p1, p2 = b.pods
    e1, e2 = b.stream.epochs
    return b, {"ranges": [(e1, first_cmd(p1), end_known(b, e1)), (e2, 0, last_drain(p2))],
               "true_loss": true_end - nxt, "gaps": {"lost-tail": lambda got: len(got) <= 1},
               "stats": {"epochs": 2, "reconnects": 1, "stopped": 1, "ignored": lambda v: v >= 0},
               "faults": ["boot mismatch"]}


def sc_other_pod():
    b = Bench("other-pod-and-bad-info", seed=25)
    b.run(3000); b.connect(); b.run(4000)
    b.disconnect(); b.run(500)
    # the phone briefly connects to a neighbour's pod, then reads an INFO from the future (proto 2)
    b.connected = True
    b.feed(b.pod.info_frame(serial=0xBEEF), b.now)
    b.feed(b.pod.status_frame(), b.now)
    none1 = b.command()
    b.feed(b.pod.info_frame(proto=2), b.now)
    none2 = b.command()
    b.connected = False
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
    last = next(f for k, a, f in reversed(b.log) if k == "frame" and f.startswith("01"))
    b.feed(bytes.fromhex(last), b.now)    # a pod bug: a DATA frame after stream_stopped
    b.feed(pl.build_cal(), b.now)         # CAL: ignored in v1, not 'bad'
    b.feed(bytes([pl.T_DATA, 2, 0, 0]), b.now)          # truncated DATA
    b.feed(bytes([0x55]) + bytes(19), b.now)            # unknown type
    b.finish()
    p = b.pods[0]; ep = b.stream.epochs[0]
    return b, {"ranges": [(ep, first_cmd(p), last_drain(p))],
               "stats": {"after_close": 1, "bad": 2, "stopped": 1}}


def sc_round_tie():
    """A crafted 1024-tick hole: 1024 x 208 / 32768 = 6.5 exactly, so n = 6 (rounds .5 up)."""
    b = Bench("hole-rounding-tie", seed=29)
    b.connected = True
    b.feed(b.pod.info_frame(), 0.0)
    b.feed(pl.build_status(0, 3900, 1000, 0, 0, 0, 0, 16, 32), 5.0)
    cmd = b.command()
    seq = cmd[1]
    p = b.pods[0]
    p.ticks = [1000, 1158, 1158 + 1024]   # the pod's truth for the 3 crafted samples
    smp = [(t, p.raw(i)) for i, t in enumerate(p.ticks)]
    b.feed(pl.build_data(0, 0, 1000, pl.F_RESYNC, seq, smp), 20.0)
    six = lambda got: got == [6]
    b.finish()
    return b, {"no_ranges_ok": True, "gaps": {"imu": six}}


SCENARIOS = [sc_clean, sc_scale, sc_reconnect_rewind, sc_overflow, sc_lap, sc_pkt_hole, sc_tick_wrap,
             sc_sleep_wake, sc_sleep_cut, sc_imu_restart, sc_wake_pickup, sc_stale, sc_status_race,
             sc_pod_mtu, sc_mtu_small_stop, sc_stop_exact_tail, sc_stop_then_start,
             sc_stop_then_start_late, sc_stop_timeout_late, sc_stop_start_lost, sc_window_then_wake,
             sc_stop_timeout_finish, sc_lost_tail,
             sc_lost_tail_catchup,
             sc_same_boot_id, sc_boot_mismatch, sc_other_pod, sc_held_data, sc_clock_step,
             sc_after_close, sc_round_tie]


def run_all(write=True, quiet=False):
    """Run every scenario. Returns the number of failed scenarios."""
    if write:
        DATA.mkdir(exist_ok=True)
        files = {k: open(DATA / f"fake_pod_{k}.csv", "w", newline="")
                 for k in ("frames", "expected", "gaps", "stats", "events")}
        stat_keys = list(pl.PodStream(lambda s: None).stats)
        files["frames"].write("scenario,step,kind,arrival_ms,payload\n")
        files["expected"].write("scenario,t,ax,ay,az,gx,gy,gz\n")
        files["gaps"].write("scenario,kind,n,idx,t0,t1,lower_bound\n")
        files["stats"].write("scenario," + ",".join(stat_keys) + ",faults\n")
        files["events"].write("scenario,k,kind,info\n")
    n_fail = 0
    for sc in SCENARIOS:
        b, expect = sc()
        fails = check(b, expect)
        try:                              # the replay must reproduce everything from the log alone
            rs, rst, rev = replay(b.log)
            if rs != b.samples:
                fails.append("replay: samples differ")
            if rst.gaps != b.stream.gaps or rst.stats != b.stream.stats or rst.faults != b.stream.faults:
                fails.append("replay: gaps / stats / faults differ")
            if [k for k, _ in rev] != [k for k, _ in b.events if k != "need-command"] and \
                    [k for k, _ in rev if k != "need-command"] != [k for k, _ in b.events if k != "need-command"]:
                fails.append("replay: events differ")
        except AssertionError as e:
            fails.append(f"replay: {e}")
        n_fail += bool(fails)
        if not quiet:
            gaps = ", ".join(f"{g['kind']} {g['n']}" for g in b.stream.gaps) or "no gaps"
            note = f"  ({b.note})" if b.note else ""
            print(f"{'PASS' if not fails else 'FAIL'}  {b.name:28} {len(b.samples):6} samples, {gaps}{note}")
            for f in fails:
                print(f"      - {f}")
        if write:
            for k, (kind, arr, payload) in enumerate(b.log):
                files["frames"].write(f"{b.name},{k},{kind},{'' if arr is None else arr},{payload}\n")
            for s in b.samples:
                files["expected"].write(f"{b.name},{s['t']!r},{s['ax']!r},{s['ay']!r},{s['az']!r},"
                                        f"{s['gx']!r},{s['gy']!r},{s['gz']!r}\n")
            for g in b.stream.gaps:
                files["gaps"].write(f"{b.name},{g['kind']},{g['n']},{g['idx']},{g['t0']!r},{g['t1']!r},"
                                    f"{int(bool(g.get('lower_bound')))}\n")
            files["stats"].write(b.name + "," + ",".join(str(b.stream.stats[k]) for k in stat_keys)
                                 + "," + "|".join(b.stream.faults) + "\n")
            for k, (kind, info) in enumerate(rev):
                files["events"].write(f"{b.name},{k},{kind},{'' if info is None or isinstance(info, dict) else info}\n")
    if write:
        for f in files.values():
            f.close()
    return n_fail


def main():
    n_fail = run_all()
    print(f"\n{len(SCENARIOS) - n_fail}/{len(SCENARIOS)} scenarios PASS. Vectors in data/fake_pod_*.csv")
    sys.exit(1 if n_fail else 0)


if __name__ == "__main__":
    main()
