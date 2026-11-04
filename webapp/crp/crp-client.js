/**
 * CRP (Channel Register Protocol) Binary Protocol Client — Browser Edition
 *
 * Ported from src/crp-client.js (Node) to run in the browser.
 * Uses Uint8Array instead of Buffer, and a minimal EventEmitter.
 *
 * All multi-byte values are little-endian on the wire.
 */

import { EventEmitter } from './event-emitter.js';
import { allocBytes, concatBytes, copyBytes, fromString, toString } from './byte-utils.js';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

export const PacketType = Object.freeze({
  NOP:          0x00,
  HELLO:        0x01,
  BYE:          0x02,
  WRITE:        0x04,   // txn>0: expects RESPONSE; txn=0: fire-and-forget
  WRITE_NOACK:  0x05,   // Deprecated wire alias — use WRITE with txn=0
  READ:         0x06,
  SUBSCRIBE:    0x08,
  PUSH_SETUP:   0x09,
  QUERY_REG:    0x0A,
  QUERY_GROUP:  0x0B,
  RESPONSE:     0x0C,
  CHANNEL_OPEN: 0x0E,
  BULK_DATA:    0x10,
  SEEK:         0x12,
  END:          0x16,
  LOG:          0x18,
  QUERY_DEVICE: 0x1A,
  UNLOCK:       0x1C,
});

export const Status = Object.freeze({
  OK:            0,
  UNKNOWN_REG:   1,
  ACCESS_DENIED: 2,
  INVALID_VALUE: 3,
  BUSY:          4,
});

export const ErrorCode = Object.freeze({
  UNKNOWN_REG:      1,
  ACCESS_DENIED:    2,
  INVALID_VALUE:    3,
  NO_CHANNELS:      4,
  BAD_CHANNEL:      5,
  NOT_SUBSCRIBABLE: 6,
  INTERVAL_TOO_FAST:7,
  BUSY:             8,
  TRANSFER_FAILED:  9,
});

/** ChanType field: bits 0-4 encode the channel kind */
export const ChanKind = Object.freeze({
  SUBSCRIPTION: 1,
  PUSH:         2,
  BULK_READ:    3,
  BULK_WRITE:   4,
});

/** ChanType flag bits */
export const ChanFlag = Object.freeze({
  ENCRYPTED:    0x40, // bit 6
  SEEK_SUPPORT: 0x80, // bit 7
});

/** Sentinel channel byte used in END to tear down the whole session */
export const SESSION_END_CHANNEL = 0xFF;

// ---------------------------------------------------------------------------
// Little-endian helpers (pure byte-index math — already browser-compatible)
// ---------------------------------------------------------------------------

function readU16LE(buf, offset) {
  return buf[offset] | (buf[offset + 1] << 8);
}

function readU32LE(buf, offset) {
  return (
    (buf[offset]) |
    (buf[offset + 1] << 8) |
    (buf[offset + 2] << 16) |
    ((buf[offset + 3] << 24) >>> 0)   // >>> 0 keeps it unsigned
  );
}

function writeU16LE(buf, value, offset) {
  buf[offset]     = value & 0xFF;
  buf[offset + 1] = (value >>> 8) & 0xFF;
}

function writeU32LE(buf, value, offset) {
  buf[offset]     = value & 0xFF;
  buf[offset + 1] = (value >>> 8) & 0xFF;
  buf[offset + 2] = (value >>> 16) & 0xFF;
  buf[offset + 3] = (value >>> 24) & 0xFF;
}

// ---------------------------------------------------------------------------
// Reverse-lookup maps (for debug / toString)
// ---------------------------------------------------------------------------

const packetName = Object.fromEntries(
  Object.entries(PacketType).map(([k, v]) => [v, k]),
);

const statusName = Object.fromEntries(
  Object.entries(Status).map(([k, v]) => [v, k]),
);

const errorName = Object.fromEntries(
  Object.entries(ErrorCode).map(([k, v]) => [v, k]),
);

// ---------------------------------------------------------------------------
// TxnID Allocator  (round-robin 1..255, 0 reserved for device-initiated)
// ---------------------------------------------------------------------------

class TxnAllocator {
  constructor() {
    this._next = 1;
    this._inUse = new Set();
  }

