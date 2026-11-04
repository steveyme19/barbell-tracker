"""
CRP 0x0B in Python: Ansync Labs' Channel Register Protocol, the pod's link since D45 (docs/ble.md 2).

Generic CRP, standard library only (the links import bleak / pyserial when they open):
  - packet builders and a parser, byte for byte the vendored engine's crp_packet.c
    (firmware/pod/src/crp);
  - level 1 framing for USB, `[len u16][packet]`, with a deframer that resyncs on a bad length;
  - an asyncio Session: HELLO, READ, WRITE, no retries, txn ids allocated exactly as the vendored
    JS CRPClient does (webapp/crp/crp-client.js), so both send the same bytes;
  - two thin links: SerialLink (level 1) and BleakLink (level 0, Ansync's GATT service).
The pod's registers and frames live in podlink.py, not here.

    import crp
    link = crp.BleakLink(address)              # or crp.SerialLink("COM5")
    s = crp.Session(link, tap=on_packet)       # tap(pkt, arrival_ms): every received packet, first
    await link.open()                          # make the Session first, so no packet goes untapped
    peer = await s.hello()                     # the peer's HELLO, parsed
    status, value = await s.read(reg)
    status = await s.write(reg, value)
    await s.close()                            # BYE
    await link.close()

    python crp.py --selftest     # builders, parser, deframer, Session and SerialLink on fakes (no hardware)
    python crp.py --vectors      # canonical packets as JSON, for webapp/test/crp_wire.mjs
"""

import asyncio
import inspect
import json
import struct
import sys
import threading
import time

# ---- constants (firmware/pod/src/crp/crp/crp_types.h, protocol 0x0B) ----
VERSION = 0x0B
SESSION_CHANNEL = 0xFF        # END 0xFF ends the whole session; a RESPONSE's ch 0xFF = no channel
FLAG_PUSH = 0x01              # HELLO flags: the device pushes (WRITE txn 0)
L1_MAX = 1024                 # level 1: the longest packet a length may announce (docs/ble.md 2.5)
TXN_TIMEOUT_S = 5.0           # CRPClient's default txnTimeoutMs

# packet types
NOP, HELLO, BYE = 0x00, 0x01, 0x02
WRITE, READ = 0x04, 0x06      # WRITE txn 0 = fire and forget (the pod's pushes)
SUBSCRIBE, PUSH_SETUP, QUERY_REG, QUERY_GROUP = 0x08, 0x09, 0x0A, 0x0B
RESPONSE, CHANNEL_OPEN, BULK_DATA, SEEK = 0x0C, 0x0E, 0x10, 0x12
END, LOG, QUERY_DEVICE, UNLOCK = 0x16, 0x18, 0x1A, 0x1C

# RESPONSE status codes
OK, UNKNOWN_REG, ACCESS_DENIED, INVALID_VALUE = 0x00, 0x01, 0x02, 0x03
NO_CHANNELS, BAD_CHANNEL, NOT_SUBSCRIBABLE, INTERVAL_TOO_FAST = 0x04, 0x05, 0x06, 0x07
BUSY, TRANSFER_FAILED, VERSION_MISMATCH = 0x08, 0x09, 0x0A

LOG_ERROR, LOG_WARN, LOG_INFO, LOG_DEBUG = 0, 1, 2, 3

TYPE_NAMES = {NOP: "NOP", HELLO: "HELLO", BYE: "BYE", WRITE: "WRITE", READ: "READ",
              SUBSCRIBE: "SUBSCRIBE", PUSH_SETUP: "PUSH_SETUP", QUERY_REG: "QUERY_REG",
              QUERY_GROUP: "QUERY_GROUP", RESPONSE: "RESPONSE", CHANNEL_OPEN: "CHANNEL_OPEN",
              BULK_DATA: "BULK_DATA", SEEK: "SEEK", END: "END", LOG: "LOG",
              QUERY_DEVICE: "QUERY_DEVICE", UNLOCK: "UNLOCK"}
STATUS_NAMES = {OK: "OK", UNKNOWN_REG: "UNKNOWN_REG", ACCESS_DENIED: "ACCESS_DENIED",
                INVALID_VALUE: "INVALID_VALUE", NO_CHANNELS: "NO_CHANNELS", BAD_CHANNEL: "BAD_CHANNEL",
                NOT_SUBSCRIBABLE: "NOT_SUBSCRIBABLE", INTERVAL_TOO_FAST: "INTERVAL_TOO_FAST", BUSY: "BUSY",
                TRANSFER_FAILED: "TRANSFER_FAILED", VERSION_MISMATCH: "VERSION_MISMATCH"}

# the shortest packet of each type (crp_types.h CRP_MIN_*): the engine drops anything shorter, and
# a type not in here is unknown to it (0x05, the old WRITE_NOACK, included)
MIN_LEN = {NOP: 1, HELLO: 10, BYE: 9, WRITE: 6, READ: 6, SUBSCRIBE: 10, PUSH_SETUP: 10,
           QUERY_REG: 6, QUERY_GROUP: 6, RESPONSE: 8, CHANNEL_OPEN: 24, BULK_DATA: 6, SEEK: 6,
           END: 2, LOG: 7, QUERY_DEVICE: 6, UNLOCK: 38}

# Ansync's Bluetooth convention (docs/ble.md 2.4): every CRP device shares this service
SERVICE_UUID = "00005090-0000-1000-8000-00805f9b34fb"
RX_UUID = "000050b0-0000-1000-8000-00805f9b34fb"     # we write packets here
TX_UUID = "000050b1-0000-1000-8000-00805f9b34fb"     # the device notifies its packets here


class CrpError(Exception):
    pass


class CrpTimeout(CrpError, TimeoutError):
    pass


class CrpClosed(CrpError):
    """A transaction that can't be answered any more: BYE, END 0xFF, close() or the link went down."""


def now_ms():
    return time.time() * 1000       # the arrival clock: wall time, like the browser's Date.now()


def _bytes(x):
    return x.encode() if isinstance(x, str) else bytes(x)


# ---- builders (crp_packet.c's serializer, field for field) ----
def hello(ver=VERSION, max_ch=4, max_chunk=512, flags=0, device_id=0):
    """HELLO, 10 B. The defaults are CRPClient.sendHello's."""
    return struct.pack("<BBBHBI", HELLO, ver, max_ch, max_chunk, flags, device_id)


def bye(base=0, device_id=0):
    """BYE, 9 B. CRPClient's close() sends base 0 and the device_id it put in its HELLO."""
    return struct.pack("<BII", BYE, base, device_id)


def nop():
    return bytes([NOP])


def read(txn, reg, value=b""):
    """READ, 6 B. `value` is the engine's optional bulk total_size suffix; nobody here uses it."""
    return struct.pack("<BBI", READ, txn, reg) + bytes(value)


