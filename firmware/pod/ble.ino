// ble.ino: the PodLink v1 Bluetooth service (docs/ble.md; plan: docs/ble-plan.md step 5).
// GATT table, advertising, events, the link-setup timeline, and bleLink's Bluetooth side: its
// send (one notification), the readable STATUS, connect, disconnect and the sleep prologue. The
// stream itself (the sender, CTRL commands, STATUS) is link.ino's, run on bleLink.
// Every notification, STATUS included, goes out on the DATA characteristic: Windows can deliver
// notifications of two characteristics out of order (measured 2026-10-23, D28), one
// characteristic keeps them in the order sent.
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
                                              // bleLink.pushOn (pod.ino): the central subscribed to DATA (CCCD)
volatile uint32_t dataOnMs = 0;               // millis() when DATA notifications were turned on
volatile uint32_t ctrlDropped = 0;            // CTRL writes lost to a full queue

// ---- loop()-only state ----
// The stream (cursor, frames, drains, STATUS queue) is bleLink's (pod.ino, link.ino): bleLink.up
// = connected, bleLink.mtu = loop's copy of evMtu, taken when its STATUS is queued.
bool bleOk = false;                           // begin() worked and boot_id was drawn: advertising
uint16_t connHdl = BLE_CONN_HANDLE_INVALID;
uint32_t seenConnects = 0, seenDisconnects = 0;
uint32_t hvnQueued = 0;                       // notifications handed to the SoftDevice
uint32_t connectMs = 0;
uint8_t linkStep = 0;                         // link-setup timeline progress (docs/ble.md 5)
uint32_t retryMs = 0;                         // the timeline's one retry after BUSY, while retried
bool retried = false;
uint32_t lastStatusValueMs = 0;
bool lastUsb = false, lastChg = false;

uint8_t sleepReasonNow = 0;                   // the going_to_sleep STATUS's sleep_reason
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
      bleLink.pushOn = false;                 // no bonding: every connection starts unsubscribed
      bleUp = true;
      nConnects++;
      linkEvent = true;
      break;
    }
    case BLE_GAP_EVT_DISCONNECTED:
      if (h != evConn) break;
      bleUp = false;
      bleLink.pushOn = false;
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
  bleLink.pushOn = on;
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
uint32_t inFlight() {
  int32_t d = (int32_t)(hvnQueued - hvnDone);
  return d < 0 ? 0 : d;
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

// ---- bleLink's Bluetooth side (link.ino calls these through bleLink, set in bleSetup) ----
// Every packet goes out on DATA, STATUS included (D28, top of this file).
bool bleSend(const uint8_t* p, uint16_t len) {
  return hvx(chrData.handles().value_handle, p, len);
}

uint16_t bleInterval() {
  return evInterval;                          // live: the BLE task copies every update
}

void bleReadable(const uint8_t* f) {
  chrStatus.write(f, 20);
}

// ---- connection state (from the event counters) ----
void onDisconnect() {
  bleLink.up = false;
  endStream(bleLink);                         // the session hold stays: a dropout keeps it
  hvnQueued = hvnDone;                        // a dead link reports no TX complete
  connHdl = BLE_CONN_HANDLE_INVALID;
  msg.print("# ble disconnected, reason 0x");
  msg.println(discReason, HEX);
  msg.send();
}

void onConnect(uint32_t now) {
  bleLink.up = true;
  connHdl = evConn;
  connectMs = now;
  linkStep = 0;
  retried = false;
  bleLink.mtu = evMtu;
  hvnQueued = hvnDone;
  bleLink.nextStatusMs = now + 1000;
  uint8_t f[20];
  buildStatus(bleLink, f, 0, head);
  chrStatus.write(f, 20);                     // the readable value is fresh at every connect
  msg.println("# ble connected");
  msg.send();
}

void printLink() {
  msg.print("# link mtu ");
  msg.print(bleLink.mtu);
  msg.print(" interval ");
  msg.print(evInterval * 1.25, 2);
  msg.print(" ms latency ");
  msg.print(evLatency);
  msg.print(" timeout ");
  msg.print(evTimeout * 10);
  msg.print(" ms phy ");
  msg.print(evPhy == 2 ? "2M" : evPhy == 4 ? "coded" : "1M");
  msg.print(" dl ");
  msg.print(evDl);
  msg.print(" n_max ");
  msg.println(nMax(bleLink));
  msg.send();
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
    msg.print("# link ");
    msg.print(what);
    msg.print(" error 0x");
    msg.println(err, HEX);
    msg.send();
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
  if (linkStep == 2 && bleLink.pushOn && (int32_t)(now - dataOnMs) >= 2000) {   // signed: dataOnMs can be after now
    ble_gap_conn_params_t p;                  // a named struct: g++ rejects &(type){...}
    p.min_conn_interval = 24;                 // 30 ms
    p.max_conn_interval = 40;                 // 50 ms
    p.slave_latency = 0;
    p.conn_sup_timeout = 400;                 // 4 s
    if (stepDone(sd_ble_gap_conn_param_update(connHdl, &p), now, "param update")) linkStep = 3;
  }
}

// ---- once, from setup() ----
// Quiet: setup() prints bleBanner() later, once a PC has had its chance to open the port.
const char* bleFail = NULL;                   // why Bluetooth is off, for the banner
int bleFreeHeap = 0;
char bleName[16];

bool bleSetup() {
  bleLink.kind = LINK_BLE;                    // the stream (link.ino) goes out through these
  bleLink.send = bleSend;
  bleLink.interval = bleInterval;
  bleLink.readable = bleReadable;
  bleLink.mtu = 23;
  Bluefruit.autoConnLed(false);               // the core's LED logic is active HIGH, the XIAO's LEDs are LOW
  Bluefruit.configPrphBandwidth(BANDWIDTH_MAX);   // MTU 247, 125 ms events, 3 notify slots (HVN_SLOTS)
  if (!Bluefruit.begin()) {
    bleFail = "# BLE failed to start (SoftDevice RAM?), USB only";
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
    bleFail = "# boot_id RNG failed, not advertising";
    return false;
  }

  Bluefruit.setTxPower(4);                    // before Advertising.start(), which applies it
  snprintf(bleName, sizeof bleName, "BarPod-%04X", (unsigned)(NRF_FICR->DEVICEADDR[0] & 0xFFFF));
  Bluefruit.setName(bleName);                 // after begin() (it resets the name), before addName()
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
  buildStatus(bleLink, f, 0, head);
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
    bleFail = "# ble advertising failed to start";
    return false;
  }
  vTaskSuspendAll();                              // the heap walk isn't locked against the BLE task
  bleFreeHeap = dbgHeapFree();
  xTaskResumeAll();
  bleOk = true;
  return true;
}

