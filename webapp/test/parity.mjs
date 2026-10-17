// Parity test: the JS rep algorithm (webapp/reps.js) must match Python (analysis/reps.py).
//
// Usage, from the repo root, with the Python venv active:
//   node webapp/test/parity.mjs                      # default test files
//   node webapp/test/parity.mjs analysis/data/reps_pause.zip
//
// Pass/fail tolerance (roadmap 0.5): ROM within 1 mm, MCV within 0.005 m/s.
// Each file is also cut off partway (45% and 70% of the way in) to test what the live
// app shows mid-set, before the drift fix has a still to finish on.

import { readFileSync, existsSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { parseRecording } from "../fileio.js";
import { analyze, magnitude, liftFromName, SETTINGS } from "../reps.js";
import { SessionTracker, formatTime } from "../sets.js";
import { fitProfile } from "../lv.js";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const PYTHON = process.env.PYTHON || (process.platform === "win32" ? "python" : "python3");
const ROM_TOL = 0.001;
const MCV_TOL = 0.005;

const defaults = [
  "analysis/data/fake_reps_pause.csv",
  "analysis/data/fake_reps_nopause.csv",
  "analysis/data/fake_reps_drift.csv",
  "analysis/data/fake_bench_grinder.csv",
  "analysis/data/fake_bench.csv",
  "analysis/data/fake_squat.csv",
  "analysis/data/fake_deadlift.csv",
  "analysis/data/reps_pause.zip",
  "analysis/data/reps_nopause.zip",
  "analysis/data/bar-2026-10-16-1835.csv",
  "analysis/data/bar-2026-10-16-1844.csv",
  "analysis/data/bar-2026-10-16-1845.csv",
  "analysis/data/bar-2026-10-16-1920.csv",
  "analysis/data/bar-phone-2026-10-16-2123.csv",   // Android app, native sensors, 402 Hz (A2)
  "analysis/data/bar-squat-2026-10-16-2131.csv",   // native squat set, box-sit bottoms (A2 comparison)
  "analysis/data/bar-squat-2026-10-16-2134-browser.csv",   // same squats, browser 60 Hz (A2 comparison)
  "analysis/data/bar-phone-2026-10-16-2206.csv",   // saved by the app's Save CSV (A3)
  "analysis/data/bar-machine-2026-10-17-1334-set01-lat-raise.csv",   // phone on a cable stack (D20)
  // [file, lift]: track it under another name. Here the gyro has to say "machine" (D21).
  ["analysis/data/bar-machine-2026-10-17-1334-set01-lat-raise.csv", "free"],
];
const files = (process.argv.length > 2 ? process.argv.slice(2) : defaults)
  .map((f) => (Array.isArray(f) ? [path.resolve(ROOT, f[0]), f[1]] : [path.resolve(ROOT, f), null]))
  .filter(([f]) => {
    if (existsSync(f)) return true;
    console.log(`skip (not found): ${path.relative(ROOT, f)}`);
    return false;
  });

let failures = 0;
for (const [file, forced] of files) {
  const buf = readFileSync(file);
  const rec = await parseRecording(file, buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength));
  const lift = forced ?? liftFromName(path.basename(file));   // bench / squat / deadlift in the name, else phone
  const S = { ...SETTINGS, LIFT: lift };
  for (const frac of [null, 0.45, 0.7]) {
  const until = frac === null ? null : frac * (rec.t[rec.t.length - 1] - rec.t[0]);
  let n = rec.t.length;
  if (until !== null) n = rec.t.findIndex((x) => x - rec.t[0] >= until);
  const name = path.relative(ROOT, file) + ` [${lift}]` + (until === null ? "" : ` (first ${until.toFixed(1)} s)`);
  const imu = Object.fromEntries(["ax", "ay", "az", "gx", "gy", "gz"].map((k) => [k, rec[k].slice(0, n)]));
  const js = analyze(rec.t.slice(0, n), magnitude(imu.ax, imu.ay, imu.az), S, imu);
  const pyArgs = [path.join(ROOT, "analysis", "reps.py"), file, "--json", `--lift=${lift}`];
  if (until !== null) pyArgs.push(`--until=${until}`);
  const py = JSON.parse(execFileSync(PYTHON, pyArgs, { cwd: path.join(ROOT, "analysis"), encoding: "utf8" }));

  const problems = [];
  if (js.error) problems.push(`JS error: ${js.error}`);
  else if (js.lift !== py.lift) problems.push(`profile JS ${js.lift} vs Python ${py.lift}`);
  else if (!js.mount !== !py.mount || (js.mount && (js.mount.kind !== py.mount.kind ||
      Math.abs(js.mount.rot - py.mount.rot) > 1e-6 || Math.abs(js.mount.side - py.mount.side) > 1e-6))) {
    problems.push(`mount JS ${JSON.stringify(js.mount)} vs Python ${JSON.stringify(py.mount)}`);
  } else if (js.reps.length !== py.reps.length) {
    problems.push(`rep count JS ${js.reps.length} vs Python ${py.reps.length}`);
  } else {
    js.reps.forEach((r, k) => {
      const p = py.reps[k];
      if (Math.abs(r.rom - p.rom) > ROM_TOL) problems.push(`rep ${k + 1} ROM ${r.rom.toFixed(4)} vs ${p.rom.toFixed(4)}`);
      if (Math.abs(r.mcv - p.mcv) > MCV_TOL) problems.push(`rep ${k + 1} MCV ${r.mcv.toFixed(4)} vs ${p.mcv.toFixed(4)}`);
      if (r.leveled !== p.leveled) problems.push(`rep ${k + 1} leveled JS ${r.leveled} vs Python ${p.leveled}`);
      if (r.flags.join() !== p.flags.join()) problems.push(`rep ${k + 1} flags JS [${r.flags}] vs Python [${p.flags}]`);
    });
  }
  if (problems.length) {
    failures++;
    console.log(`FAIL ${name}\n  ${problems.join("\n  ")}`);
  } else {
    const worst = js.reps.reduce((m, r, k) => Math.max(m, Math.abs(r.rom - py.reps[k].rom)), 0);
    const seen = js.lift === lift ? "" : ` -> ${js.lift}`;
    console.log(`PASS ${name}${seen}: ${js.reps.length} reps, worst ROM difference ${(worst * 1000).toFixed(3)} mm`);
  }
  }
}
// ---------- session mode (analysis/sets.py vs webapp/sets.js) ----------
// The JS tracker is fed one sample at a time, like the live app. It has to find the
// same sets as Python working on the whole file: same start/end and rest (within
// 0.01 s), same rep count, reps within the usual tolerance, same RPE estimate.
const sessions = [
  ["analysis/data/fake_session_squat.csv", ["squat"]],
  ["analysis/data/fake_session_squat.csv", ["machine"]],      // same motion as a weight-stack machine (D19)
  ["analysis/data/fake_session_joined.csv", ["squat", "phone"]],
  ["analysis/data/fake_session_joined.csv", ["free"]],        // hand-held moves: the gyro says free (D21)
  ["analysis/data/bar-machine-2026-10-17-1334-set01-lat-raise.csv", ["free"]],   // the gyro says machine (D21)
];
for (const [rel, lifts] of process.argv.length > 2 ? [] : sessions) {
  const file = path.resolve(ROOT, rel);
  if (!existsSync(file)) { console.log(`skip (not found): ${rel}`); continue; }
  const buf = readFileSync(file);
  const rec = await parseRecording(file, buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength));
  const tracker = new SessionTracker({ liftFor: (k) => lifts[Math.min(k, lifts.length - 1)] });
  for (let i = 0; i < rec.t.length; i++) {
    tracker.push({ t: rec.t[i], ax: rec.ax[i], ay: rec.ay[i], az: rec.az[i], gx: rec.gx[i], gy: rec.gy[i], gz: rec.gz[i] });
  }
  tracker.finish();
  const js = tracker.sets;
  const py = JSON.parse(execFileSync(PYTHON, [path.join(ROOT, "analysis", "sets.py"), file, "--json", `--lifts=${lifts}`],
    { cwd: path.join(ROOT, "analysis"), encoding: "utf8", maxBuffer: 1 << 26 }));
  const problems = [];
  if (js.length !== py.length) problems.push(`set count JS ${js.length} vs Python ${py.length}`);
  else js.forEach((s, k) => {
    const p = py[k];
    const near = (x, y) => Math.abs(x - y) <= 0.01;
    if (!near(s.startT, p.start_t) || !near(s.endT, p.end_t)) problems.push(`set ${k + 1} time JS ${s.startT.toFixed(2)}-${s.endT.toFixed(2)} vs Python ${p.start_t.toFixed(2)}-${p.end_t.toFixed(2)}`);
    if ((s.restBefore === null) !== (p.rest_before === null) || (s.restBefore !== null && !near(s.restBefore, p.rest_before))) problems.push(`set ${k + 1} rest JS ${s.restBefore} vs Python ${p.rest_before}`);
    if (s.lift !== p.lift) problems.push(`set ${k + 1} profile JS ${s.lift} vs Python ${p.lift}`);
    if (s.reps.length !== p.reps.length) problems.push(`set ${k + 1} reps JS ${s.reps.length} vs Python ${p.reps.length}`);
    else s.reps.forEach((r, j) => {
      if (Math.abs(r.rom - p.reps[j].rom) > ROM_TOL) problems.push(`set ${k + 1} rep ${j + 1} ROM ${r.rom.toFixed(4)} vs ${p.reps[j].rom.toFixed(4)}`);
      if (Math.abs(r.mcv - p.reps[j].mcv) > MCV_TOL) problems.push(`set ${k + 1} rep ${j + 1} MCV ${r.mcv.toFixed(4)} vs ${p.reps[j].mcv.toFixed(4)}`);
    });
    if (JSON.stringify(s.rpeEst) !== JSON.stringify(p.rpe_est)) problems.push(`set ${k + 1} RPE JS ${s.rpeEst} vs Python ${p.rpe_est}`);
  });
  if (problems.length) { failures++; console.log(`FAIL session ${rel}\n  ${problems.join("\n  ")}`); }
  else console.log(`PASS session ${rel} [${lifts}]: ${js.length} sets (${js.map((s) => s.lift).join(", ")}), rests ${js.slice(1).map((s) => formatTime(s.restBefore)).join(" ")}`);
}

