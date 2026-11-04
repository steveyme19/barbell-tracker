// Data sources. Every source calls onSample({ t, ax, ay, az, gx, gy, gz })
// with t in seconds, acceleration in m/s^2 (including gravity), rotation in deg/s.
// The algorithm and UI never know which source they're getting (decision D4).
// NativeSensorSource is the Android app's phone sensor (docs/android.md A2). The pod's
// Bluetooth stream (PodLink, docs/ble.md) is decoded by PodStream, a line-for-line port of
// analysis/podlink.py checked by webapp/test/pod.mjs; PodWebSource (bottom) feeds it over Web
// Bluetooth, PodNativeSource over the app's PodLink plugin. Since D45 the frames travel inside
// CRP (docs/ble.md 2): PodCrpLink runs the session over the vendored client, on our own transports.

import { CRPClient, ErrorCode, PacketType, Status } from "./crp/crp-client.js";
import { EventEmitter } from "./crp/event-emitter.js";

/** Live phone sensor via DeviceMotion. Chrome typically delivers ~60 Hz. */
export class PhoneSource {
  constructor(onSample) {
    this.onSample = onSample;
    this.interval = null;            // ms between events, as the browser reports it
    this.handler = (e) => this.handle(e);
  }

  async start() {
    if (!("DeviceMotionEvent" in window)) {
      throw new Error("This browser has no motion sensor support.");
    }
    // iPhones need explicit permission (Android Chrome doesn't, but this is harmless).
    if (typeof DeviceMotionEvent.requestPermission === "function") {
      const res = await DeviceMotionEvent.requestPermission();
      if (res !== "granted") throw new Error("Motion sensor permission was denied.");
    }
    window.addEventListener("devicemotion", this.handler);
  }

  stop() {
    window.removeEventListener("devicemotion", this.handler);
  }

  handle(e) {
    const a = e.accelerationIncludingGravity;
    if (!a || a.x === null || a.x === undefined) return;
    const r = e.rotationRate || {};
    this.interval = e.interval;
    this.onSample({
      t: e.timeStamp / 1000,
      ax: a.x, ay: a.y, az: a.z,
      gx: r.beta ?? 0, gy: r.gamma ?? 0, gz: r.alpha ?? 0,
    });
  }
}

// The native plugin's proxy (see native.js for why it's Capacitor.Plugins).
let barSensors = null;

/**
 * Live phone sensor in the Android app, via the BarSensors plugin
 * (android/.../BarSensorsPlugin.java). Full rate (200+ Hz), full precision, and t from
 * the sensor's own clock. Samples arrive in ~50 ms batches.
 */
export class NativeSensorSource {
  constructor(onSample) {
    this.onSample = onSample;
    this.listener = null;
    this.info = null;                // sensor names and max rates, from the plugin
  }

  async start() {
    barSensors ??= window.Capacitor.Plugins.BarSensors;
    this.listener = await barSensors.addListener("samples", (b) => {
      for (let i = 0; i < b.t.length; i++) {
        this.onSample({ t: b.t[i], ax: b.ax[i], ay: b.ay[i], az: b.az[i], gx: b.gx[i], gy: b.gy[i], gz: b.gz[i] });
      }
    });
    this.info = await barSensors.start();
  }

  async stop() {
    await barSensors?.stop();
    await this.listener?.remove();
    this.listener = null;
  }
}

/**
 * Plays back a recording (from fileio.parseRecording).
 * realtime = true: at the speed it was recorded, like a live session (speed = 10 plays
 *   a whole workout 10x faster, timers and all).
 * realtime = false: all at once, for checking results quickly.
 */
export class ReplaySource {
  constructor(onSample, rec, { realtime = false, speed = 1, onDone = () => {} } = {}) {
    this.onSample = onSample;
    this.rec = rec;
    this.realtime = realtime;
    this.speed = speed;
    this.onDone = onDone;
    this.i = 0;
    this.timer = null;
  }

  async start() {
    const n = this.rec.t.length;
    if (!this.realtime) {
      for (; this.i < n; this.i++) this.emit(this.i);
      this.onDone();
      return;
    }
    const t0 = this.rec.t[0];
    const wall0 = performance.now();
    this.timer = setInterval(() => {
      const now = ((performance.now() - wall0) / 1000) * this.speed;
      while (this.i < n && this.rec.t[this.i] - t0 <= now) this.emit(this.i++);
      if (this.i >= n) { this.stop(); this.onDone(); }
    }, 20);
  }

  stop() {
    clearInterval(this.timer);
    this.timer = null;
  }

  emit(i) {
    const r = this.rec;
    this.onSample({ t: r.t[i], ax: r.ax[i], ay: r.ay[i], az: r.az[i], gx: r.gx[i], gy: r.gy[i], gz: r.gz[i] });
  }
}

// ---------------------------------------------------------------------------------------------
// PodLink v1: the pod's Bluetooth stream (docs/ble.md). A line-for-line port of
// analysis/podlink.py (the reference, decision P1); webapp/test/pod.mjs checks it against the
// Python test vectors (analysis/make_fake_pod.py). Pure code, no DOM: Node imports it too.
// Since D45 the frames travel inside CRP (docs/ble.md 2, analysis/crp.py): podFrameOf() takes a
// frame out of a packet, and PodStream itself never sees CRP.

export const POD = {
  // the CRP service and its two characteristics (docs/ble.md 2.4, D45): Ansync's, shared by every
  // CRP device, so the pod is told apart by its name prefix, HELLO device_id and INFO serial
  SERVICE: "00005090-0000-1000-8000-00805f9b34fb",
  RX: "000050b0-0000-1000-8000-00805f9b34fb",       // we write packets here, one per write
  TX: "000050b1-0000-1000-8000-00805f9b34fb",       // the pod notifies every packet here, one per notification
  NAME_PREFIX: "BarPod-",
  T_DATA: 0x01, T_STATUS: 0x10, T_INFO: 0x20, T_CAL: 0x30,
  F_RESYNC: 0x01, F_OVERFLOW: 0x02, F_IMU_RESTART: 0x04, F_FINAL: 0x08,
  S_USB: 0x01, S_CHARGING: 0x02, S_BATT_LOW: 0x04, S_STREAMING: 0x08,
  S_GOING_TO_SLEEP: 0x10, S_STREAM_STOPPED: 0x20, S_MTU_TOO_SMALL: 0x40, S_BOOT_MISMATCH: 0x80,
  OP_STREAM_FROM: 0x01, OP_STREAM_LIVE: 0x02, OP_STOP: 0x03, OP_SLEEP: 0x10,
  ACCEL_LSB: 0.061 * 4 / 1000 * 9.80665,   // m/s^2 per count at +-8 g
  GYRO_LSB: 4.375 * 8 / 1000,              // deg/s per count at +-1000 deg/s
  TICK_HZ: 32768,
  LIVE_BACK: 600,                          // STREAM_LIVE lead-in for a fresh session (~2.8 s)
  // the pod's CRP registers (docs/ble.md 2.3, D45)
  REG_PROTO: 0x00010000, REG_INFO: 0x00010001, REG_STATUS: 0x00010002, REG_CAL: 0x00010003,
  REG_DATA: 0x00010004, REG_CTRL: 0x00010005, REG_PUSH: 0x00010006,
  REG_LINK_KIND: 0x00030001, REG_LINK_MTU: 0x00030002, REG_LINK_N_MAX: 0x00030003,
  REG_BENCH: 0x00040001,                   // USB only
  DEVICE_ID: 0x444F5042,                   // the pod's HELLO device_id, "BPOD" (42 50 4F 44 on the wire)
};
const SLEEP_REASONS = { 0: "none", 1: "idle", 2: "lid-down", 3: "low battery", 4: "command", 5: "IMU lost" };
const FRAME_READS = [POD.REG_INFO, POD.REG_STATUS, POD.REG_CAL];   // an OK RESPONSE to a READ of these carries a frame
const FRAME_PUSHES = [POD.REG_DATA, POD.REG_STATUS];              // so does a push (WRITE txn 0) to these

/** The u32 difference x as a signed 32-bit number (Python: int32). */
const int32 = (x) => x | 0;

/** The PodLink frame a CRP packet carries (a Uint8Array), or null (docs/ble.md 7, Feeding the
 * decoder): an OK RESPONSE to a READ of INFO, STATUS or CAL gives its value; a push (WRITE txn 0)
 * to DATA or STATUS gives its value; anything else (HELLO, NOP, LOG, BUSY, the answer to a write,
 * a client's WRITE, a short or unknown packet) gives null. Pure: nothing is counted here, so a
 * malformed frame inside a good packet still reaches the decoder's own checks (`bad`). */
export function podFrameOf(pkt) {
  const b = pkt instanceof Uint8Array ? pkt : new Uint8Array(pkt.buffer ?? pkt, pkt.byteOffset ?? 0, pkt.byteLength);
  const dv = new DataView(b.buffer, b.byteOffset, b.byteLength);
  if (b.length >= 8 && b[0] === PacketType.RESPONSE && b[2] === Status.OK) {   // 0C txn status ch reg:u32 value
    if (FRAME_READS.includes(dv.getUint32(4, true))) return b.slice(8);
    return null;
  }
  if (b.length >= 6 && b[0] === PacketType.WRITE && b[1] === 0) {               // 04 00 reg:u32 value
    if (FRAME_PUSHES.includes(dv.getUint32(2, true))) return b.slice(6);
  }
  return null;
}

