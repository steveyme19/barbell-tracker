// link.ino: one link's PodLink v1 stream (docs/ble.md 3 to 6): the sender, CTRL commands, STATUS
// and the STOP and sleep drains, on a Link (pod.ino), so every link runs the same code. Both links
// carry it over CRP (docs/ble.md 2, D45): STATUS and DATA go out as pushes on the link's CRP link
// (podcrp.c), CTRL comes in as a register write, INFO / STATUS / CAL are register reads. The owners
// bring their link up and run its CRP pass around linkTick: ble.ino for Bluetooth, usb.ino for USB
// in CRP mode.
// The send rules mirror the Link class and Pod.ctrl in analysis/make_fake_pod.py, which the
// decoders were tested against: STATUS before DATA, a built frame is resent byte-identical, loss
// counted once per index, the STOP drain is capped.
//
// Threads: all of this runs in loop(), never in Bluefruit's BLE task. The one field that task
// writes is txOn (volatile), read fresh at every use. The CRP hooks (crpRead, crpCtrl, ...) are
// called from inside podcrp's pass, also in loop(): they set state and queue STATUS frames, and
// never push (podcrp refuses it there: a RESPONSE goes out before anything it causes).

// ---- helpers ----
static inline uint32_t get32(const uint8_t* p) { return p[0] | (p[1] << 8) | (p[2] << 16) | ((uint32_t)p[3] << 24); }

// Samples per DATA frame on the link (docs/ble.md 5), 0 when not up. Bluetooth: from the MTU,
// mtu_eff - 3 - 6 (the push's WRITE header) - 14, recomputed for every frame. USB: always 16.
uint8_t nMax(const Link& L) {
  if (!L.up) return 0;
  if (L.kind == LINK_USB) return POD_MAX_N;
  int n = ((int)L.mtu - 23) / 14;             // already clamped to 23..247 by the event callback
  if (n < 0) n = 0;
  return n > POD_MAX_N ? POD_MAX_N : n;
}

// A STATUS frame now. extra = event bits; h = head (the stopped stream's end for stream_stopped).
void buildStatus(Link& L, uint8_t* f, uint8_t extra, uint32_t h) {
  uint8_t nm = nMax(L);
  uint8_t flags = extra | (usbPowered() ? POD_S_USB : 0) | (charging() ? POD_S_CHARGING : 0) |
                  (lastBattV < LOW_BATT_WARN_V && !usbPowered() ? POD_S_BATT_LOW : 0) |
                  (L.streaming ? POD_S_STREAMING : 0) | (L.up && nm < 4 ? POD_S_MTU_TOO_SMALL : 0);
  uint8_t reason = (extra & POD_S_GOING_TO_SLEEP) ? sleepReasonNow : 0;   // only the STATUS that reports it
  podPackStatus(f, flags, (uint16_t)(lastBattV * 1000), (uint32_t)rtcExtend(), h, L.lost, missedDrdy, reason, nm,
                L.up ? L.interval() : 0);
}

// Queue a STATUS for the client, while the link is up. A read of STATUS builds its own, plain one
// when the READ is processed (crpRead), so event bits only ever go out in the one STATUS that
// reports the event. A full queue (pushes stalled for seconds) drops its oldest plain STATUS;
// event STATUS frames are kept.
void queueStatus(Link& L, uint8_t extra, uint32_t h) {
  if (!L.up) return;
  uint8_t f[20];
  buildStatus(L, f, extra, h);
  if (L.nStatusQ == STATUS_Q) {
    const uint8_t EVENTS = POD_S_GOING_TO_SLEEP | POD_S_STREAM_STOPPED | POD_S_BOOT_MISMATCH;
    uint8_t drop = 0;
    while (drop < STATUS_Q - 1 && (L.statusQ[drop][1] & EVENTS)) drop++;
    memmove(L.statusQ[drop], L.statusQ[drop + 1], (STATUS_Q - 1 - drop) * 20);
    L.nStatusQ--;
  }
  memcpy(L.statusQ[L.nStatusQ++], f, 20);
}

// The session took pushes (PUSH = 1) and, on Bluetooth, TX notifications are on (docs/ble.md 5).
bool pushOn(const Link& L) {
  return L.txOn && podcrp_pushes_on(L.crp);
}

