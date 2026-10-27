// UI wiring: source -> session tracker (sets.js) -> rep algorithm -> UI (decision D4).
// Session mode: start once, lift, and the tracker finds each set and the rest between.
import { PhoneSource, NativeSensorSource, ReplaySource, PodSource, PodWebSource, PodNativeSource } from "./sources.js";
import { isNativeApp, keepScreenOn, saveFile, deleteFile, shareFiles, recordInBackground, askNotifications, vibrateNative } from "./native.js";
import { analyze, magnitude, imuOf, liftFromName, liftForExercise, SETTINGS } from "./reps.js";
import { SessionTracker, setSummary, dropRackMoves, closedSetReps, formatTime, formatRpe } from "./sets.js";
import { gapsNear, gapText } from "./gaps.js";
import { parseRecording, toCSV } from "./fileio.js";
import { askPersist, putSession, getSession, listSessions, putSetSamples, getSetSamples, getSessionData, deleteSession,
  deleteSetSamples, importSessions, COLS } from "./store.js";
import { toExport, fromExport } from "./export.js";
import { fitProfile } from "./lv.js";
import { fitCalibration, applyCalibration, CalibrationRun, G_LOCAL } from "./calib.js";
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
let lastHiddenRun = -Infinity;   // data time of the last live analysis while the screen was off
let analyzeTimer = null;
let tickTimer = null;
let wakeLock = null;
let drawQueued = false;
let sourceName = "";
let sourceKind = "phone";     // sticky: "phone" or "pod" (localStorage "source")
let podDevice = null;         // the browser: the pod picked with Connect pod (a page reload needs one more tap)
let podName = "";             // the app: the last pod its plugin connected to (it remembers the pod itself)
let podPickNew = false;       // the app: the next Start picks the nearest pod instead of the saved one
let ending = false;           // End is waiting for the source to stop (the pod's STOP handshake)
let pendingSeg = null;        // a next-set edit typed between two slow reps: { n: reps then, v: the edit }
let starting = false;         // a source is starting (the pod takes 1 to 10 s): no other session meanwhile

// ---------- incoming data ----------

