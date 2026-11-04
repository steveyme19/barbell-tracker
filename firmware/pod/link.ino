// link.ino: one link's PodLink v1 stream (docs/ble.md 3 to 6): the sender, CTRL commands, STATUS
// and the STOP and sleep drains, on a Link (pod.ino), so every link runs the same code. The link's
// owner connects it and gives it its side (send one packet, the connection interval, the readable
// STATUS): ble.ino for Bluetooth; USB gets its own (D45).
// The send rules mirror the Pod class in analysis/make_fake_pod.py, which the decoders were
// tested against: STATUS before DATA, a built frame is resent byte-identical, loss counted
// once per index, the STOP drain is capped.
//
// Threads: all of this runs in loop(), never in Bluefruit's BLE task. The one field that task
// writes is pushOn (volatile), read fresh at every use.

// ---- helpers ----
static inline uint32_t get32(const uint8_t* p) { return p[0] | (p[1] << 8) | (p[2] << 16) | ((uint32_t)p[3] << 24); }

// Samples per DATA frame at the link's MTU (docs/ble.md 5), 0 when not connected.
uint8_t nMax(const Link& L) {
  if (!L.up) return 0;
  int n = ((int)L.mtu - 3 - 14) / 14;         // Bluetooth: already clamped to 23..247 by the event callback
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

// Queue a STATUS for the phone, and refresh the readable value with a plain one (event bits
// belong only to the one STATUS that reports the event: a read after a boot_mismatch must not
// report it again). Not connected: only the readable value. A full queue (notify stalled for
// seconds) drops its oldest plain STATUS; event STATUS frames are kept.
void queueStatus(Link& L, uint8_t extra, uint32_t h) {
  uint8_t f[20];
  buildStatus(L, f, 0, head);
  if (L.readable) L.readable(f);
  if (!L.up) return;
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

// A STATUS goes out the way DATA does, in order with the DATA frames (Bluetooth: on the DATA
// characteristic, D28). DATA not subscribed: it counts as sent (queueStatus already refreshed
// the readable value).
bool sendStatus(Link& L, const uint8_t* f) {
  return !L.pushOn || L.send(f, 20);
}

bool sendData(Link& L) {
  return L.pushOn && L.send(L.dataFrame, L.dataLen);
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
// own going_to_sleep STATUS instead (bleSleepPrologue).
void finishDrain(Link& L) {
  L.draining = false;
  L.dataLen = 0;
  L.streaming = false;
  if (!L.sleepDrain) queueStatus(L, POD_S_STREAM_STOPPED, L.drainHead);
}

// A running drain is capped (it ends now: the rest is the phone's stop-tail or sleep-tail) once
// it's past its deadline or DATA can't flow (n_max < 4, DATA off) (docs/ble.md 4 STOP, 6).
bool drainCapped(const Link& L, uint32_t now) {
  return (int32_t)(now - L.drainDeadlineMs) >= 0 || nMax(L) < 4 || !L.pushOn;
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

// Every loop() pass while the link is up, after the owner's own link events: the power and 1 s
// STATUS, the drain's cap, then a send pass.
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

// ---- CTRL ops (docs/ble.md 4) ----
void handleCtrl(Link& L, const uint8_t* b, uint8_t len) {
  if (len == 0 || sleepCmd) return;           // after a SLEEP the pod ignores CTRL (docs/ble.md 6)
  uint8_t op = b[0];
  uint32_t head = ::head;                     // one snapshot for the whole command
  if (op == POD_OP_STREAM_FROM || op == POD_OP_STREAM_LIVE) {
    uint32_t cur;
    if (op == POD_OP_STREAM_FROM) {
      if (len != 10) return;
      uint32_t boot = get32(b + 2), from = get32(b + 6);
      if (boot != bootId) {                   // not this boot: say so, change nothing else
        queueStatus(L, POD_S_BOOT_MISMATCH, head);
        return;
      }
      uint32_t lo = head > RING_N ? head - RING_N : 0;    // this form: head - RING_N wraps early on
      cur = from < lo ? lo : (from > head ? head : from);
      L.overflowPending = false;
      if (from < lo) {                        // asked for samples that already left the ring
        countLost(L, from, lo);
        L.overflowPending = true;
      }
    } else {
      if (len != 4) return;
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
    sessionHold = true;
    queueStatus(L, 0, head);                  // the STATUS answering it goes before its first DATA
  } else if (op == POD_OP_STOP && len == 1) {
    sessionHold = false;                      // released at once, not after the drain
    if (!L.streaming) {
      queueStatus(L, POD_S_STREAM_STOPPED, head);
      return;
    }
    uint32_t backlog = head - L.cursor;
    uint8_t nm = nMax(L);
    L.draining = true;
    L.drainHead = head;
    L.drainDeadlineMs = millis() + 1500 + backlog * 1000 / (60 * (nm ? nm : 1));
  } else if (op == POD_OP_SLEEP && len == 2 && b[1] <= 1) {
    sleepCmd = b[1] == 0 ? 1 : 2;             // loop() runs it (the prologue can't run inside bleTick)
  }
  // Unknown ops, modes and wrong lengths are ignored.
}

// ---- the link's end and the sleep drain ----
// A disconnect ends the stream, not the session hold (docs/ble.md 5): the built frame and the
// queued STATUS frames go. The owner marks the link down first.
void endStream(Link& L) {
  L.streaming = false;                        // the session hold stays: a dropout keeps it
  L.draining = false;
  L.dataLen = 0;
  L.nStatusQ = 0;
}

// The sleep prologue's drain (docs/ble.md 6), up to the final head for at most 1.5 s from t0
// (the frame ending there has FINAL; a sleep overrides a STOP drain). The prologue (ble.ino)
// then runs it with drainCapped and sendPass.
void startSleepDrain(Link& L, uint32_t t0, uint32_t finalHead) {
  // A sleep during a STOP drain keeps the STOP's cap if that's sooner: the phone's stop wait is
  // sized on it (make_fake_pod.py stop-then-sleep-at-cap)
  uint32_t cap = t0 + 1500;
  if (!(L.draining && (int32_t)(L.drainDeadlineMs - cap) < 0)) L.drainDeadlineMs = cap;
  L.draining = L.sleepDrain = true;
  L.drainHead = finalHead;
  L.dataLen = 0;                              // a frame built for a STOP drain may carry its FINAL: rebuild it
}
