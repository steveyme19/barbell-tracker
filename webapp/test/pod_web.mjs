// PodWebSource's failure paths against a fake Web Bluetooth device carrying a fake CRP pod
// (fake_crp_pod.mjs; no radio, no browser):
//
//   node webapp/test/pod_web.mjs        (PYTHON = the venv's python, as an absolute path)
//
// The real-Chrome run (pod_browser.mjs) covers the happy path; these are the paths a pod on USB
// never takes on the bench: a connect that outlives start()'s timeout (the "zombie" source, step 6
// review), a newer session owning the pod meanwhile, the link dropping while starting or
// stopping, and a start that fails after the STREAM command went out (it must send STOP).
// Step 10: the reconnect loop (same boot: STREAM_FROM the next index; a pod that slept and woke:
// a new epoch from index 0, through connects that hang until the pickup), drops during a
// reconnect's setup, End while reconnecting (before or during the setup), a decoder request or a
// going_to_sleep during the setup, a late connect after End, and an outage's hold-off.
// The silence watchdog (11-07): a link that goes quiet with no disconnect event is called lost and
// reconnected (also in a browser that fires nothing for our own disconnect()), a page frozen for a
// second raises no false alarm, End stops the ticking, and End on a reconnect's link whose STOP
// goes unanswered still reports the stop timeout.
// D45: every connect runs the CRP connection order of docs/ble.md 7 (HELLO, READ INFO, READ STATUS,
// PUSH = 1, the command to CTRL), whose bytes the first test checks along with the capture, which
// podlink.py must replay to the same samples; End sends BYE before the disconnect, and End during a
// setup stuck before any HELLO sends one ahead of its STOP (the pod ignores a STOP outside a
// session). No stream command once End came: not from a setup that finishes late (stuck on its
// subscription, or behind a slow PUSH write), nor from a decoder request during the STOP handshake;
// each End that sends STOP must leave the fake pod's session hold off (BYE doesn't clear it). The fake device
// behaves like Chrome where it matters: new characteristic objects per connection (the old ones
// reject), notifications only after startNotifications, disconnect() fires gattserverdisconnected
// and cancels a pending connect (cancelOnDisconnect = false: browsers that can't). Frames come from
// the simulator's "clean" and "sleep-wake-rebase" scenarios (python analysis/make_fake_pod.py first).

import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { POD, PodWebSource } from "../sources.js";
import { replayCapture, worstDiff } from "./cdp.mjs";
import { FakeCrpPod, PKT, asStream, bootOf, bytes, firstIdx, fixtures, gate, hex, le32, nOf, settle, sleep, u32, until } from "./fake_crp_pod.mjs";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const PYTHON = process.env.PYTHON || join(ROOT, "analysis", ".venv", "Scripts", "python.exe");
const F = fixtures();
const { INFO, STATUS, DATAS, DATA, SW } = F;
const FAST = { backoffMs: [20, 40], connectTimeoutMs: 150 };
const NAMES = { [POD.RX]: "rx", [POD.TX]: "tx" };

const gone = () => Object.assign(new Error("GATT Server is disconnected."), { name: "NetworkError" });

class FakeChar extends EventTarget {
  constructor(name, dev, link) { super(); this.name = name; this.dev = dev; this.link = link; this.value = null; this.notifying = false; }
  live() { if (!this.dev.gatt.connected || this.link !== this.dev.link) throw gone(); }   // a dropped link's object
  async startNotifications() {
    this.live();
    this.dev.calls.push(`notify:${this.name}`);
    if (this.dev.notifyGate) { this.dev.subscribing = true; await this.dev.notifyGate; this.live(); }
    this.notifying = true;
    this.dev.onNotify?.(this);
    this.live();
  }
  async stopNotifications() {
    this.live();
    this.notifying = false;
    this.dev.calls.push(`unnotify:${this.name}`);
    this.dev.onUnnotify?.(this);
  }
  async writeValueWithResponse(b) {
    this.live();
    if (this.name !== "rx") throw new Error("not writable");
    const u8 = new Uint8Array(b.buffer ?? b, b.byteOffset ?? 0, b.byteLength).slice();
    const ms = this.dev.writeDelayOf?.(hex(u8)) ?? 0;
    if (ms) { await sleep(ms); this.live(); }
    if (!this.dev.writes.length) this.dev.subscribedFirst = Boolean(this.dev.chars.tx?.notifying);
    this.dev.writes.push(hex(u8));
    this.dev.pod.receive(u8);
  }
  notify(u8) {                        // the pod notifies: only on the current link, once subscribed
    if (!this.notifying || this.link !== this.dev.link || !this.dev.gatt.connected) return;
    this.dev.notified += 1;
    this.value = new DataView(u8.slice().buffer);
    this.dispatchEvent(new Event("characteristicvaluechanged"));
  }
}

