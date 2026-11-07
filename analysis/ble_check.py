"""
Step 5 bench check, on CRP since D45: the pod's Bluetooth link from the laptop (bleak), against
docs/ble.md 2 to 7. Needs bleak once (decision in docs/ble-plan.md):  .venv\\Scripts\\pip install bleak

    python ble_check.py                      # find a BarPod, run every check (~60 s)
    python ble_check.py --seconds=600        # stream 10 min (run log_serial.py on USB alongside)
    python ble_check.py --name=BarPod-1A2B   # pick one pod when several are near
    python ble_check.py --battery            # the pod is on battery: expect the USB flag off

Checks, in order:
  1. the advertisement: name BarPod-XXXX with XXXX the low 16 bits of the address, a static random
     address (top two bits 11), the CRP service UUID 5090
  2. the GATT table: service 5090 with exactly RX 50b0 (write, write without response) and TX 50b1
     (notify); no PodLink v1 service left
  3. before a HELLO the pod sends nothing and ignores a READ; then HELLO: ver 0x0B, max_channels 1,
     max_chunk 244, flags push, device_id "BPOD"
  4. reads: INFO (a BUSY before the pod's own MTU exchange is retried), CAL, STATUS (plain), PROTO,
     PUSH (0), KIND (1), MTU and N_MAX; and the answers to wrong requests: a READ of DATA and a
     write to INFO ACCESS_DENIED, an unknown register UNKNOWN_REG, PUSH = 2 and bad CTRL ops
     INVALID_VALUE, BENCH (USB only) UNKNOWN_REG
  5. discovery: QUERY_DEVICE (3 groups, the name), QUERY_GROUP and a QUERY_REG walk give every
     register name of groups 1 to 3, the bench group is empty; the state mirrors agree with INFO
  6. PUSH = 0: no push at all, only the engine's 1 s NOP; PUSH = 1: STATUS pushed at 1 Hz (USB
     flag, n_per_packet, no mtu_too_small, head rising)
  7. a STREAM_FROM with the wrong boot_id: answered OK, one boot_mismatch STATUS and no DATA; a
     STATUS read right after it is plain
  8. the connection order and STREAM_LIVE (back 600) through the reference decoder (PodStream): the
     command's RESPONSE, then the STATUS answering it, then the first DATA (RESYNC, its cmd_seq);
     no gaps, no faults, pkt_seq continuous, 209 to 212.5 Hz, |a| 9.3 to 10.4, ~13 frames/s at 16
  9. disconnect 3 s, reconnect: a too-old HELLO (0x0A) gets BYE, then the connection order and
     STREAM_FROM(next index): nothing lost (the ring covers it)
 10. STOP: a stream_stopped STATUS whose head = the last sample + 1, then no more DATA; after BYE
     the pod sends nothing

log_ble_sim.py runs sections 3 to 10 against make_fake_pod's simulated pod (no hardware), with
discovery answered from podcrp.c's register table and pinned to host_test.c's engine vectors.
"""

import asyncio
import statistics
import struct
import sys
import time

import crp
import podlink as pl

V1_SERVICE = "9ba40001-6de5-429c-b225-8134b9953e9d"   # PodLink v1's table: gone since D45
EVENT_BITS = pl.S_BOOT_MISMATCH | pl.S_STREAM_STOPPED | pl.S_GOING_TO_SLEEP
BUSY_RETRY_S, BUSY_FOR_S = 0.2, 3.0    # a BUSY read is retried every 200 ms for 3 s (ble.md 5)
REG_BOOT_ID, REG_SERIAL, REG_FW_BUILD, REG_HEAD, REG_NAME = 0x00020005, 0x00020006, 0x00020007, 0x00020003, 0x0002000E
# what discovery must find (docs/ble.md 2.3; a group's base, offset 0, isn't counted or walked)
GROUPS = {
    0x00010000: ["INFO", "STATUS", "CAL", "DATA", "CTRL", "PUSH"],
    0x00020000: ["BATTERY_MV", "FLAGS", "HEAD", "TICKS", "BOOT_ID", "SERIAL", "FW_BUILD", "WAKE_REASON",
                 "SLEEP_REASON", "LOST", "MISSED_DRDY", "BATT_V", "RESETREAS", "NAME"],
    0x00030000: ["KIND", "MTU", "N_MAX", "INTERVAL", "PHY", "DL", "FRAMES", "TX_DROPS", "RX_DROPS", "STACK_FREE"],
    0x00040000: [],                                   # bench: USB only
}


