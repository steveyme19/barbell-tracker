// Step 6 bench check: the web app's pod source in real Chrome, driven over the DevTools protocol
// (docs/ble-plan.md step 6). Zero install: Node's own WebSocket, the Chrome you have.
//
//   node webapp/test/pod_browser.mjs [--url=http://localhost:8765/webapp/] [--seconds=20]
//                                    [--reps [--rom=18in | --rom=46] [--exercise=Deadlift]]
//
// Needs: the pod advertising nearby (Bluetooth headphones off: they starve the laptop's
// scanning, D28), the web app served on localhost (the .claude/launch.json "webapp" server, or
// python -m http.server). It launches Chrome with a throwaway profile, answers the Bluetooth
// chooser through CDP's DeviceAccess domain (no click needed), then:
//   1. Connect pod, Start: the footer shows ~211 Hz, 16/pkt, lost 0; |a| at rest ~9.8 (raw pod)
//   2. --reps: you do 5 slow reps of --rom (cm, or inches with "in") by hand, pausing at the
//      bottom, still before and after: 5 clean reps (leveled, no flags) within 20% of --rom.
//      Prints each rep's ROM, screenshots the set card, keeps the set CSV and the capture in
//      analysis/data (P2). Measured 2026-10-23: 18 in by hand read 16.5 to 17.5 in.
//   3. End session: the stop handshake resolves, no stop-tail, and the last sample's index + 1
//      is the head in the pod's stream_stopped STATUS
//   4. the browser's capture, replayed through analysis/podlink.py, gives the same samples
//   5. a second session in the same tab works the same way

