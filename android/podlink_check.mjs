// Step 11 check: PodLinkPlugin on the phone (docs/ble-plan.md step 11), driven through the app's
// WebView over adb and the DevTools protocol (like pull-recording.mjs). Zero install.
//
//   node android/podlink_check.mjs [--install] [--seconds=20] [--drop]
//
// Needs: the phone on USB with debugging on, the pod awake nearby (D45 firmware: the CRP service)
// and not connected to anything else (close other Chrome tabs using it). Since D45 the plugin is a
// pipe for CRP packets (docs/ble.md 2) and the session runs in JS, so the page runs it here with the
// app's own PodCrpLink and PodNativeTransport (webapp/sources.js). It:
//   1. (--install) installs app\build\outputs\apk\debug\app-debug.apk and opens the app
//   2. Capacitor.Plugins.PodLink.connect(): the first time Android asks for Nearby devices (tap
//      Allow on the phone); it must resolve with mtu 247, and the pod sends nothing before HELLO
//   3. the connection order (docs/ble.md 7): HELLO, answered by the pod's (ver 0x0B, max_chunk 244,
//      push, device BPOD), then the INFO and STATUS RESPONSEs and PUSH = 1's, in that order
//   4. writes STREAM_LIVE(600) to CTRL and counts the pushes for --seconds: about 13 DATA frames/s
//      (16 samples each at 211 Hz) and a STATUS per second. Java never parses; here the packets go
//      through the app's own PodStream, which must see 0 gaps
//   5. --drop: the laptop sends w to the pod over USB (COM port found by USB ID): the sleep
//      prologue, then a reset. The plugin must say "lost", reconnect by itself (autoConnect) and say
//      "ready"; the page runs the order again on the new link, and the new boot streams from index 0
//   6. STOP (answered by stream_stopped), BYE, disconnect(); adb logcat -s PodLink shows the MTU and
//      the TX CCCD write

import { adb, page, install, sendToPod, sleep } from "./webview.mjs";

const arg = (k, d) => process.argv.find((a) => a.startsWith(`--${k}=`))?.split("=").slice(1).join("=") ?? d;
const SECONDS = Number(arg("seconds", "20"));
const INSTALL = process.argv.includes("--install");
const DROP = process.argv.includes("--drop");

let ok = true;
const check = (good, msg) => { console.log(`${good ? "PASS" : "FAIL"}  ${msg}`); ok &&= Boolean(good); };
const note = (msg) => console.log(`      ${msg}`);