  /** Allocate the next free TxnID.  Throws if all 255 slots are occupied. */
  alloc() {
    if (this._inUse.size >= 255) {
      throw new Error('CRP: TxnID pool exhausted (255 in flight)');
    }
    const start = this._next;
    while (this._inUse.has(this._next)) {
      this._next = (this._next % 255) + 1;
      if (this._next === start) {
        throw new Error('CRP: TxnID pool exhausted');
      }
    }
    const id = this._next;
    this._inUse.add(id);
    this._next = (id % 255) + 1;
    return id;
  }

  free(id) {
    this._inUse.delete(id);
  }

  reset() {
    this._inUse.clear();
    this._next = 1;
  }
}

// ---------------------------------------------------------------------------
// Channel state tracker
// ---------------------------------------------------------------------------

class ChannelState {
  /**
   * @param {number} channel   0-15
   * @param {number} chanType  raw ChanType byte
   * @param {number} reg       register address
   * @param {number} totalSize total bytes for bulk transfers (0 if N/A)
   * @param {Uint8Array|null} nonce 12-byte nonce for encrypted channels
   */
  constructor(channel, chanType, reg, totalSize = 0, nonce = null) {
    this.channel   = channel;
    this.chanType  = chanType;
    this.kind      = chanType & 0x1F;
    this.encrypted = !!(chanType & ChanFlag.ENCRYPTED);
    this.seekable  = !!(chanType & ChanFlag.SEEK_SUPPORT);
    this.reg       = reg;
    this.totalSize = totalSize;
    this.nonce     = nonce;

    // Bulk-transfer bookkeeping
    this.offset       = 0;          // current expected offset
    this.received     = 0;          // total bytes received so far
    this.gaps         = [];         // [{start, end}] of missing ranges
    this.paused       = false;
    this.chunks       = new Map();  // offset -> Uint8Array (for gap reassembly)
    this.complete     = false;
    this.seekCount    = 0;          // number of SEEKs sent for gap recovery
  }

  /**
   * Record an incoming chunk and update gap bookkeeping.
   * Returns an array of new gaps detected (may be empty).
   */
  recordChunk(offset, payload) {
    const end = offset + payload.length;
    const newGaps = [];

    if (offset > this.offset) {
      // Gap detected: we expected this.offset but got a later offset.
      const gap = { start: this.offset, end: offset };
      this.gaps.push(gap);
      newGaps.push(gap);
    }

    // Even if there was a gap, advance past this chunk.
    if (end > this.offset) {
      this.offset = end;
    }

    // Only count bytes for NEW chunks (avoid double-counting on retransmit)
    if (!this.chunks.has(offset)) {
      this.received += payload.length;
    }
    this.chunks.set(offset, new Uint8Array(payload));

    // Clear/shrink gaps this chunk covers (for SEEK retransmission)
    if (this.gaps.length > 0) {
      this.gaps = this.gaps.filter((g) => {
        if (offset <= g.start && end >= g.end) return false;
        return true;
      });
      for (const g of this.gaps) {
        if (offset <= g.start && end > g.start) g.start = end;
        if (offset < g.end && end >= g.end)     g.end = offset;
      }
      this.gaps = this.gaps.filter((g) => g.start < g.end);
    }

    // Check if bulk transfer is complete (both byte count AND no gaps)
    if (this.totalSize > 0 && this.received >= this.totalSize && this.gaps.length === 0) {
      this.complete = true;
    }

    return newGaps;
  }

  /**
   * Attempt to fill a gap.  Returns true if the gap was fully resolved.
   */
  fillGap(offset, payload) {
    const end = offset + payload.length;
    this.chunks.set(offset, new Uint8Array(payload));
    this.received += payload.length;

    // Remove any gap that this chunk fully covers
    this.gaps = this.gaps.filter((g) => {
      if (offset <= g.start && end >= g.end) return false; // fully covered
      return true;
    });

    // Shrink partially-covered gaps
    for (const g of this.gaps) {
      if (offset <= g.start && end > g.start) g.start = end;
      if (offset < g.end && end >= g.end)     g.end = offset;
    }
    this.gaps = this.gaps.filter((g) => g.start < g.end);

    if (this.totalSize > 0 && this.received >= this.totalSize && this.gaps.length === 0) {
      this.complete = true;
    }
    return this.gaps.length === 0;
  }

  /** Assemble all received chunks into a single contiguous Uint8Array (best-effort). */
  assemble() {
    const sorted = [...this.chunks.entries()].sort((a, b) => a[0] - b[0]);
    return concatBytes(sorted.map(([, buf]) => buf));
  }
}

