// ble.ino: the PodLink v1 Bluetooth service (docs/ble.md; plan: docs/ble-plan.md step 5).
// GATT table, advertising, the sender, CTRL commands, STATUS and the link-setup timeline.
// The send rules mirror the Pod class in analysis/make_fake_pod.py, which the decoders were
// tested against: STATUS before DATA, a built frame is resent byte-identical, loss counted
// once per index, the STOP drain is capped. Every notification, STATUS included, goes out on
// the DATA characteristic: Windows can deliver notifications of two characteristics out of
// order (measured 2026-10-23, D28), one characteristic keeps them in the order sent.
//
// Threads: Bluefruit's BLE task (priority 3) preempts loop() (priority 1) at any point. So:
//   - the BLE-task callbacks below only copy values, set flags or queue bytes. Nothing else,
//     and never a Serial print (it would splice into a CSV line);
//   - loop() talks to the SoftDevice directly (sd_* calls with the cached handle) and never
//     touches a BLEConnection object: the BLE task deletes it on a disconnect, at any instant.
//     Bluefruit's notify() would also wait up to 100 ms for a free slot; sd_ble_gatts_hvx never waits.

BLEService podService(POD_UUID_SERVICE);
BLECharacteristic chrData(POD_UUID_DATA);
BLECharacteristic chrCtrl(POD_UUID_CTRL);
BLECharacteristic chrStatus(POD_UUID_STATUS);
BLECharacteristic chrInfo(POD_UUID_INFO);
BLECharacteristic chrCal(POD_UUID_CAL);

const uint32_t HVN_SLOTS = 3;                 // = the HVN queue configPrphBandwidth(BANDWIDTH_MAX) sets

// ---- written in the BLE task, read by loop() ----
QueueHandle_t ctrlQueue = NULL;               // CTRL writes, 4 deep
volatile bool bleUp = false;                  // a central is connected right now
volatile uint16_t evConn = BLE_CONN_HANDLE_INVALID;
volatile uint32_t nConnects = 0, nDisconnects = 0;
volatile uint8_t discReason = 0;
volatile uint32_t hvnDone = 0;                // notifications the SoftDevice finished (TX complete)
volatile uint16_t evMtu = 23, evInterval = 0, evLatency = 0, evTimeout = 0, evDl = 27;
volatile uint8_t evPhy = 1;                   // 1 = 1M, 2 = 2M, 4 = coded
volatile bool linkEvent = false;              // one of the values above changed
volatile bool dataOn = false;                 // the central subscribed to DATA (CCCD)
volatile uint32_t dataOnMs = 0;               // millis() when DATA notifications were turned on
volatile uint32_t ctrlDropped = 0;            // CTRL writes lost to a full queue

// ---- loop()-only state ----
bool bleOk = false;                           // begin() worked and boot_id was drawn: advertising
bool connected = false;
uint16_t connHdl = BLE_CONN_HANDLE_INVALID;
uint32_t seenConnects = 0, seenDisconnects = 0;
uint32_t hvnQueued = 0;                       // notifications handed to the SoftDevice
uint32_t connectMs = 0;
uint8_t linkStep = 0;                         // link-setup timeline progress (docs/ble.md 5)
uint32_t retryMs = 0;                         // the timeline's one retry after BUSY, while retried
bool retried = false;
uint16_t mtuNow = 23;                         // loop's copy of evMtu, taken when its STATUS is queued
uint32_t nextStatusMs = 0, lastStatusValueMs = 0;
bool lastUsb = false, lastChg = false;

bool streaming = false;
uint32_t bleCursor = 0;                       // next index to send
uint8_t cmdSeq = 0;                           // cmd_seq of the stream being sent
uint16_t pktSeq = 0;                          // +1 per DATA frame accepted by the SoftDevice
bool resyncPending = false, overflowPending = false;
uint32_t lostMark = 0;                        // indices below this were already counted as lost
uint8_t dataFrame[POD_DATA_MAX];              // a built DATA frame waiting for a free slot
uint16_t dataLen = 0;                         // 0 = none built
uint8_t dataN = 0;
bool draining = false;                        // a STOP drain is running
uint32_t drainHead = 0, drainDeadlineMs = 0;
const uint8_t STATUS_Q = 8;
uint8_t statusQ[STATUS_Q][20];                // built STATUS frames, sent before any further DATA
uint8_t nStatusQ = 0;
uint32_t framesSent = 0;
uint32_t lastBleLineMs = 0;

