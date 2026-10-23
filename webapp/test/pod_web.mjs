// PodWebSource's failure paths against a fake Web Bluetooth device (no radio, no browser):
//
//   node webapp/test/pod_web.mjs
//
// The real-Chrome run (pod_browser.mjs) covers the happy path; these are the paths a pod on USB
// never takes on the bench: a connect that outlives start()'s timeout (the "zombie" source, step 6
// review), a newer session owning the pod meanwhile, the link dropping while starting or
// stopping, and a start that fails after the STREAM command went out (it must send STOP).
// Frames come from the simulator's "clean" scenario (python analysis/make_fake_pod.py first).

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { POD, PodWebSource } from "../sources.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const rows = readFileSync(join(HERE, "..", "..", "analysis", "data", "fake_pod_frames.csv"), "utf8")
  .trim().split("\n").slice(1).map((l) => l.split(",")).filter((r) => r[0] === "clean" && r[2] === "frame");
const INFO = rows.find((r) => r[4].startsWith("20"))[4];
const STATUS = rows.find((r) => r[4].startsWith("10"))[4];
const DATA = rows.find((r) => r[4].startsWith("01") && r[4].slice(26, 28) === "01")[4];   // cmd_seq 1
const dv = (hex) => new DataView(Uint8Array.from(hex.match(/../g), (h) => parseInt(h, 16)).buffer);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const NAMES = { [POD.DATA]: "data", [POD.CTRL]: "ctrl", [POD.STATUS]: "status", [POD.INFO]: "info" };

class FakeChar extends EventTarget {
  constructor(name, dev) { super(); this.name = name; this.dev = dev; this.value = null; }
  async readValue() { return dv(this.name === "info" ? INFO : STATUS); }
  async startNotifications() { this.dev.calls.push(`notify:${this.name}`); this.dev.onNotify?.(); }
  async stopNotifications() { this.dev.calls.push(`unnotify:${this.name}`); }
  async writeValueWithResponse(b) { this.dev.writes.push(Buffer.from(b).toString("hex")); }
  push(hex) { this.value = dv(hex); this.dispatchEvent(new Event("characteristicvaluechanged")); }
}

class FakeDevice extends EventTarget {
  constructor() {
    super();
    this.name = "BarPod-TEST";
    this.calls = [];
    this.writes = [];
    this.chars = {};
    this.gate = null;                 // a promise connect() waits on (a slow / absent pod)
    this.onNotify = null;
    const dev = this;
    this.gatt = {
      connected: false,
      async connect() {
        dev.calls.push("connect");
        if (dev.gate) await dev.gate;
        this.connected = true;
        return { getPrimaryService: async () => ({ getCharacteristic: async (u) => (dev.chars[NAMES[u]] ??= new FakeChar(NAMES[u], dev)) }) };
      },
      disconnect() { dev.calls.push("disconnect"); this.connected = false; },
    };
  }
  drop() { this.gatt.connected = false; this.dispatchEvent(new Event("gattserverdisconnected")); }
}

function gate() { let open; const p = new Promise((r) => { open = r; }); return { p, open }; }

let ok = true;
const check = (good, msg) => { console.log(`${good ? "PASS" : "FAIL"}  ${msg}`); ok &&= Boolean(good); };

async function zombie() {
  const d = new FakeDevice();
  const g = gate();
  d.gate = g.p;
  let got = 0;
  const a = new PodWebSource(() => got++, d, { startTimeoutMs: 100 });
  const err = await a.start().then(() => null, (e) => e);
  g.open();
  await sleep(50);
  check(err && got === 0 && d.calls.filter((c) => c === "disconnect").length === 2 && !d.chars.data,
    `zombie: start failed ("${err?.message}"), the late connection was dropped, nothing streamed (calls ${d.calls.join(" ")})`);
}

async function zombieWithNewOwner() {
  const d = new FakeDevice();
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

for (const t of [zombie, zombieWithNewOwner, dropWhileStarting, noDataSendsStop, dropWhileStopping]) {
  try { await t(); } catch (e) { check(false, `${t.name}: ${e.stack}`); }
}
console.log(ok ? "PASS" : "FAIL");
process.exit(ok ? 0 : 1);
