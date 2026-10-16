// Data sources. Every source calls onSample({ t, ax, ay, az, gx, gy, gz })
// with t in seconds, acceleration in m/s^2 (including gravity), rotation in deg/s.
// The algorithm and UI never know which source they're getting (decision D4).
// Phase 4 adds a BleSource (Web Bluetooth) with the same shape.

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

/**
 * Plays back a recording (from fileio.parseRecording).
 * realtime = true: at the speed it was recorded, like a live session.
 * realtime = false: all at once, for checking results quickly.
 */
export class ReplaySource {
  constructor(onSample, rec, { realtime = false, onDone = () => {} } = {}) {
    this.onSample = onSample;
    this.rec = rec;
    this.realtime = realtime;
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
      const now = (performance.now() - wall0) / 1000;
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