/** DataView (or bytes) -> frame object, or null if malformed (docs/ble.md 7, Input checks). */
export function decodeFrame(dv) {
  if (!(dv instanceof DataView)) dv = new DataView(dv.buffer ?? dv, dv.byteOffset ?? 0, dv.byteLength);
  if (dv.byteLength < 1) return null;
  const type = dv.getUint8(0);
  const len = dv.byteLength;
  if (type === POD.T_DATA) {
    if (len < 14 + 14 || (len - 14) % 14) return null;
    const n = dv.getUint8(1);
    if (n < 1 || n > 16 || len !== 14 + 14 * n) return null;
    const samples = [];
    for (let k = 0; k < n; k++) {
      const o = 14 + 14 * k;
      samples.push([dv.getUint16(o, true), dv.getInt16(o + 2, true), dv.getInt16(o + 4, true),
        dv.getInt16(o + 6, true), dv.getInt16(o + 8, true), dv.getInt16(o + 10, true), dv.getInt16(o + 12, true)]);
    }
    return { type, n, pkt_seq: dv.getUint16(2, true), first_idx: dv.getUint32(4, true),
      t0: dv.getUint32(8, true), flags: dv.getUint8(12), cmd_seq: dv.getUint8(13), samples };
  }
  if (type === POD.T_STATUS) {
    if (len !== 20) return null;
    return { type, flags: dv.getUint8(1), battery_mv: dv.getUint16(2, true), ticks_now: dv.getUint32(4, true),
      head: dv.getUint32(8, true), lost_overflow: dv.getUint16(12, true), missed_drdy: dv.getUint16(14, true),
      sleep_reason: dv.getUint8(16), n_per_packet: dv.getUint8(17), interval: dv.getUint16(18, true) };
  }
  if (type === POD.T_INFO) {
    if (len !== 32) return null;
    return { type, proto: dv.getUint8(1), fw_build: dv.getUint16(2, true), boot_id: dv.getUint32(4, true),
      serial: dv.getUint32(8, true), accel_lsb: dv.getFloat32(12, true), gyro_lsb: dv.getFloat32(16, true),
      odr: dv.getUint16(20, true), ring_n: dv.getUint16(22, true), wake: dv.getUint8(24), tick_hz: dv.getUint16(26, true) };
  }
  if (type === POD.T_CAL) return len === 64 ? { type } : null;
  return null;
}

/** Everything that belongs to one boot of the pod (docs/ble.md 7, Epochs). */
class PodEpoch {
  constructor(info, isReboot) {
    this.boot_id = info.boot_id;
    this.odr = info.odr || 208;
    this.ring_n = info.ring_n;
    const ok = (v) => typeof v === "number" && Number.isFinite(v) && v > 0;
    this.accel_lsb = ok(info.accel_lsb) ? info.accel_lsb : POD.ACCEL_LSB;
    this.gyro_lsb = ok(info.gyro_lsb) ? info.gyro_lsb : POD.GYRO_LSB;
    this.is_reboot = isReboot;          // not the session's first epoch: resume from index 0
    this.ref32 = null; this.ref64 = null; // tick unwrap reference, set by the first STATUS
    this.win = [];                      // wallBase candidates from the last 32 STATUS
    this.wall_base = null;
    this.t_offset = null;               // set when this epoch emits its first sample
    this.first_t = null;                // t of that first sample
    this.next_idx = null;
    this.last_idx = null;               // index / ticks64 / t of the last kept sample
    this.last_ticks64 = null;
    this.last_t = null;
    this.pkt_prev = null;
    this.held = [];                     // DATA before the first STATUS
    this.open = false;                  // a stream is open (docs/ble.md 7, Streams)
    this.status = null;                 // the latest STATUS (decoded) and its ticks64
    this.status_t64 = null;
    this.open_seq = null;               // cmd_seq of the command that opened the stream
    this.snap = null;                   // state at the last reconnect INFO (boot_mismatch undo)
  }

  ticks64(ticks) { return this.ref64 + int32(ticks - this.ref32); }
}

/**
 * The phone's decoder for the pod's stream. Feed every CRP packet the pod sends, in arrival order,
 * with its arrival time in ms: stream.packet(u8, arrivalMs) (D45); or every frame of any type
 * (captures from before D45): stream.frame(dataView, arrivalMs). Samples come out in index
 * order through onSample({ t, ax, ay, az, gx, gy, gz }); onEvent(kind, info) reports epochs,
 * gaps, faults, stops, sleep and requests (need-command, need-info).
 */
export class PodStream {
  constructor(onSample, onEvent = () => {}) {
    this.onSample = onSample;
    this.onEvent = onEvent;
    this.serial = null;                 // the session's pod
    this.ignoring = false;              // until the next good INFO
    this.need_info = false;             // boot_mismatch: re-read INFO before any command
    this.epochs = [];
    this.ep = null;                     // the newest epoch
    this.emitter = null;                // the most recent epoch that emitted a sample
    this.last_t = null;                 // last emitted t, over all epochs
    this.cmd_seq = 0;
    this.cmd = null;                    // the current STREAM command
    this.stop = null;                   // a pending STOP: { head, timed_out }
    this.stale_due = 0;                 // STOPs a later STREAM command overtook: their stream_stopped
                                        // may still come, and closes nothing (section 7, Stop)
    this.gaps = [];
    this.faults = [];
    this.stats = { samples: 0, bad: 0, pre_info: 0, ignored: 0, stale: 0, stale_stops: 0,
      after_close: 0, dupes: 0, t_backwards: 0, pkt_seq_gaps: 0, pkt_seq_lost: 0, reconnects: 0,
      epochs: 0, clock_clamped: 0, stopped: 0 };
  }

  // ---- helpers ----
  _fault(what) {
    this.faults.push(what);
    this.onEvent("fault", what);
  }

  _gap(ep, kind, n, idx, t0, t1, lowerBound = false) {
    const g = { kind, n, idx, t0, t1, boot_id: ep.boot_id };
    if (lowerBound) g.lower_bound = true;
    this.gaps.push(g);
    this.onEvent("gap", g);
    if (kind === "unexplained") this._fault("unexplained gap");
  }

  _tOf(ep, t64) {
    return ep.t_offset === null ? null : ep.t_offset + t64 / POD.TICK_HZ;
  }

  _t0Left(ep, t1) {
    if (ep.last_t !== null) return ep.last_t;
    return this.last_t !== null ? this.last_t : t1;
  }

  /** A woken (reboot) epoch that never streamed still owes everything from index 0 up to the
   * last head the phone heard: a lower-bound tail with no time (section 7, Closing). */
  _owed(ep, kind) {
    if (ep.is_reboot && !ep.open && ep.next_idx === null && ep.status !== null && ep.status.head > 0) {
      this._gap(ep, kind, ep.status.head, 0, null, null, true);
      ep.next_idx = ep.status.head;
    }
  }

  _newEpoch(info, isReboot) {
    const old = this.ep;
    if (old !== null && old.open) {
      // the pod slept or reset while disconnected: count what we know, a lower bound
      this._close(old, old.status ? old.status.head : old.next_idx, old.status_t64, "lost-tail", true);
    } else if (old !== null) {
      this._owed(old, "lost-tail");
    }
    const ep = new PodEpoch(info, isReboot);
    this.epochs.push(ep);
    this.ep = ep;
    this.stop = null;
    this.stale_due = 0;
    this.stats.epochs += 1;
    this.onEvent("epoch", { boot_id: ep.boot_id, reboot: isReboot });
    return ep;
  }

  _close(ep, head, t64, kind, lowerBound = false) {
    if (ep.open && ep.next_idx !== null && head !== null && head > ep.next_idx) {
      let t0, t1;
      if (ep.last_t === null) {
        t0 = t1 = null;
      } else {
        t0 = ep.last_t;
        t1 = t64 !== null ? this._tOf(ep, t64) : null;
      }
      this._gap(ep, kind, head - ep.next_idx, ep.next_idx, t0, t1, lowerBound);
    }
    if (head !== null && (ep.next_idx === null || head > ep.next_idx)) ep.next_idx = head;
    ep.open = false;
  }

  // ---- the entry points ----
  /** A CRP packet from the pod (D45): its frame, if it carries one, goes to frame(). */
  packet(pkt, arrivalMs) {
    const f = podFrameOf(pkt);
    if (f !== null) this.frame(f, arrivalMs);
  }

  frame(dv, arrivalMs) {
    const f = decodeFrame(dv);
    if (f === null) { this.stats.bad += 1; return; }
    if (f.type === POD.T_CAL) return;   // ignored in v1
    if (f.type === POD.T_INFO) return this._info(f);
    if (this.ep === null) { this.stats.pre_info += 1; return; }
    if (this.ignoring) { this.stats.ignored += 1; return; }
    if (f.type === POD.T_STATUS) return this._status(f, arrivalMs);
    const ep = this.ep;
    if (ep.ref32 === null) { ep.held.push(f); return; }   // DATA before the first STATUS: hold it
    this._data(ep, f);
  }

  _info(f) {
    if (f.proto !== 1 || f.boot_id === 0) {
      this.ignoring = true;             // its STATUS and DATA would be just as suspect
      this._fault("bad INFO (proto or boot_id)");
      return;
    }
    if (this.serial === null) this.serial = f.serial;
    if (f.serial !== this.serial) {
      this.ignoring = true;
      this._fault("another pod");
      return;
    }
    this.ignoring = false;
    this.need_info = false;
    const ep = this.ep;
    if (ep !== null && f.boot_id === ep.boot_id) {
      // same boot: the epoch continues. Keep what a boot_mismatch would have to roll back.
      // Nothing the pod queued for the old link can still come (it drops it at the disconnect).
      this.stats.reconnects += 1;
      this.stale_due = 0;
      ep.snap = [ep.status, ep.status_t64, ep.ref32, ep.ref64, ep.win.slice(), ep.wall_base];
      return;
    }
    this._newEpoch(f, ep !== null);
  }