import { spawn, spawnSync } from "node:child_process";
import { mkdtempSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { decodeFrame, POD } from "../sources.js";

const arg = (k, d) => process.argv.find((a) => a.startsWith(`--${k}=`))?.split("=").slice(1).join("=") ?? d;
const URL0 = arg("url", "http://localhost:8765/webapp/");
const SECONDS = Number(arg("seconds", "20"));
const REPS = process.argv.includes("--reps");
const ROM_ARG = arg("rom", "18in");
const ROM_CM = ROM_ARG.endsWith("in") ? parseFloat(ROM_ARG) * 2.54 : parseFloat(ROM_ARG);
const EXERCISE = arg("exercise", "Deadlift");   // floors and lockouts level (LIFTS in reps.py)
const CHROME = arg("chrome", "C:/Program Files/Google/Chrome/Application/chrome.exe");
const PORT = 9333;
const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const PYTHON = process.env.PYTHON || join(ROOT, "analysis", ".venv", "Scripts", "python.exe");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let ok = true;
const check = (good, msg) => { console.log(`${good ? "PASS" : "FAIL"}  ${msg}`); ok &&= Boolean(good); };
const note = (msg) => console.log(`      ${msg}`);

// ---- a minimal CDP client ----
async function cdp(wsUrl) {
  const ws = new WebSocket(wsUrl);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
  let id = 0;
  const pending = new Map();
  const handlers = new Map();
  ws.onmessage = (m) => {
    const msg = JSON.parse(m.data);
    if (msg.id && pending.has(msg.id)) {
      const { res, rej } = pending.get(msg.id);
      pending.delete(msg.id);
      msg.error ? rej(new Error(`${msg.error.message} ${msg.error.data ?? ""}`)) : res(msg.result);
    } else if (msg.method) {
      for (const h of handlers.get(msg.method) ?? []) h(msg.params);
    }
  };
  const send = (method, params = {}) => new Promise((res, rej) => {
    pending.set(++id, { res, rej });
    ws.send(JSON.stringify({ id, method, params }));
  });
  const on = (method, h) => handlers.set(method, [...(handlers.get(method) ?? []), h]);
  ws.onclose = () => {                            // Chrome died: fail every call still waiting
    for (const { rej } of pending.values()) rej(new Error("Chrome closed the DevTools connection"));
    pending.clear();
  };
  return { send, on, close: () => ws.close() };
}

async function main() {
  const profile = mkdtempSync(join(tmpdir(), "pod-chrome-"));
  const chrome = spawn(CHROME, [`--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`,
    "--no-first-run", "--no-default-browser-check", "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding", "--disable-background-timer-throttling", "--new-window", URL0], { stdio: "ignore" });
  let c = null;
  try {
    let page = null;
    for (let i = 0; i < 50 && !page; i++) {
      await sleep(200);
      try {
        const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
        page = list.find((t) => t.type === "page" && t.url.startsWith(URL0));
      } catch { /* Chrome still starting */ }
    }
    if (!page) throw new Error(`Chrome didn't open ${URL0}`);
    c = await cdp(page.webSocketDebuggerUrl);
    const js = async (expr, gesture = false) => {
      const r = await c.send("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true, userGesture: gesture });
      if (r.exceptionDetails) throw new Error(`page: ${r.exceptionDetails.exception?.description ?? r.exceptionDetails.text}`);
      return r.result.value;
    };
    const until = async (expr, ms, what) => {
      const t0 = Date.now();
      while (Date.now() - t0 < ms) {
        if (await js(expr)) return true;
        await sleep(250);
      }
      throw new Error(`timed out waiting for ${what}`);
    };

    // the Bluetooth chooser: take the first BarPod it lists
    await c.send("DeviceAccess.enable");
    let picked = null;
    c.on("DeviceAccess.deviceRequestPrompted", ({ id, devices }) => {
      const d = devices.find((x) => (x.name || "").startsWith("BarPod-"));
      if (d && !picked) {
        picked = d.name;
        c.send("DeviceAccess.selectPrompt", { id, deviceId: d.id }).catch((e) => note(`selectPrompt: ${e.message}`));
      }
    });
    await until("document.readyState === 'complete' && !!window.barDebug", 10000, "the app");
    await js("document.getElementById('podBtn').click()", true);
    await until("document.getElementById('podBtn').textContent.startsWith('Pod: ')", 30000, "the chooser to pick a pod");
    check(picked, `chooser picked ${picked}; button reads "${await js("document.getElementById('podBtn').textContent")}"`);

    // the exercise sets the profile: which heights the tracker holds level (LIFTS in reps.py)
    await js(`(() => { const e = document.getElementById('exercise'); e.value = ${JSON.stringify(EXERCISE)};
      e.dispatchEvent(new Event('change')); })()`);

    for (const round of [1, 2]) {
      console.log(`---- session ${round} ----`);
      await js("window.barDebug.podCapture = true");
      const t0 = Date.now();
      await js("document.getElementById('startBtn').click()", true);
      await until("!document.getElementById('endBtn').disabled || document.getElementById('hint').classList.contains('err')", 20000, "the session to start");
      const startErr = await js("document.getElementById('hint').classList.contains('err') ? document.getElementById('hint').textContent : ''");
      check(!startErr, `started in ${((Date.now() - t0) / 1000).toFixed(1)} s ${startErr ? `(error: ${startErr})` : ""}`);
      if (startErr) break;
      const secs = round === 1 ? SECONDS : 10;
      for (let s = 0; s < secs; s += 5) {
        await sleep(5000);
        note(`${s + 5} s: ${await js("document.getElementById('rate').textContent")}`);
      }
      if (round === 1 && REPS) {
        console.log(`      >>> Now: hold the pod still 2 s, do 5 slow reps (up ~${ROM_ARG}, pause, down, pause), hold still 3 s. 40 s.`);
        for (let s = 0; s < 40; s += 10) {
          await sleep(10000);
          note(`${s + 10} s: ${await js("document.getElementById('heroLabel').textContent + ' | ' + document.getElementById('heroBig').textContent")}`);
        }
      }
      const footer = await js("document.getElementById('rate').textContent");
      const hz = Number(footer.match(/(\d+) Hz/)?.[1]);
      check(hz >= 205 && hz <= 215 && /16\/pkt/.test(footer) && /lost 0/.test(footer), `footer "${footer}"`);
      const am = await js(`(() => { const s = window.barDebug.podCaptureData().samples.slice(-200);
        const m = s.map((x) => Math.hypot(x.ax, x.ay, x.az)).sort((a, b) => a - b); return m[m.length >> 1]; })()`);
      check(am > 9.3 && am < 10.4, `median |a| of the last 200 samples ${am.toFixed(3)} m/s^2 (raw pod)`);

      await js("document.getElementById('endBtn').click()", true);
      const tEnd = Date.now();
      await until("!document.getElementById('startBtn').disabled", 15000, "End session to finish");
      const pod = await js("window.barDebug.pod()");
      const tails = pod.gapList.filter((g) => g.kind.endsWith("-tail"));
      const idxLoss = pod.gapList.filter((g) => !g.kind.startsWith("imu"));
      // the head the pod reported when it stopped (a later 1 s STATUS reports a bigger one)
      const stopHead = (await js("window.barDebug.podCaptureData().csv")).trim().split("\n").slice(1)
        .map((row) => row.split(",")).filter((r) => r[2] === "frame")
        .map((r) => decodeFrame(Uint8Array.from(r[4].match(/../g), (h) => parseInt(h, 16))))
        .filter((f) => f?.type === POD.T_STATUS && (f.flags & POD.S_STREAM_STOPPED)).at(-1)?.head;
      check(pod.stats.stopped === 1 && !tails.length && pod.lastIdx + 1 === stopHead,
        `End took ${((Date.now() - tEnd) / 1000).toFixed(1)} s: stopped ${pod.stats.stopped}, tails ${JSON.stringify(tails)}, ` +
        `last index ${pod.lastIdx} + 1 = stop head ${stopHead}`);
      check(!idxLoss.length && !pod.faults.length && pod.stats.pkt_seq_gaps === 0,
        `decoder: index gaps ${JSON.stringify(idxLoss)}, faults ${JSON.stringify(pod.faults)}, pkt_seq gaps ${pod.stats.pkt_seq_gaps}, ` +
        `time holes ${pod.gapList.length - idxLoss.length}, samples ${pod.stats.samples}`);
      if (round === 1 && REPS) {
        const sets = await js(`window.barDebug.session().sets.map((s) => ({ lift: s.lift, n: s.nReps,
          rom: s.reps.map((r) => +(r.rom * 100).toFixed(1)), mcv: s.reps.map((r) => +r.mcv.toFixed(2)),
          leveled: s.reps.map((r) => r.leveled), flags: s.reps.map((r) => r.flags) }))`);
        for (const st of sets) {
          note(`set (${st.lift}): ROM cm ${st.rom.join(" ")} (in ${st.rom.map((r) => (r / 2.54).toFixed(1)).join(" ")}) | ` +
            `MCV ${st.mcv.join(" ")} | leveled ${JSON.stringify(st.leveled)} | flags ${JSON.stringify(st.flags)}`);
        }
        // clean reps: leveled and unflagged (moving the pod after the set adds an unanchored one)
        const clean = sets.length === 1 ? sets[0].rom.filter((r, i) => sets[0].leveled[i] && !sets[0].flags[i].length) : [];
        check(clean.length === 5 && clean.every((r) => Math.abs(r - ROM_CM) <= 0.2 * ROM_CM),
          `${sets.length} set(s); clean reps ${clean.length} (expect 5) within 20% of ${ROM_CM.toFixed(1)} cm: ${clean.join(" ")}`);
        // the card's height plot, as you saw it
        await js("(() => { const d = document.querySelector('#setList article.set details'); if (d) d.open = true; })()");
        await sleep(800);
        const box = await js(`(() => { const r = document.querySelector('#setList article.set').getBoundingClientRect();
          window.scrollTo(0, window.scrollY + r.top - 10); const q = document.querySelector('#setList article.set').getBoundingClientRect();
          return { x: q.left, y: q.top, width: q.width, height: q.height }; })()`);
        const shot = await c.send("Page.captureScreenshot", { format: "png", clip: { ...box, scale: 1 } });
        const stamp = await js("window.barDebug.session().stamp");
        const png = join(tmpdir(), `pod-set-card-${stamp}.png`);
        writeFileSync(png, Buffer.from(shot.data, "base64"));
        note(`set card screenshot: ${png}`);
        // keep the raw data (P2): the set as the app saves it, and the frames for podlink.py
        const rec = await js("window.barDebug.recording()");
        writeFileSync(join(ROOT, "analysis", "data", rec.name), rec.csv);
        writeFileSync(join(ROOT, "analysis", "data", `pod-capture-${stamp}.csv`), (await js("window.barDebug.podCaptureData()")).csv);
        note(`saved analysis/data/${rec.name} and analysis/data/pod-capture-${stamp}.csv`);
      }

      // the same frames through the Python reference decoder (P1) must give the same samples
      const cap = await js("window.barDebug.podCaptureData()");
      const dir = mkdtempSync(join(tmpdir(), "pod-cap-"));
      const csvPath = join(dir, "capture.csv");
      writeFileSync(csvPath, cap.csv);
      const py = spawnSync(PYTHON, ["-c", [
        "import csv, json, sys",
        "sys.path.insert(0, sys.argv[2])",
        "import make_fake_pod as m",
        "log = [(r['kind'], float(r['arrival_ms']) if r['arrival_ms'] else None, r['payload']) for r in csv.DictReader(open(sys.argv[1]))]",
        "smp, st, ev = m.replay(log)",
        "print(json.dumps({'samples': smp, 'gaps': st.gaps, 'faults': st.faults}))",
      ].join("\n"), csvPath, join(ROOT, "analysis")], { encoding: "utf8", maxBuffer: 1 << 28 });
      rmSync(dir, { recursive: true, force: true });
      if (py.status !== 0) {
        check(false, `python replay failed: ${py.stderr.slice(-500)}`);
      } else {
        const ref = JSON.parse(py.stdout);
        let worst = 0;
        const n = Math.min(ref.samples.length, cap.samples.length);
        for (let i = 0; i < n; i++) {
          for (const k of ["t", "ax", "ay", "az", "gx", "gy", "gz"]) worst = Math.max(worst, Math.abs(ref.samples[i][k] - cap.samples[i][k]));
        }
        check(ref.samples.length === cap.samples.length && worst < 1e-9 && ref.gaps.length === pod.gapList.length,
          `podlink.py replay of the capture: ${ref.samples.length} vs ${cap.samples.length} samples, worst diff ${worst}, ` +
          `gaps ${ref.gaps.length} vs ${pod.gapList.length}`);
      }
    }
  } finally {
    c?.close();
    chrome.kill();
    await sleep(500);
    try { rmSync(profile, { recursive: true, force: true }); } catch { /* Chrome still holds it */ }
  }
}

main().then(() => { console.log(ok ? "PASS" : "FAIL"); process.exit(ok ? 0 : 1); },
  (e) => { console.log(`FAIL  ${e.message}`); process.exit(1); });