// ---------------------------------------------------------------------------
// CRPClient
// ---------------------------------------------------------------------------

const DEFAULT_TXN_TIMEOUT_MS  = 5000;
const DEFAULT_TXN_RETRIES     = 2;

export class CRPClient extends EventEmitter {
  /**
   * @param {object} transport  Must expose:
   *   - send(buf: Uint8Array): void
   *   - on('packet', (buf: Uint8Array) => void)
   *   - (optional) on('connected', () => void)
   *   - (optional) on('disconnected', () => void)
   * @param {object} [opts]
   * @param {number} [opts.txnTimeoutMs=5000]
   * @param {number} [opts.txnRetries=2]
   */
  constructor(transport, opts = {}) {
    super();

    if (!transport || typeof transport.send !== 'function') {
      throw new TypeError('CRP: transport must implement send(buffer)');
    }
    this._transport = transport;
    this._txnTimeoutMs = opts.txnTimeoutMs ?? DEFAULT_TXN_TIMEOUT_MS;
    this._txnRetries   = opts.txnRetries   ?? DEFAULT_TXN_RETRIES;

    // Internal state
    this._txnAlloc = new TxnAllocator();
    this._pending  = new Map();  // txnId -> { resolve, reject, timer, retries, packet }
    this._channels = new Map();  // channel (0-15) -> ChannelState
    this._peerHello = null;
    this._connected = false;
    this._closed    = false;
    this._deviceId  = 0;
    this._sessionBase = 0;
    this._heartbeatTimer = null;
    this._heartbeatMs = opts.heartbeatMs ?? 1000;

    // Wire up transport events
    this._onPacket = (buf) => this._handlePacket(buf);
    this._transport.on('packet', this._onPacket);

    if (typeof this._transport.on === 'function') {
      this._transport.on('connected', () => {
        this._connected = true;
        this.emit('connected');
      });
      this._transport.on('disconnected', () => {
        this._connected = false;
        this._stopHeartbeat();
        this._abortAllPending(new Error('CRP: transport disconnected'));
        this.emit('disconnected');
      });
    }
  }

  // -----------------------------------------------------------------------
  // Public API -- outbound packets
  // -----------------------------------------------------------------------

  /**
   * Send a HELLO packet.
   */
  sendHello(config) {
    const {
      version = 0x0B,
      maxChannels = 4,
      maxChunkSize = 512,
      flags = 0,
      deviceId = 0,
    } = config;

    const buf = allocBytes(1 + 1 + 1 + 2 + 1 + 4); // 10 bytes
    let off = 0;

    buf[off++] = PacketType.HELLO;
    buf[off++] = version & 0xFF;
    buf[off++] = maxChannels & 0xFF;
    writeU16LE(buf, maxChunkSize, off); off += 2;
    buf[off++] = flags & 0xFF;
    writeU32LE(buf, deviceId >>> 0, off); off += 4;

    this._deviceId = deviceId;
    this._send(buf);
  }

  /**
   * Read a single register.
   * @param {number} reg  32-bit register address
   * @returns {Promise<{status: number, reg: number, payload: Uint8Array}>}
   */
  read(reg) {
    const txnId = this._txnAlloc.alloc();
    const buf = allocBytes(1 + 1 + 4);
    buf[0] = PacketType.READ;
    buf[1] = txnId;
    writeU32LE(buf, reg >>> 0, 2);
    return this._sendWithTxn(txnId, buf);
  }

  /**
   * Bulk read: sends a READ with offset+length appended.
   */
  readBulk(reg, offset, length) {
    return this.read(reg);
  }

  /**
   * Write a register value.
   * @param {number} reg
   * @param {Uint8Array} payload
   * @returns {Promise<{status: number, reg: number}>}
   */
  write(reg, payload) {
    const txnId = this._txnAlloc.alloc();
    const pl = new Uint8Array(payload);
    const buf = allocBytes(1 + 1 + 4 + pl.length);
    buf[0] = PacketType.WRITE;
    buf[1] = txnId;
    writeU32LE(buf, reg >>> 0, 2);
    copyBytes(pl, buf, 6);
    return this._sendWithTxn(txnId, buf);
  }

