// Rep detection, ported line for line from analysis/reps.py.
// Python is the reference (decision P1). If you change the algorithm, change it
// in Python first, then here, then rerun the parity test (webapp/test/parity.mjs).
//
// Pure functions only: no DOM, so it runs in the browser and in Node.

export const SETTINGS = {
  STILL_WINDOW: 0.2,     // s: window for the stillness test
  STILL_STD: 0.15,       // m/s^2: max wiggle inside the window
  STILL_DEV: 0.3,        // m/s^2: max |average - g| inside the window
  NOISE_STD_K: 4.0,      // noise-matched limits (D20): 4x the rest wiggle ...
  NOISE_DEV_K: 5.0,      // ... and 5x it for |average - g|,
  STILL_STD_MIN: 0.06,   // m/s^2  but never below these
  STILL_DEV_MIN: 0.08,   // m/s^2  (and never above STILL_STD / STILL_DEV)
  MIN_STILL: 0.25,       // s: a still period has to last at least this long
  STILL_MAX_SPEED: 0.1,  // m/s: faster than this during a "still" period = coasting
  LONG_STILL: 1.0,       // s: stills at least this long are real (nothing coasts that long)
  COAST_TRUST: 4.0,      // s: past this long since the last anchor, drift is too big for the speed check
  COAST_LOOK: 0.5,       // s: how far before and after a "still" period to look
  COAST_DV: 0.1,         // m/s: speed change on both sides that marks a coast
  STILL_MAX_HUMP: 0.12,  // m/s: velocity can't rise and fall more than this inside a still period (D15)
  MIN_ROM: 0.05,         // m: smaller up/down moves are wiggles, not reps
  V_ZERO: 1e-9,          // m/s: "not moving up". Drift-corrected anchors are 0 up to roundoff (±1e-17)
  V_STOP_FRAC: 0.03,     // look for a rep's start/end where v <= this x its peak, then walk on while v
                         // keeps falling: to v = 0, or to where a too-short pause's drift floor begins (D32)
  WALK_MIN_FS: 150,      // Hz: the walk needs v smooth near zero; Chrome's 60 Hz wobbles there (D32)
  STICK_SMOOTH: 0.05,    // s: velocity averaged over this before looking for the sticking dip
  STICK_PROM: 0.10,      // the dip sits this x the rep's peak below the lower peak around it (provisional, 5.3)
  STICK_RANGE: [0.10, 0.90],   // ... between these fractions of the ROM
  LONG_WINDOW: 4.0,      // s: flag reps inside longer windows (more drift)
  BIG_CORRECTION: 0.15,  // m/s: flag windows whose drift correction was this big
  LEVEL_MAX_DEG: 3,      // highest power of t in the velocity drift curve (lockouts only)
  LEVEL_MAX_DEG_BOTH: 5, // same, when bottoms are leveled too
  LEVEL_FLAG: 0.02,      // m: flag a rep whose lockout misses the common height by this much
  // Real bars (D40): a window with no still between its reps is redone rep by rep, anchored on its
  // turnarounds (turnaroundVelocity). See TA_* in reps.py for the why of each number.
  TA_BROKEN_V: 1.0,      // m/s: velocity left at an anchored window's end over this: redo it
  TA_BROKEN_TAIL: 3.0,   // m/s: the same for a tail with no end still yet (live, mid-set)
  TA_MIN_WIN: 1.0,       // s: a shorter window is never redone
  TA_BAR: ["squat", "bench", "deadlift", "free"],   // bar lifts: an impact redoes their window too
  TA_RACKED: ["squat", "bench"],   // these end at the rack (the first impact after the first lockout, D43)
  IMPACT_A: 20.0,        // m/s^2: |a - g| over this is a jolt (the hooks, the floor)
  IMPACT_W: 100.0,       // deg/s: an impact is a jolt with the gyro (offset off) over this ...
  IMPACT_NEAR: 0.05,     // s     ... within this of it
  IMPACT_PAD: 0.1,       // s: blanked around each impact in the coarse pass (on the floor any jolt)
  JOLT_PAD: 0.02,        // s: blanked around each impact when a span is integrated again
  TA_SIGMA: 0.75,        // s: the coarse pass removes drift slower than this (Gaussian high-pass)
  TA_SIGMA_LIFT: { bench: 1.75 },   // s: ... longer on the bench: paused reps are slower (D43)
  TA_MIN_MOVE: 0.15,     // m: a coarse turnaround moves at least this much
  TA_HOOK_MOVE: 0.15,    // m: a racked lift's move off the hooks under this is the unrack or rerack (D43)
  TA_REST: { deadlift: "bottom", machine: "bottom" },   // where the bar rests between reps; others "top"
  TA_FLAT_WIN: 0.37,     // s: "flat" = acceleration averaged over this stays within
  TA_FLAT_A: 0.5,        // m/s^2  this of 0
  TA_PAUSE: 0.25,        // s: a flat stretch this long next to a turnaround can be a pause ...
  TA_REACH: 0.5,         // s: ... if it reaches to within this of the coarse turnaround ...
  TA_FLOOR_REACH: 1.0,   // s: ... or, on the floor, ends within this before it (rep 1's low lands at a hitch)
  TA_COAST_DV: 0.35,     // m/s: ... isn't a coast (speed changes of opposite sign, both over this) ...
  TA_NEAR_FRAC: 0.4,     // ... and is at the turnaround's height (within this x the bigger move)
  TA_HOLD_V: 0.03,       // m/s: ... and the coarse height moves through it slower than this (D43)
  TA_EDGE: 0.25,         // s: the first/last pause's anchor averages 2 x this next to the set (and a
                         //    lockout that ends TA_START_DROP lower than it started or under its top, D43)
  TA_START_DROP: 0.005,  // m: ... on the coarse height
  TA_SET_EDGE: 3.0,      // s: a window still this close to the first/last anchor is part of the set
  // The vertical (D40): bar lifts at the pod's rate use the acceleration along up (Madgwick, D33)
  VERTICAL_FS: [150, 300],   // Hz: only at the pod's rate (a 402 Hz hand-held phone stays on |a|)
  QS_AVG: 0.1,           // s: accel and gyro averaged over this for the quasi-still test
  QS_DEV: 0.15,          // m/s^2: | |mean accel| - g | under this ...
  QS_GYRO: 3.0,          // deg/s: ... and the mean turning (bias off, not about the bar) under this ...
  BAR_AXIS: 2,           // the pod axis along the bar (Adapter A: z)
  QS_MIN: 0.25,          // s: ... this long = quasi-still (attitude only, never a velocity anchor)
  BETA_STILL: 0.2,       // Madgwick gain while still (rad/s)
  BETA_MOVE: 0.0,        // while moving: the gyro alone
  GATE_DEG: 3.0,         // a still's accel must point within this of the estimated up to pull on it
  SN_FLOOR: 0.05,        // the gradient's norm, floored: a proportional pull near convergence (see reps.py)
  // Which heights repeat every rep (see LIFTS in reps.py).
  LIFTS: {
    phone: { bottoms: false, ends: false },    // hand-held: you don't hold still at the turnaround height
    squat: { bottoms: false, ends: true },     // depth varies; starts and ends standing
    bench: { bottoms: true, ends: true },      // touches the chest; starts and ends at lockout
    deadlift: { bottoms: true, ends: true },   // touches the floor; starts and ends on the floor
    machine: { bottoms: true, ends: true },    // weight stack: comes to rest on the stack every rep (D19)
    free: { bottoms: false, ends: true },      // a free weight with no name we know (row, OHP, curl): lockouts only (D21)
  },
  LIFT: "phone",
  // Machine or free weight, from how the sensor moves (D21). See detect_mount() in reps.py.
  MOUNT_MIN_MOVE: 1.0,   // s: need this much motion between the stills to judge
  MACHINE_ROT: 3.0,      // deg/s: median turning speed while moving. Stack ~0.7, hand 10 to 25
  MACHINE_SIDE: 0.35,    // sideways / vertical acceleration while moving. Stack ~0.2, hand 0.36 to 2
  NAMED_WINS: ["phone", "squat", "bench", "deadlift"],   // these names say more than the sensor can
};