function onSample(s) {
  // wall clock at this tracker's time 0 (rest across a resume). The pod knows when each sample
  // was taken; its first ones are the 2.8 s lead-in from before Start, so ask it.
  session.t0Wall ??= source?.wallAt ? source.wallAt(s.t) - s.t * 1000 : Date.now();
  tracker.push(s);
  nSamples++;
  recent.push(s);
  if (recent.length > 50 && s.t - recent[0].t > LIVE_SECONDS + 1) {
    recent = recent.filter((x) => s.t - x.t <= LIVE_SECONDS);
  }
  // Screen off: timers stop, sensor batches don't. Run the live analysis from the data so
  // the set-start and velocity-loss buzzes still happen in your pocket.
  if (document.hidden && running && s.t - lastHiddenRun >= ANALYZE_EVERY_MS / 1000) {
    lastHiddenRun = s.t;
    runLive();
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
  // another rep came after an edit held between reps: it was mid-set, so the edit is this set's too
  if (pendingSeg && seg && reps.length > pendingSeg.n) { seg = { ...seg, ...pendingSeg.v }; pendingSeg = null; }
  // Velocity loss over finished reps: a last rep no still has locked in yet may be half a
  // concentric on raw drift (35% live on a set whose card ends at 0%), and the buzz fires once per
  // set. Only that one: the reps before it in an unanchored window are finished and their lockouts
  // leveled (6b), so a no-pause set still buzzes mid-set, one rep after the crossing.
  const last = reps[reps.length - 1];
  const locked = last && last.flags.includes("no end anchor") ? reps.slice(0, -1) : reps;
  live = { res, reps, tAbs, lift: used, velLoss: locked.length >= 2 ? setSummary(used, locked, tAbs).velLoss : null };
  if (reps.length && !buzzed) { buzz(150); buzzed = true; }   // a set started
  // Velocity-loss stop: last locked rep vs best rep in this set, the number the card will show.
  if (live.velLoss !== null && !vlAlerted && overTarget(live.velLoss)) {
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
  // continues the count after a resume, past every stored index (a restore can skip a set: its
  // index must not be reused, or the new set overwrites its CSV and stored samples)
  set.index = session.sets.reduce((m, s) => Math.max(m, s.index + 1), 0);
  set.wallStart = session.t0Wall + set.startT * 1000;
  set.wallEnd = session.t0Wall + set.endT * 1000;
  const prev = session.sets[session.sets.length - 1];
  if (prev?.restored && set.restBefore === null) set.restBefore = (set.wallStart - prev.wallEnd) / 1000;
  set.exercise = seg?.exercise ?? exercise;
  set.load = seg?.weight ?? weight;
  set.rpe = "";
  if (set.exercise) rememberExercise(set.exercise);
  live = null;
  pendingSeg = null;
  buzzed = false;
  vlAlerted = false;
  seg = null;
  set.file = setFileName(set);
  set.gaps = setGaps(set);
  session.sets.push(set);
  buzz([100, 80, 100]);                                  // a set finished
  addSetCard(set);
  for (const s of session.sets) if (s !== set) fillSetCard(s);   // a new point for their profiles
  // the saves belong to this session and source, whatever the app shows by the time they run
  const ses = session, src = source;
  if (ses.autosave) saveSet(set, ses).then(() => saveSummary(ses)).then(() => saveLink(ses, src))
    .catch((err) => showError(`Couldn't save: ${err.message}`));
  if (ses.persist) {
    putSetSamples(ses.id, set.index, set.samples, set.tAbs).then(() => persistSession(ses))
      .catch((err) => showError(`Couldn't store the set: ${err.message}`));
  }
  render();
}

function onDrop() {     // motion that wasn't a set (plates, a bump)
  live = null;
  pendingSeg = null;
  buzzed = false;
  vlAlerted = false;
  seg = null;
  render();
}

// ---------- start / end ----------

function newSession({ autosave, persist = false, from = null }) {
  pendingSeg = null;
  if (from) {
    // picking up a stored session: same id, folder and sets, a new tracker
    session = sessionFromRecord(from, { autosave, persist });
    session.part += 1;                               // a new part: its own pod.json / link.json
  } else {
    const d = new Date();
    const pad = (n) => String(n).padStart(2, "0");
    const stamp = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}`;
    // seconds in the id/folder: two sessions in the same minute must not overwrite each other
    const id = `session-${stamp}${pad(d.getSeconds())}`;
    session = { id, stamp, date: stamp.slice(0, 10), startedAt: d, folder: id,
      autosave, persist, ended: false, sets: [], part: 1 };
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
  lastHiddenRun = -Infinity;   // every new source starts its t again (the pod at 0)
  if (!from) $("setList").innerHTML = "";
  $("hint").textContent = "";
  $("hint").classList.remove("err");
}

async function startSession(from = null) {
  const pod = sourceKind === "pod";
  if (pod && !podDevice && !native) {
    showError("Tap Connect pod first (after a page reload the browser asks once more).");
    return;
  }
  if (!window.isSecureContext) {
    showError(pod ? "Bluetooth needs HTTPS (or localhost)." : "Motion sensors need HTTPS. Open the GitHub Pages link on your phone.");
    return;
  }
  if (running || starting || ending) return;
  starting = true;
  $("resumeBar").hidden = true;
  if (from) {
    // the stored record, not the offer's snapshot: edits since, and a delete since (then no resume:
    // a new part under the deleted id would overwrite the set files Delete promised to keep)
    const fresh = await getSession(from.id).catch(() => null);   // undefined = gone, null = read failed
    if (fresh === undefined) {
      starting = false;
      showError("That session was deleted, so it can't be resumed.");
      return;
    }
    if (fresh) from = fresh;
  }
  // A calibration still collecting would keep listening to the session's sensors, and its end
  // (BarSensors.stop) would cut the session's data off unnoticed: cancel it, and wait for the stop.
  if (calRun) await stopCalibration("Calibration cancelled: a session started.");
  for (const id of ["startBtn", "podBtn", "phoneBtn", "fileInput", "calBtn"]) $(id).disabled = true;
  // Screen-off recording (RecordingService) and the screen-on flag, from the tap on: Android
  // only allows starting the service while the app is in front, and a resume's restore and the
  // pod's connect can take seconds (the connect up to 30 s), so a phone locked meanwhile must not
  // stall the start. Refused: the session still runs with the screen on. A pod session leaves the
  // notification prompt for after its connect.
  let fgsError = "";
  if (native) {
    keepScreenOn(true).catch(() => {});
    recordInBackground(true, { ask: !pod }).catch((err) => {
      fgsError = `Screen-off recording didn't start, keep the screen on this session. (${err.message})`;
      showError(fgsError);
    });
  }
  if (from) {
    newSession({ autosave: native, persist: true, from });
    await restoreSets(from);
  } else {
    newSession({ autosave: native, persist: true });
  }
  if (pod) {
    // The pod: raw counts scaled by its INFO, no phone calibration (D27, until roadmap 1.3). In
    // the app, the PodLink plugin (it finds and remembers the pod itself, and keeps the link with
    // the screen off); in a browser, Web Bluetooth.
    const opts = { onEvent: onPodEvent, capture: Boolean(window.barDebug?.podCapture) };
    source = native ? new PodNativeSource(onSample, { ...opts, any: podPickNew }) : new PodWebSource(onSample, podDevice, opts);
    sourceName = native ? "pod" : `pod ${source.name}`;
    $("hint").textContent = native ? "Connecting to the pod… (the first time, allow Nearby devices)" : "Connecting to the pod…";
  } else {
    // In the Android app, the native plugin gives full-rate sensors (A2). Same samples.
    sourceName = native ? "phone (native)" : "phone";
    // Calibrated acceleration for live sensors (D23); replays are played as recorded.
    const feed = (s) => onSample(applyCalibration(s, cal));
    source = native ? new NativeSensorSource(feed) : new PhoneSource(feed);
  }
  const mine = source;
  try {
    await mine.start();
  } catch (err) {
    starting = false;
    if (native) {
      recordInBackground(false).catch(() => {});
      keepScreenOn(false).catch(() => {});
    }
    for (const id of ["startBtn", "podBtn", "phoneBtn", "fileInput"]) $(id).disabled = false;
    showSource();
    if (from) $("resumeBar").hidden = false;       // the resume offer stays: try again
    render();
    showError(err.message);
    return;
  }
  starting = false;
  // Stored only once it runs: a failed start must not leave an empty "unfinished" session
  // that hides the real one from the resume offer.
  session.ended = false;
  persistSession();
  askPersist();
  if (pod) {
    $("hint").textContent = "";
    if (fgsError) showError(fgsError);              // the connect's hint covered it: it still holds
    sourceName = `pod ${source.name}`;                // the app's plugin knows the name only now
    if (native) {
      podName = source.name;
      podPickNew = false;
      try { localStorage.setItem("podName", podName); } catch { /* storage blocked */ }
      showSource();
      askNotifications().catch(() => {});             // after Nearby devices, never at the same time
    }
  }
  // What the samples were scaled with, next to the set files, so the raw readings can be recovered.
  if (pod && session.autosave) saveFile(session.folder, partName("pod"), JSON.stringify(source.linkInfo(), null, 1)).catch(() => {});
  else if (cal && session.autosave) saveFile(session.folder, partName("calibration"), JSON.stringify(cal, null, 1)).catch(() => {});
  begin();
  if (!pod) {
    setTimeout(() => {
      if (running && nSamples === 0) showError("No sensor data. Open this page on your phone (laptops have no motion sensor).");
    }, 2000);
  }
}