  /**
   * Fire-and-forget write (WRITE with txn=0, no RESPONSE expected).
   */
  writeNoack(reg, payload) {
    const pl = new Uint8Array(payload);
    const buf = allocBytes(1 + 1 + 4 + pl.length);
    buf[0] = PacketType.WRITE;
    buf[1] = 0; // txn=0 → no ack
    writeU32LE(buf, reg >>> 0, 2);
    copyBytes(pl, buf, 6);
    this._send(buf);
  }

  /**
   * Subscribe to one or more registers at the given interval.
   * @param {number[]} regs        Array of 32-bit register addresses
   * @param {number}   intervalUs  Reporting interval in microseconds
   */
  subscribe(regs, intervalUs) {
    const txnId = this._txnAlloc.alloc();
    const buf = allocBytes(1 + 1 + 4 + regs.length * 4);
    buf[0] = PacketType.SUBSCRIBE;
    buf[1] = txnId;
    writeU32LE(buf, intervalUs >>> 0, 2);
    let off = 6;
    for (const r of regs) {
      writeU32LE(buf, r >>> 0, off);
      off += 4;
    }
    return this._sendWithTxn(txnId, buf);
  }

  /**
   * Query a register group.
   * @param {number} base  32-bit group base address
   */
  queryGroup(base) {
    const txnId = this._txnAlloc.alloc();
    const buf = allocBytes(1 + 1 + 4);
    buf[0] = PacketType.QUERY_GROUP;
    buf[1] = txnId;
    writeU32LE(buf, base >>> 0, 2);
    return this._sendWithTxn(txnId, buf);
  }

  /**
   * Query device-level metadata at a base address.
   * @param {number} base  32-bit base address (0 for direct device)
   */
  queryDevice(base) {
    const txnId = this._txnAlloc.alloc();
    const buf = allocBytes(1 + 1 + 4);
    buf[0] = PacketType.QUERY_DEVICE;
    buf[1] = txnId;
    writeU32LE(buf, base >>> 0, 2);
    return this._sendWithTxn(txnId, buf);
  }

  /**
   * Query a single register's metadata.
   * @param {number} reg  32-bit register address
   */
  queryReg(reg) {
    const txnId = this._txnAlloc.alloc();
    const buf = allocBytes(1 + 1 + 4);
    buf[0] = PacketType.QUERY_REG;
    buf[1] = txnId;
    writeU32LE(buf, reg >>> 0, 2);
    return this._sendWithTxn(txnId, buf);
  }

  /**
   * Unlock a locked register group.
   * @param {number} group  Group base address
   * @param {Uint8Array} proof  32-byte HMAC-SHA256 proof
   */
  unlock(group, proof) {
    const txnId = this._txnAlloc.alloc();
    const buf = allocBytes(1 + 1 + 4 + 32);
    buf[0] = PacketType.UNLOCK;
    buf[1] = txnId;
    writeU32LE(buf, group >>> 0, 2);
    if (proof) buf.set(proof.slice(0, 32), 6);
    return this._sendWithTxn(txnId, buf);
  }


  /**
   * Send an END packet for the specified channel (0-15) or 0xFF for session end.
   */
  endChannel(channel) {
    const buf = allocBytes(2);
    buf[0] = PacketType.END;
    buf[1] = channel & 0xFF;
    this._send(buf);
    this._channels.delete(channel);
  }

  /**
   * Seek to a specific offset on a channel.
   */
  seek(channel, offset) {
    const buf = allocBytes(1 + 1 + 4);
    buf[0] = PacketType.SEEK;
    buf[1] = channel & 0xFF;
    writeU32LE(buf, offset >>> 0, 2);
    this._send(buf);

    const ch = this._channels.get(channel);
    if (ch) {
      ch.offset = offset;
      ch.paused = false;
    }
  }

  /**
   * Pause a channel by seeking to the sentinel offset 0xFFFFFFFF.
   */
  pause(channel) {
    this.seek(channel, 0xFFFFFFFF);
    const ch = this._channels.get(channel);
    if (ch) ch.paused = true;
  }

  /**
   * Resume a paused channel by seeking back to its current offset.
   */
  resume(channel) {
    const ch = this._channels.get(channel);
    if (!ch) return;
    this.seek(channel, ch.offset);
    ch.paused = false;
  }

  /** Send a NOP (keep-alive). */
  sendNop() {
    const buf = allocBytes(1);
    buf[0] = PacketType.NOP;
    this._send(buf);
  }

