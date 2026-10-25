// PodWebSource's failure paths against a fake Web Bluetooth device (no radio, no browser):
//
//   node webapp/test/pod_web.mjs
//
// The real-Chrome run (pod_browser.mjs) covers the happy path; these are the paths a pod on USB
// never takes on the bench: a connect that outlives start()'s timeout (the "zombie" source, step 6
// review), a newer session owning the pod meanwhile, the link dropping while starting or
// stopping, and a start that fails after the STREAM command went out (it must send STOP).
// Step 10: the reconnect loop (same boot: STREAM_FROM the next index; a pod that slept and woke:
// a new epoch from index 0, through connects that hang until the pickup), drops during a
// reconnect's setup, End while reconnecting (before or during the setup), a decoder request or a
// going_to_sleep during the setup, a late connect after End, and an outage's hold-off. The fake
// behaves like Chrome where it matters: new characteristic objects per connection (the old ones
// reject), notifications only after startNotifications, disconnect() fires gattserverdisconnected
// and cancels a pending connect (cancelOnDisconnect = false: browsers that can't). Frames come
// from the simulator's "clean" and "sleep-wake-rebase" scenarios (python analysis/make_fake_pod.py
// first).

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { POD, PodWebSource } from "../sources.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const all = readFileSync(join(HERE, "..", "..", "analysis", "data", "fake_pod_frames.csv"), "utf8")
  .trim().split("\n").slice(1).map((l) => l.split(","));
const rows = all.filter((r) => r[0] === "clean" && r[2] === "frame");
const INFO = rows.find((r) => r[4].startsWith("20"))[4];
const STATUS = rows.find((r) => r[4].startsWith("10"))[4];
const DATAS = rows.filter((r) => r[4].startsWith("01") && r[4].slice(26, 28) === "01").map((r) => r[4]);   // cmd_seq 1
const DATA = DATAS[0];
const STOPPED = rows.find((r) => r[4].startsWith("10") && (parseInt(r[4].slice(2, 4), 16) & POD.S_STREAM_STOPPED))[4];
// DATA hex: type n pkt_seq(2) first_idx(4) t0(4) flags cmd_seq, little-endian
const u32 = (hex, at) => parseInt(hex.slice(at * 2, at * 2 + 8).match(/../g).reverse().join(""), 16);
const firstIdx = (hex) => u32(hex, 4);
const nOf = (hex) => parseInt(hex.slice(2, 4), 16);
const asStream = (hex, seq) => `${hex.slice(0, 24)}01${seq.toString(16).padStart(2, "0")}${hex.slice(28)}`;   // RESYNC, cmd_seq
const le32 = (v) => [0, 8, 16, 24].map((b) => ((v >>> b) & 255).toString(16).padStart(2, "0")).join("");
// a pod that sleeps and wakes: boot 1 (INFO, STATUS, DATA, going_to_sleep), then boot 2 from 0
const sw = all.filter((r) => r[0] === "sleep-wake-rebase" && r[2] === "frame").map((r) => r[4]);
const SW = {
  info1: sw.find((h) => h.startsWith("20")),
  status1: sw.find((h) => h.startsWith("10")),
  data1: sw.find((h) => h.startsWith("01")),
  asleep: sw.find((h) => h.startsWith("10") && (parseInt(h.slice(2, 4), 16) & POD.S_GOING_TO_SLEEP)),
};
SW.info2 = sw.find((h) => h.startsWith("20") && h !== SW.info1);
SW.status2 = sw.slice(sw.indexOf(SW.info2)).find((h) => h.startsWith("10"));
SW.data2 = sw.find((h) => h.startsWith("01") && h.slice(26, 28) === "02");
const bootOf = (infoHex) => u32(infoHex, 4);
const FAST = { backoffMs: [20, 40], connectTimeoutMs: 150 };
const dv = (hex) => new DataView(Uint8Array.from(hex.match(/../g), (h) => parseInt(h, 16)).buffer);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const NAMES = { [POD.DATA]: "data", [POD.CTRL]: "ctrl", [POD.STATUS]: "status", [POD.INFO]: "info" };

