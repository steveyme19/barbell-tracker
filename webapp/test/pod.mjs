// PodLink parity test: the JS PodStream (webapp/sources.js) must match the Python reference
// (analysis/podlink.py) on every test vector analysis/make_fake_pod.py writes.
//
// Usage, from the repo root (regenerate the vectors first if podlink.py or the bench changed):
//   cd analysis && python make_fake_pod.py && cd ..
//   node webapp/test/pod.mjs
//
// For every run it replays the logged decoder input (packets or frames with their arrival times,
// command() calls, STOP, timer calls) into a fresh JS PodStream and requires exactly the same
// output: samples (t and values bit for bit), gaps, counters, faults, events, command bytes,
// stop-wait times and stop-timeout results. Any difference is a FAIL. Each run is replayed twice
// (D45): from fake_pod_packets.csv (every CRP packet the client received, through
// PodStream.packet and so podFrameOf; tx and link rows are for reading only) and from
// fake_pod_frames.csv (the frames Python's crp_frame took out of them, through PodStream.frame).
// On the packets pass, podFrameOf must also give exactly the frames file's frames.

import { readFileSync, existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { PodStream, podFrameOf } from "../sources.js";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const DATA = path.join(ROOT, "analysis", "data");

function readCsv(name) {
  const file = path.join(DATA, name);
  if (!existsSync(file)) {
    console.error(`Missing ${file}. Run: cd analysis && python make_fake_pod.py`);
    process.exit(2);
  }
  const lines = readFileSync(file, "utf8").split(/\r?\n/).filter((l) => l.length);
  const head = lines[0].split(",");
  return lines.slice(1).map((l) => {
    const v = l.split(",");
    const o = {};
    head.forEach((h, i) => { o[h] = v[i]; });
    if (v.length > head.length) o[head[head.length - 1]] = v.slice(head.length - 1).join(",");
    return o;
  });
}

const groupBy = (rows) => {
  const m = new Map();
  for (const r of rows) {
    if (!m.has(r.scenario)) m.set(r.scenario, []);
    m.get(r.scenario).push(r);
  }
  return m;
};

const hexToBytes = (hex) => {
  const b = new Uint8Array(hex.length / 2);
  for (let i = 0; i < b.length; i++) b[i] = parseInt(hex.substr(2 * i, 2), 16);
  return b;
};
const hexToView = (hex) => new DataView(hexToBytes(hex).buffer);
const toHex = (b) => Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
const pyNum = (s) => (s === "None" ? null : parseFloat(s));   // Python repr() -> number / null

const expected = groupBy(readCsv("fake_pod_expected.csv"));
const gapsWant = groupBy(readCsv("fake_pod_gaps.csv"));
const statsWant = groupBy(readCsv("fake_pod_stats.csv"));
const eventsWant = groupBy(readCsv("fake_pod_events.csv"));

/** Replay one run's log into a fresh PodStream and compare everything with Python's. */
function check(name, steps) {
  const fails = [];
  const samples = [];
  const events = [];
  const s = new PodStream((x) => samples.push(x), (k, i) => events.push([k, i]));

  // 1. replay the decoder's input, checking every output the log carries along the way
  for (const st of steps) {
    if (st.kind === "pkt") {
      s.packet(hexToBytes(st.payload), parseFloat(st.arrival_ms));
    } else if (st.kind === "frame") {
      s.frame(hexToView(st.payload), parseFloat(st.arrival_ms));
    } else if (st.kind === "cmd") {
      const b = s.command();
      const got = b === null ? "none" : toHex(b);
      if (got !== st.payload) fails.push(`step ${st.step}: command() ${got}, Python ${st.payload}`);
    } else if (st.kind === "stop") {
      const got = toHex(s.stopCommand());
      if (got !== st.payload) fails.push(`step ${st.step}: stopCommand() ${got}, Python ${st.payload}`);
    } else if (st.kind === "call") {
      const [fn, val] = st.payload.split("=");
      if (fn === "finish") s.finish();
      else if (fn === "stop_wait_ms") {
        const got = s.stopWaitMs();
        if (got !== parseFloat(val)) fails.push(`step ${st.step}: stopWaitMs ${got}, Python ${val}`);
      } else if (fn === "stop_timed_out") {
        const got = s.stopTimedOut() ? "1" : "0";
        if (got !== val) fails.push(`step ${st.step}: stopTimedOut ${got}, Python ${val}`);
      } else {
        fails.push(`step ${st.step}: unknown call ${st.payload}`);
      }
    } else if (st.kind !== "tx" && st.kind !== "link") {
      fails.push(`step ${st.step}: unknown row kind ${st.kind}`);   // a new kind must not pass silently
    }
  }

  // 2. samples: same count, t and values bit for bit
  const want = expected.get(name) ?? [];
  let maxDt = 0, maxDv = 0;
  if (want.length !== samples.length) {
    fails.push(`${samples.length} samples, Python ${want.length}`);
  } else {
    for (let i = 0; i < want.length; i++) {
      const w = want[i], g = samples[i];
      maxDt = Math.max(maxDt, Math.abs(g.t - parseFloat(w.t)));
      for (const k of ["ax", "ay", "az", "gx", "gy", "gz"]) maxDv = Math.max(maxDv, Math.abs(g[k] - parseFloat(w[k])));
    }
    if (maxDt !== 0 || maxDv !== 0) fails.push(`samples differ: max |dt| ${maxDt}, max |dvalue| ${maxDv}`);
  }

  // 3. gaps
  const gw = gapsWant.get(name) ?? [];
  if (gw.length !== s.gaps.length) {
    fails.push(`${s.gaps.length} gaps, Python ${gw.length}`);
  } else {
    gw.forEach((w, i) => {
      const g = s.gaps[i];
      const same = g.kind === w.kind && g.n === +w.n && g.idx === +w.idx && g.t0 === pyNum(w.t0) &&
        g.t1 === pyNum(w.t1) && (g.lower_bound ? 1 : 0) === +w.lower_bound;
      if (!same) fails.push(`gap ${i}: ${JSON.stringify(g)} vs Python ${JSON.stringify(w)}`);
    });
  }

  // 4. counters and faults
  const sw = (statsWant.get(name) ?? [])[0];
  if (!sw) {
    fails.push("no Python stats row");
  } else {
    for (const k of Object.keys(s.stats)) {
      if (!(k in sw)) fails.push(`counter ${k} missing from the Python stats`);
      else if (s.stats[k] !== +sw[k]) fails.push(`stats.${k} = ${s.stats[k]}, Python ${sw[k]}`);
    }
    for (const k of Object.keys(sw)) {
      if (k !== "scenario" && k !== "faults" && !(k in s.stats)) fails.push(`counter ${k} missing in JS`);
    }
    if (s.faults.join("|") !== (sw.faults ?? "")) fails.push(`faults [${s.faults}], Python [${sw.faults}]`);
  }

  // 5. events, in order (kind, and the info when it's a plain string)
  const ew = eventsWant.get(name) ?? [];
  const eg = events.map(([k, i]) => [k, i === null || typeof i === "object" ? "" : String(i)]);
  if (ew.length !== eg.length) {
    fails.push(`${eg.length} events, Python ${ew.length}`);
  } else {
    for (let i = 0; i < ew.length; i++) {
      if (ew[i].kind !== eg[i][0] || (ew[i].info ?? "") !== eg[i][1]) {
        fails.push(`event ${i}: ${eg[i]} vs Python ${ew[i].kind},${ew[i].info}`);
        break;
      }
    }
  }
  return { fails, samples, gaps: s.gaps };
}

/** The packets file's run as the frames file has it: podFrameOf (JS) on every pkt row must give
 * exactly the frame rows Python's crp_frame gave, same bytes and arrivals, in order. */
function adapterDiffers(pktSteps, frameSteps) {
  const js = [];
  for (const st of pktSteps) {
    if (st.kind !== "pkt") continue;
    const f = podFrameOf(hexToBytes(st.payload));
    if (f !== null) js.push(`${st.arrival_ms} ${toHex(f)}`);
  }
  const py = (frameSteps ?? []).filter((st) => st.kind === "frame").map((st) => `${st.arrival_ms} ${st.payload}`);
  if (js.length !== py.length) return `podFrameOf gives ${js.length} frames, crp_frame ${py.length}`;
  const i = js.findIndex((v, k) => v !== py[k]);
  return i < 0 ? null : `frame ${i}: podFrameOf ${js[i].slice(0, 40)}..., crp_frame ${py[i].slice(0, 40)}...`;
}

let nRuns = 0, nFail = 0;
const names = {};
const frameRuns = groupBy(readCsv("fake_pod_frames.csv"));
for (const [file, label] of [["fake_pod_packets.csv", "packets"], ["fake_pod_frames.csv", "frames"]]) {
  const runs = label === "frames" ? frameRuns : groupBy(readCsv(file));
  names[label] = [...runs.keys()];
  console.log(`-- ${file}: ${runs.size} runs, through PodStream.${label === "packets" ? "packet" : "frame"}`);
  for (const [name, steps] of runs) {
    const { fails, samples, gaps } = check(name, steps);
    if (label === "packets") {
      // every run starts with the pod's HELLO at least, or this file isn't CRP at all
      if (!steps.some((st) => st.kind === "pkt")) fails.push("no pkt rows");
      const d = adapterDiffers(steps, frameRuns.get(name));
      if (d) fails.push(d);
    }
    nRuns++;
    if (fails.length) nFail++;
    const gapText = gaps.map((g) => `${g.kind} ${g.n}`).join(", ") || "no gaps";
    console.log(`${fails.length ? "FAIL" : "PASS"}  ${name.padEnd(28)} ${String(samples.length).padStart(6)} samples, ${gapText}`);
    for (const f of fails.slice(0, 6)) console.log(`      - ${f}`);
  }
}
// both files hold the same runs, in the same order, and every run has its expected outputs
const same = names.packets.join("|") === names.frames.join("|");
const orphans = [...statsWant.keys()].filter((n) => !names.packets.includes(n));
if (!same) console.log("FAIL  the two vector files hold different runs");
if (orphans.length) console.log(`FAIL  stats rows without a replay: ${orphans.join(", ")}`);
// make_fake_pod.py writes fake_pod_runs.csv last, and only when every scenario ran: without it the
// vectors are a killed or crashed run's leftovers, and a short set would pass here
const runsFile = path.join(DATA, "fake_pod_runs.csv");
const whole = existsSync(runsFile) &&
  readFileSync(runsFile, "utf8").split(/\r?\n/).filter((l) => l.length).slice(1).join("|") === names.packets.join("|");
if (!whole) console.log("FAIL  the vectors aren't a finished make_fake_pod.py run's (no fake_pod_runs.csv, or other runs): run it again");
console.log(`\n${nRuns - nFail}/${nRuns} replays PASS (JS PodStream vs Python podlink: ${names.packets.length} runs x packets and frames)`);
process.exit(nFail || !same || orphans.length || !whole ? 1 : 0);
