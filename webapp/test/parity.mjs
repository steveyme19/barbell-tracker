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
import { SessionTracker, formatTime, dropRackMoves, closedSetReps } from "../sets.js";
import { fitProfile } from "../lv.js";
import { fitCalibration } from "../calib.js";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const PYTHON = process.env.PYTHON || (process.platform === "win32" ? "python" : "python3");
const ROM_TOL = 0.001;
const MCV_TOL = 0.005;
// make_fake_bench.py's GUARDS (D43): [scenario, seed, per-rep MCV tolerance, set mean tolerance, ROM tolerance (m)
// or none]. Each fails when the rule beside it is reverted (python make_fake_bench.py --list says what each is)
const BENCH_GUARDS = [
  ["paused_c100", 1, 0.05, 0.03],            // issue 1: rep 1 read -48% (the bench high-pass, the outer holds)
  ["paused_single", 1, 0.05, 0.05],          // the bench high-pass (TA_SIGMA_LIFT): no card without it
  ["heavy_paused_triple", 1, 0.05, 0.03],    // the same: 1 of 3
  ["drift_working_r32", 1, 0.05, 0.03],      // TA_HOLD_V between reps: 4 of 6
  // issue 4: reps 1-4 read -9 to -14%. 7%: rep 1 reads -5%, the lockout after it turned away by TA_HOLD_V (its run
  // goes on into the next descent) and anchored on one coarse sample, the hold's tremble in it (D43 Open)
  ["tng_working_r32_pause", 1, 0.07, 0.03],
  ["tng_heavy_triple", 1, 0.05, 0.03],       // the start rule between reps: -15%
  ["single_first8", 1, 0.05, 0.05, 0.02],    // the outer exemption: -16%, ROM +7 cm
  ["paused_single_slowstart", 1, 0.05, 0.05, 0.03],   // the first hold's start rule: -12%, ROM +12 cm
  ["heavy_paused_triple_slowstart", 1, 0.05, 0.03],   // the same: rep 1 -30%
  ["bounce_24g", 1, 0.12, 0.07],             // impacts, not jolts, close the set: an empty card (rep 1 reads -10%)
  ["rerack_fumble_5cm", 2, 0.05, 0.03],      // the rack cut: 4 of 5
  ["tng_light_r44_nopause", 1, 0.05, 0.03],  // the rack cut (live, below)
  ["unrack6_soft", 1, 0.05, 0.03],           // the hooks rules: 6 for 5
  ["pod_lift_10s", 1, 0.05, 0.03],           // the closed-set clip: 4 of 5
  // controls: fast touch-and-go reps read up to 10% low (the end walk after a sharp lockout, D43 Open)
  ["tng_light_r32_pause", 1, 0.12, 0.07],
  ["bounce_16g", 1, 0.12, 0.07],
];