  /**
   * Gracefully close the session: send BYE, abort pending transactions,
   * clear channels.
   */
  close() {
    if (this._closed) return;
    this._closed = true;

    this._stopHeartbeat();

    // Send BYE
    const buf = allocBytes(1 + 4 + 4);
    buf[0] = PacketType.BYE;
    writeU32LE(buf, this._sessionBase >>> 0, 1);
    writeU32LE(buf, this._deviceId >>> 0, 5);
    this._send(buf);

    // Cleanup
    this._abortAllPending(new Error('CRP: client closed'));
    this._channels.clear();
    this._txnAlloc.reset();

    // Detach transport listener
    if (typeof this._transport.removeListener === 'function') {
      this._transport.removeListener('packet', this._onPacket);
    } else if (typeof this._transport.off === 'function') {
      this._transport.off('packet', this._onPacket);
    }
  }

  // -----------------------------------------------------------------------
  // Channel accessors
  // -----------------------------------------------------------------------

  /** @returns {ChannelState|undefined} */
  getChannel(channel) {
    return this._channels.get(channel);
  }

  /** @returns {Map<number, ChannelState>} */
  get channels() {
    return this._channels;
  }

  /** @returns {object|null} The peer HELLO data, or null if not received yet. */
  get peerHello() {
    return this._peerHello;
  }

  // -----------------------------------------------------------------------
  // Internal -- sending
  // -----------------------------------------------------------------------

  /** Low-level send through transport. */
  _send(buf) {
    // LOCAL PATCH (pod, D45) J2: hand back what the transport's send returned (a promise for a
    // Bluetooth write), so a transaction can fail with it
    return this._transport.send(buf);
  }

  /**
   * LOCAL PATCH (pod, D45) J2: a send whose promise rejects (a write the link refused) fails its
   * transaction at once, instead of 5 s later as a timeout. Only while it's still this entry's
   * txn: a late rejection never touches a reused id.
   */
  _watchSend(txnId, entry, sent) {
    if (!sent || typeof sent.then !== 'function') return;
    sent.then(undefined, (err) => {
      if (this._pending.get(txnId) !== entry) return;
      this._rejectTxn(txnId, err instanceof Error ? err : new Error(`CRP: send failed: ${err}`));
    });
  }

  /**
   * Send a packet that expects a RESPONSE keyed by txnId.
   * Returns a Promise that resolves/rejects based on the reply or timeout.
   */
  _sendWithTxn(txnId, packet) {
    return new Promise((resolve, reject) => {
      const entry = {
        resolve,
        reject,
        packet,
        retries: 0,
        timer: null,
      };
      this._pending.set(txnId, entry);
      this._startTxnTimer(txnId, entry);
      this._watchSend(txnId, entry, this._send(packet));   // LOCAL PATCH (pod, D45) J2
    });
  }

  _startTxnTimer(txnId, entry) {
    if (entry.timer) clearTimeout(entry.timer);
    entry.timer = setTimeout(() => {
      this._onTxnTimeout(txnId);
    }, this._txnTimeoutMs);
  }

  _onTxnTimeout(txnId) {
    const entry = this._pending.get(txnId);
    if (!entry) return;

    if (entry.retries < this._txnRetries) {
      entry.retries++;
      this._startTxnTimer(txnId, entry);
      this._watchSend(txnId, entry, this._send(entry.packet)); // retry; LOCAL PATCH (pod, D45) J2
    } else {
      this._pending.delete(txnId);
      this._txnAlloc.free(txnId);
      entry.reject(new Error(
        `CRP: txn ${txnId} timed out after ${entry.retries} retries`,
      ));
    }
  }

  _resolveTxn(txnId, value) {
    const entry = this._pending.get(txnId);
    if (!entry) return;
    if (entry.timer) clearTimeout(entry.timer);
    this._pending.delete(txnId);
    this._txnAlloc.free(txnId);
    entry.resolve(value);
  }

  _rejectTxn(txnId, err) {
    const entry = this._pending.get(txnId);
    if (!entry) return;
    if (entry.timer) clearTimeout(entry.timer);
    this._pending.delete(txnId);
    this._txnAlloc.free(txnId);
    entry.reject(err);
  }

  _abortAllPending(err) {
    for (const [txnId, entry] of this._pending) {
      if (entry.timer) clearTimeout(entry.timer);
      this._txnAlloc.free(txnId);
      entry.reject(err);
    }
    this._pending.clear();
  }

