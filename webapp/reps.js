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
  LONG_WINDOW: 4.0,      // s: flag reps inside longer windows (more drift)
  BIG_CORRECTION: 0.15,  // m/s: flag windows whose drift correction was this big
  LEVEL_MAX_DEG: 3,      // highest power of t in the velocity drift curve (lockouts only)
  LEVEL_MAX_DEG_BOTH: 5, // same, when bottoms are leveled too
  LEVEL_FLAG: 0.02,      // m: flag a rep whose lockout misses the common height by this much
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

function median(arr) {
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
  const x = integrate(v, t);
  const { lows, highs } = turningPoints(x, S.MIN_ROM);
  const minFrom = (i, j) => { let m = Infinity; for (let k = i; k <= j; k++) m = Math.min(m, x[k]); return m; };
  const maxFrom = (i, j) => { let m = -Infinity; for (let k = i; k <= j; k++) m = Math.max(m, x[k]); return m; };
  for (const w of windows) {
    const { i0, i1 } = w;
    let tops = highs.filter((h) => h > i0 && h < i1);
    let bottoms = lows.filter((b) => b > i0 && b < i1);
    if (!w.anchored) {
      // only turnarounds the bar has already moved away from
      tops = tops.filter((h) => x[h] - minFrom(h, i1) >= S.MIN_ROM);
      bottoms = bottoms.filter((b) => maxFrom(b, i1) - x[b] >= S.MIN_ROM);
    }
    const turns = [...tops, ...bottoms].sort((p, q) => p - q);
    if (prof.ends && turns.length) {
      const first = turns[0];
      const last = turns[turns.length - 1];
      if (bottoms.includes(first) && x[i0] - x[first] >= S.MIN_ROM) tops = [i0, ...tops];
      else if (tops.includes(first) && x[first] - x[i0] >= S.MIN_ROM) bottoms = [i0, ...bottoms];
      if (w.anchored) {
        if (bottoms.includes(last) && x[i1] - x[last] >= S.MIN_ROM) tops = [...tops, i1];
        else if (tops.includes(last) && x[last] - x[i1] >= S.MIN_ROM) bottoms = [...bottoms, i1];
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

/** Median height of the turnaround bottoms, for lifts whose bottom repeats; else null. */
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
    const hi = highs.find((h) => h > lo);
    if (hi === undefined) continue;
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
    const rom = x[end] - x[start];
    const dur = t[end] - t[start];
    let peak = -Infinity;
    for (let i = start; i <= end; i++) peak = Math.max(peak, v[i]);
    const flags = new Set();
    for (const w of windows) {
      if (w.i0 <= end && w.i1 >= start) {
        if (!w.anchored) flags.add("no end anchor");
        if (w.leveled) {
          if (w.topHeight !== undefined && Math.abs(x[hi] - (x[w.i0] + w.topHeight)) > S.LEVEL_FLAG) flags.add("uneven lockout");
        } else if (w.dur > S.LONG_WINDOW) {
          flags.add(`long window ${w.dur.toFixed(1)}s`);
        }
        if (w.anchored && Math.abs(w.rawEnd) > S.BIG_CORRECTION) flags.add("big drift correction");
      }
    }
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
  const a = Float64Array.from(aRaw, (x) => x - g);
  const nCandidates = runs.length;
  runs = confirmStillRuns(a, t, fs, runs, S);
  // Machine or free weight (D21). Stills don't depend on the lift, so this can wait till here.
  const mount = detectMount(imu, runs, fs, S);
  const lift = chooseLift(S.LIFT, mount, S);
  S = { ...S, LIFT: lift };

  const { v, windows } = velocityBetweenAnchors(a, t, fs, runs);
  levelHeights(a, t, v, windows, S);
  const x = integrate(v, t);
  // the walk to a pause's start (D32): bar and stack lifts at a real sensor rate, not hand-held tests
  const walk = lift !== "phone" && fs >= S.WALK_MIN_FS;
  const reps = findReps(x, v, t, windows, S, usualBottom(x, S), walk ? S.V_STOP_FRAC : 0);
  return { t, fs, g, a, runs, windows, v, x, reps, ignored: nCandidates - runs.length, lift, mount, noise };
}

/** imu argument for analyze() from an array of samples { t, ax, ay, az, gx, gy, gz }. */
export function imuOf(samples) {
  const col = (k) => samples.map((x) => x[k] ?? 0);
  return { ax: col("ax"), ay: col("ay"), az: col("az"), gx: col("gx"), gy: col("gy"), gz: col("gz") };
}
