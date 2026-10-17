// UI wiring: source -> algorithm -> UI (decision D4).
import { PhoneSource, NativeSensorSource, ReplaySource, isNativeApp } from "./sources.js";
import { analyze, magnitude, SETTINGS } from "./reps.js";
import { parseRecording, toCSV } from "./fileio.js";
import { drawPlot } from "./plot.js";

const $ = (id) => document.getElementById(id);
const ANALYZE_EVERY_MS = 500;   // at least this long between live passes
const LIVE_SECONDS = 8;

let samples = [];
let source = null;
let running = false;
let result = null;
let analyzeTimer = null;
let wakeLock = null;
let drawQueued = false;
let sourceName = "";
let lift = "phone";   // which heights repeat every rep (reps.js LIFTS)

// ---------- incoming data ----------

function onSample(s) {
  samples.push(s);
  if (!drawQueued) {
    drawQueued = true;
    requestAnimationFrame(() => { drawQueued = false; drawLive(); updateFooter(); });
  }
}

function runAnalysis() {
  if (samples.length < 10) return;
  const t = samples.map((s) => s.t);
  const a = magnitude(samples.map((s) => s.ax), samples.map((s) => s.ay), samples.map((s) => s.az));
  result = analyze(t, a, { ...SETTINGS, LIFT: lift });
  render();
}

// Live re-analysis. Each pass redoes the whole set, so at 400 Hz (native sensors) a long
// set takes a while: wait 2x the last pass's run time when that's more than 500 ms, so
// the UI never spends more than about a third of its time on analysis.
function scheduleAnalysis(lastMs = 0) {
  analyzeTimer = setTimeout(() => {
    if (!running) return;
    const t0 = performance.now();
    runAnalysis();
    scheduleAnalysis(performance.now() - t0);
  }, Math.max(ANALYZE_EVERY_MS, 2 * lastMs));
}

// ---------- start / stop ----------

async function startPhone() {
  if (!window.isSecureContext) {
    showError("Motion sensors need HTTPS. Open the GitHub Pages link on your phone.");
    return;
  }
  reset();
  // In the Android app, the native plugin gives full-rate sensors (A2). Same samples.
  const native = isNativeApp();
  sourceName = native ? "phone (native)" : "phone";
  source = native ? new NativeSensorSource(onSample) : new PhoneSource(onSample);
  try {
    await source.start();
  } catch (err) {
    showError(err.message);
    return;
  }
  begin();
  setTimeout(() => {
    if (running && samples.length === 0) {
      showError("No sensor data. Open this page on your phone (laptops have no motion sensor).");
    }
  }, 2000);
}

async function startReplay(file) {
  let rec;
  try {
    rec = await parseRecording(file.name, await file.arrayBuffer());
  } catch (err) {
    showError(err.message);
    return;
  }
  reset();
  sourceName = file.name;
  const named = Object.keys(SETTINGS.LIFTS).find((l) => file.name.toLowerCase().includes(l));
  if (named) setLift(named);
  const realtime = $("realtime").checked;
  source = new ReplaySource(onSample, rec, { realtime, onDone: stop });
  begin();
  await source.start();
}

function begin() {
  running = true;
  $("startBtn").disabled = true;
  $("stopBtn").disabled = false;
  $("downloadBtn").disabled = true;
  setStatus("Hold still…", "wait");
  scheduleAnalysis();
  keepAwake();
}

async function stop() {
  if (!running) return;
  running = false;
  await source?.stop();   // native source: wait for its last batch
  clearTimeout(analyzeTimer);
  runAnalysis();
  drawLive();
  updateFooter();
  $("startBtn").disabled = false;
  $("stopBtn").disabled = true;
  $("downloadBtn").disabled = samples.length === 0;
  const n = result?.reps?.length ?? 0;
  setStatus(`Done · ${n} rep${n === 1 ? "" : "s"}`, "");
  wakeLock?.release().catch(() => {});
  wakeLock = null;
}

function reset() {
  samples = [];
  result = null;
  $("hint").classList.remove("err");
  render();
  drawLive();
}

// ---------- screen stays on during a set ----------

async function keepAwake() {
  try { wakeLock = await navigator.wakeLock?.request("screen"); } catch { /* not supported */ }
}
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && running) keepAwake();
});

// ---------- download ----------

function recordingName() {
  const d = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  return `bar-${lift}-${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}.csv`;
}