// What the pod's stream reports while it runs. A lost link reconnects by itself (both pod sources):
// the banner says so, and the set cards flag any data it couldn't get back.
function onPodEvent(kind, info) {
  if (kind === "fault") showError(`Pod: ${info}.`);
  if (kind === "link" || kind === "sleep") showPodBanner();
}

// What wakes the pod, by its sleep reason (docs/ble.md 3): motion, or only USB / reset (IMU off)
const WAKE_HINT = { idle: "bump the bar to wake it", "lid-down": "bump the bar to wake it",
  "low battery": "charge it over USB", "IMU lost": "plug in USB or press its reset button" };

/** The banner above the big card: the pod's link down (reconnecting), asleep, or battery low. */
function showPodBanner() {
  const b = $("podBanner");
  const L = running && source instanceof PodSource ? source.linkInfo() : null;
  let text = "";
  if (L && !L.linked && L.asleep) {
    // "command": our SLEEP op says which (mode 1 = IMU off); a USB s / o doesn't, assume motion
    const hint = L.asleep === "command" ? WAKE_HINT[L.sleepMode === 1 ? "IMU lost" : "idle"]
      : WAKE_HINT[L.asleep] ?? "bump the bar or plug in USB";
    text = `Pod asleep (${L.asleep}): ${hint}.`;
  }
  else if (L && !L.linked) text = "Pod link lost, reconnecting (the pod buffers 38 s).";
  else if (L?.battLow) text = "Pod battery low: charge it after this session.";
  b.hidden = !text;
  b.textContent = text;
  b.classList.toggle("bad", Boolean(L?.battLow) && L.linked);
}

// ---------- data gaps on the set cards (step 10, gaps.js) ----------

/** The flags for a set that just closed, from the pod's decoder (nothing for the phone). */
function setGaps(set) {
  if (!(source instanceof PodSource) || !tracker || tracker.t0 === null) return [];
  return gapsNear(set, { gaps: source.stream.gaps, epochs: source.stream.epochs, odr: source.stream.ep?.odr ?? 208,
    t0: tracker.t0, lastT: source.stream.last_t, endedLinkDown: source.closed && source.linkLost });
}

// ---------- the source: the phone's sensors or the pod ----------

async function connectPod() {
  if (running || starting) return;
  // the app: nothing to pick, the plugin finds the saved pod at Start. Tapped again while the pod is
  // the source: the next Start picks the nearest pod instead (a new or second pod).
  if (native) {
    if (sourceKind === "pod") podPickNew = !podPickNew;
    setSourceKind("pod");
    return;
  }
  if (!window.isSecureContext) { showError("Bluetooth needs HTTPS (or localhost)."); return; }
  try {
    podDevice = await PodWebSource.pick();           // the browser's chooser: needs this click
    setSourceKind("pod");
    $("hint").textContent = "";
    $("hint").classList.remove("err");
  } catch (err) {
    // Chrome says NotFoundError both for a cancelled chooser and for no Bluetooth adapter
    if (!(err.name === "NotFoundError" && /cancel/i.test(err.message))) showError(err.message);
  }
}

function setSourceKind(kind) {
  if (running || starting) return;
  sourceKind = kind;
  try { localStorage.setItem("source", kind); } catch { /* storage blocked */ }
  showSource();
}

function showSource() {
  const pod = sourceKind === "pod";
  const name = native ? (podPickNew ? "the nearest one" : podName) : podDevice?.name;
  $("podBtn").textContent = pod && name ? `Pod: ${name}` : pod && native ? "Pod" : "Connect pod";
  $("podBtn").classList.toggle("on", pod && (native || Boolean(podDevice)));
  $("phoneBtn").hidden = !pod;
  // The 6-face calibration is the phone's (D23); the pod's comes with roadmap 1.3.
  $("calBtn").disabled = pod && !calRun;
  $("calBtn").title = pod ? "Phone only: the pod's calibration comes with roadmap 1.3" : "";
}

async function startReplay(file) {
  if (running || starting || ending) return;
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
  $("podBtn").disabled = true;
  $("phoneBtn").disabled = true;
  scheduleAnalysis();
  tickTimer = setInterval(render, 1000);                 // the rest timer
  keepAwake();
  render();
}

async function endSession() {
  if (!running) return;
  running = false;
  ending = true;               // no new session or a past one opened until End's saves are done
  try {
    await finishSession(session, source);
  } finally {
    ending = false;
  }
}

async function finishSession(ses, src) {
  try {
    await src?.stop();         // native source: wait for its last batch
  } catch { /* the source is gone either way: still close the session */ }
  clearTimeout(analyzeTimer);
  clearInterval(tickTimer);
  tracker.finish();            // a set still in progress gets closed (and saved)
  live = null;
  $("endBtn").disabled = true;
  wakeLock?.release().catch(() => {});
  wakeLock = null;
  if (native) {
    keepScreenOn(false).catch(() => {});
    recordInBackground(false).catch(() => {});
  }
  if (ses.autosave && ses.sets.length) await saveSummary(ses).catch(() => {});
  if (ses.autosave) await saveLink(ses, src).catch(() => {});
  if (src instanceof PodSource) ses.podLink = src.linkJSON();   // barDebug.link() after the end
  showPodBanner();
  ses.ended = true;
  if (ses.persist) await persistSession(ses);
  // only now: a Start or a past session opened during the saves above would cross them
  $("startBtn").disabled = false;
  $("fileInput").disabled = false;
  $("podBtn").disabled = false;
  $("phoneBtn").disabled = false;
  showSource();                // Calibrate again (phone source only)
  render();
  renderHistory();
}