def write(txn, reg, value):
    return struct.pack("<BBI", WRITE, txn, reg) + bytes(value)


def push(reg, value):
    """A device's push: a WRITE with txn 0, which gets no answer."""
    return write(0, reg, value)


def response(txn, status, reg, value=b"", ch=SESSION_CHANNEL):
    """RESPONSE, 8 + N B. The engine always sends ch 0xFF outside channel errors."""
    return struct.pack("<BBBBI", RESPONSE, txn, status, ch, reg) + bytes(value)


def log(level, ts_ms, module, msg):
    """LOG, 7 + len(module) + len(msg) B. module and msg: str (UTF-8) or bytes."""
    m = _bytes(module)
    if len(m) > 255:
        raise ValueError("CRP: a LOG module name is at most 255 bytes")
    return struct.pack("<BBIB", LOG, level, ts_ms, len(m)) + m + _bytes(msg)


def end(ch=SESSION_CHANNEL):
    return struct.pack("<BB", END, ch)


def query_reg(txn, reg):
    return struct.pack("<BBI", QUERY_REG, txn, reg)


def query_group(txn, base):
    return struct.pack("<BBI", QUERY_GROUP, txn, base)


def query_device(txn, base=0):
    return struct.pack("<BBI", QUERY_DEVICE, txn, base)