const gone = () => Object.assign(new Error("GATT Server is disconnected."), { name: "NetworkError" });

class FakeChar extends EventTarget {
  constructor(name, dev, link) { super(); this.name = name; this.dev = dev; this.link = link; this.value = null; this.notifying = false; }
  live() { if (!this.dev.gatt.connected || this.link !== this.dev.link) throw gone(); }   // a dropped link's object
  async readValue() {
    this.live();
    if (this.name === "info" && this.dev.readGate) { this.dev.reading = true; await this.dev.readGate; }
    this.live();
    return dv(this.name === "info" ? this.dev.info : this.dev.status);
  }
  async startNotifications() {
    this.live();
    this.dev.calls.push(`notify:${this.name}`);
    this.notifying = true;
    this.dev.onNotify?.(this);
    this.live();
  }
  async stopNotifications() {
    this.live();
    this.notifying = false;
    this.dev.calls.push(`unnotify:${this.name}`);
    this.dev.onUnnotify?.(this);      // e.g. let a held read finish now: after finish(), before the disconnect
  }
  async writeValueWithResponse(b) {
    this.live();
    const hex = Buffer.from(b).toString("hex");
    this.dev.writes.push(hex);
    if (hex === "03" && this.dev.answerStop) setTimeout(() => this.dev.chars.data?.push(STOPPED), 5);
  }
  push(hex) {                         // the pod notifies: only on the current link, once subscribed
    if (!this.notifying || this.link !== this.dev.link || !this.dev.gatt.connected) return;
    this.value = dv(hex);
    this.dispatchEvent(new Event("characteristicvaluechanged"));
  }
}

class FakeDevice extends EventTarget {
  constructor({ cancelOnDisconnect = true } = {}) {
    super();
    this.name = "BarPod-TEST";
    this.calls = [];
    this.writes = [];
    this.chars = {};                  // the current link's characteristics
    this.link = 0;
    this.gate = null;                 // a promise connect() waits on (a slow / absent pod)
    this.readGate = null;             // a promise the INFO read waits on (a slow setup)
    this.reading = false;
    this.onNotify = null;
    this.onUnnotify = null;
    this.answerStop = false;          // answer a STOP write with a stream_stopped STATUS
    this.info = INFO;                 // what the INFO / STATUS reads return (a woken pod: new ones)
    this.status = STATUS;
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
        if (!this.connected) { dev.link += 1; dev.chars = {}; }   // a new connection: new attribute objects
        this.connected = true;
        const link = dev.link;
        return { getPrimaryService: async () => ({ getCharacteristic: async (u) => {
          if (link !== dev.link || !this.connected) throw gone();
          return (dev.chars[NAMES[u]] ??= new FakeChar(NAMES[u], dev, link));
        } }) };
      },
      disconnect() {
        dev.calls.push("disconnect");
        if (cancelOnDisconnect) for (const rej of [...pending]) rej(Object.assign(new Error("Connection attempt aborted."), { name: "AbortError" }));
        const was = this.connected;
        this.connected = false;
        if (was) dev.dispatchEvent(new Event("gattserverdisconnected"));
      },
    };
  }
  drop() { this.gatt.connected = false; this.dispatchEvent(new Event("gattserverdisconnected")); }
}

function gate() { let open; const p = new Promise((r) => { open = r; }); return { p, open }; }
const until = async (cond, ms, what) => {
  const t0 = Date.now();
  while (!cond()) { if (Date.now() - t0 > ms) throw new Error(`timed out waiting for ${what}`); await sleep(5); }
};

let ok = true;
const check = (good, msg) => { console.log(`${good ? "PASS" : "FAIL"}  ${msg}`); ok &&= Boolean(good); };

