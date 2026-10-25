// Data sources. Every source calls onSample({ t, ax, ay, az, gx, gy, gz })
// with t in seconds, acceleration in m/s^2 (including gravity), rotation in deg/s.
// The algorithm and UI never know which source they're getting (decision D4).
// NativeSensorSource is the Android app's phone sensor (docs/android.md A2). The pod's
// Bluetooth stream (PodLink, docs/ble.md) is decoded by PodStream, a line-for-line port of
// analysis/podlink.py checked by webapp/test/pod.mjs; PodWebSource (bottom) feeds it over Web
// Bluetooth. The app's own Bluetooth source comes in docs/ble-plan.md step 12.

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

export const POD = {
  SERVICE: "9ba40001-6de5-429c-b225-8134b9953e9d",
  DATA: "9ba40002-6de5-429c-b225-8134b9953e9d",
  CTRL: "9ba40003-6de5-429c-b225-8134b9953e9d",
  STATUS: "9ba40004-6de5-429c-b225-8134b9953e9d",
  INFO: "9ba40005-6de5-429c-b225-8134b9953e9d",
  CAL: "9ba40006-6de5-429c-b225-8134b9953e9d",
  T_DATA: 0x01, T_STATUS: 0x10, T_INFO: 0x20, T_CAL: 0x30,
  F_RESYNC: 0x01, F_OVERFLOW: 0x02, F_IMU_RESTART: 0x04, F_FINAL: 0x08,
  S_USB: 0x01, S_CHARGING: 0x02, S_BATT_LOW: 0x04, S_STREAMING: 0x08,
  S_GOING_TO_SLEEP: 0x10, S_STREAM_STOPPED: 0x20, S_MTU_TOO_SMALL: 0x40, S_BOOT_MISMATCH: 0x80,
  OP_STREAM_FROM: 0x01, OP_STREAM_LIVE: 0x02, OP_STOP: 0x03, OP_SLEEP: 0x10,
  ACCEL_LSB: 0.061 * 4 / 1000 * 9.80665,   // m/s^2 per count at +-8 g
  GYRO_LSB: 4.375 * 8 / 1000,              // deg/s per count at +-1000 deg/s
  TICK_HZ: 32768,
  LIVE_BACK: 600,                          // STREAM_LIVE lead-in for a fresh session (~2.8 s)
};
const SLEEP_REASONS = { 0: "none", 1: "idle", 2: "lid-down", 3: "low battery", 4: "command", 5: "IMU lost" };

/** The u32 difference x as a signed 32-bit number (Python: int32). */
const int32 = (x) => x | 0;

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
 * The phone's decoder for the pod's stream. Feed every frame of any type, in arrival order,
 * with its arrival time in ms: stream.frame(dataView, arrivalMs). Samples come out in index
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

  // ---- the one entry point ----
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
// The pod over Web Bluetooth (desktop and Android Chrome, on https or localhost; docs/ble-plan.md
// section 3 and step 6). Pick the pod with PodWebSource.pick() inside a click (the browser's
// chooser needs the user gesture), then use it like any other source: start(), stop(). A new
// PodWebSource per session gets its own PodStream, so t starts at 0 again. A dropped link
// reconnects by itself (step 10) with the same PodStream: STREAM_FROM the next index gets the
// missed samples back from the pod's 38 s ring, and a pod that slept or reset comes back as a new
// epoch with t still rising. stop() counts the tail.

/** Bytes of a DataView or Uint8Array as lowercase hex (the capture's payload column). */
const hexOf = (v) => Array.from(new Uint8Array(v.buffer, v.byteOffset, v.byteLength),
  (b) => b.toString(16).padStart(2, "0")).join("");
const sleepMs = (ms) => new Promise((res) => setTimeout(res, ms));
// Which source owns each pod's connection. Two sources share one BluetoothDevice (a new session
// after a failed one), and a connection is per device, so only the owner may disconnect it.
const podOwners = new WeakMap();

