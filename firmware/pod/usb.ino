// usb.ino: the pod's USB port, in text mode or CRP mode (docs/ble.md 2.5, D45).
//
// Text mode, from boot: the CSV (pod.ino usbDrain), the '#' lines and the bench characters, as
// before D45. It stays the independent truth for usb_clock.py, compare_ble_usb.py and
// check_soak.py, which check the frame path and so must not share it.
// CRP mode: the bytes 0A 00 01 0B (a level 1 HELLO: length 10, type 0x01, version 0x0B) switch
// the port. From there on crpUsb (podcrp.c) deframes what comes in and frames what goes out
// ([len u16][packet], level 1), the CSV stops, '#' lines go out as LOG packets (module "pod"), and
// usbLink runs the same stream as Bluetooth (link.ino) with its own cursor, n_max 16 and interval
// 0. It never touches the session hold.
// Back to text when the port closes, at BYE or END, after 3 s without receiving (podcrp's
// liveness), or when no HELLO completes within 3 s of the switch. Except at a port close, the
// CRP bytes already queued go out first (FLUSH: the port may hold the start of a packet). Then
// the CSV restarts live (csvCursor = head), and the bench characters are ignored for 3 s and
// after that count only as a 1-byte read, because CRP packets contain 'h' and 'w'.
// Without a boot_id (the RNG failed at boot) the switch is refused and the port stays text: an
// INFO with boot_id 0 must never go out (docs/ble.md 3).
// The make_fake_pod.py model of all this: Link.receive_bytes, to_crp, to_text and step (USB).
//
// Threads: loop() only. The port's state is loop()'s usbOpen (pod.ino).

uint32_t usbSwitchWin = 0xFFFFFFFF;          // the last 4 text bytes, newest lowest: finds a switch split across reads
bool usbPortWasOpen = false;
uint32_t usbCrpSinceMs = 0;                  // the switch: a session must start within 3 s of it
bool benchStrict = false;                    // after the first CRP session: only a 1-byte read is a bench character
uint32_t benchQuietUntilMs = 0;              // and none until this, after every way back to text
char benchReq = 0;                           // a BENCH write (CRP mode), run by loop() after the pass
bool usbSessionNote = false;                 // a session started: the # line goes out after the pass
uint8_t usbEndWhy = 0;                       // why CRP mode ended (PODCRP_END_*, or USB_END_NO_HELLO)
uint32_t usbSwitches = 0;                    // switches to CRP since boot (the # line)

const uint8_t USB_END_NO_HELLO = 0x10;       // usbEndWhy: no session within 3 s of the switch
static const uint8_t USB_SWITCH[4] = {0x0A, 0x00, 0x01, 0x0B};

// ---- crpUsb's side (link.ino sets these up in linksInit) ----
// crpUsb's send hook: as much as the port takes now, never waiting (a host that holds the port
// open without reading must not block loop()). podcrp keeps the rest in its 2 KB spool.
uint32_t usbCrpSend(podcrp_link_t* c, const uint8_t* p, uint32_t len) {
  (void)c;
  if (!usbOpen) return 0;
  int room = Serial.availableForWrite();
  if (room <= 0) return 0;
  uint32_t n = len < (uint32_t)room ? len : (uint32_t)room;
  return Serial.write(p, n);                  // fits the FIFO: no wait
}

uint16_t usbInterval() {
  return 0;                                   // USB has no connection interval (STATUS says 0)
}

// The BENCH register (USB only; podcrp answers anything but s, o, w, i and h INVALID_VALUE).
// Exactly what the text character does, but run by loop() after this pass (benchRun), so the
// WRITE's RESPONSE goes out first: s, o and w sleep, h hangs.
uint8_t crpBench(podcrp_link_t* c, uint8_t ch) {
  (void)c;
  if (sleepCmd || inPrologue) return CRP_STATUS_BUSY;   // as CTRL: from the SLEEP op on
  benchReq = (char)ch;
  return CRP_STATUS_OK;
}