/** 'bar-bench-2026-11-03-1830.csv' -> 'bench'. Anything else -> the default lift. */
// The word right after "bar-" wins (session files end in the exercise name, e.g.
// bar-machine-...-set02-lat-pulldown.csv); else any lift name in it; else the default.
export function liftFromName(name, S = SETTINGS) {
  const lower = name.split(/[\\/]/).pop().toLowerCase();   // file name only, either slash
  const first = lower.startsWith("bar-") ? lower.slice(4).split("-")[0] : "";
  if (first in S.LIFTS) return first;
  return Object.keys(S.LIFTS).find((l) => lower.includes(l)) ?? S.LIFT;
}

/** Typed exercise name -> tracking profile (D19). Squat/bench/deadlift by name, "phone"
 *  for a hand-held test, anything else "machine", which only counts when the sensor
 *  can't tell machine from free weight (chooseLift, D21). */
export function liftForExercise(exercise) {
  const lower = exercise.toLowerCase();
  return ["phone", "deadlift", "bench", "squat"].find((l) => lower.includes(l)) ?? "machine";
}

/**
 * 'machine' (weight stack, Smith, sled on rails) or 'free' (bar, dumbbell, hand), from the
 * motion after the first still (D21), as { kind, rot, side }. null if there's no gyro data
 * or too little motion yet. imu = { ax, ay, az, gx, gy, gz } arrays. See detect_mount() in
 * reps.py: rot = median turning speed while moving (deg/s, gyro offset from the stills
 * taken off), side = rms sideways / rms vertical acceleration while moving (smoothed over
 * STILL_WINDOW so stack rattle doesn't count).
 */
export function detectMount(imu, runs, fs, S = SETTINGS) {
  if (!imu || !imu.gx) return null;
  const N = imu.gx.length;
  let anyGyro = false;
  for (let i = 0; i < N && !anyGyro; i++) anyGyro = imu.gx[i] !== 0 || imu.gy[i] !== 0 || imu.gz[i] !== 0;
  if (!anyGyro) return null;
  const still = new Uint8Array(N);
  for (const [s, e] of runs) for (let i = s; i <= e; i++) still[i] = 1;
  const moving = [];                                       // after the first still, not still
  for (let i = runs[0][1] + 1; i < N; i++) if (!still[i]) moving.push(i);
  if (moving.length < S.MOUNT_MIN_MOVE * fs) return null;
  // gyro offset and gravity direction: averages over the still samples
  const off = [0, 0, 0];
  const up = [0, 0, 0];
  let cnt = 0;
  for (let i = 0; i < N; i++) {
    if (!still[i]) continue;
    off[0] += imu.gx[i]; off[1] += imu.gy[i]; off[2] += imu.gz[i];
    up[0] += imu.ax[i]; up[1] += imu.ay[i]; up[2] += imu.az[i];
    cnt++;
  }
  for (let k = 0; k < 3; k++) { off[k] /= cnt; up[k] /= cnt; }
  const turn = moving.map((i) => Math.hypot(imu.gx[i] - off[0], imu.gy[i] - off[1], imu.gz[i] - off[2]));
  const rot = median(turn);
  const g = Math.hypot(up[0], up[1], up[2]);
  for (let k = 0; k < 3; k++) up[k] /= g;
  const n = Math.trunc(S.STILL_WINDOW * fs);
  const [sx, sy, sz] = [imu.ax, imu.ay, imu.az].map((y) => rolling(y, n).mean);
  let side2 = 0;
  let vert2 = 0;
  for (const i of moving) {
    const along = sx[i] * up[0] + sy[i] * up[1] + sz[i] * up[2];
    const px = sx[i] - along * up[0], py = sy[i] - along * up[1], pz = sz[i] - along * up[2];
    side2 += px * px + py * py + pz * pz;
    vert2 += (along - g) ** 2;
  }
  const side = Math.sqrt(side2 / vert2);
  const kind = rot < S.MACHINE_ROT && side < S.MACHINE_SIDE ? "machine" : "free";
  return { kind, rot, side };
}

/** The profile a set is tracked with (D21). See choose_lift() in reps.py. */
export function chooseLift(named, mount, S = SETTINGS) {
  if (S.NAMED_WINS.includes(named)) return named;
  if (mount) return mount.kind;
  return named || "free";
}

/** Total acceleration, sqrt(x^2 + y^2 + z^2). Tilt-proof vertical signal (D9). */
export function magnitude(ax, ay, az) {
  return ax.map((x, i) => Math.sqrt(x * x + ay[i] * ay[i] + az[i] * az[i]));
}

/** Running trapezoid integral of y over t, starting at 0. */
export function integrate(y, t) {
  const out = new Float64Array(y.length);
  for (let i = 1; i < y.length; i++) {
    out[i] = out[i - 1] + 0.5 * (y[i] + y[i - 1]) * (t[i] - t[i - 1]);
  }
  return out;
}

export function median(arr) {
  const s = Array.from(arr).sort((a, b) => a - b);
  const m = s.length >> 1;
  return s.length % 2 ? s[m] : 0.5 * (s[m - 1] + s[m]);
}

/** Centered rolling mean and sample std, same window and edges as pandas. */
export function rolling(y, n) {
  const N = y.length;
  const minP = Math.max(Math.floor(n / 2), 1);
  const back = Math.floor(n / 2);
  const fwd = Math.floor((n - 1) / 2);
  // prefix sums for O(N)
  const s1 = new Float64Array(N + 1);
  const s2 = new Float64Array(N + 1);
  for (let i = 0; i < N; i++) {
    s1[i + 1] = s1[i] + y[i];
    s2[i + 1] = s2[i] + y[i] * y[i];
  }
  const mean = new Float64Array(N);
  const std = new Float64Array(N);
  for (let i = 0; i < N; i++) {
    const lo = Math.max(i - back, 0);
    const hi = Math.min(i + fwd, N - 1);
    const k = hi - lo + 1;
    if (k < minP) { mean[i] = NaN; std[i] = 0; continue; }
    const sum = s1[hi + 1] - s1[lo];
    const sq = s2[hi + 1] - s2[lo];
    mean[i] = sum / k;
    std[i] = k > 1 ? Math.sqrt(Math.max((sq - (sum * sum) / k) / (k - 1), 0)) : 0;
  }
  return { mean, std };
}

/** [start, end] index pairs of still periods at least MIN_STILL long. */
export function findStillRuns(aRaw, g, fs, S = SETTINGS, stdMax = S.STILL_STD, devMax = S.STILL_DEV) {
  const n = Math.trunc(S.STILL_WINDOW * fs);
  const { mean, std } = rolling(aRaw, n);
  const N = aRaw.length;
  const still = (i) => std[i] < stdMax && Math.abs(mean[i] - g) < devMax;
  const runs = [];
  let i = 0;
  while (i < N) {
    if (still(i)) {
      let j = i;
      while (j < N && still(j)) j++;
      if ((j - i) / fs >= S.MIN_STILL) {
        // a sample only counts as still once its whole window is quiet,
        // so widen the run back out by half a window on each side
        runs.push([Math.max(i - Math.floor(n / 2), 0), Math.min(j - 1 + Math.floor(n / 2), N - 1)]);
      }
      i = j;
    } else {
      i++;
    }
  }
  return runs;
}

/**
 * Noise-matched still limits (D20): median wiggle inside the longest still, times
 * NOISE_*_K, clamped between the *_MIN floors and the default limits. See reps.py.
 */
