// Step 12 check: a pod session in the app itself (docs/ble-plan.md step 12), driven through the
// app's WebView over adb and the DevTools protocol. Hands-free: the pod on the laptop's USB, the
// phone on USB with debugging, unlocked, the pod not connected to anything else.
//
//   node android/pod_session_check.mjs [--install] [--dark=25]
//
//   1. Start a session with the pod as the source (the app's PodNativeSource on PodLinkPlugin):
//      the footer shows ~210 Hz, 16/pkt, MTU 247
//   2. the screen off (adb keyevent): the decoder keeps getting samples while the page is hidden
//   3. with the screen still off, the pod's w (the sleep prologue, then a reset): "asleep", then
//      the plugin reconnects by itself and the app streams the new boot from 0, screen still dark
//   4. screen on, End: STOP answered, link.json and pod.json saved in Documents/BarTracker/<session>/
//      (pulled and checked), and the app's capture replays through podlink.py to the same samples
//   5. a second session right after: starts, streams, ends. The screen-off step locks the phone
//      and Start needs the app in front: unlock it when the script asks
// (--install: npx cap sync, gradlew assembleDebug and adb install first. --two: only two short
// sessions back to back, with the phone unlocked.)

import { execSync, execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { adb, page, install, sendToPod, sleep } from "./webview.mjs";
import { replayCapture, worstDiff } from "../webapp/test/cdp.mjs";

const arg = (k, d) => process.argv.find((a) => a.startsWith(`--${k}=`))?.split("=").slice(1).join("=") ?? d;
const DARK = Number(arg("dark", "25"));
const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, "..");
const PYTHON = process.env.PYTHON || join(ROOT, "analysis", ".venv", "Scripts", "python.exe");

let ok = true;
const check = (good, msg) => { console.log(`${good ? "PASS" : "FAIL"}  ${msg}`); ok &&= Boolean(good); };
const note = (msg) => console.log(`      ${msg}`);

async function until(js, expr, ms, what) {
  const t0 = Date.now();
  while (Date.now() - t0 < ms) { if (await js(expr)) return Date.now() - t0; await sleep(500); }
  throw new Error(`timed out waiting for ${what}`);
}

/** Start must be tapped with the app in front (Android starts the screen-off service only then),
 * so wake the phone, bring the app up, and wait for it to be unlocked if it isn't. */
async function ensureFront() {
  // mCurrentFocus names the activity in full (io.github...bartracker/io.github...bartracker.MainActivity)
  const front = () => /io\.github\.steveyme19\.bartracker\/\S*MainActivity/.test(adb("shell", "dumpsys", "window").match(/mCurrentFocus=.*/)?.[0] ?? "");
  adb("shell", "input", "keyevent", "KEYCODE_WAKEUP");
  adb("shell", "am", "start", "-n", "io.github.steveyme19.bartracker/.MainActivity");
  await sleep(1500);
  if (front()) return;
  console.log(">>> UNLOCK the phone (the app should be on screen)");
  const t0 = Date.now();
  while (!front()) {
    if (Date.now() - t0 > 180000) throw new Error("the app never came to the front (phone locked?)");
    await sleep(1000);
  }
  await sleep(1000);
}

async function session(js, n) {
  await ensureFront();
  await js("window.barDebug.podCapture = true");
  if (n === 1) {
    await js(`(() => { const e = document.getElementById('exercise'); e.value = 'Deadlift'; e.dispatchEvent(new Event('change')); })()`);
    if (!(await js("document.getElementById('podBtn').classList.contains('on')"))) await js("document.getElementById('podBtn').click()");
  }
  await js("document.getElementById('startBtn').click()");
  await until(js, "!document.getElementById('endBtn').disabled || document.getElementById('hint').classList.contains('err')", 60000, "the session to start");
  const err = await js("document.getElementById('hint').classList.contains('err') ? document.getElementById('hint').textContent : ''");
  check(!err, `session ${n} started ${err ? `(error: ${err})` : ""}`);
  return !err;
}

/** Start, 8 s of streaming, End: STOP answered once, no faults. */
async function shortSession(js, n) {
  if (!(await session(js, n))) return;
  await sleep(8000);
  const got = await js("window.barDebug.pod()?.stats.samples ?? 0");
  await js("document.getElementById('endBtn').click()");
  await until(js, "!document.getElementById('startBtn').disabled", 20000, `End session ${n}`);
  const L = await js("window.barDebug.link()");
  check(got > 1500 && L.stats.stopped === 1 && !L.faults.length, `session ${n}: ${got} samples in 8 s, stopped ${L.stats.stopped}, faults ${JSON.stringify(L.faults)}`);
}

