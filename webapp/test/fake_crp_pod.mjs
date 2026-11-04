// A fake pod that speaks CRP (D45, docs/ble.md 2), shared by the pod sources' tests (pod_web.mjs on
// a fake Web Bluetooth device, pod_native.mjs on a fake PodLink plugin): no radio, no browser, no
// phone. It answers the way the firmware does where the sources care:
// - HELLO opens a session (PUSH off) and gets the pod's HELLO; before one, everything else is
//   ignored. A version below 0x0B gets BYE. BYE, END 0xFF and the link going down end the session.
// - READ of INFO, STATUS, CAL, PROTO, PUSH and the MTU register, built when it arrives; BUSY when
//   8 + len > MTU - 3; ACCESS_DENIED for DATA and CTRL, UNKNOWN_REG for the rest.
// - WRITE of PUSH (0 or 1) and CTRL (the ops of section 4: OK, INVALID_VALUE for a bad op or length,
//   BUSY while busy is set), each answered by a RESPONSE.
// - Pushes (a WRITE with txn 0 to DATA or STATUS, by the frame's type) only while the session has
//   PUSH on (docs/ble.md 5).
// - The session hold (docs/ble.md 6, `hold`): a taken STREAM command sets it (a STREAM_FROM with
//   another boot's id changes nothing), STOP clears it, and so does a reboot (wake()). BYE and a
//   drop leave it on, so a test can tell an End that really released it.
// One packet's answers go out together on the next turn, the RESPONSE first and then the pushes it
// caused (as the pod sends them: an answering STATUS after its RESPONSE); push() sends at once. A
// held INFO read (holdInfo) is answered only at release(), without holding up anything else.
// Everything it receives is kept (rx, ctrls) for the tests to check.
//
// The frames come from the simulator's fixtures, analysis/data/fake_pod_frames.csv (python
// analysis/make_fake_pod.py first).

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { POD } from "../sources.js";

const HERE = dirname(fileURLToPath(import.meta.url));

export const hex = (u8) => Buffer.from(u8.buffer ?? u8, u8.byteOffset ?? 0, u8.byteLength).toString("hex");
export const bytes = (h) => Uint8Array.from(Buffer.from(h, "hex"));
export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
/** One turn of the event loop: every answer the fake scheduled has gone out. */
export const settle = () => new Promise((r) => setImmediate(r));
export const until = async (cond, ms, what) => {
  const t0 = Date.now();
  while (!cond()) { if (Date.now() - t0 > ms) throw new Error(`timed out waiting for ${what}`); await sleep(5); }
};
export function gate() { let open; const p = new Promise((r) => { open = r; }); return { p, open }; }

// frame hex helpers: DATA is type n pkt_seq(2) first_idx(4) t0(4) flags cmd_seq, little-endian
export const u32 = (h, at) => parseInt(h.slice(at * 2, at * 2 + 8).match(/../g).reverse().join(""), 16);
export const le32 = (v) => [0, 8, 16, 24].map((b) => ((v >>> b) & 255).toString(16).padStart(2, "0")).join("");
const le16 = (v) => [0, 8].map((b) => ((v >>> b) & 255).toString(16).padStart(2, "0")).join("");
const h8 = (v) => (v & 255).toString(16).padStart(2, "0");
export const firstIdx = (h) => u32(h, 4);
export const nOf = (h) => parseInt(h.slice(2, 4), 16);
export const asStream = (h, seq) => `${h.slice(0, 24)}01${h8(seq)}${h.slice(28)}`;   // RESYNC, cmd_seq
export const bootOf = (infoHex) => u32(infoHex, 4);
const flagsOf = (h) => parseInt(h.slice(2, 4), 16);

/** The frames (hex) the tests push and the reads answer: the simulator's "clean" scenario (INFO,
 * STATUS, the cmd_seq 1 DATA frames, its stream_stopped STATUS) and "sleep-wake-rebase" (boot 1's
 * INFO, STATUS, DATA and going_to_sleep, then boot 2's INFO, STATUS and cmd_seq 2 DATA from 0). */
