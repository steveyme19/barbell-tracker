// UI wiring: source -> session tracker (sets.js) -> rep algorithm -> UI (decision D4).
// Session mode: start once, lift, and the tracker finds each set and the rest between.
import { PhoneSource, NativeSensorSource, ReplaySource } from "./sources.js";
import { isNativeApp, keepScreenOn, saveFile, deleteFile, shareFiles } from "./native.js";
import { analyze, magnitude, imuOf, liftFromName, liftForExercise, SETTINGS } from "./reps.js";
import { SessionTracker, setSummary, dropRackMoves, formatTime, formatRpe } from "./sets.js";
import { parseRecording, toCSV } from "./fileio.js";
import { askPersist, putSession, listSessions, putSetSamples, getSetSamples } from "./store.js";
import { drawPlot } from "./plot.js";

const $ = (id) => document.getElementById(id);
const ANALYZE_EVERY_MS = 500;   // at least this long between live passes
const LIVE_SECONDS = 8;
const REST_AFTER = 3;           // s of quiet after a rep before the card shows the rest timer
const DEFAULT_NAMES = { phone: "Phone test", squat: "Squat", bench: "Bench", deadlift: "Deadlift", machine: "Machine", free: "Free weight" };
const SUGGESTIONS = ["Squat", "Bench", "Deadlift", "Lat pulldown", "Cable row", "Leg extension", "Leg curl", "Chest press", "Phone test"];
// What the typed name means for tracking (liftForExercise D19, chooseLift D21).
const PROFILE_HINT = {
  phone: "Tracked as a hand-held phone test.",
  squat: "Tracked as a squat: lockouts level, RPE estimate on.",
  bench: "Tracked as a bench: chest and lockout level, RPE estimate on.",
  deadlift: "Tracked as a deadlift: floor and lockout level, RPE estimate on.",
  machine: "Not squat, bench or deadlift: the sensor checks machine vs free weight.",
  free: "Tracked as a free weight: lockouts only, no RPE estimate.",
};
const BLANK_HINT = "No name: the sensor checks machine vs free weight.";
const hintFor = (name) => (name.trim() ? PROFILE_HINT[liftForExercise(name)] : BLANK_HINT);
// What to call a set: its name, or what it was tracked as (machine / free weight).
const nameOf = (s) => s.exercise || DEFAULT_NAMES[s.lift] || "Set";
const native = isNativeApp();

let source = null;
let running = false;
let tracker = null;
let session = null;       // { stamp, folder, autosave, sets: [] }
let live = null;          // analysis of the set in progress: { res, reps, tAbs }
let recent = [];          // last LIVE_SECONDS of samples (live plot, rate)
let nSamples = 0;
let exercise = "";            // sticky: what you typed for the next set (blank = sensor decides)
let weight = "";              // sticky: weight for the next set, lb (text, may be empty)
let lift = "free";            // profile the name asks for (liftForExercise); blank = "free" if the sensor can't tell
let seg = null;               // { exercise, weight, lift } as they were when this set started moving
let buzzed = false;       // vibrated for the set in progress yet?
let vlTarget = 20;        // velocity-loss stop target, % (0 = off). Sticky.
let vlAlerted = false;    // buzzed for the target in this set yet?
let analyzeTimer = null;
let tickTimer = null;
let wakeLock = null;
let drawQueued = false;
let sourceName = "";

// ---------- incoming data ----------

function onSample(s) {
  session.t0Wall ??= Date.now();      // wall clock at this tracker's time 0 (rest across a resume)
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
  const named = seg?.lift ?? lift;
  const res = analyze(samples.map((s) => s.t),
    magnitude(samples.map((s) => s.ax), samples.map((s) => s.ay), samples.map((s) => s.az)),
    { ...SETTINGS, LIFT: named }, imuOf(samples));
  const used = res.error ? named : res.lift;        // the name, or what the sensor saw (D21)
  const reps = res.error ? [] : dropRackMoves(res.reps, used);
  live = { res, reps, tAbs, lift: used };
  if (reps.length && !buzzed) { buzz(150); buzzed = true; }   // a set started
  // Velocity-loss stop: last rep vs best rep in this set, same number the card shows.
  if (reps.length >= 2 && !vlAlerted && overTarget(setSummary(used, reps, tAbs).velLoss)) {
    buzz([400, 120, 400]);
    vlAlerted = true;
  }
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
  set.index = session.sets.length;               // continues the count after a resume
  set.wallStart = session.t0Wall + set.startT * 1000;
  set.wallEnd = session.t0Wall + set.endT * 1000;
  const prev = session.sets[session.sets.length - 1];
  if (prev?.restored && set.restBefore === null) set.restBefore = (set.wallStart - prev.wallEnd) / 1000;
  set.exercise = seg?.exercise ?? exercise;
  set.load = seg?.weight ?? weight;
  set.rpe = "";
  if (set.exercise) rememberExercise(set.exercise);
  live = null;
  buzzed = false;
  vlAlerted = false;
  seg = null;
  set.file = setFileName(set);
  session.sets.push(set);
  buzz([100, 80, 100]);                                  // a set finished
  addSetCard(set);
  if (session.autosave) saveSet(set).then(saveSummary).catch((err) => showError(`Couldn't save: ${err.message}`));
  if (session.persist) {
    putSetSamples(session.id, set.index, set.samples, set.tAbs).then(persistSession)
      .catch((err) => showError(`Couldn't store the set: ${err.message}`));
  }
  render();
}