// ---- BLE-task callbacks: copy, flag, queue, nothing else ----
// Every event, after Bluefruit's own handling (its getters are already updated).
void bleEventCb(ble_evt_t* e) {
  uint16_t h = e->evt.common_evt.conn_handle;
  switch (e->header.evt_id) {
    case BLE_GAP_EVT_CONNECTED: {
      const ble_gap_evt_connected_t& c = e->evt.gap_evt.params.connected;
      if (c.role != BLE_GAP_ROLE_PERIPH) break;
      evConn = h;
      evMtu = 23;
      evDl = 27;
      evPhy = 1;
      evInterval = c.conn_params.max_conn_interval;
      evLatency = c.conn_params.slave_latency;
      evTimeout = c.conn_params.conn_sup_timeout;
      dataOn = false;                         // no bonding: every connection starts unsubscribed
      bleUp = true;
      nConnects++;
      linkEvent = true;
      break;
    }
    case BLE_GAP_EVT_DISCONNECTED:
      if (h != evConn) break;
      bleUp = false;
      dataOn = false;
      discReason = e->evt.gap_evt.params.disconnected.reason;
      nDisconnects++;
      break;
    case BLE_GATTS_EVT_HVN_TX_COMPLETE:
      if (h == evConn) hvnDone += e->evt.gatts_evt.params.hvn_tx_complete.count;   // the only writer
      break;
    case BLE_GATTS_EVT_EXCHANGE_MTU_REQUEST: {  // the phone asked; Bluefruit answered min(its, 247)
      uint16_t m = e->evt.gatts_evt.params.exchange_mtu_request.client_rx_mtu;
      evMtu = m > 247 ? 247 : (m < 23 ? 23 : m);
      linkEvent = true;
      break;
    }
    case BLE_GATTC_EVT_EXCHANGE_MTU_RSP: {      // we asked for 247: the MTU is the smaller one
      uint16_t m = e->evt.gattc_evt.params.exchange_mtu_rsp.server_rx_mtu;
      evMtu = m > 247 ? 247 : (m < 23 ? 23 : m);
      linkEvent = true;
      break;
    }
    case BLE_GAP_EVT_CONN_PARAM_UPDATE: {       // also sent when the phone said no: copy what holds
      const ble_gap_conn_params_t& p = e->evt.gap_evt.params.conn_param_update.conn_params;
      evInterval = p.max_conn_interval;
      evLatency = p.slave_latency;
      evTimeout = p.conn_sup_timeout;
      linkEvent = true;
      break;
    }
    case BLE_GAP_EVT_PHY_UPDATE:
      if (e->evt.gap_evt.params.phy_update.status == BLE_HCI_STATUS_CODE_SUCCESS)
        evPhy = e->evt.gap_evt.params.phy_update.tx_phy;
      linkEvent = true;
      break;
    case BLE_GAP_EVT_DATA_LENGTH_UPDATE:
      evDl = e->evt.gap_evt.params.data_length_update.effective_params.max_tx_octets;
      linkEvent = true;
      break;
    default:
      break;
  }
}

// DATA's CCCD writes, inline in the BLE task (no malloc, no deferral)
void bleCccdCb(uint16_t h, BLECharacteristic* chr, uint16_t value) {
  (void)h; (void)chr;
  bool on = value & BLE_GATT_HVX_NOTIFICATION;
  dataOn = on;
  if (on) dataOnMs = millis();
}

// CTRL writes, inline in the BLE task: copy the bytes into the queue for loop()
void bleCtrlCb(uint16_t h, BLECharacteristic* chr, uint8_t* data, uint16_t len) {
  (void)h; (void)chr;
  if (len > 16) return;                       // setMaxLen(16): can't happen
  CtrlMsg m;
  m.len = len;
  memcpy(m.b, data, len);
  if (xQueueSend(ctrlQueue, &m, 0) != pdTRUE) ctrlDropped++;   // never wait in the BLE task
}