  _status(f, arrivalMs) {
    let ep = this.ep;
    if (f.flags & POD.S_BOOT_MISMATCH) {
      // STREAM_FROM had the wrong boot_id: our last INFO was stale. Undo everything since it
      // and ignore the link until INFO is read again.
      this._fault("boot mismatch");
      if (ep.snap !== null) {
        let win;
        [ep.status, ep.status_t64, ep.ref32, ep.ref64, win, ep.wall_base] = ep.snap;
        ep.win = win.slice(-32);
      }
      this.ignoring = this.need_info = true;
      this.onEvent("need-info", null);
      return;
    }
    if (ep.ref32 !== null) {
      const t64 = ep.ticks64(f.ticks_now);
      const rebooted = ((ep.next_idx !== null && f.head < ep.next_idx) ||
                        (ep.last_ticks64 !== null && t64 < ep.last_ticks64 - 2 * POD.TICK_HZ));
      if (rebooted) {
        // the pod rebooted but kept its boot_id: a new epoch, starting with this STATUS
        this._fault("reboot without a new boot_id");
        const info = { boot_id: ep.boot_id, odr: ep.odr, ring_n: ep.ring_n,
          accel_lsb: ep.accel_lsb, gyro_lsb: ep.gyro_lsb };
        ep = this._newEpoch(info, true);
        this.onEvent("need-command", null);
      }
    }
    if (ep.ref32 === null) {
      ep.ref32 = ep.ref64 = f.ticks_now;
    } else {
      ep.ref64 += int32(f.ticks_now - ep.ref32);
      ep.ref32 = f.ticks_now;
    }
    const t64 = ep.ref64;
    ep.win.push(arrivalMs - t64 / 32.768);
    if (ep.win.length > 32) ep.win.shift();
    ep.wall_base = Math.min(...ep.win);
    ep.status = f; ep.status_t64 = t64;

    if (f.flags & POD.S_GOING_TO_SLEEP) {
      // the ring dies with the sleep: whatever didn't arrive is lost
      this._close(ep, f.head, t64, "sleep-tail");
      this.stop = null;
      this.stats.stopped += 1;
      this.onEvent("stopped", "sleep");
      this.onEvent("sleep", SLEEP_REASONS[f.sleep_reason] ?? "?");
    } else if (f.flags & POD.S_STREAM_STOPPED) {
      const cmd = this.cmd;
      if (cmd !== null && !cmd.started && ep.open && ep.open_seq !== cmd.seq && this.stale_due > 0) {
        // the end of a stream the phone already replaced: the new STREAM_FROM fetches it again.
        // Only an overtaken STOP gets this answer: after a reconnect, a STOP sent on the new link
        // can be answered before the new stream's first DATA, and that one closes the stream.
        this.stale_due -= 1;
        this.stats.stale_stops += 1;
      } else {
        this._close(ep, f.head, t64, "stop-tail");
        this.stop = null;
        this.stats.stopped += 1;
        this.onEvent("stopped", "stop");
      }
    }
    if (f.flags & POD.S_MTU_TOO_SMALL) this.onEvent("mtu-too-small", null);

    const held = ep.held;
    ep.held = [];
    for (const d of held) this._data(ep, d);
  }

  _data(ep, f) {
    const cmd = this.cmd;
    if (cmd === null || f.cmd_seq !== cmd.seq) { this.stats.stale += 1; return; }
    const firstFrame = !cmd.started;
    if (firstFrame) {
      cmd.started = true;
      this.stale_due = 0;               // DATA and STATUS come in order (D28): no stale stop is left
      ep.snap = null;                   // this boot answered: nothing left to roll back
      const hadIdx = ep.next_idx !== null;
      cmd.resync_skip = cmd.op === POD.OP_STREAM_LIVE && hadIdx;
      if (cmd.op === POD.OP_STREAM_FROM) ep.next_idx = cmd.from;
      else if (!hadIdx) ep.next_idx = f.first_idx;
      ep.open = true; ep.open_seq = cmd.seq;
    } else if (!ep.open) {
      // frames of a stream that already closed: everything below its head was counted
      this.stats.after_close += 1;
      return;
    }

    // pkt_seq continuity (per epoch, mod 2^16)
    if ((f.flags & POD.F_RESYNC) || ep.pkt_prev === null) {
      ep.pkt_prev = f.pkt_seq;
    } else {
      if (f.pkt_seq !== ((ep.pkt_prev + 1) & 0xFFFF)) {
        this.stats.pkt_seq_gaps += 1;
        this.stats.pkt_seq_lost += (f.pkt_seq - ep.pkt_prev - 1) & 0xFFFF;
      }
      ep.pkt_prev = f.pkt_seq;
    }

    const hole = 1.5 * POD.TICK_HZ / ep.odr;
    for (let k = 0; k < f.samples.length; k++) {
      const [dt, gx, gy, gz, ax, ay, az] = f.samples[k];
      const idx = f.first_idx + k;
      if (idx < ep.next_idx) { this.stats.dupes += 1; continue; }
      const t64 = ep.ticks64((f.t0 + dt) >>> 0);
      if (ep.t_offset === null) {
        this._setOffset(ep, t64);
        ep.first_t = ep.t_offset + t64 / POD.TICK_HZ;
      }
      const t = ep.t_offset + t64 / POD.TICK_HZ;
      if (this.last_t !== null && t <= this.last_t) {
        // time went backwards inside a boot: a pod bug. Count it, never emit it.
        this.stats.t_backwards += 1;
        if (this.stats.t_backwards === 1) this._fault("time went backwards");
        ep.next_idx = idx + 1;
        continue;
      }
      if (idx > ep.next_idx) {
        let kind;
        if (f.flags & POD.F_OVERFLOW) kind = "overflow";
        else if (firstFrame && k === 0 && (f.flags & POD.F_RESYNC) && cmd.resync_skip) kind = "resync";
        else kind = "unexplained";
        this._gap(ep, kind, idx - ep.next_idx, ep.next_idx, this._t0Left(ep, t), t);
      } else if (ep.last_idx !== null && idx === ep.last_idx + 1 && t64 - ep.last_ticks64 > hole) {
        const kind = k === 0 && (f.flags & POD.F_IMU_RESTART) ? "imu-restart" : "imu";
        const dtk = t64 - ep.last_ticks64;
        // floor(x + 0.5): .5 rounds up, the same as podlink.py
        this._gap(ep, kind, Math.floor(dtk * ep.odr / POD.TICK_HZ + 0.5) - 1, idx, ep.last_t, t);
      }
      const s = { t, ax: ax * ep.accel_lsb, ay: ay * ep.accel_lsb, az: az * ep.accel_lsb,
        gx: gx * ep.gyro_lsb, gy: gy * ep.gyro_lsb, gz: gz * ep.gyro_lsb };
      ep.next_idx = idx + 1;
      ep.last_idx = idx; ep.last_ticks64 = t64; ep.last_t = t;
      this.last_t = t;
      this.emitter = ep;
      this.stats.samples += 1;
      this.onSample(s);
    }
  }

  _setOffset(ep, t64) {
    const prev = this.emitter;
    if (prev === null) {
      ep.t_offset = -t64 / POD.TICK_HZ;              // t = 0 at the very first sample
      return;
    }
    ep.t_offset = prev.t_offset + (ep.wall_base - prev.wall_base) / 1000;
    if (ep.t_offset + t64 / POD.TICK_HZ <= this.last_t + 0.001) {
      ep.t_offset = this.last_t + 0.001 - t64 / POD.TICK_HZ;
      this.stats.clock_clamped += 1;
      this.onEvent("clock-clamped", null);
    }
  }

  // ---- what the phone writes ----
  /** The STREAM command to write now (Uint8Array), or null while the phone must not stream. */
  command() {
    if (this.ignoring || this.need_info) return null;
    const ep = this.ep;
    if (this.stop !== null) this.stale_due += 1;   // its stream_stopped may still come: the old stream's end
    this.stop = null;                   // a STREAM command cancels a pending STOP wait
    this.cmd_seq = (this.cmd_seq + 1) & 0xFF;
    if (ep !== null && (ep.next_idx !== null || ep.is_reboot)) {
      const from = ep.next_idx !== null ? ep.next_idx : 0;
      this.cmd = { op: POD.OP_STREAM_FROM, seq: this.cmd_seq, from, started: false };
      const b = new Uint8Array(10);
      const dv = new DataView(b.buffer);
      dv.setUint8(0, POD.OP_STREAM_FROM); dv.setUint8(1, this.cmd_seq);
      dv.setUint32(2, ep.boot_id, true); dv.setUint32(6, from, true);
      return b;
    }
    this.cmd = { op: POD.OP_STREAM_LIVE, seq: this.cmd_seq, back: POD.LIVE_BACK, started: false };
    return new Uint8Array([POD.OP_STREAM_LIVE, this.cmd_seq, POD.LIVE_BACK & 0xFF, POD.LIVE_BACK >> 8]);
  }

