// PodNativeSource (the app's pod source, docs/ble-plan.md step 12) against a fake PodLink plugin
// that behaves like android/.../PodLinkPlugin.java (no phone, no radio):
//
//   node webapp/test/pod_native.mjs
//
// The fake hands frames in as the plugin does: base64, batched, each with an elapsedRealtime
// arrival time; INFO and STATUS before connect() resolves; "lost" then "ready" (a new link number)
// around a reconnect, with the new link's INFO and STATUS before "ready"; a write for an old link
// refused. Covered: the start (the clock pair, the held frames, STREAM_LIVE), a same-boot reconnect
// (STREAM_FROM the next index), a sleep and wake (a new epoch from 0), a going_to_sleep during the
// plugin's reconnect (no false "back"), a decoder request during it (one command, not two), the STOP
// handshake, End while the link is down, End while a reconnect's command is still in flight (STOP
// anyway), a going_to_sleep in that window (no "back"), a failed write on a live link (retried, with
// backoff, until it works), End while that retry waits or while a re-read runs (no stream command
// after the STOP), End during the plugin's connection order ("linking": STOP once it's "ready"), and
// a start that fails (STOP sent, listeners gone).
// Frames come from the simulator's "clean" and "sleep-wake-rebase" scenarios (python
// analysis/make_fake_pod.py first).

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { POD, PodNativeSource } from "../sources.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const all = readFileSync(join(HERE, "..", "..", "analysis", "data", "fake_pod_frames.csv"), "utf8")
  .trim().split("\n").slice(1).map((l) => l.split(","));
const clean = all.filter((r) => r[0] === "clean" && r[2] === "frame").map((r) => r[4]);
const INFO = clean.find((h) => h.startsWith("20"));
const STATUS = clean.find((h) => h.startsWith("10"));
const DATAS = clean.filter((h) => h.startsWith("01") && h.slice(26, 28) === "01");   // cmd_seq 1
const STOPPED = clean.find((h) => h.startsWith("10") && (parseInt(h.slice(2, 4), 16) & POD.S_STREAM_STOPPED));
const sw = all.filter((r) => r[0] === "sleep-wake-rebase" && r[2] === "frame").map((r) => r[4]);
const SW = { info1: sw.find((h) => h.startsWith("20")), status1: sw.find((h) => h.startsWith("10")), data1: sw.find((h) => h.startsWith("01")),
  asleep: sw.find((h) => h.startsWith("10") && (parseInt(h.slice(2, 4), 16) & POD.S_GOING_TO_SLEEP)) };
SW.info2 = sw.find((h) => h.startsWith("20") && h !== SW.info1);
SW.status2 = sw.slice(sw.indexOf(SW.info2)).find((h) => h.startsWith("10"));
SW.data2 = sw.find((h) => h.startsWith("01") && h.slice(26, 28) === "02");
const u32 = (hex, at) => parseInt(hex.slice(at * 2, at * 2 + 8).match(/../g).reverse().join(""), 16);
const le32 = (v) => [0, 8, 16, 24].map((b) => ((v >>> b) & 255).toString(16).padStart(2, "0")).join("");
const asStream = (hex, seq) => `${hex.slice(0, 24)}01${seq.toString(16).padStart(2, "0")}${hex.slice(28)}`;   // RESYNC, cmd_seq
const b64 = (hex) => Buffer.from(hex, "hex").toString("base64");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const WALL0 = 1_800_000_000_000;              // the phone's wall clock when elapsedRealtime was ELAPSED0
const ELAPSED0 = 5_000_000;

