"""
Step 5 bench check: the pod's Bluetooth service from the laptop (bleak), against docs/ble.md.
Needs bleak once (decision in docs/ble-plan.md):  .venv\\Scripts\\pip install bleak

    python ble_check.py                      # find a BarPod, run every check (~45 s)
    python ble_check.py --seconds=600        # stream 10 min (run log_serial.py on USB alongside)
    python ble_check.py --name=BarPod-1A2B   # pick one pod when several are near
    python ble_check.py --battery            # the pod is on battery: expect the USB flag off

Checks, in order:
  1. the advertisement: name BarPod-XXXX and the 9ba40001 service UUID
  2. the 5 characteristics and their properties; INFO, CAL and STATUS read back whole
  3. STATUS notifies at 1 Hz (USB flag, n_per_packet, no mtu_too_small after 3 s)
  4. a STREAM_FROM with the wrong boot_id gets a boot_mismatch STATUS and starts nothing
  5. STREAM_LIVE (back 600): DATA through the reference decoder (PodStream), no gaps, no faults,
     pkt_seq continuous, ~211 Hz, ~13 frames/s at MTU 247
  6. disconnect 3 s, reconnect, STREAM_FROM(next index): nothing lost (the ring covers it)
  7. STOP: a stream_stopped STATUS whose head = the last sample + 1, then no more DATA
"""

import asyncio
import statistics
import struct
import sys
import time

from bleak import BleakClient, BleakScanner

import podlink as pl

PROPS = {pl.DATA_UUID: {"notify"}, pl.CTRL_UUID: {"write"}, pl.STATUS_UUID: {"read"},
         pl.INFO_UUID: {"read"}, pl.CAL_UUID: {"read"}}


def now_ms():
    return time.monotonic() * 1000


class Report:
    def __init__(self):
        self.ok = True

    def check(self, good, msg):
        print(("PASS  " if good else "FAIL  ") + msg, flush=True)
        self.ok = self.ok and bool(good)

    def note(self, msg):
        print("      " + msg, flush=True)


class Link:
    """One connection: every notification is logged raw (arrival ms, kind, bytes) and, while a
    PodStream is attached, fed to it in arrival order. All notifications come on DATA (D28);
    the kind is the frame's type byte: pl.DATA_UUID for DATA frames, pl.STATUS_UUID for STATUS."""

    def __init__(self, dev):
        self.dev = dev
        self.client = None
        self.log = []
        self.stream = None

    def _cb(self):
        def cb(_sender, data):
            t = now_ms()
            b = bytes(data)
            kind = pl.STATUS_UUID if b[:1] == bytes([pl.T_STATUS]) else pl.DATA_UUID
            self.log.append((t, kind, b))
            if self.stream is not None:
                self.stream.frame(b, t)
        return cb

    async def connect(self):
        # WinRT closes the scanner's device object at a disconnect: find it again each time
        for attempt in range(3):
            try:
                if self.client is not None:
                    self.dev = await BleakScanner.find_device_by_address(self.dev.address, timeout=10) or self.dev
                self.client = BleakClient(self.dev)
                await self.client.connect()
                break
            except Exception:
                if attempt == 2:
                    raise
                await asyncio.sleep(1)
        self.t_connect = now_ms()

    async def subscribe(self):
        await self.client.start_notify(pl.DATA_UUID, self._cb())

    async def write(self, b):
        await self.client.write_gatt_char(pl.CTRL_UUID, b, response=True)

    def since(self, t0, uuid=None):
        return [(t, u, b) for t, u, b in self.log if t >= t0 and (uuid is None or u == uuid)]


async def find(name):
    seen = {}

    def cb(dev, adv):
        uuids = [u.lower() for u in adv.service_uuids]
        nm = adv.local_name or dev.name or ""
        if pl.SERVICE in uuids or nm.startswith("BarPod-"):
            seen[dev.address] = (dev, nm, pl.SERVICE in uuids, adv.rssi)

    async with BleakScanner(cb):
        await asyncio.sleep(6)
    pods = [v for v in seen.values() if not name or v[1] == name]
    return pods


