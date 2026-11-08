"""
log_ble.py's receiver (and ble_check.py's protocol checks) against make_fake_pod.py's simulated pod,
no hardware (D45). The same Receiver, connection order and output files as a bench run, over fake
links into the simulator (make_fake_pod's Bench: the pod, its CRP engine per link, the radio, the
USB cable), on an asyncio loop that runs on simulated time: minutes of streaming in seconds, the
same every run. Then the tools that read those files run on them, as after a bench run.

    python log_ble_sim.py          # or: python log_ble.py --selftest

Each run is checked against the simulator's ground truth (never the decoder's own numbers):
  - values: every sample = the pod's raw counts x its scale factors, exactly; t spacing = its ticks;
  - accounting: per boot, the samples plus the counted gaps tile what the pod streamed exactly once;
  - the capture replays (podlink.replay_rows) to the same samples, gaps, stats and faults, and the
    connection order's bytes on the wire are HELLO, READ INFO, READ STATUS, PUSH = 1, CTRL;
  - the link JSON has every key the other tools read, and the CSV every sample.
Runs:
  scan-filter     log_ble's scan: the CRP service and the BarPod- name, both required
  ble-outage      Bluetooth, a 3 s drop: nothing lost; compare_ble_usb.py against the pod's own CSV
  ble-overflow    a 45 s drop, past the 38.8 s ring: one overflow gap = the pod's lost_overflow rise
  ble-sleep       the SLEEP op, a wake 5 s later, --until-wake: check_sleep.py passes
  ble-mtu-busy    a late MTU exchange: INFO BUSY, retried; one INFO, no reconnect
  ble-mtu-23      an MTU stuck at 23: the run gives up (the MTU register says 23), writes nothing
  ble-boot-mismatch  a reset in an outage, a stale INFO at the reconnect: boot_mismatch, then the
                  receiver re-reads INFO and STATUS and streams the new boot from index 0
  not-a-pod       another CRP device answers the HELLO: the run stops there, sending nothing more
  usb-beside-ble  --usb and Bluetooth at once, a port reopen on USB; compare_ble_usb.py --exact on
                  the two captures, and on two planted faults (a value, a tick) it must FAIL
  usb-quiet       the PC's reader stalls 4.5 s: the port is reopened, nothing lost
  usb-pod-leaves  the PC's writes stop: the pod goes back to its CSV and the capture ends
  sim-discovery   the discovery answers added to the simulated pod (make_fake_pod has none) give
                  the bytes of host_test.c's t_discovery vectors, which the real engine gives
  ble-check       ble_check.py's sections 3 to 10 (no advertisement or GATT: the simulator has none);
                  section 5's names come from podcrp.c's own register table
"""

import asyncio
import contextlib
import functools
import io
import json
import re
import selectors
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

import ble_check
import compare_ble_usb as cbu
import crp
import log_ble as lb
import log_serial
import make_fake_pod as m
import podlink as pl

HERE = Path(__file__).parent
JSON_KEYS = {"samples", "ended", "gaps", "faults", "stats", "lost_overflow_by_boot", "sleeps", "epochs",
             "last_idx", "next_idx", "lost_counted"}       # what check_sleep, compare_ble_usb and check_soak read


class VirtualLoop(asyncio.SelectorEventLoop):
    """An event loop on simulated time: when nothing is ready it jumps straight to the next timer
    instead of waiting for it. Real sockets (its own wakeup pipe) still get polled."""

    def __init__(self):
        self._vt = 0.0
        sel = selectors.SelectSelector()
        poll = sel.select

        def select(timeout=None):
            ev = poll(0)
            if not ev and timeout:
                self._vt += timeout
            return ev
        sel.select = select
        super().__init__(sel)
        self._clock_resolution = 1e-9     # timers fire at their time, not up to a Windows tick early

    def time(self):
        return self._vt


# ---- the simulator behind fake links ----
class Sim:
    """make_fake_pod's Bench with its own phone idle: the radio's packets go to the open fake
    Bluetooth link, and a USB link plugs a UsbHost into the cable. drive() runs it in step with the
    loop, 1 ms of simulation per 1 ms of loop time; every_ms holds the scenario's actions."""

    def __init__(self, seed, **bench_kw):
        self.b = m.Bench("log_ble", seed=seed, **bench_kw)
        self.ble = None
        self.usb = []                      # every fake USB link opened, in order
        self.b.on_packet = self._to_ble    # instead of the Bench's own client
        self.b.link_down = self._link_down
        self.every_ms = []
        self.b.run(3000)                   # the pod samples 3 s before anyone connects

    def _to_ble(self, pkt, arrival):
        if self.ble is not None and self.ble.up and not self.ble.deaf:
            self.ble.on_packet(bytes(pkt), arrival)

    def _link_down(self):                  # the Bench's disconnect, ours or the pod's before sleep
        m.Client.link_down(self.b)
        if self.ble is not None:
            self.ble.down()

    def clock(self):
        return self.b.now / 1000

    async def drive(self):
        loop = asyncio.get_running_loop()
        base = loop.time() - self.b.now / 1000
        while True:
            await asyncio.sleep(0.001)
            want = (loop.time() - base) * 1000
            while self.b.now + 1 <= want + 1e-6:
                self.b.run(1)
                for fn in self.every_ms:
                    fn()


