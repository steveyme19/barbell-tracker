// The pod's calibration in the app (D42): applyPodCalibration (webapp/calib.js) must give exactly what
// calib_pod.apply() gives in Python (P1), and only the pod it was made on gets it.
//
//   node webapp/test/calib_pod.mjs        (PYTHON = the venv's python, an absolute path, as for parity.mjs)
//
// The installed webapp/pod-calibration.json, when there is one, must be one the app can apply.

import { execFileSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { applyPodCalibration, podCalibrationFor, podCalibrationProblem, podHex } from "../calib.js";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const PYTHON = process.env.PYTHON || (process.platform === "win32" ? "python" : "python3");

let ok = true;
const check = (good, msg) => { console.log(`${good ? "PASS" : "FAIL"}  ${msg}`); ok &&= Boolean(good); };

// a calibration like calib_pod.py's, with a gyro bias and scale it hasn't got (y: neither, z: no bias)
const CAL = { pod: 0x41ef151f, accel: { bias: [-0.179, -0.055, 0.031], scale: [1.0046, 0.9981, 1.0123] },
  gyro: { bias: [0.61, null, null], scale: [1.008, null, 1.015] } };
// samples like the pod's: accel up to 4 g, gyro up to 500 deg/s, a fixed pseudo-random sequence
let seed = 7;
const rnd = () => ((seed = (seed * 1103515245 + 12345) % 2147483648) / 2147483648) * 2 - 1;
const samples = Array.from({ length: 200 }, (_, i) => ({ t: i / 208, ax: 40 * rnd(), ay: 40 * rnd(), az: 40 * rnd(),
  gx: 500 * rnd(), gy: 500 * rnd(), gz: 500 * rnd() }));

const js = samples.map((s) => applyPodCalibration(s, CAL));
const PY = [
  "import sys, json, numpy as np, calib_pod",
  "d = json.loads(sys.stdin.read())",
  "acc = np.array([[s['ax'], s['ay'], s['az']] for s in d['samples']])",
  "gyr = np.array([[s['gx'], s['gy'], s['gz']] for s in d['samples']])",
  "a, g = calib_pod.apply(d['cal'], acc, gyr)",
  "print(json.dumps({'a': a.tolist(), 'g': g.tolist()}))",
].join("\n");
const py = JSON.parse(execFileSync(PYTHON, ["-c", PY], { cwd: path.join(ROOT, "analysis"), encoding: "utf8",
  input: JSON.stringify({ cal: CAL, samples }) }));
let same = 0;
js.forEach((s, i) => {
  const want = [...py.a[i], ...py.g[i]];
  if (["ax", "ay", "az", "gx", "gy", "gz"].every((k, j) => s[k] === want[j])) same++;
});
check(same === samples.length, `applyPodCalibration = calib_pod.apply() bit for bit: ${same}/${samples.length} samples`);
check(js[3].gy === samples[3].gy && js[3].t === samples[3].t,
  "a gyro bias and scale it hasn't got leave that axis as it was; t untouched");
check(applyPodCalibration(samples[0], null) === samples[0], "no calibration: the sample as it came");

// only the pod it was made on
check(podCalibrationFor(CAL, 0x41ef151f) === CAL, "the same pod's serial: applied");
check(podCalibrationFor(CAL, 0x12345678) === null, "another pod's serial: not applied");
check(podCalibrationFor({ ...CAL, pod: null }, 0x12345678) !== null, "no serial recorded (made from a recording): applied");
check(podCalibrationFor(null, 0x41ef151f) === null, "none installed: nothing");
check(podHex(0x41ef151f) === "0x41ef151f" && podHex(0x0000beef) === "0x0000beef" && podHex(0xffffffff) === "0xffffffff",
  `serials in hex as podlink.py prints them: ${podHex(0x41ef151f)}, ${podHex(0x0000beef)}, ${podHex(0xffffffff)}`);

// The shape check the app runs on pod-calibration.json before it applies it (podCalibrationProblem): what
// --install writes passes, anything else is refused with a reason (the app says it once, then ignores the file)
// GOOD: what --install writes (all 6 scales); PARTIAL: a gyro axis it hasn't got (the app can take that)
const GOOD = { what: "test", made: "2026-10-31T23:10:00", from: "pod-calibration-2026-10-31-2310.json", pod: 0x41ef151f,
  accel: CAL.accel, gyro: { bias: [0.61, -0.4, 0.25], scale: [1.008, 0.993, 1.015] } };
const PARTIAL = { ...GOOD, gyro: CAL.gyro };
const BAD = {
  "not an object": [1, 2, 3],
  "accel scale 8% low": { ...GOOD, accel: { ...GOOD.accel, scale: [1.0, 0.92, 1.0] } },
  "accel scale 8% high": { ...GOOD, accel: { ...GOOD.accel, scale: [1.08, 1.0, 1.0] } },
  "gyro scale off": { ...GOOD, gyro: { ...GOOD.gyro, scale: [1.0, 1.0, 1.07] } },
  "NaN accel bias": { ...GOOD, accel: { ...GOOD.accel, bias: [NaN, 0, 0] } },
  "Infinity gyro scale": { ...GOOD, gyro: { ...GOOD.gyro, scale: [1, Infinity, 1] } },
  "NaN gyro bias": { ...GOOD, gyro: { ...GOOD.gyro, bias: [0, NaN, 0] } },
  "2 accel scales": { ...GOOD, accel: { ...GOOD.accel, scale: [1, 1] } },
  "4 gyro scales": { ...GOOD, gyro: { ...GOOD.gyro, scale: [1, 1, 1, 1] } },
  "2 gyro biases": { ...GOOD, gyro: { ...GOOD.gyro, bias: [0.1, 0.2] } },
  "a scale as text": { ...GOOD, accel: { ...GOOD.accel, scale: ["1.0", 1, 1] } },
  "no gyro": { ...GOOD, gyro: undefined },
  "the serial as text": { ...GOOD, pod: "0x41ef151f" },
  "a serial past 32 bits": { ...GOOD, pod: 2 ** 32 },
  "no made": { ...GOOD, made: undefined },
};
const FINE = { good: GOOD, "any pod": { ...GOOD, pod: null }, "no gyro bias": { ...GOOD, gyro: { ...GOOD.gyro, bias: null } } };
check(Object.values(FINE).every((c) => podCalibrationProblem(c) === null) && podCalibrationProblem(PARTIAL) === null,
  `a good one passes (this pod's, any pod's, no gyro bias, a gyro axis it hasn't got): ${podCalibrationProblem(GOOD)}`);
const missed = Object.entries(BAD).filter(([, c]) => podCalibrationProblem(c) === null).map(([k]) => k);
check(!missed.length, `refused: ${Object.keys(BAD).join(", ")}` + (missed.length ? `; NOT ${missed.join(", ")}` : ""));
check(/8%|5%/.test(podCalibrationProblem(BAD["accel scale 8% low"])) && /gyro bias/.test(podCalibrationProblem(BAD["NaN gyro bias"])),
  `each with its reason: "${podCalibrationProblem(BAD["accel scale 8% low"])}", "${podCalibrationProblem(BAD["NaN gyro bias"])}"`);

// The same verdicts as calib_pod.install_problem in Python on everything JSON can carry (NaN can't: Python
// refuses to write it, the app's JSON.parse refuses to read it). A null gyro scale is the one difference:
// the app can leave that axis alone, --install wants all 6.
const asResult = (c) => ({ ...c, complete: true, accel: c.accel && { ...c.accel, ok: true } });   // a saved result
const cases = { ...FINE, ...Object.fromEntries(Object.entries(BAD).filter(([k]) => !/NaN|Infinity|not an object/.test(k))) };
const PY_SHAPE = [
  "import sys, json, calib_pod",
  "d = json.loads(sys.stdin.read())",
  "print(json.dumps({k: calib_pod.install_problem(c) for k, c in d.items()}))",
].join("\n");
const pyWhy = JSON.parse(execFileSync(PYTHON, ["-c", PY_SHAPE], { cwd: path.join(ROOT, "analysis"), encoding: "utf8",
  input: JSON.stringify(Object.fromEntries(Object.entries({ ...cases, partial: PARTIAL }).map(([k, c]) => [k, asResult(c)]))) }));
const differ = Object.keys(cases).filter((k) => (pyWhy[k] === null) !== (podCalibrationProblem(cases[k]) === null));
check(!differ.length, `the app and --install agree on ${Object.keys(cases).length} cases (${Object.keys(cases).join(", ")})` +
  (differ.length ? `; NOT ${differ.map((k) => `${k} (py: ${pyWhy[k]}, js: ${podCalibrationProblem(cases[k])})`).join("; ")}` : ""));
check(pyWhy.partial !== null, `a gyro axis it hasn't got: the app could leave it alone, --install refuses it: ${pyWhy.partial}`);

// the installed one, if any: what --install writes, one the app takes
const installed = path.join(ROOT, "webapp", "pod-calibration.json");
if (existsSync(installed)) {
  let c = null, why;
  try { c = JSON.parse(readFileSync(installed, "utf8")); why = podCalibrationProblem(c); } catch (err) { why = `it doesn't parse: ${err.message}`; }
  check(why === null, `webapp/pod-calibration.json (${c?.from}, pod ${Number.isInteger(c?.pod) ? podHex(c.pod) : c?.pod}) is one the app can apply` +
    (why ? `: NOT, ${why}` : ""));
} else {
  console.log("skip  webapp/pod-calibration.json: none installed yet (calib_pod.py --install)");
}

console.log(ok ? "ALL PASS" : "SOME FAILED");
process.exit(ok ? 0 : 1);