class FakeDevice extends EventTarget {
  constructor({ cancelOnDisconnect = true, info = INFO, status = STATUS, silentDisconnect = false } = {}) {
    super();
    this.name = "BarPod-TEST";
    this.calls = [];
    this.writes = [];                 // every packet written to RX (hex), every link
    this.notified = 0;                // notifications delivered to a subscriber
    this.subscribedFirst = null;      // TX notifications were on at the first write
    this.chars = {};                  // the current link's characteristics
    this.link = 0;
    this.gate = null;                 // a promise connect() waits on (a slow / absent pod)
    this.notifyGate = null;           // a promise startNotifications() waits on (a stuck subscription)
    this.subscribing = false;         // one is waiting on it
    this.writeDelayOf = null;         // (hex) => ms that write takes (a write with response the link layer retries)
    this.onNotify = null;
    this.onUnnotify = null;
    this.mutedLink = 0;               // this connection's radio is gone and nobody says so: nothing arrives
    this.pod = new FakeCrpPod({ info, status });
    this.pod.stopped = F.STOPPED;
    const pending = new Set();        // connects waiting on the gate (Chrome cancels them on disconnect)
    const dev = this;
    this.gatt = {
      connected: false,
      async connect() {
        dev.calls.push("connect");
        if (dev.gate) {
          let reject;
          const cancel = new Promise((_, rej) => { reject = rej; });
          pending.add(reject);
          try { await Promise.race([dev.gate, cancel]); } finally { pending.delete(reject); }
        }
        if (!this.connected) { dev.link += 1; dev.chars = {}; dev.linkUp(); }   // a new connection: new attribute objects
        this.connected = true;
        const link = dev.link;
        return { getPrimaryService: async (u) => {
          if (u !== POD.SERVICE) throw Object.assign(new Error("No Services matching UUID found in Device."), { name: "NotFoundError" });
          return { getCharacteristic: async (c) => {
            if (link !== dev.link || !this.connected) throw gone();
            if (!NAMES[c]) throw Object.assign(new Error("No Characteristics matching UUID found in Service."), { name: "NotFoundError" });
            return (dev.chars[NAMES[c]] ??= new FakeChar(NAMES[c], dev, link));
          } };
        } };
      },
      disconnect() {
        dev.calls.push("disconnect");
        if (cancelOnDisconnect) for (const rej of [...pending]) rej(Object.assign(new Error("Connection attempt aborted."), { name: "AbortError" }));
        const was = this.connected;
        this.connected = false;
        // silentDisconnect: a browser that fires nothing even for our own disconnect()
        if (was) { dev.pod.down(); if (!silentDisconnect) dev.dispatchEvent(new Event("gattserverdisconnected")); }
      },
    };
  }
  /** The pod's packets go out as notifications on this connection's TX. */
  linkUp() {
    const link = this.link;
    this.pod.up((pkts) => {
      if (link !== this.link || !this.gatt.connected || link === this.mutedLink) return;
      for (const p of pkts) this.chars.tx?.notify(p);
    });
  }
  drop() { this.gatt.connected = false; this.pod.down(); this.dispatchEvent(new Event("gattserverdisconnected")); }
}

/** A test's closing End, where the STOP's answer isn't what's checked: the fake pod answers it, so
 * stop() doesn't wait out the decoder's stop wait (2.5 s and more) for nothing. */
const end = (s, pod) => { pod.answerStop = true; return s.stop(); };
let ok = true;
const check = (good, msg) => { console.log(`${good ? "PASS" : "FAIL"}  ${msg}`); ok &&= Boolean(good); };
const evOf = (ev) => (k, i) => ev.push(`${k}:${typeof i === "string" ? i : i?.reboot ? "reboot" : ""}`);
const connects = (d) => d.calls.filter((c) => c === "connect").length;

/** The STREAM command reached the pod (and its RESPONSE went out): the pod may stream now. */
async function commanded(d, n, what = "the STREAM command") {
  await until(() => d.pod.ctrls.length > n, 3000, what);
  await settle();
}

