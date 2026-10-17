// UI wiring: source -> session tracker (sets.js) -> rep algorithm -> UI (decision D4).
// Session mode: start once, lift, and the tracker finds each set and the rest between.
import { PhoneSource, NativeSensorSource, ReplaySource } from "./sources.js";
import { isNativeApp, keepScreenOn, saveFile, deleteFile, shareFiles } from "./native.js";
import { analyze, magnitude, liftFromName, liftForExercise, SETTINGS } from "./reps.js";
import { SessionTracker, setSummary, dropRackMoves, formatTime, formatRpe } from "./sets.js";
import { parseRecording, toCSV } from "./fileio.js";
import { drawPlot } from "./plot.js";

const $ = (id) => document.getElementById(id);
const ANALYZE_EVERY_MS = 500;   // at least this long between live passes
const LIVE_SECONDS = 8;
const REST_AFTER = 3;           // s of quiet after a rep before the card shows the rest timer
const DEFAULT_NAMES = { phone: "Phone test", squat: "Squat", bench: "Bench", deadlift: "Deadlift", machine: "Machine" };
const SUGGESTIONS = ["Squat", "Bench", "Deadlift", "Lat pulldown", "Cable row", "Leg extension", "Leg curl", "Chest press", "Phone test"];
// What the typed name means for tracking (liftForExercise, D19).
const PROFILE_HINT = {
  phone: "Tracked as a hand-held phone test.",
  squat: "Tracked as a squat: lockouts level, RPE estimate on.",
  bench: "Tracked as a bench: chest and lockout level, RPE estimate on.",
  deadlift: "Tracked as a deadlift: floor and lockout level, RPE estimate on.",
  machine: "Tracked as a weight-stack machine: strap the phone to the top plate. No RPE estimate.",
};
const native = isNativeApp();

let source = null;
let running = false;
let tracker = null;
let session = null;       // { stamp, folder, autosave, sets: [] }
let live = null;          // analysis of the set in progress: { res, reps, tAbs }
let recent = [];          // last LIVE_SECONDS of samples (live plot, rate)
let nSamples = 0;
let exercise = "Phone test";  // sticky: what you typed for the next set
let weight = "";              // sticky: weight for the next set, lb (text, may be empty)
let lift = "phone";           // tracking profile from the exercise name (liftForExercise)
let seg = null;               // { exercise, weight, lift } as they were when this set started moving
let buzzed = false;       // vibrated for the set in progress yet?
let analyzeTimer = null;
let tickTimer = null;
let wakeLock = null;
let drawQueued = false;
let sourceName = "";

// ---------- incoming data ----------

function onSample(s) {
  tracker.push(s);
  nSamples++;
  recent.push(s);
  if (recent.length > 50 && s.t - recent[0].t > LIVE_SECONDS + 1) {
    recent = recent.filter((x) => s.t - x.t <= LIVE_SECONDS);
  }
  if (!drawQueued) {
    drawQueued = true;
    requestAnimationFrame(() => { drawQueued = false; drawLive(); updateFooter(); });
  }
}

// Live analysis of the set in progress only (the tracker hands over just that
// segment), so a long session stays as fast as a single set.
function runLive() {
  if (!tracker?.inSegment) { live = null; return; }
  const { samples, tAbs } = tracker.segmentSamples();
  if (samples.length < 10) return;
  const res = analyze(samples.map((s) => s.t),
    magnitude(samples.map((s) => s.ax), samples.map((s) => s.ay), samples.map((s) => s.az)),
    { ...SETTINGS, LIFT: seg?.lift ?? lift });
  const reps = res.error ? [] : dropRackMoves(res.reps, seg?.lift ?? lift);
  live = { res, reps, tAbs };
  if (reps.length && !buzzed) { buzz(150); buzzed = true; }   // a set started
}

// Each pass redoes the whole set so far. At 400 Hz a long set takes a while: wait 2x
// the last pass's run time when that's more than 500 ms.
function scheduleAnalysis(lastMs = 0) {
  analyzeTimer = setTimeout(() => {
    if (!running) return;
    const t0 = performance.now();
    runLive();
    render();
    scheduleAnalysis(performance.now() - t0);
  }, Math.max(ANALYZE_EVERY_MS, 2 * lastMs));
}