// ---- helpers ----
static inline uint32_t get32(const uint8_t* p) { return p[0] | (p[1] << 8) | (p[2] << 16) | ((uint32_t)p[3] << 24); }

uint32_t inFlight() {
  int32_t d = (int32_t)(hvnQueued - hvnDone);
  return d < 0 ? 0 : d;
}

// Samples per DATA frame at the current MTU (docs/ble.md 5), 0 when not connected.
uint8_t nMax() {
  if (!connected) return 0;
  int n = ((int)mtuNow - 3 - 14) / 14;        // already clamped to 23..247 by the event callback
  if (n < 0) n = 0;
  return n > POD_MAX_N ? POD_MAX_N : n;
}

// A STATUS frame now. extra = event bits; h = head (the stopped stream's end for stream_stopped).
void buildStatus(uint8_t* f, uint8_t extra, uint32_t h) {
  uint8_t nm = nMax();
  uint8_t flags = extra | (usbPowered() ? POD_S_USB : 0) | (charging() ? POD_S_CHARGING : 0) |
                  (lastBattV < LOW_BATT_WARN_V && !usbPowered() ? POD_S_BATT_LOW : 0) |
                  (streaming ? POD_S_STREAMING : 0) | (connected && nm < 4 ? POD_S_MTU_TOO_SMALL : 0);
  podPackStatus(f, flags, (uint16_t)(lastBattV * 1000), (uint32_t)rtcExtend(), h, lostBle, 0, 0, nm,
                connected ? evInterval : 0);
}

// Queue a STATUS for the phone, and refresh the readable value with a plain one (event bits
// belong only to the one STATUS that reports the event: a read after a boot_mismatch must not
// report it again). Not connected: only the readable value. A full queue (notify stalled for
// seconds) drops its oldest plain STATUS; event STATUS frames are kept.
void queueStatus(uint8_t extra, uint32_t h) {
  uint8_t f[20];
  buildStatus(f, 0, head);
  chrStatus.write(f, 20);
  if (!connected) return;
  buildStatus(f, extra, h);
  if (nStatusQ == STATUS_Q) {
    const uint8_t EVENTS = POD_S_GOING_TO_SLEEP | POD_S_STREAM_STOPPED | POD_S_BOOT_MISMATCH;
    uint8_t drop = 0;
    while (drop < STATUS_Q - 1 && (statusQ[drop][1] & EVENTS)) drop++;
    memmove(statusQ[drop], statusQ[drop + 1], (STATUS_Q - 1 - drop) * 20);
    nStatusQ--;
  }
  memcpy(statusQ[nStatusQ++], f, 20);
}

// One notification, never waiting: true if the SoftDevice took it. Any refusal (no free slot,
// an MTU exchange running, the link just went) is tried again on the next pass.
bool hvx(uint16_t valueHandle, const uint8_t* p, uint16_t len) {
  if (inFlight() >= HVN_SLOTS) return false;
  ble_gatts_hvx_params_t hv;
  memset(&hv, 0, sizeof hv);
  hv.handle = valueHandle;
  hv.type = BLE_GATT_HVX_NOTIFICATION;
  hv.p_len = &len;
  hv.p_data = p;
  if (sd_ble_gatts_hvx(connHdl, &hv) != NRF_SUCCESS) return false;
  hvnQueued++;
  return true;
}

// A STATUS goes out on DATA, in order with the DATA frames. DATA not subscribed: it counts as
// sent (queueStatus already refreshed the readable value).
bool sendStatus(const uint8_t* f) {
  return !dataOn || hvx(chrData.handles().value_handle, f, 20);
}

bool sendData() {
  return dataOn && hvx(chrData.handles().value_handle, dataFrame, dataLen);
}

// Count the samples from `from` up to lo as lost, once per index (docs/ble.md 4).
void countLost(uint32_t from, uint32_t lo) {
  uint32_t f = from > lostMark ? from : lostMark;
  if (lo > f) lostBle += lo - f;
  if (lo > lostMark) lostMark = lo;
}

