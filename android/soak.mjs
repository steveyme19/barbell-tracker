// The step 12 soak (docs/ble-plan.md step 12): a long pod session in the app with the phone locked,
// in parts. The phone is on USB (debugging on) only for each command; in between it's locked in a
// pocket, unplugged. The pod stays on the laptop's USB the whole time, recorded by
//   python analysis/usb_clock.py <json> --samples=<prefix>
// (every sample, and each boot's wall-clock origin: the truth check_soak.py compares against).
//
//   node android/soak.mjs start [--exercise=Deadlift]   unlock first: the session, with the capture on
//   node android/soak.mjs status                         locked is fine: link, samples, sets so far
//   node android/soak.mjs sleep                          locked is fine: the BLE SLEEP op (mode 0, wakes on motion)
//   node android/soak.mjs end [--out=DIR]                locked is fine: End, then everything into
//                                                        analysis/data/soak-<stamp>/ (P2), or DIR
// Then: python analysis/check_soak.py analysis/data/soak-<stamp> <usb_clock json>

import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { APP, adb, page, sleep, ensureFront } from "./webview.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, "..");
const STATE = join(tmpdir(), "bar-soak.json");      // what start learned, for the later parts
const arg = (k, d) => process.argv.find((a) => a.startsWith(`--${k}=`))?.split("=").slice(1).join("=") ?? d;
const pid = () => { try { return adb("shell", "pidof", APP); } catch { return ""; } };
const battery = () => Number(adb("shell", "dumpsys", "battery").match(/level: (\d+)/)?.[1]);
const fgs = () => /RecordingService[\s\S]*?isForeground=true/.test(adb("shell", "dumpsys", "activity", "services", APP));
const state = () => JSON.parse(readFileSync(STATE, "utf8"));

async function until(js, expr, ms, what) {
  const t0 = Date.now();
  while (Date.now() - t0 < ms) { if (await js(expr)) return; await sleep(500); }
  throw new Error(`timed out waiting for ${what}`);
}

const STATUS = `(() => {
  const L = window.barDebug.link(), s = window.barDebug.session();
  return { hidden: document.hidden, running: !document.getElementById('endBtn').disabled,
    samples: window.barDebug.pod()?.stats.samples ?? null, linked: L?.linked ?? null, reconnects: L?.reconnects ?? null,
    events: (L?.linkEvents ?? []).map((e) => e.kind + ' ' + new Date(e.at).toTimeString().slice(0, 8)),
    gaps: (L?.gapList ?? []).map((g) => g.kind + ' ' + g.n), epochs: L?.epochList.length ?? null, faults: L?.faults ?? [],
    sets: s?.sets.length ?? 0, buzzes: window.barDebug.buzzes().map((b) => (b.hidden ? 'dark ' : 'lit ') + new Date(b.at).toTimeString().slice(0, 8)) };
})()`;

async function start() {
  await ensureFront();
  const { js, close } = await page();
  try {
    if (await js("!document.getElementById('endBtn').disabled")) throw new Error("a session is already running: end it first");
    await js("window.barDebug.podCapture = true");
    const ex = arg("exercise", "Deadlift");
    await js(`(() => { const e = document.getElementById('exercise'); e.value = ${JSON.stringify(ex)}; e.dispatchEvent(new Event('change')); })()`);
    // only when it isn't the source yet: a tap on an active pod button means "the nearest pod next Start"
    if (!(await js("document.getElementById('podBtn').classList.contains('on')"))) await js("document.getElementById('podBtn').click()");
    await js("document.getElementById('startBtn').click()");
    await until(js, "!document.getElementById('endBtn').disabled || document.getElementById('hint').classList.contains('err')", 60000, "the session to start");
    const err = await js("document.getElementById('hint').classList.contains('err') ? document.getElementById('hint').textContent : ''");
    if (err && !/Screen-off recording/.test(err)) throw new Error(`the session didn't start: ${err}`);
    if (err) console.log(`FAIL  ${err}`);
    const s = await js("({ folder: window.barDebug.session().folder, stamp: window.barDebug.session().stamp })");
    const st = { ...s, exercise: ex, pid: pid(), startedAt: Date.now(), battery: battery() };
    writeFileSync(STATE, JSON.stringify(st, null, 1));
    await sleep(4000);
    console.log(JSON.stringify({ ...st, fgs: fgs(), ...(await js(STATUS)) }, null, 1));
    console.log("Started. Unplug the phone, lock it, pocket it.");
  } finally {
    close();
  }
}