  // -----------------------------------------------------------------------
  // Internal -- incoming packet dispatcher
  // -----------------------------------------------------------------------

  _handlePacket(buf) {
    if (!buf || buf.length < 1) return;

    const type = buf[0];

    switch (type) {
      case PacketType.HELLO:        return this._onHello(buf);
      case PacketType.BYE:          return this._onBye(buf);
      case PacketType.RESPONSE:     return this._onResponse(buf);
      case PacketType.CHANNEL_OPEN: return this._onChannelOpen(buf);
      case PacketType.BULK_DATA:    return this._onBulkData(buf);
      case PacketType.SEEK:         return this._onSeek(buf);
      case PacketType.END:          return this._onEnd(buf);
      case PacketType.WRITE:        return this._onWrite(buf);
      case PacketType.WRITE_NOACK:  return this._onWriteNoack(buf);
      case PacketType.READ:         return this._onRead(buf);
      case PacketType.SUBSCRIBE:    return this._onSubscribe(buf);
      case PacketType.PUSH_SETUP:   return this._onPushSetup(buf);
      case PacketType.QUERY_GROUP:  return this._onQueryGroup(buf);
      case PacketType.QUERY_REG:    return this._onQueryReg(buf);
      case PacketType.NOP:          return; // silently ignore
      default:
        this.emit('unknown-packet', { type, raw: buf });
    }
  }

  // ---- HELLO (inbound from peer / device) --------------------------------

  _onHello(buf) {
    // type(1)+ver(1)+maxch(1)+maxchunk(2)+flags(1)+regbits(1)+devid(4)
    if (buf.length < 10) return;

    const version     = buf[1];
    const maxChannels = buf[2];
    const maxChunkSize = readU16LE(buf, 3);
    const flags       = buf[5];
    const deviceId    = readU32LE(buf, 6);

    this._peerHello = {
      version,
      maxChannels,
      maxChunkSize,
      flags,
      deviceId,
    };

    this._startHeartbeat();
    this.emit('hello', this._peerHello);
  }

  _startHeartbeat() {
    this._stopHeartbeat();
    if (this._heartbeatMs <= 0) return;
    this._heartbeatTimer = setInterval(() => {
      if (this._closed) return;
      this.sendNop();
    }, this._heartbeatMs);
  }

  _stopHeartbeat() {
    if (this._heartbeatTimer !== null) {
      clearInterval(this._heartbeatTimer);
      this._heartbeatTimer = null;
    }
  }

  // ---- BYE ---------------------------------------------------------------

  _onBye(buf) {
    // type(1)+base(4)+devid(4)
    if (buf.length < 9) return;
    const base     = readU32LE(buf, 1);
    const deviceId = readU32LE(buf, 5);

    const info = { base, deviceId };
    this.emit('bye', info);

    // Clean up
    this._abortAllPending(new Error('CRP: peer sent BYE'));
    this._channels.clear();
    this._txnAlloc.reset();
  }

  // ---- RESPONSE ----------------------------------------------------------

  _onResponse(buf) {
    // type(1)+txn(1)+status(1)+channel(1)+reg(4)+payload(var)
    if (buf.length < 8) return;
    const txnId   = buf[1];
    const status  = buf[2];
    const channel = buf[3];
    const reg     = readU32LE(buf, 4);
    const payload = buf.length > 8 ? new Uint8Array(buf.slice(8)) : allocBytes(0);

    if (status !== 0) {
      const codeName = errorName[status] ?? statusName[status] ?? `0x${status.toString(16)}`;
      this.emit('error', { txnId, channel, code: status, reg });

      if (txnId !== 0) {
        // LOCAL PATCH (pod, D45) J1: the Error carries the status number and the register, so a
        // caller can tell BUSY (8, retried) from the rest without parsing the message
        const err = new Error(
          `CRP ERROR: code=${codeName} reg=0x${reg.toString(16)} ch=${channel}`,
        );
        err.code = status;
        err.reg = reg;
        this._rejectTxn(txnId, err);
      }
      return;
    }

    const result = { txnId, status, reg, payload };
    this.emit('response', result);
    this._resolveTxn(txnId, result);
  }

  // ---- CHANNEL_OPEN ------------------------------------------------------