const defaults = [
  "analysis/data/fake_reps_pause.csv",
  "analysis/data/fake_reps_nopause.csv",
  "analysis/data/fake_reps_drift.csv",
  "analysis/data/fake_bench_grinder.csv",
  "analysis/data/fake_bench_sticking.csv",   // 2 light, 3 sticking, 6 on the rule's edges: known answers (5.3)
  "analysis/data/fake_bench.csv",
  "analysis/data/fake_squat.csv",
  "analysis/data/fake_deadlift.csv",
  "analysis/data/reps_pause.zip",
  "analysis/data/reps_nopause.zip",
  // the first loaded-bar session (2026-10-31, the pod on the sleeve end): no still between reps,
  // turnaround anchors on the gyro's vertical (D40); the ham curl stays on the old path
  "analysis/data/gym-2026-10-31/session-2026-10-31-140809/bar-squat-2026-10-31-1408-set01.csv",
  "analysis/data/gym-2026-10-31/session-2026-10-31-141021/bar-squat-2026-10-31-1410-set01.csv",
  "analysis/data/gym-2026-10-31/session-2026-10-31-141346/bar-squat-2026-10-31-1413-set01.csv",
  // the deadlift also ended 1.4 s after its last set-down (End tapped, no still yet): the last rep must
  // stay anchored (D41's open-end rule once dropped it: 4 reps)
  ["analysis/data/gym-2026-10-31/session-2026-10-31-142804/bar-deadlift-2026-10-31-1428-set01.csv", "deadlift", { cuts: [30.0], closed: { 30: 5 } }],
  "analysis/data/gym-2026-10-31/session-2026-10-31-144712/bar-machine-2026-10-31-1447-set01-ham-curl.csv",
  "analysis/data/fake_motion_squat_tng.csv",   // exact physics at the pod's rate: the same failure, known answer (D40)
  "analysis/data/fake_motion_deadlift_pins.csv",   // the sleeve spinning through every rest: the quasi-stills hold it (D41)
  "analysis/data/fake_motion_deadlift_creep.csv",  // slow floor breaks and hitches, rep 1 out of a still (D41)
  "analysis/data/fake_motion_squat_breath.csv",    // breaths and eased-down lockouts between squats (D43)
  "analysis/data/fake_motion_deadlift_soft.csv",   // set-downs the gyro doesn't confirm still land the bar (D43)
  // make_fake_bench.py's guard set (D43), calibrated: paused, racked and bounced bench at the pod's rate
  ...BENCH_GUARDS.map(([name, seed]) => `analysis/data/fake_bench_${name}_s${seed}_cal.csv`),
  ["analysis/data/fake_session_squat.csv", "squat"],     // a whole session as one analysis: rests, guards
  ["analysis/data/fake_session_squat.csv", "machine"],
  ["analysis/data/gym-2026-10-31/session-2026-10-31-142804/bar-deadlift-2026-10-31-1428-set01.csv", "free"],   // no rack cut (TA_RACKED)
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
  .map((f) => (Array.isArray(f) ? [path.resolve(ROOT, f[0]), f[1], f[2] ?? {}] : [path.resolve(ROOT, f), null, {}]))
  .filter(([f]) => {
    if (existsSync(f)) return true;
    console.log(`skip (not found): ${path.relative(ROOT, f)}`);
    return false;
  });

let failures = 0;
for (const [file, forced, opts] of files) {
  const buf = readFileSync(file);
  const rec = await parseRecording(file, buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength));
  const lift = forced ?? liftFromName(path.basename(file));   // bench / squat / deadlift in the name, else phone
  const S = { ...SETTINGS, LIFT: lift };
  for (const frac of [null, 0.45, 0.7, ...(opts.cuts ?? []).map((sec) => ({ sec }))]) {
  const until = frac === null ? null : frac.sec ?? frac * (rec.t[rec.t.length - 1] - rec.t[0]);
  let n = rec.t.length;
  if (until !== null) n = rec.t.findIndex((x) => x - rec.t[0] >= until);
  const name = path.relative(ROOT, file) + ` [${lift}]` + (until === null ? "" : ` (first ${until.toFixed(1)} s)`);
  const imu = Object.fromEntries(["ax", "ay", "az", "gx", "gy", "gz"].map((k) => [k, rec[k].slice(0, n)]));
  const js = analyze(rec.t.slice(0, n), magnitude(imu.ax, imu.ay, imu.az), S, imu);
  const pyArgs = [path.join(ROOT, "analysis", "reps.py"), file, "--json", `--lift=${lift}`];
  if (until !== null) pyArgs.push(`--until=${until}`);
  const pyOut = execFileSync(PYTHON, pyArgs, { cwd: path.join(ROOT, "analysis"), encoding: "utf8", maxBuffer: 1 << 26 });
  let py;
  try { py = JSON.parse(pyOut); } catch { py = { error: `Python's JSON doesn't parse (a NaN?): ${pyOut.slice(0, 120)}` }; }

  const problems = [];
  if (py.error) problems.push(py.error);
  else if (js.error) problems.push(`JS error: ${js.error}`);
  else if (js.reps.some((r) => !(r.dur > 0) || !Number.isFinite(r.mcv) || !Number.isFinite(r.rom))) {
    problems.push("a JS rep with no duration or a non-finite MCV/ROM");
  }
  else if (until !== null && opts.closed?.[until] !== undefined && closedSetReps(js.reps, js.lift).length !== opts.closed[until]) {
    problems.push(`known answer: the set closed here has ${opts.closed[until]} reps, JS ${closedSetReps(js.reps, js.lift).length}`);
  }
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
      // the sticking point (analytics.md section 4): both none, or the same place and speed
      if (!r.sticking !== !p.sticking || (r.sticking && (Math.abs(r.sticking.pos - p.sticking.pos) > 0.01 ||
          Math.abs(r.sticking.v - p.sticking.v) > MCV_TOL))) {
        problems.push(`rep ${k + 1} sticking JS ${JSON.stringify(r.sticking)} vs Python ${JSON.stringify(p.sticking)}`);
      }
    });
    // the known answer: fake_bench_sticking's dips (make_fake_reps.py sticking)
    const truthFile = until === null && path.basename(file) === "fake_bench_sticking.csv" && file.replace(/\.csv$/, "_truth.json");
    if (truthFile && existsSync(truthFile)) {
      const truth = JSON.parse(readFileSync(truthFile, "utf8"));
      if (js.reps.length !== truth.length) problems.push(`known answer: ${js.reps.length} reps, ${truth.length} made`);
      js.reps.forEach((r, k) => {
        const want = truth[k];
        if ((want === null) !== !r.sticking || (want !== null && Math.abs(r.sticking.pos - want) > 0.03)) {
          problems.push(`known answer: rep ${k + 1} sticking ${r.sticking ? r.sticking.pos.toFixed(3) : "none"} vs true ${want === null ? "none" : want.toFixed(3)}`);
        }
      });
    }
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
// 0.01 s), same rep count, reps within the usual tolerance, same RPE estimate. A third entry
// is the known answer (reps per set), a fourth each rep's true MCV (within MCV_TRUTH): both
// sides sharing a bug is what they catch (D31, D32). A fifth entry overrides the tolerance:
// { tol: per rep, mean: the set's mean MCV } (relative).
const MCV_TRUTH = 0.05;               // relative
const FAKE_SESSION_MCV = [0.8, 0.85, 0.9, 1.0, 1.15].map((up) => 0.55 / up);   // make_fake_session.py: ROM / UPS
const sessions = [
  // a 0.25 s hold at lockout after every rep, too short for a still: swallowed into the rep, 7 of
  // these 20 reps read 5 to 24% low before D32
  ["analysis/data/fake_session_squat.csv", ["squat"], [5, 5, 5, 5], Array(4).fill(FAKE_SESSION_MCV)],
  ["analysis/data/fake_session_squat.csv", ["machine"]],      // same motion as a weight-stack machine (D19)
  ["analysis/data/fake_session_joined.csv", ["squat", "phone"]],
  ["analysis/data/fake_session_joined.csv", ["free"]],        // hand-held moves: the gyro says free (D21)
  ["analysis/data/bar-machine-2026-10-17-1334-set01-lat-raise.csv", ["free"]],   // the gyro says machine (D21)
  // the phone picked up off the bar 4 s after the last rerack, End tapped while it moves (D31)
  ["analysis/data/fake_session_squat_handled.csv", ["squat"], [5, 5, 5, 5], Array(4).fill(FAKE_SESSION_MCV)],
  // the real pod, hand reps, then the pod handled before the set closed: a 2 m "rep 6" (D31)
  ["analysis/data/bar-deadlift-2026-10-23-1857-set01.csv", ["deadlift"], [5]],
  // 2026-10-31 at the gym (D40). Truth: the video, each rep's plate height synced to the pod (r 0.999,
  // ROM +-2 cm, MCV +-4%); squats read ~3% high (the pod's uncalibrated scale or the video's). The
  // deadlift has no video: the pod's own forensics (calibrated, floor to floor); its rep 1 read +31% from
  // the 6 cm hitch off the floor until TA_FLOOR_REACH (D41). The ham curl: today's numbers
  // make_fake_motion.py squat_tng: 6 touch-and-go squats at 208 Hz with a walkout, the sway, sleeve roll, the
  // pod's roll-dependent offset and a ringing rerack, exact truth (the old code found 2 of them)
  ["analysis/data/fake_motion_squat_tng.csv", ["squat"], [6], [[0.8714, 0.8400, 0.7412, 0.5895, 0.5182, 0.4000]], { tol: 0.08, mean: 0.05 }],
  // make_fake_motion.py deadlift_pins: 10 dead-stop pulls from the pins at 208 Hz, the sleeve spinning 21 to 25 deg/s
  // through every rest and the gyro 1.5% fast: only the quasi-stills hold the attitude (QS off or BAR_AXIS wrong: 7 reps)
  ["analysis/data/fake_motion_deadlift_pins.csv", ["deadlift"], [10], [[0.4429, 0.4133, 0.3827, 0.3488, 0.3261, 0.3061, 0.2913, 0.2661, 0.2544, 0.2417]], { tol: 0.08, mean: 0.05 }],
  // make_fake_motion.py deadlift_creep: 6 floor pulls at 208 Hz (accel calibrated): four creep off the floor
  // (1 to 2.6 cm at 3.4 to 5.4 cm/s, |a| under 0.5: flat to TA_FLAT_WIN), two with a 6 cm hitch; rep 1 out of a
  // 5.5 s still, the others after 1.5 s hands-on floor rests and a ringing set-down. Exact truth, liftoff to lockout
  ["analysis/data/fake_motion_deadlift_creep.csv", ["deadlift"], [6], [[0.4194, 0.3732, 0.4370, 0.3636, 0.3400, 0.3537]], { tol: 0.05, mean: 0.03 }],
  ["analysis/data/gym-2026-10-31/session-2026-10-31-140809/bar-squat-2026-10-31-1408-set01.csv", ["squat"], [8],
    [[0.83, 0.87, 0.87, 0.87, 0.92, 0.81, 0.87, 0.84]], { tol: 0.08, mean: 0.05 }],
  ["analysis/data/gym-2026-10-31/session-2026-10-31-141021/bar-squat-2026-10-31-1410-set01.csv", ["squat"], [6],
    [[0.722, 0.696, 0.639, 0.677, 0.715, 0.626]], { tol: 0.08, mean: 0.05 }],
  ["analysis/data/gym-2026-10-31/session-2026-10-31-141346/bar-squat-2026-10-31-1413-set01.csv", ["squat"], [3],
    [[0.403, 0.318, 0.276]], { tol: 0.08, mean: 0.05 }],
  ["analysis/data/gym-2026-10-31/session-2026-10-31-142804/bar-deadlift-2026-10-31-1428-set01.csv", ["deadlift"], [5],
    [[0.501, 0.552, 0.510, 0.543, 0.504]], { tol: 0.05, mean: 0.03 }],
  // the same set as a nameless free lift: floor contacts between reps aren't a rerack (TA_RACKED)
  ["analysis/data/gym-2026-10-31/session-2026-10-31-142804/bar-deadlift-2026-10-31-1428-set01.csv", ["free"], [5]],
  ["analysis/data/gym-2026-10-31/session-2026-10-31-144712/bar-machine-2026-10-31-1447-set01-ham-curl.csv", ["machine"], [10],
    [[0.378, 0.385, 0.331, 0.372, 0.352, 0.343, 0.320, 0.335, 0.318, 0.205]], { tol: 0.01 }],
  // make_fake_motion.py squat_breath (D43): 2.5 and 3 s breaths at lockout and two lockouts eased down out of
  // (8 and 6 cm/s) between squats, which D43's lockout rules mustn't change (they read the same before them)
  ["analysis/data/fake_motion_squat_breath.csv", ["squat"], [5], [[0.7647, 0.7111, 0.6500, 0.5565, 0.4500]], { tol: 0.08, mean: 0.05 }],
  // ... deadlift_soft: deadlift_creep with every set-down but the last barely turning the bar (gyro ring x0.15):
  // a floor lift lands on every jolt, not only the impacts (rep 3 read +31% when it didn't)
  ["analysis/data/fake_motion_deadlift_soft.csv", ["deadlift"], [6], [[0.4194, 0.3732, 0.4370, 0.3636, 0.3400, 0.3537]], { tol: 0.05, mean: 0.03 }],
];
// make_fake_bench.py's guards (BENCH_GUARDS), raw and calibrated, against each file's own _truth.json
for (const [name, seed, tol, mean, rom] of BENCH_GUARDS) {
  for (const sensor of ["raw", "cal"]) {
    const rel = `analysis/data/fake_bench_${name}_s${seed}_${sensor}.csv`;
    const tj = path.resolve(ROOT, rel.replace(/\.csv$/, "_truth.json"));
    const reps = existsSync(tj) ? JSON.parse(readFileSync(tj, "utf8")).reps : null;
    sessions.push(reps ? [rel, ["bench"], [reps.length], [reps.map((r) => r.mcv)], { tol, mean, rom, roms: [reps.map((r) => r.rom)] }] : [rel, ["bench"]]);
  }
}
for (const [rel, lifts, expect, truth, opt] of process.argv.length > 2 ? [] : sessions) {
  const file = path.resolve(ROOT, rel);
  if (!existsSync(file)) { console.log(`skip (not found): ${rel}`); continue; }
  const buf = readFileSync(file);
  const rec = await parseRecording(file, buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength));
  const drops = [];
  const tracker = new SessionTracker({ liftFor: (k) => lifts[Math.min(k, lifts.length - 1)], onDrop: (d) => drops.push(d) });
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
  // what isn't a set still hands its raw samples to the app (P2): each drop non-empty, in time
  // order, and clear of every set's reps
  for (const d of drops) {
    if (d.threw) problems.push(`analyze() threw in the tracker: ${d.error}`);
    if (!d.samples.length || d.samples.length !== d.tAbs.length) problems.push(`a drop with ${d.samples.length} samples, ${d.tAbs.length} times`);
    else if (d.tAbs.some((t, i) => i && t <= d.tAbs[i - 1])) problems.push("a drop's times go backwards");
    else if (js.some((s) => d.tAbs[0] < s.endT && d.tAbs.at(-1) > s.startT)) problems.push(`a drop at ${d.tAbs[0].toFixed(1)} s overlaps a set's reps`);
  }
  // make_fake_session.py: the plate bumps come within SET_GAP of each rerack, so they join that set;
  // the one segment with no reps is the unrack, step and rerack after set 2
  if (rel.endsWith("fake_session_squat.csv") && lifts[0] === "squat" && drops.length !== 1) {
    problems.push(`known answer: 1 no-rep rack walk dropped with its samples, got ${drops.length}`);
  }
  if (expect && JSON.stringify(js.map((s) => s.reps.length)) !== JSON.stringify(expect)) {
    problems.push(`known answer: reps per set ${JSON.stringify(expect)}, got ${JSON.stringify(js.map((s) => s.reps.length))}`);
  }
  let worstTruth = 0;
  if (truth) js.forEach((s, k) => s.reps.forEach((r, j) => {
    const want = truth[k]?.[j];
    if (want === undefined) return;
    const err = (r.mcv - want) / want;
    if (Math.abs(err) > Math.abs(worstTruth)) worstTruth = err;
    if (Math.abs(err) > (opt?.tol ?? MCV_TRUTH)) problems.push(`known answer: set ${k + 1} rep ${j + 1} MCV ${r.mcv.toFixed(3)} vs true ${want.toFixed(3)} (${(err * 100).toFixed(1)}%)`);
  }));
  if (opt?.rom) js.forEach((s, k) => s.reps.forEach((r, j) => {
    const want = opt.roms[k]?.[j];
    if (want !== undefined && Math.abs(r.rom - want) > opt.rom) problems.push(`known answer: set ${k + 1} rep ${j + 1} ROM ${r.rom.toFixed(3)} vs true ${want.toFixed(3)} m`);
  }));
  if (truth && opt?.mean) js.forEach((s, k) => {
    if (!truth[k]) return;                // a set the truth doesn't have: the count check reports it
    const want = truth[k].reduce((p, x) => p + x, 0) / truth[k].length;
    const got = s.reps.reduce((p, r) => p + r.mcv, 0) / s.reps.length;
    if (Math.abs(got / want - 1) > opt.mean) problems.push(`known answer: set ${k + 1} mean MCV ${got.toFixed(3)} vs true ${want.toFixed(3)}`);
  });
  const truthNote = truth ? `, MCV vs truth worst ${(worstTruth * 100).toFixed(1)}%` : "";
  if (problems.length) { failures++; console.log(`FAIL session ${rel}\n  ${problems.join("\n  ")}`); }
  else console.log(`PASS session ${rel} [${lifts}]: ${js.length} sets (${js.map((s) => s.lift).join(", ")}), rests ${js.slice(1).map((s) => formatTime(s.restBefore)).join(" ")}${truthNote}, ${drops.length} dropped (samples kept)`);
}