export function stillLimits(aRaw, fs, runs, S = SETTINGS) {
  const n = Math.trunc(S.STILL_WINDOW * fs);
  const { std } = rolling(aRaw, n);
  let best = runs[0];
  for (const r of runs) if (r[1] - r[0] > best[1] - best[0]) best = r;
  const noise = median(Array.from(std.slice(best[0], best[1] + 1)));
  const stdMax = Math.min(S.STILL_STD, Math.max(S.STILL_STD_MIN, S.NOISE_STD_K * noise));
  const devMax = Math.min(S.STILL_DEV, Math.max(S.STILL_DEV_MIN, S.NOISE_DEV_K * noise));
  return { stdMax, devMax, noise };
}

/** Drop "still" periods that are really steady motion (accelerometers can't tell). */
/** Change in velocity from sample i to sample j (clipped to the recording). */
function speedChange(a, t, i, j) {
  i = Math.max(i, 0);
  j = Math.min(j, t.length - 1);
  if (j <= i) return 0;
  const v = integrate(a.slice(i, j + 1), t.slice(i, j + 1).map((x) => x - t[i]));
  return v[v.length - 1];
}

/** Least-squares straight line through (x, y): returns [slope, intercept] like numpy.polyfit(x, y, 1). */
function lineFit(x, y) {
  const n = x.length;
  let sx = 0, sy = 0;
  for (let i = 0; i < n; i++) { sx += x[i]; sy += y[i]; }
  const mx = sx / n, my = sy / n;
  let sxy = 0, sxx = 0;
  for (let i = 0; i < n; i++) { sxy += (x[i] - mx) * (y[i] - my); sxx += (x[i] - mx) ** 2; }
  const slope = sxy / sxx;
  return [slope, my - slope * mx];
}

/** How much velocity rises and falls inside samples s..e, after removing a straight line. */
function velocityHump(a, t, s, e) {
  const tw = t.slice(s, e + 1).map((x) => x - t[s]);
  if (tw.length < 3) return 0;
  const v = integrate(a.slice(s, e + 1), tw);
  const [m, b] = lineFit(tw, v);
  let lo = Infinity, hi = -Infinity;
  for (let i = 0; i < v.length; i++) {
    const r = v[i] - (m * tw[i] + b);
    lo = Math.min(lo, r); hi = Math.max(hi, r);
  }
  return hi - lo;
}

/** Throw out "still" periods that are really coasting. See confirm_still_runs() in reps.py. */
export function confirmStillRuns(a, t, fs, runs, S = SETTINGS) {
  const n = Math.trunc(S.COAST_LOOK * fs);
  const confirmed = [runs[0]];
  for (const [s, e] of runs.slice(1)) {
    if (velocityHump(a, t, s, e) > S.STILL_MAX_HUMP) continue;   // speeds up and slows down: moving
    if (t[e] - t[s] >= S.LONG_STILL) {   // long stills are real
      confirmed.push([s, e]);
      continue;
    }
    const i0 = confirmed[confirmed.length - 1][1];
    let still;
    if (t[s] - t[i0] < S.COAST_TRUST) {   // recent anchor: check the speed directly
      const seg = a.slice(i0, e + 1);
      const tw = t.slice(i0, e + 1).map((x) => x - t[i0]);
      const vRaw = integrate(seg, tw);
      let from = s - i0;
      if (from < 0) from = Math.max(vRaw.length + from, 0);   // same as Python slicing
      let sum = 0;
      for (let k = from; k < vRaw.length; k++) sum += Math.abs(vRaw[k]);
      still = sum / (vRaw.length - from) < S.STILL_MAX_SPEED;
    } else {                              // old anchor: speeding up then slowing down = coast
      const before = speedChange(a, t, s - n, s);
      const after = speedChange(a, t, e, e + n);
      still = !(before * after < 0 && Math.min(Math.abs(before), Math.abs(after)) > S.COAST_DV);
    }
    if (still) confirmed.push([s, e]);
  }
  return confirmed;
}


// ---- attitude (D33's Madgwick, here since D40; the same as reps.py) ----

/** Per-sample bool: inside one of the [start, end] runs. */
function stillMask(n, runs) {
  const m = new Uint8Array(n);
  for (const [s, e] of runs) for (let i = s; i <= e; i++) m[i] = 1;
  return m;
}

const norm3 = (v) => Math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2]);
const cross3 = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];

/** The unit quaternion [w, x, y, z] that turns vector a onto vector b. */
function quatFromTo(a, b) {
  const na = norm3(a), nb = norm3(b);
  a = a.map((x) => x / na);
  b = b.map((x) => x / nb);
  const c = a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
  if (c < -0.999999) {                                  // opposite: any axis at right angles
    const ax = Math.abs(a[0]) < 0.9 ? cross3(a, [1, 0, 0]) : cross3(a, [0, 1, 0]);
    const n = norm3(ax);
    return [0, ax[0] / n, ax[1] / n, ax[2] / n];
  }
  const q = [1 + c, ...cross3(a, b)];
  const n = Math.sqrt(q[0] * q[0] + q[1] * q[1] + q[2] * q[2] + q[3] * q[3]);
  return q.map((x) => x / n);
}

/** madgwick() in reps.py: Madgwick's IMU update per sample, the trapezoid's mean gyro per step.
 * t, acc (rows [x, y, z]), gyr (rad/s rows), still and trusted (per-sample) -> quaternions. */
function madgwick(t, acc, gyr, still, q0, trusted, S) {
  const n = t.length;
  const out = new Array(n);
  let [q0_, q1, q2, q3] = q0;
  out[0] = [q0_, q1, q2, q3];
  const cosGate = Math.cos((S.GATE_DEG * Math.PI) / 180);
  for (let i = 1; i < n; i++) {
    const dt = t[i] - t[i - 1];
    const gx = 0.5 * (gyr[i][0] + gyr[i - 1][0]), gy = 0.5 * (gyr[i][1] + gyr[i - 1][1]), gz = 0.5 * (gyr[i][2] + gyr[i - 1][2]);
    let [ax, ay, az] = acc[i];
    let beta = S.BETA_MOVE;
    if (still[i]) {
      const ux = 2 * (q1 * q3 - q0_ * q2), uy = 2 * (q0_ * q1 + q2 * q3), uz = 1 - 2 * (q1 * q1 + q2 * q2);
      const na = Math.sqrt(ax * ax + ay * ay + az * az);
      if ((trusted && trusted[i]) || (na > 0 && (ux * ax + uy * ay + uz * az) / na > cosGate)) beta = S.BETA_STILL;
    }
    let qd0 = 0.5 * (-q1 * gx - q2 * gy - q3 * gz);
    let qd1 = 0.5 * (q0_ * gx + q2 * gz - q3 * gy);
    let qd2 = 0.5 * (q0_ * gy - q1 * gz + q3 * gx);
    let qd3 = 0.5 * (q0_ * gz + q1 * gy - q2 * gx);
    const norm = Math.sqrt(ax * ax + ay * ay + az * az);
    if (norm > 0) {
      ax /= norm; ay /= norm; az /= norm;
      const _2q0 = 2 * q0_, _2q1 = 2 * q1, _2q2 = 2 * q2, _2q3 = 2 * q3;
      const _4q0 = 4 * q0_, _4q1 = 4 * q1, _4q2 = 4 * q2;
      const _8q1 = 8 * q1, _8q2 = 8 * q2;
      const q0q0 = q0_ * q0_, q1q1 = q1 * q1, q2q2 = q2 * q2, q3q3 = q3 * q3;
      const s0 = _4q0 * q2q2 + _2q2 * ax + _4q0 * q1q1 - _2q1 * ay;
      const s1 = _4q1 * q3q3 - _2q3 * ax + 4 * q0q0 * q1 - _2q0 * ay - _4q1 + _8q1 * q1q1 + _8q1 * q2q2 + _4q1 * az;
      const s2 = 4 * q0q0 * q2 + _2q0 * ax + _4q2 * q3q3 - _2q3 * ay - _4q2 + _8q2 * q1q1 + _8q2 * q2q2 + _4q2 * az;
      const s3 = 4 * q1q1 * q3 - _2q1 * ax + 4 * q2q2 * q3 - _2q2 * ay;
      const sn = Math.max(Math.sqrt(s0 * s0 + s1 * s1 + s2 * s2 + s3 * s3), S.SN_FLOOR);
      if (sn > 0) {
        qd0 -= (beta * s0) / sn;
        qd1 -= (beta * s1) / sn;
        qd2 -= (beta * s2) / sn;
        qd3 -= (beta * s3) / sn;
      }
    }
    q0_ += qd0 * dt; q1 += qd1 * dt; q2 += qd2 * dt; q3 += qd3 * dt;
    const qn = Math.sqrt(q0_ * q0_ + q1 * q1 + q2 * q2 + q3 * q3);
    q0_ /= qn; q1 /= qn; q2 /= qn; q3 /= qn;
    out[i] = [q0_, q1, q2, q3];
  }
  return out;
}