def status_name(st):
    return crp.STATUS_NAMES.get(st, f"0x{st:02x}")


class Report:
    def __init__(self, say=None):
        self.ok = True
        self.say = say or (lambda msg: print(msg, flush=True))

    def check(self, good, msg):
        self.say(("PASS  " if good else "FAIL  ") + msg)
        self.ok = self.ok and bool(good)

    def note(self, msg):
        self.say("      " + msg)


class Pod:
    """One connection: a link (crp.BleakLink, or a fake in the selftest), its CRP Session, and every
    packet the pod sent, logged in arrival order as (ms on our clock, packet, parsed, frame). While a
    PodStream is attached, the tap feeds it every packet first (docs/ble.md 7)."""

    def __init__(self, link, clock):
        self.link = link
        self.clock = clock
        self.log = []
        self.stream = None
        self.session = crp.Session(link, tap=self._tap)   # before open: nothing goes untapped

    def _tap(self, pkt, ms):
        self.log.append((self.clock() * 1000, pkt, crp.parse(pkt), pl.crp_frame(pkt)))
        if self.stream is not None:
            self.stream.packet(pkt, ms)

    def ms(self):
        return self.clock() * 1000

    def pushes(self, reg=None, since=0):
        """(index, ms, decoded frame) of every push (WRITE txn 0) from log index `since` on."""
        out = []
        for i, (t, _, p, f) in enumerate(self.log[since:], since):
            if p and p["type"] == crp.WRITE and p["txn"] == 0 and (reg is None or p["reg"] == reg):
                out.append((i, t, pl.decode_frame(f) if f is not None else None))
        return out

    def statuses(self, since=0):
        return [(i, t, d) for i, t, d in self.pushes(pl.REG_STATUS, since) if d is not None]

    def datas(self, since=0):
        return [(i, t, d) for i, t, d in self.pushes(pl.REG_DATA, since) if d is not None]

    async def request(self, build, timeout=None):
        """(status, value) of any client request: build(txn) -> packet. The Session's own request
        path (its txn ids, its timeout), for the discovery queries it has no method for."""
        return await self.session._request(build, timeout)

    async def read_retry(self, reg):
        """(status, value, BUSY answers) of a READ with the BUSY retry (docs/ble.md 5)."""
        t0, busy = self.clock(), 0
        while True:
            st, val = await self.session.read(reg)
            if st != crp.BUSY or self.clock() - t0 + BUSY_RETRY_S > BUSY_FOR_S:
                return st, val, busy + (st == crp.BUSY)
            busy += 1
            await asyncio.sleep(BUSY_RETRY_S)

    async def frame(self, reg):
        st, val, _ = await self.read_retry(reg)
        return pl.decode_frame(val) if st == crp.OK else None

    async def u(self, reg, fmt):
        st, val = await self.session.read(reg)
        return struct.unpack(fmt, val)[0] if st == crp.OK and len(val) == struct.calcsize(fmt) else None


async def open_pod(new_link, clock):
    pod = Pod(await new_link(), clock)
    await pod.link.open()
    return pod