class SimBle:
    """Shaped like crp.BleakLink: level 0, no heartbeat, one notification = one packet, with the
    radio's latency and order. A close is a disconnect, and (like bleak) it reports on_down too."""
    level, heartbeat = 0, None

    def __init__(self, sim):
        self.sim = sim
        self.on_packet = self.on_down = None
        self.up = False
        self.deaf = False                  # out of range, not yet noticed: nothing arrives, writes fail

    async def open(self):
        b = self.sim.b
        if b.pod is None or b.connected:
            raise RuntimeError("no BarPod advertising")   # asleep (or the link is someone else's)
        self.sim.ble = self
        with_discovery(b.pod.ble)
        b.connected = True
        b.log.append(("link", b.wall(), "connect"))
        b.pod.ble.on_connect()
        self.up = True
        await asyncio.sleep(0.015)         # TX notifications on (Bench.connect's 15 ms)

    def send(self, pkt):
        if not self.up:
            raise OSError("not connected")
        if self.deaf:                      # as crp.BleakLink does: report the link down, then raise
            self.down()
            raise OSError("Could not write value: Unreachable")
        self.sim.b.send_raw(bytes(pkt))

    def down(self):
        if self.up:
            self.up = False
            if self.on_down:
                self.on_down()

    async def close(self):
        if self.up:
            self.sim.b.disconnect()        # the Bench calls link_down, so down() reports it


class SimUsb:
    """Shaped like crp.SerialLink: level 1 through a crp.Deframer (UsbHost's), a NOP every 1 s.
    The port opens in text mode, so the CSV comes first; the HELLO switches the pod to CRP. A
    close reports nothing (SerialLink's reader just stops)."""
    level, heartbeat = 1, 1.0

    def __init__(self, sim):
        self.sim = sim
        self.on_packet = self.on_down = None
        self.host = None
        self.mute = False                  # test-only: the PC's writes stop reaching the pod
        sim.usb.append(self)

    @property
    def deframer(self):
        return self.host.deframer if self.host else None

    async def open(self):
        b = self.sim.b
        if b.pod is None:
            raise OSError("no such port")
        host = m.UsbHost(b)
        host.on_packet = lambda pkt, arr: self.on_packet(bytes(pkt), arr) if host.open and self.on_packet else None
        b.usb = host
        host.open_port()
        self.host = host
        await asyncio.sleep(0.1)           # the CSV runs before the HELLO: dropped, and not "the pod left"

    def send(self, pkt):
        if self.host is None or not self.host.open:
            raise OSError("the port is closed")
        if not self.mute:
            self.host.send_raw(bytes(pkt))

    async def close(self):
        if self.host is not None and self.host.open:
            self.host.close_port()


class SimTarget:
    def __init__(self, sim, kind):
        self.sim, self.kind = sim, kind

    async def link(self):
        return SimBle(self.sim) if self.kind == "ble" else SimUsb(self.sim)

    def __str__(self):
        return f"the simulated pod ({self.kind})"


# ---- discovery: make_fake_pod's pod answers none, so the firmware's is added here ----
FW = HERE.parent / "firmware" / "pod"
SIM_NAME = "BarPod-51A0"                   # the simulated pod's name (it advertises nothing)
HOST_TEST_NAME = "BarPod-TEST"             # the name host_test.c gives podcrp_init
DISCOVERY = (crp.QUERY_DEVICE, crp.QUERY_GROUP, crp.QUERY_REG)
MIRRORS = ("BOOT_ID", "SERIAL", "FW_BUILD", "HEAD", "NAME")    # the state mirrors ble_check reads


@functools.lru_cache(None)
def firmware():
    """podcrp.c's register table, read from the firmware source rather than typed in a third time, so
    ble_check's GROUPS and REG_* (docs/ble.md 2.3) meet the table the pod really has. Returns
    ({addr: (name, type, access, flags, size)}, {constant: value}) with podcrp.h's and crp_types.h's
    constants."""
    k = {}
    for f in (FW / "podcrp.h", FW / "src" / "crp" / "crp" / "crp_types.h"):
        src = f.read_text()
        k.update((n, int(v, 16)) for n, v in re.findall(r"#define\s+(\w+)\s+(0x[0-9A-Fa-f]+)", src))
        k.update((n, int(v, 16)) for n, v in re.findall(r"\b(CRP_TYPE_\w+)\s*=\s*(0x[0-9A-Fa-f]+)", src))
    k.update(R=k["CRP_ACCESS_R"], W=k["CRP_ACCESS_W"], PUSHED=k["CRP_REG_PUSH_ON_CHANGE"])   # podcrp.c's shorthands

    def val(x):                            # "R | W", "PUSHED", "0", "32"
        out = 0
        for part in x.split("|"):
            part = part.strip()
            out |= k[part] if part in k else int(part, 0)
        return out
    rows = re.findall(r'\{(PODCRP_REG_\w+), "(\w+)", (CRP_TYPE_\w+), ([^,]+), ([^,]+), (\w+), \w+\}',
                      (FW / "podcrp.c").read_text())
    return {k[a]: (nm, k[t], val(acc), val(fl), val(size)) for a, nm, t, acc, fl, size in rows}, k


def discovery_answer(p, kind, name=SIM_NAME):
    """The engine's RESPONSE to a QUERY_DEVICE / QUERY_GROUP / QUERY_REG over podcrp.c's region ops
    (crp_engine.c handle_query_*, with coil's patches 4 and 5): one region per group, the bench group
    registered on USB only."""
    regs, k = firmware()
    mask, bench = k["PODCRP_GROUP_MASK"], k["PODCRP_GROUP_BENCH"]
    here = {a: r for a, r in regs.items() if a & mask != bench or kind == "usb"}
    if p["type"] == crp.QUERY_DEVICE:     # mask 0 (a direct device), the HELLO flags, the regions, the name
        groups = len({a & mask for a in here})
        return crp.response(p["txn"], crp.OK, p["base"], struct.pack("<IBI", 0, crp.FLAG_PUSH, groups) + name.encode())
    if p["type"] == crp.QUERY_GROUP:      # mask, R / W flags, the count past the base (patch 5)
        g = p["base"] & mask
        mine = [r for a, r in here.items() if a & mask == g]
        n = sum(1 for a in here if a & mask == g and a != g)
        flags = ((k["CRP_GROUP_READABLE"] if any(r[2] & k["R"] for r in mine) else 0)
                 | (k["CRP_GROUP_WRITABLE"] if any(r[2] & k["W"] for r in mine) else 0))
        val = struct.pack("<IBI", mask, flags, n) if n else bytes(9)    # none: still OK, 9 zero bytes
        return crp.response(p["txn"], crp.OK, p["base"], val)
    r = here.get(p["reg"])                 # QUERY_REG; a miss is UNKNOWN_REG (patch 4)
    if r is None:
        return crp.response(p["txn"], crp.UNKNOWN_REG, p["reg"])
    nm, typ, acc, fl, size = r             # type, access, flags, size, min_interval 0, the name
    return crp.response(p["txn"], crp.OK, p["reg"], struct.pack("<BBBIIB", typ, acc, fl, size, 0, len(nm)) + nm.encode())