async function connectionOrder() {
  // pick(): the chooser's filter is the CRP service plus the name prefix
  let filters = null;
  const nav = globalThis.navigator;
  Object.defineProperty(nav, "bluetooth", { value: { requestDevice: async (o) => { filters = o.filters; return null; } }, configurable: true });
  await PodWebSource.pick();
  delete nav.bluetooth;
  check(JSON.stringify(filters) === JSON.stringify([{ services: [POD.SERVICE], namePrefix: "BarPod-" }]),
    `pick(): the chooser filters on the CRP service and the BarPod- prefix (${JSON.stringify(filters)})`);

  const d = new FakeDevice();
  d.pod.answerStop = true;
  const s = new PodWebSource(() => {}, d, { capture: true, startTimeoutMs: 2000 });
  const started = s.start();
  await commanded(d, 0);
  for (const f of DATAS.slice(0, 20)) d.pod.push(f);
  await started;
  const want = [PKT.hello, PKT.read(1, POD.REG_INFO), PKT.read(2, POD.REG_STATUS), PKT.write(3, POD.REG_PUSH, "01"),
    PKT.write(4, POD.REG_CTRL, "02015802")];
  const got = d.pod.rx.slice(0, 5);
  check(JSON.stringify(got) === JSON.stringify(want) && d.subscribedFirst === true,
    `the connection order: TX notifications on, then HELLO, READ INFO, READ STATUS, PUSH = 1, STREAM_LIVE(600) to CTRL (${got.join(" ")})`);
  await until(() => s.linkInfo().mtu !== null, 1000, "the MTU register");
  const pushed = DATAS.slice(0, 20).reduce((n, f) => n + nOf(f), 0);
  check(d.pod.rx[5] === PKT.read(5, POD.REG_LINK_MTU) && s.linkInfo().mtu === 247 && s.stream.stats.samples === pushed,
    `then the MTU register for the footer (MTU ${s.linkInfo().mtu}), ${s.stream.stats.samples} samples`);
  await s.stop();
  const rx = d.pod.rx.slice(6);
  check(PKT.ctrlOf(rx[0]) === "03" && rx.at(-1) === PKT.bye && d.calls.at(-1) === "disconnect" && s.stream.stats.stopped === 1 && !d.pod.hold,
    `End: STOP to CTRL, answered (the hold released), then BYE and the disconnect (${rx.join(" ")}; calls ${d.calls.slice(-3).join(" ")})`);
  const kinds = new Set(s.capture.log.map((r) => r[0]));
  const pkts = s.capture.log.filter((r) => r[0] === "pkt").length;
  const txs = s.capture.log.filter((r) => r[0] === "tx").map((r) => r[2]);
  check(pkts === d.notified && JSON.stringify(txs) === JSON.stringify(d.pod.rx.slice(0, -1)) && kinds.has("cmd") && kinds.has("stop"),
    `the capture: a pkt row per packet received (${pkts}), a tx row per packet sent but the BYE (${txs.length}), cmd and stop rows`);
  const ref = replayCapture(s.captureCSV(), PYTHON, ROOT);
  check(!ref.error && ref.samples.length === s.capture.samples.length && worstDiff(ref.samples, s.capture.samples) === 0 &&
    ref.gaps.length === s.stream.gaps.length,
    ref.error ? `podlink.py replay of the capture failed: ${ref.error}` :
      `podlink.py replays the capture to the same ${ref.samples.length} samples (worst diff ${worstDiff(ref.samples, s.capture.samples)}), gaps ${ref.gaps.length}`);
}

async function zombie() {
  const d = new FakeDevice({ cancelOnDisconnect: false });   // the late connect lands after all
  const g = gate();
  d.gate = g.p;
  let got = 0;
  const a = new PodWebSource(() => got++, d, { startTimeoutMs: 100 });
  const err = await a.start().then(() => null, (e) => e);
  g.open();
  await sleep(50);
  check(err && got === 0 && !d.gatt.connected && !d.chars.tx && !d.pod.rx.length,
    `zombie: start failed ("${err?.message}"), the late connection was dropped, nothing sent or streamed (calls ${d.calls.join(" ")})`);
}

async function zombieWithNewOwner() {
  const d = new FakeDevice({ cancelOnDisconnect: false });
  const g = gate();
  d.gate = g.p;
  let gotA = 0, gotB = 0;
  const a = new PodWebSource(() => gotA++, d, { startTimeoutMs: 100 });
  await a.start().catch(() => {});
  const b = new PodWebSource(() => gotB++, d, { startTimeoutMs: 2000 });
  const started = b.start();
  g.open();
  await commanded(d, 0);
  d.pod.push(DATA);
  await started;
  const disconnects = d.calls.filter((c) => c === "disconnect").length;
  const hellos = d.pod.rx.filter((h) => h === PKT.hello).length;
  check(gotA === 0 && gotB > 0 && disconnects === 1 && d.gatt.connected && hellos === 1,
    `a newer session owns the pod: the zombie leaves its connection alone (A ${gotA}, B ${gotB} samples, disconnects ${disconnects}, ` +
    `connected ${d.gatt.connected}, HELLOs ${hellos})`);
  await end(b, d.pod);
}

async function dropWhileStarting() {
  const d = new FakeDevice();
  d.onNotify = () => setTimeout(() => d.drop(), 10);
  const s = new PodWebSource(() => {}, d, { startTimeoutMs: 2000 });
  const t0 = Date.now();
  const err = await s.start().then(() => null, (e) => e);
  check(err && /dropped/.test(err.message) && Date.now() - t0 < 1000 && !d.pod.ctrls.includes("03"),
    `link drops while starting: fails at once ("${err?.message}"), no STOP to a dead link (CTRL ${d.pod.ctrls.join(" ")})`);
}

