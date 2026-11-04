// PodNativeSource (the app's pod source, docs/ble-plan.md step 12) against a fake PodLink plugin
// that behaves like android/.../PodLinkPlugin.java, carrying a fake CRP pod (fake_crp_pod.mjs; no
// phone, no radio):
//
//   node webapp/test/pod_native.mjs
//
// The fake plugin is a dumb pipe as the real one is: the pod's packets come in base64, batched
// (one packet's answers together), each with an elapsedRealtime arrival time; our packets go out
// through write({ b, link }) in call order, one at a time, and a write for an old link is refused.
// "lost" then "ready" (a new link number, TX notifications on) around a reconnect; since D45 the
// connection order on the new link (HELLO, READ INFO, READ STATUS, PUSH = 1, the command) is this
// side's. Covered: the start (the clock pair, a held packet, the order's bytes, STREAM_LIVE), a
// same-boot reconnect (STREAM_FROM the next index), a sleep and wake (a new epoch from 0), a
// going_to_sleep during the reconnect's order (no false "back", no command), a decoder request
// during it (one command, not two), the STOP handshake and BYE (the fake pod's session hold off
// after every End that sends STOP), a decoder request during that handshake (no stream command
// after the STOP), End while the link is down, End
// while a reconnect's command is still in flight (STOP anyway), a going_to_sleep in that window (no
// "back"), a failed CTRL write on a live link (retried, with backoff, until it works), End while
// that retry waits or while a re-read runs (no stream command after the STOP), End during the
// plugin's GATT chain ("linking": the order, then STOP, once it's "ready"), End while a reconnect's
// order fails after HELLO (MTU 23: the STOP still goes), and a start that fails (STOP sent,
// listeners gone). Frames come from the simulator's "clean" and "sleep-wake-rebase"
// scenarios (python analysis/make_fake_pod.py first).

import { POD, PodNativeSource } from "../sources.js";
import { FakeCrpPod, PKT, asStream, bootOf, bytes, fixtures, hex, le32, nOf, settle, sleep, u32, until } from "./fake_crp_pod.mjs";

const F = fixtures();
const { INFO, STATUS, DATAS, SW } = F;
const b64 = (h) => Buffer.from(h, "hex").toString("base64");
const WALL0 = 1_800_000_000_000;              // the phone's wall clock when elapsedRealtime was ELAPSED0
const ELAPSED0 = 5_000_000;