/** quasi_stills() in reps.py: [mask, averaged accel rows]. gyr: rows, bias off (deg/s). */
function quasiStills(acc, gyr, g, fs, S) {
  const n = Math.max(Math.trunc(S.QS_AVG * fs), 1);
  const N = acc.length;
  const am3 = [0, 1, 2].map((k) => rolling(acc.map((r) => r[k]), n).mean);
  const wm = [0, 1, 2].filter((k) => k !== S.BAR_AXIS).map((k) => rolling(gyr.map((r) => r[k]), n).mean);
  const ok = new Uint8Array(N);
  for (let i = 0; i < N; i++) {
    const an = Math.sqrt(am3[0][i] * am3[0][i] + am3[1][i] * am3[1][i] + am3[2][i] * am3[2][i]);
    const wn = Math.sqrt(wm[0][i] * wm[0][i] + wm[1][i] * wm[1][i]);
    ok[i] = Math.abs(an - g) < S.QS_DEV && wn < S.QS_GYRO ? 1 : 0;
  }
  const mask = new Uint8Array(N);
  for (const [s, e] of runsOf(ok)) if ((e + 1 - s) / fs >= S.QS_MIN) for (let i = s; i <= e; i++) mask[i] = 1;
  const am = new Array(N);
  for (let i = 0; i < N; i++) am[i] = [am3[0][i], am3[1][i], am3[2][i]];
  return [mask, am];
}

/** vertical_accel() in reps.py: acceleration along up (gravity off) from the gyro-tracked attitude,
 * pulled onto gravity at stills and quasi-stills, forward from the first still and backward before it. */
function verticalAccel(t, acc, gyro, runs, fs, S) {
  const N = t.length;
  const still = stillMask(N, runs);
  let gs = 0, cnt = 0;
  for (let i = 0; i < N; i++) if (still[i]) { gs += norm3(acc[i]); cnt++; }
  const g = gs / cnt;
  const bias = [0, 1, 2].map((k) => median(gyro.filter((_, i) => still[i]).map((r) => r[k])));
  const wDeg = gyro.map((r) => [r[0] - bias[0], r[1] - bias[1], r[2] - bias[2]]);
  const [qs, am] = quasiStills(acc, wDeg, g, fs, S);
  const pull = new Uint8Array(N);
  for (let i = 0; i < N; i++) pull[i] = still[i] || qs[i] ? 1 : 0;
  const [s0, e0] = runs[0];
  const m0 = [0, 0, 0];
  for (let i = s0; i <= e0; i++) for (let k = 0; k < 3; k++) m0[k] += acc[i][k];
  const q0 = quatFromTo(m0.map((x) => x / (e0 + 1 - s0)), [0, 0, 1]);
  const w = wDeg.map((r) => r.map((x) => (x * Math.PI) / 180));
  const q = new Array(N);
  const fwd = madgwick(t.slice(s0), am.slice(s0), w.slice(s0), pull.slice(s0), q0, pull.slice(s0), S);
  for (let i = s0; i < N; i++) q[i] = fwd[i - s0];
  if (s0) {
    const idx = [];
    for (let i = s0; i >= 0; i--) idx.push(i);
    const back = madgwick(idx.map((i) => -t[i]), idx.map((i) => am[i]), idx.map((i) => w[i].map((x) => -x)),
      idx.map((i) => pull[i]), q0, idx.map((i) => pull[i]), S);
    for (let k = 0; k < idx.length; k++) q[idx[k]] = back[k];
  }
  const out = new Float64Array(N);
  for (let i = 0; i < N; i++) {
    const [qw, qx, qy, qz] = q[i];
    // world up in the sensor frame: the rotation matrix's third row
    const ux = 2 * (qx * qz - qw * qy), uy = 2 * (qy * qz + qw * qx), uz = 1 - 2 * (qx * qx + qy * qy);
    out[i] = acc[i][0] * ux + acc[i][1] * uy + acc[i][2] * uz - g;
  }
  return out;
}

/** Velocity with straight-line drift removed between each pair of still periods. */
export function velocityBetweenAnchors(a, t, fs, runs) {
  const v = new Float64Array(a.length);
  const windows = [];
  for (let k = 0; k + 1 < runs.length; k++) {
    const i0 = runs[k][1];
    const i1 = runs[k + 1][0];
    if (i1 - i0 < 2) continue;
    const tw = t.slice(i0, i1 + 1).map((x) => x - t[i0]);
    const vRaw = integrate(a.slice(i0, i1 + 1), tw);
    const T = tw[tw.length - 1];
    const end = vRaw[vRaw.length - 1];
    for (let i = 0; i < vRaw.length; i++) v[i0 + i] = vRaw[i] - (end * tw[i]) / T;
    windows.push({ i0, i1, dur: T, rawEnd: end, anchored: true });
  }
  const last = runs[runs.length - 1][1];
  const tail = a.slice(last);
  let maxAbs = 0;
  for (const x of tail) maxAbs = Math.max(maxAbs, Math.abs(x));
  if (tail.length > 0.2 * fs && maxAbs > 0.5) {
    const tw = t.slice(last).map((x) => x - t[last]);
    const vRaw = integrate(tail, tw);
    for (let i = 0; i < vRaw.length; i++) v[last + i] = vRaw[i];
    windows.push({ i0: last, i1: a.length - 1, dur: tw[tw.length - 1], rawEnd: NaN, anchored: false });
  }
  return { v, windows };
}

/** Lows and highs where the height reverses by at least minMove ("zigzag"). */
export function turningPoints(x, minMove) {
  const lows = [];
  const highs = [];
  let trend = 0;
  let lo = 0;
  let hi = 0;
  for (let i = 1; i < x.length; i++) {
    if (x[i] < x[lo]) lo = i;
    if (x[i] > x[hi]) hi = i;
    if (trend <= 0 && x[i] - x[lo] >= minMove) {
      lows.push(lo); trend = 1; hi = i;
    } else if (trend >= 0 && x[hi] - x[i] >= minMove) {
      highs.push(hi); trend = -1; lo = i;
    }
  }
  if (trend === 1) highs.push(hi);
  else if (trend === -1) lows.push(lo);
  return { lows, highs };
}

/** One rep per bottom-to-top move. Boundaries come from v = 0, never a threshold. */
/** Least squares for a few unknowns: solve (A^T A) p = A^T b by Gaussian elimination. */
function leastSquares(A, b) {
  const n = A[0].length;
  const M = Array.from({ length: n }, () => new Float64Array(n + 1));
  for (let r = 0; r < A.length; r++) {
    for (let i = 0; i < n; i++) {
      for (let j = 0; j < n; j++) M[i][j] += A[r][i] * A[r][j];
      M[i][n] += A[r][i] * b[r];
    }
  }
  for (let c = 0; c < n; c++) {
    let p = c;
    for (let r = c + 1; r < n; r++) if (Math.abs(M[r][c]) > Math.abs(M[p][c])) p = r;
    [M[c], M[p]] = [M[p], M[c]];
    for (let r = c + 1; r < n; r++) {
      const f = M[r][c] / M[c][c];
      for (let j = c; j <= n; j++) M[r][j] -= f * M[c][j];
    }
  }
  const sol = new Float64Array(n);
  for (let i = n - 1; i >= 0; i--) {
    let acc = M[i][n];
    for (let j = i + 1; j < n; j++) acc -= M[i][j] * sol[j];
    sol[i] = acc / M[i][i];
  }
  return sol;
}