def mirror(link, reg, name=SIM_NAME):
    """A state mirror ble_check reads, encoded as podcrp.c's table types it (link.ino's read hook
    gives the same values); None for the rest, which make_fake_pod's own on_read answers."""
    regs, k = firmware()
    r = regs.get(reg)
    if r is None or r[0] not in MIRRORS:
        return None
    info = pl.decode_frame(link.pod.info_frame())
    v = {"BOOT_ID": info["boot_id"], "SERIAL": info["serial"], "FW_BUILD": info["fw_build"],
         "HEAD": link.pod.head, "NAME": name}[r[0]]
    if r[1] == k["CRP_TYPE_STRING"]:
        return v.encode()                  # its own length, no NUL (link.ino)
    return struct.pack({k["CRP_TYPE_UINT8"]: "<B", k["CRP_TYPE_UINT16"]: "<H", k["CRP_TYPE_UINT32"]: "<I"}[r[1]], v)


def with_discovery(link, name=SIM_NAME):
    """Give one of make_fake_pod's Links the firmware's discovery and the mirrors above. Wrapped on the
    instance, once, so make_fake_pod stays exactly what the decoders were proven against. The answers
    go out through the Link's own send(): the spool, the slots and the MTU drop apply."""
    if getattr(link, "discovery", False):
        return
    link.discovery = True
    process, on_read = link.process, link.on_read

    def process_q(pkt):
        p = crp.parse(pkt)
        if p is not None and link.session and p["type"] in DISCOVERY:   # before a HELLO: ignored, as the rest
            link.last_recv = link.b.now
            link.send(discovery_answer(p, link.kind, name))
        else:
            process(pkt)

    def on_read_m(p):
        val = mirror(link, p["reg"], name)
        if val is None:
            return on_read(p)
        ok = 8 + len(val) <= link.max_pkt()    # make_fake_pod's BUSY rule
        link.send(crp.response(p["txn"], crp.OK if ok else crp.BUSY, p["reg"], val if ok else b""))
    link.process, link.on_read = process_q, on_read_m


def discovery_pin():
    """discovery_answer against firmware/pod/test/host_test.c's t_discovery vectors: every literal
    req / expect pair there, the bytes the real engine and podcrp.c give (bash firmware/pod/test/build.sh
    checks them). This pins the sim's answers, so ble_check's offsets meet the engine's own layout,
    not a second reading of crp_engine.c. Returns (failure strings, pairs checked)."""
    k = firmware()[1]
    src = (FW / "test" / "host_test.c").read_text()
    body = src[src.index("static void t_discovery"):]
    body = body[:body.index("\n}\n")]
    usb_at = body.index("fresh(PODCRP_USB)")
    arg = r'(?:rg\(\w+\)|str_hex\("[^"]*"\))'
    expr = r'(?:"[^"]*"|hx\("[^"]*"(?:\s*,\s*' + arg + r')*\))'

    def ev(e):                             # host_test's hx / rg / str_hex, for the %s-only formats
        hx = re.match(r'hx\("([^"]*)"(.*)\)$', e, re.S)
        if not hx:
            return bytes.fromhex(e.strip('"'))
        fmt, args = hx.groups()
        subs = [k[r].to_bytes(4, "little").hex() if r else s.encode().hex()
                for r, s in re.findall(r'rg\((\w+)\)|str_hex\("([^"]*)"\)', args)]
        if fmt.count("%") != len(subs) or fmt.count("%s") != len(subs):
            return None
        return bytes.fromhex(fmt.replace("%s", "{}").format(*subs))
    fails, n = [], 0
    for mo in re.finditer(r"req\(l,\s*(" + expr + r")\);\s*expect\(l,\s*(" + expr + r"),", body, re.S):
        kind = "usb" if mo.start() > usb_at else "ble"
        q, want = ev(mo.group(1)), ev(mo.group(2))
        p = crp.parse(q) if q else None
        got = discovery_answer(p, kind, HOST_TEST_NAME) if p and p["type"] in DISCOVERY else None
        n += 1
        if want is None or got != want:
            fails.append(f"{kind} {q.hex(' ') if q else mo.group(1)}: the sim answers {got.hex(' ') if got else None}, "
                         f"host_test.c wants {want.hex(' ') if want else mo.group(2)}")
    if n < 11:                             # 9 on Bluetooth, 2 on USB on 11-04: a vacuous pass is a FAIL
        fails.append(f"only {n} req / expect pairs found in host_test.c's t_discovery (want 11 or more)")
    return fails, n


class Tracked(lb.Receiver):
    """log_ble's Receiver, also keeping each sample and its (boot_id, index): the truth's key."""

    def __init__(self, folder, name, sim, kind, target=None, clock=None):
        self.emits, self.smp, self.lines = [], [], []
        self.paths = [folder / f"{p}-{name}.{e}" for p, e in (("bar-pod", "csv"), ("pod-capture", "csv"), ("pod-link", "json"))]
        self.sim = sim
        super().__init__(self.paths[0], self.paths[1], target or SimTarget(sim, kind), clock=clock or sim.clock,
                         say=self.lines.append)

    def on_sample(self, s):
        ep = self.stream.ep
        self.emits.append((ep.boot_id, ep.last_idx))
        self.smp.append(s)
        super().on_sample(s)