async function noDataSendsStop() {
  const d = new FakeDevice();
  const s = new PodWebSource(() => {}, d, { startTimeoutMs: 200 });
  const err = await s.start().then(() => null, (e) => e);
  check(err && d.pod.ctrls[0]?.startsWith("02") && d.pod.ctrls.at(-1) === "03" && !d.pod.hold && d.pod.rx.at(-1) === PKT.bye && d.calls.at(-1) === "disconnect",
    `no data after the STREAM command: STOP releases the pod's session hold, then BYE and disconnect (CTRL ${d.pod.ctrls.join(" ")})`);
}

async function dropWhileStopping() {
  const d = new FakeDevice();
  const s = new PodWebSource(() => {}, d, { startTimeoutMs: 2000, capture: true });
  const started = s.start();
  await commanded(d, 0);
  d.pod.push(DATA);
  await started;
  const stopping = s.stop();
  await sleep(30);
  d.drop();
  const t0 = Date.now();
  await stopping;
  const log = s.capture.log.map((r) => r[2]);
  check(Date.now() - t0 < 500 && !log.some((p) => p.startsWith("stop_timed_out")) && log.at(-1) === "finish" &&
    !s.stream.faults.includes("stop timed out"),
    `link drops while stopping: stop() ends at once, no "stop timed out", finish() counts the tail (faults ${JSON.stringify(s.stream.faults)})`);
}

async function reconnectSameBoot() {
  const d = new FakeDevice();
  const ev = [];
  let got = 0;
  const s = new PodWebSource(() => got++, d, { ...FAST, onEvent: (k, i) => ev.push(`${k}:${typeof i === "string" ? i : ""}`) });
  const started = s.start();
  await commanded(d, 0);
  d.pod.push(DATAS[0]);
  await started;
  const before = got;
  const n = d.pod.rx.length;
  d.drop();
  await until(() => ev.includes("link:back"), 2000, "the link to come back");
  const from = firstIdx(DATAS[0]) + nOf(DATAS[0]);
  const want = `0102${le32(bootOf(INFO))}${le32(from)}`;      // STREAM_FROM(seq 2, this boot, next index)
  d.pod.push(asStream(DATAS[1], 2));                         // the pod's answer: the next frame, RESYNC
  await sleep(20);
  const order = d.pod.rx.slice(n, n + 5).map((h) => (PKT.ctrlOf(h) !== null ? "ctrl" : h.slice(0, 2)));
  check(ev.filter((e) => e === "link:lost").length === 1 && d.pod.ctrls.at(-1) === want && got > before &&
    s.reconnects === 1 && s.stream.gaps.length === 0 && connects(d) === 2 && s.linkInfo().linked &&
    JSON.stringify(order) === JSON.stringify(["01", "06", "06", "04", "ctrl"]),
    `same boot: lost once, back after ${connects(d) - 1} reconnect, the whole order again (${order.join(" ")}), STREAM_FROM the next index ` +
    `(${d.pod.ctrls.at(-1)}), samples ${before} -> ${got}, gaps ${s.stream.gaps.length}`);
  await end(s, d.pod);
}

/** The radio loses the link and the browser never says so (no gattserverdisconnected, as Android
 * Chrome with Bluetooth switched off): the stream goes silent. While packets flow (one per 100 ms
 * here, the pod's STATUS per 1 s scaled down) nothing happens; once they stop, the watchdog calls
 * the link lost after silenceMs, drops it itself, and the reconnect resumes from the next index with
 * no gap (the 11-07 audit). */