// ---------- live: the 2026-10-31 gym sets as the app sees them mid-set (D40) ----------
// analyze() on a growing buffer every 0.25 s plus the rack filter, like app.js's live view. The
// count may dip at most its file's allowance (dips: a rep re-anchored as the set goes on; 0 unless
// it's known) and never shows more than the set's final count (ahead: the ham curl's old-path flicker
// shows one ahead once). mcv: each rep's MCV as the app shows it while it's the newest rep (the value
// it shows most often) within LIVE_MCV of the truth; with lockouts (s from the file's start), at most
// offMax passes from 0.3 s after a rep's true lockout on that show the newest rep over LIVE_OFF off
// (D41: the open last floor rest's anchor, whose error shows at first and is fixed by the next rest).
const LIVE_MCV = 0.08, LIVE_OFF = 0.10;
const live = [
  ["analysis/data/fake_motion_squat_tng.csv", "squat", 6],
  ["analysis/data/gym-2026-10-31/session-2026-10-31-140809/bar-squat-2026-10-31-1408-set01.csv", "squat", 8],
  ["analysis/data/gym-2026-10-31/session-2026-10-31-141021/bar-squat-2026-10-31-1410-set01.csv", "squat", 6, { dips: 1 }],
  ["analysis/data/gym-2026-10-31/session-2026-10-31-141346/bar-squat-2026-10-31-1413-set01.csv", "squat", 3],
  ["analysis/data/gym-2026-10-31/session-2026-10-31-142804/bar-deadlift-2026-10-31-1428-set01.csv", "deadlift", 5, { mcv: [0.501, 0.552, 0.510, 0.543, 0.504],
    // offMax 1: at 14.50 s drop_rack_moves takes rep 1 for handling while rep 2's first cm show (the count
    // stays 1, so the dips don't see it): one pass shows rep 2's start alone
    lockouts: [11.271, 14.968, 18.632, 22.029, 27.361], offMax: 1 }],
  ["analysis/data/gym-2026-10-31/session-2026-10-31-144712/bar-machine-2026-10-31-1447-set01-ham-curl.csv", "machine", 10, { ahead: 1, dips: 1 }],
  ["analysis/data/fake_motion_deadlift_pins.csv", "deadlift", 10],
  // the open last floor rest on all of it (D41): anchored on its last 0.5 s, 17 passes showed a creeping break's rep
  // +30 to +38% off (6 now: rep 3 +31% until the next rest anchors, its start on the threshold, V_STOP_FRAC)
  ["analysis/data/fake_motion_deadlift_creep.csv", "deadlift", 6, { mcv: [0.4194, 0.3732, 0.4370, 0.3636, 0.3400, 0.3537],
    lockouts: [9.74, 13.96, 17.95, 22.18, 26.48, 30.75], offMax: 8 }],
  ["analysis/data/fake_motion_squat_breath.csv", "squat", 5],
  // dips 1: at 25.25 s, 5 to 4 for a pass, and the same before D43 (its soft set-downs aren't impacts)
  ["analysis/data/fake_motion_deadlift_soft.csv", "deadlift", 6, { mcv: [0.4194, 0.3732, 0.4370, 0.3636, 0.3400, 0.3537],
    lockouts: [9.74, 13.96, 17.95, 22.18, 26.48, 30.75], offMax: 8, dips: 1 }],
];
// make_fake_bench.py's guards live (D43), calibrated, none allowed a count ahead. The rack cut's own guard,
// tng_light_r44_nopause, raw and calibrated too (without the cut: a dip at the rerack, 24.75 s). The slow-start
// sets dip once at 7.25 s, in the eased-into descent: the same without the first-hold rule (before D43: twice)
const LIVE_DIPS = { paused_single_slowstart: 1, heavy_paused_triple_slowstart: 1 };
for (const [name, seed] of BENCH_GUARDS) {
  if (name === "pod_lift_10s") continue;   // known (D43 Open): the off-bar rest sets the still limits and g (D20)
  for (const sensor of name === "tng_light_r44_nopause" ? ["raw", "cal"] : ["cal"]) {
    const rel = `analysis/data/fake_bench_${name}_s${seed}_${sensor}.csv`;
    const tj = path.resolve(ROOT, rel.replace(/\.csv$/, "_truth.json"));
    if (existsSync(tj)) live.push([rel, "bench", JSON.parse(readFileSync(tj, "utf8")).reps.length, { dips: LIVE_DIPS[name] ?? 0 }]);
  }
}
for (const [rel, lift, final, { ahead = 0, dips: allowed = 0, mcv: truthMcv = null, lockouts = null, offMax = 0 } = {}] of process.argv.length > 2 ? [] : live) {
  const file = path.resolve(ROOT, rel);
  if (!existsSync(file)) { console.log(`skip (not found): ${rel}`); continue; }
  const buf = readFileSync(file);
  const rec = await parseRecording(file, buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength));
  const counts = [];
  const shown = [];                     // shown[k]: the MCVs (1 mm/s) shown while rep k was the newest
  let off = 0;                          // passes after the newest rep's lockout showing it over LIVE_OFF off
  for (let T = rec.t[0] + 3; T <= rec.t[rec.t.length - 1] + 1e-9; T += 0.25) {
    const n = rec.t.findIndex((x) => x > T);
    const k = n < 0 ? rec.t.length : n;
    const imu = Object.fromEntries(["ax", "ay", "az", "gx", "gy", "gz"].map((c) => [c, rec[c].slice(0, k)]));
    const r = analyze(rec.t.slice(0, k), magnitude(imu.ax, imu.ay, imu.az), { ...SETTINGS, LIFT: lift }, imu);
    const rr = r.error ? [] : dropRackMoves(r.reps, r.lift);
    counts.push(rr.length);
    if (rr.length) (shown[rr.length - 1] ??= []).push(Math.round(rr[rr.length - 1].mcv * 1000) / 1000);
    const j = rr.length - 1;
    if (lockouts && j >= 0 && j < lockouts.length && T - rec.t[0] >= lockouts[j] + 0.3 && Math.abs(rr[j].mcv / truthMcv[j] - 1) > LIVE_OFF) off++;
  }
  const dips = counts.filter((c, i) => i && c < counts[i - 1]).length;
  const most = Math.max(...counts);
  const mode = (xs) => xs.reduce((best, x) => (xs.filter((y) => y === x).length > xs.filter((y) => y === best).length ? x : best), xs[0]);
  const held = truthMcv ? truthMcv.map((want, k) => (shown[k] ? mode(shown[k]) / want - 1 : null)) : [];
  const offMcv = held.filter((e) => e === null || Math.abs(e) > LIVE_MCV);
  const bad = dips > allowed || most > final + ahead || counts.at(-1) !== final || offMcv.length > 0 || (lockouts && off > offMax);
  if (bad) failures++;
  const heldNote = (truthMcv ? `, shown MCV vs truth ${held.map((e) => (e === null ? "none" : `${(e * 100).toFixed(1)}%`)).join(" ")}` : "") +
    (lockouts ? `, ${off} passes after a lockout showing it over ${LIVE_OFF * 100}% off (allowed ${offMax})` : "");
  console.log(`${bad ? "FAIL" : "PASS"} live ${path.basename(rel)} [${lift}]: ${counts.length} passes, final ${counts.at(-1)} (want ${final}), most ${most}, dips ${dips} (allowed ${allowed})${heldNote}`);
}

