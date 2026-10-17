// Data sources. Every source calls onSample({ t, ax, ay, az, gx, gy, gz })
// with t in seconds, acceleration in m/s^2 (including gravity), rotation in deg/s.
// The algorithm and UI never know which source they're getting (decision D4).
// Phase 4 adds a BleSource (Web Bluetooth) with the same shape. NativeSensorSource is
// the Android app's phone sensor (docs/android.md A2).

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

// The Capacitor bridge puts window.Capacitor in the Android app (no import needed, so
// webapp/ keeps no build step). In a normal browser it isn't there.
let barSensors = null;
export function isNativeApp() {
  return !!window.Capacitor?.isNativePlatform?.();
}

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
    // The bridge builds a proxy for every native plugin; registerPlugin lives in
    // @capacitor/core, which a no-build app never loads.
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