/**
 * No-pause sets: remove curved drift using heights that repeat every rep (lockouts;
 * bottoms for bench and deadlift; the stills before and after the set for barbell lifts).
 * Velocity drift e(tau) = d1 tau + d2 tau^2 + ... (tau = time / window length),
 * e(1) = raw end velocity, the other d's and the repeated heights by least squares.
 * Still moving when the data ends: d1 is fitted too (provisional).
 * See level_heights() in reps.py for the full explanation.
 */
export function levelHeights(a, t, v, windows, S = SETTINGS) {
  const prof = S.LIFTS[S.LIFT];
  const hooks = S.TA_RACKED.includes(S.LIFT);   // squat, bench: on a pod the stills before and after are the hooks
  const x = integrate(v, t);
  const { lows, highs } = turningPoints(x, S.MIN_ROM);
  const minFrom = (i, j) => { let m = Infinity; for (let k = i; k <= j; k++) m = Math.min(m, x[k]); return m; };
  const maxFrom = (i, j) => { let m = -Infinity; for (let k = i; k <= j; k++) m = Math.max(m, x[k]); return m; };
  for (const w of windows) {
    if (w.turns) continue;             // redone rep by rep (turnaroundVelocity)
    const { i0, i1 } = w;
    let tops = highs.filter((h) => h > i0 && h < i1);
    let bottoms = lows.filter((b) => b > i0 && b < i1);
    if (!w.anchored) {
      // only turnarounds the bar has already moved away from
      tops = tops.filter((h) => x[h] - minFrom(h, i1) >= S.MIN_ROM);
      bottoms = bottoms.filter((b) => maxFrom(b, i1) - x[b] >= S.MIN_ROM);
    }
    if (hooks && prof.bottoms) {
      // bench: a low less than TA_HOOK_MOVE under the lockout before it is the hooks, not the chest (D43)
      bottoms = bottoms.filter((b) => {
        let m = -1;
        for (const h of highs) if (h < b && h > m) m = h;
        return x[m < 0 ? i0 : m] - x[b] >= S.TA_HOOK_MOVE;
      });
    }
    const turns = [...tops, ...bottoms].sort((p, q) => p - q);
    if (prof.ends && turns.length) {
      const first = turns[0];
      const last = turns[turns.length - 1];
      const rep = hooks ? S.TA_HOOK_MOVE : S.MIN_ROM;   // up off the hooks by less: the unrack (D43)
      if (bottoms.includes(first) && x[i0] - x[first] >= S.MIN_ROM) tops = [i0, ...tops];
      else if (tops.includes(first) && x[first] - x[i0] >= rep) bottoms = [i0, ...bottoms];
      if (w.anchored) {
        if (bottoms.includes(last) && x[i1] - x[last] >= S.MIN_ROM) tops = [...tops, i1];
        else if (tops.includes(last) && x[last] - x[i1] >= rep) bottoms = [...bottoms, i1];
      }
    }
    const groups = tops.length >= 2 ? [tops] : [];
    if (prof.bottoms && bottoms.length >= 2) groups.push(bottoms);
    const nRows = groups.reduce((n, g) => n + g.length, 0);
    const nH = groups.length;
    let minRows = nH + (w.anchored ? 2 : 1);
    if (w.anchored && prof.ends) minRows = nH + 1;   // same height at both stills: one exact bend
    if (nRows < minRows) continue;
    const maxDeg = nH === 2 ? S.LEVEL_MAX_DEG_BOTH : S.LEVEL_MAX_DEG;
    const tw = t.slice(i0, i1 + 1).map((x) => x - t[i0]);
    const T = tw[tw.length - 1];
    const vRaw = integrate(a.slice(i0, i1 + 1), tw);
    const xRaw = integrate(vRaw, tw);
    const rows = [];
    const rhs = [];
    let d;
    let sol;
    if (w.anchored) {
      const deg = Math.min(maxDeg, Math.max(2, nRows - nH));
      const eEnd = vRaw[vRaw.length - 1];
      groups.forEach((g, gi) => {
        for (const h of g) {
          const tau = tw[h - i0] / T;
          const row = [];
          for (let k = 2; k <= deg; k++) row.push(T * (tau ** (k + 1) / (k + 1) - tau ** 2 / 2));
          for (let j = 0; j < nH; j++) row.push(j === gi ? 1 : 0);
          rows.push(row);
          rhs.push(xRaw[h - i0] - (T * eEnd * tau ** 2) / 2);
        }
      });
      sol = leastSquares(rows, rhs);
      d = [eEnd];                                       // d1 = eEnd - d2 - ... - d_deg
      for (let k = 0; k < deg - 1; k++) { d[0] -= sol[k]; d.push(sol[k]); }
    } else {
      const deg = Math.max(1, Math.min(maxDeg, nRows - nH - 1));   // unknowns d1..d_deg and the heights
      groups.forEach((g, gi) => {
        for (const h of g) {
          const tau = tw[h - i0] / T;
          const row = [];
          for (let k = 1; k <= deg; k++) row.push((T * tau ** (k + 1)) / (k + 1));
          for (let j = 0; j < nH; j++) row.push(j === gi ? 1 : 0);
          rows.push(row);
          rhs.push(xRaw[h - i0]);
        }
      });
      sol = leastSquares(rows, rhs);
      d = Array.from(sol.slice(0, deg));
    }
    for (let i = 0; i < tw.length; i++) {
      const tau = tw[i] / T;
      let e = 0;
      for (let k = 0; k < d.length; k++) e += d[k] * tau ** (k + 1);
      v[i0 + i] = vRaw[i] - e;
    }
    w.leveled = true;
    if (groups[0] === tops) w.topHeight = sol[sol.length - nH];   // lockout height, relative to i0
  }
  return v;
}


// ---- turnaround anchors (D40): a window with no still between its reps, redone rep by rep ----

/** Gaussian moving average (sigma in s), ends padded with the end values (np.convolve's order). */
function gaussSmooth(y, fs, sigma) {
  const n = Math.trunc(4 * sigma * fs);
  const k = new Float64Array(2 * n + 1);
  let ks = 0;
  for (let j = 0; j <= 2 * n; j++) { k[j] = Math.exp(-0.5 * ((j - n) / (sigma * fs)) ** 2); ks += k[j]; }
  for (let j = 0; j <= 2 * n; j++) k[j] /= ks;
  const L = y.length;
  const yp = new Float64Array(L + 2 * n);                   // padded with the end values, once
  for (let i = 0; i < yp.length; i++) yp[i] = y[Math.min(Math.max(i - n, 0), L - 1)];
  const kr = k.slice().reverse();                           // kr[j] = k[2n - j]: the same sums, same order
  const out = new Float64Array(L);
  for (let i = 0; i < L; i++) {
    let acc = 0;
    for (let j = 0; j <= 2 * n; j++) acc += yp[i + j] * kr[j];
    out[i] = acc;
  }
  return out;
}

/** mask widened by n samples each way. */
function dilate(mask, n) {
  const out = new Uint8Array(mask.length);
  for (let i = 0; i < mask.length; i++) {
    if (!mask[i]) continue;
    for (let j = Math.max(i - n, 0); j <= Math.min(i + n, mask.length - 1); j++) out[j] = 1;
  }
  return out;
}