export class PodWebSource {
  /** The browser's device chooser, filtered to pods. Call it from a click handler. */
  static async pick() {
    if (!globalThis.navigator?.bluetooth) {
      throw new Error("This browser has no Web Bluetooth. Use Chrome (desktop or Android) or the app.");
    }
    return navigator.bluetooth.requestDevice({ filters: [{ services: [POD.SERVICE] }] });
  }

  /**
   * device: from pick(). onEvent(kind, info): the PodStream events plus ("link", "lost" |
   * "asleep" | "back"). capture: keep every frame, command and call (the make_fake_pod.py log
   * format, so podlink.py can replay it) and every decoded sample. backoffMs: waits between
   * reconnect tries, the last one repeating (docs/ble-plan.md 3); connectTimeoutMs: a connect that
   * hangs (the pod out of range) is cancelled after this. Tests shorten all three.
   */
  constructor(onSample, device, { onEvent = () => {}, capture = false, startTimeoutMs = 10000,
    backoffMs = [500, 1000, 2000, 4000, 5000], connectTimeoutMs = 10000 } = {}) {
    this.device = device;
    this.onEvent = onEvent;
    this.capture = capture ? { log: [], samples: [] } : null;
    this.startTimeoutMs = startTimeoutMs;   // no first sample by then: start() fails (tests shorten it)
    this.backoffMs = backoffMs;
    this.connectTimeoutMs = connectTimeoutMs;
    this.chars = null;
    this.closed = false;                // close() ran: a setup still in flight must stop and touch nothing
    this.linkLost = false;
    this.started = false;               // start() succeeded: from then on a dropped link reconnects
    this.stopping = false;              // stop() ran: a drop now is the end, not something to fix
    this.reconnecting = false;          // the reconnect loop is running
    this.holdOffUntil = 0;              // no reconnect try before this Date.now() (barDebug outages)
    this.asleep = null;                 // the pod said it went to sleep (the reason) and hasn't woken (a new boot)
    this.sleepMode = null;              // the mode of the SLEEP op we sent (1 = IMU off: motion won't wake it)
    this.reconnects = 0;                // links that came back
    this.charsLive = false;             // this.chars belong to the current connection (not a dropped one)
    this.linkReady = false;             // ...and DATA notifications are on: a STOP handshake can be heard
    this.inSetup = false;               // the connection order runs: its own command answers any request
    this.setupP = null;                 // the reconnect's setup in flight (stop() lets it finish first)
    this.linkEvents = [];               // { at: Date.now(), kind: lost / asleep / back, ... } for link.json
    this.cmdSent = false;               // a STREAM command went out (the pod holds the session)
    this.gotData = null;                // resolves start() at the first sample
    this.lostWait = null;               // fails start() when the link drops
    this.stopped = null;                // resolves stop()'s wait at the stream_stopped STATUS
    this.wake = null;                   // ends stop()'s wait early when the link drops
    this.stream = new PodStream((s) => {
      if (this.closed) return;
      if (this.capture) this.capture.samples.push(s);
      this.gotData?.();
      onSample(s);
    }, (kind, info) => this.event(kind, info));
    this.onValue = (e) => { if (!this.closed) this.frame(e.target.value); };
    this.onGone = () => {
      const was = this.linkLost;
      this.linkLost = true;
      this.charsLive = this.linkReady = false;
      this.lostWait?.();
      this.wake?.();
      if (this.closed || this.stopping) return;
      if (!was) {                       // once per outage, not per failed reconnect try
        const kind = this.asleep ? "asleep" : "lost";
        this.linkEvents.push({ at: Date.now(), kind, ...(this.asleep ? { reason: this.asleep } : {}) });
        this.onEvent("link", kind);
      }
      if (this.started) this.reconnect();
    };
  }

  /** After every await in the setup: a closed source (start() timed out) gives up here. */
  alive() {
    if (this.closed) throw new Error("The pod source was closed.");
  }