// Build the next DATA frame from bleCursor into dataFrame. false = nothing to send yet.
bool buildData() {
  uint8_t nm = nMax();
  if (nm < 4) return false;                   // MTU too small: no quiet degrading
  if (head - bleCursor > RING_N) {            // sampling lapped the cursor
    uint32_t lo = head - RING_N;
    countLost(bleCursor, lo);
    bleCursor = lo;
    overflowPending = true;
  }
  uint32_t end = draining ? drainHead : head;
  if ((int32_t)(end - bleCursor) <= 0) return false;
  uint32_t pending = end - bleCursor;
  uint32_t t0 = ring[bleCursor & RING_MASK].ticks;
  uint32_t age = (uint32_t)rtcExtend() - t0;
  uint8_t full = pending < nm ? pending : nm;
  uint8_t n = full;
  for (uint8_t k = 1; k < full; k++) {        // a frame starts at every IMU restart, dt < 60000
    uint32_t i = bleCursor + k;
    if (isRestart(i) || ring[i & RING_MASK].ticks - t0 > POD_DT_MAX) { n = k; break; }
  }
  bool cutShort = n < full;
  if (n < nm && !(draining || cutShort || age >= POD_AGE_TICKS)) return false;   // wait for a full frame
  uint8_t flags = (resyncPending ? POD_F_RESYNC : 0) | (overflowPending ? POD_F_OVERFLOW : 0) |
                  (isRestart(bleCursor) ? POD_F_IMU_RESTART : 0) |
                  (draining && bleCursor + n == drainHead ? POD_F_FINAL : 0);
  dataLen = podPackData(dataFrame, ring, RING_MASK, bleCursor, n, pktSeq, flags, cmdSeq);
  dataN = n;
  return true;
}

void finishDrain() {
  draining = false;
  dataLen = 0;
  streaming = false;
  queueStatus(POD_S_STREAM_STOPPED, drainHead);
}

// One send pass: queued STATUS first, then up to 4 DATA frames.
void sendPass() {
  while (nStatusQ) {
    if (!sendStatus(statusQ[0])) return;
    memmove(statusQ[0], statusQ[1], (nStatusQ - 1) * 20);
    nStatusQ--;
  }
  if (!(streaming && connected)) return;
  for (int k = 0; k < 4; k++) {
    if (!dataLen && !buildData()) break;
    if (!sendData()) break;
    bleCursor += dataN;
    pktSeq++;
    framesSent++;
    resyncPending = overflowPending = false;
    dataLen = 0;
  }
  if (draining && (int32_t)(bleCursor - drainHead) >= 0 && !dataLen) finishDrain();
}

// ---- CTRL ops (docs/ble.md 4) ----
void handleCtrl(const uint8_t* b, uint8_t len) {
  if (len == 0) return;
  uint8_t op = b[0];
  if (op == POD_OP_STREAM_FROM || op == POD_OP_STREAM_LIVE) {
    uint32_t cur;
    if (op == POD_OP_STREAM_FROM) {
      if (len != 10) return;
      uint32_t boot = get32(b + 2), from = get32(b + 6);
      if (boot != bootId) {                   // not this boot: say so, change nothing else
        queueStatus(POD_S_BOOT_MISMATCH, head);
        return;
      }
      uint32_t lo = head > RING_N ? head - RING_N : 0;    // this form: head - RING_N wraps early on
      cur = from < lo ? lo : (from > head ? head : from);
      overflowPending = false;
      if (from < lo) {                        // asked for samples that already left the ring
        countLost(from, lo);
        overflowPending = true;
      }
    } else {
      if (len != 4) return;
      uint32_t back = b[2] | (b[3] << 8);
      if (back > head) back = head;
      if (back > RING_N) back = RING_N;
      cur = head - back;
      overflowPending = false;
    }
    draining = false;                         // a STREAM command ends a STOP drain, no STATUS
    dataLen = 0;                              // a built, unsent frame belonged to the old stream
    bleCursor = cur;
    cmdSeq = b[1];
    resyncPending = true;
    streaming = true;
    sessionHold = true;
    queueStatus(0, head);                     // the STATUS answering it goes before its first DATA
  } else if (op == POD_OP_STOP && len == 1) {
    sessionHold = false;                      // released at once, not after the drain
    if (!streaming) {
      queueStatus(POD_S_STREAM_STOPPED, head);
      return;
    }
    uint32_t backlog = head - bleCursor;
    uint8_t nm = nMax();
    draining = true;
    drainHead = head;
    drainDeadlineMs = millis() + 1500 + backlog * 1000 / (60 * (nm ? nm : 1));
  }
  // SLEEP (0x10) comes with step 9. Unknown ops and wrong lengths are ignored.
}

