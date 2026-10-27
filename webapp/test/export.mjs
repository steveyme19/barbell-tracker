// The session export format (webapp/export.js, D30) without a browser:
//
//   node webapp/test/export.mjs
//
//   1. stored rows -> export -> JSON text -> rows: every column bit for bit (odd values included)
//   2. malformed exports are refused with a reason (format, version, a missing column, a short one)
//   3. analysis/sessions.py reads the same JSON to the same values (sequential sums equal) and
//      writes the same set CSV as the app's toCSV (to its 7 significant digits)
// Set PYTHON to the venv's python (an absolute path) if `python` isn't it.

import { spawnSync } from "node:child_process";
import { mkdtempSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { toExport, fromExport } from "../export.js";
import { toCSV } from "../fileio.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, "..", "..");
const PYTHON = process.env.PYTHON || join(ROOT, "analysis", ".venv", "Scripts", "python.exe");
const COLS = ["t", "ax", "ay", "az", "gx", "gy", "gz"];

let ok = true;
const check = (good, msg) => { console.log(`${good ? "PASS" : "FAIL"}  ${msg}`); ok &&= Boolean(good); };

// a stored row the way store.js makes one (t, tAbs float64; the readings float32), with values a
// codec could get wrong: -0, tiny, huge, a long fraction
let seed = 7;
const rnd = () => ((seed = (seed * 1103515245 + 12345) % 2 ** 31) / 2 ** 31);
function row(session, index, n) {
  const r = { session, index, tAbs: new Float64Array(n), t: new Float64Array(n) };
  for (const c of COLS.slice(1)) r[c] = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    r.t[i] = 1234.567891234 + i / 210.4;
    r.tAbs[i] = 60.123456789 * index + i / 210.4;
    for (const c of COLS.slice(1)) r[c][i] = (rnd() - 0.5) * 40;
  }
  r.ax[0] = -0; r.ay[1] = 1e-30; r.az[2] = 3.4e38; r.gx[3] = Math.PI;
  return r;
}
const rec = (id, nSets) => ({ id, stamp: id.slice(8), date: id.slice(8, 18), folder: id, parts: 1, startedAtMs: 1792984128694,
  ended: true, autosave: true, sets: Array.from({ length: nSets }, (_, k) => ({ index: k, exercise: "Squat", lift: "squat", nReps: 5,
    file: `bar-squat-${id.slice(8)}-set${String(k + 1).padStart(2, "0")}.csv` })) });

const sessions = [
  // a typed name Python must read as UTF-8 (accents and an emoji: the night review)
  { record: { ...rec("session-2026-10-26-0100", 2), sets: rec("session-2026-10-26-0100", 2).sets.map((s, k) => (k ? s : { ...s, exercise: "Développé couché \u{1F3CB}" })) },
    rows: [row("session-2026-10-26-0100", 0, 1500), row("session-2026-10-26-0100", 1, 2301)] },
  { record: rec("session-2026-10-26-0200", 1), rows: [row("session-2026-10-26-0200", 0, 7)] },
];
const text = JSON.stringify(toExport(sessions, COLS));
const back = fromExport(JSON.parse(text), COLS);
const bytes = (a) => Buffer.from(a.buffer, a.byteOffset, a.byteLength);
let same = back.length === sessions.length;
for (let s = 0; s < sessions.length && same; s++) {
  same &&= JSON.stringify(back[s].record) === JSON.stringify(sessions[s].record) && back[s].rows.length === sessions[s].rows.length;
  for (let k = 0; k < sessions[s].rows.length && same; k++) {
    const a = sessions[s].rows[k], b = back[s].rows[k];
    same &&= b.session === a.session && b.index === a.index;
    for (const c of [...COLS, "tAbs"]) same &&= b[c].constructor === a[c].constructor && bytes(b[c]).equals(bytes(a[c]));
  }
}
const decimal = JSON.stringify(sessions.map((s) => s.rows.map((r) => COLS.map((c) => Array.from(r[c]))))).length;
check(same && Object.is(back[0].rows[0].ax[0], -0),
  `round trip: 2 sessions, 3 sets, every column bit for bit (-0 kept); ${(text.length / 1e3).toFixed(0)} kB against ${(decimal / 1e3).toFixed(0)} kB as decimal JSON`);