async function status() {
  const { js, close } = await page();
  try {
    console.log(JSON.stringify({ pid: pid(), pidAtStart: state().pid, fgs: fgs(), ...(await js(STATUS)) }, null, 1));
  } finally {
    close();
  }
}

async function sleepPod() {
  const { js, close } = await page();
  try {
    console.log(await js("window.barDebug.podSleep(0).then(() => 'SLEEP sent (mode 0: wakes on motion)', (e) => 'error: ' + e.message)"));
    await sleep(3000);
    console.log(JSON.stringify(await js(STATUS), null, 1));
  } finally {
    close();
  }
}

async function end() {
  const st = state();
  const pidEnd = pid();
  const fgsEnd = fgs();
  const { js, close } = await page();
  try {
    const before = await js(STATUS);
    if (!before.running) throw new Error("no session is running");
    await js("document.getElementById('endBtn').click()");
    await until(js, "!document.getElementById('startBtn').disabled", 60000, "End");
    const out = arg("out", join(ROOT, "analysis", "data", `soak-${st.stamp}`));
    mkdirSync(out, { recursive: true });
    // the app's own files (set CSVs, session.csv, link.json, pod.json)
    const dir = `/sdcard/Documents/BarTracker/${st.folder}`;
    const files = adb("shell", "ls", dir).split(/\s+/).filter(Boolean);
    for (const f of files) adb("pull", `${dir}/${f}`, join(out, f));
    // the capture (every frame, command and call, for podlink.py), in 2 MB pieces
    const n = await js("window.barDebug.podCaptureData()?.csv.length ?? 0");
    let csv = "";
    for (let i = 0; i < n; i += 2e6) csv += await js(`window.barDebug.podCaptureData().csv.slice(${i}, ${i + 2e6})`);
    writeFileSync(join(out, "pod-capture.csv"), csv);
    // what the app decoded, as sums: check_soak.py replays the capture and must get the same, bit for bit
    const digest = await js(`(() => { const S = window.barDebug.podCaptureData().samples; const k = ['t', 'ax', 'ay', 'az', 'gx', 'gy', 'gz'];
      const sum = Object.fromEntries(k.map((x) => [x, 0])); for (const s of S) for (const x of k) sum[x] += s[x]; return { n: S.length, sum }; })()`);
    writeFileSync(join(out, "buzzes.json"), JSON.stringify(await js("window.barDebug.buzzes()"), null, 1));
    // Android's own record of each vibration: played, or ignored (settings, background)
    const vib = adb("shell", "dumpsys", "vibrator_manager").split("\n").filter((l) => l.includes(APP));
    writeFileSync(join(out, "vibrations.txt"), vib.join("\n") + "\n");
    const soak = { ...st, endedAt: Date.now(), pidEnd, fgsAtEnd: fgsEnd, batteryEnd: battery(), digest, files, statusAtEnd: before };
    writeFileSync(join(out, "soak.json"), JSON.stringify(soak, null, 1));
    console.log(`${pidEnd === st.pid ? "PASS" : "FAIL"}  the app's process lived through the soak (pid ${st.pid} -> ${pidEnd || "gone"})`);
    console.log(`${fgsEnd ? "PASS" : "FAIL"}  RecordingService still in the foreground at the end`);
    console.log(`saved ${files.length} app files, the capture (${(n / 1e6).toFixed(1)} MB, ${digest.n} samples), buzzes, vibrations in ${out}`);
    console.log(`battery ${st.battery}% -> ${soak.batteryEnd}% over ${((soak.endedAt - st.startedAt) / 60000).toFixed(1)} min`);
  } finally {
    close();
  }
}

const cmd = process.argv[2];
const run = { start, status, sleep: sleepPod, end }[cmd];
if (!run) { console.log("node android/soak.mjs start|status|sleep|end"); process.exit(2); }
run().then(() => process.exit(0), (e) => { console.log(`FAIL  ${e.message}`); process.exit(1); });
