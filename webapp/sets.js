// Session mode: find sets in a live stream, time the rest, analyze each set.
// Port of analysis/sets.py (Python is the reference, decision P1). Python works on a
// whole file at once; this works sample by sample, and the parity test checks that
// both find the same sets, rests and reps.

import { analyze, magnitude, imuOf, SETTINGS } from "./reps.js";

export const SET_SETTINGS = {
  BLOCK: 0.25,      // s: activity is decided per block of this length
  ACTIVE_STD: 0.15, // m/s^2: block std of |a| above this = moving
  SET_GAP: 15.0,    // s: this much quiet ends a set
  LEAD: 2.0,        // s: still before the first active block handed to the rep algorithm
  TRAIL: 2.0,       // s: still after the last active block
  RACK_FRAC: 0.5,   // reps at either end shorter than this x median ROM = rack moves
  RACK_MIN_ROM: 0.12, // m: an end rep shorter than this is a rack move / stack bump (not phone tests)
  RACK_MAX_ROM: { squat: 1.0, bench: 0.8, deadlift: 1.0 },   // m: an end "rep" above this is the sensor handled (D31)
  RACK_MAX_FRAC: 2.0, // an end rep over this x the median of the set's OTHER reps is handling too
  // RPE v0, uncalibrated (analytics.md section 5): RIR = (last MCV - v1RM) / DV_PER_REP
  V1RM: { squat: 0.23, bench: 0.10, deadlift: 0.14 },
  DV_PER_REP: { squat: 0.04, bench: 0.035, deadlift: 0.04 },
};
const S = SET_SETTINGS;

function median(xs) {
  const s = [...xs].sort((a, b) => a - b);
  const m = s.length >> 1;
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}

/** The ends of a set that aren't reps. Too big first: handling the sensor (one huge end "rep").
 * Then too small: unrack/rerack bumps. The small floor comes from what's left, so a 2 m pickup
 * can't lift the median and push a real single under it. */
export function dropRackMoves(reps, lift) {
  if (!reps.length) return reps;
  const cap = S.RACK_MAX_ROM[lift] ?? Infinity;
  const base = lift !== "phone" ? S.RACK_MIN_ROM : 0;   // bar or stack, not hand-held
  // the scale: the other reps, not the rack bumps among them (a ~0 m unrack made a single "handling")
  const big = (r, others) => {
    const o = others.map((x) => x.rom).filter((x) => x >= Math.max(base, SETTINGS.MIN_ROM));
    return r.rom > cap || (o.length > 0 && r.rom > S.RACK_MAX_FRAC * median(o));
  };
  let a = 0, b = reps.length;
  while (b > a && big(reps[b - 1], reps.slice(a, b - 1))) b--;
  while (a < b && big(reps[a], reps.slice(a + 1, b))) a++;
  reps = reps.slice(a, b);
  if (!reps.length) return reps;
  let floor = base;
  if (reps.length >= 2) floor = Math.max(floor, S.RACK_FRAC * median(reps.map((r) => r.rom)));
  a = 0; b = reps.length;
  while (a < b && reps[a].rom < floor) a++;
  while (b > a && reps[b - 1].rom < floor) b--;
  return reps.slice(a, b);
}

/** A closed set's reps: the anchored ones, rack moves and handling off the ends (D31). Its TRAIL
 * is quiet, usually a still that anchors the last rep, so an unanchored rep after anchored ones ran
 * up to the cut (the sensor handled). With no rep anchored at all (hands left on the racked bar),
 * keep them all, flagged, rather than lose the set and its raw data. */
export function closedSetReps(reps, lift) {
  const anchored = reps.filter((r) => !r.flags.includes("no end anchor"));
  return dropRackMoves(anchored.length ? anchored : reps, lift);
}

/** [low, high] RPE (5 to 10, half steps) from the last rep's MCV, or null. Uncalibrated v0. */
export function rpeEstimate(lift, lastMcv) {
  if (!(lift in S.V1RM)) return null;
  const rir = Math.max(0, (lastMcv - S.V1RM[lift]) / S.DV_PER_REP[lift]);
  const rpe = 10 - rir;
  const clamp = (x) => Math.min(10, Math.max(5, Math.round(x * 2) / 2));
  return [clamp(rpe - 1), clamp(rpe + 1)];
}

export function formatRpe(r) {
  if (!r) return "--";
  return r[0] === 5 ? `≤${r[1]}` : `${r[0]}-${r[1]}`;
}