/** impact_mask() in reps.py: a jolt with the gyro (offset off) over IMPACT_W within IMPACT_NEAR. */
function impactMask(a, gyro, runs, fs, S) {
  if (!gyro) return Uint8Array.from(a, (x) => (Math.abs(x) > 25.0 ? 1 : 0));   // no gyro: a harder jolt alone
  const still = stillMask(a.length, runs);
  const m = [0, 0, 0];
  let cnt = 0;
  for (let i = 0; i < a.length; i++) if (still[i]) { for (let k = 0; k < 3; k++) m[k] += gyro[i][k]; cnt++; }
  for (let k = 0; k < 3; k++) m[k] /= cnt;
  const fast = Uint8Array.from(gyro, (r) => (Math.sqrt((r[0] - m[0]) ** 2 + (r[1] - m[1]) ** 2 + (r[2] - m[2]) ** 2) > S.IMPACT_W ? 1 : 0));
  const near = dilate(fast, Math.trunc(S.IMPACT_NEAR * fs));
  return Uint8Array.from(a, (x, i) => (Math.abs(x) > S.IMPACT_A && near[i] ? 1 : 0));
}

/** The still-to-still fix can't be trusted: the velocity left at the window's end is far too big. */
function brokenWindow(v, w, S) {
  if (w.anchored) return Math.abs(w.rawEnd) > S.TA_BROKEN_V;
  return Math.abs(v[w.i1]) > S.TA_BROKEN_TAIL;
}

/** [start, end] of every run of truthy values in mask. */
function runsOf(mask) {
  const out = [];
  let s = -1;
  for (let i = 0; i <= mask.length; i++) {
    const on = i < mask.length && mask[i];
    if (on && s < 0) s = i;
    else if (!on && s >= 0) { out.push([s, i - 1]); s = -1; }
  }
  return out;
}

/** find_pause() in reps.py: the pause at coarse turnaround k as [start, end], or null. */
function findPause(a, t, xc, k, lo, hi, fs, flat, floor, first, last, S) {
  const look = Math.trunc(S.COAST_LOOK * fs), reach = Math.trunc(S.TA_REACH * fs);
  const back = floor ? Math.trunc(S.TA_FLOOR_REACH * fs) : reach;   // how far before k the run may end
  const move = Math.max(Math.abs(xc[k] - xc[lo]), Math.abs(xc[k] - xc[hi]));
  let best = null;
  for (let [rs, re] of runsOf(flat.subarray(lo, hi + 1))) {
    rs += lo; re += lo;
    if (re - rs < S.TA_PAUSE * fs || !(rs - reach <= k && k <= re + back)) continue;
    const into = speedChange(a, t, rs - look, rs), out = speedChange(a, t, re, re + look);
    if (into * out < 0 && Math.min(Math.abs(into), Math.abs(out)) > S.TA_COAST_DV) continue;
    // a lockout's coarse height doesn't move through it (TA_HOLD_V); before the first rep or after the
    // last, a run that passes that needs no height check (see reps.py, D43)
    const mid = Math.floor((rs + re) / 2);
    const slow = Math.abs(xc[re] - xc[rs]) <= S.TA_HOLD_V * (t[re] - t[rs]);
    const outer = !floor && slow && ((first && mid <= k) || (last && mid >= k));
    if (!outer && Math.abs(xc[mid] - xc[k]) > S.TA_NEAR_FRAC * move) continue;
    if (!(floor || first || last || slow)) continue;
    const score = floor ? -(re - rs) : rs <= k && k <= re ? 0 : Math.min(Math.abs(rs - k), Math.abs(re - k));
    if (best === null || score < best[0]) best = [score, [rs, re]];
  }
  return best === null ? null : best[1];
}

/** confirmed_at() in reps.py: the first sample after the turnaround k that's minMove from it. */
function confirmedAt(x, k, minMove) {
  for (let i = k; i < x.length; i++) if (Math.abs(x[i] - x[k]) >= minMove) return i;
  return x.length;
}

/** span_velocity() in reps.py: raw velocity less a straight line through the two anchors' zeros;
 * on a floor lift (hit given) the span closes on height at the landing instead. */
function spanVelocity(vr, tw, s, e, vs, ve, hit = null) {
  const tt = Float64Array.from(tw.subarray(s, e + 1), (x) => x - tw[s]);
  const v = Float64Array.from(vr.subarray(s, e + 1), (x) => x - vs);
  const T = tt[tt.length - 1];
  if (hit) {
    let land = -1;
    for (let i = 0; i < tt.length; i++) if (hit[s + i] && tt[i] > T / 2) land = i;
    if (land >= 0) {
      while (land > 0 && hit[s + land - 1]) land--;          // back to the onset of that last jolt
      if (tt[land] > 0) {
        const x = integrate(v.subarray(0, land + 1), tt.subarray(0, land + 1));
        const c = (2 * x[land]) / tt[land] ** 2;
        for (let i = 0; i <= land; i++) v[i] -= c * tt[i];   // height back to 0 at the landing
      } else {
        v.fill(0);                                           // the span starts in that jolt: on the floor
      }
      for (let i = land + 1; i < v.length; i++) v[i] = 0;
      return v;
    }
  }
  for (let i = 0; i < v.length; i++) v[i] -= ((ve - vs) * tt[i]) / T;
  return v;
}

/** turnaround_velocity() in reps.py: velocity over a redone window, rep by rep (coarse pass for the
 * rep structure, the lift's resting turnarounds as anchors, then section 6's line span by span). */