// ---------- the vertical (D40): its gate, JS = Python per sample, and no chaos ----------
// Browser 60 Hz and phone 402 Hz bar lifts stay on |a| - g (exactly); pod-rate bar lifts get the vertical.
// On the pod files the vertical must agree JS vs Python to 1e-9 m/s^2 per sample, and Python's own must
// move less than 1e-9 under a 1e-12 nudge of the accel: Madgwick's normalized pull made it chaotic
// (0.07 m/s^2 from 1e-15) until SN_FLOOR, and a rep tolerance can't see a chaotic filter under 1 mm.
const gate = [
  ["analysis/data/bar-squat-2026-10-16-2131.csv", false],           // phone native sensors, 402 Hz
  ["analysis/data/bar-squat-2026-10-16-2134-browser.csv", false],   // browser, 60 Hz
  ["analysis/data/gym-2026-10-31/session-2026-10-31-140809/bar-squat-2026-10-31-1408-set01.csv", true],
  ["analysis/data/gym-2026-10-31/session-2026-10-31-141021/bar-squat-2026-10-31-1410-set01.csv", true],
  ["analysis/data/gym-2026-10-31/session-2026-10-31-141346/bar-squat-2026-10-31-1413-set01.csv", true],
  ["analysis/data/gym-2026-10-31/session-2026-10-31-142804/bar-deadlift-2026-10-31-1428-set01.csv", true],
  ["analysis/data/fake_motion_squat_tng.csv", true],
  ["analysis/data/fake_motion_deadlift_pins.csv", true],
  ["analysis/data/fake_motion_deadlift_creep.csv", true],
  ["analysis/data/fake_motion_squat_breath.csv", true],
  ["analysis/data/fake_motion_deadlift_soft.csv", true],
  ["analysis/data/fake_bench_bounce_24g_s1_cal.csv", true],
  ["analysis/data/fake_bench_heavy_paused_triple_s1_raw.csv", true],
];
const PY_VERT = [
  "import sys, json, numpy as np, reps",
  "from pathlib import Path",
  "p = Path(sys.argv[1])",
  "t, a = reps.load_phyphox(p, reps.AXIS)",
  "acc, gy = reps.load_imu(p, len(t))",
  "lift = reps.lift_from_name(p.name)",
  "r = reps.analyze(t, a, lift, acc, gy)",
  "on_a = bool(np.array_equal(r['a'], a - r['g']))",
  "nudge = 0.0",
  "if not on_a:",
  "    r2 = reps.analyze(t, a, lift, acc + np.random.default_rng(1).normal(0, 1e-12, acc.shape), gy)",
  "    nudge = float(np.max(np.abs(r2['a'] - r['a'])))",
  "print(json.dumps({'on_a': on_a, 'nudge': nudge, 'a': [float(x) for x in r['a']]}))",
].join("\n");
for (const [rel, vertical] of process.argv.length > 2 ? [] : gate) {
  const file = path.resolve(ROOT, rel);
  if (!existsSync(file)) { console.log(`skip (not found): ${rel}`); continue; }
  const buf = readFileSync(file);
  const rec = await parseRecording(file, buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength));
  const imu = Object.fromEntries(["ax", "ay", "az", "gx", "gy", "gz"].map((k) => [k, rec[k]]));
  const aRaw = magnitude(imu.ax, imu.ay, imu.az);
  const r = analyze(rec.t, aRaw, { ...SETTINGS, LIFT: liftFromName(path.basename(file)) }, imu);
  const jsOnA = !r.error && r.a.every((x, i) => x === aRaw[i] - r.g);
  const py = JSON.parse(execFileSync(PYTHON, ["-c", PY_VERT, file], { cwd: path.join(ROOT, "analysis"), encoding: "utf8", maxBuffer: 1 << 26 }));
  let worst = 0;
  if (!r.error && py.a.length === r.a.length) py.a.forEach((x, i) => { worst = Math.max(worst, Math.abs(x - r.a[i])); });
  else worst = Infinity;
  const problems = [];
  if (jsOnA === vertical || py.on_a === vertical) problems.push(`want ${vertical ? "the vertical" : "|a| - g"}: JS ${jsOnA ? "|a| - g" : "vertical"}, Python ${py.on_a ? "|a| - g" : "vertical"}`);
  if (vertical && worst > 1e-9) problems.push(`the vertical JS vs Python: worst ${worst.toExponential(1)} m/s^2 (want <= 1e-9)`);
  if (vertical && !(py.nudge <= 1e-9)) problems.push(`Python's vertical moved ${py.nudge.toExponential(1)} under a 1e-12 nudge (chaotic: SN_FLOOR?)`);
  if (problems.length) { failures++; console.log(`FAIL vertical ${path.basename(rel)}\n  ${problems.join("\n  ")}`); }
  else console.log(`PASS vertical ${path.basename(rel)} (${r.fs.toFixed(0)} Hz): ${vertical ? `the vertical, JS vs Python worst ${worst.toExponential(1)}, a 1e-12 nudge moves it ${py.nudge.toExponential(1)}` : "|a| - g, both"}`);
}