function download() {
  const blob = new Blob([toCSV(samples)], { type: "text/csv" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = recordingName();
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}

// ---------- drawing ----------

function render() {
  const reps = result?.reps ?? [];
  if (running) {
    if (result?.error) setStatus("Hold still…", "wait");
    else if (result) setStatus(`Live · ${reps.length} rep${reps.length === 1 ? "" : "s"}`, "live");
  }

  const last = reps[reps.length - 1];
  const pending = Boolean(last && last.flags.includes("no end anchor"));
  $("mcv").textContent = last ? last.mcv.toFixed(2) : "--";
  $("mcv").parentElement.classList.toggle("pending", pending);
  $("lastLabel").textContent = pending ? "Last rep · pause to lock it in" : "Last rep · mean velocity";
  $("repCount").textContent = reps.length;
  $("rom").textContent = last ? (last.rom * 100).toFixed(1) : "--";
  $("peak").textContent = last ? last.peak.toFixed(2) : "--";
  if (reps.length >= 2) {
    const best = Math.max(...reps.map((r) => r.mcv));
    $("loss").textContent = `${Math.round((1 - last.mcv / best) * 100)}%`;
  } else {
    $("loss").textContent = "--";
  }

  const tbody = $("repTable").querySelector("tbody");
  tbody.innerHTML = "";
  reps.forEach((r, k) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${k + 1}</td><td>${(r.rom * 100).toFixed(1)} cm</td>` +
      `<td>${r.mcv.toFixed(2)}</td><td>${r.peak.toFixed(2)}</td><td>${r.dur.toFixed(2)} s</td>` +
      `<td class="flag">${r.flags.length ? "⚠" : ""}</td>`;
    if (r.flags.length) tr.title = r.flags.join(", ");
    tbody.appendChild(tr);
  });

  drawHeight();
}

function drawLive() {
  const canvas = $("liveCanvas");
  if (!samples.length) { drawPlot(canvas, { series: [] }); return; }
  const g = result?.g ?? 9.81;
  const tEnd = samples[samples.length - 1].t;
  const xs = [];
  const ys = [];
  for (let i = samples.length - 1; i >= 0; i--) {
    const s = samples[i];
    if (tEnd - s.t > LIVE_SECONDS) break;
    xs.push(s.t - tEnd);
    ys.push(Math.sqrt(s.ax * s.ax + s.ay * s.ay + s.az * s.az) - g);
  }
  drawPlot(canvas, { series: [{ x: xs.reverse(), y: ys.reverse(), color: cssVar("--line") }], yLabel: "m/s²", minRange: 2 });
}

function drawHeight() {
  const canvas = $("heightCanvas");
  if (!result || result.error) { drawPlot(canvas, { series: [] }); return; }
  const { t, x, reps, windows } = result;
  const cm = Array.from(x, (v) => v * 100);
  // Everything after the last still is provisional: its drift fix gets redone once
  // you pause (and before the 2nd lockout there's no fix at all). Gray and dashed.
  const open = windows.find((w) => !w.anchored);
  const cut = open ? open.i0 : t.length - 1;
  const series = [{ x: t.slice(0, cut + 1), y: cm.slice(0, cut + 1), color: cssVar("--line") }];
  if (open) series.push({ x: t.slice(cut), y: cm.slice(cut), color: cssVar("--muted"), dash: [4, 4] });
  drawPlot(canvas, {
    series,
    spans: reps.map((r) => ({ x0: t[r.start], x1: t[r.end], color: cssVar("--rep") })),
    yLabel: "cm",
    minRange: 10,
  });
}

function updateFooter() {
  let rate = "--";
  if (source instanceof PhoneSource && source.interval) {
    rate = `${Math.round(1000 / source.interval)} Hz (interval ${source.interval.toFixed(1)} ms)`;
  } else if (samples.length > 10) {
    // Measured from the sample timestamps, so it's the real rate, not the requested one.
    const n = samples.length;
    rate = `${Math.round((n - 1) / (samples[n - 1].t - samples[0].t))} Hz`;
    if (source instanceof NativeSensorSource && source.info) rate += ` (${source.info.accel})`;
  }
  $("rate").textContent = sourceName ? `${sourceName} · ${rate}` : rate;
  $("samples").textContent = `${samples.length} samples`;
}

function setStatus(text, kind) {
  const el = $("status");
  el.textContent = text;
  el.className = `pill ${kind || ""}`;
}

function showError(msg) {
  setStatus("Error", "err");
  $("hint").textContent = msg;
  $("hint").classList.add("err");
}

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

// ---------- wire up ----------

$("startBtn").addEventListener("click", startPhone);
$("stopBtn").addEventListener("click", stop);
$("downloadBtn").addEventListener("click", download);
// For android/pull-recording.mjs (over USB) until the app can save files itself (A3).
window.barDebug = { recording: () => ({ name: recordingName(), csv: toCSV(samples) }) };
// ---------- lift picker ----------

function setLift(value) {
  lift = value in SETTINGS.LIFTS ? value : "phone";
  const radio = document.querySelector(`input[name="lift"][value="${lift}"]`);
  if (radio) radio.checked = true;
  try { localStorage.setItem("lift", lift); } catch { /* storage blocked: just don't remember */ }
  if (samples.length >= 10) runAnalysis();
}
document.querySelectorAll('input[name="lift"]').forEach((r) => r.addEventListener("change", () => setLift(r.value)));
try { setLift(localStorage.getItem("lift") || "phone"); } catch { setLift("phone"); }

$("fileInput").addEventListener("change", (e) => {
  const f = e.target.files[0];
  if (f) startReplay(f);
  e.target.value = "";
});
window.addEventListener("resize", () => { drawLive(); drawHeight(); });
render();
drawLive();