function turnaroundVelocity(a, t, fs, w, lift, S) {
  const { i0, i1 } = w;
  const tw = Float64Array.from(t.subarray(i0, i1 + 1), (x) => x - t[i0]);
  const aw = a.subarray(i0, i1 + 1);
  const L = aw.length;
  const floor = (S.TA_REST[lift] ?? "top") === "bottom";
  // what the bar hit: on the floor every jolt (D41), else the jolts the gyro confirms (not a chest bounce, D43)
  const imp = floor ? Uint8Array.from(aw, (x) => (Math.abs(x) > S.IMPACT_A ? 1 : 0)) : w.impacts;
  // 1. coarse pass
  const blank = dilate(imp, Math.trunc(S.IMPACT_PAD * fs));
  const am = Float64Array.from(aw, (x, i) => (blank[i] ? 0 : x));
  const n = Math.max(1, Math.trunc(S.TA_FLAT_WIN * fs));
  const fm = rolling(am, n).mean;
  const flat = Uint8Array.from(fm, (x) => (Math.abs(x) < S.TA_FLAT_A ? 1 : 0));
  const stops = runsOf(blank).map(([, je]) => je)
    .filter((je) => runsOf(flat.subarray(je)).some(([rs, re]) => rs <= n && re - rs >= S.TA_PAUSE * fs));
  let vc = new Float64Array(L);
  const starts = [0, ...stops], ends = [...stops, L - 1];
  for (let j = 0; j < starts.length; j++) {
    const s = starts[j], e = ends[j];
    const seg = integrate(am.subarray(s, e + 1), Float64Array.from(tw.subarray(s, e + 1), (x) => x - tw[s]));
    for (let i = 0; i < seg.length; i++) vc[s + i] = seg[i];
  }
  const sigma = S.TA_SIGMA_LIFT[lift] ?? S.TA_SIGMA;
  const sm = gaussSmooth(vc, fs, sigma);
  vc = Float64Array.from(vc, (x, i) => x - sm[i]);
  const xc = integrate(vc, tw);
  let { lows, highs } = turningPoints(xc, S.TA_MIN_MOVE);
  // a squat or bench set ends at the first impact after its first lockout (the bar hit the rack): its
  // turnarounds are the coarse height's up to there (rack: the last sample before it; see reps.py, D43)
  let rack = L - 1, racked = false;
  if (S.TA_RACKED.includes(lift)) {
    let first = -1;
    for (let i = highs.length ? highs[0] + 1 : 0; i < L; i++) if (imp[i]) { first = i; break; }
    if (first >= 0) {
      rack = first - 1;
      ({ lows, highs } = turningPoints(xc.subarray(0, rack + 1), S.TA_MIN_MOVE));
      racked = true;                                     // no rep in progress after it, live or not
    }
  }
  if (floor && !w.anchored) {
    // no end still yet (live): a turnaround the high-pass confirms only in the last TA_SIGMA waits, but
    // the last low once the bar has landed (a stop between the high before it and TA_REACH after it; see reps.py)
    const cut = xc.length - 1 - Math.trunc(sigma * fs);
    const reach = Math.trunc(S.TA_REACH * fs);
    const lastLow = lows[lows.length - 1];
    const before = (k) => highs.reduce((m, h) => (h < k ? Math.max(m, h) : m), -1);
    const landed = (k) => k === lastLow && stops.some((je) => before(k) < je && je <= k + reach);
    lows = lows.filter((k) => confirmedAt(xc, k, S.TA_MIN_MOVE) <= cut || landed(k));
    highs = highs.filter((k) => confirmedAt(xc, k, S.TA_MIN_MOVE) <= cut);
  }
  const turns = [...lows, ...highs].sort((p, q) => p - q);
  // 2. anchors
  const rests = [];
  const resting = floor ? lows : highs;
  for (const k of resting) {
    const j = turns.indexOf(k);
    const lo = j > 0 ? turns[j - 1] : 0;
    const hi = j + 1 < turns.length ? turns[j + 1] : rack;
    rests.push([k, findPause(am, tw, xc, k, lo, hi, fs, flat, floor, k === resting[0], k === resting[resting.length - 1], S)]);
  }
  const closed = w.anchored || racked;
  const edge = Math.trunc(S.TA_EDGE * fs);
  let anchors = [];
  rests.forEach(([k, p], j) => {
    if (p === null) { anchors.push([k, k, k]); return; }  // touch-and-go
    let [ps, pe] = p;
    if (j === 0 && rests.length > 1) {
      ps = Math.max(ps, pe - 2 * edge);    // before rep 1: its end, unless the descent began in it: the 2 x
      let top = p[0];                      // TA_EDGE up to its coarse top, the anchor where it was (see reps.py, D43)
      for (let i = p[0] + 1; i <= pe; i++) if (xc[i] > xc[top]) top = i;
      if (!floor && xc[pe] < xc[top] - S.TA_START_DROP) { anchors.push([Math.floor((ps + pe) / 2), Math.max(p[0], top - 2 * edge), top]); return; }
    }
    else if (j === rests.length - 1 && j > 0 && floor && !closed) { /* open, on the floor: all of it (see reps.py) */ }
    else if (j > 0 && (j === rests.length - 1 || (!floor && xc[pe] < xc[ps] - S.TA_START_DROP))) {
      pe = Math.min(pe, ps + 2 * edge);    // after the last: its start; between reps too if it ends lower (D43)
    }
    anchors.push([Math.floor((ps + pe) / 2), ps, pe]);
  });
  // the window's own stills anchor the set's ends when they're part of it (see reps.py)
  const end = L - 1;
  if (anchors.length && anchors[0][0] <= S.TA_SET_EDGE * fs) {
    anchors = [[0, 0, 0], ...anchors.filter((an) => an[0] > S.TA_REACH * fs)];
  }
  if (anchors.length && w.anchored && !racked && end - anchors[anchors.length - 1][0] <= S.TA_SET_EDGE * fs) {
    anchors = [...anchors.filter((an) => an[0] < end - S.TA_REACH * fs), [end, end, end]];
  }
  w.anchors = anchors.map(([k]) => i0 + k);
  w.rests = anchors.length ? [i0 + anchors[0][0], i0 + anchors[anchors.length - 1][0], closed] : null;
  const v = new Float64Array(L);
  if (!anchors.length) return v;
  // 3. spans
  const hit = dilate(imp, Math.trunc(S.JOLT_PAD * fs));
  const vr = integrate(Float64Array.from(aw, (x, i) => (hit[i] ? 0 : x)), tw);
  const zero = anchors.map(([, s0, s1]) => { let m = 0; for (let i = s0; i <= s1; i++) m += vr[i]; return m / (s1 + 1 - s0); });
  for (let j = 0; j + 1 < anchors.length; j++) {
    const s = anchors[j][0], e = anchors[j + 1][0];
    if (e > s) v.set(spanVelocity(vr, tw, s, e, zero[j], zero[j + 1], floor ? hit : null), s);
  }
  if (!closed) {
    const s = anchors[anchors.length - 1][0];
    for (let i = s; i < L; i++) v[i] = vr[i] - zero[zero.length - 1];
  }
  return v;
}

/** Median height of the turnaround bottoms, for lifts whose bottom repeats; else null. */
/** Where a hard concentric sticks (analytics.md section 4): the deepest dip with a rise before and
 * after it, STICK_PROM x the rep's peak below the lower of those peaks, inside STICK_RANGE of the
 * ROM; null for a rep that doesn't dip. Same running-sum mean as reps.py, to the last bit. */
export function stickingPoint(x, v, t, rep, S = SETTINGS) {
  const s = rep.start, e = rep.end, n = e - s + 1;
  if (n < 5 || rep.rom <= 0) return null;
  const fs = (n - 1) / (t[e] - t[s]);
  const h = Math.floor(Math.max(1, Math.floor(S.STICK_SMOOTH * fs)) / 2);
  const c = new Float64Array(n + 1);
  for (let i = 0; i < n; i++) c[i + 1] = c[i] + v[s + i];
  const vv = new Float64Array(n);
  for (let i = 0; i < n; i++) {
    const lo = Math.max(i - h, 0), hi = Math.min(i + h, n - 1);
    vv[i] = (c[hi + 1] - c[lo]) / (hi - lo + 1);
  }
  let peak = -Infinity;
  for (const y of vv) peak = Math.max(peak, y);
  const left = new Float64Array(n), right = new Float64Array(n);
  for (let i = 0; i < n; i++) left[i] = i ? Math.max(left[i - 1], vv[i]) : vv[i];
  for (let i = n - 1; i >= 0; i--) right[i] = i < n - 1 ? Math.max(right[i + 1], vv[i]) : vv[i];
  let best = null;
  for (let i = 1; i < n - 1; i++) {
    if (!(vv[i] < vv[i - 1] && vv[i] <= vv[i + 1])) continue;
    const pos = (x[s + i] - x[s]) / rep.rom;
    if (!(S.STICK_RANGE[0] <= pos && pos <= S.STICK_RANGE[1])) continue;
    const depth = Math.min(left[i - 1], right[i + 1]) - vv[i];
    if (depth >= S.STICK_PROM * peak && (best === null || depth > best.depth)) best = { i, depth, pos };
  }
  if (best === null) return null;
  return { pos: best.pos, v: vv[best.i], depth: best.depth, t: t[s + best.i] - t[s] };
}

export function usualBottom(x, S = SETTINGS) {
  if (!S.LIFTS[S.LIFT].bottoms) return null;
  const { lows } = turningPoints(x, S.MIN_ROM);
  return lows.length >= 3 ? median(lows.map((i) => x[i])) : null;
}

/**
 * One rep per bottom-to-top move. bottomRef = the usual bottom height (bench, deadlift,
 * machine): a rep may start from a stop near it even if the move began lower, which
 * cuts a setup move off the first rep. See find_reps() in reps.py.
 */