async function zombie() {
  const d = new FakeDevice({ cancelOnDisconnect: false });   // the late connect lands after all
  const g = gate();
  d.gate = g.p;
  let got = 0;
  const a = new PodWebSource(() => got++, d, { startTimeoutMs: 100 });
  const err = await a.start().then(() => null, (e) => e);
  g.open();
  await sleep(50);
  check(err && got === 0 && !d.gatt.connected && !d.chars.data,
    `zombie: start failed ("${err?.message}"), the late connection was dropped, nothing streamed (calls ${d.calls.join(" ")})`);
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
  await sleep(50);
  d.chars.data.push(DATA);
  await started;
  const disconnects = d.calls.filter((c) => c === "disconnect").length;
  check(gotA === 0 && gotB > 0 && disconnects === 1 && d.gatt.connected,
    `a newer session owns the pod: the zombie leaves its connection alone (A ${gotA}, B ${gotB} samples, disconnects ${disconnects}, connected ${d.gatt.connected})`);
  await b.stop();
}

async function dropWhileStarting() {
  const d = new FakeDevice();
  d.onNotify = () => setTimeout(() => d.drop(), 10);
  const s = new PodWebSource(() => {}, d, { startTimeoutMs: 2000 });
  const t0 = Date.now();
  const err = await s.start().then(() => null, (e) => e);
  check(err && /dropped/.test(err.message) && Date.now() - t0 < 1000 && !d.writes.includes("03"),
    `link drops while starting: fails at once ("${err?.message}"), no STOP to a dead link (writes ${d.writes.join(" ")})`);
}

async function noDataSendsStop() {
  const d = new FakeDevice();
  const s = new PodWebSource(() => {}, d, { startTimeoutMs: 200 });
  const err = await s.start().then(() => null, (e) => e);
  check(err && d.writes[0]?.startsWith("02") && d.writes.at(-1) === "03" && d.calls.at(-1) === "disconnect",
    `no data after the STREAM command: STOP releases the pod's session hold, then disconnect (writes ${d.writes.join(" ")})`);
}

async function dropWhileStopping() {
  const d = new FakeDevice();
  const s = new PodWebSource(() => {}, d, { startTimeoutMs: 2000, capture: true });
  const started = s.start();
  await sleep(30);
  d.chars.data.push(DATA);
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
  await sleep(30);
  d.chars.data.push(DATAS[0]);
  await started;
  const before = got;
  d.drop();
  await until(() => ev.includes("link:back"), 2000, "the link to come back");
  const from = firstIdx(DATAS[0]) + nOf(DATAS[0]);
  const want = `0102${le32(bootOf(INFO))}${le32(from)}`;      // STREAM_FROM(seq 2, this boot, next index)
  d.chars.data.push(asStream(DATAS[1], 2));                  // the pod's answer: the next frame, RESYNC
  await sleep(20);
  const connects = d.calls.filter((c) => c === "connect").length;
  check(ev.filter((e) => e === "link:lost").length === 1 && d.writes.at(-1) === want && got > before &&
    s.reconnects === 1 && s.stream.gaps.length === 0 && connects === 2 && s.linkInfo().linked,
    `same boot: lost once, back after ${connects - 1} reconnect, STREAM_FROM the next index (${d.writes.at(-1)}), ` +
    `samples ${before} -> ${got}, gaps ${s.stream.gaps.length}`);
  await s.stop();
}

async function reconnectAfterSleep() {
  const d = new FakeDevice();
  d.info = SW.info1; d.status = SW.status1;
  const ev = [];
  let got = 0;
  const s = new PodWebSource(() => got++, d, { ...FAST, onEvent: (k, i) => ev.push(`${k}:${typeof i === "string" ? i : i?.reboot ? "reboot" : ""}`) });
  const started = s.start();
  await sleep(30);
  d.chars.data.push(SW.data1);
  await started;
  d.chars.data.push(SW.asleep);                              // going_to_sleep (idle), then its disconnect
  const g = gate();
  d.gate = g.p;                                              // asleep: every connect hangs
  d.drop();
  await sleep(400);                                          // a few connects time out meanwhile
  const hung = d.calls.filter((c) => c === "connect").length - 1;
  const asleepBanner = s.linkInfo().asleep;
  d.info = SW.info2; d.status = SW.status2;                  // the pickup: a new boot
  g.open();
  d.gate = null;
  await until(() => ev.includes("link:back"), 3000, "the woken pod");
  const want = `0102${le32(bootOf(SW.info2))}${le32(0)}`;    // STREAM_FROM(new boot, 0): the pickup itself
  const before = got;
  d.chars.data.push(SW.data2);
  await sleep(20);
  check(ev.includes("sleep:idle") && ev.includes("link:asleep") && !ev.includes("link:lost") && asleepBanner === "idle" &&
    hung >= 2 && ev.includes("epoch:reboot") && d.writes.at(-1) === want && got > before && s.asleep === null,
    `sleep and wake: "asleep (${asleepBanner})", ${hung} connects timed out while it slept, back as a new epoch, ` +
    `STREAM_FROM(new boot, 0) (${d.writes.at(-1)}), samples resume (${before} -> ${got})`);
  await s.stop();
}

