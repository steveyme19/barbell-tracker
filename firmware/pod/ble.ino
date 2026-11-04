// ble.ino: the pod's Bluetooth side of CRP (docs/ble.md 2.4 and 5, D45; plan: docs/ble-plan.md).
// GATT table, address, advertising, events, the link-setup timeline, and bleLink's Bluetooth side:
// its send (one notification on TX), connect, disconnect, its CRP pass and its part of the sleep
// prologue. The stream itself (the sender, CTRL commands, STATUS, the prologue) is link.ino's, run
// on bleLink; CRP itself (the engine, the spool, the registers) is podcrp.c's, run on crpBle.
// Every packet the pod sends goes out on the one TX characteristic: Windows can deliver
// notifications of two characteristics out of order (measured 2026-10-23, D28), one
// characteristic keeps them in the order sent.
//
// Threads: Bluefruit's BLE task (priority 3) preempts loop() (priority 1) at any point. So:
//   - the BLE-task callbacks below only copy values, set flags or queue bytes. Nothing else,
//     and never a Serial print (it would splice into a CSV line). An RX write goes into podcrp's
//     RX queue (single producer, single consumer); loop() runs the engine on it;
//   - loop() talks to the SoftDevice directly (sd_* calls with the cached handle) and never
//     touches a BLEConnection object: the BLE task deletes it on a disconnect, at any instant.
//     Bluefruit's notify() would also wait up to 100 ms for a free slot and split anything above
//     MTU - 3; sd_ble_gatts_hvx never waits, and podcrp never hands it more than MTU - 3.

BLEService crpService(POD_CRP_SERVICE);
BLECharacteristic chrRx(POD_CRP_RX);
BLECharacteristic chrTx(POD_CRP_TX);

const uint32_t HVN_SLOTS = 3;                 // = the HVN queue configPrphBandwidth(BANDWIDTH_MAX) sets

// The pod's own static random address (docs/ble.md 2.4): the factory address (NRF_FICR->DEVICEADDR)
// XOR this fixed mask, top two bits 11. New with the CRP build, so phones and PCs see a new device
// with no cached PodLink v1 attribute table. Little-endian (addr[0] first): the mask is
// 0x5A3C96E1D24B. Chosen once: changing it changes every pod's address and name again.
const uint8_t POD_ADDR_MASK[6] = {0x4B, 0xD2, 0xE1, 0x96, 0x3C, 0x5A};

// ---- written in the BLE task, read by loop() ----
volatile bool bleUp = false;                  // a central is connected right now
volatile uint16_t evConn = BLE_CONN_HANDLE_INVALID;
volatile uint32_t nConnects = 0, nDisconnects = 0;
volatile uint8_t discReason = 0;
volatile uint32_t hvnDone = 0;                // notifications the SoftDevice finished (TX complete)
volatile uint16_t evMtu = 23, evInterval = 0, evLatency = 0, evTimeout = 0, evDl = 27;
volatile uint8_t evPhy = 1;                   // 1 = 1M, 2 = 2M, 4 = coded
volatile bool linkEvent = false;              // one of the values above changed
                                              // bleLink.txOn (pod.ino): the central subscribed to TX (CCCD)

// ---- loop()-only state ----
// The stream (cursor, frames, drains, STATUS queue) is bleLink's (pod.ino, link.ino): bleLink.up
// = connected, bleLink.mtu = loop's copy of evMtu, taken when its STATUS is queued (and given to
// crpBle with it).
bool bleOk = false;                           // begin() worked, the address is set, boot_id was drawn: advertising
uint16_t connHdl = BLE_CONN_HANDLE_INVALID;
uint16_t txHandle = 0;                        // TX's value handle, for sd_ble_gatts_hvx
uint32_t seenConnects = 0, seenDisconnects = 0;
uint32_t hvnQueued = 0;                       // notifications handed to the SoftDevice
uint32_t connectMs = 0;
uint8_t linkStep = 0;                         // link-setup timeline progress (docs/ble.md 5)
uint32_t retryMs = 0;                         // the timeline's one retry after BUSY, while retried
bool retried = false;
bool pushWasOn = false;                       // the session's PUSH, as of the last pass
uint32_t pushOnMs = 0;                        // when it went on: the conn param update is 2 s later

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
      bleLink.txOn = false;                   // no bonding: every connection starts unsubscribed
      bleUp = true;
      nConnects++;
      linkEvent = true;
      break;
    }
    case BLE_GAP_EVT_DISCONNECTED:
      if (h != evConn) break;
      bleUp = false;
      bleLink.txOn = false;
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