# ---- parser (crp_packet.c's deserializer: the same minimum lengths and offsets) ----
def parse(pkt):
    """bytes -> {type, ...fields}, or None if the engine would drop it (empty, unknown type, or
    shorter than its type's minimum). Like the engine, bytes past a fixed-size packet's end are
    ignored, and a NOP is a NOP at any length."""
    b = bytes(pkt)
    if not b:
        return None
    t = b[0]
    need = MIN_LEN.get(t)
    if need is None or len(b) < need:
        return None
    if t == NOP:
        return {"type": t}
    if t == HELLO:
        _, ver, max_ch, max_chunk, flags, dev = struct.unpack_from("<BBBHBI", b)
        return {"type": t, "ver": ver, "max_ch": max_ch, "max_chunk": max_chunk, "flags": flags,
                "device_id": dev}
    if t == BYE:
        _, base, dev = struct.unpack_from("<BII", b)
        return {"type": t, "base": base, "device_id": dev}
    if t in (WRITE, READ):
        _, txn, reg = struct.unpack_from("<BBI", b)
        return {"type": t, "txn": txn, "reg": reg, "value": b[6:]}
    if t == RESPONSE:
        _, txn, status, ch, reg = struct.unpack_from("<BBBBI", b)
        return {"type": t, "txn": txn, "status": status, "ch": ch, "reg": reg, "value": b[8:]}
    if t in (SUBSCRIBE, PUSH_SETUP):
        _, txn, interval = struct.unpack_from("<BBI", b)
        n = ((len(b) - 6) // 4) & 0xFF                  # the engine's reg_count is a u8
        return {"type": t, "txn": txn, "interval_us": interval,
                "regs": list(struct.unpack_from(f"<{n}I", b, 6))}
    if t == QUERY_REG:
        _, txn, reg = struct.unpack_from("<BBI", b)
        return {"type": t, "txn": txn, "reg": reg}
    if t in (QUERY_GROUP, QUERY_DEVICE):
        _, txn, base = struct.unpack_from("<BBI", b)
        return {"type": t, "txn": txn, "base": base}
    if t == CHANNEL_OPEN:
        _, txn, ch, chan_type, reg, total = struct.unpack_from("<BBBBII", b)
        return {"type": t, "txn": txn, "ch": ch, "chan_type": chan_type, "reg": reg,
                "total_size": total, "nonce": b[12:24]}
    if t == BULK_DATA:
        _, ch, offset = struct.unpack_from("<BBI", b)
        return {"type": t, "ch": ch, "offset": offset, "value": b[6:]}
    if t == SEEK:
        _, ch, offset = struct.unpack_from("<BBI", b)
        return {"type": t, "ch": ch, "offset": offset}
    if t == END:
        return {"type": t, "ch": b[1]}
    if t == LOG:
        _, level, ts, modlen = struct.unpack_from("<BBIB", b)
        if len(b) < 7 + modlen:
            return None                                 # the module name runs past the end
        return {"type": t, "level": level, "ts_ms": ts, "module": b[7:7 + modlen], "msg": b[7 + modlen:]}
    if t == UNLOCK:
        _, txn, group = struct.unpack_from("<BBI", b)
        return {"type": t, "txn": txn, "group": group, "proof": b[6:38]}
    return None


def build(p):
    """parse()'s inverse: a parsed dict back to bytes (a canonical packet round-trips exactly)."""
    t = p["type"]
    if t == NOP:
        return nop()
    if t == HELLO:
        return hello(p["ver"], p["max_ch"], p["max_chunk"], p["flags"], p["device_id"])
    if t == BYE:
        return bye(p["base"], p["device_id"])
    if t == WRITE:
        return write(p["txn"], p["reg"], p["value"])
    if t == READ:
        return read(p["txn"], p["reg"], p["value"])
    if t == RESPONSE:
        return response(p["txn"], p["status"], p["reg"], p["value"], p["ch"])
    if t in (SUBSCRIBE, PUSH_SETUP):
        return struct.pack(f"<BBI{len(p['regs'])}I", t, p["txn"], p["interval_us"], *p["regs"])
    if t == QUERY_REG:
        return query_reg(p["txn"], p["reg"])
    if t == QUERY_GROUP:
        return query_group(p["txn"], p["base"])
    if t == QUERY_DEVICE:
        return query_device(p["txn"], p["base"])
    if t == CHANNEL_OPEN:
        if len(p["nonce"]) != 12:
            raise ValueError("CRP: a CHANNEL_OPEN nonce is 12 bytes")
        return struct.pack("<BBBBII", t, p["txn"], p["ch"], p["chan_type"], p["reg"],
                           p["total_size"]) + bytes(p["nonce"])
    if t == BULK_DATA:
        return struct.pack("<BBI", t, p["ch"], p["offset"]) + bytes(p["value"])
    if t == SEEK:
        return struct.pack("<BBI", t, p["ch"], p["offset"])
    if t == END:
        return end(p["ch"])
    if t == LOG:
        return log(p["level"], p["ts_ms"], p["module"], p["msg"])
    if t == UNLOCK:
        if len(p["proof"]) != 32:
            raise ValueError("CRP: an UNLOCK proof is 32 bytes")
        return struct.pack("<BBI", t, p["txn"], p["group"]) + bytes(p["proof"])
    raise ValueError(f"CRP: no packet type 0x{t:02x}")


# ---- level 1 framing (USB): [len u16][packet] ----
def frame_l1(pkt):
    n = len(pkt)
    if not 1 <= n <= L1_MAX:
        raise ValueError(f"CRP: a level 1 packet is 1..{L1_MAX} bytes, not {n}")
    return n.to_bytes(2, "little") + bytes(pkt)


class Deframer:
    """Level 1 bytes in, whole packets out. A length of 0 or above 1024 drops one byte and rescans;
    a partial packet waits for the rest. Text can't pass for a length (docs/ble.md 2.5): the high
    byte of any pair of text bytes is at least 0x0A, so the length is at least 2560. That also
    holds for the last text byte plus the low byte of a packet of 5 bytes or more (0x05xx and up),
    which is why the text before the pod's HELLO reply (10 B) can't hide it."""

    def __init__(self):
        self.buf = bytearray()
        self.dropped = 0            # bytes thrown away while rescanning

    @property
    def pending(self):
        return len(self.buf)

    def feed(self, data):
        self.buf += data
        b = self.buf
        out = []
        i = 0
        while len(b) - i >= 2:
            n = b[i] | b[i + 1] << 8
            if n == 0 or n > L1_MAX:
                i += 1                              # not a length: drop one byte and rescan
                self.dropped += 1
                continue
            if len(b) - i - 2 < n:
                break                               # a partial packet: wait for the rest
            out.append(bytes(b[i + 2:i + 2 + n]))
            i += 2 + n
        del b[:i]
        return out


# ---- the session (a client's side) ----
class TxnAllocator:
    """CRPClient's TxnAllocator line for line: round robin over 1..255 from 1, skipping ids still in
    flight. free() doesn't move the pointer; reset() (close, a peer's BYE or END 0xFF) puts it back
    at 1. Same calls, same ids, so Python and JS send the same bytes."""

    def __init__(self):
        self.next = 1
        self.in_use = set()

    def alloc(self):
        if len(self.in_use) >= 255:
            raise CrpError("CRP: TxnID pool exhausted (255 in flight)")
        start = self.next
        while self.next in self.in_use:
            self.next = self.next % 255 + 1
            if self.next == start:
                raise CrpError("CRP: TxnID pool exhausted")
        txn = self.next
        self.in_use.add(txn)
        self.next = txn % 255 + 1
        return txn

    def free(self, txn):
        self.in_use.discard(txn)

    def reset(self):
        self.in_use.clear()
        self.next = 1


class Session:
    """One CRP session over a link: any object with send(pkt) (plain or async) that calls
    link.on_packet(pkt, arrival_ms) on the event loop for every packet it receives, and
    link.on_down() when it drops. The Session sets both callbacks when it's made.

    - tap(pkt, arrival_ms) is called first for every received packet (malformed ones too, and
      after close()), before any transaction matching: a capture sees everything in arrival order.
    - No retries (docs/ble.md 2.2: the engine runs a repeated WRITE again). A transaction that isn't
      answered within `timeout` s raises CrpTimeout; its txn is freed and a late answer is ignored.
    - heartbeat: a NOP every that many seconds once the peer's HELLO is in (CRPClient starts its
      heartbeat there too). None takes the link's default (SerialLink 1 s, BleakLink none), 0 is off.

    Inside the class, hello / read / write / bye / nop are the module's builders (methods aren't in
    scope as bare names)."""

    def __init__(self, link, timeout=TXN_TIMEOUT_S, heartbeat=None, tap=None):
        self.link = link
        self.timeout = timeout
        self.heartbeat = getattr(link, "heartbeat", None) if heartbeat is None else heartbeat
        self.tap = tap
        self.txns = TxnAllocator()
        self.pending = {}           # txn -> future of (status, value)
        self.peer = None            # the peer's latest HELLO, parsed
        self.device_id = 0          # ours, from hello(): close()'s BYE carries it, as CRPClient's does
        self.active = False         # the peer's HELLO came and nothing has ended the session since
        self.ended = None           # why it ended: "bye", "end", "link down" or "closed"
        self.closed = False
        self._hello_wait = None
        self._beat = None
        link.on_packet = self._on_packet
        link.on_down = self._on_down

    # ---- outbound ----
    async def send(self, pkt):
        """One raw packet, no transaction (a NOP, a push, an END)."""
        r = self.link.send(pkt)
        if inspect.isawaitable(r):
            await r

    async def send_nop(self):
        await self.send(nop())

    async def hello(self, ver=VERSION, max_ch=4, max_chunk=512, flags=0, device_id=0, timeout=None):
        """Send HELLO (CRPClient.sendHello's defaults) and wait for the peer's. Returns it parsed.
        A BYE instead (the peer refuses our version) raises CrpError."""
        self.device_id = device_id
        fut = asyncio.get_running_loop().create_future()
        self._hello_wait = fut

        async def go():
            await self.send(hello(ver, max_ch, max_chunk, flags, device_id))
            return await fut
        try:
            return await asyncio.wait_for(go(), self.timeout if timeout is None else timeout)
        except TimeoutError:
            raise CrpTimeout("CRP: no HELLO from the peer") from None
        finally:
            if self._hello_wait is fut:
                self._hello_wait = None

    async def read(self, reg, timeout=None):
        """(status, value) of a READ. Any status comes back as is (BUSY included); a timeout raises."""
        return await self._request(lambda txn: read(txn, reg), timeout)

    async def write(self, reg, value, timeout=None):
        """The status of a WRITE."""
        status, _ = await self._request(lambda txn: write(txn, reg, value), timeout)
        return status

    async def close(self):
        """BYE (base 0, our device_id), then every open transaction fails and the txn ids start
        over, as CRPClient.close() does. A second close() sends nothing."""
        if self.closed:
            return
        self.closed = True
        self._stop_beat()
        try:
            await self.send(bye(0, self.device_id))
        except Exception:
            pass                    # a dead link can't take the BYE, and that's fine
        self._end("closed", reset=True)

    async def _request(self, build_pkt, timeout):
        txn = self.txns.alloc()
        fut = asyncio.get_running_loop().create_future()
        self.pending[txn] = fut     # before sending: an answer can beat the write's own confirmation

        async def go():
            await self.send(build_pkt(txn))
            return await fut
        try:
            return await asyncio.wait_for(go(), self.timeout if timeout is None else timeout)
        except TimeoutError:
            raise CrpTimeout(f"CRP: txn {txn} timed out") from None
        finally:
            if self.pending.get(txn) is fut:        # not answered or aborted: free it here
                del self.pending[txn]
                self.txns.free(txn)

    # ---- inbound ----
    def _on_packet(self, pkt, arrival_ms):
        pkt = bytes(pkt)
        if self.tap:
            self.tap(pkt, arrival_ms)               # first, before any matching
        if self.closed:
            return
        p = parse(pkt)
        if p is None:
            return
        t = p["type"]
        if t == RESPONSE:
            fut = self.pending.pop(p["txn"], None)   # txn 0 or one we gave up on: nothing waits
            if fut is not None:
                self.txns.free(p["txn"])
                if not fut.done():
                    fut.set_result((p["status"], p["value"]))
        elif t == HELLO:
            self.peer = p
            self.active = True
            self.ended = None
            self._start_beat()
            if self._hello_wait is not None and not self._hello_wait.done():
                self._hello_wait.set_result(p)
        elif t == BYE or (t == END and p["ch"] == SESSION_CHANNEL):
            self._end("bye" if t == BYE else "end", reset=True)

    def _on_down(self):
        self._end("link down", reset=False)         # CRPClient frees the txns but keeps the pointer

    def _end(self, why, reset):
        self.active = False
        self.ended = why
        self._stop_beat()                           # CRPClient keeps beating after a peer's BYE; the session's over, so we stop
        err = CrpClosed(f"CRP: session ended ({why})")
        for txn, fut in self.pending.items():
            self.txns.free(txn)
            if not fut.done():
                fut.set_exception(err)
        self.pending.clear()
        if reset:
            self.txns.reset()
        if self._hello_wait is not None and not self._hello_wait.done():
            self._hello_wait.set_exception(CrpError(f"CRP: the peer answered HELLO with {why}"))

    def _start_beat(self):
        self._stop_beat()
        if self.heartbeat and not self.closed:
            self._beat = asyncio.get_running_loop().create_task(self._beat_loop(self.heartbeat))

    def _stop_beat(self):
        if self._beat is not None:
            self._beat.cancel()
            self._beat = None

    async def _beat_loop(self, every):
        while True:
            await asyncio.sleep(every)
            try:
                await self.send(nop())
            except Exception:
                return                              # the link is gone; its on_down says so


# ---- links (thin; used from S6/S7 on, not tested on hardware here) ----
class SerialLink:
    """CRP over USB serial, level 1 framing. A reader thread deframes and hands each packet to the
    event loop with the time its last bytes arrived. `port` is a port name (opened with pyserial)
    or an open serial-like object (read / write / in_waiting), which close() leaves open."""
    level = 1
    heartbeat = 1.0                 # USB clients send a NOP every 1 s (docs/ble.md 2.2)

    def __init__(self, port, baud=115200):
        self.port = port
        self.baud = baud
        self.ser = None
        self.deframer = Deframer()
        self.on_packet = None
        self.on_down = None
        self._loop = None
        self._thread = None
        self._stop = threading.Event()
        self._owned = False

    async def open(self):
        self._loop = asyncio.get_running_loop()
        if isinstance(self.port, str):
            import serial           # pyserial, only for a real port
            self.ser = serial.Serial(self.port, self.baud, timeout=0.05)
            self._owned = True
        else:
            self.ser = self.port
        self._stop.clear()
        self._thread = threading.Thread(target=self._reader, name="crp-serial", daemon=True)
        self._thread.start()
        return self

    def _reader(self):
        try:
            while not self._stop.is_set():
                data = self.ser.read(getattr(self.ser, "in_waiting", 0) or 1)
                if not data:
                    continue
                ms = now_ms()
                for pkt in self.deframer.feed(data):
                    self._loop.call_soon_threadsafe(self._deliver, pkt, ms)
        except Exception:
            if not self._stop.is_set():
                try:
                    self._loop.call_soon_threadsafe(self._down)
                except RuntimeError:
                    pass                            # the loop is already closed

    def _deliver(self, pkt, ms):
        if self.on_packet:
            self.on_packet(pkt, ms)

    def _down(self):
        if self.on_down:
            self.on_down()

    def send(self, pkt):
        self.ser.write(frame_l1(pkt))

    async def close(self):
        self._stop.set()
        if self._thread is not None:
            await asyncio.to_thread(self._thread.join, 1.0)
        if self._owned and self.ser is not None:
            self.ser.close()


class BleakLink:
    """CRP over Bluetooth, level 0: one notification on TX = one packet, one write to RX = one
    packet. Writes go with response, one at a time behind a lock. Needs bleak."""
    level = 0
    heartbeat = None                # Bluetooth clients send no heartbeat (docs/ble.md 2.2)

    def __init__(self, address_or_device, timeout=10.0):
        self.target = address_or_device
        self.timeout = timeout
        self.client = None
        self.on_packet = None
        self.on_down = None
        self._lock = asyncio.Lock()

    async def open(self):
        from bleak import BleakClient, BleakScanner
        dev = self.target
        if isinstance(dev, str):
            dev = await BleakScanner.find_device_by_address(dev, timeout=self.timeout)
            if dev is None:
                raise CrpError(f"CRP: no device at {self.target}")
        self.client = BleakClient(dev, disconnected_callback=lambda _c: self._down())
        await self.client.connect()
        await self.client.start_notify(TX_UUID, self._notified)
        return self

    def _notified(self, _sender, data):
        if self.on_packet:
            self.on_packet(bytes(data), now_ms())

    def _down(self):
        if self.on_down:
            self.on_down()

    @property
    def mtu(self):
        return self.client.mtu_size if self.client else None

    async def send(self, pkt):
        async with self._lock:
            await self.client.write_gatt_char(RX_UUID, bytes(pkt), response=True)

    async def close(self):
        if self.client is not None and self.client.is_connected:
            await self.client.disconnect()


# ---- canonical packets (webapp/test/crp_wire.mjs, firmware/pod/test/host_test.c) ----
def vectors():
    """[{name, hex}]. The pod's registers and frames in here are only examples to fill the packets
    (docs/ble.md 2.3 and 3; podlink.py owns them). The client-side ones are what CRPClient sends
    after `sendHello({})`, `read(INFO)`, `write(CTRL, ...)`, `sendNop()` and `close()`."""
    info_r, status_r, data_r, ctrl_r = 0x00010001, 0x00010002, 0x00010004, 0x00010005
    bpod = 0x444F5042                               # "BPOD": 42 50 4F 44 on the wire
    info = struct.pack("<BBHIIffHHBBHI", 0x20, 1, 10, 0x1234ABCD, 0x89ABCDEF, 0.0023928226, 0.035,
                       208, 8192, 0, 0, 32768, 0)
    status = struct.pack("<BBHIIHHBBH", 0x10, 0x08, 4012, 655360, 4160, 0, 0, 0, 16, 36)
    data = struct.pack("<BBHIIBB", 0x01, 16, 7, 4144, 650000, 0x01, 1)      # 16 samples, RESYNC, cmd_seq 1
    data += b"".join(struct.pack("<H6h", 157 * k, 12 + k, -7 - k, 3, 105 + k, -48, 4093 - k)
                     for k in range(16))
    v = [
        ("hello", hello()),                                         # CRPClient.sendHello({})
        ("pod_hello_ble", hello(VERSION, 1, 244, FLAG_PUSH, bpod)),
        ("pod_hello_usb", hello(VERSION, 1, 1024, FLAG_PUSH, bpod)),
        ("read_info", read(1, info_r)),
        ("write_ctrl", write(2, ctrl_r, b"\x02\x01\x58\x02")),      # STREAM_LIVE cmd_seq 1, back 600
        ("nop", nop()),
        ("bye", bye()),                                             # CRPClient.close() after sendHello({})
        ("response_info", response(1, OK, info_r, info)),
        ("response_busy", response(3, BUSY, info_r)),
        ("push_data", push(data_r, data)),                          # 244 B: exactly MTU 247 - 3
        ("push_status", push(status_r, status)),
        ("log", log(LOG_INFO, 123456, "pod", "link mtu 247 phy 1M")),
        ("response_ctrl_ok", response(2, OK, ctrl_r)),              # the answer to write_ctrl
        ("end_session", end(SESSION_CHANNEL)),
        ("pod_bye", bye(0, bpod)),                                  # the pod's answer to a too-old HELLO
        ("hello_v0a", hello(0x0A)),                                 # a too-old client
    ]
    return [{"name": n, "hex": b.hex()} for n, b in v]


# ---- self-test (no hardware: fakes only) ----
class _FakeLink:
    """A level 0 link in memory. What the Session sends goes to `peer` (a function: packet ->
    reply packets), whose replies arrive on the next loop pass, like notifications. async_send makes
    send() take 10 ms, so a reply beats the write's confirmation, as a BLE write with response can."""
    level = 0

    def __init__(self, peer=None, heartbeat=None, async_send=False):
        self.peer = peer
        self.heartbeat = heartbeat
        self.async_send = async_send
        self.fail = False
        self.sent = []
        self.on_packet = None
        self.on_down = None
        self.t = 1000.0

    def deliver(self, pkt):
        self.t += 1
        self.on_packet(pkt, self.t)

    def send(self, pkt):
        if self.fail:
            raise OSError("the link refused the write")
        self.sent.append(bytes(pkt))
        loop = asyncio.get_running_loop()
        for r in (self.peer(pkt) if self.peer else []):
            loop.call_soon(self.deliver, r)
        if self.async_send:
            return asyncio.sleep(0.01)
        return None


class _FakeSerial:
    """A serial port in memory for SerialLink: what's written goes through a Deframer to `peer`,
    whose replies come back level 1 framed, 3 bytes per read, after the `lead` text."""

    def __init__(self, peer, lead=b""):
        self.peer = peer
        self.rx = bytearray(lead)
        self.cv = threading.Condition()
        self.written = bytearray()
        self.d = Deframer()

    @property
    def in_waiting(self):
        with self.cv:
            return min(3, len(self.rx))

    def read(self, n):
        with self.cv:
            if not self.rx:
                self.cv.wait(0.02)
            k = min(n, 3, len(self.rx))
            out = bytes(self.rx[:k])
            del self.rx[:k]
            return out

    def write(self, data):
        self.written += data
        for p in self.d.feed(data):
            for r in self.peer(p):
                with self.cv:
                    self.rx += frame_l1(r)
                    self.cv.notify()


def _fake_device(reads, writes, hello_reply):
    """A CRP device in a few lines: HELLO gets hello_reply, a READ gets reads[reg] = (status, value),
    a WRITE (txn > 0) writes[reg] = status; an unknown register gets UNKNOWN_REG, None is silence."""
    def answer(pkt):
        p = parse(pkt)
        if p is None:
            return []
        if p["type"] == HELLO:
            return [hello_reply] if hello_reply is not None else []
        if p["type"] == READ:
            if p["reg"] not in reads:
                return [response(p["txn"], UNKNOWN_REG, p["reg"])]
            r = reads[p["reg"]]
            return [] if r is None else [response(p["txn"], r[0], p["reg"], r[1])]
        if p["type"] == WRITE and p["txn"]:
            st = writes.get(p["reg"], UNKNOWN_REG)
            return [] if st is None else [response(p["txn"], st, p["reg"])]
        return []
    return answer


def _check_packets(check):
    # exact bytes, written out from crp_packet.c's offsets (a round trip alone would pass a
    # symmetric mistake such as big-endian on both sides)
    known = [
        ("HELLO, CRPClient's defaults", hello(), "010b0400020000000000"),
        ("HELLO, the pod's on BLE", hello(VERSION, 1, 244, FLAG_PUSH, 0x444F5042), "010b01f4000142504f44"),
        ("BYE", bye(0x01020304, 0x0A0B0C0D), "02040302010d0c0b0a"),
        ("NOP", nop(), "00"),
        ("READ", read(1, 0x00010001), "060101000100"),
        ("WRITE", write(2, 0x00010005, b"\x02\x01\x58\x02"), "04020500010002015802"),
        ("a push (WRITE txn 0)", push(0x00010004, b"\x01\x02"), "040004000100" + "0102"),
        ("RESPONSE BUSY", response(3, BUSY, 0x00010001), "0c0308ff01000100"),
        ("RESPONSE with a value", response(7, OK, 0x00030002, b"\xf7\x00"), "0c0700ff02000300f700"),
        ("LOG", log(LOG_INFO, 0x01020304, "pod", "hi"), "18020403020103706f646869"),
        ("END 0xFF", end(), "16ff"),
        ("QUERY_GROUP", query_group(5, 0x00020000), "0b0500000200"),
    ]
    for name, b, want in known:
        check(b.hex() == want, f"bytes: {name} = {want}")

    # every type round-trips: parse gives the fields, build gives the bytes back
    cases = [
        (nop(), {"type": NOP}),
        (hello(0x0B, 1, 1024, 1, 0x444F5042),
         {"type": HELLO, "ver": 0x0B, "max_ch": 1, "max_chunk": 1024, "flags": 1, "device_id": 0x444F5042}),
        (bye(0, 7), {"type": BYE, "base": 0, "device_id": 7}),
        (read(255, 0xFFFFFFFF), {"type": READ, "txn": 255, "reg": 0xFFFFFFFF, "value": b""}),
        (read(4, 1, b"\x00\x04\x00\x00"), {"type": READ, "txn": 4, "reg": 1, "value": b"\x00\x04\x00\x00"}),
        (write(9, 0x00010006, b"\x01"), {"type": WRITE, "txn": 9, "reg": 0x00010006, "value": b"\x01"}),
        (write(9, 0x00010006, b""), {"type": WRITE, "txn": 9, "reg": 0x00010006, "value": b""}),
        (push(0x00010002, bytes(20)), {"type": WRITE, "txn": 0, "reg": 0x00010002, "value": bytes(20)}),
        (response(1, OK, 0x00010001, bytes(range(32))),
         {"type": RESPONSE, "txn": 1, "status": OK, "ch": 0xFF, "reg": 0x00010001, "value": bytes(range(32))}),
        (response(2, ACCESS_DENIED, 0x00010004, ch=3),
         {"type": RESPONSE, "txn": 2, "status": ACCESS_DENIED, "ch": 3, "reg": 0x00010004, "value": b""}),
        (log(LOG_WARN, 4000000000, "pod", "batt low"),
         {"type": LOG, "level": LOG_WARN, "ts_ms": 4000000000, "module": b"pod", "msg": b"batt low"}),
        (log(LOG_DEBUG, 0, "", ""), {"type": LOG, "level": LOG_DEBUG, "ts_ms": 0, "module": b"", "msg": b""}),
        (end(3), {"type": END, "ch": 3}),
        (query_reg(1, 0x00010001), {"type": QUERY_REG, "txn": 1, "reg": 0x00010001}),
        (query_group(2, 0x00010000), {"type": QUERY_GROUP, "txn": 2, "base": 0x00010000}),
        (query_device(3), {"type": QUERY_DEVICE, "txn": 3, "base": 0}),
    ]
    others = [   # the types the pod never uses, built by hand, so parse covers all of crp_packet.c
        {"type": SUBSCRIBE, "txn": 5, "interval_us": 100000, "regs": [0x00010002, 0x00020001]},
        {"type": PUSH_SETUP, "txn": 6, "interval_us": 0, "regs": [0x00010002]},
        {"type": CHANNEL_OPEN, "txn": 7, "ch": 2, "chan_type": 0x83, "reg": 0x00050001,
         "total_size": 4096, "nonce": bytes(range(12))},
        {"type": BULK_DATA, "ch": 2, "offset": 512, "value": b"abc"},
        {"type": SEEK, "ch": 2, "offset": 0xFFFFFFFF},
        {"type": UNLOCK, "txn": 8, "group": 0x00040000, "proof": bytes(range(32))},
    ]
    cases += [(build(p), p) for p in others]
    sizes = {HELLO: 10, BYE: 9, NOP: 1, QUERY_REG: 6, QUERY_GROUP: 6, QUERY_DEVICE: 6,
             END: 2, SEEK: 6, CHANNEL_OPEN: 24, UNLOCK: 38}      # the fixed-size types
    for b, want in cases:
        p = parse(b)
        name = TYPE_NAMES[want["type"]]
        good = p == want and build(p) == b and (want["type"] not in sizes or len(b) == sizes[want["type"]])
        check(good, f"round trip: {name} {b.hex()[:40]}{'...' if len(b) > 20 else ''}")

    # what the engine drops, parse drops
    bad = [b""] + [bytes([t]) + bytes(n - 2) for t, n in MIN_LEN.items() if n > 1]   # one byte short
    bad += [bytes([t]) + bytes(40) for t in (0x03, 0x05, 0x07, 0x1B, 0x1D, 0x7F, 0xFF)]  # unknown types
    bad.append(bytes([LOG, 2, 0, 0, 0, 0, 5]) + b"pod")                                 # module past the end
    check(all(parse(b) is None for b in bad),
          f"parse drops {len(bad)} malformed packets: empty, each type one byte short, unknown types "
          "(0x05 too), a LOG whose module runs past the end")
    check(parse(b"\x00\x01\x02") == {"type": NOP} and parse(hello() + b"\xff") == parse(hello())
          and parse(bye() + b"\x01")["device_id"] == 0,
          "like the engine, a NOP is a NOP at any length, and bytes past a fixed packet's end are ignored")
    try:
        log(0, 0, "x" * 256, "")
        check(False, "a 256-byte LOG module is refused")
    except ValueError:
        check(True, "a 256-byte LOG module is refused")


def _check_framing(check):
    import random
    pkts = [hello(), read(1, 0x00010001), write(2, 0x00010005, b"\x02\x01\x58\x02"), nop(),
            response(1, OK, 0x00010001, bytes(32)), push(0x00010004, bytes(238)), end(), bytes([LOG]) + bytes(1023)]
    stream = b"".join(frame_l1(p) for p in pkts)
    check(frame_l1(hello())[:4] == b"\x0a\x00\x01\x0b",
          "frame_l1(HELLO) starts 0A 00 01 0B, the bytes that switch the pod's USB to CRP")
    for n in (0, 1025):
        try:
            frame_l1(bytes(n))
            check(False, f"frame_l1 refuses {n} bytes")
        except ValueError:
            check(True, f"frame_l1 refuses {n} bytes")

    d = Deframer()
    check(d.feed(stream) == pkts and d.dropped == 0 and d.pending == 0,
          f"{len(pkts)} packets in one feed, 1 to 1024 bytes, nothing dropped")
    d = Deframer()
    out = []
    for i in range(len(stream)):
        out += d.feed(stream[i:i + 1])
    check(out == pkts and d.dropped == 0, "the same stream one byte at a time")
    good = True
    two = frame_l1(hello()) + frame_l1(response(1, OK, 0x00010001, bytes(32)))
    for i in range(len(two) + 1):
        d = Deframer()
        good &= d.feed(two[:i]) + d.feed(two[i:]) == [hello(), response(1, OK, 0x00010001, bytes(32))]
    check(good, f"two packets split at every one of {len(two) + 1} points")
    d = Deframer()
    f = frame_l1(response(1, OK, 0x00010001, bytes(32)))
    check(d.feed(f[:17]) == [] and d.pending == 17 and d.feed(f[17:]) == [response(1, OK, 0x00010001, bytes(32))],
          "a partial packet waits for the rest")

    pod = hello(VERSION, 1, 1024, FLAG_PUSH, 0x444F5042)
    d = Deframer()
    check(d.feed(b"a,b\n" + frame_l1(pod)) == [pod] and d.dropped == 4,
          "text before the HELLO: 'a,b\\n' then 0A 00 01 0B ... gives the HELLO, 4 bytes dropped")
    text = (b"123456,0.010254,-0.020508,9.810547,0.0350,-0.0700,0.1050\r\n"
            b"# link phy 1M mtu 247 interval 36\n# batt 4.01 V\n17,1")
    d = Deframer()
    check(d.feed(text) + d.feed(frame_l1(pod) + frame_l1(read(1, 0x00010001))) == [pod, read(1, 0x00010001)]
          and d.dropped == len(text), "CSV and # lines, cut mid-line, before the pod's HELLO reply: all dropped")

    garbage = [b"\x00\x00", b"\xff\xff", b"\x01\x04", b"\x00\x00\x00", b"\xa5" * 7, b"oops\r\n"]
    want, s = [], b""
    for g, p in zip(garbage, [hello(), read(2, 0x00010002), response(2, BUSY, 0x00010002),
                              write(3, 0x00010006, b"\x01"), pod, push(0x00010002, bytes(20))]):
        s += g + frame_l1(p)
        want.append(p)
    d = Deframer()
    check(d.feed(s) == want and d.dropped == sum(map(len, garbage)),
          "planted garbage between packets: length 0, 0xFFFF, 1025, zeros, A5s, text; every packet out")

    rnd = random.Random(45)
    good = True
    for _ in range(200):          # text of any length between packets of 5 B or more always resyncs
        ps = [rnd.choice([hello(), read(rnd.randrange(1, 256), 0x00010001), response(1, OK, 2, bytes(rnd.randrange(40))),
                          push(0x00010004, bytes(rnd.randrange(28, 239)))]) for _ in range(5)]
        s = b"".join(bytes(rnd.randrange(0x0A, 0x7F) for _ in range(rnd.randrange(0, 30))) + frame_l1(p) for p in ps)
        d = Deframer()
        out = []
        k = 0
        while k < len(s):
            j = k + rnd.randrange(1, 60)
            out += d.feed(s[k:j])
            k = j
        good &= out == ps
    check(good, "200 random runs: text bytes of any length between packets, fed in random pieces")


async def _check_session(check):
    INFO_R, STATUS_R, CTRL_R, PUSH_R, SILENT = 0x00010001, 0x00010002, 0x00010005, 0x00010006, 0x000100EE
    info = bytes(range(32))
    pod = hello(VERSION, 1, 244, FLAG_PUSH, 0x444F5042)
    dev = _fake_device({INFO_R: (OK, info), STATUS_R: (BUSY, b""), SILENT: None},
                       {CTRL_R: OK, PUSH_R: OK, SILENT: None}, pod)
    loop = asyncio.get_running_loop()

    # the connection order: HELLO, READ, READ, WRITE, WRITE
    taps = []
    link = _FakeLink(dev)
    s = Session(link)

    def tap(pkt, ms):
        q = parse(pkt)
        fut = s.pending.get(q["txn"]) if q and q["type"] == RESPONSE else None
        taps.append((pkt, ms, fut is not None and not fut.done()))
    s.tap = tap
    peer = await s.hello()
    check(link.sent == [hello()] and peer == parse(pod) and s.active,
          "hello() sends CRPClient's default HELLO and returns the peer's, parsed")
    r1 = await s.read(INFO_R)
    r2 = await s.read(STATUS_R)
    w1 = await s.write(PUSH_R, b"\x01")
    w2 = await s.write(CTRL_R, b"\x02\x01\x58\x02")
    check(link.sent[1:] == [read(1, INFO_R), read(2, STATUS_R), write(3, PUSH_R, b"\x01"),
                            write(4, CTRL_R, b"\x02\x01\x58\x02")],
          "txn ids 1, 2, 3, 4 in call order, the bytes the builders give")
    check(r1 == (OK, info) and r2 == (BUSY, b"") and w1 == OK and w2 == OK,
          "read gives (status, value), BUSY included; write gives the status")
    check(not s.pending and not s.txns.in_use, "every answered txn is freed")
    resp = [t for t in taps if t[0][0] == RESPONSE]
    check(len(taps) == 5 and len(resp) == 4 and all(t[2] for t in resp) and [t[1] for t in taps] == [1001, 1002, 1003, 1004, 1005],
          "tap sees every packet first, with its arrival ms, while its txn is still waiting")

    # pushes, LOG and junk are tapped in order and change nothing
    taps.clear()
    junk = [push(0x00010004, bytes(28)), b"\x05\x01\x02\x03\x04\x05\x06", log(LOG_INFO, 5, "pod", "hi"),
            response(0, OK, 0x00010002, bytes(20)), response(99, OK, INFO_R, info), b"\x0c\x01"]
    for p in junk:
        link.deliver(p)
    check([t[0] for t in taps] == junk and not s.pending and s.active and s.txns.next == 5,
          "pushes, LOG, an unknown type, txn 0 and unknown txn RESPONSEs, a short one: tapped, nothing else")

    # a timeout, no retry, the late answer ignored, the next id moves on
    n_sent = len(link.sent)
    t0 = loop.time()
    try:
        await s.read(SILENT, timeout=0.05)
        check(False, "an unanswered READ times out")
    except CrpTimeout:
        dt = loop.time() - t0
        check(0.04 <= dt < 0.5 and link.sent[n_sent:] == [read(5, SILENT)] and 5 not in s.txns.in_use,
              f"an unanswered READ times out after {dt * 1000:.0f} ms, sent once (no retry), its txn freed")
    taps.clear()
    link.deliver(response(5, OK, SILENT, b"late"))
    r = await s.read(INFO_R)
    check(len(taps) == 2 and r == (OK, info) and link.sent[-1] == read(6, INFO_R),
          "the late answer is tapped and ignored; the next READ gets txn 6, not 5 again")

    # an async send whose answer beats its confirmation
    link2 = _FakeLink(dev, async_send=True)
    s2 = Session(link2)
    await s2.hello()
    check(await s2.read(INFO_R) == (OK, info) and await s2.write(CTRL_R, b"\x03") == OK,
          "an async send (BLE write with response): the RESPONSE arriving first still resolves")

    # a send that fails frees its txn and raises
    link.fail = True
    try:
        await s.read(INFO_R)
        check(False, "a failed send raises")
    except OSError:
        check(7 not in s.txns.in_use and not s.pending, "a failed send raises at once and frees its txn")
    link.fail = False
    await s.read(INFO_R)
    check(link.sent[-1] == read(8, INFO_R), "and the pointer moved on, as CRPClient's does (txn 8 next)")

    # concurrent requests, answered in reverse
    link3 = _FakeLink()
    s3 = Session(link3)
    a = asyncio.create_task(s3.read(INFO_R))
    b = asyncio.create_task(s3.read(STATUS_R))
    await asyncio.sleep(0.01)
    link3.deliver(response(2, OK, STATUS_R, b"B"))
    link3.deliver(response(1, OK, INFO_R, b"A"))
    check(await a == (OK, b"A") and await b == (OK, b"B") and link3.sent == [read(1, INFO_R), read(2, STATUS_R)],
          "two READs in flight, answered in reverse order: each gets its own")

    # the session ends: BYE and END 0xFF reset the ids, a dropped link doesn't
    for why, pkt, reset in (("bye", bye(0, 0x444F5042), True), ("end", end(SESSION_CHANNEL), True),
                            ("link down", None, False)):
        link4 = _FakeLink(dev)
        s4 = Session(link4)
        await s4.hello()
        await s4.read(INFO_R)
        task = asyncio.create_task(s4.read(SILENT))
        await asyncio.sleep(0.01)
        link4.deliver(end(3))                       # a channel's END ends nothing
        alive = not task.done()
        if pkt is None:
            link4.on_down()
        else:
            link4.deliver(pkt)
        try:
            await task
            ok = False
        except CrpClosed:
            ok = True
        await s4.read(INFO_R)
        nxt = 1 if reset else 3
        check(alive and ok and s4.ended == why and link4.sent[-1] == read(nxt, INFO_R),
              f"{why}: END on channel 3 ends nothing, then the waiting READ fails with CrpClosed and "
              f"the next txn is {nxt}{' (reset, as CRPClient does)' if reset else ' (no reset)'}")

    # heartbeat: after the peer's HELLO only, stops at close; close sends BYE once with our device_id
    link5 = _FakeLink(dev, heartbeat=0.02)
    s5 = Session(link5)
    await asyncio.sleep(0.07)
    before = link5.sent.count(nop())
    await s5.hello(device_id=0x1234)
    await asyncio.sleep(0.13)
    beats = link5.sent.count(nop())
    await s5.close()
    n = len(link5.sent)
    await asyncio.sleep(0.07)
    await s5.close()
    check(before == 0 and beats >= 3 and link5.sent[n - 1] == bye(0, 0x1234) and len(link5.sent) == n,
          f"heartbeat: no NOP before the peer's HELLO, {beats} in 0.13 s at 20 ms, none after close; "
          "BYE carries our device_id, a second close sends nothing")
    s6 = Session(_FakeLink(dev, heartbeat=0.02), heartbeat=0)
    check(s6.heartbeat == 0 and Session(_FakeLink(heartbeat=0.5)).heartbeat == 0.5,
          "heartbeat=0 turns a link's default off; None takes it")

    # hello that fails
    s7 = Session(_FakeLink(_fake_device({}, {}, None)))
    try:
        await s7.hello(timeout=0.05)
        check(False, "hello() with no answer times out")
    except CrpTimeout:
        check(True, "hello() with no answer times out")
    s8 = Session(_FakeLink(_fake_device({}, {}, bye(0, 0x444F5042))))
    try:
        await s8.hello(ver=0x0A, timeout=1)
        check(False, "a BYE in answer to HELLO fails hello()")
    except CrpTimeout:
        check(False, "a BYE in answer to HELLO fails hello() at once, not by timeout")
    except CrpError:
        check(not s8.active, "a BYE in answer to HELLO (a too-old version) fails hello() at once")

    # the allocator itself, against CRPClient's semantics
    t = TxnAllocator()
    ids = [t.alloc() for _ in range(255)]
    try:
        t.alloc()
        full = False
    except CrpError:
        full = True
    t.free(5)
    t.free(200)
    t.free(3)
    again = [t.alloc(), t.alloc(), t.alloc()]
    check(ids == list(range(1, 256)) and full and again == [3, 5, 200],
          "ids 1..255, the 256th in flight refused, then the freed ones in round-robin order from the pointer")
    t = TxnAllocator()
    seq = []
    for _ in range(257):
        seq.append(t.alloc())
        t.free(seq[-1])
    check(seq[:3] == [1, 2, 3] and seq[254:] == [255, 1, 2], "freed ids still go round robin: ... 255, 1, 2")

    # SerialLink end to end on an in-memory port: text first, packets split 3 bytes per read
    lead = b"a,b\n123,0.5,0.25\r\n# boot\n"
    fake = _FakeSerial(dev, lead)
    sl = SerialLink(fake)
    staps = []
    s9 = Session(sl, heartbeat=0, tap=lambda p, ms: staps.append((p, ms)))
    await sl.open()
    t_open = now_ms()
    peer = await s9.hello()
    r = await s9.read(INFO_R)
    await s9.close()
    await sl.close()
    check(peer == parse(pod) and r == (OK, info) and sl.deframer.dropped == len(lead)
          and bytes(fake.written) == frame_l1(hello()) + frame_l1(read(1, INFO_R)) + frame_l1(bye())
          and [p for p, _ in staps] == [pod, response(1, OK, INFO_R, info)]
          and all(t_open - 1 <= ms <= now_ms() for _, ms in staps) and not sl._thread.is_alive(),
          "SerialLink: level 1 out, text before the HELLO dropped, split replies reassembled, "
          "arrival ms from the reader, the thread stopped")
    check(SerialLink.heartbeat == 1.0 and SerialLink.level == 1 and BleakLink.heartbeat is None and BleakLink.level == 0,
          "link defaults: USB level 1 with a 1 s NOP, Bluetooth level 0 with none")


def _check_vectors(check):
    v = vectors()
    names = [x["name"] for x in v]
    need = ["hello", "pod_hello_ble", "read_info", "write_ctrl", "nop", "bye", "response_info",
            "response_busy", "push_data", "push_status", "log"]
    check(len(set(names)) == len(names) and all(n in names for n in need),
          f"--vectors: {len(v)} unique names, the {len(need)} the notes ask for among them")
    pk = {x["name"]: bytes.fromhex(x["hex"]) for x in v}
    check(all(parse(b) is not None and build(parse(b)) == b for b in pk.values()),
          "every vector parses and rebuilds to itself")
    check(len(pk["push_data"]) == 244 and len(pk["response_info"]) == 40 and len(pk["push_status"]) == 26,
          "push_data is 244 B (MTU 247 - 3), response_info 8 + 32, push_status 6 + 20")
    try:
        import podlink
        dec = podlink.decode_frame
    except Exception as e:                      # podlink.py is someone else's file: don't fail on it
        print(f"SKIP  the vector frames against podlink.decode_frame (podlink didn't import: {e!r})")
        return
    fr = [(n, parse(pk[n])["value"]) for n in ("response_info", "push_data", "push_status")]
    got = [dec(f) for _, f in fr]
    check(all(g is not None for g in got) and [g["type"] for g in got] == [0x20, 0x01, 0x10]
          and got[0]["proto"] == 1 and got[1]["n"] == 16 and got[2]["head"] == got[1]["first_idx"] + 16,
          "the INFO, DATA and STATUS inside them are valid PodLink frames (podlink.decode_frame)")


def selftest():
    res = []

    def check(good, msg):
        res.append(bool(good))
        print(f"{'PASS' if good else 'FAIL'}  {msg}")

    _check_packets(check)
    _check_framing(check)
    asyncio.run(_check_session(check))
    _check_vectors(check)
    ok = all(res)
    print(f"{'PASS' if ok else 'FAIL'}  {sum(res)}/{len(res)} checks")
    return ok


def main():
    if "--selftest" in sys.argv:
        return selftest()
    if "--vectors" in sys.argv:
        print("[\n" + ",\n".join(json.dumps(x) for x in vectors()) + "\n]")
        return True
    print(__doc__)
    return False


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