async def protocol(r, new_link, clock, seconds=15.0, on_usb=True, gravity=(9.3, 10.4), discovery=True,
                   name=None, after_open=None):
    """Sections 3, 4 and 6 to 10 (and 5 with discovery): everything from a link to the pod on. new_link()
    gives a fresh, unopened link per connection; clock() is seconds. after_open(pod) runs once the
    first link is up (the GATT table check). gravity=None skips the |a| check (a simulated pod)."""
    sleep = asyncio.sleep
    pod = await open_pod(new_link, clock)
    if after_open is not None:
        await after_open(pod)
    s = pod.session

    # ---- 3. nothing before a HELLO, then the HELLO ----
    await sleep(0.5)
    quiet = len(pod.log)
    try:
        await s.read(pl.REG_INFO, timeout=0.5)
        ignored = False
    except crp.CrpTimeout:
        ignored = True
    r.check(quiet == 0 and ignored and len(pod.log) == 0,
            f"before a HELLO: {quiet} packets in 0.5 s, a READ {'ignored' if ignored else 'answered'} (want nothing, ignored)")
    peer = await s.hello()
    r.check(peer["ver"] == crp.VERSION and peer["max_ch"] == 1 and peer["max_chunk"] == 244
            and peer["flags"] == crp.FLAG_PUSH and peer["device_id"] == pl.DEVICE_ID,
            f"HELLO: ver 0x{peer['ver']:02x}, max_channels {peer['max_ch']}, max_chunk {peer['max_chunk']}, "
            f"flags 0x{peer['flags']:02x}, device_id 0x{peer['device_id']:08x} (want 0x0b, 1, 244, 0x01, 0x444f5042 BPOD)")

    # ---- 4. reads, and the answers to wrong requests ----
    st, info_b, busy = await pod.read_retry(pl.REG_INFO)
    info = pl.decode_frame(info_b) if st == crp.OK else None
    r.check(info is not None and info_b[:2] == b"\x20\x01",
            f"INFO: {status_name(st)}, {len(info_b)} B starting {info_b[:2].hex(' ')} "
            f"({busy} BUSY before the pod's MTU exchange)")
    if info is None:
        return r
    good = (info["boot_id"] != 0 and info["tick_hz"] == 32768 and info["odr"] == 208
            and abs(info["accel_lsb"] - pl.ACCEL_LSB) < 1e-9 and abs(info["gyro_lsb"] - pl.GYRO_LSB) < 1e-9)
    r.check(good, f"INFO: boot_id 0x{info['boot_id']:08x}, fw {info['fw_build']}, serial 0x{info['serial']:08x}, "
                  f"ring_n {info['ring_n']}, odr {info['odr']}, wake {info['wake']}")
    st, cal_b = await s.read(pl.REG_CAL)
    r.check(st == crp.OK and len(cal_b) == 64 and cal_b[:2] == b"\x30\x00" and not any(cal_b[2:]),
            f"CAL: {status_name(st)}, {len(cal_b)} B, stub 30 00")
    st_r = await pod.frame(pl.REG_STATUS)
    r.check(st_r is not None and not st_r["flags"] & EVENT_BITS,
            f"STATUS read: {'decodes' if st_r else 'bad'}, flags 0x{st_r['flags'] if st_r else 0:02x} (no event bits)")
    proto, push, kind = await pod.u(pl.REG_PROTO, "<B"), await pod.u(pl.REG_PUSH, "<B"), await pod.u(pl.REG_LINK_KIND, "<B")
    mtu, n_max = await pod.u(pl.REG_LINK_MTU, "<H"), await pod.u(pl.REG_LINK_N_MAX, "<B")
    r.check(proto == 1 and push == 0 and kind == 1, f"PROTO {proto}, PUSH {push}, KIND {kind} (want 1, 0, 1)")
    want_n = None if mtu is None else max(0, min(16, (mtu - 23) // 14))
    r.check(mtu is not None and 79 <= mtu <= 247 and n_max == want_n,
            f"MTU {mtu} (the link's ATT MTU, clamped to 247), N_MAX {n_max} (want {want_n})")
    if mtu != 247:
        r.note(f"MTU {mtu}, not 247: fewer samples per frame than a phone gets")
    wrong = [   # lambdas: each request goes out only when its turn comes
        ("a READ of DATA", lambda: s.read(pl.REG_DATA), crp.ACCESS_DENIED),
        ("a READ of an unknown register", lambda: s.read(0x000100EE), crp.UNKNOWN_REG),
        ("a write to INFO", lambda: s.write(pl.REG_INFO, bytes(32)), crp.ACCESS_DENIED),
        ("PUSH = 2", lambda: s.write(pl.REG_PUSH, b"\x02"), crp.INVALID_VALUE),
        ("CTRL op 0x7f", lambda: s.write(pl.REG_CTRL, b"\x7f"), crp.INVALID_VALUE),
        ("CTRL STOP with a stray byte", lambda: s.write(pl.REG_CTRL, b"\x03\x00"), crp.INVALID_VALUE),
        # 'x' isn't a bench character: harmless even if a build wrongly took BENCH on Bluetooth
        ("BENCH 'x' (USB only)", lambda: s.write(pl.REG_BENCH, b"x"), crp.UNKNOWN_REG),
    ]
    for what, req, want in wrong:
        got = await req()
        got = got[0] if isinstance(got, tuple) else got
        r.check(got == want, f"{what}: {status_name(got)} (want {status_name(want)})")

    # ---- 5. discovery (Ansync's dashboard lists the registers this way) ----
    if discovery:
        st, v = await pod.request(lambda txn: crp.query_device(txn, 0))
        groups = struct.unpack_from("<I", v, 5)[0] if st == crp.OK and len(v) >= 9 else None
        dev_name = v[9:].decode(errors="replace") if st == crp.OK else None
        r.check(groups == 3 and (name is None or dev_name == name),
                f"QUERY_DEVICE: {groups} groups, name {dev_name!r} (want 3{', ' + repr(name) if name else ''})")
        for base, want in GROUPS.items():
            st, v = await pod.request(lambda txn: crp.query_group(txn, base))
            count = struct.unpack_from("<I", v, 5)[0] if st == crp.OK and len(v) >= 9 else None
            names, off = [], 1
            while count and len(names) < count and off < 64:
                st, v = await pod.request(lambda txn: crp.query_reg(txn, base + off))
                if st == crp.OK and len(v) >= 12:
                    names.append(v[12:12 + v[11]].decode(errors="replace"))
                off += 1
            r.check(count == len(want) and names == want,
                    f"group 0x{base:08x}: count {count}, names {names} (want {len(want)})")
        mirrors = {"BOOT_ID": (await pod.u(REG_BOOT_ID, "<I"), info["boot_id"]),
                   "SERIAL": (await pod.u(REG_SERIAL, "<I"), info["serial"]),
                   "FW_BUILD": (await pod.u(REG_FW_BUILD, "<H"), info["fw_build"])}
        head = await pod.u(REG_HEAD, "<I")
        st, nm = await s.read(REG_NAME)
        nm = nm.rstrip(b"\x00").decode(errors="replace") if st == crp.OK else None
        r.check(all(a == b for a, b in mirrors.values()) and head is not None and st_r and head >= st_r["head"]
                and (name is None or nm == name),
                f"state mirrors: {', '.join(f'{k} {a}' for k, (a, _) in mirrors.items())}, HEAD {head}, NAME {nm!r} "
                f"(INFO and the STATUS read agree)")

    # ---- 6. PUSH = 0 sends nothing, PUSH = 1 the STATUS at 1 Hz ----
    i0 = len(pod.log)
    await sleep(2.5)
    pushed = pod.pushes(since=i0)
    nops = sum(1 for _, pkt, _, _ in pod.log[i0:] if pkt == crp.nop())
    r.check(not pushed and nops >= 1, f"PUSH 0 for 2.5 s: {len(pushed)} pushes, {nops} NOPs (the engine's 1 s heartbeat)")
    st = await s.write(pl.REG_PUSH, b"\x01")
    r.check(st == crp.OK and await pod.u(pl.REG_PUSH, "<B") == 1, f"PUSH = 1: {status_name(st)}, reads back 1")
    i0 = len(pod.log)
    await sleep(4.2)
    sts = pod.statuses(i0)
    gaps = [b[1] - a[1] for a, b in zip(sts, sts[1:])]
    r.check(len(sts) >= 4 and max(gaps, default=9e9) <= 1250,
            f"{len(sts)} STATUS pushes in 4.2 s, spacing {[round(g) for g in gaps]} ms")
    if sts:
        last = sts[-1][2]
        usb = bool(last["flags"] & pl.S_USB)
        r.check(usb == on_usb, f"USB flag {usb} (expect {on_usb}), battery {last['battery_mv']} mV")
        r.check(not last["flags"] & pl.S_MTU_TOO_SMALL and last["n_per_packet"] >= 4,
                f"n_per_packet {last['n_per_packet']}, mtu_too_small clear, interval {last['interval'] * 1.25:.2f} ms")
        heads = [d["head"] for _, _, d in sts]
        r.check(heads == sorted(heads) and heads[-1] > heads[0], f"head rises {heads[0]} -> {heads[-1]}")

    # ---- 7. boot_mismatch ----
    i0 = len(pod.log)
    t0 = pod.ms()
    bad = (info["boot_id"] ^ 0x5A5A5A5A) or 1
    st = await s.write(pl.REG_CTRL, struct.pack("<BBII", pl.OP_STREAM_FROM, 0x77, bad, 0))
    while pod.ms() - t0 < 1200 and not any(d["flags"] & pl.S_BOOT_MISMATCH for _, _, d in pod.statuses(i0)):
        await sleep(0.01)
    rd = await pod.frame(pl.REG_STATUS)                   # read right after it
    r.check(st == crp.OK and rd is not None and not rd["flags"] & EVENT_BITS,
            f"STREAM_FROM, wrong boot_id: {status_name(st)} (taken); a STATUS read right after it is plain "
            f"(flags 0x{rd['flags'] if rd else 0:02x})")
    await sleep(max(0.0, 1.2 - (pod.ms() - t0) / 1000))
    mism = [d for _, _, d in pod.statuses(i0) if d["flags"] & pl.S_BOOT_MISMATCH]
    r.check(len(mism) == 1 and not mism[0]["flags"] & pl.S_STREAMING and not pod.datas(i0),
            f"wrong boot_id: {len(mism)} boot_mismatch STATUS, no stream, {len(pod.datas(i0))} DATA")

    # ---- 8. the connection order and STREAM_LIVE through the reference decoder ----
    samples, events = [], []
    dec = pl.PodStream(samples.append, lambda k, x: events.append((pod.ms(), k, x)))
    st = await s.write(pl.REG_PUSH, b"\x00")              # the order (ble.md 7): reads first, then PUSH = 1
    pod.stream = dec
    await pod.read_retry(pl.REG_INFO)
    await pod.read_retry(pl.REG_STATUS)
    st1 = await s.write(pl.REG_PUSH, b"\x01")
    cmd = dec.command()
    r.check(st == crp.OK and st1 == crp.OK and cmd is not None and cmd[0] == pl.OP_STREAM_LIVE,
            f"the connection order: PUSH 0, INFO, STATUS, PUSH 1; PodStream's first command: {cmd.hex(' ') if cmd else None}")
    i_cmd = len(pod.log)
    t_cmd = pod.ms()
    st = await s.write(pl.REG_CTRL, cmd)
    r.check(st == crp.OK, f"STREAM_LIVE: {status_name(st)}")
    r.note(f"streaming {seconds:.0f} s ...")
    t_end = t_cmd + seconds * 1000
    while pod.ms() < t_end:
        await sleep(min(30.0, (t_end - pod.ms()) / 1000))
        if seconds > 60:
            r.note(f"{(pod.ms() - t_cmd) / 1000:.0f} s: {len(samples)} samples, gaps {len(dec.gaps)}, faults {len(dec.faults)}")
    data = pod.datas(i_cmd)
    i_resp = next((i for i, (_, _, p, _) in enumerate(pod.log[i_cmd:], i_cmd)
                   if p and p["type"] == crp.RESPONSE and p["reg"] == pl.REG_CTRL), None)
    i_st = next((i for i, _, d in pod.statuses(i_cmd) if d["flags"] & pl.S_STREAMING), None)
    r.check(data and data[0][2]["flags"] & pl.F_RESYNC and data[0][2]["cmd_seq"] == cmd[1],
            f"first DATA: RESYNC, cmd_seq {data[0][2]['cmd_seq'] if data else None} (want {cmd[1]})")
    r.check(i_resp is not None and i_st is not None and data and i_resp < i_st < data[0][0],
            f"in arrival order: the command's RESPONSE (packet {i_resp}), the STATUS answering it ({i_st}), "
            f"the first DATA ({data[0][0] if data else None})")
    span = (data[-1][1] - data[0][1]) / 1000 if len(data) > 1 else 0
    sizes = {}
    for _, _, f in data:
        sizes[14 + 14 * f["n"]] = sizes.get(14 + 14 * f["n"], 0) + 1
    r.check(len(data) > 0 and span > 0, f"{len(data)} DATA pushes in {span:.1f} s = {len(data) / max(span, 1e-9):.1f}/s, "
                                         f"frame sizes {dict(sorted(sizes.items()))}")
    ts = [x["t"] for x in samples]
    rate = (len(ts) - 1) / (ts[-1] - ts[0]) if len(ts) > 1 else 0
    r.check(209 <= rate <= 212.5, f"{len(samples)} samples, {rate:.2f} Hz from the pod's ticks")
    if gravity is not None:
        am = statistics.median([(x["ax"] ** 2 + x["ay"] ** 2 + x["az"] ** 2) ** 0.5 for x in samples]) if samples else 0
        r.check(gravity[0] <= am <= gravity[1], f"median |a| {am:.3f} m/s^2 (raw pod, no calibration)")
    r.check(not dec.gaps and not dec.faults and dec.stats["pkt_seq_gaps"] == 0 and dec.stats["pre_info"] == 0,
            f"decoder: gaps {dec.gaps}, faults {dec.faults}, pkt_seq gaps {dec.stats['pkt_seq_gaps']}, "
            f"pre_info {dec.stats['pre_info']}, dupes {dec.stats['dupes']}, stale {dec.stats['stale']}")

    # ---- 9. reconnect: a too-old HELLO, then the connection order and STREAM_FROM ----
    n_before = len(samples)
    pod.stream = None
    await pod.link.close()                                # a dropout: no BYE
    await sleep(3.0)
    pod = await open_pod(new_link, clock)
    s = pod.session
    try:
        await s.hello(ver=0x0A, timeout=2.0)
        old = "a HELLO back"
    except crp.CrpTimeout:
        old = "no answer"
    except crp.CrpError:
        old = "BYE"
    byes = [p for _, _, p, _ in pod.log if p and p["type"] == crp.BYE]
    r.check(old == "BYE" and byes and byes[0]["device_id"] == pl.DEVICE_ID,
            f"a HELLO with version 0x0a: {old} (want BYE from BPOD)")
    pod.stream = dec
    peer = await s.hello()
    await pod.read_retry(pl.REG_INFO)
    await pod.read_retry(pl.REG_STATUS)
    st = await s.write(pl.REG_PUSH, b"\x01")
    cmd = dec.command()
    r.check(peer["device_id"] == pl.DEVICE_ID and st == crp.OK and cmd is not None and cmd[0] == pl.OP_STREAM_FROM,
            f"after the reconnect PodStream asks {cmd.hex(' ') if cmd else None}")
    st = await s.write(pl.REG_CTRL, cmd)
    await sleep(8.0)
    r.check(st == crp.OK and len(samples) > n_before + 8 * 200 and not dec.gaps and not dec.faults,
            f"resumed: +{len(samples) - n_before} samples, gaps {dec.gaps}, faults {dec.faults}, "
            f"reconnects {dec.stats['reconnects']}")

    # ---- 10. STOP, then BYE ----
    i_stop = len(pod.log)
    t_stop = pod.ms()
    stop = dec.stop_command()
    wait = dec.stop_wait_ms()
    st = await s.write(pl.REG_CTRL, stop)
    while pod.ms() - t_stop < wait and not any(k == "stopped" for t, k, _ in events if t >= t_stop):
        await sleep(0.05)
    stopped = [t for t, k, _ in events if t >= t_stop and k == "stopped"]
    i_ss, ss = next(((i, d) for i, _, d in pod.statuses(i_stop) if d["flags"] & pl.S_STREAM_STOPPED), (None, None))
    last_idx = dec.ep.last_idx if dec.ep else None
    r.check(st == crp.OK and stopped and ss is not None and last_idx is not None and ss["head"] == last_idx + 1
            and not dec.gaps,
            f"STOP: {status_name(st)}, stream_stopped after {((stopped[0] if stopped else pod.ms()) - t_stop):.0f} ms "
            f"(wait {wait:.0f}), head {ss['head'] if ss else None}, last index {last_idx}, gaps {dec.gaps}")
    finals = [f for _, _, f in pod.datas(i_stop) if f["flags"] & pl.F_FINAL]
    r.note(f"FINAL frames after STOP: {len(finals)}")
    await sleep(2.0)
    late = pod.datas(i_ss + 1) if i_ss is not None else pod.datas(i_stop)   # by arrival order, not time
    sts = [d for _, _, d in pod.statuses(i_ss + 1)] if i_ss is not None else []
    r.check(not late and sts and not any(x["flags"] & pl.S_STREAMING for x in sts) and dec.stats["after_close"] == 0,
            f"after STOP: {len(late)} DATA, {len(sts)} STATUS, streaming flag clear, after_close {dec.stats['after_close']}")
    await s.close()                                       # BYE: the session ends, the link stays
    await sleep(0.2)                                      # a push already on its way may still land
    i_bye = len(pod.log)
    await sleep(1.5)
    r.check(len(pod.log) == i_bye, f"after BYE: {len(pod.log) - i_bye} packets in 1.5 s (want none: no session)")
    await pod.link.close()
    return r


# ---- Bluetooth only: the advertisement and the GATT table ----
def low16(address):
    """'C3:12:34:56:AB:CD' -> 'ABCD', the end of the MAC (the name's suffix, docs/ble.md 2.4)."""
    return address.replace(":", "").replace("-", "").upper()[-4:]


async def find(name):
    from bleak import BleakScanner
    seen = {}

    def cb(dev, adv):
        uuids = [u.lower() for u in adv.service_uuids]
        nm = adv.local_name or dev.name or ""
        if crp.SERVICE_UUID in uuids or nm.startswith("BarPod-"):
            # one device calls back many times (advertisement, scan response): keep what any said
            _, nm0, had, _ = seen.get(dev.address, (None, "", False, None))
            seen[dev.address] = (dev, nm or nm0, had or crp.SERVICE_UUID in uuids, adv.rssi)

    async with BleakScanner(cb):
        await asyncio.sleep(6)
    return [v for v in seen.values() if v[1].startswith("BarPod-") and (not name or v[1] == name)]


async def main():
    name = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--name=")), "")
    seconds = next((float(a.split("=", 1)[1]) for a in sys.argv if a.startswith("--seconds=")), 15.0)
    on_usb = "--battery" not in sys.argv
    r = Report()
    links = []
    # everything inside the try, the scan too: no adapter, Bluetooth off, a request that timed out or
    # the link gone all end in a FAIL line and the final verdict, not just a traceback
    try:
        from bleak import BleakScanner

        # ---- 1. advertisement ----
        pods = await find(name)
        if not pods:
            r.check(False, "no BarPod found in 6 s of scanning" + crp.no_pod_hint())
            return r
        dev, nm, has_uuid, rssi = pods[0]
        r.check(len(nm) == 11 and nm[7:] == low16(dev.address),
                f"advertises name {nm!r} ({dev.address}, {rssi} dBm): the address's low 16 bits")
        top = int(dev.address.replace("-", ":").split(":")[0], 16)
        r.check(top & 0xC0 == 0xC0, f"a static random address: top byte 0x{top:02x} (top two bits 11)")
        r.check(has_uuid, "advertisement carries the CRP service UUID 5090")
        if len(pods) > 1:
            r.note(f"{len(pods)} pods in range, using the first (pick one with --name=)")

        # ---- 2. the GATT table (checked once the first link is up) ----
        async def gatt(pod):
            client = pod.link.client
            svc = client.services.get_service(crp.SERVICE_UUID)
            chars = {c.uuid.lower(): set(c.properties) for c in svc.characteristics} if svc else {}
            r.check(svc is not None and len(chars) == 2, f"service 5090 found with {len(chars)} characteristics (want 2)")
            r.check(chars.get(crp.RX_UUID) == {"write", "write-without-response"},
                    f"RX 50b0: {sorted(chars.get(crp.RX_UUID, []))} (want exactly write, write-without-response)")
            r.check(chars.get(crp.TX_UUID) == {"notify"}, f"TX 50b1: {sorted(chars.get(crp.TX_UUID, []))} (want exactly notify)")
            r.check(client.services.get_service(V1_SERVICE) is None, "no PodLink v1 service (9ba40001) left")
            r.note(f"services: {sorted(sv.uuid[:8] for sv in client.services)}; MTU as Windows reports it: {client.mtu_size}")

        first = [dev]

        async def new_link():
            # WinRT closes the scanner's device object at a disconnect: find it again each time
            d = first.pop() if first else (await BleakScanner.find_device_by_address(dev.address, timeout=10) or dev)
            links.append(crp.BleakLink(d))
            return links[-1]

        return await protocol(r, new_link, time.monotonic, seconds=seconds, on_usb=on_usb, name=nm, after_open=gatt)
    except Exception as e:
        import traceback
        traceback.print_exc()
        r.check(False, f"the checks stopped early: {type(e).__name__}: {e}")
        return r
    finally:
        for link in links:             # don't leave the pod connected: it takes one client at a time
            try:
                await link.close()
            except Exception:
                pass


if __name__ == "__main__":
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        sys.exit(0)
    rep = asyncio.run(main())
    print("PASS" if rep.ok else "FAIL")
    sys.exit(0 if rep.ok else 1)