async function silentLink(opts = {}) {
  const d = new FakeDevice(opts);
  const ev = [];
  let got = 0;
  const s = new PodWebSource(() => got++, d, { ...FAST, silenceMs: 300, onEvent: (k, i) => ev.push(`${k}:${typeof i === "string" ? i : ""}`) });
  const started = s.start();
  await commanded(d, 0);
  d.pod.push(DATAS[0]);
  await started;
  let k = 1;
  for (; k < 8; k++) { await sleep(100); d.pod.push(DATAS[k]); }      // 700 ms of traffic
  const falseAlarm = ev.includes("link:lost");
  const before = got;
  d.mutedLink = d.link;
  const t0 = Date.now();
  await until(() => ev.includes("link:lost"), 3000, "the watchdog to call the silent link lost");
  const ms = Date.now() - t0;
  await until(() => ev.includes("link:back"), 3000, "the link to come back");
  const from = firstIdx(DATAS[k - 1]) + nOf(DATAS[k - 1]);
  const want = `0102${le32(bootOf(INFO))}${le32(from)}`;      // STREAM_FROM(seq 2, this boot, next index)
  d.pod.push(asStream(DATAS[k], 2));
  await sleep(20);
  const lost = s.linkEvents.filter((e) => e.kind === "lost");
  check(!falseAlarm && ms >= 200 && ms < 1500 && lost.length === 1 && /^silent \d+ ms$/.test(lost[0].why ?? "") &&
    s.reconnects === 1 && d.pod.ctrls.at(-1) === want && got > before && s.stream.gaps.length === 0 && d.calls.includes("disconnect"),
    `a silent link (no disconnect event${opts.silentDisconnect ? ", not even for our own disconnect()" : ""}): no alarm during traffic, lost ${ms} ms after the last packet (${lost[0]?.why}), ` +
    `dropped by the source, back with STREAM_FROM the next index, samples ${before} -> ${got}, gaps ${s.stream.gaps.length}`);
  let ticks = 0;
  const tick = s.checkSilence.bind(s);
  s.checkSilence = () => { ticks++; tick(); };              // the interval calls this.checkSilence()
  await sleep(2 * s.tickMs);                                // ticks, but well inside silenceMs: no second "lost" before End
  await s.stop();
  const atEnd = ticks;
  await sleep(4 * s.tickMs);
  check(atEnd > 0 && ticks === atEnd, `End stops the watchdog (${atEnd} ticks up to the end, ${ticks - atEnd} after it)`);
}

/** A frozen or throttled page: the event loop is blocked for 1 s while the link is fine, and the
 * packet that queued meanwhile is delivered just after the overdue tick. The late tick must only
 * restart the clock, not call a live link dead; and a link muted after that is still caught. */
async function frozenPage() {
  const d = new FakeDevice();
  const ev = [];
  const s = new PodWebSource(() => {}, d, { ...FAST, silenceMs: 300, onEvent: (k, i) => ev.push(`${k}:${typeof i === "string" ? i : ""}`) });
  const started = s.start();
  await commanded(d, 0);
  d.pod.push(DATAS[0]);
  await started;
  let k = 1;
  for (; k < 4; k++) { await sleep(100); d.pod.push(DATAS[k]); }
  await new Promise((res) => setImmediate(() => {
    const t = Date.now() + 1000;
    while (Date.now() < t) { /* the page frozen for 1 s */ }
    setImmediate(() => { d.pod.push(DATAS[k++]); res(); });   // its queued packet, after the overdue tick
  }));
  for (let j = 0; j < 4; j++, k++) { await sleep(100); d.pod.push(DATAS[k]); }
  const links = ev.filter((e) => e.startsWith("link:"));
  d.mutedLink = d.link;
  const t0 = Date.now();
  await until(() => ev.includes("link:lost"), 3000, "the muted link to be called lost after the freeze");
  const ms = Date.now() - t0;
  check(!links.length && ms < 1500, `a page frozen for 1 s: no false "lost" on a live link (link events before the mute: ` +
    `${links.join(" ") || "none"}), then a muted link caught ${ms} ms later`);
  await end(s, d.pod);
}

const silentLinkNoEvent = () => silentLink({ silentDisconnect: true });

async function reconnectAfterSleep() {
  const d = new FakeDevice({ info: SW.info1, status: SW.status1 });
  const ev = [];
  let got = 0;
  const s = new PodWebSource(() => got++, d, { ...FAST, onEvent: evOf(ev) });
  const started = s.start();
  await commanded(d, 0);
  d.pod.push(SW.data1);
  await started;
  d.pod.push(SW.asleep);                                     // going_to_sleep (idle), then its disconnect
  const g = gate();
  d.gate = g.p;                                              // asleep: every connect hangs
  d.drop();
  await sleep(400);                                          // a few connects time out meanwhile
  const hung = connects(d) - 1;
  const asleepBanner = s.linkInfo().asleep;
  d.pod.wake(SW.info2, SW.status2);                          // the pickup: a new boot
  g.open();
  d.gate = null;
  await until(() => ev.includes("link:back"), 3000, "the woken pod");
  const want = `0102${le32(bootOf(SW.info2))}${le32(0)}`;    // STREAM_FROM(new boot, 0): the pickup itself
  const before = got;
  d.pod.push(SW.data2);
  await sleep(20);
  check(ev.includes("sleep:idle") && ev.includes("link:asleep") && !ev.includes("link:lost") && asleepBanner === "idle" &&
    hung >= 2 && ev.includes("epoch:reboot") && d.pod.ctrls.at(-1) === want && got > before && s.asleep === null,
    `sleep and wake: "asleep (${asleepBanner})", ${hung} connects timed out while it slept, back as a new epoch, ` +
    `STREAM_FROM(new boot, 0) (${d.pod.ctrls.at(-1)}), samples resume (${before} -> ${got})`);
  await end(s, d.pod);
}