// ---------- stored sessions: history, resume ----------

const SET_FIELDS = ["index", "exercise", "load", "rpe", "file", "restBefore", "wallStart", "wallEnd",
  "lift", "nReps", "bestMcv", "lastMcv", "velLoss", "avgRom", "rpeEst", "gaps"];

function sessionRecord(ses = session) {
  return {
    id: ses.id, stamp: ses.stamp, date: ses.date, folder: ses.folder, parts: ses.part ?? 1,
    startedAtMs: ses.startedAt.getTime(), ended: Boolean(ses.ended), autosave: ses.autosave,
    sets: ses.sets.map((s) => Object.fromEntries(SET_FIELDS.map((k) => [k, s[k] ?? null]))),
  };
}

function persistSession(ses = session) {
  if (!ses?.persist) return Promise.resolve();
  // the history list (and historyCache, which the load-velocity profiles read) follows the edit
  return putSession(sessionRecord(ses)).then(() => { if (!running && !starting && !ending) renderHistory(); })
    .catch((err) => showError(`Couldn't store the session: ${err.message}`));
}

function sessionFromRecord(rec, { autosave, persist }) {
  return { id: rec.id, stamp: rec.stamp, date: rec.date, folder: rec.folder, startedAt: new Date(rec.startedAtMs),
    autosave, persist, ended: rec.ended, sets: [], part: rec.parts ?? 1 };
}