// ---- connection state (from the event counters) ----
void onDisconnect() {
  connected = false;
  streaming = false;                          // the session hold stays: a dropout keeps it
  draining = false;
  dataLen = 0;
  nStatusQ = 0;
  hvnQueued = hvnDone;                        // a dead link reports no TX complete
  connHdl = BLE_CONN_HANDLE_INVALID;
  Serial.print("# ble disconnected, reason 0x");
  Serial.println(discReason, HEX);
}

void onConnect(uint32_t now) {
  connected = true;
  connHdl = evConn;
  connectMs = now;
  linkStep = 0;
  retried = false;
  mtuNow = evMtu;
  hvnQueued = hvnDone;
  nextStatusMs = now + 1000;
  uint8_t f[20];
  buildStatus(f, 0, head);
  chrStatus.write(f, 20);                     // the readable value is fresh at every connect
  Serial.println("# ble connected");
}

void printLink() {
  Serial.print("# link mtu ");
  Serial.print(mtuNow);
  Serial.print(" interval ");
  Serial.print(evInterval * 1.25, 2);
  Serial.print(" ms latency ");
  Serial.print(evLatency);
  Serial.print(" timeout ");
  Serial.print(evTimeout * 10);
  Serial.print(" ms phy ");
  Serial.print(evPhy == 2 ? "2M" : evPhy == 4 ? "coded" : "1M");
  Serial.print(" dl ");
  Serial.print(evDl);
  Serial.print(" n_max ");
  Serial.println(nMax());
}

// Retry a SoftDevice request once, 0.5 s later, if it came back BUSY. true = this step is done.
bool stepDone(uint32_t err, uint32_t now, const char* what) {
  if (err == NRF_ERROR_BUSY && !retried) {
    retried = true;
    retryMs = now + 500;
    return false;
  }
  retried = false;
  if (err != NRF_SUCCESS) {
    Serial.print("# link ");
    Serial.print(what);
    Serial.print(" error 0x");
    Serial.println(err, HEX);
  }
  return true;
}

// The link-setup timeline (docs/ble.md 5), spaced so the SoftDevice calls don't collide.
// Every call is a plain SoftDevice request: it returns at once, the answer comes as an event.
void linkSetup(uint32_t now) {
  if (retried && (int32_t)(now - retryMs) < 0) return;
  if (linkStep == 0 && now - connectMs >= 200) {
    // Data length (bigger radio packets). BUSY: the phone started one, Bluefruit answers it.
    if (evDl > 27) linkStep = 1;
    else {
      uint32_t err = sd_ble_gap_data_length_update(connHdl, NULL, NULL);
      if (err == NRF_ERROR_BUSY || stepDone(err, now, "data length")) linkStep = 1;
    }
  }
  if (linkStep == 1 && now - connectMs >= 1000) {
    // MTU 247, if the phone hasn't asked (Chrome on Android may never ask). One-shot per link.
    if (evMtu > 23) linkStep = 2;              // the phone already exchanged (live value)
    else if (stepDone(sd_ble_gattc_exchange_mtu_request(connHdl, 247), now, "mtu")) linkStep = 2;
  }
  if (linkStep == 2 && dataOn && (int32_t)(now - dataOnMs) >= 2000) {   // signed: dataOnMs can be after now
    ble_gap_conn_params_t p;                  // a named struct: g++ rejects &(type){...}
    p.min_conn_interval = 24;                 // 30 ms
    p.max_conn_interval = 40;                 // 50 ms
    p.slave_latency = 0;
    p.conn_sup_timeout = 400;                 // 4 s
    if (stepDone(sd_ble_gap_conn_param_update(connHdl, &p), now, "param update")) linkStep = 3;
  }
}

