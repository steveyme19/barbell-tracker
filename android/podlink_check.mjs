// Step 11 check: PodLinkPlugin on the phone (docs/ble-plan.md step 11), driven through the app's
// WebView over adb and the DevTools protocol (like pull-recording.mjs). Zero install.
//
//   node android/podlink_check.mjs [--install] [--seconds=20] [--drop]
//
// Needs: the phone on USB with debugging on, the pod awake nearby and not connected to anything
// else (close other Chrome tabs using it). It:
//   1. (--install) installs app\build\outputs\apk\debug\app-debug.apk and opens the app
//   2. Capacitor.Plugins.PodLink.connect(): the first time Android asks for Nearby devices (tap
//      Allow on the phone); it must resolve with mtu 247, and INFO and STATUS arrive first
//   3. writes STREAM_LIVE(600) and counts "pod" frames for --seconds: about 13 DATA frames/s
//      (16 samples each at 211 Hz) and a STATUS per second. Java never parses; here the frames
//      also go through the app's own PodStream (webapp/sources.js), which must see 0 gaps
//   4. --drop: the laptop sends w to the pod over USB (COM port found by USB ID): the sleep
//      prologue, then a reset. The plugin must say "lost", reconnect by itself (autoConnect),
//      run the connection order again and say "ready"; the new boot streams from index 0
//   5. STOP, disconnect(); adb logcat -s PodLink shows the MTU and the DATA CCCD write