async function endWhileReconnecting() {
  const d = new FakeDevice();
  const s = new PodWebSource(() => {}, d, { ...FAST, capture: true });
  const started = s.start();
  await commanded(d, 0);
  d.pod.push(DATA);
  await started;
  d.gate = new Promise(() => {});                            // the pod is gone for good
  d.drop();
  await sleep(100);
  const sent = d.pod.rx.length;
  const t0 = Date.now();
  await s.stop();
  const ms = Date.now() - t0;
  const n = connects(d);
  await sleep(300);
  const after = connects(d);
  check(ms < 200 && d.pod.rx.length === sent && after === n && s.capture.log.at(-1)[2] === "finish",
    `End while reconnecting: stop() in ${ms} ms, nothing (STOP, BYE) to a dead link, no connect tries after it (${n} -> ${after})`);
}

async function outageHoldsOff() {
  const d = new FakeDevice();
  const ev = [];
  const s = new PodWebSource(() => {}, d, { ...FAST, onEvent: (k, i) => ev.push(`${k}:${i ?? ""}`) });
  const started = s.start();
  await commanded(d, 0);
  d.pod.push(DATA);
  await started;
  const t0 = Date.now();
  s.outage(300);
  await until(() => ev.includes("link:back"), 2000, "the end of the outage");
  const ms = Date.now() - t0;
  check(ev.includes("link:lost") && ms >= 300 && ms < 600 && connects(d) === 2,
    `outage(300): link lost, no reconnect for ${ms} ms, then back on the first try`);
  await end(s, d.pod);
}

// a session streaming (cmd_seq 1), for the tests below
async function streaming(d, opts = {}) {
  const ev = [];
  const s = new PodWebSource(() => {}, d, { ...FAST, capture: true, onEvent: evOf(ev), ...opts });
  const started = s.start();
  await commanded(d, 0);
  d.pod.push(d.pod.info === INFO ? DATA : SW.data1);
  await started;
  return { s, ev };
}

async function dropDuringReconnectSetup() {
  const d = new FakeDevice();
  const { s, ev } = await streaming(d);
  let tries = 0;
  d.onNotify = () => { if (++tries <= 2) d.drop(); };        // the first two reconnects drop mid-setup
  d.drop();
  await until(() => ev.includes("link:back"), 3000, "the link to come back");
  check(ev.filter((e) => e === "link:lost").length === 1 && s.linkEvents.filter((e) => e.kind === "lost").length === 1 &&
    connects(d) === 4 && s.reconnects === 1 && s.linkInfo().linked,
    `drops during the reconnect's setup: one outage reported (${ev.filter((e) => e.startsWith("link")).join(" ")}), back on try ${connects(d) - 1}`);
  await end(s, d.pod);
}

async function endDuringStuckSetup() {
  const d = new FakeDevice();
  const { s } = await streaming(d);
  d.pod.holdInfo = true;                                     // the reconnect connects, then its INFO read hangs
  d.pod.wake(SW.info2, SW.status2);                          // (a woken pod: a late INFO would be a new epoch)
  let late = null;
  d.pod.onRx = (u8) => {                                     // it lands as End's BYE goes out, after finish()
    if (u8[0] !== 0x02) return;
    const n0 = d.notified;
    d.pod.release();
    late = d.notified - n0;
  };
  d.drop();
  await until(() => d.pod.reading, 2000, "the reconnect's INFO read");
  const epochs = s.stream.epochs.length;
  const t0 = Date.now();
  await s.stop();
  const ms = Date.now() - t0;
  await sleep(50);
  const log = s.capture.log.map((r) => r[2]);
  check(d.pod.ctrls.at(-1) === "03" && ms >= 900 && ms < 1500 && log.at(-1) === "finish" && late === 1 &&
    s.stream.epochs.length === epochs && !d.gatt.connected,
    `End while a reconnect's setup is stuck on a live link: STOP still released the hold (CTRL ${d.pod.ctrls.join(" ")}), ` +
    `after ${ms} ms; the INFO that landed after finish added nothing (delivered ${late}, epochs ${epochs} -> ${s.stream.epochs.length}, ` +
    `capture ends with ${log.at(-1)})`);
}