  /** STOP. Starts the stop wait: resolve on the "stopped" event. */
  stopCommand() {
    const st = this.ep ? this.ep.status : null;
    this.stop = { head: st ? st.head : null, timed_out: false };
    return new Uint8Array([POD.OP_STOP]);
  }

  /** How long to wait for the stream_stopped STATUS (docs/ble.md 7, Stop), ms. */
  stopWaitMs() {
    const ep = this.ep;
    const st = ep ? ep.status : null;
    if (st === null || st.n_per_packet < 4 || ep.next_idx === null) return 2500.0;
    const behind = Math.max(0, st.head - ep.next_idx) + 1.2 * ep.odr;
    return 2500.0 + behind / (60 * st.n_per_packet) * 1000;
  }

  /** No stream_stopped in time: fault, the stream stays open. False if the wait was over. */
  stopTimedOut() {
    if (this.stop === null || this.stop.timed_out) return false;
    this.stop.timed_out = true;
    this._fault("stop timed out");
    return true;
  }

  /** The session is over: a stream still open gets a lower-bound tail to the last head heard. */
  finish() {
    const ep = this.ep;
    const kind = this.stop !== null ? "stop-tail" : "lost-tail";
    if (ep !== null && ep.open) {
      this._close(ep, ep.status ? ep.status.head : null, ep.status_t64, kind, true);
    } else if (ep !== null) {
      this._owed(ep, kind);
    }
    this.stop = null;
  }

  /** Arrival-clock ms of sample time t (the latest wallBase of the epoch t came from). */
  wallAt(t) {
    const eps = this.epochs.filter((e) => e.first_t !== null);
    let ep = eps[0];
    for (let i = eps.length - 1; i >= 0; i--) if (eps[i].first_t <= t) { ep = eps[i]; break; }
    return ep.wall_base + (t - ep.t_offset) * 1000;
  }
}

// ---------------------------------------------------------------------------------------------
// The pod's CRP session (D45, docs/ble.md 2 and 7) over the vendored CRPClient (webapp/crp): HELLO,
// the reads with their BUSY retry, PUSH, CTRL and BYE. It never feeds the decoder: the transport
// hands every packet to PodSource.packet() first (its tap, before it emits "packet"), so PodStream
// sees them all in arrival order with their own times. No retries (the engine runs a repeated
// WRITE again, so a retried STOP answers twice), and no heartbeat on Bluetooth (the pod ignores
// the client's silence there; USB clients send a NOP every 1 s).

/** The status of a transaction a RESPONSE rejected (crp-client.js J1), or null if it went
 * unanswered (a timeout, the link). A DOMException has a numeric code too, but no reg. */
const crpStatusOf = (err) => (err && Number.isInteger(err.code) && Number.isInteger(err.reg) ? err.code : null);

export class PodCrpLink {
  /**
   * transport: what CRPClient takes (send(u8), on("packet" | "connected" | "disconnected")).
   * heartbeatMs: 0 on Bluetooth, 1000 on USB. busyRetryMs / busyForMs: the BUSY retry (docs/ble.md
   * 5). now / sleep: the clock the retry runs on (tests drive both).
   */
  constructor(transport, { heartbeatMs = 0, txnTimeoutMs = 5000, busyRetryMs = 200, busyForMs = 3000,
    now = () => Date.now(), sleep = (ms) => sleepMs(ms) } = {}) {
    this.client = new CRPClient(transport, { txnTimeoutMs, txnRetries: 0, heartbeatMs });
    this.txnTimeoutMs = txnTimeoutMs;
    this.busyRetryMs = busyRetryMs;
    this.busyForMs = busyForMs;
    this.now = now;
    this.sleep = sleep;
    this.peer = null;                   // the pod's HELLO (CRPClient's parse)
    this.busy = 0;                      // BUSY answers to reads (each retried)
    this.mtuRead = null;                // the MTU register, read after the BUSY retries gave up
    this.cancels = new Set();           // pending hello()s: close() fails them at once (CRPClient only fails transactions)
  }

  /** HELLO (CRPClient.sendHello's defaults), then the pod's, which must say device_id "BPOD"
   * (docs/ble.md 2.4: every Ansync device shares the service). Resolves to the pod's HELLO; fails
   * on a BYE (the pod refuses our version), another device, a dropped link, close() or no answer. */
  hello() {
    const c = this.client;
    this.peer = null;
    return new Promise((resolve, reject) => {
      let timer = null;
      const done = (err, h) => {
        clearTimeout(timer);
        c.off("hello", onHello); c.off("bye", onBye); c.off("disconnected", onDown);
        this.cancels.delete(cancel);
        if (err) reject(err); else resolve(h);
      };
      const cancel = () => done(new Error("The pod's session was closed."));
      this.cancels.add(cancel);
      const onHello = (h) => {
        if (h.deviceId !== POD.DEVICE_ID) {
          return done(new Error(`Not a pod: its HELLO says device 0x${h.deviceId.toString(16).padStart(8, "0")}.`));
        }
        this.peer = h;
        done(null, h);
      };
      const onBye = () => done(new Error("The pod refused the session (BYE)."));
      const onDown = () => done(new Error("The link dropped before the pod's HELLO."));
      timer = setTimeout(() => done(new Error("The pod didn't answer HELLO.")), this.txnTimeoutMs);
      c.on("hello", onHello); c.on("bye", onBye); c.on("disconnected", onDown);
      try { c.sendHello({}); } catch (err) { done(err); }
    });
  }

  /** READ reg (INFO, STATUS or CAL) with the BUSY retry (docs/ble.md 5): every busyRetryMs for
   * busyForMs, then the MTU register (10 B, fits at MTU 23) says why. Resolves to the value, the
   * frame (the transport's tap already fed it to the decoder). Fails with err.code = the status
   * (BUSY after giving up, with err.mtu = the MTU read, or null), or CRPClient's own error when
   * unanswered. */
  async readFrame(reg) {
    const t0 = this.now();
    for (;;) {
      try {
        return (await this.client.read(reg)).payload;
      } catch (err) {
        if (crpStatusOf(err) !== ErrorCode.BUSY) throw err;
      }
      this.busy += 1;
      if (this.now() - t0 + this.busyRetryMs > this.busyForMs) break;
      await this.sleep(this.busyRetryMs);
    }
    this.mtuRead = await this.mtu().catch(() => null);
    throw Object.assign(new Error(`The pod's link is too small (MTU ${this.mtuRead ?? "unknown"}).`),
      { code: ErrorCode.BUSY, reg, mtu: this.mtuRead });
  }

  /** WRITE PUSH: on = this session takes the pod's pushes (DATA, STATUS). Step 4 of the connection
   * order, after the reads: a STATUS pushed before the reconnect INFO would belong to the old epoch
   * (docs/ble.md 7). Fails unless OK. */
  async push(on) {
    await this.client.write(POD.REG_PUSH, new Uint8Array([on ? 1 : 0]));
  }

  /** WRITE CTRL with an op (command(), stopCommand(), SLEEP). Resolves to its status: 0 OK,
   * 3 INVALID_VALUE (unknown op or length), 8 BUSY (from the SLEEP op on), docs/ble.md 2.3. Fails
   * only when unanswered (a timeout, the link, a session end), and is never retried. */
  async ctrl(b) {
    try {
      await this.client.write(POD.REG_CTRL, b);
      return Status.OK;
    } catch (err) {
      const st = crpStatusOf(err);
      if (st === null) throw err;
      return st;
    }
  }

  /** The link's MTU register (u16), or null if the answer isn't 2 bytes. */
  async mtu() {
    const v = (await this.client.read(POD.REG_LINK_MTU)).payload;
    return v.length === 2 ? v[0] | (v[1] << 8) : null;
  }

  /** BYE (base 0, our device_id 0) and every open transaction and hello() fails. Final: a new
   * session needs a new PodCrpLink. */
  close() {
    this.client.close();
    for (const cancel of [...this.cancels]) cancel();
  }
}

// ---------------------------------------------------------------------------------------------
// The pod's sources (docs/ble-plan.md section 3) and their transports. Since D45 every connect and
// reconnect runs the connection order of docs/ble.md 7 over a fresh PodCrpLink: HELLO, READ INFO,
// READ STATUS, PUSH = 1, then the STREAM command to CTRL. A transport hands every packet the pod
// sends to its tap first (PodSource.packet: the decoder, in arrival order, with its own arrival
// time) and only then to the session, so PodStream never depends on CRPClient's events.

/** Bytes of a DataView or Uint8Array as lowercase hex (the capture's payload column). */
const hexOf = (v) => Array.from(new Uint8Array(v.buffer, v.byteOffset, v.byteLength),
  (b) => b.toString(16).padStart(2, "0")).join("");
const sleepMs = (ms) => new Promise((res) => setTimeout(res, ms));
/** A send refused before it started (the link is down). Already handled: CRPClient drops the result
 * of a send without a transaction (HELLO, BYE), so it mustn't become an unhandled rejection. */
const refused = (why) => { const p = Promise.reject(new Error(why)); p.catch(() => {}); return p; };
// Which source owns each pod's connection. Two sources share one BluetoothDevice (a new session
// after a failed one), and a connection is per device, so only the owner may disconnect it.
const podOwners = new WeakMap();

/**
 * What both pod sources share (docs/ble-plan.md section 3): the decoder and what it asks for, the
 * link's story (lost / asleep / back, once per outage), linkInfo and link.json, the capture (the
 * make_fake_pod.py log format, so podlink.py can replay it), the connection order and the CTRL ops
 * over the current link's CRP session (this.crp), and the STOP handshake. PodWebSource (Web
 * Bluetooth) and PodNativeSource (the app's PodLink plugin) add the transport: name, start(),
 * stop() and close(). app.js treats both as "the pod".
 */