// One push, [04][00][reg][frame], never waiting: true if the link took it. podcrp applies the
// rest of the rule (Bluetooth: an empty spool, a free slot, it fits; USB: room plus 128 B), and a
// refused frame is resent byte-identical on a later pass.
bool linkPush(Link& L, const uint8_t* f, uint16_t len) {
  return podcrp_push(L.crp, f[0] == POD_T_STATUS ? PODCRP_REG_STATUS : PODCRP_REG_DATA, f, len);
}

// A STATUS goes out the way DATA does, in order with the DATA frames (one TX characteristic on
// Bluetooth, D28). Pushes off: it counts as sent (a READ builds its own).
bool sendStatus(Link& L, const uint8_t* f) {
  return !pushOn(L) || linkPush(L, f, 20);
}

bool sendData(Link& L) {
  return pushOn(L) && linkPush(L, L.dataFrame, L.dataLen);
}

// Count the samples from `from` up to lo as lost, once per index (docs/ble.md 4).
void countLost(Link& L, uint32_t from, uint32_t lo) {
  uint32_t f = from > L.lostMark ? from : L.lostMark;
  if (lo > f) L.lost += lo - f;
  if (lo > L.lostMark) L.lostMark = lo;
}

// Build the next DATA frame from the cursor into dataFrame. false = nothing to send yet.
bool buildData(Link& L) {
  uint8_t nm = nMax(L);
  if (nm < 4) return false;                   // MTU too small: no quiet degrading
  uint32_t h = head;                          // the sampler task moves it: read it once
  if (h - L.cursor > RING_N) {                // sampling lapped the cursor
    uint32_t lo = h - RING_N;
    countLost(L, L.cursor, lo);
    L.cursor = lo;
    L.overflowPending = true;
  }
  uint32_t end = L.draining ? L.drainHead : h;
  if ((int32_t)(end - L.cursor) <= 0) return false;
  uint32_t pending = end - L.cursor;
  uint32_t t0 = ring[L.cursor & RING_MASK].ticks;
  uint32_t age = (uint32_t)rtcExtend() - t0;
  uint8_t full = pending < nm ? pending : nm;
  uint8_t n = full;
  for (uint8_t k = 1; k < full; k++) {        // a frame starts at every IMU restart, dt < 60000
    uint32_t i = L.cursor + k;
    if (isRestart(i) || ring[i & RING_MASK].ticks - t0 > POD_DT_MAX) { n = k; break; }
  }
  bool cutShort = n < full;
  if (n < nm && !(L.draining || cutShort || age >= POD_AGE_TICKS)) return false;   // wait for a full frame
  uint8_t flags = (L.resyncPending ? POD_F_RESYNC : 0) | (L.overflowPending ? POD_F_OVERFLOW : 0) |
                  (isRestart(L.cursor) ? POD_F_IMU_RESTART : 0) |
                  (L.draining && L.cursor + n == L.drainHead ? POD_F_FINAL : 0);
  L.dataLen = podPackData(L.dataFrame, ring, RING_MASK, L.cursor, n, L.pktSeq, flags, L.cmdSeq);
  L.dataN = n;
  __asm__ volatile("" ::: "memory");          // the copy happens before the check below
  if (head - L.cursor > RING_N) {             // the sampler overwrote the oldest slots mid-copy: drop
    L.dataLen = 0;                            // the frame, the next pass's lap check counts the loss
    return false;
  }
  return true;
}

// The end of a drain, done or capped. A STOP's says stream_stopped; the sleep prologue sends its
// own going_to_sleep STATUS instead (sleepPrologue).
void finishDrain(Link& L) {
  L.draining = false;
  L.dataLen = 0;
  L.streaming = false;
  if (!L.sleepDrain) queueStatus(L, POD_S_STREAM_STOPPED, L.drainHead);
}

// A running drain is capped (it ends now: the rest is the phone's stop-tail or sleep-tail) once
// it's past its deadline or DATA can't flow (n_max < 4, pushes off) (docs/ble.md 4 STOP, 6).
bool drainCapped(const Link& L, uint32_t now) {
  return (int32_t)(now - L.drainDeadlineMs) >= 0 || nMax(L) < 4 || !pushOn(L);
}

