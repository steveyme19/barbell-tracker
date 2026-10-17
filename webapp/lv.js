// Load-velocity profile and e1RM (roadmap 4.3). Port of analysis/lv.py (P1).
// One point per set: (load, best-rep MCV). Straight line by least squares, 2+ loads.
// e1RM = load where the line reaches the lift's velocity at 1RM (squat, bench,
// deadlift only), and only within 2x the heaviest load used.

import { SET_SETTINGS } from "./sets.js";

const E1RM_MAX_X = 2.0;

/** points: [[load, mcv], ...] -> { a, b, r2, n, loads, maxLoad, e1rm } or null. */
export function fitProfile(points, lift) {
  const pts = points.map(([l, v]) => [Number(l), Number(v)]).filter(([l, v]) => l > 0 && v > 0);
  const loads = [...new Set(pts.map(([l]) => l))].sort((x, y) => x - y);
  if (loads.length < 2) return null;
  const n = pts.length;
  const mx = pts.reduce((s, [l]) => s + l, 0) / n;
  const my = pts.reduce((s, [, v]) => s + v, 0) / n;
  let sxx = 0, sxy = 0, syy = 0;
  for (const [l, v] of pts) { sxx += (l - mx) ** 2; sxy += (l - mx) * (v - my); syy += (v - my) ** 2; }
  const b = sxy / sxx;
  const a = my - b * mx;
  const r2 = syy > 0 ? (sxy * sxy) / (sxx * syy) : 1;
  let e1rm = null;
  const v1 = SET_SETTINGS.V1RM[lift];
  if (v1 !== undefined && b < 0) {
    const e = (v1 - a) / b;
    if (e > 0 && e <= E1RM_MAX_X * loads[loads.length - 1]) e1rm = e;
  }
  return { a, b, r2, n, loads: loads.length, maxLoad: loads[loads.length - 1], e1rm };
}
