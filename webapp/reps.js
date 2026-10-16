// Rep detection, ported line for line from analysis/reps.py.
// Python is the reference (decision P1). If you change the algorithm, change it
// in Python first, then here, then rerun the parity test (webapp/test/parity.mjs).
//
// Pure functions only: no DOM, so it runs in the browser and in Node.

export const SETTINGS = {
  STILL_WINDOW: 0.2,     // s: window for the stillness test
  STILL_STD: 0.15,       // m/s^2: max wiggle inside the window
  STILL_DEV: 0.3,        // m/s^2: max |average - g| inside the window
  MIN_STILL: 0.25,       // s: a still period has to last at least this long
  STILL_MAX_SPEED: 0.1,  // m/s: faster than this during a "still" period = coasting
  LONG_STILL: 1.0,       // s: stills at least this long are real (nothing coasts that long)
  COAST_TRUST: 4.0,      // s: past this long since the last anchor, drift is too big for the speed check
  COAST_LOOK: 0.5,       // s: how far before and after a "still" period to look
  COAST_DV: 0.1,         // m/s: speed change on both sides that marks a coast
  MIN_ROM: 0.05,         // m: smaller up/down moves are wiggles, not reps
  LONG_WINDOW: 4.0,      // s: flag reps inside longer windows (more drift)
  BIG_CORRECTION: 0.15,  // m/s: flag windows whose drift correction was this big
  LEVEL_MIN_TOPS: 3,     // lockouts needed inside one window before we level them
  LEVEL_MIN_TOPS_OPEN: 2, // same, for motion with no still after it yet (live, mid-set)
  LEVEL_MAX_DEG: 3,      // highest power of t in the velocity drift curve
  LEVEL_FLAG: 0.02,      // m: flag a rep whose lockout misses the common height by this much
};

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
export function findStillRuns(aRaw, g, fs, S = SETTINGS) {
  const n = Math.trunc(S.STILL_WINDOW * fs);
  const { mean, std } = rolling(aRaw, n);
  const N = aRaw.length;
  const still = (i) => std[i] < S.STILL_STD && Math.abs(mean[i] - g) < S.STILL_DEV;
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

/** Drop "still" periods that are really steady motion (accelerometers can't tell). */
/** Change in velocity from sample i to sample j (clipped to the recording). */
function speedChange(a, t, i, j) {
  i = Math.max(i, 0);
  j = Math.min(j, t.length - 1);
  if (j <= i) return 0;
  const v = integrate(a.slice(i, j + 1), t.slice(i, j + 1).map((x) => x - t[i]));
  return v[v.length - 1];
}

/** Throw out "still" periods that are really coasting. See confirm_still_runs() in reps.py. */
export function confirmStillRuns(a, t, fs, runs, S = SETTINGS) {
  const n = Math.trunc(S.COAST_LOOK * fs);
  const confirmed = [runs[0]];
  for (const [s, e] of runs.slice(1)) {
    if (t[e] - t[s] >= S.LONG_STILL) {   // long stills are always real
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
 * No-pause sets: remove curved drift by assuming every lockout is at the same height.
 * Velocity drift e(tau) = d1 tau + d2 tau^2 + d3 tau^3 (tau = time / window length),
 * e(1) = raw end velocity, d2..d3 and the lockout height H by least squares.
 * Still moving when the data ends: d1 is fitted too, from 2+ lockouts (provisional).
 * See level_tops() in reps.py for the full explanation.
 */
export function levelTops(a, t, v, windows, S = SETTINGS) {
  const x = integrate(v, t);
  const { highs } = turningPoints(x, S.MIN_ROM);
  for (const w of windows) {
    const { i0, i1 } = w;
    let tops = highs.filter((h) => h > i0 && h < i1);
    if (!w.anchored) {
      // only lockouts the bar has already come down from
      tops = tops.filter((h) => {
        let lo = Infinity;
        for (let i = h; i <= i1; i++) lo = Math.min(lo, x[i]);
        return x[h] - lo >= S.MIN_ROM;
      });
    }
    if (tops.length < (w.anchored ? S.LEVEL_MIN_TOPS : S.LEVEL_MIN_TOPS_OPEN)) continue;
    const tw = t.slice(i0, i1 + 1).map((x) => x - t[i0]);
    const T = tw[tw.length - 1];
    const vRaw = integrate(a.slice(i0, i1 + 1), tw);
    const xRaw = integrate(vRaw, tw);
    const rows = [];
    const rhs = [];
    let d;
    let sol;
    if (w.anchored) {
      const deg = Math.min(S.LEVEL_MAX_DEG, tops.length - 1);
      const eEnd = vRaw[vRaw.length - 1];
      for (const h of tops) {
        const tau = tw[h - i0] / T;
        const row = [];
        for (let k = 2; k <= deg; k++) row.push(T * (tau ** (k + 1) / (k + 1) - tau ** 2 / 2));
        row.push(1);
        rows.push(row);
        rhs.push(xRaw[h - i0] - (T * eEnd * tau ** 2) / 2);
      }
      sol = leastSquares(rows, rhs);
      d = [eEnd];                                       // d1 = eEnd - d2 - ... - d_deg
      for (let k = 0; k < sol.length - 1; k++) { d[0] -= sol[k]; d.push(sol[k]); }
    } else {
      const deg = Math.max(1, Math.min(S.LEVEL_MAX_DEG, tops.length - 2));   // unknowns d1..d_deg and H
      for (const h of tops) {
        const tau = tw[h - i0] / T;
        const row = [];
        for (let k = 1; k <= deg; k++) row.push((T * tau ** (k + 1)) / (k + 1));
        row.push(1);
        rows.push(row);
        rhs.push(xRaw[h - i0]);
      }
      sol = leastSquares(rows, rhs);
      d = Array.from(sol.slice(0, sol.length - 1));
    }
    for (let i = 0; i < tw.length; i++) {
      const tau = tw[i] / T;
      let e = 0;
      for (let k = 0; k < d.length; k++) e += d[k] * tau ** (k + 1);
      v[i0 + i] = vRaw[i] - e;
    }
    w.leveled = true;
    w.topHeight = sol[sol.length - 1];                // H, relative to the height at i0
  }
  return v;
}

export function findReps(x, v, t, windows, S = SETTINGS) {
  const { lows, highs } = turningPoints(x, S.MIN_ROM);
  const reps = [];
  for (const lo of lows) {
    const hi = highs.find((h) => h > lo);
    if (hi === undefined) continue;
    let start = lo;
    for (let i = lo; i <= hi; i++) if (v[i] <= 0 && x[i] <= x[lo] + S.MIN_ROM) start = i;
    let end = hi;
    for (let i = start + 1; i <= hi; i++) {
      if (v[i] <= 0 && x[i] >= x[hi] - S.MIN_ROM) { end = i; break; }
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
          if (Math.abs(x[hi] - (x[w.i0] + w.topHeight)) > S.LEVEL_FLAG) flags.add("uneven lockout");
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
 * (use magnitude() for a phone). Returns everything the UI needs.
 */
export function analyze(tIn, aRaw, S = SETTINGS) {
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
  const a = Float64Array.from(aRaw, (x) => x - g);
  const nCandidates = runs.length;
  runs = confirmStillRuns(a, t, fs, runs, S);

  const { v, windows } = velocityBetweenAnchors(a, t, fs, runs);
  levelTops(a, t, v, windows, S);
  const x = integrate(v, t);
  const reps = findReps(x, v, t, windows, S);
  return { t, fs, g, a, runs, windows, v, x, reps, ignored: nCandidates - runs.length };
}