// ---- once, from setup() ----
bool bleSetup() {
  Bluefruit.autoConnLed(false);               // the core's LED logic is active HIGH, the XIAO's LEDs are LOW
  Bluefruit.configPrphBandwidth(BANDWIDTH_MAX);   // MTU 247, 125 ms events, 3 notify slots (HVN_SLOTS)
  if (!Bluefruit.begin()) {
    Serial.println("# BLE failed to start (SoftDevice RAM?), USB only");
    return false;
  }
  // boot_id: random per boot, never 0 (docs/ble.md 3, INFO)
  uint32_t t0 = millis();
  bool got = false;
  while (!got && millis() - t0 < 100) {
    uint8_t avail = 0;
    sd_rand_application_bytes_available_get(&avail);
    if (avail >= 4 && sd_rand_application_vector_get((uint8_t*)&bootId, 4) == NRF_SUCCESS && bootId != 0) got = true;
    else delay(1);
  }
  if (!got) {
    bootId = 0;
    Serial.println("# boot_id RNG failed, not advertising");
    return false;
  }

  Bluefruit.setTxPower(4);                    // before Advertising.start(), which applies it
  char name[16];
  snprintf(name, sizeof name, "BarPod-%04X", (unsigned)(NRF_FICR->DEVICEADDR[0] & 0xFFFF));
  Bluefruit.setName(name);                    // after begin() (it resets the name), before addName()
  Bluefruit.setEventCallback(bleEventCb);
  Bluefruit.Periph.setConnInterval(24, 40);   // the preferred parameters a phone can read (PPCP)
  Bluefruit.Periph.setConnSupervisionTimeout(400);   // agree with the request at DATA on + 2 s
  ctrlQueue = xQueueCreate(4, sizeof(CtrlMsg));

  // The GATT table, frozen from this build on (docs/ble.md 2). Set every length: Bluefruit's
  // default is 20 B and it cuts anything longer without a word.
  podService.begin();
  chrData.setProperties(CHR_PROPS_NOTIFY);
  chrData.setPermission(SECMODE_OPEN, SECMODE_NO_ACCESS);   // read perm = the CCCD's write perm
  chrData.setMaxLen(244);
  chrData.setCccdWriteCallback(bleCccdCb, false);
  chrData.begin();
  chrCtrl.setProperties(CHR_PROPS_WRITE);     // write with response
  chrCtrl.setPermission(SECMODE_NO_ACCESS, SECMODE_OPEN);
  chrCtrl.setMaxLen(16);
  chrCtrl.setWriteCallback(bleCtrlCb, false);
  chrCtrl.begin();
  chrStatus.setProperties(CHR_PROPS_READ);      // notified STATUS frames go out on DATA (D28)
  chrStatus.setPermission(SECMODE_OPEN, SECMODE_NO_ACCESS);
  chrStatus.setFixedLen(20);
  chrStatus.begin();
  chrInfo.setProperties(CHR_PROPS_READ);
  chrInfo.setPermission(SECMODE_OPEN, SECMODE_NO_ACCESS);
  chrInfo.setFixedLen(32);
  chrInfo.begin();
  chrCal.setProperties(CHR_PROPS_READ);
  chrCal.setPermission(SECMODE_OPEN, SECMODE_NO_ACCESS);
  chrCal.setFixedLen(64);
  chrCal.begin();

  // Values before advertising, so no read ever returns the zero default
  uint8_t f[64];
  podPackInfo(f, FW_BUILD, bootId, NRF_FICR->DEVICEID[0], ODR_HZ, RING_N, wakeReason);
  chrInfo.write(f, 32);
  memset(f, 0, 64);
  f[0] = POD_T_CAL;                           // stub until roadmap 1.3: byte 1 = 0, no calibration
  chrCal.write(f, 64);
  buildStatus(f, 0, head);
  chrStatus.write(f, 20);
  lastUsb = usbPowered();
  lastChg = charging();

  // All advertising data before the first start(): Bluefruit freezes the lengths there
  Bluefruit.Advertising.addFlags(BLE_GAP_ADV_FLAGS_LE_ONLY_GENERAL_DISC_MODE);
  Bluefruit.Advertising.addService(podService);   // flags + 128-bit UUID: 21 of 31 B
  Bluefruit.ScanResponse.addName();
  Bluefruit.Advertising.restartOnDisconnect(true);
  Bluefruit.Advertising.setInterval(32, 244);     // 20 ms fast, 152.5 ms slow (0.625 ms units)
  Bluefruit.Advertising.setFastTimeout(30);       // s of fast advertising
  if (!Bluefruit.Advertising.start(0)) {          // 0 = forever
    Serial.println("# ble advertising failed to start");
    return false;
  }

  uint8_t mac[6];
  Bluefruit.getAddr(mac);                         // little-endian: mac[5] is the first byte shown
  Serial.print("# ble begin ok, ");
  Serial.print(name);
  Serial.print(" ");
  for (int i = 5; i >= 0; i--) {
    if (mac[i] < 16) Serial.print('0');
    Serial.print(mac[i], HEX);
    if (i) Serial.print(':');
  }
  Serial.print(", boot_id 0x");
  Serial.print(bootId, HEX);
  Serial.print(", ring ");
  Serial.print(RING_N);
  vTaskSuspendAll();                              // the heap walk isn't locked against the BLE task
  int freeHeap = dbgHeapFree();
  xTaskResumeAll();
  Serial.print(" samples, free heap ");
  Serial.print(freeHeap);
  Serial.println(" B");
  bleOk = true;
  return true;
}

