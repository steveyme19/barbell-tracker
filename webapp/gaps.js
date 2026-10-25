// Data-gap flags for the set cards (docs/ble-plan.md step 10). A hole in the pod's data (a dropout
// longer than its 38 s ring, a restart, a link that never came back) among a set's analyzed
// samples, or right at their edge, breaks the integration the reps come from: flag the card. Pure
// functions, so node tests them (webapp/test/gaps.mjs); app.js hands in the decoder's state.
//
// Times: the decoder's gaps and epochs are in its t; a set's tAbs and startT are session times,
// t - t0 (the tracker's first sample). A flag keeps its session time t, so its "at 0:14" (from the
// set's first rep) stays right after a rename re-analysis or a resume.

import { formatTime } from "./sets.js";

export const GAP_FLAG_S = 0.1;   // shorter holes (a missed sample or two) don't hurt the reps

/**
 * The flags for one set: [{ t, dur, kind, n }], dur null = open-ended (the phone knows only where
 * the data stopped). gaps / epochs: PodStream's; odr: samples/s; t0: the tracker's t0; lastT: the
 * last emitted t; endedLinkDown: the session ended while the link was down.
 */
export function gapsNear(set, { gaps, epochs, odr = 208, t0, lastT = null, endedLinkDown = false }) {
  if (!set.tAbs?.length || t0 === null || t0 === undefined) return [];
  const eps = 1.5 / odr;                  // a hole that starts or ends right at an analyzed sample counts
  const from = set.tAbs[0] - eps;
  const to = set.tAbs[set.tAbs.length - 1] + eps;
  const touches = (a, b) => b >= from && a <= to;
  const out = [];
  for (const g of gaps) {
    if (g.t0 === null || !(g.n > 0)) continue;
    const a = g.t0 - t0;
    // a tail the phone knows only a lower bound of (the link never came back): open-ended
    const dur = g.lower_bound ? null : g.t1 !== null ? g.t1 - g.t0 : g.n / odr;
    if ((dur !== null && dur < GAP_FLAG_S) || !touches(a, a + (dur ?? 0))) continue;
    out.push({ t: a, dur, kind: dur === null ? "link-lost" : g.kind, n: g.n });
  }
  // the pod restarted (a reset, or sleep and wake): the hole runs from the last sample before it
  for (let k = 1; k < epochs.length; k++) {
    if (epochs[k].first_t === null) continue;
    let prev = null;
    for (let j = k - 1; j >= 0 && prev === null; j--) prev = epochs[j].last_t;
    const a = (prev ?? epochs[k].first_t) - t0;
    if (touches(a, epochs[k].first_t - t0)) out.push({ t: a, dur: null, kind: "restart", n: null });
  }
  // the session ended with the link still down: everything after the last sample is missing
  if (endedLinkDown && lastT !== null) {
    const a = lastT - t0;
    if (touches(a, a)) out.push({ t: a, dur: null, kind: "link-lost", n: null });
  }
  // one flag per spot: a restart explains the open-ended tail at the same sample, and two
  // open-ended flags at one sample are one
  const same = (x, y) => Math.abs(x.t - y.t) <= eps;
  return out.filter((g, i) => g.kind !== "link-lost" ||
    (!out.some((r) => r.kind === "restart" && same(r, g)) && out.findIndex((h) => h.kind === "link-lost" && same(h, g)) === i))
    .sort((x, y) => x.t - y.t);
}

/** "data gap 1.2 s at 0:14: reps near it unreliable" (at: from the set's first rep). */
export function gapText(set) {
  const gaps = set.gaps;
  if (!gaps?.length) return "";
  const at = (g) => formatTime(Math.max(0, g.t - set.startT));
  const parts = gaps.map((g) => (g.kind === "restart" ? `pod restarted at ${at(g)}`
    : g.dur === null ? `link lost at ${at(g)}` : `data gap ${g.dur.toFixed(1)} s at ${at(g)}`));
  return `${parts.join("; ")}: reps near ${gaps.length > 1 ? "them" : "it"} unreliable`;
}