class FakePlugin {
  constructor({ info = INFO, status = STATUS } = {}) {
    this.ls = { pod: [], podState: [] };
    this.writes = [];
    this.attempts = [];
    this.calls = [];
    this.info = info;
    this.status = status;
    this.link = 0;
    this.t0 = Date.now();
    this.connectError = null;
    this.answerStop = false;
    this.writeDelay = 0;                // ms a write takes to go out (a slow link)
    this.failWrites = 0;                // this many next writes fail (a GATT busy, not a dropped link)
    this.rereadDelay = 0;               // ms reread()'s two GATT reads take
  }
  elapsed() { return ELAPSED0 + (Date.now() - this.t0); }
  async addListener(name, fn) {
    this.ls[name].push(fn);
    return { remove: async () => { this.ls[name] = this.ls[name].filter((f) => f !== fn); } };
  }
  emit(hexes, w = this.elapsed()) {
    const e = { f: hexes.map(b64), w: hexes.map(() => w) };
    for (const fn of this.ls.pod) fn(e);
  }
  state(state) { for (const fn of this.ls.podState) fn({ state, status: 0, mtu: 247, link: this.link }); }
  async connect(opts) {
    this.calls.push(`connect:${JSON.stringify(opts)}`);
    if (this.connectError) throw new Error(this.connectError);
    this.link++;
    this.emit([this.info, this.status]);                 // the plugin flushes them before resolving
    return { name: "BarPod-TEST", address: "F5:EA:43:DB:E2:6C", mtu: 247, link: this.link, wallMs: WALL0 + (this.elapsed() - ELAPSED0), elapsedMs: this.elapsed() };
  }
  async write({ b, link }) {
    this.attempts.push(Buffer.from(b, "base64").toString("hex"));   // in call order, done or not
    if (this.writeDelay) await sleep(this.writeDelay);
    if (link !== undefined && link !== this.link) throw new Error("Stale link: the pod reconnected since.");
    if (this.failWrites > 0) { this.failWrites--; throw new Error("Write failed (status 133)."); }
    const hex = Buffer.from(b, "base64").toString("hex");
    this.writes.push(hex);
    if (hex === "03" && this.answerStop) setTimeout(() => this.emit([STOPPED]), 5);
  }
  async reread() {
    this.calls.push("reread");
    if (this.rereadDelay) await sleep(this.rereadDelay);
    this.emit([this.info, this.status]);
  }
  async disconnect() { this.calls.push("disconnect"); this.state("closed"); }
  /** A drop and the plugin's own reconnect: "lost", the new link's INFO and STATUS, then any
   * notification that came in right after the subscription (the same flush), "ready". Nothing can
   * come before the reads: notifications only start with the DATA CCCD, the order's last step. */
  reconnect({ info = this.info, status = this.status, after = [] } = {}) {
    this.state("lost");
    this.link++;
    this.info = info;
    this.status = status;
    this.emit([info, status, ...after]);
    this.state("ready");
  }
}

let ok = true;
const check = (good, msg) => { console.log(`${good ? "PASS" : "FAIL"}  ${msg}`); ok &&= Boolean(good); };
const events = () => { const ev = []; return { ev, onEvent: (k, i) => ev.push(`${k}:${typeof i === "string" ? i : i?.reboot ? "reboot" : ""}`) }; };

async function streaming(plugin, data = DATAS[0]) {
  const E = events();
  let got = 0;
  const s = new PodNativeSource(() => got++, { plugin, onEvent: E.onEvent, capture: true, startTimeoutMs: 500 });
  const started = s.start();
  await sleep(20);
  plugin.emit([data]);
  await started;
  return { s, ev: E.ev, got: () => got };
}

async function start() {
  const p = new FakePlugin();
  const { s, got } = await streaming(p);
  const log = s.capture.log;
  const info = log.find((r) => r[0] === "frame" && r[2].startsWith("20"));
  // the held INFO's arrival: the plugin's wall clock at that elapsedRealtime (within a few ms)
  const expected = WALL0 + (info ? 0 : NaN);
  check(p.calls[0] === 'connect:{"any":false}' && p.writes[0] === "02015802" && got() > 0 && s.started &&
    Math.abs(info[1] - expected) < 1000 && s.name === "BarPod-TEST" && s.mtu === 247 && s.linkInfo().mtu === 247,
    `start: connect, INFO/STATUS held until the clock pair, then STREAM_LIVE(600) (${p.writes[0]}), ${got()} samples, ` +
    `INFO stamped ${(info[1] - WALL0).toFixed(0)} ms after the phone's wall clock origin`);
  await s.stop();
}