export function findReps(x, v, t, windows, S = SETTINGS, bottomRef = null, stopFrac = 0) {
  const { lows, highs } = turningPoints(x, S.MIN_ROM);
  const reps = [];
  for (const lo of lows) {
    let hi = highs.find((h) => h > lo);
    if (hi === undefined) continue;
    for (const w of windows) {
      // a closed set's rep tops out by its last anchor: the next move up (the pod pulled off the sleeve)
      // would carry the zigzag's top on into itself (see reps.py, D43)
      const r = w.rests;
      if (w.turns && r && r[2] && lo < r[1] && r[1] < hi) {
        let m = lo;
        for (let i = lo + 1; i <= r[1]; i++) if (x[i] > x[m]) m = i;
        if (x[m] - x[lo] >= S.MIN_ROM) hi = m;
      }
    }
    let segMax = -Infinity;
    for (let i = lo; i <= hi; i++) segMax = Math.max(segMax, v[i]);
    const vz = Math.max(S.V_ZERO, stopFrac * segMax);
    const base = bottomRef === null ? x[lo] : Math.max(x[lo], bottomRef);
    let start = lo, found = false;
    for (let i = lo; i <= hi; i++) if (v[i] <= vz && x[i] <= base + S.MIN_ROM) { start = i; found = true; }
    if (found) {                       // back to v = 0, or to where a stop's drift floor ends
      while (start > lo && v[start] > S.V_ZERO && v[start - 1] < v[start]) start--;
    }
    let end = hi;
    found = false;
    for (let i = start + 1; i <= hi; i++) {
      if (v[i] <= vz && x[i] >= x[hi] - S.MIN_ROM) { end = i; found = true; break; }
    }
    if (found) {                       // on to v = 0, or to where a pause's drift floor begins
      while (end < hi && v[end] > S.V_ZERO && v[end + 1] < v[end]) end++;
    }
    if (end <= start) continue;        // a flat stretch (turnaround anchors zero the walkout and rerack)
    const rom = x[end] - x[start];
    const dur = t[end] - t[start];
    let peak = -Infinity;
    for (let i = start; i <= end; i++) peak = Math.max(peak, v[i]);
    const flags = new Set();
    let skip = false;
    for (const w of windows) {
      if (w.i0 <= end && w.i1 >= start) {
        if (w.turns) {
          // its reps lie between the first and the last resting turnaround (see reps.py)
          const [first, last, closed] = w.rests ?? [w.i1, w.i0, true];
          if (start < first || (end > last && closed)) skip = true;
          flags.add("turnaround anchors");
          if (end > last && !closed) flags.add("no end anchor");
          continue;
        }
        if (!w.anchored) flags.add("no end anchor");
        if (w.leveled) {
          if (w.topHeight !== undefined && Math.abs(x[hi] - (x[w.i0] + w.topHeight)) > S.LEVEL_FLAG) flags.add("uneven lockout");
        } else if (w.dur > S.LONG_WINDOW) {
          flags.add(`long window ${w.dur.toFixed(1)}s`);
        }
        if (w.anchored && Math.abs(w.rawEnd) > S.BIG_CORRECTION) flags.add("big drift correction");
      }
    }
    if (skip) continue;
    const leveled = windows.some((w) => w.leveled && w.i0 <= end && w.i1 >= start);
    reps.push({ start, end, rom, dur, mcv: rom / dur, peak, leveled, flags: [...flags].sort() });
  }
  return reps;
}

/**
 * Full pipeline. t in seconds (any start), aRaw = vertical signal in m/s^2
 * (use magnitude() for a phone). S.LIFT = the profile the name or picker gave;
 * imu = { ax, ay, az, gx, gy, gz } arrays lets the sensor check machine vs free weight
 * (D21), and res.lift is the profile actually used. Returns everything the UI needs.
 */
export function analyze(tIn, aRaw, S = SETTINGS, imu = null) {
  if (tIn.length < 10) return { error: "Not enough data yet." };
  const t = Float64Array.from(tIn, (x) => x - tIn[0]);
  const dts = [];
  for (let i = 1; i < t.length; i++) dts.push(t[i] - t[i - 1]);
  const fs = 1 / median(dts);

  const early = [];
  for (let i = 0; i < t.length && t[i] < 0.3; i++) early.push(aRaw[i]);
  let g = median(early.length ? early : [aRaw[0]]);
  let runs = findStillRuns(aRaw, g, fs, S);
  if (!runs.length) return { error: "No still periods found. Start with the phone still.", fs };
  let sum = 0;
  let cnt = 0;
  for (const [s, e] of runs) for (let i = s; i <= e; i++) { sum += aRaw[i]; cnt++; }
  g = sum / cnt;
  runs = findStillRuns(aRaw, g, fs, S);
  if (!runs.length) return { error: "No still periods found. Start with the phone still.", fs };
  // Noise-matched limits (D20), only when tighter than the defaults, then gravity again.
  const { stdMax, devMax, noise } = stillLimits(aRaw, fs, runs, S);
  if (stdMax < S.STILL_STD || devMax < S.STILL_DEV) {
    runs = findStillRuns(aRaw, g, fs, S, stdMax, devMax);
    if (!runs.length) return { error: "No still periods found. Start with the phone still.", fs };
    sum = 0;
    cnt = 0;
    for (const [s, e] of runs) for (let i = s; i <= e; i++) { sum += aRaw[i]; cnt++; }
    g = sum / cnt;
    runs = findStillRuns(aRaw, g, fs, S, stdMax, devMax);
    if (!runs.length) return { error: "No still periods found. Start with the phone still.", fs };
  }
  let a = Float64Array.from(aRaw, (x) => x - g);
  const nCandidates = runs.length;
  runs = confirmStillRuns(a, t, fs, runs, S);
  // Machine or free weight (D21). Stills don't depend on the lift, so this can wait till here.
  const mount = detectMount(imu, runs, fs, S);
  const lift = chooseLift(S.LIFT, mount, S);
  S = { ...S, LIFT: lift };
  // A bar lift from the pod: the acceleration along up instead of |a| - g (D40)
  const hasGyro = imu && imu.gx && [imu.gx, imu.gy, imu.gz].some((c) => c.some((x) => x !== 0));
  const gyro = hasGyro ? Array.from({ length: t.length }, (_, i) => [imu.gx[i], imu.gy[i], imu.gz[i]]) : null;
  if (gyro && S.TA_BAR.includes(lift) && fs >= S.VERTICAL_FS[0] && fs <= S.VERTICAL_FS[1]) {
    const acc = Array.from({ length: t.length }, (_, i) => [imu.ax[i], imu.ay[i], imu.az[i]]);
    a = verticalAccel(t, acc, gyro, runs, fs, S);
  }

  const { v, windows } = velocityBetweenAnchors(a, t, fs, runs);
  // Windows with no still between their reps get turnaround anchors (D40): a broken window, or a bar
  // lift's window holding an impact. Not a hand-held phone test, nor a browser's 60 Hz
  const real = lift !== "phone" && fs >= S.WALK_MIN_FS;
  const hits = real ? impactMask(a, gyro, runs, fs, S) : null;
  const turns = windows.filter((w) => real && w.dur >= S.TA_MIN_WIN
    && (brokenWindow(v, w, S) || (S.TA_BAR.includes(lift) && hits.subarray(w.i0, w.i1 + 1).some((h) => h))));
  for (const w of turns) {
    w.turns = true;
    w.impacts = hits.subarray(w.i0, w.i1 + 1);   // the bar hit something: a jolt the gyro confirms
  }
  levelHeights(a, t, v, windows, S);
  for (const w of turns) v.set(turnaroundVelocity(a, t, fs, w, lift, S), w.i0);
  const x = integrate(v, t);
  // the walk to a pause's start (D32): bar and stack lifts at a real sensor rate, not hand-held tests
  const walk = lift !== "phone" && fs >= S.WALK_MIN_FS;
  const reps = findReps(x, v, t, windows, S, usualBottom(x, S), walk ? S.V_STOP_FRAC : 0);
  for (const r of reps) r.sticking = stickingPoint(x, v, t, r, S);   // analytics.md section 4
  return { t, fs, g, a, runs, windows, v, x, reps, ignored: nCandidates - runs.length, lift, mount, noise };
}

/** imu argument for analyze() from an array of samples { t, ax, ay, az, gx, gy, gz }. */
export function imuOf(samples) {
  const col = (k) => samples.map((x) => x[k] ?? 0);
  return { ax: col("ax"), ay: col("ay"), az: col("az"), gx: col("gx"), gy: col("gy"), gz: col("gz") };
}