async def main():
    name = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--name=")), "")
    seconds = next((float(a.split("=", 1)[1]) for a in sys.argv if a.startswith("--seconds=")), 15.0)
    on_usb = "--battery" not in sys.argv
    r = Report()

    # ---- 1. advertisement ----
    pods = await find(name)
    if not pods:
        r.check(False, "no BarPod found in 6 s of scanning")
        return r
    dev, nm, has_uuid, rssi = pods[0]
    r.check(nm.startswith("BarPod-") and len(nm) == 11, f"advertises name {nm!r} ({dev.address}, {rssi} dBm)")
    r.check(has_uuid, "advertisement carries the service UUID 9ba40001")
    if len(pods) > 1:
        r.note(f"{len(pods)} pods in range, using the first (pick one with --name=)")

    # ---- 2. GATT table and reads ----
    ln = Link(dev)
    await ln.connect()
    svc = ln.client.services.get_service(pl.SERVICE)
    r.check(svc is not None, "service 9ba40001 found")
    chars = {c.uuid.lower(): set(c.properties) for c in svc.characteristics} if svc else {}
    r.check(len(chars) == 5, f"{len(chars)} characteristics (expect 5)")
    for uuid, want in PROPS.items():
        got = chars.get(uuid)
        r.check(got == want, f"{uuid[:8]} properties {sorted(got) if got else None} (want exactly {sorted(want)})")
    r.note(f"MTU as Windows reports it: {ln.client.mtu_size}")

    info_b = bytes(await ln.client.read_gatt_char(pl.INFO_UUID))
    info = pl.decode_frame(info_b)
    r.check(info is not None and info_b[:2] == b"\x20\x01", f"INFO reads {len(info_b)} B starting {info_b[:2].hex(' ')}")
    if info:
        good = (info["boot_id"] != 0 and info["tick_hz"] == 32768 and info["odr"] == 208
                and abs(info["accel_lsb"] - pl.ACCEL_LSB) < 1e-9 and abs(info["gyro_lsb"] - pl.GYRO_LSB) < 1e-9)
        r.check(good, f"INFO: boot_id 0x{info['boot_id']:08x}, fw {info['fw_build']}, serial 0x{info['serial']:08x}, "
                      f"ring_n {info['ring_n']}, odr {info['odr']}, wake {info['wake']}")
    cal_b = bytes(await ln.client.read_gatt_char(pl.CAL_UUID))
    r.check(len(cal_b) == 64 and cal_b[:2] == b"\x30\x00" and not any(cal_b[2:]), f"CAL reads {len(cal_b)} B, stub 30 00")
    st_b = bytes(await ln.client.read_gatt_char(pl.STATUS_UUID))
    st = pl.decode_frame(st_b)
    r.check(st is not None, f"STATUS reads {len(st_b)} B, type 0x{st_b[0]:02x}")

    # ---- 3. STATUS at 1 Hz (on DATA), plus one at each link change ----
    await ln.subscribe()
    await asyncio.sleep(max(0.0, 3.5 - (now_ms() - ln.t_connect) / 1000))
    t0 = now_ms()
    await asyncio.sleep(4.2)
    sts = [(t, pl.decode_frame(b)) for t, _, b in ln.since(t0, pl.STATUS_UUID)]
    gaps = [b[0] - a[0] for a, b in zip(sts, sts[1:])]
    r.check(len(sts) >= 4 and max(gaps, default=9e9) <= 1250,
            f"{len(sts)} STATUS notifications on DATA in 4.2 s, spacing {[round(g) for g in gaps]} ms")
    if sts:
        last = sts[-1][1]
        usb = bool(last["flags"] & pl.S_USB)
        r.check(usb == on_usb, f"USB flag {usb} (expect {on_usb}), battery {last['battery_mv']} mV")
        r.check(not last["flags"] & pl.S_MTU_TOO_SMALL and last["n_per_packet"] >= 4,
                f"n_per_packet {last['n_per_packet']}, mtu_too_small clear, interval "
                f"{last['interval'] * 1.25:.2f} ms")
        heads = [s["head"] for _, s in sts]
        r.check(heads == sorted(heads) and heads[-1] > heads[0], f"head rises {heads[0]} -> {heads[-1]}")

    # ---- 4. boot_mismatch ----
    t0 = now_ms()
    bad = (info["boot_id"] ^ 0x5A5A5A5A) or 1
    await ln.write(struct.pack("<BBII", pl.OP_STREAM_FROM, 0x77, bad, 0))
    while now_ms() - t0 < 1200 and not any(pl.decode_frame(b)["flags"] & pl.S_BOOT_MISMATCH
                                             for _, _, b in ln.since(t0, pl.STATUS_UUID)):
        await asyncio.sleep(0.01)
    rd = pl.decode_frame(bytes(await ln.client.read_gatt_char(pl.STATUS_UUID)))   # read right after it
    events = pl.S_BOOT_MISMATCH | pl.S_STREAM_STOPPED | pl.S_GOING_TO_SLEEP
    r.check(rd is not None and not rd["flags"] & events,
            f"a STATUS read right after it is plain (flags 0x{rd['flags']:02x}): event bits only in the one STATUS")
    await asyncio.sleep(max(0.0, 1.2 - (now_ms() - t0) / 1000))
    sts = [pl.decode_frame(b) for _, _, b in ln.since(t0, pl.STATUS_UUID)]
    mism = [s for s in sts if s["flags"] & pl.S_BOOT_MISMATCH]
    r.check(len(mism) == 1 and not mism[0]["flags"] & pl.S_STREAMING and not ln.since(t0, pl.DATA_UUID),
            f"wrong boot_id: {len(mism)} boot_mismatch STATUS, no stream, "
            f"{len(ln.since(t0, pl.DATA_UUID))} DATA")

    # ---- 5. STREAM_LIVE through the reference decoder ----
    samples, events = [], []
    s = pl.PodStream(samples.append, lambda kind, x: events.append((now_ms(), kind, x)))
    ln.stream = s
    s.frame(info_b, now_ms())
    s.frame(bytes(await ln.client.read_gatt_char(pl.STATUS_UUID)), now_ms())   # connection order (ble.md 7)
    cmd = s.command()
    r.check(cmd[0] == pl.OP_STREAM_LIVE, f"PodStream's first command: {cmd.hex(' ')}")
    t_cmd = now_ms()
    await ln.write(cmd)
    print(f"      streaming {seconds:.0f} s ...", flush=True)
    t_end = t_cmd + seconds * 1000
    while now_ms() < t_end:
        await asyncio.sleep(min(30.0, (t_end - now_ms()) / 1000))
        if seconds > 60:
            r.note(f"{(now_ms() - t_cmd) / 1000:.0f} s: {len(samples)} samples, gaps {len(s.gaps)}, "
                   f"faults {len(s.faults)}")
    data = [(t, pl.decode_frame(b)) for t, _, b in ln.since(t_cmd, pl.DATA_UUID)]
    data = [(t, f) for t, f in data if f]
    first_st = next((t for t, _, b in ln.since(t_cmd, pl.STATUS_UUID)
                     if pl.decode_frame(b)["flags"] & pl.S_STREAMING), None)
    r.check(data and data[0][1]["flags"] & pl.F_RESYNC and data[0][1]["cmd_seq"] == cmd[1],
            f"first DATA: RESYNC, cmd_seq {data[0][1]['cmd_seq'] if data else None}")
    r.check(first_st is not None and data and first_st <= data[0][0],
            "the STATUS answering the command (streaming) came before the first DATA (one channel, D28)")
    span = (data[-1][0] - data[0][0]) / 1000 if len(data) > 1 else 0
    sizes = {}
    for _, f in data:
        sizes[14 + 14 * f["n"]] = sizes.get(14 + 14 * f["n"], 0) + 1
    r.check(len(data) > 0 and span > 0, f"{len(data)} DATA frames in {span:.1f} s = {len(data) / max(span, 1e-9):.1f}/s, "
                                         f"sizes {dict(sorted(sizes.items()))}")
    ts = [x["t"] for x in samples]
    rate = (len(ts) - 1) / (ts[-1] - ts[0]) if len(ts) > 1 else 0
    r.check(209 <= rate <= 212.5, f"{len(samples)} samples, {rate:.2f} Hz from the pod's ticks")
    am = statistics.median([(x["ax"] ** 2 + x["ay"] ** 2 + x["az"] ** 2) ** 0.5 for x in samples]) if samples else 0
    r.check(9.3 <= am <= 10.4, f"median |a| {am:.3f} m/s^2 (raw pod, no calibration)")
    r.check(not s.gaps and not s.faults and s.stats["pkt_seq_gaps"] == 0,
            f"decoder: gaps {s.gaps}, faults {s.faults}, pkt_seq gaps {s.stats['pkt_seq_gaps']}, "
            f"dupes {s.stats['dupes']}, stale {s.stats['stale']}")

    # ---- 6. reconnect with STREAM_FROM ----
    n_before = len(samples)
    await ln.client.disconnect()
    await asyncio.sleep(3.0)
    await ln.connect()
    s.frame(bytes(await ln.client.read_gatt_char(pl.INFO_UUID)), now_ms())
    s.frame(bytes(await ln.client.read_gatt_char(pl.STATUS_UUID)), now_ms())
    await ln.subscribe()
    cmd = s.command()
    r.check(cmd[0] == pl.OP_STREAM_FROM, f"after the reconnect PodStream asks {cmd.hex(' ')}")
    await ln.write(cmd)
    await asyncio.sleep(8.0)
    r.check(len(samples) > n_before + 8 * 200 and not s.gaps and not s.faults,
            f"resumed: +{len(samples) - n_before} samples, gaps {s.gaps}, faults {s.faults}, "
            f"reconnects {s.stats['reconnects']}")

    # ---- 7. STOP ----
    stopped = []
    t_stop = now_ms()
    await ln.write(s.stop_command())
    wait = s.stop_wait_ms()
    while now_ms() - t_stop < wait and not any(k == "stopped" for t, k, _ in events if t >= t_stop):
        await asyncio.sleep(0.05)
    stopped = [t for t, k, _ in events if t >= t_stop and k == "stopped"]
    st = next((pl.decode_frame(b) for _, _, b in ln.since(t_stop, pl.STATUS_UUID)
               if pl.decode_frame(b)["flags"] & pl.S_STREAM_STOPPED), None)
    last_idx = s.ep.last_idx if s.ep else None
    r.check(stopped and st is not None and last_idx is not None and st["head"] == last_idx + 1 and not s.gaps,
            f"STOP: stream_stopped after {((stopped[0] if stopped else now_ms()) - t_stop):.0f} ms "
            f"(wait {wait:.0f}), head {st['head'] if st else None}, last index {last_idx}, gaps {s.gaps}")
    finals = [f for _, f in ((t, pl.decode_frame(b)) for t, _, b in ln.since(t_stop, pl.DATA_UUID)) if f["flags"] & pl.F_FINAL]
    r.note(f"FINAL frames after STOP: {len(finals)}")
    await asyncio.sleep(2.0)
    # by arrival order, not time (Windows' monotonic clock ticks in ~15.6 ms steps)
    i_st = next((i for i, (t, k, b) in enumerate(ln.log) if t >= t_stop and k == pl.STATUS_UUID
                 and pl.decode_frame(b)["flags"] & pl.S_STREAM_STOPPED), len(ln.log))
    after = ln.log[i_st + 1:]
    late = [x for x in after if x[1] == pl.DATA_UUID]    # anything after the stream_stopped STATUS
    sts = [pl.decode_frame(b) for _, k, b in after if k == pl.STATUS_UUID]
    r.check(not late and sts and not any(x["flags"] & pl.S_STREAMING for x in sts)
            and s.stats["after_close"] == 0,
            f"after STOP: {len(late)} DATA, {len(sts)} STATUS, streaming flag clear, "
            f"after_close {s.stats['after_close']}")
    await ln.client.disconnect()
    return r


if __name__ == "__main__":
    rep = asyncio.run(main())
    print("PASS" if rep.ok else "FAIL")
    sys.exit(0 if rep.ok else 1)