const refuse = (mutate, want) => {
  const o = JSON.parse(text);
  mutate(o);
  try { fromExport(o, COLS); return false; } catch (e) { return want.test(e.message); }
};
check(refuse((o) => { o.format = "x"; }, /Not a Bar Tracker/) && refuse((o) => { o.version = 2; }, /version 2/) &&
  refuse((o) => { delete o.sessions[0].sets[1].cols.gz; }, /no gz column/) &&
  refuse((o) => { o.sessions[1].sets[0].n = 8; }, /has 7 samples, not 8/) && refuse((o) => { delete o.sessions[0].record.id; }, /no id/),
  "malformed exports refused: another format, version 2, a missing column, a short column, a session with no id");

// Python reads the same file
const dir = mkdtempSync(join(tmpdir(), "bt-export-"));
const path = join(dir, "export.json");
writeFileSync(path, text);
const py = spawnSync(PYTHON, ["-c", [
  "import json, sys",
  "sys.path.insert(0, sys.argv[2])",
  "import sessions as S",
  "out = []",
  "for s in S.load(sys.argv[1]):",
  "    for st in s['sets']:",
  "        sums = {}",
  "        for c in S.COLS + ['tAbs']:",
  "            acc = 0.0",
  "            for x in st[c]: acc += float(x)",
  "            sums[c] = acc",
  "        out.append({'id': s['record']['id'], 'index': st['index'], 'n': st['n'], 'sums': sums, 'csv': S.set_csv(st),",
  "                    'names': [x.get('exercise') for x in s['record']['sets']]})",
  "print(json.dumps(out))",
].join("\n"), path, join(ROOT, "analysis")], { encoding: "utf8", maxBuffer: 1 << 26 });
rmSync(dir, { recursive: true, force: true });
if (py.status !== 0) {
  check(false, `sessions.py failed: ${py.stderr.slice(-400)}`);
} else {
  const got = JSON.parse(py.stdout);
  const rows = sessions.flatMap((s) => s.rows);
  let sums = got.length === rows.length, csv = sums, worst = 0;
  for (let k = 0; k < rows.length && sums; k++) {
    const r = rows[k], g = got[k];
    sums &&= g.id === r.session && g.index === r.index && g.n === r.t.length;
    for (const c of [...COLS, "tAbs"]) { let acc = 0; for (const x of r[c]) acc += x; sums &&= acc === g.sums[c]; }
    // the app's own CSV of the same samples (toCSV), against sessions.py's: the same numbers
    const samples = Array.from(r.t, (_, i) => Object.fromEntries(COLS.map((c) => [c, r[c][i]])));
    const a = toCSV(samples).trim().split("\n"), b = g.csv.trim().split("\n");
    csv &&= a.length === b.length && a[0] === b[0];
    for (let i = 1; i < a.length && csv; i++) {
      const x = a[i].split(",").map(Number), y = b[i].split(",").map(Number);
      for (let j = 0; j < x.length; j++) worst = Math.max(worst, Math.abs(x[j] - y[j]) / Math.max(1e-30, Math.abs(x[j])));
    }
  }
  check(sums, "sessions.py reads the same values (every column's sequential sum equal, bit for bit)");
  check(got[0].names[0] === "Développé couché \u{1F3CB}", `sessions.py reads a typed name as UTF-8: "${got[0].names[0]}"`);
  check(csv && worst < 1e-6, `sessions.py's set CSV matches the app's toCSV: same rows, worst relative difference ${worst.toExponential(1)}`);
}
console.log(ok ? "PASS" : "FAIL");
process.exit(ok ? 0 : 1);