async function main() {
  if (process.argv.includes("--two")) {
    // just two short sessions back to back, the phone unlocked (the screen-off run locks it)
    const { js, close } = await page();
    try { await shortSession(js, 1); await shortSession(js, 2); } finally { close(); }
    return;
  }
  if (process.argv.includes("--install")) {
    execSync("npx cap sync android", { cwd: ROOT, stdio: "ignore" });
    // by its full path: cmd may not look in the current folder (NoDefaultCurrentDirectoryInExePath)
    execFileSync("cmd.exe", ["/c", join(HERE, "gradlew.bat"), "assembleDebug"], { cwd: HERE, stdio: "ignore" });
    note(`installed: ${await install()}`);
  }
  const { js, close } = await page();
  const samples = () => js("window.barDebug.pod()?.stats.samples ?? 0");
  try {
    if (!(await session(js, 1))) return;
    await sleep(6000);
    // the footer redraws on animation frames, which only run while the page is visible
    const footer = await js("document.getElementById('rate').textContent");
    const hz = Number(footer.match(/(\d+) Hz/)?.[1]);
    if (await js("document.hidden")) {
      const L = await js("window.barDebug.pod()");
      check(L.perPacket === 16 && L.mtu === 247 && L.lost === 0, `page hidden, from linkInfo: ${L.perPacket}/pkt, MTU ${L.mtu}, lost ${L.lost}`);
    } else {
      check(/^pod BarPod-/.test(footer) && hz >= 205 && hz <= 215 && /16\/pkt/.test(footer) && /MTU 247/.test(footer) && /lost 0/.test(footer),
        `footer "${footer}"`);
    }

    // the screen off: the page is hidden, the session runs under RecordingService
    adb("shell", "input", "keyevent", "KEYCODE_SLEEP");
    await sleep(2000);
    const s0 = await samples();
    const hidden = await js("document.hidden");
    await sleep(DARK * 1000);
    const s1 = await samples();
    const rate = (s1 - s0) / DARK;
    check(hidden && rate > 200 && rate < 220, `screen off ${DARK} s: page hidden ${hidden}, the decoder got ${s1 - s0} samples (${rate.toFixed(1)}/s)`);

    // the pod sleeps and resets with the screen still off: the plugin reconnects, the app streams the new boot
    note(`sent w to the pod: ${sendToPod("w") || "(no answer)"}`);
    const tW = Date.now();
    await until(js, "window.barDebug.link()?.linkEvents.some((e) => e.kind === 'asleep')", 15000, "the asleep report");
    const banner = await js("document.getElementById('podBanner').hidden ? '' : document.getElementById('podBanner').textContent");
    await until(js, "window.barDebug.link()?.epochList.length === 2 && window.barDebug.link()?.linked", 90000, "the woken pod, screen off");
    const back = Date.now() - tW;
    const s2 = await samples();
    await sleep(5000);
    const s3 = await samples();
    const L = await js("window.barDebug.link()");
    check(/asleep/i.test(banner) && L.epochList.at(-1).reboot && L.reconnects === 1 && s3 > s2 && (await js("document.hidden")),
      `screen off: "${banner}", back by itself as a new boot ${(back / 1000).toFixed(1)} s after the w, streaming again (${s3 - s2} samples in 5 s), still dark`);
    check(!L.gapList.some((g) => g.kind === "sleep-tail") && !L.faults.length,
      `nothing lost to the sleep: gaps ${JSON.stringify(L.gapList.map((g) => `${g.kind} ${g.n}`))}, faults ${JSON.stringify(L.faults)}`);

    // screen on, End: the STOP handshake and the files the app saved
    adb("shell", "input", "keyevent", "KEYCODE_WAKEUP");
    await sleep(1500);
    await js("document.getElementById('endBtn').click()");
    await until(js, "!document.getElementById('startBtn').disabled", 20000, "End session");
    const folder = await js("window.barDebug.session().folder");
    const end = await js("window.barDebug.link()");
    check(end.stats.stopped >= 2 && !end.faults.length, `End: stopped ${end.stats.stopped} (the sleep and the STOP), faults ${JSON.stringify(end.faults)}`);
    const dir = `/sdcard/Documents/BarTracker/${folder}`;
    const files = adb("shell", "ls", dir).split(/\s+/);
    const tmp = mkdtempSync(join(tmpdir(), "pod-session-"));
    let linkOk = false;
    try {
      adb("pull", `${dir}/link.json`, join(tmp, "link.json"));
      const lj = JSON.parse(readFileSync(join(tmp, "link.json"), "utf8"));
      linkOk = lj.reconnects === 1 && lj.epochList.length === 2 && lj.mtu === 247 && lj.linkEvents.some((e) => e.kind === "back");
      note(`link.json: reconnects ${lj.reconnects}, epochs ${lj.epochList.length}, mtu ${lj.mtu}, events ${lj.linkEvents.map((e) => e.kind).join(" ")}`);
    } catch (e) { note(`link.json: ${e.message.split("\n")[0]}`); }
    rmSync(tmp, { recursive: true, force: true });
    check(files.includes("link.json") && files.includes("pod.json") && linkOk, `saved in Documents/BarTracker/${folder}: ${files.join(" ")}`);
    const cap = await js("window.barDebug.podCaptureData()");
    const ref = replayCapture(cap.csv, PYTHON, ROOT);
    check(!ref.error && ref.samples.length === cap.samples.length && worstDiff(ref.samples, cap.samples) < 1e-9 && ref.gaps.length === end.gapList.length,
      ref.error ? `podlink.py replay failed: ${ref.error}` : `podlink.py replay of the app's capture: ${ref.samples.length} vs ${cap.samples.length} samples, gaps ${ref.gaps.length} vs ${end.gapList.length}`);

    // a second session right after
    await shortSession(js, 2);
  } finally {
    try { adb("shell", "input", "keyevent", "KEYCODE_WAKEUP"); } catch { /* fine */ }
    close();
  }
}

main().then(() => { console.log(ok ? "PASS" : "FAIL"); process.exit(ok ? 0 : 1); },
  (e) => { console.log(`FAIL  ${e.message}`); process.exit(1); });