export class PodSource {
  constructor(onSample, { onEvent = () => {}, capture = false } = {}) {
    this.onEvent = onEvent;
    this.capture = capture ? { log: [], samples: [] } : null;
    this.closed = false;                // close() ran: a setup still in flight must stop and touch nothing
    this.linkLost = false;
    this.started = false;               // start() succeeded: from then on a dropped link reconnects
    this.stopping = false;              // stop() ran: a drop now is the end, not something to fix
    this.asleep = null;                 // the pod said it went to sleep (the reason) and hasn't woken (a new boot)
    this.sleepMode = null;              // the mode of the SLEEP op we sent (1 = IMU off: motion won't wake it)
    this.reconnects = 0;                // links that came back
    this.inSetup = false;               // the connection order runs: its own command answers any request
    this.linkEvents = [];               // { at: Date.now(), kind: lost / asleep / back, ... } for link.json
    this.mtu = null;                    // the link's MTU: the plugin's, or the pod's MTU register on Web Bluetooth
    this.crp = null;                    // the current link's CRP session (PodCrpLink)
    this.rereadP = null;                // a re-read (need-info) in flight: one at a time
    this.cmdSent = false;               // a STREAM command went out (the pod holds the session)
    this.gotData = null;                // resolves start() at the first sample
    this.lostWait = null;               // fails start() when the link drops
    this.stopped = null;                // resolves stop()'s wait at the stream_stopped STATUS
    this.wake = null;                   // ends stop()'s wait early when the link drops
    this.stream = new PodStream((smp) => {
      if (this.closed) return;
      if (this.capture) this.capture.samples.push(smp);
      this.gotData?.();
      onSample(smp);
    }, (kind, info) => this.event(kind, info));
  }

  /** After every await in a setup: a closed source (start() timed out, End) gives up here. */
  alive() {
    if (this.closed) throw new Error("The pod source was closed.");
  }

  get name() { return "pod"; }

  log(kind, arrival, payload) {
    this.capture?.log.push([kind, arrival, payload]);
  }

  /** One CRP packet the pod sent (D45) into the decoder, through podFrameOf, stamped with its
   * arrival (ms): the transport's tap, for every packet in arrival order, the ones without a frame
   * too (the capture's pkt rows, so a replay sees the whole story). Nothing after the session
   * ended: a reconnect's late read must not add an epoch after finish(). */
  packet(u8, t = Date.now()) {
    if (this.closed) return;
    this.log("pkt", t, hexOf(u8));
    this.stream.packet(u8, t);
  }

  /** One packet we sent: the capture's tx row (for reading only, docs/ble.md 7), until the end. */
  sent(u8) {
    if (!this.closed) this.log("tx", Date.now(), hexOf(u8));
  }

  event(kind, info) {
    if (kind === "stopped") this.stopped?.();
    if (kind === "sleep") this.asleep = info;   // its deliberate disconnect follows: wait for the wake
    if (kind === "epoch" && info.reboot) { this.asleep = null; this.sleepMode = null; }   // a wake is a new boot
    // The decoder asks: the pod rebooted without a new boot_id, or our INFO was stale. Not during
    // a connection order (or while the link is down): the command it writes answers that (docs/ble.md 7).
    if (kind === "need-command" && !this.inSetup && !this.linkLost) this.command().catch(() => {});
    if (kind === "need-info" && !this.inSetup && !this.linkLost) this.reread().catch(() => {});
    this.onEvent(kind, info);
    // asleep while we were already reconnecting (it said so during a setup): the banner must know
    if (kind === "sleep" && this.linkLost && this.started && !this.closed && !this.stopping) {
      this.linkEvents.push({ at: Date.now(), kind: "asleep", reason: info });
      this.onEvent("link", "asleep");
    }
  }

  /** The link went. Reported once per outage: "asleep" if the pod said so first, else "lost". */
  linkDown() {
    const was = this.linkLost;
    this.linkLost = true;
    this.lostWait?.();
    this.wake?.();
    if (this.closed || this.stopping || was) return;
    const kind = this.asleep ? "asleep" : "lost";
    this.linkEvents.push({ at: Date.now(), kind, ...(this.asleep ? { reason: this.asleep } : {}) });
    this.onEvent("link", kind);
  }

  /** It came back: a reconnect's connection order and command are done. */
  linkUp(extra = {}) {
    this.linkLost = false;
    this.reconnects += 1;
    this.linkEvents.push({ at: Date.now(), kind: "back", ...extra });
    this.onEvent("link", "back");
  }

  /** Steps 2 to 4 of the connection order (docs/ble.md 7) on one link's session o = { crp, done }:
   * HELLO (device_id BPOD), READ INFO, READ STATUS (each BUSY retried), PUSH = 1. Each step runs
   * once: done counts the ones that worked, so a try after a failure goes on where it stopped (a
   * second INFO would count a second reconnect). The reads reach the decoder through the tap; the
   * STREAM command (step 5) is the caller's. */
  async runOrder(o) {
    const steps = [() => o.crp.hello(), () => o.crp.readFrame(POD.REG_INFO), () => o.crp.readFrame(POD.REG_STATUS),
      () => o.crp.push(true)];
    const was = this.inSetup;
    this.inSetup = true;
    try {
      for (; o.done < steps.length; o.done++) {
        this.alive();
        try {
          await steps[o.done]();
        } catch (err) {
          if (err?.mtu != null) this.mtu = err.mtu;   // the BUSY reads gave up: the MTU register says why
          throw err;
        }
      }
      this.alive();
    } finally {
      this.inSetup = was;
    }
  }

  /** The STREAM command (docs/ble.md 4) to CTRL. Fails unless the pod took it: BUSY means its sleep
   * prologue runs. A STREAM_FROM with a stale boot_id is taken too; its answer is the boot_mismatch
   * STATUS (need-info). Never once End came: every path (a setup, a resume, a re-read, a decoder
   * request during the STOP handshake) funnels through here, and a STREAM that reaches the pod after
   * End's STOP sets the session hold again, which BYE doesn't clear (docs/ble.md 2.2, 6). */
  async command() {
    if (this.stopping) throw new Error("End came: no stream command after its STOP.");
    const crp = this.crp;
    if (!crp) throw new Error("The pod isn't connected.");
    const b = this.stream.command();
    this.log("cmd", "", b ? hexOf(b) : "none");
    if (!b) throw new Error(`The pod can't stream: ${this.stream.faults.at(-1) ?? "not ready"}.`);
    this.cmdSent = true;
    const st = await crp.ctrl(b);
    if (st !== Status.OK) throw new Error(`The pod didn't take the STREAM command (status ${st}).`);
  }

  /** Step 5 of the connection order: the command, then the re-read a boot_mismatch answer asks for,
   * when it came while the event couldn't act on it (during a setup, or with the link still down). */
  async orderCommand() {
    await this.command();
    if (this.stream.need_info && !this.stopping && !this.closed) await this.reread();
  }

  /** need-info (a boot_mismatch: the INFO we had was stale): READ INFO and STATUS again, then the
   * command (docs/ble.md 7). One at a time; no stream command once End came. */
  reread() {
    this.rereadP ??= (async () => {
      const was = this.inSetup;
      this.inSetup = true;                // a request the STATUS read raises is answered by this command
      try {
        if (!this.crp) throw new Error("The pod isn't connected.");
        await this.crp.readFrame(POD.REG_INFO);
        await this.crp.readFrame(POD.REG_STATUS);
        this.alive();
      } finally {
        this.inSetup = was;
      }
      if (this.stopping) return;          // End came during the re-read: no stream command after its STOP
      await this.command();
    })().finally(() => { this.rereadP = null; });
    return this.rereadP;
  }

  /** The SLEEP op (docs/ble.md 4): mode 0 wakes on motion, 1 = IMU off until USB or reset. BUSY is
   * fine: a sleep prologue runs already. */
  async sleepPod(mode = 0) {
    if (!this.crp || this.linkLost) throw new Error("The pod isn't connected.");
    const b = new Uint8Array([POD.OP_SLEEP, mode]);
    this.log("ctrl", "", hexOf(b));               // not "cmd": a replay checks those against command()
    this.sleepMode = mode;
    const st = await this.crp.ctrl(b);
    if (st !== Status.OK && st !== ErrorCode.BUSY) throw new Error(`The pod didn't take SLEEP (status ${st}).`);
  }

  /** STOP, then wait for the pod's stream_stopped STATUS (everything up to its head has arrived,
   * or the tail is counted as a gap) or the link to drop, at most the decoder's stop wait.
   * send(bytes) writes it to CTRL. up(): the link is still there (a silent wait is a real timeout). */
  async stopHandshake(send, up = () => !this.linkLost) {
    let done = false;
    let failed = false;
    const stopped = new Promise((res) => { this.stopped = () => { done = true; res(); }; this.wake = res; });
    const b = this.stream.stopCommand();
    this.log("stop", "", hexOf(b));
    const wait = this.stream.stopWaitMs();
    try {
      await send(b);
      await Promise.race([stopped, sleepMs(wait)]);
    } catch {
      failed = true;                             // the link went: finish() counts the tail
    }
    this.stopped = this.wake = null;
    // a real timeout only; a dropped link goes straight to finish() (docs/ble.md 7)
    if (!done && !failed && up()) this.log("call", "", `stop_timed_out=${this.stream.stopTimedOut() ? 1 : 0}`);
  }