def go(sim, *runs):
    """Run receivers to their end on the virtual loop: runs = (receiver, log_ble.session kwargs).
    Then their files are written, as log_ble.main() does."""
    async def main():
        drive = asyncio.ensure_future(sim.drive())
        try:
            await asyncio.gather(*(lb.session(rx, quit_flag=lambda: False, **kw) for rx, kw in runs))
        finally:
            drive.cancel()
    loop = VirtualLoop()
    try:
        loop.run_until_complete(main())
    finally:
        loop.close()
    for rx, _ in runs:
        rx.close(rx.paths[2])


# ---- checks ----
def truth(rx, ranges):
    """The simulator's ground truth (make_fake_pod.check's rules) on a receiver's samples. ranges:
    [(boot_id, start, end)], what the pod streamed per boot. Returns failure strings."""
    fails = []
    pods = {p.boot_id: p for p in rx.sim.b.pods}
    t = [s["t"] for s in rx.smp]
    if any(b <= a for a, b in zip(t, t[1:])):
        fails.append("t not strictly increasing")
    prev = None
    for (boot, idx), s in zip(rx.emits, rx.smp):
        p = pods[boot]
        gx, gy, gz, ax, ay, az = p.raw(idx)
        a_l, g_l = m.f32(p.accel_lsb), m.f32(p.gyro_lsb)
        if tuple(s[k] for k in ("ax", "ay", "az", "gx", "gy", "gz")) != (ax * a_l, ay * a_l, az * a_l, gx * g_l, gy * g_l, gz * g_l):
            fails.append(f"boot {boot:#x} index {idx}: values differ from the pod's")
            break
        if prev and prev[0] == boot and abs((s["t"] - prev[2]) - (p.ticks[idx] - p.ticks[prev[1]]) / 32768) > 1e-9:
            fails.append(f"boot {boot:#x} index {idx}: t step differs from the pod's ticks")
            break
        prev = (boot, idx, s["t"])
    for boot, start, end in ranges:
        cover = sorted([(i, i + 1, "sample") for b, i in rx.emits if b == boot]
                       + [(g["idx"], g["idx"] + g["n"], g["kind"]) for g in rx.stream.gaps
                          if g["boot_id"] == boot and g["kind"] in m.INDEX_KINDS and g["n"] > 0])
        at, bad = start, None
        for a, z, what in cover:
            if a != at:
                bad = f"at index {at} the next is {what} from {a}"
                break
            at = z
        if bad is None and at != end:
            bad = f"covered up to {at}, want {end}"
        if bad:
            fails.append(f"boot {boot:#x} accounting [{start}, {end}): {bad}")
    if not ranges:
        fails.append("no ranges (the accounting check would be vacuous)")
    return fails


def files(rx):
    """The capture replays to what the receiver decoded; the JSON and the CSV are complete."""
    fails = []
    try:
        samples, s, _ = pl.replay_rows(pl.read_capture(rx.paths[1]))
        if samples != rx.smp:
            fails.append(f"the capture replays to {len(samples)} samples, not the receiver's {len(rx.smp)} (or values differ)")
        live_faults = [f for f in rx.stream.faults if f != "MTU too small"]   # that one is the receiver's, not the decoder's
        if (s.gaps, s.stats, s.faults) != (rx.stream.gaps, rx.stream.stats, live_faults):
            fails.append("the capture replays to other gaps / stats / faults")
    except AssertionError as e:
        fails.append(f"the capture doesn't replay: {e}")
    j = json.loads(rx.paths[2].read_text())
    if not JSON_KEYS <= set(j):
        fails.append(f"link JSON lacks {sorted(JSON_KEYS - set(j))}")
    if j["samples"] != len(rx.smp) or j["gaps"] != json.loads(json.dumps(rx.stream.gaps)):
        fails.append("link JSON samples / gaps differ from the receiver's")
    rows = len(rx.paths[0].read_text().splitlines()) - 1
    if rows != len(rx.smp):
        fails.append(f"bar-pod CSV has {rows} rows, the receiver decoded {len(rx.smp)}")
    return fails


def sent(rx):
    """Every packet the receiver sent, from its capture's tx rows, without the heartbeat NOPs."""
    rows = pl.read_capture(rx.paths[1])
    return [bytes.fromhex(p) for k, _, p in rows if k == "tx" and p != "00"]


def wire_order(rx, n_connects):
    """Each connect's opening on the wire, byte for byte: HELLO, READ INFO, READ STATUS, PUSH = 1
    and CTRL with the logged command, txn ids 1 to 4 (a fresh Session per connect)."""
    rows = pl.read_capture(rx.paths[1])
    cmds = [p for k, _, p in rows if k == "cmd"]
    tx = sent(rx)
    want = []
    for c in cmds[:n_connects]:
        want.append([crp.hello(), crp.read(1, pl.REG_INFO), crp.read(2, pl.REG_STATUS),
                     crp.write(3, pl.REG_PUSH, b"\x01"), crp.write(4, pl.REG_CTRL, bytes.fromhex(c))])
    got, k = [], 0
    for w in want:
        while k < len(tx) and tx[k] != w[0]:
            k += 1
        got.append(tx[k:k + 5])
        k += 5
    return None if got == want and len(cmds) >= n_connects else f"the connection order on the wire: {[[x.hex() for x in g] for g in got]}"


def quietly(fn, *a):
    """Run fn, keeping its prints: (result, output)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        r = fn(*a)
    return r, out.getvalue()


def usb_truth(pod, path):
    """The pod's own CSV, as log_serial.py records it from USB text mode (make_fake_pod.csv_line)."""
    lines = (m.csv_line(pod, i).decode() for i in range(pod.head))
    quietly(log_serial.record, lines, path)


def first_cursor(link):
    return link.cmd_log[0][1]