async function reconnectSameBoot() {
  const p = new FakePlugin();
  const { s, ev, got } = await streaming(p);
  const before = got();
  p.reconnect();
  await sleep(20);
  const from = u32(DATAS[0], 4) + parseInt(DATAS[0].slice(2, 4), 16);
  const want = `0102${le32(u32(INFO, 4))}${le32(from)}`;
  p.emit([asStream(DATAS[1], 2)]);
  await sleep(10);
  check(ev.includes("link:lost") && ev.includes("link:back") && p.writes.at(-1) === want && s.reconnects === 1 && got() > before && s.stream.gaps.length === 0,
    `same-boot reconnect: lost, back, STREAM_FROM the next index for link 2 (${p.writes.at(-1)}), samples ${before} -> ${got()}, gaps ${s.stream.gaps.length}`);
  await s.stop();
}

async function sleepAndWake() {
  const p = new FakePlugin({ info: SW.info1, status: SW.status1 });
  const { s, ev } = await streaming(p, SW.data1);
  p.emit([SW.asleep]);                                   // going_to_sleep, then the pod's disconnect
  p.state("lost");
  await sleep(10);
  const banner = s.linkInfo();
  p.state("lost");                                       // the plugin's failed autoConnect tries say nothing new
  p.reconnect({ info: SW.info2, status: SW.status2 });   // the pickup: a new boot
  await sleep(20);
  const want = `0102${le32(u32(SW.info2, 4))}${le32(0)}`;
  check(ev.includes("link:asleep") && !ev.includes("link:lost") && banner.asleep === "idle" && !banner.linked &&
    ev.includes("epoch:reboot") && p.writes.at(-1) === want && ev.includes("link:back") && s.asleep === null,
    `sleep and wake: "asleep (idle)" once, a new epoch, STREAM_FROM(new boot, 0) (${p.writes.at(-1)}), back`);
  await s.stop();
}

async function sleepDuringReconnect() {
  const p = new FakePlugin({ info: SW.info1, status: SW.status1 });
  const { s, ev } = await streaming(p, SW.data1);
  const writes = p.writes.length;
  p.reconnect({ after: [SW.asleep] });                   // the pod says going_to_sleep as the link comes back
  await sleep(20);
  const mid = ev.slice();
  p.state("lost");                                       // ...and disconnects
  await sleep(10);
  check(!mid.includes("link:back") && mid.includes("link:asleep") && p.writes.length === writes && !s.linkInfo().linked,
    `going_to_sleep during the plugin's reconnect: no false "back", no command (${mid.filter((e) => e.startsWith("link")).join(" ")})`);
  await s.stop();
}

async function requestDuringReconnect() {
  const p = new FakePlugin();
  const { s } = await streaming(p);
  const head = u32(STATUS, 8);
  for (const f of DATAS) { if (u32(f, 4) > head + 20) break; p.emit([f]); }   // past the STATUS's head
  const ev = [];
  s.onEvent = (k) => ev.push(k);
  const writes = p.writes.length;
  p.reconnect();                                         // that STATUS reads as "rebooted, same boot_id"
  await sleep(30);
  const after = p.writes.slice(writes);
  check(ev.includes("need-command") && after.length === 1 && after[0].startsWith("01"),
    `a decoder request during the plugin's reconnect: one command at "ready", not two (${after.join(" ")})`);
  await s.stop();
}