// ---------- tracker events ----------

function onSet(set) {
  set.exercise = seg?.exercise ?? exercise;
  set.load = seg?.weight ?? weight;
  set.rpe = "";
  rememberExercise(set.exercise);
  live = null;
  buzzed = false;
  seg = null;
  set.file = setFileName(set);
  session.sets.push(set);
  buzz([100, 80, 100]);                                  // a set finished
  addSetCard(set);
  if (session.autosave) saveSet(set).then(saveSummary).catch((err) => showError(`Couldn't save: ${err.message}`));
  render();
}

function onDrop() {     // motion that wasn't a set (plates, a bump)
  live = null;
  buzzed = false;
  seg = null;
  render();
}

// ---------- start / end ----------

function newSession({ autosave }) {
  const d = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  const stamp = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}`;
  session = { stamp, date: stamp.slice(0, 10), startedAt: d, folder: `session-${stamp}`, autosave, sets: [] };
  tracker = new SessionTracker({
    liftFor: () => seg?.lift ?? lift,
    onSegmentStart: () => { seg = { exercise, weight, lift }; },
    onSet,
    onDrop,
  });
  live = null;
  recent = [];
  nSamples = 0;
  $("setList").innerHTML = "";
  $("hint").textContent = "";
  $("hint").classList.remove("err");
}

async function startSession() {
  if (!window.isSecureContext) {
    showError("Motion sensors need HTTPS. Open the GitHub Pages link on your phone.");
    return;
  }
  newSession({ autosave: native });
  // In the Android app, the native plugin gives full-rate sensors (A2). Same samples.
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
    if (running && nSamples === 0) showError("No sensor data. Open this page on your phone (laptops have no motion sensor).");
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
  // Replays don't write files unless you tap Save (barDebug.autosaveReplays: USB testing).
  newSession({ autosave: native && Boolean(window.barDebug?.autosaveReplays) });
  sourceName = file.name;
  const named = liftFromName(file.name);
  if (named !== lift && file.name.toLowerCase().includes(named)) setExercise(DEFAULT_NAMES[named]);
  const realtime = $("realtime").checked;
  source = new ReplaySource(onSample, rec, { realtime, speed: Number($("speed").value), onDone: endSession });
  begin();
  await source.start();
}

function begin() {
  running = true;
  $("startBtn").disabled = true;
  $("endBtn").disabled = false;
  $("fileInput").disabled = true;
  scheduleAnalysis();
  tickTimer = setInterval(render, 1000);                 // the rest timer
  keepAwake();
  render();
}

async function endSession() {
  if (!running) return;
  running = false;
  await source?.stop();        // native source: wait for its last batch
  clearTimeout(analyzeTimer);
  clearInterval(tickTimer);
  tracker.finish();            // a set still in progress gets closed (and saved)
  live = null;
  $("startBtn").disabled = false;
  $("endBtn").disabled = true;
  $("fileInput").disabled = false;
  wakeLock?.release().catch(() => {});
  wakeLock = null;
  if (native) keepScreenOn(false).catch(() => {});
  if (session.autosave && session.sets.length) await saveSummary().catch(() => {});
  render();
}

// ---------- screen stays on for the whole session ----------

async function keepAwake() {
  // In the app, the window flag is certain; the WebView's Wake Lock API may not be (A3).
  if (native) return keepScreenOn(true).catch(() => {});
  try { wakeLock = await navigator.wakeLock?.request("screen"); } catch { /* not supported */ }
}
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && running && !native) keepAwake();
});

function buzz(pattern) {
  try { navigator.vibrate?.(pattern); } catch { /* no vibration */ }
}

// ---------- saving ----------
// App: every set is written to Documents/BarTracker/session-<stamp>/ when it finishes,
// plus session.csv (one row per set: exercise, weight, reps, rest, velocities, RPE; no
// raw data). Set files start bar-<profile>- so Python knows how to track them.
// Browser: buttons only (auto downloads would pile up).

function setFileName(set) {
  const slug = set.exercise.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 30);
  const tail = slug && slug !== set.lift ? `-${slug}` : "";
  return `bar-${set.lift}-${session.stamp}-set${String(set.index + 1).padStart(2, "0")}${tail}.csv`;
}

// CSV field: quoted if it has a comma, quote or newline in it (exercise names are typed).
const csvField = (x) => (/[",\n]/.test(String(x)) ? `"${String(x).replace(/"/g, '""')}"` : String(x));