async function endWhileReconnecting() {
  const d = new FakeDevice();
  const s = new PodWebSource(() => {}, d, { ...FAST, capture: true });
  const started = s.start();
  await sleep(30);
  d.chars.data.push(DATA);
  await started;
  d.gate = new Promise(() => {});                            // the pod is gone for good
  d.drop();
  await sleep(100);
  const writes = d.writes.length;
  const t0 = Date.now();
  await s.stop();
  const ms = Date.now() - t0;
  const n = d.calls.filter((c) => c === "connect").length;
  await sleep(300);
  const after = d.calls.filter((c) => c === "connect").length;
  check(ms < 200 && d.writes.length === writes && after === n && s.capture.log.at(-1)[2] === "finish",
    `End while reconnecting: stop() in ${ms} ms, no STOP to a dead link, no connect tries after it (${n} -> ${after})`);
}

async function outageHoldsOff() {
  const d = new FakeDevice();
  const ev = [];
  const s = new PodWebSource(() => {}, d, { ...FAST, onEvent: (k, i) => ev.push(`${k}:${i ?? ""}`) });
  const started = s.start();
  await sleep(30);
  d.chars.data.push(DATA);
  await started;
  const t0 = Date.now();
  s.outage(300);
  await until(() => ev.includes("link:back"), 2000, "the end of the outage");
  const ms = Date.now() - t0;
  check(ev.includes("link:lost") && ms >= 300 && ms < 600 && d.calls.filter((c) => c === "connect").length === 2,
    `outage(300): link lost, no reconnect for ${ms} ms, then back on the first try`);
  await s.stop();
}

// a session streaming on DATA (cmd_seq 1), for the tests below
async function streaming(d, opts = {}) {
  const ev = [];
  const s = new PodWebSource(() => {}, d, { ...FAST, capture: true, onEvent: (k, i) => ev.push(`${k}:${typeof i === "string" ? i : i?.reboot ? "reboot" : ""}`), ...opts });
  const started = s.start();
  await sleep(30);
  d.chars.data.push(d.info === INFO ? DATA : SW.data1);
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
  const connects = d.calls.filter((c) => c === "connect").length;
  check(ev.filter((e) => e === "link:lost").length === 1 && s.linkEvents.filter((e) => e.kind === "lost").length === 1 &&
    connects === 4 && s.reconnects === 1 && s.linkInfo().linked,
    `drops during the reconnect's setup: one outage reported (${ev.filter((e) => e.startsWith("link")).join(" ")}), back on try ${connects - 1}`);
  await s.stop();
}