class FakePlugin {
  constructor({ info = INFO, status = STATUS } = {}) {
    this.ls = { pod: [], podState: [] };
    this.pod = new FakeCrpPod({ info, status });
    this.pod.stopped = F.STOPPED;
    this.writes = [];                   // packets written (hex), in order
    this.attempts = [];                 // every write() (hex), in call order, done or not
    this.links = [];                    // the link each write() named
    this.calls = [];
    this.link = 0;
    this.linked = false;                // a link is ready (the plugin's step READY)
    this.t0 = Date.now();
    this.connectError = null;
    this.early = [];                    // packets flushed before connect() resolves (the pod sends none before HELLO)
    this.writeDelay = 0;                // ms a write takes to go out (a slow link)
    this.failCtrl = 0;                  // this many next CTRL writes fail (a GATT busy, not a dropped link)
    this.q = Promise.resolve();         // the plugin runs one GATT operation at a time
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
  /** CTRL values written, in call order, done or not (hex). */
  ctrlAttempts() { return this.attempts.map(PKT.ctrlOf).filter((v) => v !== null); }
  /** The link is ready: the pod's packets reach JS while it is this link. */
  linkUp() {
    this.linked = true;
    const link = this.link;
    this.pod.up((pkts) => { if (this.linked && link === this.link) this.emit(pkts.map(hex)); });
  }
  async connect(opts) {
    this.calls.push(`connect:${JSON.stringify(opts)}`);
    if (this.connectError) throw new Error(this.connectError);
    this.link++;
    this.linkUp();
    if (this.early.length) this.emit(this.early);         // the plugin flushes before resolving
    setImmediate(() => this.state("ready"));               // as the plugin does, right after (JS ignores it while starting)
    return { name: "BarPod-TEST", address: "F5:EA:43:DB:E2:6C", mtu: 247, link: this.link, wallMs: WALL0 + (this.elapsed() - ELAPSED0), elapsedMs: this.elapsed() };
  }
  write({ b, link }) {
    const h = Buffer.from(b, "base64").toString("hex");
    this.attempts.push(h);
    this.links.push(link);
    const run = async () => {
      if (this.writeDelay) await sleep(this.writeDelay);
      if (!this.linked) throw new Error("The pod isn't connected.");
      if (link !== undefined && link !== this.link) throw new Error("Stale link: the pod reconnected since.");
      if (this.failCtrl > 0 && PKT.ctrlOf(h) !== null) { this.failCtrl--; throw new Error("Write failed (status 133)."); }
      this.writes.push(h);
      this.pod.receive(bytes(h));
    };
    const p = this.q.then(run);
    this.q = p.catch(() => {});
    return p;
  }
  async disconnect() { this.calls.push("disconnect"); this.linked = false; this.pod.down(); this.state("closed"); }
  /** The link drops ("lost"); the plugin's autoConnect waits for the pod. */
  drop() { this.linked = false; this.pod.down(); this.state("lost"); }
  /** Its GATT chain done on a new link (services, MTU, TX CCCD): "ready". */
  relink() { this.link++; this.linkUp(); this.state("ready"); }
  /** A drop and the plugin's own reconnect; info / status: a woken pod's new boot. */
  reconnect({ info, status } = {}) {
    this.drop();
    if (info) this.pod.wake(info, status);
    this.relink();
  }
}

let ok = true;
const check = (good, msg) => { console.log(`${good ? "PASS" : "FAIL"}  ${msg}`); ok &&= Boolean(good); };
const events = () => { const ev = []; return { ev, onEvent: (k, i) => ev.push(`${k}:${typeof i === "string" ? i : i?.reboot ? "reboot" : ""}`) }; };

/** CTRL got its n+1-th value (and its RESPONSE went out). */
async function commanded(p, n, what = "the STREAM command") {
  await until(() => p.pod.ctrls.length > n, 3000, what);
  await settle();
}

async function streaming(plugin, data = DATAS[0]) {
  const E = events();
  let got = 0;
  const s = new PodNativeSource(() => got++, { plugin, onEvent: E.onEvent, capture: true, startTimeoutMs: 500 });
  const started = s.start();
  await commanded(plugin, 0);
  plugin.pod.push(data);
  await started;
  return { s, ev: E.ev, got: () => got };
}

async function start() {
  const p = new FakePlugin();
  p.early = ["00"];                                      // a packet the plugin flushed before connect() resolved
  const { s, got } = await streaming(p);
  const log = s.capture.log;
  const early = log.find((r) => r[0] === "pkt");
  const info = log.find((r) => r[0] === "pkt" && r[2].startsWith("0c") && r[2].slice(8, 16) === le32(POD.REG_INFO));
  const want = [PKT.hello, PKT.read(1, POD.REG_INFO), PKT.read(2, POD.REG_STATUS), PKT.write(3, POD.REG_PUSH, "01"),
    PKT.write(4, POD.REG_CTRL, "02015802")];
  // arrivals on the plugin's clock pair: the phone's wall clock at that elapsedRealtime (within a few ms)
  check(p.calls[0] === 'connect:{"any":false}' && JSON.stringify(p.writes) === JSON.stringify(want) && p.links.every((l) => l === 1) &&
    got() > 0 && s.started && early?.[2] === "00" && Math.abs(early[1] - WALL0) < 1000 && Math.abs(info[1] - WALL0) < 1000 &&
    s.name === "BarPod-TEST" && s.mtu === 247 && s.linkInfo().mtu === 247,
    `start: connect, a held packet stamped once the clock pair came (${(early?.[1] - WALL0).toFixed(0)} ms), then the order on link 1 ` +
    `(${p.writes.join(" ")}), STREAM_LIVE(600), ${got()} samples, INFO stamped ${(info[1] - WALL0).toFixed(0)} ms after the wall clock origin`);
  await s.stop();
}

async function reconnectSameBoot() {
  const p = new FakePlugin();
  const { s, ev, got } = await streaming(p);
  const before = got();
  const n = p.pod.ctrls.length;
  const w = p.writes.length;
  p.reconnect();
  await commanded(p, n);
  const from = u32(DATAS[0], 4) + nOf(DATAS[0]);
  const want = `0102${le32(bootOf(INFO))}${le32(from)}`;
  p.pod.push(asStream(DATAS[1], 2));
  await sleep(10);
  const order = p.writes.slice(w, w + 5).map((h) => (PKT.ctrlOf(h) !== null ? "ctrl" : h.slice(0, 2)));
  check(ev.includes("link:lost") && ev.includes("link:back") && p.pod.ctrls.at(-1) === want && s.reconnects === 1 && got() > before &&
    s.stream.gaps.length === 0 && JSON.stringify(order) === JSON.stringify(["01", "06", "06", "04", "ctrl"]) && p.links.at(-1) === 2,
    `same-boot reconnect: lost, back, the order again for link 2 (${order.join(" ")}), STREAM_FROM the next index (${p.pod.ctrls.at(-1)}), ` +
    `samples ${before} -> ${got()}, gaps ${s.stream.gaps.length}`);
  await s.stop();
}

async function sleepAndWake() {
  const p = new FakePlugin({ info: SW.info1, status: SW.status1 });
  const { s, ev } = await streaming(p, SW.data1);
  p.pod.push(SW.asleep);                                 // going_to_sleep, then the pod's disconnect
  p.drop();
  await sleep(10);
  const banner = s.linkInfo();
  p.state("lost");                                       // the plugin's failed autoConnect tries say nothing new
  const n = p.pod.ctrls.length;
  p.reconnect({ info: SW.info2, status: SW.status2 });   // the pickup: a new boot
  await commanded(p, n);
  await sleep(10);
  const want = `0102${le32(bootOf(SW.info2))}${le32(0)}`;
  check(ev.includes("link:asleep") && !ev.includes("link:lost") && banner.asleep === "idle" && !banner.linked &&
    ev.includes("epoch:reboot") && p.pod.ctrls.at(-1) === want && ev.includes("link:back") && s.asleep === null,
    `sleep and wake: "asleep (idle)" once, a new epoch, STREAM_FROM(new boot, 0) (${p.pod.ctrls.at(-1)}), back`);
  await s.stop();
}

async function sleepDuringReconnect() {
  const p = new FakePlugin({ info: SW.info1, status: SW.status1 });
  const { s, ev } = await streaming(p, SW.data1);
  const n = p.pod.ctrls.length;
  p.pod.onPushOn = () => [SW.asleep];                    // the pod says going_to_sleep as the link comes back
  p.reconnect();
  await until(() => ev.includes("link:asleep"), 2000, "the asleep report");
  await sleep(20);
  const mid = ev.slice();
  p.drop();                                              // ...and disconnects
  await sleep(10);
  check(!mid.includes("link:back") && mid.includes("link:asleep") && p.pod.ctrls.length === n && !s.linkInfo().linked,
    `going_to_sleep during the reconnect's order: no false "back", no command (${mid.filter((e) => e.startsWith("link")).join(" ")})`);
  await s.stop();
}

async function requestDuringReconnect() {
  const p = new FakePlugin();
  const { s } = await streaming(p);
  const head = u32(STATUS, 8);
  for (const f of DATAS) { if (u32(f, 4) > head + 20) break; p.pod.push(f); }   // past the STATUS's head
  const ev = [];
  s.onEvent = (k) => ev.push(k);
  const n = p.pod.ctrls.length;
  p.reconnect();                                         // that STATUS read reads as "rebooted, same boot_id"
  await commanded(p, n);
  await sleep(30);
  const after = p.pod.ctrls.slice(n);
  check(ev.includes("need-command") && after.length === 1 && after[0].startsWith("01"),
    `a decoder request during the reconnect's order: one command at its end, not two (${after.join(" ")})`);
  await s.stop();
}

async function stopHandshake() {
  const p = new FakePlugin();
  p.pod.answerStop = true;
  const { s, got } = await streaming(p);
  await s.stop();
  const n = got();
  p.emit([hex(p.pod.pushOf(DATAS[2]))]);                 // anything after the end is ignored
  check(p.pod.ctrls.at(-1) === "03" && !p.pod.hold && p.writes.at(-1) === PKT.bye && s.stream.stats.stopped === 1 && !s.stream.faults.length &&
    p.calls.at(-1) === "disconnect" && p.ls.pod.length === 0 && p.ls.podState.length === 0 && got() === n,
    `End: STOP answered (stopped ${s.stream.stats.stopped}, the hold released), BYE, disconnect, listeners removed, nothing after it`);
}

async function requestDuringStop() {
  const p = new FakePlugin();
  const { s, ev } = await streaming(p);
  const head = u32(STATUS, 8);
  for (const f of DATAS) { if (u32(f, 4) > head + 20) break; p.pod.push(f); }   // the decoder past STATUS's head
  const n = p.pod.ctrls.length;
  // after the STOP's RESPONSE: a STATUS that reads as "rebooted, same boot_id" (need-command), then stream_stopped
  p.pod.onCtrl = (v) => (v[0] === POD.OP_STOP ? [STATUS, F.STOPPED] : []);
  await s.stop();
  await sleep(20);
  const after = p.pod.ctrls.slice(n);
  check(ev.some((e) => e.startsWith("need-command")) && JSON.stringify(after) === JSON.stringify(["03"]) && !p.pod.hold &&
    p.writes.at(-1) === PKT.bye,
    `a decoder request during End's STOP handshake: no stream command after the STOP (CTRL ${after.join(" ")}, hold ${p.pod.hold})`);
}

async function stopWhileDown() {
  const p = new FakePlugin();
  const { s } = await streaming(p);
  p.drop();
  const tries = p.attempts.length;
  const t0 = Date.now();
  await s.stop();
  const log = s.capture.log.map((r) => r[2]);
  check(Date.now() - t0 < 200 && p.attempts.length === tries && log.at(-1) === "finish" && p.calls.at(-1) === "disconnect",
    `End while the link is down: nothing (STOP, BYE) to a dead link, finish(), disconnect (${Date.now() - t0} ms)`);
}

async function stopDuringResume() {
  const p = new FakePlugin();
  p.pod.answerStop = true;
  const { s, ev } = await streaming(p);
  const n = p.pod.ctrls.length;
  const c = p.ctrlAttempts().length;
  p.writeDelay = 150;                                    // the link is slow: the reconnect's STREAM_FROM takes a while to go out
  p.reconnect();
  await until(() => p.ctrlAttempts().length > c, 3000, "the reconnect's command");   // "ready", its command in flight
  await s.stop();
  const after = p.pod.ctrls.slice(n);
  check(after.length === 2 && after[0].startsWith("0102") && after[1] === "03" && !p.pod.hold && s.stream.stats.stopped === 1 &&
    ev.includes("link:back") && !s.stream.faults.length,
    `End during the reconnect (ready, command in flight): the command, then STOP answered (${after.join(" ")}), back once`);
}

async function sleepDuringResume() {
  const p = new FakePlugin({ info: SW.info1, status: SW.status1 });
  const { s, ev } = await streaming(p, SW.data1);
  const c = p.ctrlAttempts().length;
  p.writeDelay = 100;
  p.reconnect();
  await until(() => p.ctrlAttempts().length > c, 3000, "the reconnect's command");
  p.pod.push(SW.asleep);                                 // going_to_sleep while the resume's command is out
  await sleep(150);
  check(!ev.includes("link:back") && s.linkLost && s.asleep !== null,
    `going_to_sleep while the reconnect's command is in flight: no "back" (${ev.filter((e) => e.startsWith("link")).join(" ")})`);
  p.writeDelay = 0;
  await s.stop();
}

async function resumeRetry() {
  const p = new FakePlugin();
  const { s, ev } = await streaming(p);
  p.failCtrl = 1;                                        // the first STREAM_FROM after the reconnect fails
  const n = p.pod.ctrls.length;
  p.reconnect();
  await sleep(200);
  const early = ev.includes("link:back");
  await sleep(1100);
  // the failed try used cmd_seq 2 (the pod never saw it): the retry is STREAM_FROM with cmd_seq 3,
  // and the order isn't run again (one HELLO on link 2)
  const hellos = p.writes.filter((h) => h === PKT.hello).length;
  check(!early && ev.includes("link:back") && p.pod.ctrls.length === n + 1 && p.pod.ctrls.at(-1).startsWith("0103") && !s.linkLost && hellos === 2,
    `a reconnect's command that fails on a live link: tried again 1 s later, then back (${p.pod.ctrls.slice(n).join(" ")}, HELLOs ${hellos})`);
  await s.stop();
}

async function stopDuringRetry() {
  const p = new FakePlugin();
  p.pod.answerStop = true;
  const { s } = await streaming(p);
  p.failCtrl = 1;                                        // the reconnect's command fails, its retry waits 1 s
  const n = p.ctrlAttempts().length;
  p.reconnect();
  await sleep(200);
  await s.stop();                                        // End during that wait: STOP now
  await sleep(1200);
  const after = p.ctrlAttempts().slice(n);
  check(after.at(-1) === "03" && after.filter((h) => h.startsWith("01")).length === 1 && !p.pod.hold,
    `End while a reconnect's failed command waits for its retry: STOP last, no stream command after it (${after.join(" ")})`);
}

async function retryUntilItWorks() {
  const p = new FakePlugin();
  const { s, ev } = await streaming(p);
  p.failCtrl = 3;                                        // 3 fails on a live link: no new "ready" will come
  p.reconnect();
  await sleep(7300);                                     // tries at 0, 1, 3 and 7 s
  const tries = p.ctrlAttempts().filter((h) => h.startsWith("01")).length;    // STREAM_FROM; the first STREAM_LIVE is 02
  check(ev.includes("link:back") && !s.linkLost && p.pod.ctrls.at(-1).startsWith("01") && tries === 4,
    `a reconnect's command failing 3 times on a live link: still tried (1, 2, 4 s apart), then back (${tries} tries)`);
  await s.stop();
}

async function newLinkDuringRetry() {
  const p = new FakePlugin();
  const { s } = await streaming(p);
  p.failCtrl = 1;                                        // link 2's command fails: its retry waits 1 s
  p.reconnect();
  await sleep(200);
  const n = p.ctrlAttempts().length;
  p.reconnect();                                         // link 3 meanwhile: its own resume sends the command
  await sleep(1300);
  const after = p.ctrlAttempts().slice(n);
  check(after.filter((h) => h.startsWith("01")).length === 1 && !s.linkLost,
    `a newer link during a retry's wait: one command, from the newer link's resume (${after.join(" ")})`);
  await s.stop();
}

async function stopDuringOrder() {
  const p = new FakePlugin();
  p.pod.answerStop = true;
  const { s } = await streaming(p);
  p.drop();
  p.state("linking");                                    // the plugin's autoConnect has the link: its GATT chain runs
  const n = p.ctrlAttempts().length;
  const w = p.writes.length;
  const stopped = s.stop();
  await sleep(100);
  p.relink();                                            // the chain is done: End runs the order, then its STOP
  await stopped;
  const after = p.ctrlAttempts().slice(n);
  const order = p.writes.slice(w, w + 5).map((h) => (PKT.ctrlOf(h) !== null ? `ctrl ${PKT.ctrlOf(h)}` : h.slice(0, 2)));
  check(after.length === 1 && after[0] === "03" && p.pod.ctrls.at(-1) === "03" && !p.pod.hold && s.stream.stats.stopped === 1 &&
    JSON.stringify(order) === JSON.stringify(["01", "06", "06", "04", "ctrl 03"]),
    `End during the plugin's GATT chain: waits for "ready", then the order and STOP over the new link (${order.join(" ")}), no stream command`);
}

async function stopWhenOrderFails() {
  const p = new FakePlugin();
  p.pod.answerStop = true;
  const { s } = await streaming(p);
  p.pod.mtu = 23;                                        // the new link is too small: its INFO read stays BUSY (docs/ble.md 5)
  const n = p.pod.ctrls.length;
  p.reconnect();
  await until(() => s.crp?.busy > 0, 2000, "the BUSY INFO read");
  const t0 = Date.now();
  await s.stop();                                        // End while the order retries: it gives up after 3 s
  const ms = Date.now() - t0;
  const calls = s.capture.log.filter((r) => r[0] === "call").map((r) => r[2]);
  check(JSON.stringify(p.pod.ctrls.slice(n)) === JSON.stringify(["03"]) && !p.pod.hold && s.linkInfo().mtu === 23 && ms < 4500 &&
    !calls.some((c) => c.startsWith("stop_timed_out")) && calls.at(-1) === "finish" && p.writes.at(-1) === PKT.bye,
    `End while a reconnect's order fails after HELLO (MTU 23): the STOP still releases the hold, unanswered (CTRL ${p.pod.ctrls.slice(n).join(" ")}), ` +
    `MTU ${s.linkInfo().mtu} from the register, ${ms} ms, then BYE`);
}

async function stopDuringReread() {
  const p = new FakePlugin();
  const { s } = await streaming(p);
  // the reconnect's STREAM_FROM gets a boot_mismatch STATUS (the INFO was stale): the resume
  // re-reads INFO (slowly) before its next command
  const mismatch = `10${(parseInt(STATUS.slice(2, 4), 16) | POD.S_BOOT_MISMATCH).toString(16).padStart(2, "0")}${STATUS.slice(4)}`;
  p.pod.onCtrl = (v) => {
    if (v[0] !== POD.OP_STREAM_FROM) return [];
    p.pod.onCtrl = null;
    p.pod.readDelay = 150;
    return [mismatch];
  };
  const n = p.pod.ctrls.length;
  const r = p.writes.length;
  p.reconnect();
  await until(() => s.rereadP !== null, 3000, "the re-read");
  await s.stop();                                        // End during the re-read
  await sleep(200);
  const after = p.pod.ctrls.slice(n);
  const infos = p.writes.slice(r).filter((h) => h.startsWith("06") && h.slice(4) === le32(POD.REG_INFO)).length;
  check(infos === 2 && after.length === 2 && after[0].startsWith("01") && after[1] === "03" && !p.pod.hold && s.stream.faults.includes("boot mismatch"),
    `End while a reconnect re-reads INFO: STOP only, no stream command after it (${after.join(" ")}, INFO reads ${infos})`);
}

async function startFails() {
  const p1 = new FakePlugin();
  p1.connectError = "No pod found. Bump it to wake it and keep it close.";
  const s1 = new PodNativeSource(() => {}, { plugin: p1 });
  const e1 = await s1.start().then(() => null, (e) => e);
  const p2 = new FakePlugin();
  const s2 = new PodNativeSource(() => {}, { plugin: p2, startTimeoutMs: 100 });
  const e2 = await s2.start().then(() => null, (e) => e);
  check(e1?.message.startsWith("No pod found") && p1.ls.pod.length === 0 && e2 && p2.pod.ctrls[0]?.startsWith("02") && p2.pod.ctrls.at(-1) === "03" &&
    !p2.pod.hold && p2.writes.at(-1) === PKT.bye && p2.calls.at(-1) === "disconnect",
    `a start that fails: the plugin's reason ("${e1?.message}"); no data after the command: STOP, BYE, disconnect ("${e2?.message}")`);
}

for (const t of [start, reconnectSameBoot, sleepAndWake, sleepDuringReconnect, requestDuringReconnect, stopHandshake, requestDuringStop, stopWhileDown,
  stopDuringResume, sleepDuringResume, resumeRetry, stopDuringRetry, retryUntilItWorks, newLinkDuringRetry, stopDuringOrder,
  stopWhenOrderFails, stopDuringReread, startFails]) {
  try { await t(); } catch (e) { check(false, `${t.name}: ${e.stack}`); }
}
console.log(ok ? "PASS" : "FAIL");
process.exit(ok ? 0 : 1);