async function endDuringStuckSubscribe() {
  const d = new FakeDevice();
  const { s } = await streaming(d);
  const g = gate();
  d.notifyGate = g.p;                                        // the reconnect connects, then its TX subscription hangs
  d.drop();
  await until(() => d.subscribing, 2000, "the reconnect's subscription");
  const n = d.pod.rx.length;
  const c = d.pod.ctrls.length;
  const t0 = Date.now();
  await s.stop();
  const ms = Date.now() - t0;                                // 1 s for the setup, 1 s for a STOP nobody can answer
  g.open();
  await sleep(20);
  const rx = d.pod.rx.slice(n);
  // no HELLO answered on that link yet: without our own HELLO first the pod would ignore the STOP
  check(rx[0] === PKT.hello && PKT.ctrlOf(rx[1]) === "03" && JSON.stringify(d.pod.ctrls.slice(c)) === JSON.stringify(["03"]) &&
    !d.pod.hold && s.capture.log.at(-1)[2] === "finish" && !d.gatt.connected && ms < 2500,
    `End while a reconnect's subscription is stuck: a HELLO, then the STOP the pod takes (${rx.map((h) => PKT.ctrlOf(h) ?? h.slice(0, 2)).join(" ")}), ` +
    `no stream command, disconnected, in ${ms} ms (the unanswered STOP waits 1 s at most)`);
}

async function endDuringSetupThenHandshake() {
  const d = new FakeDevice();
  const { s } = await streaming(d);
  d.pod.answerStop = true;
  d.pod.holdInfo = true;
  d.drop();
  await until(() => d.pod.reading, 2000, "the reconnect's INFO read");
  const n = d.pod.ctrls.length;
  setTimeout(() => d.pod.release(), 100);                    // the setup finishes a moment after End
  await s.stop();
  const after = d.pod.ctrls.slice(n);
  // no stream command once End came (it could land after the STOP, and BYE leaves the hold on);
  // the setup's PUSH is on, so the STOP is the full handshake
  check(JSON.stringify(after) === JSON.stringify(["03"]) && !d.pod.hold && s.stream.stats.stopped === 1 && !s.stream.faults.length,
    `End during a reconnect's setup that then finishes: no stream command, the STOP handshake runs on the new link and is answered ` +
    `(stopped ${s.stream.stats.stopped}, faults ${JSON.stringify(s.stream.faults)}, CTRL after the drop ${after.join(" ")}, hold ${d.pod.hold})`);
}

/** The same End, but the pod never answers the STOP on that live link: a real stop timeout, and
 * it must be reported as one. The link isn't "back" yet at that point (End came before the
 * reconnect reported it), which used to hide the fault and the capture's stop_timed_out row. */
async function endDuringSetupStopUnanswered() {
  const d = new FakeDevice();
  const { s } = await streaming(d);
  d.pod.holdInfo = true;
  d.drop();
  await until(() => d.pod.reading, 2000, "the reconnect's INFO read");
  setTimeout(() => d.pod.release(), 100);                    // the setup finishes a moment after End
  await s.stop();
  const calls = s.capture.log.filter((r) => r[0] === "call").map((r) => r[2]);
  check(s.stream.faults.includes("stop timed out") && calls.includes("stop_timed_out=1") && d.pod.ctrls.at(-1) === "03",
    `End on a reconnect's link whose STOP goes unanswered: the stop timeout is reported (faults ${JSON.stringify(s.stream.faults)}, ` +
    `capture calls ${calls.filter((c) => c.startsWith("stop_timed_out")).join(" ") || "none"})`);
}

async function endThenSubscribeCompletes() {
  const d = new FakeDevice();
  const { s } = await streaming(d);
  const g = gate();
  d.notifyGate = g.p;                                        // the reconnect's TX subscription hangs...
  d.drop();
  await until(() => d.subscribing, 2000, "the reconnect's subscription");
  const c = d.pod.ctrls.length;
  const stopping = s.stop();
  await sleep(1150);                                         // ...past stop()'s 1 s wait, into stopOnly's (its answer can't be heard)
  g.open();                                                  // then the setup's whole order runs inside that wait
  await stopping;
  await sleep(50);
  const after = d.pod.ctrls.slice(c);
  check(JSON.stringify(after) === JSON.stringify(["03"]) && !d.pod.hold && d.pod.rx.at(-1) === PKT.bye,
    `End, then the stuck subscription completes during the STOP's wait: the setup sends no stream command after the STOP, ` +
    `the hold is released (CTRL after End ${after.join(" ")}, hold ${d.pod.hold}, then BYE)`);
}

async function endBehindSlowPush() {
  const d = new FakeDevice();
  const { s } = await streaming(d);
  let slowed = 0;                                            // the reconnect's PUSH = 1 takes 1.3 s (link-layer retries)
  d.writeDelayOf = (h) => (h === PKT.write(3, POD.REG_PUSH, "01") && !slowed++ ? 1300 : 0);
  d.drop();
  await until(() => slowed > 0, 3000, "the reconnect's PUSH write");
  const c = d.pod.ctrls.length;
  await s.stop();                                            // its 1 s wait runs out: the STOP queues behind PUSH
  await sleep(50);
  const after = d.pod.ctrls.slice(c);
  check(JSON.stringify(after) === JSON.stringify(["03"]) && !d.pod.hold && d.pod.rx.at(-1) === PKT.bye,
    `End behind a reconnect's slow PUSH write: the order finishes after the STOP went, and no stream command follows it ` +
    `(CTRL after End ${after.join(" ")}, hold ${d.pod.hold}, then BYE)`);
}