function onDrop() {     // motion that wasn't a set (plates, a bump)
  live = null;
  buzzed = false;
  vlAlerted = false;
  seg = null;
  render();
}

// ---------- start / end ----------

function newSession({ autosave, persist = false, from = null }) {
  if (from) {
    // picking up a stored session: same id, folder and sets, a new tracker
    session = sessionFromRecord(from, { autosave, persist });
  } else {
    const d = new Date();
    const pad = (n) => String(n).padStart(2, "0");
    const stamp = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}`;
    // seconds in the id/folder: two sessions in the same minute must not overwrite each other
    const id = `session-${stamp}${pad(d.getSeconds())}`;
    session = { id, stamp, date: stamp.slice(0, 10), startedAt: d, folder: id,
      autosave, persist, ended: false, sets: [] };
  }
  tracker = new SessionTracker({
    liftFor: () => seg?.lift ?? lift,
    onSegmentStart: () => { seg = { exercise, weight, lift }; },
    onSet,
    onDrop,
  });
  live = null;
  recent = [];
  nSamples = 0;
  if (!from) $("setList").innerHTML = "";
  $("hint").textContent = "";
  $("hint").classList.remove("err");
}

async function startSession(from = null) {
  if (!window.isSecureContext) {
    showError("Motion sensors need HTTPS. Open the GitHub Pages link on your phone.");
    return;
  }
  $("resumeBar").hidden = true;
  if (from) {
    newSession({ autosave: native, persist: true, from });
    await restoreSets(from);
  } else {
    newSession({ autosave: native, persist: true });
  }
  session.ended = false;
  persistSession();
  askPersist();
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
  newSession({ autosave: native && Boolean(window.barDebug?.autosaveReplays), persist: Boolean(window.barDebug?.persistReplays) });
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
  session.ended = true;
  if (session.persist) await persistSession();
  render();
  renderHistory();
}

// ---------- stored sessions: history, resume ----------

const SET_FIELDS = ["index", "exercise", "load", "rpe", "file", "restBefore", "wallStart", "wallEnd",
  "lift", "nReps", "bestMcv", "lastMcv", "velLoss", "avgRom", "rpeEst"];

function sessionRecord() {
  return {
    id: session.id, stamp: session.stamp, date: session.date, folder: session.folder,
    startedAtMs: session.startedAt.getTime(), ended: Boolean(session.ended), autosave: session.autosave,
    sets: session.sets.map((s) => Object.fromEntries(SET_FIELDS.map((k) => [k, s[k] ?? null]))),
  };
}

function persistSession() {
  if (!session?.persist) return Promise.resolve();
  return putSession(sessionRecord()).catch((err) => showError(`Couldn't store the session: ${err.message}`));
}

function sessionFromRecord(rec, { autosave, persist }) {
  return { id: rec.id, stamp: rec.stamp, date: rec.date, folder: rec.folder, startedAt: new Date(rec.startedAtMs),
    autosave, persist, ended: rec.ended, sets: [] };
}

// Load a stored session's sets back in: raw samples from the store, re-analyzed (same
// pipeline, so the same reps), cards rebuilt. Rest and wall times come from the record.
async function restoreSets(rec) {
  $("setList").innerHTML = "";
  for (const r of rec.sets) {
    const raw = await getSetSamples(rec.id, r.index);
    if (!raw) continue;
    const set = { ...r, samples: raw.samples, tAbs: raw.tAbs, restored: true };
    const { res, used, reps } = analyzeSet(set.samples, set.exercise);
    if (!reps.length) continue;
    Object.assign(set, setSummary(used, reps, set.tAbs), { reps, analysis: res, restBefore: r.restBefore });
    session.sets.push(set);
    addSetCard(set);
  }
}