async function stopHandshake() {
  const p = new FakePlugin();
  p.answerStop = true;
  const { s, got } = await streaming(p);
  await s.stop();
  const n = got();
  p.emit([DATAS[2]]);                                    // anything after the end is ignored
  check(p.writes.at(-1) === "03" && s.stream.stats.stopped === 1 && !s.stream.faults.length && p.calls.at(-1) === "disconnect" &&
    p.ls.pod.length === 0 && p.ls.podState.length === 0 && got() === n,
    `End: STOP answered (stopped ${s.stream.stats.stopped}), disconnect, listeners removed, nothing after it`);
}

async function stopWhileDown() {
  const p = new FakePlugin();
  const { s } = await streaming(p);
  p.state("lost");
  const writes = p.writes.length;
  const t0 = Date.now();
  await s.stop();
  const log = s.capture.log.map((r) => r[2]);
  check(Date.now() - t0 < 200 && p.writes.length === writes && log.at(-1) === "finish" && p.calls.at(-1) === "disconnect",
    `End while the link is down: no STOP to a dead link, finish(), disconnect (${Date.now() - t0} ms)`);
}

async function stopDuringResume() {
  const p = new FakePlugin();
  p.answerStop = true;
  const { s, ev } = await streaming(p);
  p.writeDelay = 150;                                    // the reconnect's STREAM_FROM is slow to go out
  p.reconnect();
  await sleep(20);                                       // "ready", its command still in flight
  const writes = p.writes.length;
  await s.stop();
  const after = p.writes.slice(writes);
  check(after.length === 2 && after[0].startsWith("0102") && after[1] === "03" && s.stream.stats.stopped === 1 && ev.includes("link:back") &&
    !s.stream.faults.length,
    `End during the plugin's reconnect (ready, command in flight): the command, then STOP answered (${after.join(" ")}), back once`);
}

async function sleepDuringResume() {
  const p = new FakePlugin({ info: SW.info1, status: SW.status1 });
  const { s, ev } = await streaming(p, SW.data1);
  p.writeDelay = 100;
  p.reconnect();
  await sleep(20);
  p.emit([SW.asleep]);                                   // going_to_sleep while the resume's write is out
  await sleep(150);
  check(!ev.includes("link:back") && s.linkLost && s.asleep !== null,
    `going_to_sleep while the reconnect's command is in flight: no "back" (${ev.filter((e) => e.startsWith("link")).join(" ")})`);
  p.writeDelay = 0;
  await s.stop();
}

async function resumeRetry() {
  const p = new FakePlugin();
  const { s, ev } = await streaming(p);
  p.failWrites = 1;                                      // the first STREAM_FROM after the reconnect fails
  const writes = p.writes.length;
  p.reconnect();
  await sleep(200);
  const early = ev.includes("link:back");
  await sleep(1100);
  // the failed try used cmd_seq 2 (the pod never saw it): the retry is STREAM_FROM with cmd_seq 3
  check(!early && ev.includes("link:back") && p.writes.length === writes + 1 && p.writes.at(-1).startsWith("0103") && !s.linkLost,
    `a reconnect's command that fails on a live link: tried again 1 s later, then back (${p.writes.slice(writes).join(" ")})`);
  await s.stop();
}

async function stopDuringRetry() {
  const p = new FakePlugin();
  p.answerStop = true;
  const { s } = await streaming(p);
  p.failWrites = 1;                                      // the reconnect's command fails, its retry waits 1 s
  const n = p.attempts.length;
  p.reconnect();
  await sleep(200);
  await s.stop();                                        // End during that wait: STOP now
  await sleep(1200);
  const after = p.attempts.slice(n);
  check(after.at(-1) === "03" && after.filter((h) => h.startsWith("01")).length === 1,
    `End while a reconnect's failed command waits for its retry: STOP last, no stream command after it (${after.join(" ")})`);
}

async function retryUntilItWorks() {
  const p = new FakePlugin();
  const { s, ev } = await streaming(p);
  p.failWrites = 3;                                      // 3 fails on a live link: no new "ready" will come
  p.reconnect();
  await sleep(7300);                                     // tries at 0, 1, 3 and 7 s
  const tries = p.attempts.filter((h) => h.startsWith("01")).length;    // STREAM_FROM; the first STREAM_LIVE is 02
  check(ev.includes("link:back") && !s.linkLost && p.writes.at(-1).startsWith("01") && tries === 4,
    `a reconnect's command failing 3 times on a live link: still tried (1, 2, 4 s apart), then back (${tries} tries)`);
  await s.stop();
}