def last_head(link):
    return link.drain_heads[-1][1]


# ---- the runs ----
def ble_outage(folder):
    sim = Sim(seed=101)
    rx = Tracked(folder, "ble-outage", sim, "ble")
    go(sim, (rx, dict(seconds=20, outages=[(3.0, 8.0)])))
    p = sim.b.pods[0]
    fails = truth(rx, [(p.boot_id, first_cursor(p.ble), last_head(p.ble))]) + files(rx)
    st = rx.stream.stats
    if rx.stream.gaps or rx.stream.faults or st["reconnects"] != 1 or st["stopped"] != 1 or rx.ended != "done":
        fails.append(f"gaps {rx.stream.gaps}, faults {rx.stream.faults}, reconnects {st['reconnects']}, "
                     f"stopped {st['stopped']}, ended {rx.ended!r}")
    fails += [f for f in [wire_order(rx, 2)] if f]
    usb_csv = folder / "bar-xiao-ble-outage.csv"
    usb_truth(p, usb_csv)
    good, out = quietly(cbu.compare, str(usb_csv), str(rx.paths[0]), str(rx.paths[2]))
    if not good:
        fails.append("compare_ble_usb.py against the pod's CSV:\n" + out)
    return rx, fails


def ble_overflow(folder):
    sim = Sim(seed=102)
    rx = Tracked(folder, "ble-overflow", sim, "ble")
    go(sim, (rx, dict(seconds=75, outages=[(45.0, 5.0)])))
    p = sim.b.pods[0]
    fails = truth(rx, [(p.boot_id, first_cursor(p.ble), last_head(p.ble))]) + files(rx)
    over = [g["n"] for g in rx.stream.gaps if g["kind"] == "overflow"]
    rng = rx.lost_by_boot.get(str(p.boot_id))
    if len(over) != 1 or over[0] != p.lost or not rng or rng[1] - rng[0] != p.lost or rx.ended != "done":
        fails.append(f"overflow gaps {over}, the pod lost {p.lost}, its lost_overflow {rng}, ended {rx.ended!r}")
    usb_csv = folder / "bar-xiao-ble-overflow.csv"
    usb_truth(p, usb_csv)
    good, out = quietly(cbu.compare, str(usb_csv), str(rx.paths[0]), str(rx.paths[2]))
    if not good:
        fails.append("compare_ble_usb.py against the pod's CSV:\n" + out)
    return rx, fails


def ble_sleep(folder):
    sim = Sim(seed=103)
    asleep = []

    def wake():                            # 5 s after the pod falls asleep, a pickup wakes it
        b = sim.b
        if b.pod is None and not asleep:
            asleep.append(b.now)
        if asleep and len(b.pods) == 1 and b.now - asleep[0] >= 5000:
            b.wake(boot_id=0x2103)
    sim.every_ms.append(wake)
    rx = Tracked(folder, "ble-sleep", sim, "ble")
    go(sim, (rx, dict(seconds=60, outages=[], sleeps=[(6.0, 0)], until_wake=3.0)))
    p1, p2 = sim.b.pods
    fails = truth(rx, [(p1.boot_id, first_cursor(p1.ble), last_head(p1.ble)),
                       (p2.boot_id, 0, last_head(p2.ble))]) + files(rx)
    st = rx.stream.stats
    if st["epochs"] != 2 or st["stopped"] != 2 or rx.stream.faults or rx.ended != "done":
        fails.append(f"epochs {st['epochs']}, stopped {st['stopped']}, faults {rx.stream.faults}, ended {rx.ended!r}")
    r = subprocess.run([sys.executable, str(HERE / "check_sleep.py"), str(rx.paths[2])],
                       capture_output=True, text=True, cwd=HERE, timeout=120)
    if r.returncode != 0:
        fails.append("check_sleep.py:\n" + r.stdout + r.stderr)
    return rx, fails


def ble_dead_write(folder):
    """The pod goes out of range half a second before a SLEEP op: the radio is silent and the laptop
    can't know yet (the pod's 4 s supervision timeout), so the SLEEP's write is the first thing to
    meet the dead link and fails (bleak: Unreachable). That must read as the link going, not end
    the run (the 11-07 audit): log_ble reconnects once the pod times out and advertises again, the
    ring covers the gap, and the pod, which never got the SLEEP, streams on."""
    sim = Sim(seed=111)
    rx = Tracked(folder, "ble-dead-write", sim, "ble")
    when = {}

    def radio():
        b = sim.b
        if "deaf" not in when and sim.ble is not None and sim.ble.up and sim.clock() - rx.t0 >= 7.5:
            when["deaf"] = b.now
            sim.ble.deaf = True
        if "deaf" in when and "timeout" not in when and b.now - when["deaf"] >= 4000:
            when["timeout"] = b.now
            b.disconnect()                 # the pod's supervision timeout: it drops its side, advertises again
    sim.every_ms.append(radio)
    go(sim, (rx, dict(seconds=25, outages=[], sleeps=[(8.0, 0)])))
    if rx.ended != "done":                 # the bug itself: the failed write ended the run
        return rx, [f"the run ended {rx.ended!r} at the failed SLEEP write instead of reconnecting"]
    p = sim.b.pods[0]
    fails = truth(rx, [(p.boot_id, first_cursor(p.ble), last_head(p.ble))]) + files(rx)
    st = rx.stream.stats
    if len(sim.b.pods) != 1 or rx.sleeps or rx.stream.gaps or rx.stream.faults or st["reconnects"] != 1             or st["stopped"] != 1 or rx.ended != "done" or "timeout" not in when:
        fails.append(f"pods {len(sim.b.pods)}, sleeps {rx.sleeps}, gaps {rx.stream.gaps}, faults {rx.stream.faults}, "
                     f"reconnects {st['reconnects']}, stopped {st['stopped']}, ended {rx.ended!r}, radio {when}")
    if not any("SLEEP: CRP: the link refused" in ln for ln in rx.lines):
        fails.append("no 'SLEEP: CRP: the link refused the packet' line: the failed write wasn't reported as the link going")
    fails += [f for f in [wire_order(rx, 2)] if f]
    return rx, fails