  /** STOP on a session without PUSH (a reconnect's order didn't get that far): nothing would hear
   * its answer, so don't wait for one. It still releases the pod's session hold, or the pod keeps
   * the 30 min idle limit and no lid-down for nothing (docs/ble.md 6). At most 1 s. */
  async stopOnly(crp) {
    const b = this.stream.stopCommand();
    this.log("stop", "", hexOf(b));
    await Promise.race([crp.ctrl(b), sleepMs(1000)]).catch(() => {});
  }

  finishStream() {
    this.stream.finish();
    this.log("call", "", "finish");
  }

  wallAt(t) { return this.stream.wallAt(t); }

  /** For the footer and pod.json: the latest STATUS, INFO-derived values and loss counts. */
  linkInfo() {
    const ep = this.stream.ep;
    const st = ep?.status ?? null;
    const lost = this.stream.gaps.filter((g) => !g.kind.startsWith("imu")).reduce((n, g) => n + g.n, 0);
    return {
      name: this.name, serial: this.stream.serial, bootId: ep?.boot_id ?? null, odr: ep?.odr ?? null,
      ringN: ep?.ring_n ?? null, accelLsb: ep?.accel_lsb ?? null, gyroLsb: ep?.gyro_lsb ?? null,
      perPacket: st?.n_per_packet ?? null, intervalMs: st ? st.interval * 1.25 : null, mtu: this.mtu,
      volts: st ? st.battery_mv / 1000 : null, podLost: st?.lost_overflow ?? null,
      battLow: Boolean(st && (st.flags & POD.S_BATT_LOW)),
      lost, gaps: this.stream.gaps.length, faults: this.stream.faults.slice(), stats: { ...this.stream.stats },
      linked: !this.linkLost, asleep: this.asleep, sleepMode: this.sleepMode, reconnects: this.reconnects,
      epochs: this.stream.epochs.length,
    };
  }

  /** link.json (docs/ble-plan.md step 10): the link's whole story next to the set files (P2). */
  linkJSON() {
    return {
      ...this.linkInfo(),
      linkEvents: this.linkEvents.slice(),
      epochList: this.stream.epochs.map((e) => ({ bootId: e.boot_id, reboot: e.is_reboot, firstT: e.first_t,
        tOffset: e.t_offset, wallBase: e.wall_base, odr: e.odr, ringN: e.ring_n, lastIdx: e.last_idx,
        podLost: e.status?.lost_overflow ?? null, missedDrdy: e.status?.missed_drdy ?? null })),
      gapList: this.stream.gaps.map((g) => ({ ...g })),
    };
  }

  /** The capture as a CSV in make_fake_pod.py's vector format (scenario "capture"). */
  captureCSV() {
    if (!this.capture) return "";
    const rows = this.capture.log.map(([k, a, p], i) => `capture,${i},${k},${a},${p}`);
    return ["scenario,step,kind,arrival_ms,payload", ...rows].join("\n") + "\n";
  }
}

// ---------------------------------------------------------------------------------------------
// The pod over Web Bluetooth (desktop and Android Chrome, on https or localhost; docs/ble-plan.md
// section 3 and step 6). Pick the pod with PodWebSource.pick() inside a click (the browser's
// chooser needs the user gesture), then use it like any other source: start(), stop(). A new
// PodWebSource per session gets its own PodStream, so t starts at 0 again. A dropped link
// reconnects by itself (step 10) with the same PodStream: STREAM_FROM the next index gets the
// missed samples back from the pod's 38 s ring, and a pod that slept or reset comes back as a new
// epoch with t still rising. stop() counts the tail.

/**
 * CRPClient's transport over one Web Bluetooth connection (docs/ble.md 2.4). Each TX notification
 * is one packet: copied, handed to tap(u8, Date.now()), then emitted as "packet". send() writes to
 * RX with response, each write after the one before has finished, in call order (Chrome runs one
 * GATT operation at a time), and returns that write's promise, which fails its transaction (J2).
 * "disconnected" once, at gattserverdisconnected. One per connection: a dropped connection's
 * characteristics never come back.
 */
export class PodWebTransport extends EventEmitter {
  constructor(rx, tx, device, tap, onSend = () => {}) {
    super();
    this.rx = rx;
    this.tx = tx;
    this.device = device;
    this.onSend = onSend;               // the capture's tx row
    this.up = true;                     // until the link drops or we close it
    this.chain = Promise.resolve();     // the last write, settled
    this.onValue = (e) => {
      if (!this.up) return;
      const v = e.target.value;
      const u8 = new Uint8Array(v.buffer.slice(v.byteOffset, v.byteOffset + v.byteLength));   // ours to keep
      tap(u8, Date.now());
      this.emit("packet", u8);
    };
    this.onGone = () => this.down();
    tx.addEventListener("characteristicvaluechanged", this.onValue);
    device.addEventListener("gattserverdisconnected", this.onGone);
  }

  send(u8) {
    if (!this.up) return refused("The pod's link is down.");
    this.onSend(u8);
    const b = u8.slice();
    const p = this.chain.then(() => (this.up ? this.rx.writeValueWithResponse(b) : refused("The pod's link is down.")));
    this.chain = p.catch(() => {});     // a failed write doesn't hold up the next one (and p counts as handled)
    return p;
  }

  /** Resolves once every write so far has finished or failed. */
  idle() { return this.chain; }

  down() {
    if (!this.up) return;
    this.close();
    this.emit("disconnected");          // CRPClient fails every open transaction now
  }

  /** Ours to drop: nothing in or out from here, and no "disconnected". */
  close() {
    this.up = false;
    this.tx.removeEventListener("characteristicvaluechanged", this.onValue);
    this.device.removeEventListener("gattserverdisconnected", this.onGone);
  }
}

export class PodWebSource extends PodSource {
  /** The browser's device chooser, filtered to pods: the CRP service and the BarPod- name (every
   * Ansync device shares the service, docs/ble.md 2.4). Call it from a click handler. */
  static async pick() {
    if (!globalThis.navigator?.bluetooth) {
      throw new Error("This browser has no Web Bluetooth. Use Chrome (desktop or Android) or the app.");
    }
    return navigator.bluetooth.requestDevice({ filters: [{ services: [POD.SERVICE], namePrefix: POD.NAME_PREFIX }] });
  }

  /**
   * device: from pick(). onEvent(kind, info): the PodStream events plus ("link", "lost" |
   * "asleep" | "back"). capture: keep every packet, command and call (the make_fake_pod.py log
   * format, so podlink.py can replay it) and every decoded sample. backoffMs: waits between
   * reconnect tries, the last one repeating (docs/ble-plan.md 3); connectTimeoutMs: a connect that
   * hangs (the pod out of range) is cancelled after this. Tests shorten all three.
   */
  constructor(onSample, device, { onEvent = () => {}, capture = false, startTimeoutMs = 10000,
    backoffMs = [500, 1000, 2000, 4000, 5000], connectTimeoutMs = 10000 } = {}) {
    super(onSample, { onEvent, capture });
    this.device = device;
    this.startTimeoutMs = startTimeoutMs;   // no first sample by then: start() fails (tests shorten it)
    this.backoffMs = backoffMs;
    this.connectTimeoutMs = connectTimeoutMs;
    this.transport = null;              // the current connection's PodWebTransport (this.crp runs on it)
    this.reconnecting = false;          // the reconnect loop is running
    this.holdOffUntil = 0;              // no reconnect try before this Date.now() (barDebug outages)
    this.linkLive = false;              // this.crp belongs to the current connection (not a dropped one)
    this.linkReady = false;             // ...and its PUSH is on: a STOP handshake can be heard
    this.setupP = null;                 // the reconnect's setup in flight (stop() lets it finish first)
    this.onGone = () => {
      this.linkLive = this.linkReady = false;
      this.linkDown();                  // once per outage, not per failed reconnect try
      if (this.closed || this.stopping) return;
      if (this.started) this.reconnect();
    };
  }

  get name() { return this.device.name || "pod"; }

  /** Connect and find the CRP characteristics. Right after a disconnect (End, then Start at once)
   * Chrome can hand back a connection that drops a moment later: try again, up to 3 times (the
   * reconnect loop makes one try per round: it retries anyway). */
  async discover(attempts = 3) {
    for (let attempt = 1; ; attempt++) {
      this.alive();
      try {
        const server = await this.connectGatt();
        // closed while connecting (start() timed out): drop this late connection, unless a newer
        // source on this pod owns it now
        if (this.closed && !podOwners.has(this.device)) this.device.gatt.disconnect();
        this.alive();
        const svc = await server.getPrimaryService(POD.SERVICE);
        const ch = { rx: await svc.getCharacteristic(POD.RX), tx: await svc.getCharacteristic(POD.TX) };
        this.alive();
        return ch;
      } catch (err) {
        if (this.closed || attempt >= attempts) throw err;
        await sleepMs(500 * attempt);
      }
    }
  }

  /** gatt.connect(), cancelled after connectTimeoutMs: Chrome can wait a long time for a pod that
   * is out of range or asleep, and the reconnect loop must keep its rhythm. */
  async connectGatt() {
    const p = this.device.gatt.connect();
    // A connect the browser couldn't cancel can land after we gave up and closed: drop it, unless
    // a newer source owns the pod now (current Chrome rejects it instead: AbortError).
    p.then(() => {
      if (this.closed && !podOwners.has(this.device)) { try { this.device.gatt.disconnect(); } catch { /* gone */ } }
    }, () => {});
    let timer = null;
    const late = new Promise((_, rej) => {
      timer = setTimeout(() => {
        try { this.device.gatt.disconnect(); } catch { /* nothing to cancel */ }
        rej(new Error("The pod didn't answer the connect."));
      }, this.connectTimeoutMs);
    });
    try {
      return await Promise.race([p, late]);
    } finally {
      clearTimeout(timer);
    }
  }

