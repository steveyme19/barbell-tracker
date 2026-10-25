// The set cards' data-gap flags (webapp/gaps.js, docs/ble-plan.md step 10), on made-up sets:
//
//   node webapp/test/gaps.mjs
//
// A set is its analyzed samples (tAbs, lead-in and trail-out included) and its first rep's start
// (startT), in session time; the decoder's gaps and epochs are in stream t, offset by t0.

import { gapsNear, gapText } from "../gaps.js";

let ok = true;
const check = (good, msg) => { console.log(`${good ? "PASS" : "FAIL"}  ${msg}`); ok &&= Boolean(good); };

const ODR = 208;
const T0 = 5;                                            // the tracker's t0 in stream t
const span = (a, b) => Array.from({ length: Math.round((b - a) * ODR) + 1 }, (_, i) => a + i / ODR);
// a set analyzed from 100 s to 130 s, first rep at 102 s
const SET = { tAbs: span(100, 130), startT: 102 };
const at = (t) => t + T0;                                // session time -> stream t
const gap = (kind, a, b, n, extra = {}) => ({ kind, t0: at(a), t1: b === null ? null : at(b), n, ...extra });
const ep = (first, last) => ({ first_t: first === null ? null : at(first), last_t: last === null ? null : at(last) });
const flags = (set, o) => { const g = gapsNear(set, { gaps: [], epochs: [ep(0, 0)], odr: ODR, t0: T0, ...o }); return { g, text: gapText({ ...set, gaps: g }) }; };

{ // a 60 s outage mid-set: 22 s lost, the hole starts at the set's last sample
  const { g, text } = flags(SET, { gaps: [gap("overflow", 130, 152, 4576)] });
  check(g.length === 1 && /^data gap 22\.0 s at 0:28/.test(text), `hole at the set's last sample: "${text}"`);
}
{ // the same hole 1 s after the set's samples end (its trail-out is already in tAbs): the set is
  // complete, no flag. Adding another 2 s of trail to the window flagged it (review finding 7)
  const { g } = flags(SET, { gaps: [gap("overflow", 131, 153, 4576)] });
  check(g.length === 0, `hole 1 s after the set's last sample: no flag (${g.length})`);
}
{ // a hole that ends at the set's first sample (the set right after an outage) is flagged
  const { g, text } = flags(SET, { gaps: [gap("overflow", 78, 100, 4576)] });
  check(g.length === 1 && /data gap 22\.0 s at 0:00/.test(text), `hole ending at the set's first sample: "${text}"`);
}
{ // short holes: a missed sample or two don't count, half a second does
  const { g } = flags(SET, { gaps: [gap("imu", 110, 110.01, 1), gap("imu-restart", 115, 115.5, 0), gap("unexplained", 120, 120.5, 104)] });
  check(g.length === 1 && g[0].kind === "unexplained", `holes of 10 ms (n 1), 0.5 s with n 0, 0.5 s with n 104: flags ${JSON.stringify(g.map((x) => x.kind))}`);
}
{ // the link never came back (End while reconnecting): a lower-bound tail is open-ended (finding 5)
  const { g, text } = flags(SET, { gaps: [gap("lost-tail", 130, 130.2, 40, { lower_bound: true })] });
  check(g.length === 1 && g[0].dur === null && /^link lost at 0:28/.test(text), `lower-bound tail: "${text}"`);
}
{ // ...and with no tail recorded at all, the session ending with the link down still flags it
  const { g, text } = flags(SET, { lastT: at(130), endedLinkDown: true });
  check(g.length === 1 && /^link lost at 0:28/.test(text), `ended with the link down, no tail gap: "${text}"`);
  const { g: g2 } = flags(SET, { lastT: at(130), endedLinkDown: false });
  check(g2.length === 0, `the same set with the link up at the end: no flag (${g2.length})`);
}
{ // a restart whose hole spans the set's end (the pod reset mid-set): the interval counts, not just
  // the new epoch's first sample far past the set (finding 5); the lost-tail at the same spot is one flag
  const epochs = [ep(0, 130), ep(170, 300)];
  const { g, text } = flags(SET, { epochs, gaps: [gap("lost-tail", 130, null, 900, { lower_bound: true })] });
  check(g.length === 1 && g[0].kind === "restart" && /^pod restarted at 0:28: reps near it unreliable$/.test(text), `reset mid-set: "${text}"`);
}
{ // a restart long after the set: no flag
  const { g } = flags(SET, { epochs: [ep(0, 140), ep(170, 300)] });
  check(g.length === 0, `restart 10 s after the set: no flag (${g.length})`);
}
{ // two holes in one set: both, in time order
  const { text } = flags(SET, { gaps: [gap("overflow", 125, 128, 624), gap("resync", 105, 106, 208)] });
  check(text === "data gap 1.0 s at 0:03; data gap 3.0 s at 0:23: reps near them unreliable", `two holes: "${text}"`);
}
{ // the flag's "at" follows the set's first rep: a rename that drops the first rep moves it (finding 10)
  const { g } = flags(SET, { gaps: [gap("overflow", 120, 121, 208)] });
  const before = gapText({ ...SET, gaps: g });
  const after = gapText({ ...SET, startT: 106, gaps: g });
  check(/at 0:18/.test(before) && /at 0:14/.test(after), `"at" from the first rep: "${before}" -> after a rename "${after}"`);
}
{ // the phone (no decoder state) and an empty set give nothing
  check(gapsNear({ tAbs: [] }, { gaps: [gap("overflow", 1, 2, 9)], epochs: [], t0: T0 }).length === 0 && gapText({ gaps: [] }) === "",
    "an empty set or no gaps: no flag, no text");
}

console.log(ok ? "PASS" : "FAIL");
process.exit(ok ? 0 : 1);