def ble_mtu_busy(folder):
    sim = Sim(seed=104, native_mtu=False, mtu_after=1200)
    rx = Tracked(folder, "ble-mtu-busy", sim, "ble")
    go(sim, (rx, dict(seconds=8, outages=[])))
    p = sim.b.pods[0]
    fails = truth(rx, [(p.boot_id, first_cursor(p.ble), last_head(p.ble))]) + files(rx)
    infos = sum(1 for k, _, h in pl.read_capture(rx.paths[1])
                if k == "pkt" and (pl.crp_frame(bytes.fromhex(h)) or b"")[:1] == bytes([pl.T_INFO]))
    st = rx.stream.stats
    if rx.busy < 4 or infos != 1 or st["reconnects"] != 0 or rx.stream.faults or rx.ended != "done":
        fails.append(f"{rx.busy} BUSY (want 4 or more), {infos} INFO (want 1), reconnects {st['reconnects']}, "
                     f"faults {rx.stream.faults}, ended {rx.ended!r}")
    return rx, fails


def ble_mtu_23(folder):
    sim = Sim(seed=105, mtu=23, native_mtu=False)
    rx = Tracked(folder, "ble-mtu-23", sim, "ble")
    go(sim, (rx, dict(seconds=10, outages=[])))
    writes = [x for x in sent(rx) if x[0] == crp.WRITE]
    fails = files(rx)
    if "MTU is too small" not in rx.ended or "MTU 23" not in rx.ended or rx.n or writes:
        fails.append(f"ended {rx.ended!r}, {rx.n} samples, {len(writes)} writes (want: gave up at MTU 23, nothing written)")
    return rx, fails