// TX's CCCD writes, inline in the BLE task (no malloc, no deferral)
void bleCccdCb(uint16_t h, BLECharacteristic* chr, uint16_t value) {
  (void)h; (void)chr;
  bleLink.txOn = value & BLE_GATT_HVX_NOTIFICATION;
}

// RX writes, inline in the BLE task: one write is one CRP packet, copied into crpBle's RX queue
// for loop() (podcrp_rx_packet only queues, and counts a full queue or a packet over 64 B)
void bleRxCb(uint16_t h, BLECharacteristic* chr, uint8_t* data, uint16_t len) {
  (void)h; (void)chr;
  podcrp_rx_packet(&crpBle, data, len);
}

// ---- helpers ----
uint32_t inFlight() {
  int32_t d = (int32_t)(hvnQueued - hvnDone);
  return d < 0 ? 0 : d;
}

// One notification, never waiting: true if the SoftDevice took it. Any refusal (no free slot,
// an MTU exchange running, the link just went) is tried again on a later pass.
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

// ---- bleLink's Bluetooth side (link.ino sets these up in linksInit) ----
// crpBle's send hook: one CRP packet as one notification on TX, all or nothing. Every packet the
// pod sends shares the 3 slots (docs/ble.md 5); only while connected with TX notifications on.
uint32_t bleCrpSend(podcrp_link_t* c, const uint8_t* p, uint32_t len) {
  (void)c;
  if (!bleLink.up || !bleLink.txOn || len > 0xFFFF) return 0;
  return hvx(txHandle, p, (uint16_t)len) ? len : 0;
}

uint16_t bleInterval() {
  return evInterval;                          // live: the BLE task copies every update
}