async function requestDuringStop() {
  const d = new FakeDevice();
  const { s, ev } = await streaming(d);
  const head = u32(STATUS, 8);
  for (const f of DATAS) { if (firstIdx(f) > head + 20) break; d.pod.push(f); }   // the decoder past STATUS's head
  const n = d.pod.ctrls.length;
  // after the STOP's RESPONSE: a STATUS that reads as "rebooted, same boot_id" (need-command), then stream_stopped
  d.pod.onCtrl = (v) => (v[0] === POD.OP_STOP ? [STATUS, F.STOPPED] : []);
  await s.stop();
  await sleep(50);
  const after = d.pod.ctrls.slice(n);
  check(ev.some((e) => e.startsWith("need-command")) && JSON.stringify(after) === JSON.stringify(["03"]) && !d.pod.hold &&
    d.pod.rx.at(-1) === PKT.bye,
    `a decoder request during End's STOP handshake: no stream command after the STOP (CTRL ${after.join(" ")}, hold ${d.pod.hold})`);
}

async function requestDuringSetup() {
  const d = new FakeDevice();
  const { s } = await streaming(d);
  // the decoder past the pod's next STATUS head: that STATUS reads as "rebooted, same boot_id"
  const head = u32(STATUS, 8);
  for (const f of DATAS) { if (firstIdx(f) > head + 20) break; d.pod.push(f); }
  const n = d.pod.ctrls.length;
  const ev = [];
  s.onEvent = (k) => ev.push(k);
  d.drop();
  await until(() => s.reconnects === 1, 2000, "the reconnect");
  await sleep(50);
  const after = d.pod.ctrls.slice(n);
  check(ev.includes("need-command") && after.length === 1 && after[0].startsWith("01"),
    `a request during the connection order (need-command at the STATUS read): one STREAM command, not two (CTRL ${after.join(" ")})`);
  await end(s, d.pod);
}

async function sleepDuringSetup() {
  const d = new FakeDevice({ info: SW.info1, status: SW.status1 });
  const { s, ev } = await streaming(d);
  d.pod.onPushOn = () => (s.asleep ? [] : [SW.asleep]);    // going_to_sleep as it reconnects: right after PUSH = 1
  d.drop();
  await until(() => ev.includes("link:asleep"), 2000, "the asleep report");
  const g = gate();
  d.gate = g.p;                                              // and then it really sleeps
  d.drop();
  await sleep(200);
  const before = ev.slice();
  d.pod.onPushOn = null;
  d.pod.wake(SW.info2, SW.status2);
  g.open();
  d.gate = null;
  await until(() => ev.includes("link:back"), 3000, "the woken pod");
  check(!before.includes("link:back") && before.filter((e) => e === "link:lost").length === 1 && s.linkInfo().linked &&
    s.asleep === null && ev.includes("epoch:reboot"),
    `going_to_sleep during a reconnect's setup: no false "back" (${before.filter((e) => e.startsWith("link")).join(" ")}), asleep until the wake, then back`);
  await end(s, d.pod);
}

async function lateConnectAfterEnd() {
  const d = new FakeDevice({ cancelOnDisconnect: false });   // a browser that can't cancel the connect
  const { s } = await streaming(d, { connectTimeoutMs: 100, backoffMs: [20, 5000] });
  const g = gate();
  d.gate = g.p;
  d.drop();
  await sleep(200);                                          // the reconnect's connect timed out, still pending
  await s.stop();                                            // End during the next backoff
  g.open();                                                  // ...and the old connect lands after End
  await sleep(50);
  check(!d.gatt.connected, `a connect that lands after End is dropped (connected ${d.gatt.connected}, calls ${d.calls.slice(-3).join(" ")})`);
}

for (const t of [connectionOrder, zombie, zombieWithNewOwner, dropWhileStarting, noDataSendsStop, dropWhileStopping,
  reconnectSameBoot, silentLink, silentLinkNoEvent, frozenPage, reconnectAfterSleep, endWhileReconnecting, outageHoldsOff,
  dropDuringReconnectSetup, endDuringStuckSetup, endDuringStuckSubscribe, endDuringSetupThenHandshake, endDuringSetupStopUnanswered, endThenSubscribeCompletes,
  endBehindSlowPush, requestDuringSetup, requestDuringStop, sleepDuringSetup, lateConnectAfterEnd]) {
  try { await t(); } catch (e) { check(false, `${t.name}: ${e.stack}`); }
}
console.log(ok ? "PASS" : "FAIL");
process.exit(ok ? 0 : 1);