// One send pass: queued STATUS first, then up to 4 DATA frames.
void sendPass(Link& L) {
  while (L.nStatusQ) {
    if (!sendStatus(L, L.statusQ[0])) return;
    memmove(L.statusQ[0], L.statusQ[1], (L.nStatusQ - 1) * 20);
    L.nStatusQ--;
  }
  if (!(L.streaming && L.up)) return;
  for (int k = 0; k < 4; k++) {
    if (!L.dataLen && !buildData(L)) break;
    if (!sendData(L)) break;
    L.cursor += L.dataN;
    L.pktSeq++;
    L.framesSent++;
    L.resyncPending = L.overflowPending = false;
    L.dataLen = 0;
  }
  if (L.draining && (int32_t)(L.cursor - L.drainHead) >= 0 && !L.dataLen) finishDrain(L);
}

// Every loop() pass while the link is up, between its CRP steps (crpIn, then this, then crpOut):
// the power and 1 s STATUS, the drain's cap, then a send pass.
void linkTick(Link& L, uint32_t now, bool powerChanged) {
  if (powerChanged) queueStatus(L, 0, head);
  if ((int32_t)(now - L.nextStatusMs) >= 0) {
    L.nextStatusMs += 1000;
    if ((int32_t)(now - L.nextStatusMs) >= 0) L.nextStatusMs = now + 1000;   // after a long stall
    queueStatus(L, 0, head);
  }
  if (L.draining && drainCapped(L, now)) finishDrain(L);
  sendPass(L);
}

// A link's CRP pass around its own work (docs/ble.md 5, podcrp.h): the engine packets that waited
// first, then every queued client packet through the engine; after the link's work and sendPass,
// the spool again and the engine's tick (its NOP after 1 s of sending nothing; USB: liveness).
void crpIn(Link& L, uint32_t now) {
  podcrp_spool_drain(L.crp);
  podcrp_process_all(L.crp, now);
}

void crpOut(Link& L, uint32_t now) {
  podcrp_spool_drain(L.crp);
  podcrp_tick(L.crp, now);
}

// ---- CTRL ops (docs/ble.md 2.3 and 4). Returns the WRITE's CRP status. ----
uint8_t handleCtrl(Link& L, const uint8_t* b, uint8_t len) {
  if (sleepCmd || inPrologue) return CRP_STATUS_BUSY;   // from the SLEEP op on (docs/ble.md 6)
  if (len == 0) return CRP_STATUS_INVALID_VALUE;        // podcrp already refuses it
  uint8_t op = b[0];
  uint32_t head = ::head;                     // one snapshot for the whole command
  if ((op == POD_OP_STREAM_FROM && len == 10) || (op == POD_OP_STREAM_LIVE && len == 4)) {
    uint32_t cur;
    if (op == POD_OP_STREAM_FROM) {
      uint32_t boot = get32(b + 2), from = get32(b + 6);
      if (boot != bootId) {                   // not this boot: say so, change nothing else. Taken: its
        queueStatus(L, POD_S_BOOT_MISMATCH, head);   // real answer is the boot_mismatch STATUS
        return CRP_STATUS_OK;
      }
      uint32_t lo = head > RING_N ? head - RING_N : 0;    // this form: head - RING_N wraps early on
      cur = from < lo ? lo : (from > head ? head : from);
      L.overflowPending = false;
      if (from < lo) {                        // asked for samples that already left the ring
        countLost(L, from, lo);
        L.overflowPending = true;
      }
    } else {
      uint32_t back = b[2] | (b[3] << 8);
      if (back > head) back = head;
      if (back > RING_N) back = RING_N;
      cur = head - back;
      L.overflowPending = false;
    }
    L.draining = false;                       // a STREAM command ends a STOP drain, no STATUS
    L.dataLen = 0;                            // a built, unsent frame belonged to the old stream
    L.cursor = cur;
    L.cmdSeq = b[1];
    L.resyncPending = true;
    L.streaming = true;
    if (L.kind == LINK_BLE) sessionHold = true;   // USB never touches the hold (docs/ble.md 2.5)
    queueStatus(L, 0, head);                  // the STATUS answering it goes before its first DATA
    return CRP_STATUS_OK;
  }
  if (op == POD_OP_STOP && len == 1) {
    if (L.kind == LINK_BLE) sessionHold = false;  // released at once, not after the drain
    if (!L.streaming) {
      queueStatus(L, POD_S_STREAM_STOPPED, head);
      return CRP_STATUS_OK;
    }
    uint32_t backlog = head - L.cursor;
    uint8_t nm = nMax(L);
    L.draining = true;
    L.drainHead = head;
    L.drainDeadlineMs = millis() + 1500 + backlog * 1000 / (60 * (nm ? nm : 1));
    return CRP_STATUS_OK;
  }
  if (op == POD_OP_SLEEP && len == 2 && b[1] <= 1) {
    sleepCmd = b[1] == 0 ? 1 : 2;             // loop() runs it after this pass, once the RESPONSE is out
    return CRP_STATUS_OK;
  }
  return CRP_STATUS_INVALID_VALUE;            // unknown op, mode or length: nothing changed
}

