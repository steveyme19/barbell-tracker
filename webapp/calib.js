// Accelerometer calibration from 6 still positions (algorithm.md section 1). Port of
// analysis/calib.py. Per axis: scale = (up - down) / 2g, bias = (up + down) / 2,
// corrected = (raw - bias) / scale. Applied to every live sample (D23).

export const G_LOCAL = 9.80;   // m/s^2 (West Lafayette ~9.801)

/** [axis, sign] of the axis closest to vertical in a still reading [x, y, z]. */
export function orientation(m) {
  let k = 0;
  for (let i = 1; i < 3; i++) if (Math.abs(m[i]) > Math.abs(m[k])) k = i;
  return [k, m[k] > 0 ? 1 : -1];
}

/** 6 still readings, one per face -> { bias, scale, residual } or null if a face is missing. */
export function fitCalibration(means, g = G_LOCAL) {
  const up = [null, null, null], down = [null, null, null];
  for (const m of means) {
    const [k, s] = orientation(m);
    (s > 0 ? up : down)[k] = m[k];
  }
  if (up.includes(null) || down.includes(null)) return null;
  const scale = [0, 1, 2].map((k) => (up[k] - down[k]) / (2 * g));
  const bias = [0, 1, 2].map((k) => (up[k] + down[k]) / 2);
  let residual = 0;
  for (const m of means) {
    const c = [0, 1, 2].map((k) => (m[k] - bias[k]) / scale[k]);
    residual = Math.max(residual, Math.abs(Math.hypot(...c) - g));
  }
  return { bias, scale, residual };
}

/** A sample with the calibration applied to its acceleration (gyro untouched). */
export function applyCalibration(s, cal) {
  if (!cal) return s;
  return { ...s,
    ax: (s.ax - cal.bias[0]) / cal.scale[0],
    ay: (s.ay - cal.bias[1]) / cal.scale[1],
    az: (s.az - cal.bias[2]) / cal.scale[2] };
}

/**
 * Collects the 6 positions from a live sample stream. push(sample) returns the step's
 * state: it waits for STILL_S seconds of stillness in an orientation it hasn't got yet,
 * then records the average. done when all 6 faces are in.
 */
export class CalibrationRun {
  constructor() {
    this.means = [];            // one [x, y, z] per face
    this.faces = new Set();     // "x+", "z-", ...
    this.win = [];              // recent samples
  }

  static STILL_S = 2.0;         // s of stillness per face
  static STILL_STD = 0.04;      // m/s^2: max wiggle of |a| to count as still

  get done() { return this.faces.size === 6; }

  push(s) {
    this.win.push(s);
    while (this.win.length && s.t - this.win[0].t > CalibrationRun.STILL_S) this.win.shift();
    if (this.done || s.t - this.win[0].t < CalibrationRun.STILL_S * 0.95) return null;
    const n = this.win.length;
    const mag = this.win.map((x) => Math.hypot(x.ax, x.ay, x.az));
    const mm = mag.reduce((a, b) => a + b, 0) / n;
    const sd = Math.sqrt(mag.reduce((a, b) => a + (b - mm) ** 2, 0) / n);
    if (sd > CalibrationRun.STILL_STD) return null;
    const m = ["ax", "ay", "az"].map((k) => this.win.reduce((a, x) => a + x[k], 0) / n);
    const [k, sign] = orientation(m);
    const face = "xyz"[k] + (sign > 0 ? "+" : "-");
    if (this.faces.has(face)) return null;
    this.faces.add(face);
    this.means.push(m);
    this.win = [];
    return face;
  }
}