  get name() { return this.device.name || "pod"; }

  log(kind, arrival, payload) {
    this.capture?.log.push([kind, arrival, payload]);
  }

  /** One frame (a read or a notification) into the decoder, stamped on arrival. Nothing after the
   * session ended: a reconnect's late read must not add an epoch after finish(). */
  frame(dv) {
    if (this.closed) return;
    const t = Date.now();
    this.log("frame", t, hexOf(dv));
    this.stream.frame(dv, t);
  }

  event(kind, info) {
    if (kind === "stopped") this.stopped?.();
    if (kind === "sleep") this.asleep = info;   // its deliberate disconnect follows: wait for the wake
    if (kind === "epoch" && info.reboot) { this.asleep = null; this.sleepMode = null; }   // a wake is a new boot
    // The decoder asks: the pod rebooted without a new boot_id, or our INFO was stale. Not during
    // the connection order: the command it is about to write answers that (docs/ble.md 7).
    if (kind === "need-command" && !this.inSetup) this.command().catch(() => {});
    if (kind === "need-info" && !this.inSetup) this.reread().catch(() => {});
    this.onEvent(kind, info);
    // asleep while we were already reconnecting (it said so during a setup): the banner must know
    if (kind === "sleep" && this.linkLost && this.started && !this.closed && !this.stopping) {
      this.linkEvents.push({ at: Date.now(), kind: "asleep", reason: info });
      this.onEvent("link", "asleep");
    }
  }

  /** Connect and find the characteristics. Right after a disconnect (End, then Start at once)
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
        const ch = {};
        for (const [k, u] of [["data", POD.DATA], ["ctrl", POD.CTRL], ["status", POD.STATUS], ["info", POD.INFO]]) {
          ch[k] = await svc.getCharacteristic(u);
        }
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

  /** Connect and run the connection order (docs/ble.md 7). Also the reconnect: the same stream,
   * so its command is STREAM_FROM the next index (or index 0 of a pod that woke up). */
  async setup(attempts = 3) {
    this.inSetup = true;
    try {
      const ch = await this.discover(attempts);
      this.chars?.data.removeEventListener("characteristicvaluechanged", this.onValue);   // the old link's
      this.chars = ch;
      this.charsLive = true;
      this.frame(await ch.info.readValue());
      this.alive();
      this.frame(await ch.status.readValue());
      this.alive();
      ch.data.addEventListener("characteristicvaluechanged", this.onValue);
      this.device.addEventListener("gattserverdisconnected", this.onGone);
      await ch.data.startNotifications();          // STATUS frames come on DATA too (D28)
      this.alive();
      this.linkReady = this.charsLive;
      this.inSetup = false;                        // from here a request is answered as it comes
      await this.command();
    } finally {
      this.inSetup = false;
    }
  }

  async command() {
    const b = this.stream.command();
    this.log("cmd", "", b ? hexOf(b) : "none");
    if (!b) throw new Error(`The pod can't stream: ${this.stream.faults.at(-1) ?? "not ready"}.`);
    this.cmdSent = true;
    await this.chars.ctrl.writeValueWithResponse(b);
  }

  async reread() {
    this.frame(await this.chars.info.readValue());
    this.frame(await this.chars.status.readValue());
    await this.command();
  }