import { execFileSync, spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const arg = (k, d) => process.argv.find((a) => a.startsWith(`--${k}=`))?.split("=").slice(1).join("=") ?? d;
const SECONDS = Number(arg("seconds", "20"));
const INSTALL = process.argv.includes("--install");
const DROP = process.argv.includes("--drop");
const APP = "io.github.steveyme19.bartracker";
const PORT = 9334;
const HERE = dirname(fileURLToPath(import.meta.url));
const PYTHON = process.env.PYTHON || join(HERE, "..", "analysis", ".venv", "Scripts", "python.exe");
const sdkAdb = join(process.env.LOCALAPPDATA ?? "", "Android", "Sdk", "platform-tools", "adb.exe");
const adb = (...args) => execFileSync(existsSync(sdkAdb) ? sdkAdb : "adb", args, { encoding: "utf8" }).trim();
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let ok = true;
const check = (good, msg) => { console.log(`${good ? "PASS" : "FAIL"}  ${msg}`); ok &&= Boolean(good); };
const note = (msg) => console.log(`      ${msg}`);

async function page() {
  let pid = "";
  for (let i = 0; i < 40 && !pid; i++) { try { pid = adb("shell", "pidof", APP); } catch { /* not up yet */ } if (!pid) await sleep(250); }
  if (!pid) throw new Error("Bar Tracker isn't running on the phone.");
  adb("forward", `tcp:${PORT}`, `localabstract:webview_devtools_remote_${pid}`);
  let pages = [];
  for (let i = 0; i < 40 && !pages.length; i++) {
    try { pages = (await (await fetch(`http://127.0.0.1:${PORT}/json`)).json()).filter((p) => p.type === "page"); } catch { /* WebView starting */ }
    if (!pages.length) await sleep(250);
  }
  if (!pages.length) throw new Error("No WebView page to inspect (a debug build?).");
  const ws = new WebSocket(pages[0].webSocketDebuggerUrl);
  await new Promise((r, j) => { ws.onopen = r; ws.onerror = j; });
  let id = 0;
  const waiting = new Map();
  ws.onmessage = (m) => { const d = JSON.parse(m.data); waiting.get(d.id)?.(d); waiting.delete(d.id); };
  const js = (expr) => new Promise((res, rej) => {
    const n = ++id;
    waiting.set(n, (d) => (d.result?.exceptionDetails ? rej(new Error(d.result.exceptionDetails.exception?.description ?? "page error"))
      : res(d.result?.result?.value)));
    ws.send(JSON.stringify({ id: n, method: "Runtime.evaluate", params: { expression: expr, awaitPromise: true, returnByValue: true } }));
  });
  return { js, close: () => { ws.close(); adb("forward", "--remove", `tcp:${PORT}`); } };
}

// The pod reads a bench command only while a host holds its port open (loop() checks Serial
// first), so stay open until it answers: "# sleeping: ..." (up to 3 s).
function sendToPod(cmd) {
  const code = ["import serial, time, serial.tools.list_ports as lp",
    "port = next(p.device for p in lp.comports() if '2886' in p.hwid)",
    "s = serial.Serial(port, 115200, timeout=0.3); s.reset_input_buffer(); time.sleep(0.3); s.write(b'" + cmd + "')",
    "t0 = time.time()",
    "while time.time() - t0 < 3:",
    "    ln = s.readline().decode('ascii', 'ignore').strip()",
    "    if ln.startswith('# sleeping'): print(ln); break",
    "s.close()"].join("\n");
  const r = spawnSync(PYTHON, ["-c", code], { encoding: "utf8" });
  if (r.status !== 0) throw new Error(`couldn't send ${cmd} to the pod over USB: ${r.stderr.slice(-300)}`);
  return r.stdout.trim();
}

async function main() {
  if (INSTALL) {
    note(adb("install", "-r", join(HERE, "app", "build", "outputs", "apk", "debug", "app-debug.apk")).split("\n").at(-1));
    adb("shell", "am", "force-stop", APP);
    adb("shell", "am", "start", "-n", `${APP}/.MainActivity`);
    await sleep(3000);
  }
  adb("logcat", "-c");
  const { js, close } = await page();
  try {
    // A small listener harness in the page: every frame kept with its arrival time, every state,
    // and the frames through the app's own PodStream (the plugin itself never parses them).
    await js(`(async () => {
      const P = Capacitor.Plugins.PodLink;
      const { PodStream } = await import("./sources.js");
      const b64 = (u8) => btoa(String.fromCharCode(...u8));
      const H = window.__pl = { frames: [], states: [], samples: 0, events: [], auto: false };
      H.stream = new PodStream(() => { H.samples++; }, (k, i) => H.events.push(k === "gap" ? "gap:" + i.kind + ":" + i.n : k));
      await P.removeAllListeners();
      await P.addListener("pod", (e) => {
        for (let i = 0; i < e.f.length; i++) {
          const raw = atob(e.f[i]);
          const u8 = Uint8Array.from(raw, (c) => c.charCodeAt(0));
          H.frames.push([u8[0], e.w[i]]);
          H.stream.frame(new DataView(u8.buffer), e.w[i]);
        }
      });
      await P.addListener("podState", (e) => {
        H.states.push(e.state);
        // like the app will (step 12): after a reconnect, the decoder says what to stream, for this link
        if (e.state === "ready" && H.auto) { const b = H.stream.command(); if (b) P.write({ b: b64(b), link: e.link }); }
      });
      // the decoder's requests (a reboot without a new boot_id, a stale INFO): answer them
      H.stream.onEvent = ((orig) => (k, i) => {
        orig(k, i);
        if (k === "need-command" && H.auto) { const b = H.stream.command(); if (b) P.write({ b: b64(b) }); }
        if (k === "need-info") P.reread().then(() => { const b = H.stream.command(); if (b) P.write({ b: b64(b) }); });
      })(H.stream.onEvent);
      H.write = (u8) => P.write({ b: b64(u8) });
      return true;
    })()`);
    console.log(">>> connecting: if the phone asks for Nearby devices, tap Allow");
    const t0 = Date.now();
    const info = await js(`Capacitor.Plugins.PodLink.connect().then((r) => r, (e) => ({ error: e.message }))`);
    const firstTypes = await js("window.__pl.frames.slice(0, 2).map((f) => f[0])");
    check(!info.error && info.mtu === 247 && firstTypes[0] === 0x20 && firstTypes[1] === 0x10,
      `connect() in ${((Date.now() - t0) / 1000).toFixed(1)} s: ${JSON.stringify(info)}; first frames INFO, STATUS (${firstTypes.map((x) => "0x" + x.toString(16)).join(" ")})`);
    if (info.error) return;
    // now, while it's fresh: a debug build logs every event at verbose level and the 256 KiB log
    // buffer wraps within minutes
    const log = adb("logcat", "-d", "-s", "PodLink:*").split("\n").filter((l) => /PodLink/.test(l));
    for (const l of log.slice(-8)) note(l.replace(/^.*PodLink\s*:\s*/, "logcat: "));
    check(log.some((l) => /mtu 247/.test(l)) && log.some((l) => /DATA CCCD write, status 0/.test(l)), "logcat shows the MTU and the DATA CCCD write");
    await js(`(async () => { const b = window.__pl.stream.command(); await window.__pl.write(b); window.__pl.auto = true; })()`);
    await sleep(5000);                            // STREAM_LIVE(600) sends 2.8 s of lead-in at once: count after it
    const n0 = await js("window.__pl.frames.length");
    const s0 = await js("window.__pl.samples");
    await sleep(SECONDS * 1000);
    const st = await js(`(() => { const H = window.__pl; const f = H.frames.slice(${n0});
      const span = (f.at(-1)[1] - f[0][1]) / 1000;
      const w = H.frames.map((x) => x[1]); let back = 0; for (let i = 1; i < w.length; i++) if (w[i] < w[i - 1]) back++;
      return { span, data: f.filter((x) => x[0] === 1).length, status: f.filter((x) => x[0] === 0x10).length, back,
        samples: H.samples, inWindow: H.samples - ${s0}, gaps: H.stream.gaps.length, faults: H.stream.faults, perPkt: H.stream.ep?.status?.n_per_packet }; })()`);
    const fps = st.data / st.span;
    check(fps > 12 && fps < 14.5 && st.status >= SECONDS - 2 && st.back === 0,
      `${fps.toFixed(1)} DATA frames/s and ${(st.status / st.span).toFixed(2)} STATUS/s over ${st.span.toFixed(1)} s, arrival times in order`);
    check(st.gaps === 0 && !st.faults.length && st.samples > 0,
      `the app's PodStream on those frames: ${st.samples} samples (${(st.inWindow / st.span).toFixed(1)}/s in the window), ${st.perPkt}/pkt, gaps ${st.gaps}, faults ${JSON.stringify(st.faults)}`);

    if (DROP) {
      const before = await js("window.__pl.states.length");
      const samples0 = await js("window.__pl.samples");
      note(`sent w to the pod over USB: ${sendToPod("w") || "(no answer)"}`);
      const tDrop = Date.now();
      for (let i = 0; i < 240; i++) {
        const s = await js(`window.__pl.states.slice(${before})`);
        if (s.includes("ready")) break;
        await sleep(250);
      }
      const states = await js(`window.__pl.states.slice(${before})`);
      await sleep(5000);
      const after = await js(`(() => { const H = window.__pl; return { samples: H.samples, epochs: H.stream.epochs.length,
        reboot: H.stream.epochs.at(-1)?.is_reboot, first: H.stream.epochs.at(-1)?.first_t !== null, events: H.events.slice(-6),
        faults: H.stream.faults }; })()`);
      check(states.includes("lost") && states.includes("ready") && after.epochs === 2 && after.reboot && after.samples > samples0 && !after.faults.length,
        `reconnect by itself: states ${states.join(" ")} in ${((Date.now() - tDrop) / 1000).toFixed(1)} s, a new boot (epochs ${after.epochs}), ` +
        `samples ${samples0} -> ${after.samples}, events ${after.events.join(" ")}`);
    }

    await js("window.__pl.auto = false; window.__pl.write(window.__pl.stream.stopCommand())");
    await sleep(1500);
    await js("Capacitor.Plugins.PodLink.disconnect()");
    const states = await js("window.__pl.states");
    check(states.at(-1) === "closed", `disconnect(): states ${states.join(" ")}`);
    if (DROP) {
      const log = adb("logcat", "-d", "-s", "PodLink:*").split("\n").filter((l) => /PodLink/.test(l));
      for (const l of log.slice(-10)) note(l.replace(/^.*PodLink\s*:\s*/, "logcat: "));
    }
  } finally {
    close();
  }
}

main().then(() => { console.log(ok ? "PASS" : "FAIL"); process.exit(ok ? 0 : 1); },
  (e) => { console.log(`FAIL  ${e.message}`); process.exit(1); });