def usb_beside_ble(folder):
    sim = Sim(seed=106)
    ble = Tracked(folder, "both-ble", sim, "ble")
    usb = Tracked(folder, "both-usb", sim, "usb")
    go(sim, (ble, dict(seconds=30, outages=[])), (usb, dict(seconds=24, outages=[(4.0, 8.0)])))
    p = sim.b.pods[0]
    fails = [f"ble: {f}" for f in truth(ble, [(p.boot_id, first_cursor(p.ble), last_head(p.ble))]) + files(ble)]
    fails += [f"usb: {f}" for f in truth(usb, [(p.boot_id, first_cursor(p.usb), last_head(p.usb))]) + files(usb)]
    for name, rx, rec in (("ble", ble, 0), ("usb", usb, 1)):
        st = rx.stream.stats
        if rx.stream.gaps or rx.stream.faults or st["reconnects"] != rec or st["stopped"] != 1 or rx.ended != "done":
            fails.append(f"{name}: gaps {rx.stream.gaps}, faults {rx.stream.faults}, reconnects {st['reconnects']}, "
                         f"stopped {st['stopped']}, ended {rx.ended!r}")
    fails += [f"usb: {f}" for f in [wire_order(usb, 2)] if f]
    modes = [(mo, why) for _, mo, why in p.usb.mode_log]
    if modes != [("crp", "hello"), ("text", "closed"), ("crp", "hello"), ("text", "bye")]:
        fails.append(f"the pod's USB modes {modes}: want CRP, text at the close, CRP, text at the BYE")
    nops = sum(1 for k, _, h in pl.read_capture(usb.paths[1]) if k == "tx" and h == "00")
    if nops < 15:
        fails.append(f"{nops} NOPs from the USB session in ~24 s (want one a second)")
    if not sim.usb[0].host.text_rx or sim.usb[0].deframer.dropped < sim.usb[0].host.text_rx:
        fails.append("no CSV dropped before the first HELLO: the port didn't open in text mode")

    # --exact on the two captures, then on two planted faults it must catch
    good, out = quietly(cbu.exact, str(ble.paths[1]), str(usb.paths[1]))
    if not good:
        fails.append("compare_ble_usb.py --exact:\n" + out)
    rows = pl.read_capture(usb.paths[1])
    data = [i for i, (kind, _, h) in enumerate(rows) if kind == "pkt" and (pl.crp_frame(bytes.fromhex(h)) or b"")[:1] == bytes([pl.T_DATA])]
    k = data[len(data) // 2]               # mid-run: its samples are on both links
    for what, off in (("a raw count", 6 + 14 + 2), ("a tick", 6 + 14 + 14)):    # sample 0's gx; sample 1's dt
        bad = folder / f"pod-capture-planted-{off}.csv"
        b = bytearray(bytes.fromhex(rows[k][2]))
        b[off] ^= 0x01
        lines = usb.paths[1].read_text().splitlines()
        parts = lines[k + 1].split(",")
        parts[4] = b.hex()
        lines[k + 1] = ",".join(parts)
        bad.write_text("\n".join(lines) + "\n")
        good, out = quietly(cbu.exact, str(ble.paths[1]), str(bad))
        if good:
            fails.append(f"compare_ble_usb.py --exact passed a capture with {what} changed")
    return usb, fails


def usb_quiet(folder):
    sim = Sim(seed=107)
    stalled = []

    def stall():
        if sim.usb and sim.b.now >= 9000 and not stalled:
            stalled.append(sim.b.now)
            sim.b.usb.read_stall_until = sim.b.now + 4500     # the PC's reader takes nothing for 4.5 s
    sim.every_ms.append(stall)
    rx = Tracked(folder, "usb-quiet", sim, "usb")
    go(sim, (rx, dict(seconds=20, outages=[])))
    p = sim.b.pods[0]
    fails = truth(rx, [(p.boot_id, first_cursor(p.usb), last_head(p.usb))]) + files(rx)
    st = rx.stream.stats
    if rx.stream.gaps or rx.stream.faults or st["reconnects"] != 1 or rx.ended != "done" or len(sim.usb) != 2:
        fails.append(f"gaps {rx.stream.gaps}, faults {rx.stream.faults}, reconnects {st['reconnects']}, "
                     f"ports opened {len(sim.usb)}, ended {rx.ended!r}")
    return rx, fails


def usb_pod_leaves(folder):
    sim = Sim(seed=108)
    muted = []

    def mute():
        if sim.usb and sim.b.now >= 9000 and not muted:
            muted.append(sim.b.now)
            sim.usb[0].mute = True
    sim.every_ms.append(mute)
    rx = Tracked(folder, "usb-pod-leaves", sim, "usb")
    go(sim, (rx, dict(seconds=30, outages=[])))
    p = sim.b.pods[0]
    ep = rx.stream.epochs[0]
    end = max(ep.status["head"], max(i for _, i in rx.emits) + 1)
    fails = truth(rx, [(p.boot_id, first_cursor(p.usb), end)]) + files(rx)
    texts = [(mo, why) for _, mo, why in p.usb.mode_log if mo == "text"]
    stops = [x for x in sent(rx) if x[:1] == bytes([crp.WRITE]) and x[2:6] == pl.REG_CTRL.to_bytes(4, "little") and x[6:] == b"\x03"]
    tails = [g for g in rx.stream.gaps if g["kind"].endswith("-tail")]
    if not rx.ended.startswith("the pod ended the session") or "CSV" not in rx.ended or texts != [("text", "silence")] \
            or stops or rx.stream.faults or any(not g.get("lower_bound") or g["n"] > p.head - g["idx"] for g in tails) \
            or sim.usb[0].host.open:
        fails.append(f"ended {rx.ended!r}, the pod's text modes {texts}, {len(stops)} STOPs after it left, "
                     f"faults {rx.stream.faults}, tails {tails}, port open {sim.usb[0].host.open}")
    return rx, fails


def ble_boot_mismatch(folder):
    """The pod resets during an outage and the reconnect's INFO read answers the old boot's INFO (a
    cached value, make_fake_pod's boot-mismatch-rollback): STREAM_FROM(old boot) gets the
    boot_mismatch STATUS, and run() answers the decoder's need-info on the same link: READ INFO,
    READ STATUS, then the command, STREAM_FROM(new boot, 0)."""
    sim = Sim(seed=110)
    reset = []

    def boot():                            # 2 s into the outage; the new boot outruns the old head and ticks
        b = sim.b
        if not reset and not b.connected and b.now >= 9000:
            reset.append(b.now)
            old = b.pod.info_frame()
            b.pod = None
            b.wake(boot_id=0x2110)
            b.pod.ble.info_override = old
    sim.every_ms.append(boot)
    rx = Tracked(folder, "ble-boot-mismatch", sim, "ble")
    go(sim, (rx, dict(seconds=40, outages=[(20.0, 4.0)])))
    p1, p2 = sim.b.pods
    # the old boot is known up to the last head the phone heard, or the last sample if DATA came after it
    end1 = max(rx.stream.epochs[0].status["head"], max(i for b, i in rx.emits if b == p1.boot_id) + 1)
    fails = truth(rx, [(p1.boot_id, first_cursor(p1.ble), end1), (p2.boot_id, 0, last_head(p2.ble))]) + files(rx)
    st = rx.stream.stats
    tails = [g for g in rx.stream.gaps if g["kind"] == "lost-tail"]      # at most one, a lower bound
    others = [g for g in rx.stream.gaps if g["kind"] != "lost-tail"]
    if rx.stream.faults != ["boot mismatch"] or st["epochs"] != 2 or st["reconnects"] != 1 or st["stopped"] != 1 \
            or len(tails) > 1 or any(not g.get("lower_bound") or g["n"] > p1.head - g["idx"] for g in tails) \
            or others or rx.ended != "done":
        fails.append(f"faults {rx.stream.faults} (want boot mismatch), epochs {st['epochs']}, reconnects "
                     f"{st['reconnects']}, stopped {st['stopped']}, gaps {rx.stream.gaps}, ended {rx.ended!r}")
    fails += [f for f in [wire_order(rx, 2)] if f]
    cmds = [p for k, _, p in pl.read_capture(rx.paths[1]) if k == "cmd"]
    tx = sent(rx)
    k = next((i for i, x in enumerate(tx) if x == crp.hello() and i > 0), None)     # the reconnect's HELLO
    want = [crp.read(5, pl.REG_INFO), crp.read(6, pl.REG_STATUS), crp.write(7, pl.REG_CTRL, bytes.fromhex(cmds[2]))] \
        if len(cmds) >= 3 else None
    if k is None or want is None or tx[k + 5:k + 8] != want or not cmds[2].startswith("01") \
            or cmds[2][4:12] != p2.boot_id.to_bytes(4, "little").hex() or cmds[2][12:] != "00000000":
        fails.append(f"after boot_mismatch: commands {cmds}, sent {[x.hex() for x in tx[(k or 0) + 5:(k or 0) + 8]]} "
                     "(want READ INFO, READ STATUS, then STREAM_FROM(new boot, 0) on the same session)")
    return rx, fails


class ForeignBle:
    """Another device on Ansync's CRP service (every one shares it, docs/ble.md 2.4): it answers a
    HELLO with its own device_id."""
    level, heartbeat = 0, None
    DEVICE_ID = 0x4C494F43                 # "COIL"

    def __init__(self, sent):
        self.sent = sent
        self.on_packet = self.on_down = None

    async def open(self):
        pass

    def send(self, pkt):
        self.sent.append(bytes(pkt))
        if pkt[:1] == bytes([crp.HELLO]):
            hello = crp.hello(crp.VERSION, 4, 512, crp.FLAG_PUSH, self.DEVICE_ID)
            asyncio.get_running_loop().call_later(0.02, lambda: self.on_packet(hello, 1000.0))

    async def close(self):
        pass


def not_a_pod(folder):
    """HELLO from a CRP device that isn't a BarPod: the run stops right there, sending nothing more
    (no READ, no PUSH, no CTRL), and still writes its three files."""
    loop = VirtualLoop()
    sent_pkts = []

    class Target:
        kind = "ble"

        async def link(self):
            return ForeignBle(sent_pkts)

        def __str__(self):
            return "a foreign CRP device"

    rx = Tracked(folder, "not-a-pod", None, "ble", target=Target(), clock=loop.time)
    try:
        loop.run_until_complete(lb.session(rx, seconds=5, outages=[], quit_flag=lambda: False))
    finally:
        loop.close()
    rx.close(rx.paths[2])
    fails = files(rx)
    if "not a BarPod" not in rx.ended or f"0x{ForeignBle.DEVICE_ID:08x}" not in rx.ended or sent_pkts != [crp.hello()]:
        fails.append(f"ended {rx.ended!r}, sent {[x.hex() for x in sent_pkts]} (want: stopped at the HELLO, nothing else sent)")
    return rx, fails


def scan_filter():
    """log_ble.is_pod: Ansync's service AND the BarPod- name, since every CRP device shares the
    service (docs/ble.md 2.4). Returns failure strings."""
    class Adv:                             # the fields of bleak's BLEDevice / AdvertisementData it reads
        def __init__(self, local=None, name=None, uuids=()):
            self.local_name, self.name, self.service_uuids = local, name, list(uuids)
    U = crp.SERVICE_UUID
    cases = [("a BarPod", Adv("BarPod-1A2B", uuids=[U]), True),
             ("the name from the device, the UUID in capitals", Adv(None, "BarPod-00FF", [U.upper()]), True),
             ("another device on the CRP service", Adv("CoilDriver", uuids=[U]), False),
             ("a BarPod- name on the old PodLink v1 service", Adv("BarPod-1A2B", uuids=[ble_check.V1_SERVICE]), False),
             ("the CRP service, no name", Adv(uuids=[U]), False)]
    return [f"is_pod({what}) = {not want}, want {want}" for what, a, want in cases if lb.is_pod(a, a) != want]


def ble_check_sim(folder):
    """ble_check.protocol on the simulated pod (seconds 15 of streaming, as the bench default), with
    section 5 on the discovery answers above."""
    sim = Sim(seed=109)
    lines = []
    r = ble_check.Report(say=lines.append)

    async def new_link():
        return SimBle(sim)

    async def main():
        drive = asyncio.ensure_future(sim.drive())
        try:
            await ble_check.protocol(r, new_link, sim.clock, seconds=15.0, on_usb=False, gravity=None,
                                     discovery=True, name=SIM_NAME)
        finally:
            drive.cancel()
    loop = VirtualLoop()
    try:
        loop.run_until_complete(main())
    finally:
        loop.close()
    n = sum(1 for x in lines if x.startswith(("PASS", "FAIL")))
    fails = [x for x in lines if x.startswith("FAIL")]
    sec5 = ["QUERY_DEVICE"] + [f"group 0x{g:08x}" for g in ble_check.GROUPS] + ["state mirrors"]
    missing = [w for w in sec5 if not any(x.startswith("PASS") and w in x for x in lines)]
    if missing:
        fails.append(f"section 5 didn't pass (or didn't run): {missing}")
    if n < 40:
        fails.append(f"only {n} checks ran")
    return r, fails, n


def selftest(keep=False):
    folder = Path(tempfile.mkdtemp(prefix="log_ble_sim-"))
    ok = True
    try:
        fails = scan_filter()
        print(f"{'PASS' if not fails else 'FAIL'}  {'scan-filter':16} the CRP service and the BarPod- name, both required")
        for f in fails:
            print("      - " + f)
        ok &= not fails
        for fn in (ble_outage, ble_overflow, ble_sleep, ble_dead_write, ble_mtu_busy, ble_mtu_23, ble_boot_mismatch, not_a_pod,
                   usb_beside_ble, usb_quiet, usb_pod_leaves):
            try:
                rx, fails = fn(folder)
                what = f"{rx.n} samples, gaps {[(g['kind'], g['n']) for g in rx.stream.gaps]}, ended {rx.ended!r}"
            except Exception as e:
                import traceback
                rx, fails, what = None, [traceback.format_exc()], f"{type(e).__name__}: {e}"
            name = fn.__name__.replace("_", "-")
            print(f"{'PASS' if not fails else 'FAIL'}  {name:16} {what}")
            for f in fails:
                print("      - " + f.replace("\n", "\n        "))
            if fails and rx is not None:
                print("        the receiver said:\n        " + "\n        ".join(rx.lines[-25:]))
            ok &= not fails
        try:
            fails, n = discovery_pin()
        except Exception:
            import traceback
            fails, n = [traceback.format_exc()], 0
        print(f"{'PASS' if not fails else 'FAIL'}  {'sim-discovery':16} {n} of host_test.c's discovery vectors, byte for byte")
        for f in fails:
            print("      - " + f)
        ok &= not fails
        try:
            r, fails, n = ble_check_sim(folder)
        except Exception:
            import traceback
            fails, n = [traceback.format_exc()], 0
        print(f"{'PASS' if not fails else 'FAIL'}  {'ble-check':16} {n} checks of ble_check.py on the simulated pod")
        for f in fails:
            print("      - " + f)
        ok &= not fails
    finally:
        if keep:
            print(f"files kept in {folder}")
        else:
            shutil.rmtree(folder, ignore_errors=True)
    print(f"{'PASS' if ok else 'FAIL'}  log_ble.py on the simulated pod")
    return ok


if __name__ == "__main__":
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        sys.exit(0)
    sys.exit(0 if selftest(keep="--keep" in sys.argv) else 1)