export function fixtures() {
  const all = readFileSync(join(HERE, "..", "..", "analysis", "data", "fake_pod_frames.csv"), "utf8")
    .trim().split(/\r?\n/).slice(1).map((l) => l.split(","));
  const clean = all.filter((r) => r[0] === "clean" && r[2] === "frame").map((r) => r[4]);
  const F = {
    INFO: clean.find((h) => h.startsWith("20")),
    STATUS: clean.find((h) => h.startsWith("10")),
    DATAS: clean.filter((h) => h.startsWith("01") && h.slice(26, 28) === "01"),
    STOPPED: clean.find((h) => h.startsWith("10") && (flagsOf(h) & POD.S_STREAM_STOPPED)),
  };
  F.DATA = F.DATAS[0];
  const sw = all.filter((r) => r[0] === "sleep-wake-rebase" && r[2] === "frame").map((r) => r[4]);
  const SW = {
    info1: sw.find((h) => h.startsWith("20")),
    status1: sw.find((h) => h.startsWith("10")),
    data1: sw.find((h) => h.startsWith("01")),
    asleep: sw.find((h) => h.startsWith("10") && (flagsOf(h) & POD.S_GOING_TO_SLEEP)),
  };
  SW.info2 = sw.find((h) => h.startsWith("20") && h !== SW.info1);
  SW.status2 = sw.slice(sw.indexOf(SW.info2)).find((h) => h.startsWith("10"));
  SW.data2 = sw.find((h) => h.startsWith("01") && h.slice(26, 28) === "02");
  F.SW = SW;
  return F;
}

// CRP 0x0B (docs/ble.md 2.1)
const OK = 0, UNKNOWN_REG = 1, ACCESS_DENIED = 2, INVALID_VALUE = 3, BUSY = 8;
/** The pod's HELLO: ver 0x0B, max_ch 1, max_chunk 244, flags PUSH, device_id "BPOD". */
export const POD_HELLO = `010b01${le16(244)}01${le32(POD.DEVICE_ID)}`;
const POD_BYE = `02${le32(0)}${le32(POD.DEVICE_ID)}`;
const READ_ONLY = [POD.REG_PROTO, POD.REG_INFO, POD.REG_STATUS, POD.REG_CAL, POD.REG_DATA,
  POD.REG_LINK_KIND, POD.REG_LINK_MTU, POD.REG_LINK_N_MAX];

/** The CTRL op's length is right (docs/ble.md 4). */
const ctrlOk = (v) => ({ [POD.OP_STREAM_FROM]: 10, [POD.OP_STREAM_LIVE]: 4, [POD.OP_STOP]: 1, [POD.OP_SLEEP]: 2 })[v[0]] === v.length;

/** The packets a client sends, in hex: what the tests compare against. */
export const PKT = {
  hello: "010b0400020000000000",               // CRPClient.sendHello({})
  read: (txn, reg) => `06${h8(txn)}${le32(reg)}`,
  write: (txn, reg, vHex) => `04${h8(txn)}${le32(reg)}${vHex}`,
  bye: `02${le32(0)}${le32(0)}`,               // CRPClient.close(): base 0, device_id 0
  /** The value of a client's CTRL write (hex), or null if it isn't one. */
  ctrlOf: (h) => (h.startsWith("04") && h.slice(2, 4) !== "00" && h.slice(4, 12) === le32(POD.REG_CTRL) ? h.slice(12) : null),
};

export class FakeCrpPod {
  constructor({ info, status, mtu = 247 } = {}) {
    this.info = info;                   // what the reads answer (hex frames); a woken pod: wake()
    this.status = status;
    this.cal = `30${"00".repeat(63)}`;
    this.mtu = mtu;                     // the ATT MTU (BUSY when a READ's answer won't fit)
    this.linked = false;
    this.session = false;               // a HELLO came and nothing has ended it since
    this.pushOn = false;                // the PUSH register
    this.hold = false;                  // the session hold: 30 min idle limit, no lid-down while on
    this.send = null;                  // the current link's way to the client: send([u8, ...])
    this.rx = [];                       // every packet received (hex), every link, in order
    this.ctrls = [];                    // every CTRL value received (hex), in order
    this.held = [];                     // INFO answers waiting for release()
    this.holdInfo = false;              // INFO reads are answered only at release()
    this.reading = false;               // an INFO read is held
    this.readDelay = 0;                 // ms before a READ is answered (a slow one: nothing else waits for it)
    this.busy = false;                  // CTRL is answered BUSY (the sleep prologue)
    this.answerStop = false;            // a STOP is answered with stopped (the stream_stopped STATUS)
    this.stopped = null;
    this.onRx = null;                   // (u8): every packet received, before it's handled
    this.onPushOn = null;               // () => [frame hex]: pushed right after PUSH = 1's RESPONSE
    this.onCtrl = null;                 // (value u8) => [frame hex]: pushed after a taken CTRL op's RESPONSE
  }

  // ---- the link ----
  /** A new link: no session until HELLO. send(pkts) delivers a batch of packets to the client. */
  up(send) {
    this.linked = true;
    this.session = this.pushOn = this.reading = false;
    this.held = [];
    this.send = send;
  }

  down() {
    this.linked = false;
    this.session = this.pushOn = this.reading = false;
    this.held = [];
    this.send = null;
  }

  /** A woken pod (a reboot): the new boot's INFO and STATUS. */
  wake(info, status) {
    this.info = info;
    this.status = status;
    this.busy = false;
    this.hold = false;                  // a reboot clears the hold
  }