// ---- the link's end and the sleep drain ----
// A disconnect or a session end ends the stream, not the session hold (docs/ble.md 2.2, 5): the
// built frame and the queued STATUS frames go. The owner marks a disconnected link down first.
void endStream(Link& L) {
  L.streaming = false;                        // the session hold stays: a dropout keeps it
  L.draining = false;
  L.dataLen = 0;
  L.nStatusQ = 0;
}

// The sleep prologue's drain (docs/ble.md 6), up to the final head for at most 1.5 s from t0
// (the frame ending there has FINAL; a sleep overrides a STOP drain). sleepPrologue then runs it
// with drainCapped and sendPass.
void startSleepDrain(Link& L, uint32_t t0, uint32_t finalHead) {
  // A sleep during a STOP drain keeps the STOP's cap if that's sooner: the phone's stop wait is
  // sized on it (make_fake_pod.py stop-then-sleep-at-cap)
  uint32_t cap = t0 + 1500;
  if (!(L.draining && (int32_t)(L.drainDeadlineMs - cap) < 0)) L.drainDeadlineMs = cap;
  L.draining = L.sleepDrain = true;
  L.drainHead = finalHead;
  L.dataLen = 0;                              // a frame built for a STOP drain may carry its FINAL: rebuild it
}

// ---- the CRP hooks (podcrp.h), shared by both links; c->user is the Link ----
static inline Link& linkOf(podcrp_link_t* c) { return *(Link*)c->user; }

// A register read (docs/ble.md 2.3), built now: so a STATUS read's head is at least that of any
// DATA already sent on this link. buf is zeroed and room is the register's full size (podcrp.c's
// table); podcrp answers PROTO, PUSH, KIND, MTU and the drop counters itself, and BUSY for an
// answer that wouldn't fit. 0 = not here (UNKNOWN_REG).
uint32_t crpRead(podcrp_link_t* c, uint32_t reg, uint8_t* buf, uint32_t room) {
  Link& L = linkOf(c);
  uint8_t f[20];
  switch (reg) {
    case PODCRP_REG_INFO:
      if (room < 32) return 0;
      podPackInfo(buf, FW_BUILD, bootId, NRF_FICR->DEVICEID[0], ODR_HZ, RING_N, wakeReason);
      return 32;
    case PODCRP_REG_STATUS:                   // plain: no event bits (docs/ble.md 2.3)
      if (room < 20) return 0;
      buildStatus(L, buf, 0, head);
      return 20;
    case PODCRP_REG_CAL:                      // a stub: byte 1 = 0, no calibration (the app applies the pod's, D42)
      if (room < 64) return 0;
      buf[0] = POD_T_CAL;
      return 64;
    // the state mirrors, for Ansync's dashboard (the app reads none of them)
    case PODCRP_REG_BATTERY_MV: podPut16(buf, (uint16_t)(lastBattV * 1000)); return 2;
    case PODCRP_REG_FLAGS: buildStatus(L, f, 0, head); buf[0] = f[1]; return 1;   // the state bits
    case PODCRP_REG_HEAD: podPut32(buf, head); return 4;
    case PODCRP_REG_TICKS: podPut32(buf, (uint32_t)rtcExtend()); return 4;
    case PODCRP_REG_BOOT_ID: podPut32(buf, bootId); return 4;
    case PODCRP_REG_SERIAL: podPut32(buf, NRF_FICR->DEVICEID[0]); return 4;
    case PODCRP_REG_FW_BUILD: podPut16(buf, FW_BUILD); return 2;
    case PODCRP_REG_WAKE_REASON: buf[0] = wakeReason; return 1;
    case PODCRP_REG_SLEEP_REASON: buf[0] = inPrologue ? sleepReasonNow : 0; return 1;   // 0 while awake
    case PODCRP_REG_LOST: podPut32(buf, L.lost); return 4;                         // not saturated
    case PODCRP_REG_MISSED_DRDY: podPut32(buf, missedDrdy); return 4;
    case PODCRP_REG_BATT_V: { float v = lastBattV; memcpy(buf, &v, 4); return 4; }
    case PODCRP_REG_RESETREAS: podPut32(buf, resetReas); return 4;
    case PODCRP_REG_NAME: {
      uint32_t n = strlen(bleName);
      if (n > room) n = room;
      memcpy(buf, bleName, n);
      return n;
    }
    // the link the read arrives on
    case PODCRP_REG_N_MAX: buf[0] = nMax(L); return 1;
    case PODCRP_REG_INTERVAL: podPut16(buf, L.up ? L.interval() : 0); return 2;
    case PODCRP_REG_PHY: buf[0] = L.kind == LINK_BLE ? evPhy : 0; return 1;        // USB: none, 0
    case PODCRP_REG_DL: podPut16(buf, L.kind == LINK_BLE ? evDl : 0); return 2;
    case PODCRP_REG_FRAMES: podPut32(buf, L.framesSent); return 4;
    case PODCRP_REG_STACK_FREE:               // the loop task's (this one's) least free stack, bytes
      podPut32(buf, uxTaskGetStackHighWaterMark(NULL) * sizeof(StackType_t));
      return 4;
    default:
      return 0;
  }
}