// ---------- the rack filter's ends (analysis/sets.py drop_rack_moves vs webapp/sets.js) ----------
// Known answers on made-up ROMs (m): handling (too big) goes first, then rack bumps (too small).
const rackCases = [
  ["squat", [0.55, 0.55, 0.56, 0.54, 1.98], [0.55, 0.55, 0.56, 0.54]],   // a pickup after the set
  ["deadlift", [0.55, 1.98], [0.55]],              // a heavy single, then handling: the single stays
  ["squat", [1.5], []],                            // nothing but handling
  ["bench", [0.06, 0.42, 0.41, 0.85], [0.42, 0.41]],   // unrack bump, then a 0.85 m pickup (cap 0.8)
  ["free", [0.9, 0.5, 0.5], [0.9, 0.5, 0.5]],      // 0.9 < 2 x 0.5: a free lift keeps it
  ["free", [0.5, 0.5, 1.2], [0.5, 0.5]],           // 1.2 > 2 x 0.5: handling
  ["phone", [0.3, 0.05, 0.3], [0.3, 0.05, 0.3]],   // phone tests: no floor, only the middle anyway
  // a ~0 m unrack bump must not set the scale for the handling test (night review)
  ["squat", [0.0, 0.549], [0.549]],                // a heavy single after the unrack
  ["squat", [0.549, 0.03], [0.549]],               // ... and before the rerack bump
  ["bench", [0.40, 0.07], [0.40]],
  ["squat", [0.03, 0.55, 0.03], [0.55]],
  ["squat", [0.055, 0.55], [0.55]],
  ["free", [0.02, 0.5, 1.2], [0.5]],               // the bump goes, the 1.2 m handling too
  ["phone", [0.0, 0.5], [0.5]],                    // phone: no floor, but a 0 m "rep" still sets no scale
];
// closed sets (closed_set_reps / closedSetReps): an unanchored rep after anchored ones goes; with
// none anchored they all stay, flagged (hands on the racked bar: the end's quiet wasn't a still)
const closedCases = [
  ["squat", [[0.55, 0], [0.55, 0], [0.55, 0], [0.50, 1]], [0.55, 0.55, 0.55]],
  ["squat", [[0.55, 1], [0.55, 1], [0.55, 1]], [0.55, 0.55, 0.55]],
];
{
  const reps = (roms) => roms.map((rom) => ({ rom }));
  const py = JSON.parse(execFileSync(PYTHON, ["-c", [
    "import json, sys", "sys.path.insert(0, sys.argv[1])", "import sets",
    "cases = json.loads(sys.argv[2])",
    "print(json.dumps([[r['rom'] for r in sets.drop_rack_moves([{'rom': x} for x in roms], lift)] for lift, roms, _ in cases]))",
  ].join("\n"), path.join(ROOT, "analysis"), JSON.stringify(rackCases)], { cwd: path.join(ROOT, "analysis"), encoding: "utf8" }));
  const bad = rackCases.filter(([lift, roms, want], k) => {
    const js = dropRackMoves(reps(roms), lift).map((r) => r.rom);
    return JSON.stringify(js) !== JSON.stringify(want) || JSON.stringify(py[k]) !== JSON.stringify(want);
  });
  if (bad.length) { failures++; console.log(`FAIL rack filter: ${bad.map(([l, r, w]) => `${l} ${JSON.stringify(r)} -> want ${JSON.stringify(w)}`).join("; ")}`); }
  else console.log(`PASS rack filter: ${rackCases.length} known answers, Python and JS (handling off the ends first, then rack bumps)`);
  const mk = (rs) => rs.map(([rom, u]) => ({ rom, flags: u ? ["no end anchor"] : [] }));
  const pyc = JSON.parse(execFileSync(PYTHON, ["-c", [
    "import json, sys", "sys.path.insert(0, sys.argv[1])", "import sets",
    "cases = json.loads(sys.argv[2])",
    "print(json.dumps([[r['rom'] for r in sets.closed_set_reps([{'rom': x, 'flags': ['no end anchor'] if u else []} for x, u in rs], lift)] for lift, rs, _ in cases]))",
  ].join("\n"), path.join(ROOT, "analysis"), JSON.stringify(closedCases)], { cwd: path.join(ROOT, "analysis"), encoding: "utf8" }));
  const badc = closedCases.filter(([lift, rs, want], k) => JSON.stringify(closedSetReps(mk(rs), lift).map((r) => r.rom)) !== JSON.stringify(want)
    || JSON.stringify(pyc[k]) !== JSON.stringify(want));
  if (badc.length) { failures++; console.log(`FAIL closed sets: ${badc.map(([l, r, w]) => `${l} ${JSON.stringify(r)} -> want ${JSON.stringify(w)}`).join("; ")}`); }
  else console.log(`PASS closed sets: ${closedCases.length} known answers, Python and JS (an unanchored rep after anchored ones goes; none anchored, all stay)`);
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

// ---------- accelerometer calibration (analysis/calib.py vs webapp/calib.js) ----------
// A fake sensor reading 1.5 % high on x, 0.8 % low on z, with offsets, in 6 positions.
const calCases = [
  [[0.12, 0.03, 9.80 * 0.992 - 0.05], [0.12, 0.03, -9.80 * 0.992 - 0.05], [9.80 * 1.015 + 0.12, 0.03, -0.05],
    [-9.80 * 1.015 + 0.12, 0.03, -0.05], [0.12, 9.80 + 0.03, -0.05], [0.12, -9.80 + 0.03, -0.05]],
  [[0.1, 0, 9.8], [0.1, 0, -9.8], [9.8, 0, 0]],                    // faces missing: no fit
];
for (const means of calCases) {
  const js = fitCalibration(means);
  const py = JSON.parse(execFileSync(PYTHON, [path.join(ROOT, "analysis", "calib.py"), JSON.stringify(means), "--json"],
    { cwd: path.join(ROOT, "analysis"), encoding: "utf8" }));
  const close = (a, b) => a.every((x, i) => Math.abs(x - b[i]) < 1e-12);
  const ok = (js === null && py === null) ||
    (js && py && close(js.bias, py.bias) && close(js.scale, py.scale) && Math.abs(js.residual - py.residual) < 1e-12);
  if (!ok) { failures++; console.log(`FAIL calib\n  JS ${JSON.stringify(js)}\n  Py ${JSON.stringify(py)}`); }
  else console.log(`PASS calib: ${js ? `scale ${js.scale.map((s) => ((s - 1) * 100).toFixed(2) + "%").join(" ")}, bias ${js.bias.map((b) => b.toFixed(2)).join(" ")}` : "no fit (faces missing)"}`);
}

process.exit(failures ? 1 : 0);