  /** Connect and run the connection order (docs/ble.md 7) over a fresh session. Also the reconnect:
   * the same stream, so its command is STREAM_FROM the next index (or index 0 of a pod that woke up). */
  async setup(attempts = 3) {
    this.inSetup = true;
    try {
      const ch = await this.discover(attempts);
      this.dropLink();                             // the old connection's session, if any
      const t = new PodWebTransport(ch.rx, ch.tx, this.device, (u8, ms) => this.packet(u8, ms), (u8) => this.sent(u8));
      const crp = new PodCrpLink(t, { heartbeatMs: 0 });   // no heartbeat on Bluetooth (docs/ble.md 2.2)
      this.transport = t;
      this.crp = crp;
      this.linkLive = true;
      this.device.addEventListener("gattserverdisconnected", this.onGone);
      await ch.tx.startNotifications();            // every packet the pod sends comes on TX, in order (D28)
      await this.runOrder({ crp, done: 0 });
      this.linkReady = this.linkLive && crp === this.crp;
      this.inSetup = false;                        // from here a request is answered as it comes
      // End came during the order: stop() sends its STOP over this link (the handshake, now that PUSH
      // is on), and a stream command could reach the pod behind it (as resume() does natively)
      if (this.stopping) return;
      await this.orderCommand();
      // the footer's MTU: Web Bluetooth doesn't tell, the pod's MTU register does
      crp.mtu().then((m) => { if (m !== null && crp === this.crp && !this.closed) this.mtu = m; }, () => {});
    } finally {
      this.inSetup = false;
    }
  }

  /** The previous connection's transport and session, at a reconnect: that link is gone, so no BYE,
   * and every transaction still open on it fails now. */
  dropLink() {
    const t = this.transport;
    const crp = this.crp;
    this.transport = this.crp = null;
    this.linkLive = this.linkReady = false;
    t?.close();
    crp?.close();
  }

  /** Why no sample came, from the latest STATUS (start()'s 10 s timeout). */
  whyNoData() {
    const st = this.stream.ep?.status;
    if (!this.crp) return "Couldn't reach the pod. Bump it to wake it (plug it in if its battery was low), keep it close, and try again.";
    if (st && (st.flags & POD.S_MTU_TOO_SMALL)) return "The pod's Bluetooth link is too small (MTU). Try again; if it stays, this browser can't carry the pod.";
    if (this.stream.faults.length) return `Pod: ${this.stream.faults.at(-1)}.`;
    return "The pod sent no data in 10 s.";
  }

  /** Resolves at the first sample; fails after 10 s (or when the link drops) with the reason. */
  async start() {
    podOwners.set(this.device, this);
    let timer = null;
    const first = new Promise((res) => { this.gotData = () => { this.gotData = null; res(); }; });
    const late = new Promise((_, rej) => { timer = setTimeout(() => rej(new Error(this.whyNoData())), this.startTimeoutMs); });
    const lost = new Promise((_, rej) => { this.lostWait = () => rej(new Error("The pod's link dropped while starting. Try again.")); });
    const setup = this.setup();
    setup.catch(() => {});                       // a setup that outlives the timeout fails quietly
    try {
      await Promise.race([setup.then(() => first), late, lost]);
      this.started = true;
    } catch (err) {
      // The pod already holds a session for this command: release it (best effort), or it
      // keeps the 30 min idle limit for nothing (docs/ble.md 6).
      if (this.cmdSent && !this.linkLost && this.crp && this.device.gatt.connected) {
        await Promise.race([this.crp.ctrl(new Uint8Array([POD.OP_STOP])), sleepMs(1000)]).catch(() => {});
      }
      await this.close();
      throw err;
    } finally {
      clearTimeout(timer);
      this.lostWait = null;
    }
  }

  /** Back to streaming after a dropout: setup() again with backoff until it works or stop() runs.
   * A sleeping pod is tried at the same rhythm: its pickup is a reboot, so a new epoch from 0. */
  async reconnect() {
    if (this.reconnecting) return;
    this.reconnecting = true;
    try {
      for (let k = 0; ; k++) {
        await sleepMs(this.backoffMs[Math.min(k, this.backoffMs.length - 1)]);
        while (!this.closed && !this.stopping && Date.now() < this.holdOffUntil) await sleepMs(100);
        if (this.closed || this.stopping) return;
        try {
          this.setupP = this.setup(1);
          await this.setupP;
          if (this.closed || this.stopping) return;
          if (!this.device.gatt.connected) continue;   // dropped again during the setup
          // it said going_to_sleep during this setup: its disconnect is a moment away, no "back"
          if (this.asleep) throw new Error("The pod went to sleep during the reconnect.");
          this.linkUp({ tries: k + 1 });
          return;
        } catch {
          if (this.closed || this.stopping) return;
          // a half-made connection: drop it so the next try starts clean (we still own the pod)
          try { if (podOwners.get(this.device) === this && this.device.gatt.connected) this.device.gatt.disconnect(); } catch { /* gone */ }
        }
      }
    } finally {
      this.reconnecting = false;
      this.setupP = null;
    }
  }

  /** Test and bench hook (barDebug.podOutage): drop the link now and hold off reconnecting for
   * ms, like a radio outage of that length (log_ble.py --outage). */
  outage(ms) {
    if (!this.started || this.closed) return false;
    this.holdOffUntil = Date.now() + ms;
    try { this.device.gatt.disconnect(); } catch { /* already gone */ }
    return true;
  }

  /**
   * STOP, wait for the pod's stream_stopped STATUS (everything up to its head has arrived, or
   * the tail is counted as a gap), then BYE and disconnect. Resolves after the last sample. While
   * the link is down (reconnecting) there is nothing to STOP: the tail is counted and the loop ends.
   */
  async stop() {
    this.stopping = true;
    // A reconnect mid-setup may already hold a live link: let it finish (at most 1 s), then STOP
    // over it, or the pod keeps the session hold: 30 min idle limit, no lid-down (docs/ble.md 6).
    // From here that setup sends no stream command (setup(), command()), however late it finishes
    if (this.setupP && this.linkLost && this.linkLive) await Promise.race([this.setupP.catch(() => {}), sleepMs(1000)]);
    const crp = this.crp;
    const live = crp && this.linkLive && this.device.gatt.connected && podOwners.get(this.device) === this;
    if (live && this.linkLost && !this.linkReady) {
      // no PUSH yet: just release the hold. The pod ignores a STOP outside a session (docs/ble.md
      // 2.2), so with no HELLO answered yet (the setup stuck before it went out) a HELLO goes first;
      // writes go out in call order
      if (crp.peer === null) crp.hello().catch(() => {});
      await this.stopOnly(crp);
    } else if (live && (!this.linkLost || this.linkReady)) {
      await this.stopHandshake((b) => crp.ctrl(b));
    }
    this.finishStream();
    await this.close();
  }

  async close() {
    this.closed = true;
    this.linkLive = this.linkReady = false;
    this.device.removeEventListener("gattserverdisconnected", this.onGone);
    const t = this.transport;
    const crp = this.crp;
    this.transport = this.crp = null;
    const owner = podOwners.get(this.device) === this;
    if (crp) {
      // BYE ends the pod's session (docs/ble.md 2.2), only on our own live link (a newer source on
      // this pod may share the connection), and its write goes out before the unsubscribe
      const bye = owner && t.up && crp.peer !== null && this.device.gatt.connected;
      if (!bye) t.close();
      crp.close();                                 // BYE (dropped on a closed transport), open transactions fail
      if (bye) await Promise.race([t.idle(), sleepMs(500)]);
    }
    if (t) {
      t.close();
      // the characteristic is shared with any newer source on this pod: only the owner unsubscribes
      if (owner) { try { await t.tx.stopNotifications(); } catch { /* already gone */ } }
    }
    if (owner) {
      podOwners.delete(this.device);
      try { this.device.gatt.disconnect(); } catch { /* already gone */ }
    }
  }
}

// ---------------------------------------------------------------------------------------------
// The pod through the Android app's PodLink plugin (android/.../PodLinkPlugin.java; docs/ble-plan.md
// section 3 and step 12). The plugin is a dumb pipe: every packet the pod notifies comes in raw,
// with its arrival time on elapsedRealtime, and ours go out to RX in call order. This side runs the
// CRP session (PodNativeTransport, PodCrpLink) and decodes with the same PodStream. The plugin finds
// and remembers the pod itself (no chooser), and reconnects by itself, screen off too: a "ready"
// after a drop means a new link with TX notifications on, and it's this side's turn to run the
// connection order and write the command.

const fromB64 = (b64) => Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
const toB64 = (u8) => btoa(String.fromCharCode(...u8));

/**
 * CRPClient's transport over one link of the PodLink plugin: send() is plugin.write({ b, link }),
 * which the plugin runs in call order, one GATT operation at a time, and refuses for an older link.
 * The plugin's "pod" events go to PodNativeSource, which taps each packet into the decoder first
 * and then hands it here (deliver). "disconnected" once, when the plugin says the link is lost.
 */