uint8_t crpCtrl(podcrp_link_t* c, const uint8_t* p, uint32_t len) {
  return handleCtrl(linkOf(c), p, (uint8_t)len);
}

// A session started (a HELLO answered). PUSH is 0 in it: nothing is pushed until the client's
// reads are done and it writes PUSH = 1 (docs/ble.md 7).
void crpSessionStart(podcrp_link_t* c) {
  if (linkOf(c).kind == LINK_USB) usbSessionStart();
}

// A session ended on a live link (BYE, END 0xFF, a HELLO over it, USB silence): its stream ends
// exactly as at a disconnect, the hold stays (docs/ble.md 2.2). podcrp has turned PUSH off and, on
// Bluetooth, emptied the spool. USB goes back to text, except at a HELLO (usb.ino).
void crpSessionEnd(podcrp_link_t* c, uint8_t why) {
  Link& L = linkOf(c);
  endStream(L);
  if (L.kind == LINK_USB && why != PODCRP_END_HELLO) usbSessionEnd(why);
}

// Once, from setup(), before anything can sleep the pod: both CRP links and their Links. The name
// (bleIdentity) must be set first: the engine keeps its length.
void linksInit() {
  podcrp_hooks_t h;
  memset(&h, 0, sizeof h);
  h.read = crpRead;
  h.ctrl = crpCtrl;
  h.on_session_start = crpSessionStart;
  h.on_session_end = crpSessionEnd;
  h.send = bleCrpSend;                        // ble.ino: one notification on TX
  podcrp_init(&crpBle, PODCRP_BLE, &h, PODCRP_DEVICE_ID, bleName);
  crpBle.user = &bleLink;
  h.send = usbCrpSend;                        // usb.ino: what the port takes
  h.bench = crpBench;                         // the BENCH register: USB only
  podcrp_init(&crpUsb, PODCRP_USB, &h, PODCRP_DEVICE_ID, bleName);
  crpUsb.user = &usbLink;

  bleLink.kind = LINK_BLE;
  bleLink.crp = &crpBle;
  bleLink.interval = bleInterval;
  bleLink.mtu = 23;
  usbLink.kind = LINK_USB;
  usbLink.crp = &crpUsb;
  usbLink.interval = usbInterval;
  usbLink.txOn = true;                        // no CCCD on USB: PUSH alone decides
  lastPower = (usbPowered() ? POD_S_USB : 0) | (charging() ? POD_S_CHARGING : 0);
}

// ---- the sleep prologue (docs/ble.md 6), from goToSleep() once the sampler is parked, so head is
// final. On every link that's up (Bluetooth connected, USB in CRP mode): if streaming, drain DATA
// up to that head for at most 1.5 s (the frame ending there has FINAL; a sleep overrides a STOP
// drain); then STATUS going_to_sleep with the reason and the final head, and at most 300 ms until
// nothing is in flight or queued (the slots, the spool and the STATUS queue); then Bluetooth
// disconnects deliberately, so the phone knows at once instead of after the 4 s supervision
// timeout. Meanwhile CTRL is answered BUSY (handleCtrl), and a link that goes down skips to the end.