/** 125.4 -> "2:05" */
export function formatTime(sec) {
  const s = Math.round(sec);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/** Numbers for the set card and session.csv. tAbs[i] = session time of segment sample i. */
export function setSummary(lift, reps, tAbs) {
  const mcvs = reps.map((r) => r.mcv);
  const best = Math.max(...mcvs), last = mcvs[mcvs.length - 1];
  return {
    lift, nReps: reps.length,
    startT: tAbs[reps[0].start], endT: tAbs[reps[reps.length - 1].end],
    bestMcv: best, lastMcv: last, velLoss: 1 - last / best,
    avgRom: reps.reduce((s, r) => s + r.rom, 0) / reps.length,
    rpeEst: rpeEstimate(lift, last),
  };
}

/**
 * Feed it samples with push(); it calls onSet(set) whenever a set finishes (15 s of
 * quiet after it). liftFor(k) gives the lift for the k-th set found (the sticky picker).
 * Times are session times: seconds since the first sample.
 */
export class SessionTracker {
  constructor({ liftFor = () => "phone", onSet = () => {}, onSegmentStart = () => {}, onDrop = () => {} } = {}) {
    Object.assign(this, { liftFor, onSet, onSegmentStart, onDrop });
    this.t0 = null;
    this.buf = [];          // samples kept: during rest only the last few seconds
    this.bufT = [];         // their session times
    this.block = -1;        // block being filled
    this.blockA = [];       // |a| of its samples
    this.segStart = null;   // first active block of the open segment
    this.segLast = null;    // last active block
    this.sets = [];
    this.now = 0;           // session time of the latest sample
  }

  get gapBlocks() { return Math.round(S.SET_GAP / S.BLOCK); }

  /** Is a set (or something that might be one) in progress? */
  get inSegment() { return this.segStart !== null; }

  /** Quiet time since the last active block of the open segment, s. */
  get quietFor() { return this.segLast === null ? 0 : this.now - (this.segLast + 1) * S.BLOCK; }

  /** Samples of the open segment so far, lead-in included (for live analysis). */
  segmentSamples() {
    if (!this.inSegment) return { samples: [], tAbs: [] };
    const from = this.segStart * S.BLOCK - S.LEAD;
    let i = 0;
    while (i < this.bufT.length && this.bufT[i] < from) i++;
    return { samples: this.buf.slice(i), tAbs: this.bufT.slice(i) };
  }

  push(s) {
    if (this.t0 === null) this.t0 = s.t;
    const rel = s.t - this.t0;
    this.now = rel;
    const k = Math.floor(rel / S.BLOCK);
    if (k !== this.block) {
      if (this.block >= 0) this.finishBlock(this.block);
      for (let b = this.block + 1; b < k; b++) this.finishBlock(b, true);   // empty blocks: quiet
      this.block = k;
      this.blockA = [];
    }
    this.blockA.push(Math.sqrt(s.ax * s.ax + s.ay * s.ay + s.az * s.az));   // same as magnitude() and Python
    this.buf.push(s);
    this.bufT.push(rel);
    if (!this.inSegment) this.trim();
  }

  /** End of session: decide the last block and close an open segment. */
  finish() {
    if (this.block >= 0) this.finishBlock(this.block);
    this.block = Infinity;
    if (this.inSegment) this.closeSegment();
  }

  finishBlock(b, empty = false) {
    const xs = empty ? [] : this.blockA;
    let active = false;
    if (xs.length >= 3) {
      const m = xs.reduce((p, x) => p + x, 0) / xs.length;
      const v = xs.reduce((p, x) => p + (x - m) ** 2, 0) / xs.length;   // population std, like numpy
      active = Math.sqrt(v) > S.ACTIVE_STD;
    }
    if (active) {
      if (!this.inSegment) { this.segStart = b; this.onSegmentStart(b * S.BLOCK); }
      this.segLast = b;
    } else if (this.inSegment && b - this.segLast >= this.gapBlocks) {
      this.closeSegment();
    }
  }

  closeSegment() {
    const from = this.segStart * S.BLOCK - S.LEAD;
    const to = (this.segLast + 1) * S.BLOCK + S.TRAIL;
    const idx = [];
    for (let i = 0; i < this.bufT.length; i++) if (this.bufT[i] >= from && this.bufT[i] < to) idx.push(i);
    const samples = idx.map((i) => this.buf[i]);
    const tAbs = idx.map((i) => this.bufT[i]);
    this.segStart = this.segLast = null;
    this.trim();
    const named = this.liftFor(this.sets.length);
    const res = analyze(samples.map((x) => x.t), magnitude(samples.map((x) => x.ax), samples.map((x) => x.ay),
      samples.map((x) => x.az)), { ...SETTINGS, LIFT: named }, imuOf(samples));
    const lift = res.error ? named : res.lift;            // the name, or what the sensor saw (D21)
    const reps = res.error ? [] : closedSetReps(res.reps, lift);
    if (!reps.length) { this.onDrop(); return; }          // plates, a bump, the sensor handled: not a set
    const set = { ...setSummary(lift, reps, tAbs), reps, samples, tAbs, analysis: res, index: this.sets.length };
    const prev = this.sets[this.sets.length - 1];
    set.restBefore = prev ? set.startT - prev.endT : null;
    this.sets.push(set);
    this.onSet(set);
  }

  /** Outside a segment, keep only what a new segment's lead-in could need. */
  trim() {
    const keepFrom = (this.block - 1) * S.BLOCK - S.LEAD;
    let i = 0;
    while (i < this.bufT.length && this.bufT[i] < keepFrom) i++;
    if (i > 0) { this.buf.splice(0, i); this.bufT.splice(0, i); }
  }
}