// A '#' line in CRP mode (MsgLine.send): a LOG packet, module "pod", without the "# " and the line
// end. Lossy like a push (podcrp_log: needs a session and room plus 128 B).
bool usbLog(char* line, uint16_t n) {
  while (n && (line[n - 1] == '\n' || line[n - 1] == '\r')) n--;
  line[n] = 0;
  const char* m = line;
  if (n >= 2 && m[0] == '#' && m[1] == ' ') m += 2;
  return podcrp_log(&crpUsb, CRP_LOG_INFO, "pod", m);
}

// ---- the modes ----
void usbToCrp(uint32_t now) {
  podcrp_reset(&crpUsb);                      // a fresh engine and deframer (text mode's spool is always empty)
  usbMode = USB_CRP;
  usbLink.up = true;
  usbLink.nextStatusMs = now + 1000;          // the 1 s STATUS (pushed once the session's PUSH is 1)
  usbCrpSinceMs = now;
  usbSwitchWin = 0xFFFFFFFF;
  usbSwitches++;
}

// No boot_id (the RNG failed at boot, ble.ino drawBootId): an INFO would say boot_id 0, which never
// goes out (docs/ble.md 3), so the port stays in text mode. The PC speaks CRP all the same: its
// bytes stop counting as bench characters, as after a session.
void usbRefuseCrp(uint32_t now) {
  usbSwitchWin = 0xFFFFFFFF;
  benchStrict = true;
  benchQuietUntilMs = now + 3000;
  msg.println("# usb crp refused: no boot_id (the RNG failed at boot)");
  msg.send();
}

// CRP mode ends (the port stays open): the stream ends, and text resumes once the spool is out.
void usbLeaveCrp(uint8_t why) {
  endStream(usbLink);                         // already done when a session ended (crpSessionEnd)
  usbLink.up = false;
  usbMode = USB_FLUSH;
  usbEndWhy = why;
}

// Text again, with the spool out (or the port closed): the engine reset, the CSV live from head.
void usbTextResume(uint32_t now) {
  podcrp_reset(&crpUsb);
  usbMode = USB_TEXT;
  csvCursor = head;
  benchStrict = true;
  benchQuietUntilMs = now + 3000;
  usbSwitchWin = 0xFFFFFFFF;
  msg.print("# usb back to text: ");
  msg.println(usbEndWhy == PODCRP_END_BYE ? "BYE" : usbEndWhy == PODCRP_END_END ? "END"
            : usbEndWhy == PODCRP_END_SILENCE ? "3 s silent" : usbEndWhy == USB_END_NO_HELLO ? "no HELLO in 3 s"
            : "port closed");
  msg.send();
}

// crpSessionEnd (link.ino), from inside crpUsb's pass: BYE, END or silence. Flags only.
void usbSessionEnd(uint8_t why) {
  if (usbMode == USB_CRP) usbLeaveCrp(why);
}

// crpSessionStart (link.ino), from inside crpUsb's pass. A HELLO queued right behind a BYE or
// END starts a session while the spool flushes: still CRP then, the bytes never left its framing.
void usbSessionStart() {
  if (usbMode == USB_FLUSH) {
    uint32_t now = millis();
    usbMode = USB_CRP;
    usbLink.up = true;
    usbLink.nextStatusMs = now + 1000;
    usbCrpSinceMs = now;
  }
  usbSessionNote = true;
}

// The port's open and close edges. A close in CRP mode (or flushing) is the way back to text at
// once: nothing can go out any more.
void usbPortEvents(uint32_t now) {
  if (usbOpen == usbPortWasOpen) return;
  usbPortWasOpen = usbOpen;
  usbSwitchWin = 0xFFFFFFFF;                  // a new port open starts in text, with nothing pending
  if (!usbOpen && usbMode != USB_TEXT) {
    endStream(usbLink);                       // podcrp_reset calls no hook
    usbLink.up = false;
    usbEndWhy = 0;
    usbTextResume(now);                       // its # line is dropped: the port is closed
  }
}

