// Accelerometer calibration from 6 still positions (algorithm.md section 1). Port of
// analysis/calib.py. Per axis: scale = (up - down) / 2g, bias = (up + down) / 2,
// corrected = (raw - bias) / scale. Applied to every live sample (D23). The pod's calibration is made
// over USB by analysis/calib_pod.py and ships with the app (pod-calibration.json, D42).

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

/** A pod's serial the way podlink.py prints it: 0x41ef151f. */
export const podHex = (n) => `0x${(n >>> 0).toString(16).padStart(8, "0")}`;

export const POD_SCALE_OK = 0.05;   // calib_pod.SCALE_OK: --install refuses a scale further than this from 1

/** Why a pod-calibration.json can't be applied, or null if it can: the shape calib_pod.py --install
 * writes (install_problem there checks the same). Accel bias and scale 3 finite numbers each, gyro scale
 * 3 (a null: that axis left as it is), every scale within POD_SCALE_OK of 1, gyro bias null or 3 numbers
 * (or nulls), the pod's serial null or a 32-bit number, and when it was made. */
export function podCalibrationProblem(c) {
  const num = (x) => typeof x === "number" && Number.isFinite(x);
  const three = (x, ok) => Array.isArray(x) && x.length === 3 && x.every(ok);
  const scale = (s) => num(s) && Math.abs(s - 1) <= POD_SCALE_OK;
  const pct = `${POD_SCALE_OK * 100}%`;
  if (typeof c !== "object" || c === null || Array.isArray(c)) return "it isn't a JSON object";
  if (!three(c.accel?.bias, num)) return "its accel bias isn't 3 numbers";
  if (!three(c.accel?.scale, scale)) return `its accel scale isn't 3 numbers within ${pct} of 1`;
  if (!three(c.gyro?.scale, (s) => s === null || scale(s))) return `its gyro scale isn't 3 numbers (or nulls) within ${pct} of 1`;
  if (!(c.gyro.bias == null || three(c.gyro.bias, (b) => b === null || num(b)))) return "its gyro bias isn't null or 3 numbers (or nulls)";
  if (!(c.pod == null || (Number.isInteger(c.pod) && c.pod >= 0 && c.pod <= 0xffffffff))) return "its pod serial isn't null or a 32-bit number";
  if (typeof c.made !== "string" || !c.made) return "it has no \"made\" time";
  return null;
}

/** The pod's calibration if it's this pod's (D42): one made on another pod (calib_pod.py read its serial
 * off the USB port; this one's INFO says another, D27) isn't applied. One with no serial is. */
export function podCalibrationFor(pc, serial) {
  if (!pc) return null;
  return pc.pod === null || pc.pod === undefined || pc.pod === serial ? pc : null;
}

/** A pod sample with its calibration applied, accel and gyro: corrected = (raw - bias) / scale per axis
 * (calib_pod.apply() in Python). A gyro bias or scale it hasn't got (no rest, no turn about that axis)
 * is skipped. */
export function applyPodCalibration(s, pc) {
  if (!pc) return s;
  const a = pc.accel, gb = pc.gyro.bias ?? [null, null, null], gs = pc.gyro.scale;
  return { ...s,
    ax: (s.ax - a.bias[0]) / a.scale[0],
    ay: (s.ay - a.bias[1]) / a.scale[1],
    az: (s.az - a.bias[2]) / a.scale[2],
    gx: (s.gx - (gb[0] ?? 0)) / (gs[0] ?? 1),
    gy: (s.gy - (gb[1] ?? 0)) / (gs[1] ?? 1),
    gz: (s.gz - (gb[2] ?? 0)) / (gs[2] ?? 1) };
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