async function saveSet(set) {
  const { uri } = await saveFile(session.folder, set.file, toCSV(set.samples));
  return uri;
}

function summaryCSV() {
  const head = "set,date,start,file,exercise,lift,load_lb,reps,rpe,rpe_est,rest_before_s,best_mcv,last_mcv," +
    "vel_loss_pct,avg_rom_cm,rep_mcvs,to_failure,mount,paused_lockout,notes";
  const rows = session.sets.map((s, k) => {
    const start = new Date(session.startedAt.getTime() + s.startT * 1000).toTimeString().slice(0, 8);
    return [k + 1, session.date, start, s.file, csvField(s.exercise), s.lift, csvField(s.load), s.nReps, csvField(s.rpe),
      s.rpeEst ? (s.rpeEst[0] === 5 ? `<=${s.rpeEst[1]}` : `${s.rpeEst[0]}-${s.rpeEst[1]}`) : "",
      s.restBefore === null ? "" : Math.round(s.restBefore),
      s.bestMcv.toFixed(3), s.lastMcv.toFixed(3), Math.round(s.velLoss * 100), (s.avgRom * 100).toFixed(1),
      s.reps.map((r) => r.mcv.toFixed(2)).join(" "), "", "", "", ""].join(",");
  });
  return [head, ...rows].join("\n") + "\n";
}

async function saveSummary() {
  return (await saveFile(session.folder, "session.csv", summaryCSV())).uri;
}

function download(name, text) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([text], { type: "text/csv" }));
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}

async function shareSession() {
  if (!session?.sets.length) return;
  if (!native) { download(`${session.folder}.csv`, summaryCSV()); return; }
  try {
    await shareFiles([await saveSummary()], `Workout ${session.stamp}`);
  } catch (err) {
    showError(`Couldn't share: ${err.message}`);
  }
}

async function shareSet(set) {
  if (!native) { download(set.file, toCSV(set.samples)); return; }
  try {
    await shareFiles([await saveSet(set)], set.file);
  } catch (err) {
    showError(`Couldn't share: ${err.message}`);
  }
}

// ---------- set cards ----------

function addSetCard(set) {
  const card = $("setCardTpl").content.firstElementChild.cloneNode(true);
  set.card = card;
  card.querySelector(".set-exercise").addEventListener("change", (e) => changeSetExercise(set, e.target.value));
  card.querySelector(".set-load").value = set.load;
  card.querySelector(".set-load").addEventListener("change", (e) => { set.load = e.target.value; saveSummaryLater(); });
  card.querySelector(".set-rpe").addEventListener("change", (e) => { set.rpe = e.target.value; saveSummaryLater(); });
  card.querySelector(".set-save").addEventListener("click", () => shareSet(set));
  card.querySelector(".set-more").addEventListener("toggle", (e) => { if (e.target.open) drawSetPlot(set); });
  fillSetCard(set);
  $("setList").prepend(card);           // newest first
}