  _onChannelOpen(buf) {
    // Fixed layout: type(1)+txn(1)+ch(1)+chantype(1)+reg(4)+totalsize(4)+nonce(12) = 24
    if (buf.length < 24) return;
    const txnId    = buf[1];
    const channel  = buf[2];
    const chanType = buf[3];
    const reg      = readU32LE(buf, 4);
    const totalSize = readU32LE(buf, 8);
    const nonce    = new Uint8Array(buf.slice(12, 24));

    // Track channel state
    const state = new ChannelState(channel, chanType, reg, totalSize, nonce);
    this._channels.set(channel, state);

    const info = { txnId, channel, chanType, reg, totalSize, nonce };
    this.emit('channel-open', info);

    // Resolve pending txn if applicable
    if (txnId !== 0) {
      this._resolveTxn(txnId, { ...info, channelState: state });
    }
  }

  // ---- BULK_DATA ---------------------------------------------------------

  _onBulkData(buf) {
    // type(1)+ch(1)+offset(4)+payload(var)
    if (buf.length < 6) return;
    const channel = buf[1];
    const offset  = readU32LE(buf, 2);
    const payload = new Uint8Array(buf.slice(6));

    const ch = this._channels.get(channel);
    if (ch) {
      const newGaps = ch.recordChunk(offset, payload);
      if (newGaps.length > 0) {
        this.emit('gap-detected', { channel, gaps: newGaps });
        // Auto-SEEK recovery: request retransmission from start of first gap
        if (ch.seekable && ch.kind === ChanKind.BULK_READ) {
          this.seek(channel, newGaps[0].start);
          ch.seekCount++;
        }
      }
      if (ch.complete) {
        this.emit('bulk-complete', { channel, data: ch.assemble() });
      }
    }

    // Emit for subscription channels as subscription-data,
    // otherwise as bulk-data.
    if (ch && ch.kind === ChanKind.SUBSCRIPTION) {
      this.emit('subscription-data', { channel, offset, payload });
    } else {
      this.emit('bulk-data', { channel, offset, payload });
    }
  }

  // ---- SEEK (inbound) ----------------------------------------------------

  _onSeek(buf) {
    // type(1)+ch(1)+offset(4)
    if (buf.length < 6) return;
    const channel = buf[1];
    const offset  = readU32LE(buf, 2);

    const ch = this._channels.get(channel);
    if (ch) {
      if (offset === 0xFFFFFFFF) {
        ch.paused = true;
      } else {
        ch.offset = offset;
        ch.paused = false;
      }
    }

    this.emit('seek', { channel, offset });
  }

  // ---- END (inbound) -----------------------------------------------------

  _onEnd(buf) {
    // type(1)+ch(1)
    if (buf.length < 2) return;
    const channel = buf[1];

    if (channel === SESSION_END_CHANNEL) {
      // Session-level end
      this._abortAllPending(new Error('CRP: peer ended session'));
      this._channels.clear();
      this._txnAlloc.reset();
    } else {
      const ch = this._channels.get(channel);
      // Keep bulk read channels alive if transfer is incomplete (for gap recovery)
      if (!ch || ch.kind !== ChanKind.BULK_READ || ch.complete ||
          (ch.totalSize > 0 && ch.received >= ch.totalSize)) {
        this._channels.delete(channel);
      } else {
        // Detect all missing ranges by scanning chunks
        if (ch.totalSize > 0 && ch.received < ch.totalSize) {
          ch.gaps = []; // Rebuild gaps from actual chunk map
          const sortedOffsets = [...ch.chunks.keys()].sort((a, b) => a - b);
          let expected = 0;
          for (const off of sortedOffsets) {
            if (off > expected) {
              ch.gaps.push({ start: expected, end: off });
            }
            expected = off + ch.chunks.get(off).length;
          }
          if (expected < ch.totalSize) {
            ch.gaps.push({ start: expected, end: ch.totalSize });
          }
        }
      }
    }

    this.emit('end', { channel });
  }

  // ---- Inbound packets that a client would typically not receive but we
  //      handle for completeness (peer acting as client to us). ------------

  _onWrite(buf) {
    if (buf.length < 6) return;
    const txnId   = buf[1];
    const reg     = readU32LE(buf, 2);
    const payload = new Uint8Array(buf.slice(6));
    this.emit('write', { txnId, reg, payload });
  }