void bleBanner() {
  if (!bleOk) {
    Serial.println(bleFail ? bleFail : "# BLE off");
    return;
  }
  uint8_t mac[6];
  Bluefruit.getAddr(mac);                         // little-endian: mac[5] is the first byte shown
  Serial.print("# ble begin ok, ");
  Serial.print(bleName);
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
  Serial.print(" samples, free heap ");
  Serial.print(bleFreeHeap);
  Serial.println(" B");
}

// The BLE task's connect / disconnect counters -> loop()'s link state.
void bleLinkEvents(uint32_t now) {
  uint32_t nc = nConnects, nd = nDisconnects;
  if (nc != seenConnects || nd != seenDisconnects) {
    if (bleLink.up) onDisconnect();           // any change ends the old link's state
    seenConnects = nc;
    seenDisconnects = nd;
    if (bleUp) onConnect(now);
  }
}

// The sleep prologue (docs/ble.md 6), from goToSleep() once the sampler is parked, so head is
// final. Streaming: drain DATA up to it for at most 1.5 s (the frame ending there has FINAL; a
// sleep overrides a STOP drain). Then, if still connected: STATUS going_to_sleep with the reason
// and the final head, at most 300 ms until nothing is in flight, and a deliberate disconnect, so
// the phone knows at once instead of after the 4 s supervision timeout. CTRL is ignored from
// here on (nothing reads the queue); a disconnect at any point skips straight to the end.
void bleSleepPrologue(uint8_t reason) {
  if (!bleOk) return;
  Bluefruit.Advertising.restartOnDisconnect(false);
  Bluefruit.Advertising.stop();               // no new phone during the sleep setup
  uint32_t t0 = millis();
  uint32_t finalHead = head;
  bleLinkEvents(t0);
  bool wasStreaming = bleLink.up && bleLink.streaming;
  uint32_t cursorAtStart = bleLink.cursor;
  if (wasStreaming) {
    startSleepDrain(bleLink, t0, finalHead);  // keeps a sooner STOP cap; a built frame is rebuilt
    while (bleLink.up && bleLink.draining) {
      uint32_t now = millis();
      wdtFeed();
      bleLinkEvents(now);                     // a disconnect clears draining
      if (!bleLink.up) break;
      if (drainCapped(bleLink, now)) finishDrain(bleLink);   // capped: the rest is sleep-tail
      else sendPass(bleLink);                 // finishes the drain at the final head
      usbDrain();                             // the CSV catches up too (never blocks)
      delay(1);
    }
  }
  uint32_t tDrain = millis() - t0;
  uint32_t sentTo = bleLink.cursor;
  const char* statusWas = "none (no phone)";  // for the # line only
  if (bleLink.up) {
    sleepReasonNow = reason;
    queueStatus(bleLink, POD_S_GOING_TO_SLEEP, finalHead);
    statusWas = "not confirmed in 300 ms";
    uint32_t t1 = millis();
    while (bleLink.up && millis() - t1 < 300) {
      wdtFeed();
      bleLinkEvents(millis());
      if (!bleLink.up) { statusWas = "lost with the link"; break; }   // onDisconnect dropped the queue
      sendPass(bleLink);                      // not streaming any more: the queued STATUS only
      if (bleLink.nStatusQ == 0 && inFlight() == 0) {
        statusWas = bleLink.pushOn ? "out" : "readable only (DATA off)";
        break;
      }
      delay(1);
    }
  }
  uint32_t t2 = millis();
  bool hadLink = bleUp;
  if (hadLink) {                              // the phone sees 0x13 (remote user terminated)
    sd_ble_gap_disconnect(evConn, BLE_HCI_REMOTE_USER_TERMINATED_CONNECTION);
    while (bleUp && millis() - t2 < 500) { wdtFeed(); delay(1); }
  }
  uint32_t tDisc = millis() - t2;
  Bluefruit.Advertising.stop();               // again: the BLE task may have switched fast -> slow meanwhile
  bleLinkEvents(millis());
  msg.print("# ble sleep: ");
  if (wasStreaming) {
    msg.print("drained ");
    msg.print(sentTo - cursorAtStart);
    msg.print(" in ");
    msg.print(tDrain);
    msg.print(" ms, sleep-tail ");
    msg.print(finalHead - sentTo);
    msg.print(", ");
  }
  msg.print("STATUS ");
  msg.print(statusWas);
  if (hadLink) {
    msg.print(", disconnect ");
    msg.print(bleUp ? "timed out" : "done in ");
    if (!bleUp) { msg.print(tDisc); msg.print(" ms"); }
  }
  msg.println();
  msg.send();
}