function fillSetCard(set) {
  const c = set.card;
  c.querySelector(".set-title").textContent = `Set ${set.index + 1}`;
  c.querySelector(".set-exercise").value = set.exercise;
  c.querySelector(".set-rest").textContent = set.restBefore === null ? "first set" : `rest ${formatTime(set.restBefore)}`;
  c.querySelector(".set-stats").innerHTML = [
    [set.nReps, "reps"], [set.bestMcv.toFixed(2), "best MCV"],
    [`${Math.round(set.velLoss * 100)}%`, "vel. loss"], [formatRpe(set.rpeEst), "RPE est."],
  ].map(([v, l]) => `<div><span>${v}</span><label>${l}</label></div>`).join("");
  const tbody = c.querySelector("tbody");
  tbody.innerHTML = "";
  set.reps.forEach((r, k) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${k + 1}</td><td>${(r.rom * 100).toFixed(1)} cm</td>` +
      `<td>${r.mcv.toFixed(2)}</td><td>${r.peak.toFixed(2)}</td><td>${r.dur.toFixed(2)} s</td>` +
      `<td class="flag">${r.flags.length ? "⚠" : ""}</td>`;
    if (r.flags.length) tr.title = r.flags.join(", ");
    tbody.appendChild(tr);
  });
  if (c.querySelector(".set-more").open) drawSetPlot(set);
}

// Typed the wrong exercise? Rename the set. If the new name means a different tracking
// profile, re-run the set with it (it changes which heights get leveled) and fix the rest
// times around it. Either way it's re-saved under the new file name.
async function changeSetExercise(set, name) {
  name = name.trim();
  if (!name) { fillSetCard(set); return; }
  const newLift = liftForExercise(name);
  rememberExercise(name);
  if (newLift === set.lift) {
    const oldFile = set.file;
    set.exercise = name;
    set.file = setFileName(set);
    fillSetCard(set);
    render();
    if (session.autosave) await resave(set, oldFile);
    return;
  }
  const s = set.samples;
  const res = analyze(s.map((x) => x.t), magnitude(s.map((x) => x.ax), s.map((x) => x.ay), s.map((x) => x.az)),
    { ...SETTINGS, LIFT: newLift });
  const reps = res.error ? [] : dropRackMoves(res.reps, newLift);
  if (!reps.length) { showError("No reps with that lift setting; kept the old one."); fillSetCard(set); return; }
  const oldFile = set.file;
  Object.assign(set, setSummary(newLift, reps, set.tAbs), { reps, analysis: res, exercise: name });
  set.file = setFileName(set);
  const sets = session.sets;
  for (let k = Math.max(1, set.index); k <= Math.min(set.index + 1, sets.length - 1); k++) {
    sets[k].restBefore = sets[k].startT - sets[k - 1].endT;
    fillSetCard(sets[k]);
  }
  fillSetCard(set);
  render();
  if (session.autosave) await resave(set, oldFile);
}

async function resave(set, oldFile) {
  try {
    await saveSet(set);
    if (oldFile !== set.file) await deleteFile(session.folder, oldFile);
    await saveSummary();
  } catch (err) {
    showError(`Couldn't save: ${err.message}`);
  }
}

let summaryTimer = null;
function saveSummaryLater() {
  if (!session.autosave) return;
  clearTimeout(summaryTimer);
  summaryTimer = setTimeout(() => saveSummary().catch((err) => showError(`Couldn't save: ${err.message}`)), 800);
}

// ---------- the big card ----------

