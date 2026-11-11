// CRP on the wire, JS against the Python reference (D45, docs/ble.md 2): no radio, no browser.
//
//   node webapp/test/crp_wire.mjs        (PYTHON = the venv's python, as an absolute path)
//
// 1. The vendored CRPClient (webapp/crp) sends exactly the bytes of `analysis/crp.py --vectors` for
//    sendHello({}), read(INFO), write(CTRL, ...), sendNop() and close(), through a capturing fake
//    transport.
// 2. Python-built packets fed to client._handlePacket give the expected events and answers: a
//    RESPONSE resolves its txn, a push is a 'write' with txnId 0, BUSY rejects with code 8 (J1),
//    the pod's HELLO, BYE and END 0xFF end what they should.
// 3. J2: a send whose promise rejects fails its txn at once, and a late rejection never touches a
//    txn id that was handed out again.
// 4. podFrameOf (webapp/sources.js) equals podlink.crp_frame on every vector plus edge packets.
// 5. PodCrpLink runs the connection order of docs/ble.md 7 byte for byte, and at the same
//    simulated ms, as make_fake_pod.py's phone did in a few scenarios (clean, BUSY before the MTU
//    exchange, MTU 23 forever...): its pod answers from data/fake_pod_packets.csv
//    (python analysis/make_fake_pod.py first).
// 6. PodCrpLink on its own, against a scripted pod: the HELLO checks, statuses, BYE.

import { execFileSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { CRPClient, ErrorCode } from "../crp/crp-client.js";
import { EventEmitter } from "../crp/event-emitter.js";
import { POD, PodCrpLink, PodStream, podFrameOf } from "../sources.js";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const ANALYSIS = path.join(ROOT, "analysis");
const PYTHON = process.env.PYTHON || (process.platform === "win32" ? "python" : "python3");

let ok = true;
const check = (good, msg) => { console.log(`${good ? "PASS" : "FAIL"}  ${msg}`); ok &&= Boolean(good); };
const hex = (b) => Array.from(b instanceof Uint8Array ? b : new Uint8Array(b.buffer, b.byteOffset, b.byteLength),
  (x) => x.toString(16).padStart(2, "0")).join("");
const bytes = (h) => Uint8Array.from(h.match(/../g) ?? [], (x) => parseInt(x, 16));
const tick = () => new Promise((r) => setImmediate(r));
const settled = (p) => p.then((v) => ({ ok: true, v }), (e) => ({ ok: false, e }));

// ---- Python's side: the canonical packets, and crp_frame on them plus some edge packets ----
const PY_FRAMES = `
import json, crp, podlink as pl
status = bytes.fromhex(next(v["hex"] for v in crp.vectors() if v["name"] == "push_status"))[6:]
info = bytes.fromhex(next(v["hex"] for v in crp.vectors() if v["name"] == "response_info"))[8:]
edges = [
    ("busy_status", crp.response(4, crp.BUSY, pl.REG_STATUS)),
    ("denied_info_with_value", crp.response(10, crp.ACCESS_DENIED, pl.REG_INFO, info)),
    ("response_cal", crp.response(5, crp.OK, pl.REG_CAL, bytes([0x30]) + bytes(63))),
    ("response_info_empty", crp.response(6, crp.OK, pl.REG_INFO)),
    ("response_status_ch0", crp.response(7, crp.OK, pl.REG_STATUS, status, ch=0)),
    ("response_data_read", crp.response(8, crp.OK, pl.REG_DATA, b"\\x01\\x02")),
    ("response_ctrl_value", crp.response(9, crp.OK, pl.REG_CTRL, b"\\x10\\x00")),
    ("response_mtu", crp.response(11, crp.OK, pl.REG_LINK_MTU, b"\\xf7\\x00")),
    ("response_other_group", crp.response(12, crp.OK, 0x00020001, status)),
    ("client_write_data", crp.write(5, pl.REG_DATA, status)),
    ("push_cal", crp.push(pl.REG_CAL, bytes([0x30]) + bytes(63))),
    ("push_info", crp.push(pl.REG_INFO, info)),
    ("push_ctrl", crp.push(pl.REG_CTRL, b"\\x03")),
    ("push_status_empty", crp.push(pl.REG_STATUS, b"")),
    ("push_status_odd", crp.push(pl.REG_STATUS, status + b"\\x00")),
    ("short_response", crp.response(1, crp.OK, pl.REG_INFO)[:7]),
    ("short_write", crp.push(pl.REG_DATA, b"")[:5]),
    ("read_info", crp.read(1, pl.REG_INFO)),
    ("hello_bpod", crp.hello(crp.VERSION, 1, 244, crp.FLAG_PUSH, pl.DEVICE_ID)),
    ("unknown_type", bytes([0x7F]) + bytes(12)),
    ("empty", b""),
]
pkts = [(v["name"], bytes.fromhex(v["hex"])) for v in crp.vectors()] + edges
out = []
for name, b in pkts:
    f = pl.crp_frame(b)
    out.append({"name": name, "hex": b.hex(), "frame": None if f is None else f.hex()})
print(json.dumps(out))
`;
let vectors, framed;
try {
  vectors = JSON.parse(execFileSync(PYTHON, ["crp.py", "--vectors"], { cwd: ANALYSIS, encoding: "utf8" }));
  framed = JSON.parse(execFileSync(PYTHON, ["-c", PY_FRAMES], { cwd: ANALYSIS, encoding: "utf8" }));
} catch (e) {
  console.error(`Python failed (PYTHON=${PYTHON}; set it to the venv's python, an absolute path): ${e.message}`);
  process.exit(2);
}
const V = Object.fromEntries(vectors.map((v) => [v.name, v.hex]));
const P = (name) => bytes(V[name]);

/** CRPClient's transport, in memory: what the client sends lands in sent (hex). */
class FakeTransport extends EventEmitter {
  constructor(sendFn = null) { super(); this.sent = []; this.sendFn = sendFn; }
  send(u8) { this.sent.push(hex(u8)); return this.sendFn ? this.sendFn(u8) : undefined; }
}

// ---- 1 and 2: the client's bytes, and its answers to Python's packets ----
async function clientWire() {
  const t = new FakeTransport();
  const c = new CRPClient(t, { txnTimeoutMs: 60000, txnRetries: 0, heartbeatMs: 0 });
  const ev = [];
  for (const k of ["hello", "bye", "response", "error", "write", "push-change", "end", "unknown-packet"]) {
    c.on(k, (x) => ev.push([k, x]));
  }
  c.sendHello({});
  check(t.sent[0] === V.hello, `sendHello({}) sends crp.py's hello: ${t.sent[0]}`);
  const pInfo = settled(c.read(POD.REG_INFO));
  check(t.sent[1] === V.read_info, `the first read(INFO) is txn 1, crp.py's read_info: ${t.sent[1]}`);
  const pCtrl = settled(c.write(POD.REG_CTRL, Uint8Array.of(0x02, 0x01, 0x58, 0x02)));
  check(t.sent[2] === V.write_ctrl, `write(CTRL, STREAM_LIVE 1 600) is txn 2, crp.py's write_ctrl: ${t.sent[2]}`);
  c.sendNop();
  check(t.sent[3] === V.nop, `sendNop() sends ${t.sent[3]}`);

  c._handlePacket(P("pod_hello_ble"));
  const h = ev.find(([k]) => k === "hello")?.[1];
  check(h && h.version === 0x0B && h.maxChannels === 1 && h.maxChunkSize === 244 && h.flags === 1 &&
    h.deviceId === POD.DEVICE_ID, `the pod's Bluetooth HELLO parses: ${JSON.stringify(h)}`);
  c._handlePacket(P("pod_hello_usb"));
  check(c.peerHello.maxChunkSize === 1024 && c.peerHello.deviceId === POD.DEVICE_ID, "the pod's USB HELLO says max_chunk 1024");

  c._handlePacket(P("response_info"));
  const ri = await pInfo;
  check(ri.ok && ri.v.txnId === 1 && ri.v.reg === POD.REG_INFO && hex(ri.v.payload) === V.response_info.slice(16) &&
    ev.some(([k, x]) => k === "response" && x.txnId === 1), "an OK RESPONSE resolves read(INFO) with its 32 B value and emits 'response'");
  c._handlePacket(P("response_ctrl_ok"));
  const rc = await pCtrl;
  check(rc.ok && rc.v.txnId === 2 && rc.v.reg === POD.REG_CTRL && rc.v.payload.length === 0, "the CTRL write's OK RESPONSE resolves it (no value)");

  const pBusy = settled(c.read(POD.REG_INFO));
  check(t.sent[4] === "060301000100", `the next read is txn 3: ${t.sent[4]}`);
  c._handlePacket(P("response_busy"));
  const rb = await pBusy;
  check(!rb.ok && rb.e.code === ErrorCode.BUSY && rb.e.reg === POD.REG_INFO && /BUSY/.test(rb.e.message),
    `J1: BUSY rejects txn 3 with code ${rb.e?.code}, reg 0x${rb.e?.reg?.toString(16)} (${rb.e?.message})`);
  check(ev.some(([k, x]) => k === "error" && x.txnId === 3 && x.code === 8), "BUSY also emits 'error' with code 8");
  check(c._pending.size === 0 && c._txnAlloc._inUse.size === 0, "nothing pending, every txn id free");

  const n0 = ev.length;
  c._handlePacket(P("push_data"));
  c._handlePacket(P("push_status"));
  const w = ev.slice(n0).filter(([k]) => k === "write").map(([, x]) => x);
  check(w.length === 2 && w[0].txnId === 0 && w[0].reg === POD.REG_DATA && hex(w[0].payload) === V.push_data.slice(12) &&
    w[1].txnId === 0 && w[1].reg === POD.REG_STATUS && hex(w[1].payload) === V.push_status.slice(12),
    "the pod's pushes are 'write' events with txnId 0, DATA and STATUS, the frame as payload");
  check(!ev.slice(n0).some(([k]) => k === "response" || k === "error"), "a push answers no transaction");

  const n1 = ev.length;
  c._handlePacket(P("nop"));
  check(ev.length === n1, "a NOP from the pod is silent");
  c._handlePacket(P("log"));
  const u = ev.slice(n1).find(([k]) => k === "unknown-packet")?.[1];
  check(u && u.type === 0x18, "a LOG comes out as 'unknown-packet' (no handler upstream)");

  // the pod's BYE and END 0xFF end the session: pending txns fail, ids start over at 1
  const pBye = settled(c.read(POD.REG_STATUS));
  c._handlePacket(P("pod_bye"));
  const rbye = await pBye;
  check(!rbye.ok && ev.some(([k, x]) => k === "bye" && x.deviceId === POD.DEVICE_ID), `the pod's BYE fails a pending read (${rbye.e?.message})`);
  const pEnd = settled(c.read(POD.REG_INFO));
  check(t.sent.at(-1) === V.read_info, `after a BYE the ids start over: ${t.sent.at(-1)}`);
  c._handlePacket(P("end_session"));
  const rend = await pEnd;
  check(!rend.ok && ev.some(([k, x]) => k === "end" && x.channel === 0xFF), `END 0xFF fails a pending read (${rend.e?.message})`);

  // a fresh client's close() is crp.py's bye (base 0, device_id 0)
  const t2 = new FakeTransport();
  const c2 = new CRPClient(t2, { txnRetries: 0, heartbeatMs: 0 });
  c2.sendHello({});
  c2.close();
  check(t2.sent[1] === V.bye, `close() sends crp.py's bye: ${t2.sent[1]}`);
  c2.close();
  check(t2.sent.length === 2, "a second close() sends nothing");
}

// ---- 3: J2 ----
async function sendRejects() {
  // a write the link refuses: the read fails at once, not after the 60 s timeout
  const refused = Object.assign(new Error("GATT operation failed"), { name: "NetworkError", code: 19 });
  const t = new FakeTransport((u8) => (u8[0] === 0x06 ? Promise.reject(refused) : Promise.resolve()));
  const c = new CRPClient(t, { txnTimeoutMs: 60000, txnRetries: 0, heartbeatMs: 0 });
  const t0 = Date.now();
  const r = await settled(c.read(POD.REG_INFO));
  check(!r.ok && r.e === refused && Date.now() - t0 < 1000, `J2: a rejected send fails its txn at once with that error (${Date.now() - t0} ms)`);
  check(c._pending.size === 0 && c._txnAlloc._inUse.size === 0, "J2: the txn is gone and its id free");
  const rc = await settled(new PodCrpLink(new FakeTransport((u8) => Promise.reject(refused)), {}).ctrl(Uint8Array.of(3)));
  check(!rc.ok && rc.e === refused, "PodCrpLink.ctrl: a refused write rejects (a DOMException's numeric code is no CRP status)");

  // a resolved send leaves the txn to its RESPONSE
  const t2 = new FakeTransport(() => Promise.resolve());
  const c2 = new CRPClient(t2, { txnTimeoutMs: 60000, txnRetries: 0, heartbeatMs: 0 });
  const p2 = settled(c2.read(POD.REG_INFO));
  await tick();
  check(c2._pending.size === 1, "J2: a send that succeeds leaves the txn waiting for its answer");
  c2._handlePacket(P("response_info"));
  check((await p2).ok, "...and the RESPONSE resolves it");

  // a late rejection after the id was handed out again (BYE resets the ids) leaves the new txn alone
  let rejectFirst = null;
  const t3 = new FakeTransport(() => (rejectFirst ? Promise.resolve() : new Promise((_, rej) => { rejectFirst = rej; })));
  const c3 = new CRPClient(t3, { txnTimeoutMs: 60000, txnRetries: 0, heartbeatMs: 0 });
  const first = settled(c3.read(POD.REG_INFO));
  c3._handlePacket(P("pod_bye"));                     // fails txn 1, ids from 1 again
  await first;
  const second = settled(c3.read(POD.REG_STATUS));    // txn 1 again
  check(t3.sent[1] === "060102000100", `the next read reuses txn 1: ${t3.sent[1]}`);
  rejectFirst(new Error("the first write failed late"));
  await tick();
  check(c3._pending.size === 1, "J2: the old send's late rejection doesn't fail the new txn 1");
  c3._handlePacket(bytes("0c0100ff02000100" + V.push_status.slice(12)));
  const s2 = await second;
  check(s2.ok && s2.v.reg === POD.REG_STATUS, "...which its own RESPONSE resolves");
}

// ---- 4: the adapter ----
function adapter() {
  let bad = 0;
  for (const v of framed) {
    const u8 = bytes(v.hex);
    const f = podFrameOf(u8);
    const got = f === null ? null : hex(f);
    // the same from a DataView and an ArrayBuffer, as Web Bluetooth and the plugin hand them in
    const ab = u8.slice().buffer;
    const fDv = podFrameOf(new DataView(ab));
    const fAb = podFrameOf(ab);
    const same = [fDv, fAb].every((x) => (x === null ? null : hex(x)) === got);
    if (got !== v.frame || !same) {
      bad += 1;
      console.log(`      - ${v.name}: podFrameOf ${got}, crp_frame ${v.frame}${same ? "" : " (input types differ)"}`);
    }
  }
  check(bad === 0, `podFrameOf = podlink.crp_frame on ${framed.length} packets (${vectors.length} vectors, ${framed.length - vectors.length} edge cases)`);
  check(framed.filter((v) => v.frame !== null).length >= 6 && framed.some((v) => v.frame === ""),
    "the edge cases include frames (and an empty one, which the decoder counts as bad)");
  // a frame is a copy: changing the packet afterwards doesn't change it
  const u8 = P("push_status");
  const f = podFrameOf(u8);
  u8[6] = 0xEE;
  check(f[0] === 0x10, "podFrameOf returns a copy of the value");
}

// ---- 5: PodCrpLink against make_fake_pod.py's phone ----
const PACKETS = path.join(ANALYSIS, "data", "fake_pod_packets.csv");
function runsOf(names) {
  if (!existsSync(PACKETS)) {
    console.error(`Missing ${PACKETS}. Run: cd analysis && python make_fake_pod.py`);
    process.exit(2);
  }
  const out = new Map(names.map((n) => [n, []]));
  for (const l of readFileSync(PACKETS, "utf8").split(/\r?\n/)) {
    const v = l.split(",");
    if (out.has(v[0])) out.get(v[0]).push({ step: +v[1], kind: v[2], arrival: v[3] === "" ? null : parseFloat(v[3]), payload: v.slice(4).join(",") });
  }
  return out;
}

/**
 * The pod's side of one recorded run: every send must be the next tx row (same bytes, same
 * simulated ms); then the rows up to the next tx are delivered as the pod's answers, each through
 * the tap (PodStream.packet with its arrival) before 'packet', with the clock at the ms the
 * simulator handled it (the arrival rounded up). cmd rows are what command() must return.
 */
class ReplayPod extends EventEmitter {
  constructor(rows, stream) {
    super();
    this.rows = rows;
    this.stream = stream;
    this.i = rows.findIndex((r) => r.kind === "tx");
    this.clock = rows[this.i].arrival;
    this.sends = 0;
    this.problems = [];
    this.cmds = [];
    this.done = false;                  // the run's link went down: nothing more to answer
  }

  now() { return this.clock; }

  send(u8) {
    const r = this.rows[this.i];
    const got = hex(u8);
    if (!r || r.kind !== "tx") this.problems.push(`send ${this.sends} (${got}): Python sent nothing more`);
    else if (r.payload !== got) this.problems.push(`send ${this.sends}: ${got}, Python ${r.payload} (step ${r.step})`);
    else if (r.arrival !== this.clock) this.problems.push(`send ${this.sends} at ${this.clock} ms, Python at ${r.arrival} (step ${r.step})`);
    this.sends += 1;
    this.i += 1;
    setImmediate(() => this.answer());
  }

  answer() {
    for (; this.i < this.rows.length && !this.done; this.i++) {
      const r = this.rows[this.i];
      if (r.kind === "tx") return;
      if (r.kind === "link" && r.payload !== "connect") { this.done = true; return; }
      if (r.kind === "cmd") this.cmds.push(r.payload);
      if (r.kind !== "pkt") continue;
      const u8 = bytes(r.payload);
      this.clock = Math.ceil(r.arrival);
      this.stream.packet(u8, r.arrival);          // the tap first, as our transports do
      this.emit("packet", u8);
    }
  }
}

/** docs/ble.md 7, steps 2 to 5, the way the pod sources run it. */
async function connectionOrder(link, stream, pod, log) {
  await link.hello();
  await link.readFrame(POD.REG_INFO);
  await link.readFrame(POD.REG_STATUS);
  await link.push(true);
  const b = stream.command();
  const want = pod.cmds.shift();
  log.cmd = b === null ? "none" : hex(b);
  log.cmdWant = want;
  if (b !== null) log.ctrl = await link.ctrl(b);
}

async function connectionOrders() {
  const want = {
    clean: { busy: 0 },
    "scale-from-info": { busy: 0 },
    "crp-busy-before-mtu": { busy: 5 },
    "pod-mtu-exchange": { busy: 1 },
    "mtu-23-forever": { busy: 13, mtu: 23 },
  };
  const runs = runsOf(Object.keys(want));
  for (const [name, w] of Object.entries(want)) {
    const rows = runs.get(name);
    if (!rows.length) { check(false, `${name}: not in ${PACKETS}`); continue; }
    const stream = new PodStream(() => {});
    const pod = new ReplayPod(rows, stream);
    const link = new PodCrpLink(pod, { heartbeatMs: 0, now: () => pod.now(), sleep: async (ms) => { pod.clock += ms; await tick(); } });
    const log = {};
    const r = await settled(connectionOrder(link, stream, pod, log));
    // the tx rows the phone's connection order sent: up to the CTRL write after its command, or
    // every one before the link went down if the session gave up
    const firstCmd = rows.findIndex((x) => x.kind === "cmd");
    const end = rows.findIndex((x, k) => k > 0 && x.kind === "link" && x.payload !== "connect");
    let nTx;
    if (firstCmd >= 0 && (end < 0 || firstCmd < end)) nTx = rows.slice(0, firstCmd).filter((x) => x.kind === "tx").length + 1;
    else nTx = rows.slice(0, end < 0 ? rows.length : end).filter((x) => x.kind === "tx").length;
    const p = pod.problems;
    if (pod.sends !== nTx) p.push(`${pod.sends} sends, Python's order sent ${nTx}`);
    if (link.busy !== w.busy) p.push(`${link.busy} BUSY answers, want ${w.busy}`);
    if (w.mtu !== undefined) {
      if (r.ok || r.e.code !== ErrorCode.BUSY || r.e.mtu !== w.mtu || link.mtuRead !== w.mtu) {
        p.push(`want a BUSY failure with MTU ${w.mtu}: ${r.ok ? "it went through" : `${r.e.message} (code ${r.e.code}, mtu ${r.e.mtu})`}`);
      }
      if (stream.stats.samples !== 0) p.push(`${stream.stats.samples} samples`);
    } else {
      if (!r.ok) p.push(`the order failed: ${r.e.message}`);
      if (log.cmd !== log.cmdWant) p.push(`command() ${log.cmd}, Python ${log.cmdWant}`);
      if (log.ctrl !== 0) p.push(`ctrl() answered ${log.ctrl}`);
      if (stream.faults.length) p.push(`faults ${stream.faults}`);
      if (stream.stats.samples === 0) p.push("no samples after the command");
    }
    check(p.length === 0, `${name}: PodCrpLink's connection order = the simulator's phone (${pod.sends} packets, ` +
      `${link.busy} BUSY${w.mtu ? `, MTU ${link.mtuRead}` : ""})${p.length ? `\n      - ${p.slice(0, 4).join("\n      - ")}` : ""}`);
  }
}

// ---- 6: PodCrpLink on its own: HELLO checks, statuses, BYE ----
/** A scripted pod: answer(u8) -> packets (hex) it sends back, on the next turn. */
class ScriptPod extends EventEmitter {
  constructor(answer) { super(); this.sent = []; this.answer = answer; }
  send(u8) {
    this.sent.push(hex(u8));
    const out = this.answer(u8) ?? [];
    setImmediate(() => { for (const h of out) this.emit("packet", bytes(h)); });
  }
}

const resp = (u8, status, valueHex = "") => `0c${hex(u8.subarray(1, 2))}${status.toString(16).padStart(2, "0")}ff${hex(u8.subarray(2, 6))}${valueHex}`;

async function linkUnits() {
  // HELLO: the pod's is accepted, another device's isn't, a BYE refuses, silence times out
  let l = new PodCrpLink(new ScriptPod((u8) => (u8[0] === 0x01 ? [V.pod_hello_ble] : [])));
  const h = await settled(l.hello());
  check(h.ok && h.v.deviceId === POD.DEVICE_ID && l.peer === h.v, "hello(): the pod's HELLO (device_id BPOD) is the peer");
  l = new PodCrpLink(new ScriptPod((u8) => (u8[0] === 0x01 ? ["010b0400020000000000"] : [])));
  let r = await settled(l.hello());
  check(!r.ok && /Not a pod/.test(r.e.message) && l.peer === null, `hello(): another CRP device is refused (${r.e?.message})`);
  l = new PodCrpLink(new ScriptPod((u8) => (u8[0] === 0x01 ? [V.pod_bye] : [])));
  r = await settled(l.hello());
  check(!r.ok && /BYE/.test(r.e.message), `hello(): a BYE (our version too old) fails it (${r.e?.message})`);
  l = new PodCrpLink(new ScriptPod(() => []), { txnTimeoutMs: 50 });
  r = await settled(l.hello());
  check(!r.ok && /didn't answer/.test(r.e.message), `hello(): silence fails it after txnTimeoutMs (${r.e?.message})`);
  const dropT = new ScriptPod(() => []);
  l = new PodCrpLink(dropT);
  const pd = settled(l.hello());
  dropT.emit("disconnected");
  r = await pd;
  check(!r.ok && /dropped/.test(r.e.message), `hello(): a dropped link fails it at once (${r.e?.message})`);

  // reads, PUSH, CTRL statuses, MTU, BYE
  let busyLeft = 2;
  const pod = new ScriptPod((u8) => {
    if (u8[0] === 0x01) return [V.pod_hello_ble];
    const reg = new DataView(u8.buffer, u8.byteOffset).getUint32(2, true);
    if (u8[0] === 0x06 && reg === POD.REG_INFO) return [busyLeft-- > 0 ? resp(u8, 8) : resp(u8, 0, V.response_info.slice(16))];
    if (u8[0] === 0x06 && reg === POD.REG_LINK_MTU) return [resp(u8, 0, "f700")];
    if (u8[0] === 0x06 && reg === POD.REG_DATA) return [resp(u8, 2)];
    if (u8[0] === 0x04 && reg === POD.REG_PUSH) return [resp(u8, u8[6] <= 1 ? 0 : 3)];
    if (u8[0] === 0x04 && reg === POD.REG_CTRL) return [resp(u8, u8[6] === 0x03 ? 0 : u8[6] === 0x10 ? 8 : 3)];
    return [];
  });
  l = new PodCrpLink(pod, { busyRetryMs: 5 });
  await l.hello();
  const f = await l.readFrame(POD.REG_INFO);
  check(hex(f) === V.response_info.slice(16) && l.busy === 2, `readFrame(): two BUSY retried, then the INFO frame (busy ${l.busy})`);
  r = await settled(l.readFrame(POD.REG_DATA));
  check(!r.ok && r.e.code === 2 && l.busy === 2, `readFrame(): ACCESS_DENIED isn't retried (code ${r.e?.code})`);
  check(await l.mtu() === 247, "mtu(): the MTU register as a u16");
  await l.push(true);
  check(pod.sent.at(-1).startsWith("04") && pod.sent.at(-1).endsWith("0600010001"), `push(true) writes PUSH = 1: ${pod.sent.at(-1)}`);
  r = await settled(l.push(false));
  check(r.ok && pod.sent.at(-1).endsWith("0600010000"), "push(false) writes PUSH = 0");
  check(await l.ctrl(Uint8Array.of(0x03)) === 0, "ctrl(STOP) resolves 0 (OK)");
  check(await l.ctrl(Uint8Array.of(0x10, 0x00)) === 8, "ctrl() during the sleep prologue resolves 8 (BUSY), no throw");
  check(await l.ctrl(Uint8Array.of(0x7E)) === 3, "ctrl(unknown op) resolves 3 (INVALID_VALUE)");
  const nCtrl = pod.sent.filter((s) => s.slice(4, 12) === "05000100").length;
  check(nCtrl === 3, `no CTRL write is ever retried (${nCtrl} sent for 3 calls)`);
  l.close();
  check(pod.sent.at(-1) === V.bye, `close() sends BYE: ${pod.sent.at(-1)}`);
  check(l.client._txnRetries === 0 && l.client._heartbeatMs === 0 && l.client._txnTimeoutMs === 5000,
    "the client runs with txnRetries 0, heartbeatMs 0 (Bluetooth), txnTimeoutMs 5000");
}

for (const t of [clientWire, sendRejects, adapter, connectionOrders, linkUnits]) {
  try { await t(); } catch (e) { check(false, `${t.name}: ${e.stack}`); }
}
console.log(ok ? "PASS" : "FAIL");
process.exit(ok ? 0 : 1);