// ---- every loop() pass ----
void bleTick(uint32_t now) {
  if (!bleOk) return;
  bleLinkEvents(now);

  CtrlMsg m;
  while (xQueueReceive(ctrlQueue, &m, 0) == pdTRUE) {
    if (bleLink.up) handleCtrl(bleLink, m.b, m.len);
  }

  bool usb = usbPowered(), chg = charging();
  bool powerChanged = usb != lastUsb || chg != lastChg;
  lastUsb = usb;
  lastChg = chg;

  if (bleLink.up) {
    linkSetup(now);
    if (linkEvent) {
      linkEvent = false;
      bleLink.mtu = evMtu;                    // n_max changes here, with its STATUS queued first
      printLink();
      queueStatus(bleLink, 0, head);          // n_per_packet / interval changed
    }
    linkTick(bleLink, now, powerChanged);     // power and 1 s STATUS, the drain's cap, a send pass
  } else if (powerChanged || now - lastStatusValueMs >= 1000) {
    lastStatusValueMs = now;
    queueStatus(bleLink, 0, head);            // not connected: only the readable value
  }

  if (usbOpen && now - lastBleLineMs >= 30000) {
    lastBleLineMs = now;
    msg.print("# ble ");
    msg.print(bleLink.up ? "connected" : (Bluefruit.Advertising.isRunning() ? "advertising" : "NOT advertising"));
    msg.print(bleLink.streaming ? ", streaming" : "");
    msg.print(", frames ");
    msg.print(bleLink.framesSent);
    msg.print(", lost ");
    msg.print(bleLink.lost);
    msg.print(", hold ");
    msg.println(sessionHold ? "on" : "off");
    msg.send();
    // the sampler (sampler.ino) and USB, since boot; the latency since the last line
    msg.print("# ring head ");
    msg.print(head);
    msg.print(", missed ");
    msg.print(missedDrdy);
    msg.print(" (late ");
    msg.print(lateRead);
    msg.print("), holes ");
    msg.print(holes);
    msg.print(", xl lag ");
    msg.print(xlLag);
    msg.print(", read errors ");
    msg.print(readErrors);
    msg.print(", restarts ");
    msg.print(imuRestarts);
    msg.print(", max latency ");
    msg.print(maxLatency * 15625 / 512);
    msg.print(" us, csv lost ");
    msg.print(csvLost);
    msg.print(", ctrl dropped ");
    msg.print(ctrlDropped);
    msg.print(", lines dropped ");
    msg.println(msg.dropped);
    msg.send();
    maxLatency = 0;
  }
}