async function newLinkDuringRetry() {
  const p = new FakePlugin();
  const { s } = await streaming(p);
  p.failWrites = 1;                                      // link 2's command fails: its retry waits 1 s
  p.reconnect();
  await sleep(200);
  const n = p.attempts.length;
  p.reconnect();                                         // link 3 meanwhile: its own resume sends the command
  await sleep(1300);
  const after = p.attempts.slice(n);
  check(after.filter((h) => h.startsWith("01")).length === 1 && !s.linkLost,
    `a newer link during a retry's wait: one command, from the newer link's resume (${after.join(" ")})`);
  await s.stop();
}

async function stopDuringOrder() {
  const p = new FakePlugin();
  p.answerStop = true;
  const { s } = await streaming(p);
  p.state("lost");
  p.state("linking");                                    // the plugin's autoConnect has the link: its order runs
  const n = p.attempts.length;
  const stopped = s.stop();
  await sleep(100);
  p.link++;
  p.emit([p.info, p.status]);
  p.state("ready");                                      // the order is done: End's STOP goes over the new link
  await stopped;
  const after = p.attempts.slice(n);
  check(after.length === 1 && after[0] === "03" && p.writes.at(-1) === "03" && s.stream.stats.stopped === 1,
    `End during the plugin's connection order: waits for "ready", then STOP over the new link (${after.join(" ")}), no stream command`);
}

async function stopDuringReread() {
  const p = new FakePlugin();
  const { s } = await streaming(p);
  p.rereadDelay = 150;
  // the reconnect brings a boot_mismatch STATUS: the resume re-reads INFO before its command
  const mismatch = `10${(parseInt(STATUS.slice(2, 4), 16) | POD.S_BOOT_MISMATCH).toString(16).padStart(2, "0")}${STATUS.slice(4)}`;
  const n = p.attempts.length;
  p.reconnect({ after: [mismatch] });
  await sleep(20);
  await s.stop();                                        // End during the re-read
  await sleep(200);
  const after = p.attempts.slice(n);
  check(p.calls.includes("reread") && after.length === 1 && after[0] === "03",
    `End while a reconnect re-reads INFO: STOP only, no stream command after it (${after.join(" ")})`);
}

async function startFails() {
  const p1 = new FakePlugin();
  p1.connectError = "No pod found. Bump it to wake it and keep it close.";
  const s1 = new PodNativeSource(() => {}, { plugin: p1 });
  const e1 = await s1.start().then(() => null, (e) => e);
  const p2 = new FakePlugin();
  const s2 = new PodNativeSource(() => {}, { plugin: p2, startTimeoutMs: 100 });
  const e2 = await s2.start().then(() => null, (e) => e);
  check(e1?.message.startsWith("No pod found") && p1.ls.pod.length === 0 && e2 && p2.writes[0]?.startsWith("02") && p2.writes.at(-1) === "03" &&
    p2.calls.at(-1) === "disconnect",
    `a start that fails: the plugin's reason ("${e1?.message}"); no data after the command: STOP, disconnect ("${e2?.message}")`);
}

for (const t of [start, reconnectSameBoot, sleepAndWake, sleepDuringReconnect, requestDuringReconnect, stopHandshake, stopWhileDown,
  stopDuringResume, sleepDuringResume, resumeRetry, stopDuringRetry, retryUntilItWorks, newLinkDuringRetry, stopDuringOrder, stopDuringReread, startFails]) {
  try { await t(); } catch (e) { check(false, `${t.name}: ${e.stack}`); }
}
console.log(ok ? "PASS" : "FAIL");
process.exit(ok ? 0 : 1);