async function viewSession(rec) {
  if (running) return;
  session = sessionFromRecord(rec, { autosave: native && rec.autosave, persist: true });
  session.viewing = true;
  tracker = null;
  live = null;
  await restoreSets(rec);
  render();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

const fmtWhen = (ms) => new Date(ms).toLocaleString(undefined, { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });

async function renderHistory() {
  let all = [];
  try { all = (await listSessions()).filter((s) => s.sets.length); } catch { /* no IndexedDB */ }
  const list = $("historyList");
  list.innerHTML = all.length ? "" : '<p class="history-empty">No saved sessions yet.</p>';
  for (const rec of all) {
    const reps = rec.sets.reduce((n, s) => n + (s.nReps || 0), 0);
    const names = [...new Set(rec.sets.map((s) => s.exercise || DEFAULT_NAMES[s.lift] || "Set"))].slice(0, 3).join(", ");
    const b = document.createElement("button");
    b.className = "history-item";
    b.innerHTML = `<span>${fmtWhen(rec.startedAtMs)}${rec.ended ? "" : " (unfinished)"}</span><span>${rec.sets.length} sets · ${reps} reps · ${names}</span>`;
    b.addEventListener("click", () => viewSession(rec));
    list.appendChild(b);
  }
}

// A session that never got "End session" (app killed, phone died): offer to resume it.
async function checkUnfinished() {
  let all = [];
  try { all = await listSessions(); } catch { return; }
  const rec = all.find((s) => !s.ended);
  if (!rec) return;
  if (!rec.sets.length) {                     // nothing in it: just close it
    await putSession({ ...rec, ended: true }).catch(() => {});
    return;
  }
  const last = rec.sets[rec.sets.length - 1];
  $("resumeText").textContent = `Unfinished session from ${fmtWhen(rec.startedAtMs)}: ${rec.sets.length} sets, last one ${formatTime(Math.max(0, Date.now() - (last.wallEnd ?? rec.startedAtMs)) / 1000)} ago.`;
  $("resumeBar").hidden = false;
  $("resumeBtn").onclick = () => startSession(rec);
  $("closeOldBtn").onclick = async () => {
    await putSession({ ...rec, ended: true }).catch(() => {});
    $("resumeBar").hidden = true;
    renderHistory();
  };
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
  const m = set.analysis?.mount;                           // what the sensor saw (D21)
  const det = c.querySelector(".set-mount");
  det.textContent = m ? (m.kind === "machine" ? "machine" : "free weight") : "";
  det.title = m ? `sensor: turning ${m.rot.toFixed(1)} deg/s, sideways ${m.side.toFixed(2)}` : "";
  c.querySelector(".set-rest").textContent = set.restBefore === null ? "first set" : `rest ${formatTime(set.restBefore)}`;
  c.querySelector(".set-stats").innerHTML = [
    [set.nReps, "reps"], [set.bestMcv.toFixed(2), "best MCV"],
    [`${Math.round(set.velLoss * 100)}%`, "vel. loss", overTarget(set.velLoss)], [formatRpe(set.rpeEst), "RPE est."],
  ].map(([v, l, over]) => `<div><span${over ? ' class="over" title="over your velocity-loss target"' : ""}>${v}</span><label>${l}</label></div>`).join("");
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

// Typed the wrong exercise? Rename the set and re-run it: squat / bench / deadlift / phone
// pick their own profile, anything else (or blank) lets the sensor pick machine or free
// weight (D21). Fix the rest times around it and re-save under the new file name.
// One set through the pipeline, with the profile its name asks for (D21: the sensor picks
// for anything but squat / bench / deadlift / phone).
function analyzeSet(s, name) {
  const named = name ? liftForExercise(name) : "free";
  const res = analyze(s.map((x) => x.t), magnitude(s.map((x) => x.ax), s.map((x) => x.ay), s.map((x) => x.az)),
    { ...SETTINGS, LIFT: named }, imuOf(s));
  const used = res.error ? named : res.lift;
  return { res, used, reps: res.error ? [] : dropRackMoves(res.reps, used) };
}

async function changeSetExercise(set, name) {
  name = name.trim();
  if (name) rememberExercise(name);
  const { res, used, reps } = analyzeSet(set.samples, name);
  if (!reps.length) { showError("No reps with that lift setting; kept the old one."); fillSetCard(set); return; }
  const oldFile = set.file;
  Object.assign(set, setSummary(used, reps, set.tAbs), { reps, analysis: res, exercise: name });
  set.file = setFileName(set);
  const sets = session.sets;
  for (let k = Math.max(1, set.index); k <= Math.min(set.index + 1, sets.length - 1); k++) {
    sets[k].restBefore = sets[k].startT - sets[k - 1].endT;
    fillSetCard(sets[k]);
  }
  fillSetCard(set);
  render();
  persistSession();
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
  persistSession();
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
    const s = { ...setSummary(live.lift, liveReps, live.tAbs), index: sets.length, exercise: seg?.exercise ?? exercise };
    if (tracker.quietFor >= REST_AFTER && now - s.endT >= REST_AFTER) {
      state = "rest"; restFrom = s.endT;               // provisional: more reps soon = same set
    } else {
      state = "set";
    }
    shown = s;
  } else if (running && lastSet) {
    state = "rest"; shown = lastSet;
    // a set from before a resume lives on the old clock: count its rest in wall time
    restFrom = lastSet.restored ? now - (Date.now() - lastSet.wallEnd) / 1000 : lastSet.endT;
  } else if (running) {
    state = "ready";
  }
  hero.dataset.state = state;
  const alert = state === "set" && shown.nReps >= 2 && overTarget(shown.velLoss);
  hero.classList.toggle("alert", alert);

  const stats = (pairs) => pairs.forEach(([v, l], k) => {
    $(`stat${"ABCD"[k]}`).textContent = v;
    $(`label${"ABCD"[k]}`).textContent = l;
  });
  const what = (s) => `Set ${s.index + 1} · ${nameOf(s)}`;

  if (state === "set") {
    const last = liveReps[liveReps.length - 1];
    $("heroLabel").textContent = `${what(shown)} · live`;
    $("heroBig").innerHTML = `${last.mcv.toFixed(2)}<small>m/s</small>`;
    stats([[shown.nReps, "reps"], [(last.rom * 100).toFixed(0), "ROM cm"],
      [shown.nReps >= 2 ? `${Math.round(shown.velLoss * 100)}%` : "--", "vel. loss"], [formatRpe(shown.rpeEst), "RPE est."]]);
    $("heroSub").textContent = alert
      ? `Velocity loss ${Math.round(shown.velLoss * 100)}%, past your ${vlTarget}%: stop the set.`
      : last.flags.includes("no end anchor") ? "Pause at the top to lock the last rep in." : "";
  } else if (state === "rest") {
    $("heroLabel").textContent = `Rest · after ${what(shown)}`;
    $("heroBig").textContent = formatTime(Math.max(0, now - restFrom));
    stats([[shown.nReps, "reps"], [shown.bestMcv.toFixed(2), "best MCV"],
      [`${Math.round(shown.velLoss * 100)}%`, "vel. loss"], [formatRpe(shown.rpeEst), "RPE est."]]);
    $("heroSub").textContent = tracker.inSegment && !liveReps.length ? "Movement… (not a set yet)" : "";
  } else if (state === "ready") {
    $("heroLabel").textContent = "Recording";
    $("heroBig").textContent = "Ready. Do your first set.";
    $("heroSub").textContent = `Next: ${exercise || "any exercise (the sensor decides)"}${weight ? ` · ${weight} lb` : ""}. Hold still for a second before and after each set.`;
  } else if (sets.length) {
    const reps = sets.reduce((n, s) => n + s.nReps, 0);
    $("heroLabel").textContent = session.viewing ? `Past session · ${fmtWhen(session.startedAt.getTime())}` : "Session done";
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
  exercise = name.trim();                                  // blank: the sensor decides (D21)
  lift = exercise ? liftForExercise(exercise) : "free";
  $("exercise").value = exercise;
  $("profileHint").textContent = hintFor(exercise);
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

// Velocity-loss target, % of the set's best rep (0 = off). Sticky like the weight.
function setVlTarget(value) {
  const n = Number(value);
  vlTarget = Number.isFinite(n) && n > 0 ? Math.min(60, n) : 0;
  $("vlTarget").value = vlTarget || "";
  try { localStorage.setItem("vlTarget", String(vlTarget)); } catch { /* storage blocked */ }
  for (const s of session?.sets ?? []) fillSetCard(s);
  render();
}
const overTarget = (velLoss) => vlTarget > 0 && velLoss * 100 >= vlTarget;

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

$("startBtn").addEventListener("click", () => startSession());
$("endBtn").addEventListener("click", endSession);
$("shareBtn").addEventListener("click", shareSession);
$("exercise").addEventListener("change", (e) => setExercise(e.target.value));
$("exercise").addEventListener("input", (e) => { $("profileHint").textContent = hintFor(e.target.value); });
$("weight").addEventListener("change", (e) => setWeight(e.target.value));
$("vlTarget").addEventListener("change", (e) => setVlTarget(e.target.value));
try { recents = JSON.parse(localStorage.getItem("recentExercises") || "[]"); } catch { recents = []; }
fillSuggestions();
let saved = {};
try { saved = { exercise: localStorage.getItem("exercise"), weight: localStorage.getItem("weight") }; } catch { /* storage blocked */ }
// Real use is never hand-held (D21): start blank. A saved "Phone test" was only the old default.
setExercise(saved.exercise && saved.exercise !== "Phone test" ? saved.exercise : "");
setWeight(saved.weight || "");
let savedVl = null;
try { savedVl = localStorage.getItem("vlTarget"); } catch { /* storage blocked */ }
setVlTarget(savedVl ?? 20);
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
  persistReplays: false,      // store replayed sessions in history (testing)
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
renderHistory();
checkUnfinished();