function render() {
  const hero = $("hero");
  const sets = session?.sets ?? [];
  const lastSet = sets[sets.length - 1];
  const now = tracker?.now ?? 0;
  const liveReps = live?.reps ?? [];
  let state = "idle";
  let restFrom = null;
  let shown = null;                                    // the set whose numbers are shown

  if (running && liveReps.length) {
    const s = { ...setSummary(seg?.lift ?? lift, liveReps, live.tAbs), index: sets.length, exercise: seg?.exercise ?? exercise };
    if (tracker.quietFor >= REST_AFTER && now - s.endT >= REST_AFTER) {
      state = "rest"; restFrom = s.endT;               // provisional: more reps soon = same set
    } else {
      state = "set";
    }
    shown = s;
  } else if (running && lastSet) {
    state = "rest"; restFrom = lastSet.endT; shown = lastSet;
  } else if (running) {
    state = "ready";
  }
  hero.dataset.state = state;

  const stats = (pairs) => pairs.forEach(([v, l], k) => {
    $(`stat${"ABCD"[k]}`).textContent = v;
    $(`label${"ABCD"[k]}`).textContent = l;
  });
  const what = (s) => `Set ${s.index + 1} · ${s.exercise}`;

  if (state === "set") {
    const last = liveReps[liveReps.length - 1];
    $("heroLabel").textContent = `${what(shown)} · live`;
    $("heroBig").innerHTML = `${last.mcv.toFixed(2)}<small>m/s</small>`;
    stats([[shown.nReps, "reps"], [(last.rom * 100).toFixed(0), "ROM cm"],
      [shown.nReps >= 2 ? `${Math.round(shown.velLoss * 100)}%` : "--", "vel. loss"], [formatRpe(shown.rpeEst), "RPE est."]]);
    $("heroSub").textContent = last.flags.includes("no end anchor") ? "Pause at the top to lock the last rep in." : "";
  } else if (state === "rest") {
    $("heroLabel").textContent = `Rest · after ${what(shown)}`;
    $("heroBig").textContent = formatTime(Math.max(0, now - restFrom));
    stats([[shown.nReps, "reps"], [shown.bestMcv.toFixed(2), "best MCV"],
      [`${Math.round(shown.velLoss * 100)}%`, "vel. loss"], [formatRpe(shown.rpeEst), "RPE est."]]);
    $("heroSub").textContent = tracker.inSegment && !liveReps.length ? "Movement… (not a set yet)" : "";
  } else if (state === "ready") {
    $("heroLabel").textContent = "Recording";
    $("heroBig").textContent = "Ready. Do your first set.";
    $("heroSub").textContent = `Next: ${exercise}${weight ? ` · ${weight} lb` : ""}. Hold still for a second before and after each set.`;
  } else if (sets.length) {
    const reps = sets.reduce((n, s) => n + s.nReps, 0);
    $("heroLabel").textContent = "Session done";
    $("heroBig").textContent = `${sets.length} set${sets.length === 1 ? "" : "s"} · ${reps} reps`;
    $("heroSub").textContent = session.autosave ? `Saved to Documents/BarTracker/${session.folder}/` : "";
  } else {
    $("heroLabel").textContent = "Session";
    $("heroBig").textContent = "Start a session";
    $("heroSub").textContent = "Clip the phone to the bar, tap Start, then just lift. Sets and rest are found automatically.";
  }

  $("clock").textContent = running ? `● ${formatTime(now)}` : "Not recording";
  $("clock").className = `pill ${running ? "live" : ""}`;
  $("sessionBar").hidden = !sets.length;
  if (sets.length) {
    const reps = sets.reduce((n, s) => n + s.nReps, 0);
    $("sessionSummary").textContent = `${sets.length} set${sets.length === 1 ? "" : "s"} · ${reps} reps`;
  }
  $("liveCard").hidden = state !== "set";
  if (state === "set") drawHeight();
}

// ---------- drawing ----------