async function main() {
  if (INSTALL) note(`installed: ${await install()}`);
  adb("logcat", "-c");
  const { js, close } = await page();
  try {
    // A small harness in the page: every packet kept with its arrival time and kind, every state,
    // the packets through the app's own PodStream (the plugin itself never parses them), then into
    // the CRP session of the current link, as PodNativeSource does.
    await js(`(async () => {
      const P = Capacitor.Plugins.PodLink;
      const { PodStream, PodCrpLink, PodNativeTransport, POD } = await import("./sources.js");
      const NAMES = { [POD.REG_INFO]: "INFO", [POD.REG_STATUS]: "STATUS", [POD.REG_PUSH]: "PUSH", [POD.REG_CTRL]: "CTRL" };
      const kind = (u8) => {
        const reg = u8.length >= 6 ? new DataView(u8.buffer, u8.byteOffset).getUint32(u8[0] === 0x0c ? 4 : 2, true) : 0;
        if (u8[0] === 0x00) return "nop";
        if (u8[0] === 0x01) return "hello";
        if (u8[0] === 0x0c) return "resp:" + (NAMES[reg] ?? reg.toString(16)) + (u8[2] ? ":" + u8[2] : "");
        if (u8[0] === 0x04 && u8[1] === 0) return reg === POD.REG_DATA ? "data" : reg === POD.REG_STATUS ? "status" : "push:" + reg.toString(16);
        return "other:" + u8[0];
      };
      const H = window.__pl = { pkts: [], states: [], samples: 0, events: [], auto: false, t: null, crp: null };
      H.stream = new PodStream(() => { H.samples++; }, (k, i) => H.events.push(k === "gap" ? "gap:" + i.kind + ":" + i.n : k));
      await P.removeAllListeners();
      await P.addListener("pod", (e) => {
        for (let i = 0; i < e.f.length; i++) {
          const u8 = Uint8Array.from(atob(e.f[i]), (c) => c.charCodeAt(0));
          H.pkts.push([kind(u8), e.w[i]]);
          H.stream.packet(u8, e.w[i]);              // the decoder first, in arrival order
          H.t?.deliver(u8);                         // then the session
        }
      });
      // steps 2 to 4 of the connection order on the plugin's link, over a fresh session
      H.order = async (link) => {
        H.t?.down();
        H.t = new PodNativeTransport(P, link);
        H.crp = new PodCrpLink(H.t, { heartbeatMs: 0 });   // no heartbeat on Bluetooth (docs/ble.md 2.2)
        const hello = await H.crp.hello();
        await H.crp.readFrame(POD.REG_INFO);
        await H.crp.readFrame(POD.REG_STATUS);
        await H.crp.push(true);
        return hello;
      };
      // step 5: what the decoder says to stream; resolves to CTRL's status (0 OK)
      H.command = async () => { const b = H.stream.command(); return b ? H.crp.ctrl(b) : null; };
      await P.addListener("podState", (e) => {
        H.states.push(e.state);
        if (e.state === "lost") H.t?.down();        // its open transactions fail now
        // like the app (step 12): after a reconnect, the order again on the new link, then the command
        if (e.state === "ready" && H.auto) H.order(e.link).then(H.command).catch((err) => H.events.push("order failed: " + err.message));
      });
      // the decoder's requests (a reboot without a new boot_id, a stale INFO): answer them
      H.stream.onEvent = ((orig) => (k, i) => {
        orig(k, i);
        if (k === "need-command" && H.auto) H.command().catch(() => {});
        if (k === "need-info" && H.auto) H.crp.readFrame(POD.REG_INFO).then(() => H.crp.readFrame(POD.REG_STATUS)).then(H.command).catch(() => {});
      })(H.stream.onEvent);
      return true;
    })()`);
    console.log(">>> connecting: if the phone asks for Nearby devices, tap Allow");
    const t0 = Date.now();
    // connect(), then the order at once: the plugin calls a ready link dead after 5 s without a
    // packet, and the pod is silent until our HELLO
    const r = await js(`(async () => {
      const H = window.__pl;
      const info = await Capacitor.Plugins.PodLink.connect().then((x) => x, (e) => ({ error: e.message }));
      if (info.error) return { info };
      const early = H.pkts.length;
      const o = await H.order(info.link).then((h) => ({ h }), (e) => ({ error: e.message }));
      return { info, early, ...o, kinds: H.pkts.map((p) => p[0]).filter((k) => k !== "nop") };
    })()`);
    const info = r.info;
    check(!info.error && info.mtu === 247 && r.early === 0,
      `connect() in ${((Date.now() - t0) / 1000).toFixed(1)} s: ${JSON.stringify(info)}; packets before HELLO ${r.early}`);
    if (info.error) return;
    const h = r.h ?? {};
    check(!r.error && h.version === 0x0b && h.maxChunkSize === 244 && h.flags === 1 && h.deviceId === 0x444f5042,
      `HELLO answered: ${r.error ?? JSON.stringify(h)}`);
    const want = ["hello", "resp:INFO", "resp:STATUS", "resp:PUSH"];
    check(JSON.stringify(r.kinds.slice(0, 4)) === JSON.stringify(want) && !r.kinds.slice(4).includes("data"),
      `the order's answers in order: ${r.kinds.slice(0, 6).join(" ")} (no DATA before the command)`);
    // now, while it's fresh: a debug build logs every event at verbose level and the 256 KiB log
    // buffer wraps within minutes
    const log = adb("logcat", "-d", "-s", "PodLink:*").split("\n").filter((l) => /PodLink/.test(l));
    for (const l of log.slice(-8)) note(l.replace(/^.*PodLink\s*:\s*/, "logcat: "));
    check(log.some((l) => /mtu 247/.test(l)) && log.some((l) => /TX CCCD write, status 0/.test(l)), "logcat shows the MTU and the TX CCCD write");
    if (r.error) return;

    const st0 = await js("(async () => { const st = await window.__pl.command(); window.__pl.auto = true; return st; })()");
    check(st0 === 0, `STREAM_LIVE(600) to CTRL: status ${st0}`);
    await sleep(5000);                            // STREAM_LIVE(600) sends 2.8 s of lead-in at once: count after it
    const n0 = await js("window.__pl.pkts.length");
    const s0 = await js("window.__pl.samples");
    await sleep(SECONDS * 1000);
    const st = await js(`(() => { const H = window.__pl; const f = H.pkts.slice(${n0});
      const span = (f.at(-1)[1] - f[0][1]) / 1000;
      const w = H.pkts.map((x) => x[1]); let back = 0; for (let i = 1; i < w.length; i++) if (w[i] < w[i - 1]) back++;
      return { span, data: f.filter((x) => x[0] === "data").length, status: f.filter((x) => x[0] === "status").length, back,
        samples: H.samples, inWindow: H.samples - ${s0}, gaps: H.stream.gaps.length, faults: H.stream.faults, perPkt: H.stream.ep?.status?.n_per_packet }; })()`);
    const fps = st.data / st.span;
    check(fps > 12 && fps < 14.5 && st.status >= SECONDS - 2 && st.back === 0,
      `${fps.toFixed(1)} DATA pushes/s and ${(st.status / st.span).toFixed(2)} STATUS pushes/s over ${st.span.toFixed(1)} s, arrival times in order`);
    check(st.gaps === 0 && !st.faults.length && st.samples > 0,
      `the app's PodStream on those packets: ${st.samples} samples (${(st.inWindow / st.span).toFixed(1)}/s in the window), ${st.perPkt}/pkt, gaps ${st.gaps}, faults ${JSON.stringify(st.faults)}`);

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
        `reconnect by itself, the order again: states ${states.join(" ")} in ${((Date.now() - tDrop) / 1000).toFixed(1)} s, a new boot (epochs ${after.epochs}), ` +
        `samples ${samples0} -> ${after.samples}, events ${after.events.join(" ")}`);
    }

    const stop = await js(`(async () => { const H = window.__pl; H.auto = false; const n = H.events.length;
      const st = await H.crp.ctrl(H.stream.stopCommand()); await new Promise((r) => setTimeout(r, 1500));
      return { st, stopped: H.events.slice(n).includes("stopped") }; })()`);
    check(stop.st === 0 && stop.stopped, `STOP to CTRL: status ${stop.st}, answered by stream_stopped ${stop.stopped}`);
    await js("(async () => { const H = window.__pl; H.crp.close(); await H.t.idle(); await Capacitor.Plugins.PodLink.disconnect(); })()");
    const states = await js("window.__pl.states");
    check(states.at(-1) === "closed", `BYE, disconnect(): states ${states.join(" ")}`);
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