// ---- connection state (from the event counters) ----
// A disconnect ends the session and the stream (docs/ble.md 2.2, 5): crpBle's engine, spool and
// RX queue reset here in loop(), never in the BLE task. The session hold stays.
void onDisconnect() {
  bleLink.up = false;
  podcrp_reset(&crpBle);                      // PUSH off, MTU back to 23; no session-end hook
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
  pushWasOn = false;
  uint16_t m = evMtu;                         // one read: the BLE task may change it in between
  bleLink.mtu = m;
  podcrp_set_mtu(&crpBle, m);
  hvnQueued = hvnDone;
  bleLink.nextStatusMs = now + 1000;
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
  if (linkStep == 0 && now - connectMs >= 50) {
    // MTU 247 first, if the phone hasn't asked (Chrome on Android may never ask): CRP's INFO read
    // needs it (an answer that doesn't fit MTU - 3 is BUSY). One-shot per link.
    if (evMtu > 23) linkStep = 1;              // the phone already exchanged (live value)
    else if (stepDone(sd_ble_gattc_exchange_mtu_request(connHdl, 247), now, "mtu")) linkStep = 1;
  }
  if (linkStep == 1 && now - connectMs >= 250) {
    // Data length (bigger radio packets). BUSY: the phone started one, Bluefruit answers it.
    if (evDl > 27) linkStep = 2;
    else {
      uint32_t err = sd_ble_gap_data_length_update(connHdl, NULL, NULL);
      if (err == NRF_ERROR_BUSY || stepDone(err, now, "data length")) linkStep = 2;
    }
  }
  if (linkStep == 2 && pushWasOn && (int32_t)(now - pushOnMs) >= 2000) {   // 2 s after the session's PUSH = 1
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
char bleName[16];                             // BarPod-XXXX, also the CRP device name on both links
ble_gap_addr_t bleAddr;
uint8_t bleAdvLen = 0;                        // advertising bytes used, of 31

// The address and the name, from the factory address alone (no SoftDevice needed): first in
// setup(), since both CRP links take the name at init, Bluetooth or not.
void bleIdentity() {
  uint32_t a0 = NRF_FICR->DEVICEADDR[0], a1 = NRF_FICR->DEVICEADDR[1];
  memset(&bleAddr, 0, sizeof bleAddr);
  bleAddr.addr_type = BLE_GAP_ADDR_TYPE_RANDOM_STATIC;
  for (int i = 0; i < 6; i++) {
    uint8_t b = i < 4 ? a0 >> (8 * i) : a1 >> (8 * (i - 4));
    bleAddr.addr[i] = b ^ POD_ADDR_MASK[i];
  }
  bleAddr.addr[5] |= 0xC0;                    // static random: the top two bits are 11
  // The other 46 bits may be neither all 0 nor all 1 (Core spec, static address)
  bool zeros = (bleAddr.addr[5] & 0x3F) == 0, ones = bleAddr.addr[5] == 0xFF;
  for (int i = 0; i < 5; i++) {
    zeros = zeros && bleAddr.addr[i] == 0;
    ones = ones && bleAddr.addr[i] == 0xFF;
  }
  if (zeros || ones) bleAddr.addr[0] ^= 0x01;
  // The low 16 bits: the end of the MAC a phone shows (addr[5] is shown first)
  snprintf(bleName, sizeof bleName, "BarPod-%04X", (unsigned)(bleAddr.addr[0] | (bleAddr.addr[1] << 8)));
}

// boot_id: random per boot, never 0 (docs/ble.md 3, INFO). Drawn whether Bluetooth started or not:
// USB CRP serves INFO too, and refuses the switch without one (usb.ino). While the SoftDevice runs
// it owns the RNG, so through it: up to 100 ms for 4 bytes, NRF_SUCCESS required, a 0 redrawn.
// Without it (begin() failed before enabling it) the RNG peripheral itself, same rules.
bool drawBootId() {
  uint8_t sdOn = 0;
  (void)sd_softdevice_is_enabled(&sdOn);
  uint32_t t0 = millis(), v = 0;
  bootId = 0;
  if (sdOn) {
    while (!bootId && millis() - t0 < 100) {
      uint8_t avail = 0;
      sd_rand_application_bytes_available_get(&avail);
      if (avail >= 4 && sd_rand_application_vector_get((uint8_t*)&v, 4) == NRF_SUCCESS) bootId = v;
      else delay(1);
    }
  } else {
    NRF_RNG->CONFIG = RNG_CONFIG_DERCEN_Msk;   // bias correction
    NRF_RNG->EVENTS_VALRDY = 0;
    NRF_RNG->TASKS_START = 1;
    uint8_t got = 0;
    while (!bootId && millis() - t0 < 100) {
      if (!NRF_RNG->EVENTS_VALRDY) continue;   // a byte every ~120 us
      NRF_RNG->EVENTS_VALRDY = 0;
      v = (v >> 8) | ((uint32_t)NRF_RNG->VALUE << 24);
      if (++got % 4 == 0) bootId = v;          // a 0 takes 4 more bytes
    }
    NRF_RNG->TASKS_STOP = 1;
  }
  return bootId != 0;
}

bool bleSetup() {
  Bluefruit.autoConnLed(false);               // the core's LED logic is active HIGH, the XIAO's LEDs are LOW
  Bluefruit.configPrphBandwidth(BANDWIDTH_MAX);   // MTU 247, 125 ms events, 3 notify slots (HVN_SLOTS)
  bool begun = Bluefruit.begin();
  bool drawn = drawBootId();                  // either way: USB CRP needs it too
  if (!begun) {
    bleFail = drawn ? "# BLE failed to start (SoftDevice RAM?), USB only"
                    : "# BLE failed to start (SoftDevice RAM?), boot_id RNG failed too: USB text only";
    return false;
  }
  if (!drawn) {
    bleFail = "# boot_id RNG failed, not advertising (USB text only)";
    return false;
  }
  // The pod's own address (bleIdentity), before anything advertises. Without it a phone would
  // see the factory address and its cached PodLink v1 table: no advertising then.
  if (!Bluefruit.setAddr(&bleAddr)) {
    bleFail = "# ble address not set, not advertising";
    return false;
  }

  Bluefruit.setTxPower(4);                    // before Advertising.start(), which applies it
  Bluefruit.setName(bleName);                 // after begin() (it resets the name), before addName()
  Bluefruit.setEventCallback(bleEventCb);
  Bluefruit.Periph.setConnInterval(24, 40);   // the preferred parameters a phone can read (PPCP)
  Bluefruit.Periph.setConnSupervisionTimeout(400);   // agree with the request at PUSH on + 2 s

  // The GATT table, frozen from this build on (docs/ble.md 2.4): one service, RX and TX. Set
  // every length: Bluefruit's default is 20 B and it cuts anything longer without a word.
  crpService.begin();
  chrRx.setProperties(CHR_PROPS_WRITE | CHR_PROPS_WRITE_WO_RESP);
  chrRx.setPermission(SECMODE_NO_ACCESS, SECMODE_OPEN);
  chrRx.setMaxLen(244);
  chrRx.setWriteCallback(bleRxCb, false);
  chrRx.begin();
  chrTx.setProperties(CHR_PROPS_NOTIFY);
  chrTx.setPermission(SECMODE_OPEN, SECMODE_NO_ACCESS);   // read perm = the CCCD's write perm
  chrTx.setMaxLen(244);
  chrTx.setCccdWriteCallback(bleCccdCb, false);
  chrTx.begin();
  txHandle = chrTx.handles().value_handle;

  // All advertising data before the first start(): Bluefruit freezes the lengths there. Flags
  // (3 B), the 16-bit service UUID (4 B) and the whole name (13 B): 20 of 31 B, no scan response.
  Bluefruit.Advertising.addFlags(BLE_GAP_ADV_FLAGS_LE_ONLY_GENERAL_DISC_MODE);
  bool fits = Bluefruit.Advertising.addService(crpService);
  fits = Bluefruit.Advertising.addName() && fits;   // false: cut to a short name
  bleAdvLen = Bluefruit.Advertising.count();
  if (!fits) {
    bleFail = "# ble advertising data doesn't fit, not advertising";
    return false;
  }
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
  Serial.print(" (static random), advertising ");
  Serial.print(bleAdvLen);
  Serial.print(" of 31 B, boot_id 0x");
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

// ---- the sleep prologue's Bluetooth parts (the prologue itself: link.ino sleepPrologue) ----
// Its start: no restart on disconnect, and advertising off, so no new phone connects while the
// IMU is set up for sleep.
void bleSleepStart() {
  if (!bleOk) return;
  Bluefruit.Advertising.restartOnDisconnect(false);
  Bluefruit.Advertising.stop();
}

// Its end, once the going_to_sleep STATUS is out (or given up on): a deliberate disconnect, so the
// phone sees 0x13 (remote user terminated) at once instead of after the 4 s supervision timeout,
// waiting at most 500 ms. Then the # line.
void bleSleepEnd(bool wasStreaming, uint32_t cursorAtStart, uint32_t sentTo, uint32_t finalHead, uint32_t tDrain,
                 const char* statusWas) {
  if (!bleOk) return;
  uint32_t t2 = millis();
  bool hadLink = bleUp;
  if (hadLink) {
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

// ---- every loop() pass: bleLink's CRP pass (docs/ble.md 5, podcrp.h) ----
void bleTick(uint32_t now, bool powerChanged) {
  if (!bleOk) return;
  bleLinkEvents(now);

  if (bleLink.up) {
    crpIn(bleLink, now);                      // the spool, then HELLO, READs, WRITEs (CTRL: handleCtrl)
    linkSetup(now);                           // the link's own work
    if (linkEvent) {
      linkEvent = false;
      uint16_t m = evMtu;                     // one read, so both agree (a newer one sets linkEvent again)
      bleLink.mtu = m;                        // n_max changes here, with its STATUS queued first
      podcrp_set_mtu(&crpBle, m);             // and what a packet may be (MTU - 3)
      printLink();
      queueStatus(bleLink, 0, head);          // n_per_packet / interval changed
    }
    bool pushNow = podcrp_pushes_on(&crpBle);
    if (pushNow && !pushWasOn) pushOnMs = now;    // the conn param update counts from here
    pushWasOn = pushNow;
    linkTick(bleLink, now, powerChanged);     // power and 1 s STATUS, the drain's cap, a send pass
    crpOut(bleLink, now);                     // the spool again, then the NOP after 1 s of nothing
  }

  if (usbOpen && now - lastBleLineMs >= 30000) {
    lastBleLineMs = now;
    msg.print("# ble ");
    msg.print(bleLink.up ? "connected" : (Bluefruit.Advertising.isRunning() ? "advertising" : "NOT advertising"));
    msg.print(podcrp_in_session(&crpBle) ? ", crp session" : "");
    msg.print(bleLink.streaming ? ", streaming" : "");
    msg.print(", frames ");
    msg.print(bleLink.framesSent);
    msg.print(", lost ");
    msg.print(bleLink.lost);
    msg.print(", hold ");
    msg.print(sessionHold ? "on" : "off");
    msg.print(", crp tx drops ");
    msg.print(crpBle.tx_drops);
    msg.print(", rx drops ");
    msg.println(crpBle.rx_drops);
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
    msg.print(", lines dropped ");
    msg.println(msg.dropped);
    msg.send();
    maxLatency = 0;
  }
}