// ---------- load-velocity profile (analysis/lv.py vs webapp/lv.js) ----------
const lvCases = [
  ["squat", [[135, 0.82], [185, 0.66], [225, 0.51], [185, 0.64]]],
  ["bench", [[95, 0.78], [135, 0.55], [155, 0.44]]],
  ["machine", [[100, 0.6], [140, 0.45]]],           // no 1RM velocity: profile only
  ["squat", [[135, 0.8], [135, 0.78]]],             // one load: no fit
  ["deadlift", [[135, 0.9], [145, 0.89]]],          // nearly flat: e1RM too far, not reported
];
for (const [lift, pts] of lvCases) {
  const js = fitProfile(pts, lift);
  const py = JSON.parse(execFileSync(PYTHON, [path.join(ROOT, "analysis", "lv.py"), lift, JSON.stringify(pts), "--json"],
    { cwd: path.join(ROOT, "analysis"), encoding: "utf8" }));
  const near = (x, y) => (x === null && y === null) || (x !== null && y !== null && Math.abs(x - y) < 1e-9);
  const ok = (js === null && py === null) ||
    (js && py && near(js.a, py.a) && near(js.b, py.b) && near(js.r2, py.r2) && near(js.e1rm, py.e1rm) && js.n === py.n);
  if (!ok) { failures++; console.log(`FAIL lv ${lift} ${JSON.stringify(pts)}\n  JS ${JSON.stringify(js)}\n  Py ${JSON.stringify(py)}`); }
  else console.log(`PASS lv ${lift}: ${js ? `e1RM ${js.e1rm ? js.e1rm.toFixed(0) : "--"}, R2 ${js.r2.toFixed(2)}` : "no fit"}`);
}

process.exit(failures ? 1 : 0);