async function endDuringStuckSetup() {
  const d = new FakeDevice();
  const { s } = await streaming(d);
  const g = gate();
  d.readGate = g.p;                                          // the reconnect connects, then its INFO read hangs
  d.info = SW.info2;                                         // (a woken pod: a late INFO would be a new epoch)
  d.onUnnotify = () => g.open();                             // it lands while close() unsubscribes, after finish()
  d.drop();
  await until(() => d.reading, 2000, "the reconnect's INFO read");
  const epochs = s.stream.epochs.length;
  const t0 = Date.now();
  await s.stop();
  const ms = Date.now() - t0;
  await sleep(50);
  const log = s.capture.log.map((r) => r[2]);
  check(d.writes.at(-1) === "03" && ms >= 900 && ms < 1500 && log.at(-1) === "finish" && s.stream.epochs.length === epochs && !d.gatt.connected,
    `End while a reconnect's setup is stuck on a live link: STOP still released the hold (writes ${d.writes.join(" ")}), ` +
    `after ${ms} ms; the INFO that landed after finish added nothing (epochs ${epochs} -> ${s.stream.epochs.length}, capture ends with ${log.at(-1)})`);
}

async function endDuringSetupThenHandshake() {
  const d = new FakeDevice();
  const { s } = await streaming(d);
  d.answerStop = true;
  const g = gate();
  d.readGate = g.p;
  d.drop();
  await until(() => d.reading, 2000, "the reconnect's INFO read");
  setTimeout(() => { d.readGate = null; g.open(); }, 100);   // the setup finishes a moment after End
  await s.stop();
  check(d.writes.filter((w) => w === "03").length === 1 && d.writes.at(-2)?.startsWith("01") && s.stream.stats.stopped === 1 &&
    !s.stream.faults.length,
    `End during a reconnect's setup that then finishes: the STOP handshake runs on the new link and is answered ` +
    `(stopped ${s.stream.stats.stopped}, faults ${JSON.stringify(s.stream.faults)}, writes ${d.writes.join(" ")})`);
}

async function requestDuringSetup() {
  const d = new FakeDevice();
  const { s } = await streaming(d);
  // the decoder past the pod's next STATUS head: that STATUS reads as "rebooted, same boot_id"
  const head = u32(STATUS, 8);
  for (const f of DATAS) { if (firstIdx(f) > head + 20) break; d.chars.data.push(f); }
  const writes = d.writes.length;
  const ev = [];
  s.onEvent = (k) => ev.push(k);
  d.drop();
  await until(() => s.reconnects === 1, 2000, "the reconnect");
  await sleep(50);
  const after = d.writes.slice(writes);
  check(ev.includes("need-command") && after.length === 1 && after[0].startsWith("01"),
    `a request during the connection order (need-command at the STATUS read): one STREAM command, not two (writes ${after.join(" ")})`);
  await s.stop();
}

async function sleepDuringSetup() {
  const d = new FakeDevice();
  d.info = SW.info1; d.status = SW.status1;
  const { s, ev } = await streaming(d);
  d.onNotify = (ch) => { if (ch.name === "data" && !s.asleep) ch.push(SW.asleep); };   // going_to_sleep as it reconnects
  d.drop();
  await until(() => ev.includes("link:asleep"), 2000, "the asleep report");
  const g = gate();
  d.gate = g.p;                                              // and then it really sleeps
  d.drop();
  await sleep(200);
  const before = ev.slice();
  d.onNotify = null;
  d.info = SW.info2; d.status = SW.status2;
  g.open();
  d.gate = null;
  await until(() => ev.includes("link:back"), 3000, "the woken pod");
  check(!before.includes("link:back") && before.filter((e) => e === "link:lost").length === 1 && s.linkInfo().linked &&
    s.asleep === null && ev.includes("epoch:reboot"),
    `going_to_sleep during a reconnect's setup: no false "back" (${before.filter((e) => e.startsWith("link")).join(" ")}), asleep until the wake, then back`);
  await s.stop();
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

for (const t of [zombie, zombieWithNewOwner, dropWhileStarting, noDataSendsStop, dropWhileStopping,
  reconnectSameBoot, reconnectAfterSleep, endWhileReconnecting, outageHoldsOff,
  dropDuringReconnectSetup, endDuringStuckSetup, endDuringSetupThenHandshake, requestDuringSetup, sleepDuringSetup,
  lateConnectAfterEnd]) {
  try { await t(); } catch (e) { check(false, `${t.name}: ${e.stack}`); }
}
console.log(ok ? "PASS" : "FAIL");
process.exit(ok ? 0 : 1);