  // ---- packets out ----
  pushOf(frameHex) {
    const reg = frameHex.startsWith("10") ? POD.REG_STATUS : POD.REG_DATA;
    return bytes(`0400${le32(reg)}${frameHex}`);
  }

  response(txn, status, reg, valueHex = "") {
    return bytes(`0c${h8(txn)}${h8(status)}ff${le32(reg)}${valueHex}`);
  }

  /** Answers on the next turn, to the link they were made for. */
  out(pkts) {
    const send = this.send;
    if (send && pkts.length) setImmediate(() => { if (this.send === send) send(pkts); });
  }

  /** A push now (DATA or STATUS by the frame's type), only on a session with PUSH on. True if it went. */
  push(frameHex) {
    if (!this.linked || !this.session || !this.pushOn || !this.send) return false;
    this.send([this.pushOf(frameHex)]);
    return true;
  }

  /** The held INFO answers, now (synchronously, so a test can land one at an exact moment). */
  release() {
    const held = this.held;
    this.held = [];
    this.holdInfo = this.reading = false;
    if (this.send && held.length) this.send(held);
  }

  // ---- packets in ----
  receive(u8) {
    if (!this.linked) return;
    this.rx.push(hex(u8));
    this.onRx?.(u8);
    const t = u8[0];
    if (t === 0x01) {                   // HELLO: a new session, PUSH off
      if (u8.length < 10) return;
      if (u8[1] < 0x0B) return this.out([bytes(POD_BYE)]);
      this.session = true;
      this.pushOn = false;
      return this.out([bytes(POD_HELLO)]);
    }
    if (!this.session) return;          // before a HELLO the pod ignores everything else and sends nothing
    if (t === 0x02 || (t === 0x16 && u8[1] === 0xFF)) {   // BYE, END 0xFF
      this.session = this.pushOn = false;
      return;
    }
    if (u8.length < 6) return;
    const txn = u8[1];
    const reg = new DataView(u8.buffer, u8.byteOffset, u8.byteLength).getUint32(2, true);
    if (t === 0x06) return this.read(txn, reg);
    if (t === 0x04 && txn !== 0) return this.write(txn, reg, u8.subarray(6));
  }

  read(txn, reg) {
    let st = OK;
    let v = "";
    if (reg === POD.REG_INFO) v = this.info;
    else if (reg === POD.REG_STATUS) v = this.status;
    else if (reg === POD.REG_CAL) v = this.cal;
    else if (reg === POD.REG_PROTO) v = "01";
    else if (reg === POD.REG_PUSH) v = this.pushOn ? "01" : "00";
    else if (reg === POD.REG_LINK_KIND) v = "01";
    else if (reg === POD.REG_LINK_MTU) v = le16(Math.min(this.mtu, 247));
    else if (reg === POD.REG_DATA || reg === POD.REG_CTRL) st = ACCESS_DENIED;
    else st = UNKNOWN_REG;
    if (st === OK && 8 + v.length / 2 > Math.min(this.mtu, 247) - 3) st = BUSY;   // docs/ble.md 5
    const r = this.response(txn, st, reg, st === OK ? v : "");
    if (reg === POD.REG_INFO && st === OK && this.holdInfo) {
      this.held.push(r);
      this.reading = true;
      return;
    }
    if (this.readDelay) {
      const send = this.send;
      setTimeout(() => { if (this.send === send) send([r]); }, this.readDelay);
      return;
    }
    this.out([r]);
  }

  write(txn, reg, v) {
    let st;
    let after = [];
    if (reg === POD.REG_PUSH) {
      st = v.length === 1 && v[0] <= 1 ? OK : INVALID_VALUE;
      if (st === OK) {
        this.pushOn = v[0] === 1;
        if (this.pushOn) after = this.onPushOn?.() ?? [];
      }
    } else if (reg === POD.REG_CTRL) {
      this.ctrls.push(hex(v));
      st = this.busy ? BUSY : ctrlOk(v) ? OK : INVALID_VALUE;
      if (st === OK) {
        if (v[0] === POD.OP_STOP) this.hold = false;
        else if (v[0] === POD.OP_STREAM_LIVE || (v[0] === POD.OP_STREAM_FROM && u32(hex(v), 2) === bootOf(this.info))) this.hold = true;
        after = this.onCtrl?.(v) ?? [];
        if (this.answerStop && v[0] === POD.OP_STOP && this.stopped) after = [...after, this.stopped];
      }
    } else if (READ_ONLY.includes(reg)) {
      st = ACCESS_DENIED;
    } else {
      st = UNKNOWN_REG;
    }
    const pushes = this.pushOn ? after.map((f) => this.pushOf(f)) : [];   // PUSH off: nothing is pushed
    this.out([this.response(txn, st, reg), ...pushes]);
  }
}