// Load a stored session's sets back in: raw samples from the store, re-analyzed (same
// pipeline, so the same reps), cards rebuilt. Rest and wall times come from the record.
async function restoreSets(rec) {
  const ses = session;
  $("setList").innerHTML = "";
  for (const r of rec.sets) {
    const raw = await getSetSamples(rec.id, r.index);
    if (session !== ses) return;              // the app moved on (Start, another past session)
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
  if (running || starting || ending) return;
  // the stored record, not the one this button was made with (edits since are in the store)
  rec = (await getSession(rec.id).catch(() => null)) ?? rec;
  if (running || starting || ending) return;
  session = sessionFromRecord(rec, { autosave: native && rec.autosave, persist: true });
  session.viewing = true;
  tracker = null;
  live = null;
  await restoreSets(rec);
  render();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

const fmtWhen = (ms) => new Date(ms).toLocaleString(undefined, { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });

let historyCache = [];          // stored sessions, for the load-velocity profile

async function renderHistory() {
  let all = [];
  try { all = (await listSessions()).filter((s) => s.sets.length); } catch { /* no IndexedDB */ }
  historyCache = all;
  for (const s of session?.sets ?? []) fillSetCard(s);   // profiles may have more data now
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
  $("resumeBar").dataset.id = rec.id;
  $("resumeBtn").onclick = () => startSession(rec);
  $("closeOldBtn").onclick = async () => {
    $("resumeBar").hidden = true;
    // the stored record: the page-load one would undo edits and "Not a set" since, or bring a
    // deleted session back as a ghost with no sets to open
    const cur = await getSession(rec.id).catch(() => null);
    if (cur && !cur.ended) await putSession({ ...cur, ended: true }).catch(() => {});
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

// barDebug.buzzes(): when each buzz went out and whether the screen was off (the step 12 soak's check)
const buzzLog = [];

function buzz(pattern) {
  buzzLog.push({ at: Date.now(), hidden: document.hidden, pattern });
  if (buzzLog.length > 500) buzzLog.shift();
  if (native) { vibrateNative(pattern).catch(() => {}); return; }   // works with the screen off
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

async function saveSet(set, ses = session) {
  const { uri } = await saveFile(ses.folder, set.file, toCSV(set.samples));
  return uri;
}

function summaryCSV(ses = session) {
  const head = "set,date,start,file,exercise,lift,load_lb,reps,rpe,rpe_est,rest_before_s,best_mcv,last_mcv," +
    "vel_loss_pct,avg_rom_cm,rep_mcvs,to_failure,mount,paused_lockout,notes";
  const rows = ses.sets.map((s, k) => {
    // wall time when known: after a resume startT is on the new part's clock
    const start = new Date(s.wallStart ?? ses.startedAt.getTime() + s.startT * 1000).toTimeString().slice(0, 8);
    return [k + 1, ses.date, start, s.file, csvField(s.exercise), s.lift, csvField(s.load), s.nReps, csvField(s.rpe),
      s.rpeEst ? (s.rpeEst[0] === 5 ? `<=${s.rpeEst[1]}` : `${s.rpeEst[0]}-${s.rpeEst[1]}`) : "",
      s.restBefore === null ? "" : Math.round(s.restBefore),
      s.bestMcv.toFixed(3), s.lastMcv.toFixed(3), Math.round(s.velLoss * 100), (s.avgRom * 100).toFixed(1),
      s.reps.map((r) => r.mcv.toFixed(2)).join(" "), "", "", "", csvField(gapText(s))].join(",");
  });
  return [head, ...rows].join("\n") + "\n";
}

async function saveSummary(ses = session) {
  return (await saveFile(ses.folder, "session.csv", summaryCSV(ses))).uri;
}

// link.json next to the set files: the pod's link story (epochs, gaps, reconnects; P2).
async function saveLink(ses = session, src = source) {
  if (!(src instanceof PodSource) || !ses?.autosave) return null;
  return (await saveFile(ses.folder, partName("link", ses), JSON.stringify(src.linkJSON(), null, 1))).uri;
}

// A resumed session runs in parts, each with its own source: pod.json, then pod-2.json, ...
const partName = (base, ses = session) => (ses.part > 1 ? `${base}-${ses.part}.json` : `${base}.json`);

function download(name, text, type = "text/csv") {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([text], { type }));
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

// ---------- export, import, delete (roadmap 5.1; the format: export.js, D30) ----------
// In the app a session's export is session.json in its own Documents/BarTracker folder, next to its
// CSVs (a pulled folder is then complete); in a browser it's a download. Import adds what isn't
// stored yet. Delete takes a past session out of IndexedDB; its files stay (the real backup).

// One session's raw columns in memory at a time (months of sets are hundreds of MB). A browser's
// download goes in pieces under FILE_CHARS: one string can't pass V8's limit (2^29 characters,
// about 56 sessions), and Import reads each file back with file.text().
const FILE_CHARS = 2 ** 27;

async function exportSessions(ids) {
  const done = [], files = [[]];
  let size = 0;
  for (const id of ids) {
    const d = await getSessionData(id);
    if (!d) continue;
    done.push(d);
    if (native) { await saveFile(d.record.folder, "session.json", JSON.stringify(toExport([d], COLS))); continue; }
    const entry = JSON.stringify(toExport([d], COLS).sessions[0]);
    if (size && size + entry.length > FILE_CHARS) { files.push([]); size = 0; }
    files.at(-1).push(new Blob([entry]));                 // out of the JS heap at once
    size += entry.length;
    d.rows = null;                                       // the typed arrays can go now
  }
  if (!native && done.length) {
    const head = JSON.stringify(toExport([], COLS)).slice(0, -2);   // '{"format":...,"sessions":['
    const day = new Date().toISOString().slice(0, 10);
    files.forEach((parts, k) => {
      const name = done.length === 1 ? `${done[0].record.folder}.json`
        : `bartracker-export-${day}${files.length > 1 ? `-${k + 1}` : ""}.json`;
      download(name, new Blob([head, ...parts.flatMap((p, i) => (i ? [",", p] : [p])), "]}"]), "application/json");
    });
  }
  return done;
}

async function exportShown() {
  if (!session?.sets.length || running || starting || ending) return;
  try {
    const done = await exportSessions([session.id]);
    if (!done.length) { showError("This session isn't stored (a replay?), so there's nothing to export."); return; }
    showMessage(native ? `Exported to Documents/BarTracker/${session.folder}/session.json` : "Exported.");
  } catch (err) {
    showError(`Couldn't export: ${err.message}`);
  }
}

async function exportAll() {
  try {
    const ids = (await listSessions()).filter((s) => s.sets.length).map((s) => s.id);
    if (!ids.length) { showError("No stored sessions to export."); return; }
    const done = await exportSessions(ids);
    showMessage(native ? `Exported ${done.length} sessions: session.json in each one's Documents/BarTracker folder.`
      : `Exported ${done.length} sessions.`);
  } catch (err) {
    showError(`Couldn't export: ${err.message}`);
  }
}

async function importFile(file) {
  try {
    // an import is history, not a live session: never offered for resume
    const list = fromExport(JSON.parse(await file.text()), COLS).map(({ record, rows }) => ({ record: { ...record, ended: true }, rows }));
    const { added, skipped } = await importSessions(list);
    showMessage(`Imported ${added.length} session${added.length === 1 ? "" : "s"}` +
      (skipped.length ? ` (${skipped.length} already here, left as they are).` : "."));
    renderHistory();
  } catch (err) {
    showError(`Couldn't import ${file.name}: ${err.message}`);
  }
}

async function deleteShown() {
  if (!session?.viewing || running || starting || ending) return;
  if (!confirm(`Delete the session from ${fmtWhen(session.startedAt.getTime())} from the app? Its set files in Documents/BarTracker stay.`)) return;
  try {
    const id = session.id;
    await deleteSession(id);
    if ($("resumeBar").dataset.id === id) $("resumeBar").hidden = true;   // its offer is for a session that's gone
    session = null;
    $("setList").innerHTML = "";
    render();
    renderHistory();
    showMessage("Session deleted.");
  } catch (err) {
    showError(`Couldn't delete: ${err.message}`);
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

// "Not a set": a phantom no threshold can tell from a real single (the pod moved to another bar
// mid-session, D31), or anything else that shouldn't count. Out of the session and the live tracker
// (it times the next rest from its last set), its stored samples and CSV gone, and the set after it
// gets its rest from the set before (wall time: a resumed session's parts run on their own clocks).
async function dropSet(set) {
  if (!confirm(`Take Set ${set.index + 1} (${nameOf(set)}) out of this session? Its CSV file goes too.`)) return;
  const sets = session.sets, p = sets.indexOf(set);
  if (p < 0) return;
  sets.splice(p, 1);
  const k = tracker?.sets?.indexOf(set) ?? -1;
  if (k >= 0) tracker.sets.splice(k, 1);
  set.card.remove();
  const prev = sets[p - 1], next = sets[p];
  if (next) {
    next.restBefore = !prev ? null
      : prev.wallEnd != null && next.wallStart != null ? (next.wallStart - prev.wallEnd) / 1000 : next.startT - prev.endT;
  }
  for (const s of sets) fillSetCard(s);                 // the next rest, and the profiles lost a point
  render();
  persistSession();
  deleteSetSamples(session.id, set.index).catch(() => {});
  if (session.autosave) {
    try {
      await deleteFile(session.folder, set.file);
      await saveSummary();
    } catch (err) {
      showError(`Couldn't update the files: ${err.message}`);
    }
  }
  showMessage(`Set ${set.index + 1} taken out.`);
}

function addSetCard(set) {
  const card = $("setCardTpl").content.firstElementChild.cloneNode(true);
  set.card = card;
  card.querySelector(".set-exercise").addEventListener("change", (e) => changeSetExercise(set, e.target.value));
  card.querySelector(".set-load").value = set.load;
  card.querySelector(".set-load").addEventListener("change", (e) => {
    set.load = e.target.value;
    for (const s of session.sets) fillSetCard(s);        // the profile changed
    saveSummaryLater();
  });
  card.querySelector(".set-rpe").addEventListener("change", (e) => { set.rpe = e.target.value; saveSummaryLater(); });
  card.querySelector(".set-save").addEventListener("click", () => shareSet(set));
  card.querySelector(".set-drop").addEventListener("click", () => dropSet(set));
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
  c.querySelector(".set-lv").innerHTML = lvLine(set);
  const gap = c.querySelector(".set-gap");
  gap.textContent = gapText(set);
  gap.hidden = !gap.textContent;
  // the sticking region (analytics.md section 4): where the reps that dipped slowed, % of their ROM
  const stuck = set.reps.filter((r) => r.sticking);
  if (stuck.length) {
    const at = stuck.map((r) => r.sticking.pos * 100).sort((a, b) => a - b)[Math.floor(stuck.length / 2)];
    c.querySelector(".set-lv").insertAdjacentHTML("beforeend",
      `<div class="set-stick">Sticks at ~${Math.round(at)}% of the way up (${stuck.length} of ${set.reps.length} reps)</div>`);
  }
  const tbody = c.querySelector("tbody");
  tbody.innerHTML = "";
  set.reps.forEach((r, k) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${k + 1}</td><td>${(r.rom * 100).toFixed(1)} cm</td>` +
      `<td>${r.mcv.toFixed(2)}</td><td>${r.peak.toFixed(2)}</td><td>${r.dur.toFixed(2)} s</td>` +
      `<td>${r.sticking ? `${Math.round(r.sticking.pos * 100)}%` : ""}</td>` +
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
  return { res, used, reps: res.error ? [] : closedSetReps(res.reps, used) };   // a stored set: closed (D31)
}

async function changeSetExercise(set, name) {
  name = name.trim();
  if (name) rememberExercise(name);
  const { res, used, reps } = analyzeSet(set.samples, name);
  if (!reps.length) { showError("No reps with that lift setting; kept the old one."); fillSetCard(set); return; }
  const oldFile = set.file;
  const oldS = set.startT, oldE = set.endT;
  Object.assign(set, setSummary(used, reps, set.tAbs), { reps, analysis: res, exercise: name });
  if (set.wallStart != null) { set.wallStart += (set.startT - oldS) * 1000; set.wallEnd += (set.endT - oldE) * 1000; }
  set.file = setFileName(set);
  // the rests next to it, in wall time: a resumed session's parts each run on their own clock
  // (startT restarts at 0), and its array position, not set.index (a restore may skip a set)
  const sets = session.sets, p = sets.indexOf(set);
  for (let k = Math.max(1, p); k <= Math.min(p + 1, sets.length - 1); k++) {
    const a = sets[k - 1], b = sets[k];
    b.restBefore = a.wallEnd != null && b.wallStart != null ? (b.wallStart - a.wallEnd) / 1000 : b.startT - a.endT;
    fillSetCard(b);
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

// ---------- load-velocity profile (roadmap 4.3) ----------
// Points: best-rep MCV vs weight for sets with the same exercise name, last LV_DAYS of
// history plus this session. Unnamed sets can't be matched, so they're left out.

const LV_DAYS = 60;
const sameName = (a, b) => a && b && a.trim().toLowerCase() === b.trim().toLowerCase();

function lvPoints(name, skip = null) {
  const pts = [];
  const since = Date.now() - LV_DAYS * 864e5;
  for (const rec of historyCache) {
    if (rec.id === session?.id || rec.startedAtMs < since) continue;
    for (const s of rec.sets) if (sameName(s.exercise, name) && Number(s.load) > 0) pts.push([Number(s.load), s.bestMcv]);
  }
  for (const s of session?.sets ?? []) {
    if (s !== skip && sameName(s.exercise, name) && Number(s.load) > 0) pts.push([Number(s.load), s.bestMcv]);
  }
  return pts;
}

/** e1RM and "usual speed at this weight" for a set card, as HTML (empty if no profile). */
function lvLine(set) {
  if (!set.exercise || !(Number(set.load) > 0)) return "";
  const all = fitProfile(lvPoints(set.exercise), set.lift);
  const others = fitProfile(lvPoints(set.exercise, set), set.lift);     // baseline without this set
  const parts = [];
  if (all?.e1rm) parts.push(`e1RM ≈ <b>${Math.round(all.e1rm)} lb</b>`);
  if (others) {
    const usual = others.a + others.b * Number(set.load);
    const d = set.bestMcv - usual;
    parts.push(`usual at ${set.load} lb: ${usual.toFixed(2)} m/s (this set ${d >= 0 ? "+" : ""}${d.toFixed(2)})`);
  }
  if (!parts.length) return "";
  const p = all ?? others;
  return `<span title="${p.n} sets at ${p.loads} weights, R² ${p.r2.toFixed(2)}">${parts.join(" · ")}</span>`;
}

// ---------- the big card ----------

function render() {
  const hero = $("hero");
  const sets = session?.sets ?? [];
  const lastSet = sets[sets.length - 1];
  let now = tracker?.now ?? 0;
  // The pod's samples carry wall time (t follows it across outages and epochs), so the rest keeps
  // counting while the link is down or the pod sleeps, and doesn't jump back while a backlog drains.
  if (running && source instanceof PodSource && session?.t0Wall != null) now = Math.max(now, (Date.now() - session.t0Wall) / 1000);
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
  const alert = state === "set" && live?.velLoss != null && overTarget(live.velLoss);
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
      [live.velLoss != null ? `${Math.round(live.velLoss * 100)}%` : "--", "vel. loss"], [formatRpe(shown.rpeEst), "RPE est."]]);
    $("heroSub").textContent = alert
      ? `Velocity loss ${Math.round(live.velLoss * 100)}%, past your ${vlTarget}%: stop the set.`
      : last.flags.includes("no end anchor") ? "Pause at the top to lock the last rep in." : "";
  } else if (state === "rest") {
    $("heroLabel").textContent = `Rest · after ${what(shown)}`;
    $("heroBig").textContent = formatTime(Math.max(0, now - restFrom));
    stats([[shown.nReps, "reps"], [shown.bestMcv.toFixed(2), "best MCV"],
      [`${Math.round(shown.velLoss * 100)}%`, "vel. loss"], [formatRpe(shown.rpeEst), "RPE est."]]);
    const e1 = shown.exercise && fitProfile(lvPoints(shown.exercise), shown.lift)?.e1rm;
    $("heroSub").textContent = tracker?.inSegment && !liveReps.length ? "Movement… (not a set yet)"
      : e1 ? `${shown.exercise}: e1RM ≈ ${Math.round(e1)} lb` : "";
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
    $("heroSub").textContent = "Clip the pod (or the phone) to the bar, tap Start, then just lift. Sets and rest are found automatically.";
  }

  $("clock").textContent = running ? `● ${formatTime(now)}` : "Not recording";
  $("clock").className = `pill ${running ? "live" : ""}`;
  $("sessionBar").hidden = !sets.length;
  $("exportBtn").hidden = running;
  $("deleteBtn").hidden = !session?.viewing || running;
  if (sets.length) {
    const reps = sets.reduce((n, s) => n + s.nReps, 0);
    $("sessionSummary").textContent = `${sets.length} set${sets.length === 1 ? "" : "s"} · ${reps} reps`;
  }
  $("liveCard").hidden = state !== "set";
  if (state === "set") drawHeight();
  showPodBanner();
  if (running && source instanceof PodSource && source.linkLost) updateFooter();   // no samples come to call it
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
  if (source instanceof PodSource) {
    // pod BarPod-1A2B · 211 Hz · 16/pkt · MTU 247 · 45 ms · lost 0 · gaps 0 · 3.95 V (MTU: the app only)
    const n = recent.length;
    const L = source.linkInfo();
    const bits = [n > 10 ? `${Math.round((n - 1) / (recent[n - 1].t - recent[0].t))} Hz` : "--"];
    if (L.perPacket !== null) bits.push(`${L.perPacket}/pkt`);
    if (L.mtu) bits.push(`MTU ${L.mtu}`);
    if (L.intervalMs) bits.push(`${Math.round(L.intervalMs)} ms`);
    bits.push(`lost ${L.lost}`, `gaps ${L.gaps}`, `reconnects ${L.reconnects}`, `epochs ${L.epochs}`);
    if (!L.linked && running) bits.push(L.asleep ? "asleep" : "reconnecting");
    if (L.volts) bits.push(`${L.volts.toFixed(2)} V`);
    rate = bits.join(" · ");
  } else if (source instanceof PhoneSource && source.interval) {
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

function showMessage(msg) {
  $("hint").textContent = msg;
  $("hint").classList.remove("err");
}

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

// ---------- sensor calibration (algorithm.md section 1, D23) ----------
// Lay the phone on each of its 6 faces, still for 2 s each. Per axis: scale and bias.

const FACE_NAMES = { "z+": "screen up", "z-": "screen down", "x+": "right edge up", "x-": "left edge up",
  "y+": "top edge up", "y-": "bottom edge up" };
let cal = null;            // { bias, scale, residual, date } or null
let calRun = null;         // the 6-face collection in progress
let calSource = null;

function showCal() {
  const pct = (s) => `${s >= 1 ? "+" : ""}${((s - 1) * 100).toFixed(1)}%`;
  $("calStatus").textContent = cal
    ? `Calibrated ${new Date(cal.date).toLocaleDateString()}: scale x ${pct(cal.scale[0])}, y ${pct(cal.scale[1])}, z ${pct(cal.scale[2])}; ` +
      `offset x ${cal.bias[0].toFixed(2)}, y ${cal.bias[1].toFixed(2)}, z ${cal.bias[2].toFixed(2)} m/s²; fit error ${cal.residual.toFixed(3)} m/s².`
    : "Not calibrated. Lay the phone on each of its 6 sides, still for 2 s each; scale errors go straight into velocity.";
  $("calReset").disabled = !cal;
}

function showFaces() {
  $("calFaces").innerHTML = Object.entries(FACE_NAMES)
    .map(([f, name]) => `<li class="${calRun?.faces.has(f) ? "got" : ""}">${name}</li>`).join("");
}

async function startCalibration() {
  if (calRun) { stopCalibration("Calibration cancelled."); return; }
  if (running || starting) { showError("End the session first."); return; }
  calRun = new CalibrationRun();
  $("calFaces").hidden = false;
  showFaces();
  $("calBtn").textContent = "Cancel";
  $("calStatus").textContent = "Lay the phone on each side below, still for 2 s. It buzzes when it has one.";
  const take = (s) => {
    const face = calRun?.push(s);                          // raw samples: that's what we calibrate
    if (!face) return;
    buzz(80);
    showFaces();
    if (calRun.done) finishCalibration();
  };
  calSource = native ? new NativeSensorSource(take) : new PhoneSource(take);
  try { await calSource.start(); } catch (err) { stopCalibration(err.message); }
}

function finishCalibration() {
  const c = fitCalibration(calRun.means, G_LOCAL);
  if (!c) { stopCalibration("Couldn't fit: a side was missing."); return; }
  if (c.residual > 0.1 || c.scale.some((s) => Math.abs(s - 1) > 0.1)) {
    stopCalibration(`That doesn't look right (fit error ${c.residual.toFixed(2)} m/s²). Try again, flatter and stiller.`);
    return;
  }
  cal = { ...c, date: Date.now(), g: G_LOCAL };
  try { localStorage.setItem("calibration", JSON.stringify(cal)); } catch { /* storage blocked */ }
  if (native) saveFile("", "calibration.json", JSON.stringify(cal, null, 1)).catch(() => {});
  buzz([100, 80, 100]);
  stopCalibration();
}

function stopCalibration(msg = "") {
  const done = Promise.resolve(calSource?.stop()).catch(() => {});
  calSource = null;
  calRun = null;
  $("calBtn").textContent = "Calibrate";
  $("calFaces").hidden = true;
  showCal();
  showSource();                                    // the pod may have become the source meanwhile
  if (msg) $("calStatus").textContent = msg;
  return done;
}

// ---------- next set: exercise + weight (sticky) ----------

// Still lifting the open set? Once its last rep ended REST_AFTER s ago the card says Rest, and the
// "Next set" fields are for the next one (a finished set is edited on its own card). The set stays
// open 15 s longer (SET_GAP), so without this a weight typed in rest was written into it. Before
// any rep (plates going on) edits still apply. The last rep's end, not tracker.quietFor: plate bumps
// reset quietFor.
function editsOpenSet() {
  if (!seg) return false;
  const reps = live?.reps ?? [];
  return !reps.length || (tracker?.now ?? 0) - live.tAbs[reps[reps.length - 1].end] < REST_AFTER;
}

function setExercise(name) {
  exercise = name.trim();                                  // blank: the sensor decides (D21)
  lift = exercise ? liftForExercise(exercise) : "free";
  $("exercise").value = exercise;
  $("profileHint").textContent = hintFor(exercise);
  if (editsOpenSet()) seg = { ...seg, exercise, lift };   // changed it mid-set: this set too
  // over 3 s since the last rep: mid-set (a slow next rep) or done (racking)? Only another rep tells
  else if (seg) pendingSeg = { n: live?.reps?.length ?? 0, v: { ...pendingSeg?.v, exercise, lift } };
  try { localStorage.setItem("exercise", exercise); } catch { /* storage blocked */ }
  render();
}

function setWeight(value) {
  weight = String(value).trim();
  $("weight").value = weight;
  if (editsOpenSet()) seg.weight = weight;
  else if (seg) pendingSeg = { n: live?.reps?.length ?? 0, v: { ...pendingSeg?.v, weight } };
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
$("podBtn").addEventListener("click", connectPod);
$("phoneBtn").addEventListener("click", () => setSourceKind("phone"));
$("endBtn").addEventListener("click", endSession);
$("shareBtn").addEventListener("click", shareSession);
$("exportBtn").addEventListener("click", exportShown);
$("deleteBtn").addEventListener("click", deleteShown);
$("exportAllBtn").addEventListener("click", exportAll);
$("importInput").addEventListener("change", (e) => {
  const f = e.target.files[0];
  if (f) importFile(f);
  e.target.value = "";
});
$("exercise").addEventListener("change", (e) => setExercise(e.target.value));
$("exercise").addEventListener("input", (e) => { $("profileHint").textContent = hintFor(e.target.value); });
$("weight").addEventListener("change", (e) => setWeight(e.target.value));
$("calBtn").addEventListener("click", startCalibration);
$("calReset").addEventListener("click", () => {
  cal = null;
  try { localStorage.removeItem("calibration"); } catch { /* storage blocked */ }
  showCal();
});
try { cal = JSON.parse(localStorage.getItem("calibration") || "null"); } catch { cal = null; }
try { sourceKind = localStorage.getItem("source") === "pod" ? "pod" : "phone"; } catch { /* storage blocked */ }
try { podName = localStorage.getItem("podName") || ""; } catch { /* storage blocked */ }
showCal();
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
  calRun: () => calRun,
  podCapture: false,          // true before Start: keep the pod's frames for downloadPodCapture()
  buzzes: () => buzzLog.slice(),
  pod: () => (source instanceof PodSource ? { ...source.linkInfo(), gapList: source.stream.gaps,
    nextIdx: source.stream.ep?.next_idx ?? null, lastIdx: source.stream.ep?.last_idx ?? null,
    head: source.stream.ep?.status?.head ?? null } : null),
  podCaptureData: () => (source?.capture ? { csv: source.captureCSV(), samples: source.capture.samples } : null),
  // link.json now (or as the last pod session ended); a radio outage of ms (drop + no reconnect
  // meanwhile); the SLEEP op (mode 0 wake on motion, 1 IMU off)
  link: () => (source instanceof PodSource ? source.linkJSON() : session?.podLink ?? null),
  podOutage: (ms) => (running && source instanceof PodWebSource ? source.outage(ms) : false),
  podSleep: (mode = 0) => (running && source instanceof PodSource ? source.sleepPod(mode) : Promise.reject(new Error("no pod session"))),
  downloadLink: () => { const l = window.barDebug.link(); if (l) download(`link-${session?.stamp ?? "x"}.json`, JSON.stringify(l, null, 1)); },
  // The capture in make_fake_pod.py's vector format: analysis/podlink.py replays it.
  downloadPodCapture: () => { if (source?.capture) download(`pod-capture-${session?.stamp ?? "x"}.csv`, source.captureCSV()); },
  recording: () => {
    const s = session?.sets[session.sets.length - 1];
    return s ? { name: s.file, csv: toCSV(s.samples) } : { name: "none.csv", csv: "" };
  },
};
// Masthead date, like the top of a logbook page.
$("masthead").textContent = `Training log · ${new Date().toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" })}`;
showSource();
render();
drawLive();
renderHistory();
checkUnfinished();