// ---- every loop() pass ----
void bleTick(uint32_t now) {
  if (!bleOk) return;
  uint32_t nc = nConnects, nd = nDisconnects;
  if (nc != seenConnects || nd != seenDisconnects) {
    if (connected) onDisconnect();            // any change ends the old link's state
    seenConnects = nc;
    seenDisconnects = nd;
    if (bleUp) onConnect(now);
  }

  CtrlMsg m;
  while (xQueueReceive(ctrlQueue, &m, 0) == pdTRUE) {
    if (connected) handleCtrl(m.b, m.len);
  }

  bool usb = usbPowered(), chg = charging();
  bool powerChanged = usb != lastUsb || chg != lastChg;
  lastUsb = usb;
  lastChg = chg;

  if (connected) {
    linkSetup(now);
    if (linkEvent) {
      linkEvent = false;
      mtuNow = evMtu;                         // n_max changes here, with its STATUS queued first
      printLink();
      queueStatus(0, head);                   // n_per_packet / interval changed
    }
    if (powerChanged) queueStatus(0, head);
    if ((int32_t)(now - nextStatusMs) >= 0) {
      nextStatusMs += 1000;
      if ((int32_t)(now - nextStatusMs) >= 0) nextStatusMs = now + 1000;   // after a long stall
      queueStatus(0, head);
    }
    if (draining && ((int32_t)(now - drainDeadlineMs) >= 0 || nMax() < 4 || !dataOn)) finishDrain();
    sendPass();
  } else if (powerChanged || now - lastStatusValueMs >= 1000) {
    lastStatusValueMs = now;
    queueStatus(0, head);                     // not connected: only the readable value
  }

  if (Serial && now - lastBleLineMs >= 30000) {
    lastBleLineMs = now;
    Serial.print("# ble ");
    Serial.print(connected ? "connected" : (Bluefruit.Advertising.isRunning() ? "advertising" : "NOT advertising"));
    Serial.print(streaming ? ", streaming" : "");
    Serial.print(", frames ");
    Serial.print(framesSent);
    Serial.print(", lost ");
    Serial.print(lostBle);
    Serial.print(", ctrl dropped ");
    Serial.print(ctrlDropped);
    Serial.print(", hold ");
    Serial.print(sessionHold ? "on" : "off");
    Serial.print(" | max poll gap ");               // timing diagnostics (pod.ino), us
    Serial.print(diagPollGap * 15625 / 512);
    Serial.print(" us, ble ");
    Serial.print(diagBle * 15625 / 512);
    Serial.print(" us, print ");
    Serial.print(diagPrint * 15625 / 512);
    Serial.print(" us, missed ");
    Serial.print(diagMissed);
    Serial.print(", imu read errors ");
    Serial.println(diagReadErr);
    diagPollGap = diagBle = diagPrint = 0;
  }
}