// reps: the set's reps after the rack-move filter (not res.reps, which still has them)
function heightPlot(canvas, res, reps) {
  if (!res || res.error) { drawPlot(canvas, { series: [] }); return; }
  const { t, x, windows } = res;
  const cm = Array.from(x, (v) => v * 100);
  // Everything after the last still is provisional: its drift fix gets redone once
  // you pause. Gray and dashed.
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

function drawHeight() { heightPlot($("heightCanvas"), live?.res, live?.reps ?? []); }
function drawSetPlot(set) { heightPlot(set.card.querySelector(".set-canvas"), set.analysis, set.reps); }

function drawLive() {
  const canvas = $("liveCanvas");
  if (!recent.length) { drawPlot(canvas, { series: [] }); return; }
  const g = live?.res?.g ?? 9.81;
  const tEnd = recent[recent.length - 1].t;
  const xs = [], ys = [];
  for (const s of recent) {
    if (tEnd - s.t > LIVE_SECONDS) continue;
    xs.push(s.t - tEnd);
    ys.push(Math.sqrt(s.ax * s.ax + s.ay * s.ay + s.az * s.az) - g);
  }
  drawPlot(canvas, { series: [{ x: xs, y: ys, color: cssVar("--line") }], yLabel: "m/s²", minRange: 2 });
}

function updateFooter() {
  let rate = "--";
  if (source instanceof PhoneSource && source.interval) {
    rate = `${Math.round(1000 / source.interval)} Hz (interval ${source.interval.toFixed(1)} ms)`;
  } else if (recent.length > 10) {
    // Measured from the sample timestamps, so it's the real rate, not the requested one.
    const n = recent.length;
    rate = `${Math.round((n - 1) / (recent[n - 1].t - recent[0].t))} Hz`;
    if (source instanceof NativeSensorSource && source.info) rate += ` (${source.info.accel})`;
  }
  $("rate").textContent = sourceName ? `${sourceName} · ${rate}` : rate;
  $("samples").textContent = `${nSamples} samples`;
}

function showError(msg) {
  $("hint").textContent = msg;
  $("hint").classList.add("err");
}

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

// ---------- next set: exercise + weight (sticky) ----------

function setExercise(name) {
  exercise = name.trim() || "Phone test";
  lift = liftForExercise(exercise);
  $("exercise").value = exercise;
  $("profileHint").textContent = PROFILE_HINT[lift];
  if (seg) seg = { ...seg, exercise, lift };         // changed it mid-set: this set too
  try { localStorage.setItem("exercise", exercise); } catch { /* storage blocked */ }
  render();
}

function setWeight(value) {
  weight = String(value).trim();
  $("weight").value = weight;
  if (seg) seg.weight = weight;
  try { localStorage.setItem("weight", weight); } catch { /* storage blocked */ }
  render();
}

// Suggestions: names you've used (newest first), then the common ones.
let recents = [];
function rememberExercise(name) {
  recents = [name, ...recents.filter((r) => r.toLowerCase() !== name.toLowerCase())].slice(0, 12);
  try { localStorage.setItem("recentExercises", JSON.stringify(recents)); } catch { /* storage blocked */ }
  fillSuggestions();
}
function fillSuggestions() {
  const seen = new Set();
  $("exerciseList").innerHTML = [...recents, ...SUGGESTIONS]
    .filter((n) => !seen.has(n.toLowerCase()) && seen.add(n.toLowerCase()))
    .map((n) => `<option value="${n.replace(/"/g, "&quot;")}"></option>`).join("");
}

// ---------- wire up ----------

$("startBtn").addEventListener("click", startSession);
$("endBtn").addEventListener("click", endSession);
$("shareBtn").addEventListener("click", shareSession);
$("exercise").addEventListener("change", (e) => setExercise(e.target.value));
$("exercise").addEventListener("input", (e) => { $("profileHint").textContent = PROFILE_HINT[liftForExercise(e.target.value || "phone")]; });
$("weight").addEventListener("change", (e) => setWeight(e.target.value));
try { recents = JSON.parse(localStorage.getItem("recentExercises") || "[]"); } catch { recents = []; }
fillSuggestions();
let saved = {};
try { saved = { exercise: localStorage.getItem("exercise"), weight: localStorage.getItem("weight") }; } catch { /* storage blocked */ }
setExercise(saved.exercise || "Phone test");
setWeight(saved.weight || "");
$("fileInput").addEventListener("change", (e) => {
  const f = e.target.files[0];
  if (f) startReplay(f);
  e.target.value = "";
});
window.addEventListener("resize", () => { drawLive(); if (live) drawHeight(); });

// For checking the app over USB (android/pull-recording.mjs, DevTools): the session,
// and the last finished set as a CSV.
window.barDebug = {
  autosaveReplays: false,
  session: () => session,
  tracker: () => tracker,
  summary: () => (session ? summaryCSV() : ""),
  recording: () => {
    const s = session?.sets[session.sets.length - 1];
    return s ? { name: s.file, csv: toCSV(s.samples) } : { name: "none.csv", csv: "" };
  },
};
// Masthead date, like the top of a logbook page.
$("masthead").textContent = `Training log · ${new Date().toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" })}`;
render();
drawLive();