// The prologue's view of the links: Bluetooth's events, the USB port, CRP bytes (no bench characters)
void sleepEvents(uint32_t now) {
  bleLinkEvents(now);                         // a disconnect: bleLink down, its stream ended
  usbOpen = (bool)Serial;
  if (usbMode == USB_CRP) usbRead(now);       // CRP mode only: its bytes go to the engine, nothing else
  else usbPortEvents(now);
}

// One prologue pass on a link: its CRP pass around the drain (or the queued STATUS).
void sleepPass(Link& L, uint32_t now) {
  if (!L.up) return;
  crpIn(L, now);
  if (L.up) {                                 // a BYE or END just now ended its session (and its stream)
    if (L.draining && drainCapped(L, now)) finishDrain(L);   // capped: the rest is sleep-tail
    else sendPass(L);                         // finishes the drain at the final head
  }
  crpOut(L, now);
}

// Nothing of the link's still waits: no STATUS queued, the spool empty, nothing in flight.
bool linkIdle(const Link& L) {
  return L.nStatusQ == 0 && podcrp_spool_empty(L.crp) && (L.kind != LINK_BLE || inFlight() == 0);
}

void sleepPrologue(uint8_t reason) {
  bleSleepStart();                            // no new phone during the sleep setup
  sleepReasonNow = reason;
  uint32_t t0 = millis();
  uint32_t finalHead = head;
  sleepEvents(t0);
  Link* links[2] = {&bleLink, &usbLink};
  bool was[2], told[2];
  uint32_t from[2], sentTo[2];
  for (int k = 0; k < 2; k++) {
    Link& L = *links[k];
    was[k] = L.up && L.streaming;
    from[k] = L.cursor;
    if (was[k]) startSleepDrain(L, t0, finalHead);   // keeps a sooner STOP cap; a built frame is rebuilt
  }
  while ((bleLink.up && bleLink.draining) || (usbLink.up && usbLink.draining)) {
    uint32_t now = millis();
    wdtFeed();
    sleepEvents(now);                         // a disconnect clears draining
    for (int k = 0; k < 2; k++) sleepPass(*links[k], now);
    usbDrain();                               // the CSV catches up too (never blocks)
    delay(1);
  }
  uint32_t tDrain = millis() - t0;
  for (int k = 0; k < 2; k++) {
    Link& L = *links[k];
    sentTo[k] = L.cursor;
    told[k] = L.up;
    if (L.up) queueStatus(L, POD_S_GOING_TO_SLEEP, finalHead);
  }
  const char* statusWas[2] = {"none (no phone)", "none"};   // for the # lines only
  bool settled[2];
  for (int k = 0; k < 2; k++) {
    settled[k] = !told[k];
    if (told[k]) statusWas[k] = "not confirmed in 300 ms";
  }
  uint32_t t1 = millis();
  while (millis() - t1 < 300) {
    uint32_t now = millis();
    wdtFeed();
    sleepEvents(now);
    bool waiting = false;
    for (int k = 0; k < 2; k++) {
      Link& L = *links[k];
      if (settled[k]) continue;
      if (!L.up) {                            // its end dropped the queue
        statusWas[k] = "lost with the link";
        settled[k] = true;
        continue;
      }
      sleepPass(L, now);                      // not streaming any more: the queued STATUS only
      if (L.up && linkIdle(L)) {
        statusWas[k] = pushOn(L) ? "out" : "no push (PUSH off)";
        settled[k] = true;
      } else {
        waiting = true;
      }
    }
    if (!waiting) break;
    delay(1);
  }
  bleSleepEnd(was[0], from[0], sentTo[0], finalHead, tDrain, statusWas[0]);   // the disconnect, the # line
  if (told[1] || was[1]) {
    msg.print("# usb sleep: ");
    if (was[1]) {
      msg.print("drained ");
      msg.print(sentTo[1] - from[1]);
      msg.print(", sleep-tail ");
      msg.print(finalHead - sentTo[1]);
      msg.print(", ");
    }
    msg.print("STATUS ");
    msg.println(statusWas[1]);
    msg.send();
  }
}