  _onWriteNoack(buf) {
    if (buf.length < 6) return;
    const txnId   = buf[1];
    const reg     = readU32LE(buf, 2);
    const payload = new Uint8Array(buf.slice(6));
    this.emit('write', { txnId, reg, payload });
    /* txn=0 from device = unsolicited push-on-change */
    if (txnId === 0) {
      this.emit('push-change', { reg, payload });
    }
  }

  _onRead(buf) {
    if (buf.length < 6) return;
    const txnId = buf[1];
    const reg   = readU32LE(buf, 2);
    let offset  = null;
    let length  = null;
    if (buf.length >= 14) {
      offset = readU32LE(buf, 6);
      length = readU32LE(buf, 10);
    }
    this.emit('read', { txnId, reg, offset, length });
  }

  _onSubscribe(buf) {
    if (buf.length < 6) return;
    const txnId      = buf[1];
    const intervalUs = readU32LE(buf, 2);
    const regs = [];
    for (let off = 6; off + 3 < buf.length; off += 4) {
      regs.push(readU32LE(buf, off));
    }
    this.emit('subscribe', { txnId, intervalUs, regs });
  }

  _onPushSetup(buf) {
    if (buf.length < 6) return;
    const txnId      = buf[1];
    const intervalUs = readU32LE(buf, 2);
    const regs = [];
    for (let off = 6; off + 3 < buf.length; off += 4) {
      regs.push(readU32LE(buf, off));
    }
    this.emit('push-setup', { txnId, intervalUs, regs });
  }

  _onQueryGroup(buf) {
    if (buf.length < 6) return;
    const txnId = buf[1];
    const base  = readU32LE(buf, 2);
    this.emit('query-group', { txnId, base });
  }

  _onQueryReg(buf) {
    if (buf.length < 6) return;
    const txnId = buf[1];
    const reg   = readU32LE(buf, 2);
    this.emit('query-reg', { txnId, reg });
  }

  // -----------------------------------------------------------------------
  // Static helpers for building packets
  // -----------------------------------------------------------------------

  static buildResponse(txnId, status, reg, payload) {
    const pl = payload ? new Uint8Array(payload) : allocBytes(0);
    const buf = allocBytes(7 + pl.length);
    buf[0] = PacketType.RESPONSE;
    buf[1] = txnId;
    buf[2] = status;
    writeU32LE(buf, reg >>> 0, 3);
    if (pl.length > 0) copyBytes(pl, buf, 7);
    return buf;
  }

  static buildChannelOpen(txnId, channel, chanType, reg, totalSize, nonce) {
    const kind = chanType & 0x1F;
    const isBulk = (kind === ChanKind.BULK_READ || kind === ChanKind.BULK_WRITE);
    const hasNonce = !!(chanType & ChanFlag.ENCRYPTED) && nonce;

    let size = 8;
    if (isBulk) size += 4;
    if (hasNonce) size += 12;

    const buf = allocBytes(size);
    buf[0] = PacketType.CHANNEL_OPEN;
    buf[1] = txnId;
    buf[2] = channel;
    buf[3] = chanType;
    writeU32LE(buf, reg >>> 0, 4);

    let off = 8;
    if (isBulk) {
      writeU32LE(buf, (totalSize ?? 0) >>> 0, off);
      off += 4;
    }
    if (hasNonce) {
      copyBytes(nonce.slice(0, 12), buf, off);
    }
    return buf;
  }

  static buildError(txnId, channel, code, reg) {
    const buf = allocBytes(8);
    buf[0] = PacketType.ERROR;
    buf[1] = txnId;
    buf[2] = channel;
    buf[3] = code;
    writeU32LE(buf, reg >>> 0, 4);
    return buf;
  }

  static buildBulkData(channel, offset, payload) {
    const pl = new Uint8Array(payload);
    const buf = allocBytes(6 + pl.length);
    buf[0] = PacketType.BULK_DATA;
    buf[1] = channel;
    writeU32LE(buf, offset >>> 0, 2);
    copyBytes(pl, buf, 6);
    return buf;
  }

  static buildEnd(channel) {
    const buf = allocBytes(2);
    buf[0] = PacketType.END;
    buf[1] = channel & 0xFF;
    return buf;
  }
}

// ---------------------------------------------------------------------------
// Named exports for protocol helpers
// ---------------------------------------------------------------------------

export {
  readU16LE,
  readU32LE,
  writeU16LE,
  writeU32LE,
  packetName,
  statusName,
  errorName,
  ChannelState,
  TxnAllocator,
};

export default CRPClient;