export class PodNativeTransport extends EventEmitter {
  constructor(plugin, link, onSend = () => {}) {
    super();
    this.plugin = plugin;
    this.link = link;
    this.onSend = onSend;               // the capture's tx row
    this.up = true;
    this.last = Promise.resolve();      // the last write, settled
  }

  send(u8) {
    if (!this.up) return refused("The pod's link is down.");
    this.onSend(u8);
    let p;
    try {
      p = Promise.resolve(this.plugin.write({ b: toB64(u8), link: this.link }));
    } catch (err) {
      p = Promise.reject(err);
    }
    this.last = p.catch(() => {});      // also marks p handled: HELLO and BYE drop it
    return p;
  }

  /** Resolves once the last write has finished or failed (the plugin runs them in order). */
  idle() { return this.last; }

  deliver(u8) {
    if (this.up) this.emit("packet", u8);
  }

  down() {
    if (!this.up) return;
    this.up = false;
    this.emit("disconnected");          // CRPClient fails every open transaction now
  }
}

export class PodNativeSource extends PodSource {
  /**
   * plugin: Capacitor.Plugins.PodLink (tests hand in a fake). any: pick a new pod (the strongest in
   * range) instead of the saved one. startTimeoutMs: no first sample this long after connect().
   */
  constructor(onSample, { onEvent = () => {}, capture = false, plugin = null, any = false, startTimeoutMs = 10000 } = {}) {
    super(onSample, { onEvent, capture });
    this.plugin = plugin;
    this.any = any;
    this.startTimeoutMs = startTimeoutMs;
    this.listeners = [];
    this.held = [];                     // packets before connect() resolved: no clock yet (the pod sends none before HELLO)
    this.base = null;                   // wall ms - elapsedRealtime ms, from connect(): arrival = base + w
    this.link = 0;                      // the plugin's link our packets are for
    this.ord = null;                    // the session on this.link: { link, transport, crp, done, p } (order())
    this.ready = false;                 // the plugin said "ready" for this link (TX notifications on)
    this.linking = false;               // a reconnect's link is up and the plugin runs its GATT chain
    this.readyWait = null;              // End waiting for that chain to finish
    this.podName = "pod";
    this.address = null;
  }

  get name() { return this.podName; }

  onPod(e) {
    if (this.closed) return;
    if (this.base === null) { this.held.push(e); return; }
    const t = this.ord?.link === this.link ? this.ord.transport : null;
    for (let i = 0; i < e.f.length; i++) {
      const u8 = fromB64(e.f[i]);
      this.packet(u8, this.base + e.w[i]);       // the decoder first, in arrival order
      t?.deliver(u8);                            // then the session
    }
  }

  onState(e) {
    if (this.closed) return;
    if (e.state === "lost" || (e.state === "closed" && !this.stopping)) {
      this.ready = this.linking = false;
      this.ord?.transport.down();                // its open transactions fail now, not 5 s later
      this.readyWait?.();
      this.linkDown();
    } else if (e.state === "linking") {
      this.linking = true;              // a reconnect's link is up: "ready" (or nothing, if it drops) in a second or two
    } else if (e.state === "ready" && this.started) {
      // a reconnect: the plugin's part is done (TX notifications on); the connection order is ours
      this.link = e.link;               // also while End waits: its STOP goes over this link
      this.ready = true;
      this.linking = false;
      this.readyWait?.();
      if (this.stopping) return;        // End runs the order itself, then its STOP
      this.resume();                    // never rejects: it waits for the next "ready" itself
    }
  }

  /** The connection order on the plugin's link (docs/ble.md 7, steps 2 to 4) over that link's own
   * session, each step once: start(), resume() and stop() share it. */
  order(link) {
    let o = this.ord;
    if (o === null || o.link !== link) {
      o?.transport.down();
      const transport = new PodNativeTransport(this.plugin, link, (u8) => this.sent(u8));
      o = this.ord = { link, transport, crp: new PodCrpLink(transport, { heartbeatMs: 0 }), done: 0, p: null };
      this.crp = o.crp;
    }
    o.p ??= this.runOrder(o).finally(() => { o.p = null; });
    return o.p;
  }

  /** After a reconnect: the connection order on the new link, then STREAM_FROM the next index (or a
   * woken pod's 0). A step that fails on a link that's still up is tried again (1, 2, 4 s, then
   * every 5 s: the plugin sends no new "ready" for a link that stays up), from where it stopped; a
   * link that drops meanwhile waits for the next "ready", which runs its own resume(). */
  async resume() {
    const link = this.link;
    for (let k = 0; ; k++) {
      try {
        await this.order(link);
        // End came during the order (stop() sends its STOP, no stream command), or the pod said
        // going_to_sleep during it (its disconnect follows)
        if (this.stopping || this.closed || this.asleep) return;
        await this.orderCommand();
        // still this link, and the pod didn't say going_to_sleep meanwhile: it's back (also when End
        // came meanwhile: the link did come back, and End's STOP goes over it)
        if (!this.closed && link === this.link && this.ready && !this.asleep) this.linkUp({ link });
        return;
      } catch {
        await sleepMs(Math.min(1000 * 2 ** k, 5000));
        // End (no command after its STOP), a newer link (its own resume() runs), a drop, a going_to_sleep
        if (this.closed || this.stopping || link !== this.link || !this.ready || this.asleep) return;
      }
    }
  }

  whyNoData() {
    const st = this.stream.ep?.status;
    if (st && (st.flags & POD.S_MTU_TOO_SMALL)) return "The pod's Bluetooth link is too small (MTU). Try again.";
    if (this.stream.faults.length) return `Pod: ${this.stream.faults.at(-1)}.`;
    return "The pod sent no data in 10 s.";
  }

  /** connect() (Nearby devices the first time; the saved pod, or a scan), then the connection order
   * and the command; resolves at the first sample. The plugin bounds the connect itself (30 s once
   * allowed). */
  async start() {
    this.plugin ??= globalThis.Capacitor.Plugins.PodLink;
    let timer = null;
    try {
      // listeners first: the plugin's events can come before connect() resolves
      this.listeners.push(await this.plugin.addListener("pod", (e) => this.onPod(e)));
      this.listeners.push(await this.plugin.addListener("podState", (e) => this.onState(e)));
      const info = await this.plugin.connect({ any: this.any });
      this.alive();
      this.base = info.wallMs - info.elapsedMs;
      this.link = info.link;
      this.mtu = info.mtu;
      this.podName = info.name || "pod";
      this.address = info.address;
      const held = this.held;
      this.held = [];
      for (const e of held) this.onPod(e);
      const first = new Promise((res) => { this.gotData = () => { this.gotData = null; res(); }; });
      const lost = new Promise((_, rej) => { this.lostWait = () => rej(new Error("The pod's link dropped while starting. Try again.")); });
      const late = new Promise((_, rej) => { timer = setTimeout(() => rej(new Error(this.whyNoData())), this.startTimeoutMs); });
      const go = this.order(this.link).then(() => this.orderCommand());
      await Promise.race([go.then(() => first), late, lost]);
      this.started = true;
    } catch (err) {
      // the pod already holds a session for this command: release it (best effort)
      if (this.cmdSent && !this.linkLost && this.crp) {
        await Promise.race([this.crp.ctrl(new Uint8Array([POD.OP_STOP])), sleepMs(1000)]).catch(() => {});
      }
      await this.close();
      throw err;
    } finally {
      clearTimeout(timer);
      this.lostWait = null;
    }
  }

  /** STOP and its answer (when the link is up), then BYE and disconnect. Resolves after the last
   * sample. A reconnect the plugin finished ("ready") gets its STOP too, after the connection order
   * (still running, or not started yet), or the pod keeps its session hold: 30 min idle limit, no
   * lid-down (docs/ble.md 6); if that order failed after HELLO, the STOP still goes, unanswered
   * (stopOnly). The plugin writes in call order, so a command still in flight goes first, and
   * resume() sends nothing once stopping. End during the plugin's GATT chain ("linking") waits for
   * its "ready", at most 2 s. */
  async stop() {
    this.stopping = true;
    if (this.linkLost && this.linking && !this.ready) {
      await new Promise((res) => {
        const t = setTimeout(res, 2000);
        this.readyWait = () => { clearTimeout(t); res(); };
      });
      this.readyWait = null;
    }
    const up = () => !this.linkLost || (this.ready && !this.asleep);
    if (up() && this.base !== null && !this.closed) {
      await this.order(this.link).catch(() => {});
      const o = this.ord;                          // done: the order's steps that worked (HELLO .. PUSH)
      if (o?.link === this.link && o.done >= 1 && up() && !this.closed) {
        if (o.done >= 4) await this.stopHandshake((b) => o.crp.ctrl(b), up);
        else await this.stopOnly(o.crp);
      }
    }
    this.finishStream();
    await this.close();
  }

  async close() {
    this.closed = true;
    const o = this.ord;
    if (o) {
      // BYE ends the pod's session (docs/ble.md 2.2) on a link that's still up, before the disconnect
      const bye = o.transport.up && o.crp.peer !== null;
      if (!bye) o.transport.down();
      o.crp.close();                               // BYE (dropped on a link that's down), open transactions fail
      if (bye) await Promise.race([o.transport.idle(), sleepMs(500)]);
      o.transport.down();
    }
    const ls = this.listeners;
    this.listeners = [];
    for (const l of ls) { try { await l.remove(); } catch { /* gone */ } }
    try { await this.plugin?.disconnect(); } catch { /* already down */ }
  }
}