// ---- every loop() pass, at the top (pod.ino) ----
// What the PC sent, as one read burst: in text mode the bench characters and the switch, in CRP
// mode bytes for crpUsb's deframer (its pass processes them). While flushing nothing is read: the
// bytes wait in the port until the text resumes.
void usbRead(uint32_t now) {
  usbPortEvents(now);
  if (!usbOpen || usbMode == USB_FLUSH) return;
  int avail = Serial.available();
  if (avail <= 0) return;
  bool lone = avail == 1;                     // the burst is a single byte: a bench character can be one
  uint8_t buf[64];
  while (avail > 0) {
    int n = (int)Serial.read(buf, avail < (int)sizeof buf ? avail : (int)sizeof buf);
    if (n <= 0) break;
    avail -= n;
    if (usbMode == USB_CRP) {
      podcrp_rx_bytes(&crpUsb, buf, n);
      continue;
    }
    for (int i = 0; i < n; i++) {
      usbSwitchWin = (usbSwitchWin << 8) | buf[i];
      if (usbSwitchWin == 0x0A00010B) {      // a level 1 HELLO: CRP from its first byte on
        if (!bootId) {                        // the RNG failed at boot: no INFO may go out
          usbRefuseCrp(now);
          continue;
        }
        usbToCrp(now);
        podcrp_rx_bytes(&crpUsb, USB_SWITCH, 4);
        podcrp_rx_bytes(&crpUsb, buf + i + 1, n - i - 1);
        break;
      }
      if (benchStrict && (!lone || (int32_t)(now - benchQuietUntilMs) < 0)) continue;
      benchChar((char)buf[i]);
    }
  }
}

// A bench command (pod.ino, top), from a text character or the BENCH register.
void benchChar(char c) {
  if (c == 's') goToSleep(true, "command s", SLEEP_COMMAND);
  if (c == 'o') goToSleep(false, "command o", SLEEP_COMMAND);
  if (c == 'w') goToSleep(true, "command w (dry run)", SLEEP_COMMAND, true);
  if (c == 'i') imuStopReq = true;            // the sampler stops the IMU (it owns the bus)
  if (c == 'h') {                             // bench: hang; the watchdog resets in 8 s
    msg.println("# hanging loop() on purpose: the watchdog resets in 8 s");
    msg.send();
    msgFlush();                               // loop() won't return to send it
    for (;;) {}
  }
}

// From loop(), after the links' pass: a BENCH write, its RESPONSE already out.
void benchRun() {
  if (!benchReq) return;
  char c = benchReq;
  benchReq = 0;
  benchChar(c);
}

// The '#' lines out before something that never returns to loop() (sleep, a hang). In CRP mode
// they wait in crpUsb's spool, which only a pass drains: at most 100 ms of draining here.
void msgFlush() {
  uint32_t t0 = millis();
  while (usbMode != USB_TEXT && usbOpen && !podcrp_spool_empty(&crpUsb) && millis() - t0 < 100) {
    podcrp_spool_drain(&crpUsb);
    Serial.flush();
    delay(1);
  }
  Serial.flush();
}

// ---- every loop() pass, after Bluetooth (pod.ino podLinkTick): crpUsb's CRP pass ----
void usbTick(uint32_t now, bool powerChanged) {
  if (usbMode == USB_CRP) {
    crpIn(usbLink, now);                      // the spool, then the PC's packets (a BYE or END: back to text)
    if (usbMode == USB_CRP) linkTick(usbLink, now, powerChanged);   // power and 1 s STATUS, the drain, a send pass
    crpOut(usbLink, now);                     // the spool again, the NOP, liveness (3 s silent: back to text)
    if (usbMode == USB_CRP && !podcrp_in_session(&crpUsb) && now - usbCrpSinceMs >= 3000)
      usbLeaveCrp(USB_END_NO_HELLO);
  }
  if (usbMode == USB_FLUSH) {                 // back to text once the CRP bytes are out
    podcrp_spool_drain(&crpUsb);
    if (podcrp_spool_empty(&crpUsb)) usbTextResume(now);
  }
  if (usbSessionNote) {
    usbSessionNote = false;
    msg.print("# usb crp session, switch ");
    msg.println(usbSwitches);
    msg.send();
  }
}