  /** Why no sample came, from the latest STATUS (start()'s 10 s timeout). */
  whyNoData() {
    const st = this.stream.ep?.status;
    if (!this.chars) return "Couldn't reach the pod. Bump it to wake it, keep it close, and try again.";
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
      if (this.cmdSent && !this.linkLost && this.chars && this.device.gatt.connected) {
        await Promise.race([this.chars.ctrl.writeValueWithResponse(new Uint8Array([POD.OP_STOP])), sleepMs(1000)]).catch(() => {});
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
          this.linkLost = false;
          this.reconnects += 1;
          this.linkEvents.push({ at: Date.now(), kind: "back", tries: k + 1 });
          this.onEvent("link", "back");
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

  /** The SLEEP op (docs/ble.md 4): mode 0 wakes on motion, 1 = IMU off until USB or reset. */
  async sleepPod(mode = 0) {
    if (!this.chars || this.linkLost) throw new Error("The pod isn't connected.");
    const b = new Uint8Array([POD.OP_SLEEP, mode]);
    this.log("ctrl", "", hexOf(b));               // not "cmd": a replay checks those against command()
    this.sleepMode = mode;
    await this.chars.ctrl.writeValueWithResponse(b);
  }

  /**
   * STOP, wait for the pod's stream_stopped STATUS (everything up to its head has arrived, or
   * the tail is counted as a gap), then disconnect. Resolves after the last sample. While the
   * link is down (reconnecting) there is nothing to STOP: the tail is counted and the loop ends.
   */
  async stop() {
    this.stopping = true;
    // A reconnect mid-setup may already hold a live link: let it finish (at most 1 s), then STOP
    // over it, or the pod keeps the session hold: 30 min idle limit, no lid-down (docs/ble.md 6)
    if (this.setupP && this.linkLost && this.charsLive) await Promise.race([this.setupP.catch(() => {}), sleepMs(1000)]);
    const ch = this.chars;
    const live = ch && this.device.gatt.connected && podOwners.get(this.device) === this;
    if (live && this.linkLost && this.charsLive && !this.linkReady) {
      // a link without DATA notifications yet: nothing would hear the answer, so just release the hold
      const b = this.stream.stopCommand();
      this.log("stop", "", hexOf(b));
      await Promise.race([ch.ctrl.writeValueWithResponse(b), sleepMs(1000)]).catch(() => {});
    } else if (live && (!this.linkLost || this.linkReady)) {
      let done = false;
      let failed = false;
      const stopped = new Promise((res) => { this.stopped = () => { done = true; res(); }; this.wake = res; });
      const b = this.stream.stopCommand();
      this.log("stop", "", hexOf(b));
      const wait = this.stream.stopWaitMs();
      try {
        await ch.ctrl.writeValueWithResponse(b);
        await Promise.race([stopped, sleepMs(wait)]);
      } catch {
        failed = true;                           // the link went: finish() counts the tail
      }
      this.stopped = this.wake = null;
      // a real timeout only; a dropped link goes straight to finish() (docs/ble.md 7)
      if (!done && !failed && !this.linkLost) this.log("call", "", `stop_timed_out=${this.stream.stopTimedOut() ? 1 : 0}`);
    }
    this.stream.finish();
    this.log("call", "", "finish");
    await this.close();
  }

  async close() {
    this.closed = true;
    this.charsLive = this.linkReady = false;
    this.device.removeEventListener("gattserverdisconnected", this.onGone);
    const data = this.chars?.data;
    this.chars = null;
    const owner = podOwners.get(this.device) === this;
    if (data) {
      data.removeEventListener("characteristicvaluechanged", this.onValue);
      // the characteristic is shared with any newer source on this pod: only the owner unsubscribes
      if (owner) { try { await data.stopNotifications(); } catch { /* already gone */ } }
    }
    if (owner) {
      podOwners.delete(this.device);
      try { this.device.gatt.disconnect(); } catch { /* already gone */ }
    }
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
      perPacket: st?.n_per_packet ?? null, intervalMs: st ? st.interval * 1.25 : null,
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
      mtu: null,                                  // Web Bluetooth doesn't tell (the pod's # link line does)
    };
  }

  /** The capture as a CSV in make_fake_pod.py's vector format (scenario "capture"). */
  captureCSV() {
    if (!this.capture) return "";
    const rows = this.capture.log.map(([k, a, p], i) => `capture,${i},${k},${a},${p}`);
    return ["scenario,step,kind,arrival_ms,payload", ...rows].join("\n") + "\n";
  }
}
