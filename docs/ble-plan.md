# BLE link v1 (PodLink): implementation plan

**Status:** this is a read-only design. Nothing was built, flashed or run. The two judges picked different winners. This plan uses **PodLink v1** for the protocol and firmware architecture (judge 1's pick) and builds it in the **MVP-first order** (judge 2's pick), so real pod samples reach the app at step 6. It applies every must_fix item from both judges and the grafts listed for them.

## Progress

- **Step 1 done** (2c8a779): docs/ble.md, D27, android.md A4.
- **Step 2 done** (2026-10-20): `analysis/podlink.py` (reference decoder) and
  `analysis/make_fake_pod.py` (pod simulator, link, 29 scenarios, exact index accounting against
  the simulator's truth, replay from the log, vectors `data/fake_pod_*.csv`). Checked by an
  independent decoder written from the spec alone (matched 16/17 scenarios bit for bit; the
  difference was a spec gap, fixed), a 34-finding review, and a mutation test (planted decoder
  bugs must each fail a scenario). Changes against the plan below: the stop design (a timeout
  keeps the stream open; `finish()` records the lower-bound tail), `command()` refuses while
  ignoring, the rebase bound is the STATUS latency spread (+12 / +20 ms measured), not 1 ms.
- **Step 3 done** (2026-10-20): `POD`, `decodeFrame` and `PodStream` in `webapp/sources.js`, a
  line-for-line port; `webapp/test/pod.mjs` replays all 31 scenarios' vectors and requires the
  same samples (bit for bit), gaps, counters, faults, events, command bytes and stop timings.
  31/31 on the first run; parity.mjs still all PASS. JS mutation test: 28/30 planted bugs
  caught, the other 2 provably equivalent (no input can tell them apart); 3 scenarios were
  added to the bench to catch bugs that only the Python-side checks saw before.
- **Step 4 done** (2026-10-23): `firmware/pod/podlink.h` (constants, UUIDs, the DATA / STATUS /
  INFO packers), the 8192-sample ring in `pod.ino` (raw counts plus the extended RTC2 tick,
  one `rtcExtend()` for the CSV and the frames), the USB packer behind `PKT_OVER_USB` (now
  false), `podlink.py --usb` and `dt_stats.py`. On the board (COM14):
  - 600 s: 126,253 CSV samples against 7,890 DATA frames, every one matched once, worst
    |da| 0.00005 m/s^2 (print precision), index and pkt_seq continuous, STATUS / INFO ok, one
    RTC2 wrap crossed, 210.42 Hz, 0 gaps.
  - `--stall=20` (60 s, bench command `i` stops the IMU): one hole, excused by the next frame's
    IMU_RESTART, STATUS and frames kept going during the outage, 0 other gaps.
  - **dt std is 0.052 to 0.056 ms, packer on or off** (120 s each): over the 0.05 target, under
    the 0.1 fail line. It's the polling floor (one I2C status read is ~0.1 ms), not the packer;
    the step 8 data-ready interrupt is the fix.
  - Review (7 findings, all fixed): `lost_overflow` now counts BLE loss only (`lostBle`), the
    link and STATUS keep running on the IMU-miss path, the restart table has no duplicates,
    `batt_low` only on battery power (spec amended).
  - **Gotcha:** the first `i` cut the IMU's power, and the board hung (port up, no output):
    a powered-down IMU most likely holds the I2C bus and Wire waits forever. Nothing in the
    pod notices that today, so step 8 should add the watchdog (WDT) as well.
- **Step 5 done** (2026-10-23): `firmware/pod/ble.ino` (GATT table, advertising, the sender,
  the CTRL queue, STATUS, the link-setup timeline, the session hold) and `analysis/ble_check.py`
  (the bench check from the laptop with bleak; the chrome://bluetooth-internals list, scripted).
  - **Changed against this plan** (a 4-reader audit of the installed Bluefruit52Lib, then the
    bench): connect / disconnect come from the event callback (the Periph callbacks are
    deferred and can be dropped); loop() never holds a `BLEConnection*` (the BLE task deletes it
    at a disconnect) and calls the SoftDevice directly with values the event callback cached;
    notifications use `sd_ble_gatts_hvx` (Bluefruit's `notify()` waits up to 100 ms for a slot
    and races a disconnect); the CTRL and CCCD callbacks run inline in the BLE task.
  - **D28, two bench findings:** STATUS frames are notified on DATA (Windows reordered
    notifications across characteristics about 1 in 1500, and the decoder took a late STATUS
    for a reboot); the IMU poll lost about 1 sample every 2 s with the radio on, fixed with
    hardware-sequenced I2C (`imuRead()`), 0 since.
  - Link on Windows 11: MTU 247, data length 251, 45 ms interval, 2M PHY (Windows asks, the pod
    accepts), 16 samples per frame, 15.7 frames/s. Free heap after `begin()` 72.7 KB, so the
    ring stays at 8192.
  - `ble_check.py` all PASS: advertisement and UUID, the 5 characteristics and their properties,
    INFO / CAL / STATUS reads, STATUS on DATA at 1 Hz, boot_mismatch, STREAM_LIVE through
    PodStream (no gaps, no faults), a disconnect plus STREAM_FROM resume with nothing lost, STOP
    with head = last index + 1 and no DATA after it.
  - Soaks: advertising only, 600 s on USB: 126,256 samples, 210.42 Hz, 0 gaps, dt std
    0.042 ms (the single I2C status read halved the polling floor; step 4 had 0.052 to 0.056;
    this one ran on the build before the review fixes). Streaming for 600 s with the USB log
    running, on the final build: BLE 126,832 samples in 7,927 frames of 238 B (13.2/s), the
    decoder saw 0 gaps, 0 faults and 0 pkt_seq gaps, and the resume and the STOP were exact; USB
    117,838 samples, 0 gaps, dt std 0.054 ms (over the 0.05 target, under the 0.1 fail line:
    BLE-task preemption between the status read and the stamp; step 8 stamps in the ISR).
  - Review (4 lenses, a skeptic each, 16 findings confirmed), all fixed but one: `imuRead()` left
    the TWIM events set, so Wire's next transfer lost its bytes without an error (the sleep
    setup's gyro-off write; the `w` dry run now reads CTRL1_XL / CTRL2_G back: 0x10 / 0x0); the
    readable STATUS echoed event bits (it broke boot_mismatch recovery); the conn-param request
    fired at subscribe time (unsigned wrap); DATA could go out before the STATUS that reports a
    new n_max; ble_check checks that could pass vacuously. Left open, documented in ble.md 4:
    `lost_overflow` can undercount after a lap plus a disconnect with frames in flight.
  - Unchanged and known: a PC that holds the port open without reading stalls loop() (a
    blocking `Serial.print`); step 8's `usbDrain()` gates on `availableForWrite()`.
- **Step 6 done** (2026-10-23, laptop Chrome; the Pixel run is still open): `PodWebSource` in
  `webapp/sources.js`, the Connect pod button with the sticky source, the pod branch of
  startSession, the footer, `t0Wall` from `wallAt`, the `lastHiddenRun` reset, `barDebug.pod()` and
  the capture (`barDebug.podCapture`, `downloadPodCapture()`, in make_fake_pod.py's log format).
  - Tested by `webapp/test/pod_browser.mjs`: real Chrome over the DevTools protocol, the
    Bluetooth chooser answered through CDP's DeviceAccess domain. Two sessions in one tab, each:
    started in 2 s (5 s right after an End: see below), footer `210 Hz · 16/pkt · 45 ms · lost 0`,
    the stop handshake exact (last index + 1 = the stream_stopped head), 0 gaps, 0 faults, and
    **the browser's capture replayed through podlink.py gives the same samples, difference 0**
    (JS against the Python reference on real pod data).
  - Hand reps tracked as Deadlift: 5 reps of about 18 in read 17.5, 16.6, 16.9, 16.8 and 16.9 in,
    leveled, the same in reps.py on the saved set (`analysis/data/bar-deadlift-2026-10-23-1857-
    set01.csv` plus its capture); an independent integration of the raw samples gave 42.9 to
    45.1 cm. As "Phone test" the floors drift in the plot, by design (that profile levels nothing).
  - Found on the bench: Start right after End got a connection that dropped during discovery
    (Chrome finishing the old disconnect), so discovery retries up to 3 times.
  - Review (3 lenses, a skeptic each, 16 confirmed, all fixed): the 10 s start timeout left the
    setup running, and a late connect brought back a "zombie" source streaming into the app (now
    a `closed` check after every await, and a per-device owner so a zombie never disconnects a
    newer session); the controls stayed live during a start (history, replay, phone); a failed
    start stored an empty unfinished session and lost the resume offer; no STOP after a failed
    start (the pod kept its hold); a link drop didn't end the start or stop waits; smaller UI
    states. `webapp/test/pod_web.mjs` covers these paths with a fake Bluetooth device (5 PASS).
- **Step 7 done** (2026-10-24): `analysis/log_ble.py` (the receiver: PodStream, reconnect with
  STREAM_FROM, `--outage=S@T` drops, the STOP handshake; saves the samples, the capture as it
  happens and a link JSON) and `analysis/compare_ble_usb.py` (locks a BLE file onto a USB file of
  the same run by values and time steps, then requires every sample to match and every missing
  one to be a counted gap). **Roadmap 1.4's proof:**
  - 10 min, BLE and USB together: all **126,888 BLE samples matched their USB twins**, worst
    |da| 5e-5 m/s^2 (USB print precision), |dg| 0, |dt| 1 us; **0 missing**, 0 faults, 0
    pkt_seq gaps. The same capture replayed through the JS PodStream gives the same samples.
  - Outage matrix (3, 10, 30, 60 s link drops in one 8 min run): the first three lost nothing;
    the 60 s one lost exactly **5,122 samples**, one overflow gap, exactly where those samples
    sit in the USB timeline, and the pod's own lost_overflow counter rose by exactly 5,122. A
    45 s drop on the final logger: 2,141, same checks.
  - Not done as written: the drops were deliberate disconnects from the laptop, not the laptop's
    Bluetooth switched off (a system setting); the pod's ring and resume path is the same, a real
    radio-off adds the 4 s supervision timeout (a quick manual test). Chrome as a second
    receiver on the same link was skipped: one link carries one command stream and the app always
    commands, and Chrome = podlink.py is already proven by step 6's capture replay.
  - The comparer's own check: 10 planted faults caught (a dropped sample, values or a timestamp
    off, a phantom or missing gap, a wrong pod counter, a cut BLE tail, a USB log that died early
    or started late). Review (2 lenses, 10 confirmed, all fixed): tail loss and coverage weren't
    checked, the alignment could lock on too few samples, an exception lost the capture and the
    STOP, a link drop during STOP read as a stop timeout, a duplicate command after a reconnect,
    the pod counter mixed boots, same-minute reruns overwrote recordings.
  - Bluetooth headphones on the laptop starve its BLE scanning: disconnect them for these tests.

## 0. Corrections from the spec audit (2026-10-19)

`docs/ble.md` is the spec and wins wherever this plan differs. The audit (3 auditors plus
skeptics, 38 confirmed findings) changed the protocol details there; these are the
firmware-internal corrections to the sections below:

- **pkt_seq** counts DATA frames accepted by notify() only, not STATUS (section 2 DATA table
  and `sendNotify()` said "every successful notify").
- **STATUS order after STOP:** the stream_stopped STATUS comes after the drained DATA, never
  before (the STATUS trigger list said "before any data after a STREAM or STOP command").
- **Characteristic lengths:** `setFixedLen` 20 / 32 / 64 on STATUS / INFO / CAL and
  `setMaxLen(16)` on CTRL, or Bluefruit cuts them to its 20 B default. Write INFO, CAL and a
  valid STATUS before advertising.
- **UUIDs:** six separate `static const uint8_t[16]` arrays in podlink.h. BLEUuid keeps a
  pointer (BLEUuid.cpp:185-187), so patching one buffer at [12] or using a local array breaks
  the service and the advertisement.
- **boot_id:** wait for `sd_rand_application_bytes_available_get() >= 4` (at most 100 ms),
  require NRF_SUCCESS, redraw 0; on failure print `# boot_id RNG failed` and don't advertise.
- **Ring:** RING_N is a compile-time constant (a static array can't shrink at runtime). If the
  banner shows under ~16 KiB free heap, or `Bluefruit.begin()` fails after sd_ble_enable
  succeeded, rebuild with 4096. A failure inside sd_ble_enable ("App Ram Start ...") is
  SoftDevice RAM, which the ring size doesn't fix.
- **Two loss counters:** `lostBle` (feeds STATUS lost_overflow, each index counted once via a
  lostMark) and `lostUsb` (only the `# ring` line). bleCursor is checked only while streaming.
  After every copy, re-check `(head - cursor) <= RING_N`: the first frame after a clamp to lo
  is the most likely to be lapped, so the check is mandatory, not defensive.
- **IMU restarts:** the ring stores no flags, so keep `restartIdx[4]`, the first index after
  each recent restart. The packer ends a frame before such an index and sets IMU_RESTART on the
  frame starting there (live and when resending). The restart hole is 50 timed-out 20 ms waits
  plus imuStart's delays, about 1.1 s, not "50 samples".
- **RTC2 in step 8:** `rtcExtend()` takes no argument. Inside `taskENTER_CRITICAL()` it reads
  `raw24 = NRF_RTC2->COUNTER`, unwraps it against rtcLast (bumping the wrap count) and returns
  both `ext32` and `raw24`. Every caller uses it: the sampler on every pass (20 ms timeouts
  included, so a 512 s wrap is never missed while the IMU is down), STATUS ticks_now, and
  loop's 80 ms age check. The sampler derives a sample's tick from the ISR's captured
  `drdyTicks` as `ext32 - ((raw24 - drdyTicks) & 0xFFFFFF)`: a captured (older) value never
  goes into the wrap compare, which is what would add a false +512 s. Valid while drdyTicks is
  under 512 s old, which it always is. Step 8 item 6 below follows this. In steps 4-7 loop() is
  the only caller and the pod.ino form stays.
- **Sleep prologue:** `Advertising.restartOnDisconnect(false)`, `Bluefruit.disconnect(conn)`,
  then wait up to 500 ms for the disconnect-callback flag. No `Advertising.stop()` (it's
  already stopped while connected) and no fixed `delay(50)`. Wait up to 300 ms for zero in
  flight before disconnecting.
- **missed_drdy** is 0 in the polling firmware (steps 4-7). From step 8 it counts every IMU
  sample not stored: `+= n - 1` per sampler pass, +1 per late-read drop, +1 per failed burst
  read (IMU-restart holes excluded); `lateRead` stays as its own `# ring` diagnostic. The
  phone's sum of the time-hole n (ble.md section 7) over `imu` holes must then equal the
  missed_drdy delta.
- **MTU in the UI:** Web Bluetooth doesn't expose the MTU and no frame carries it, so the
  footer shows `MTU 247` for the app source only (the plugin's connect() returns it); the
  browser shows n_per_packet. Steps 6 / T8 read the MTU from the pod's `# link` line on USB.
- **`stopped` promise:** created just before the STOP write; it resolves on the next STATUS
  with stream_stopped (an event bit, set only in that one STATUS).
- **cmd_seq (after the second check):** STREAM_FROM / STREAM_LIVE carry a u8 cmd_seq right
  after the op (10 B / 4 B), and every DATA frame echoes it in byte 13 (was reserved). The
  decoder drops DATA of any older command and takes a STREAM_LIVE start index from the first
  frame of the new one, so no rule depends on guessing which STATUS answered a command.
- **STOP in the firmware:** release the session hold when STOP arrives, not after the drain.
  Cap the drain (n_max < 4 or DATA notifications off: answer at once; otherwise
  `1.5 s + backlog / (60 x n_max) s`), then send stream_stopped regardless. A disconnect or a
  STREAM command during the drain ends it without stream_stopped.
- **More step 2 scenarios** (on top of the 9): stale DATA from an older cmd_seq after a
  re-anchor; STREAM_LIVE whose first frame comes before its STATUS; STOP timeout and capped
  drain; a reboot while disconnected (lost-tail); a reboot without a new boot_id (defensive
  check); a boot_mismatch answer; malformed frames.
- **USB tick check:** `(ticks*15625//512) % 2**32 == t_us` (t_us is u32 and wraps every
  71.6 min), valid while the pod has been up under 36.4 h.

## 1. Decisions

1. **The pod owns the record and the radio only carries it.** Every IMU sample gets a 32-bit sample index and its own RTC2 tick when it is read. Samples wait in a 38 s RAM ring. After every connect or reconnect, the phone tells the pod where to resume: `STREAM_FROM(boot_id, idx)`. A dropout shorter than the ring loses nothing. A longer one loses an exact number of samples, which is counted and timed and never filled in. Why: roadmap 1.4 says "prove you aren't dropping samples", and reps.js would silently bridge a hole with its trapezoid integral.
2. **Raw int16 values and crystal ticks go over the air, and the phone scales them.** The scale factors come from the INFO characteristic and are exactly Seeed's `calcAccel`/`calcGyro`. BLE samples therefore equal the USB CSV to print rounding. Time is pure device time (D4, D25), and nothing assumes 208 Hz.
3. **A custom GATT service, frozen at the first BLE build:** DATA, CTRL, STATUS, INFO and a CAL stub. A notification carries 16 samples in 238 B at MTU 247, which is 13.2 notifications/s and 3.1 kB/s. Below MTU 73 the pod refuses to stream and says why. It does not degrade quietly.
4. **Link settings suited to a gym full of steel:** 1M PHY, +4 dBm, a 30-50 ms interval and a 4 s supervision timeout. The pod requests these explicitly after service discovery. The sender never blocks. The pod logs the values each phone actually grants, so they are measured, not assumed.
5. **The firmware moves to a data-ready interrupt and a top-priority sampler task that owns I2C (step 8),** but only after the thin path works. That decouples sampling from the radio. It also lets the CPU sleep, which is about 5 mA saved by estimate (not measured).
6. **The phone has one pure decoder plus a PodStream in `webapp/sources.js`** (CLAUDE.md rule 2), with `analysis/podlink.py` as its reference (P1).
   - Browsers use Web Bluetooth.
   - The app uses our own plain-Java Capacitor plugin, a dumb pipe with no new dependencies, safe on Java 25 (D13/D16).
   - `@capacitor-community/bluetooth-le` is rejected: it is Kotlin, needs a bundler (P3), sends one hex event per notification, and reconnects from JS.
   - A bleak logger is the roadmap 1.4 receiver.
7. **The algorithm doesn't change.** reps.js, sets.js and the D4 sample shape stay as they are, so the parity test stays valid. A real pod set is added to it at the end.
8. **D26 amendments:**
   - From a STREAM command until STOP, the idle limit is 30 min instead of 10, and the lid-down gesture is off. A dropout does not release the hold.
   - Sleep drains the ring, says why it is sleeping, then disconnects.
   - Pod data bypasses the phone calibration (D23) and stays raw until roadmap 1.3.

## 2. Firmware design (`firmware/pod/`)

### Files

| file | what |
|---|---|
| `pod.ino` | Settings, shared globals (ring, head, cursors, counters), setup, loop, power. The globals go at the top because Arduino only generates prototypes for functions, not for variables. |
| `ble.ino` (new tab) | GATT, sender, command queue, STATUS |
| `sampler.ino` (new tab, step 8) | DRDY ISR, sampler task, park handshake |
| `podlink.h` | UUIDs, frame layouts as packed structs, constants. Mirrors `docs/ble.md` byte for byte. |

The firmware uses only Bluefruit52Lib, which ships with core 1.1.13. Flash with `python firmware/flash.py pod` on this laptop: the pods were bench-tested here on 10-19 (L1 log). The rule about routing hardware work to T1 is for the coil bench, not the pod.

### GATT table

- Base UUID `9ba4xxxx-6de5-429c-b225-8134b9953e9d`, written lowercase (Web Bluetooth).
- Bluefruit takes the UUID as a little-endian byte array: `{0x9D,0x3E,0x95,0xB9,0x34,0x81,0x25,0xB2,0x9C,0x42,0xE5,0x6D,0x01,0x00,0xA4,0x9B}`. Byte [12] is the role.

| UUID | name | properties | size |
|---|---|---|---|
| 9ba40001 | service | in the advertisement | |
| 9ba40002 | DATA | Notify, `setMaxLen(244)`. Read `SECMODE_OPEN`, because Bluefruit copies the read permission to the CCCD's write permission (BLECharacteristic.cpp:296). Write `NO_ACCESS`. | up to 238 B |
| 9ba40003 | CTRL | write with response | up to 16 B |
| 9ba40004 | STATUS | read (notified STATUS frames go out on DATA, D28) | 20 B |
| 9ba40005 | INFO | read, written once in setup | 32 B |
| 9ba40006 | CAL | read; a stub until 1.3 (byte 0 = 0x30, byte 1 = 0 means "none") | 64 B |

- Freeze this table from step 5 on. Android, Chrome and Windows cache attribute tables. Bluefruit already has Service Changed on by default (bluefruit.cpp:159, so no `configServiceChanged` call), but without bonding that doesn't clear the phone's cache. That is why the CAL stub exists now.
- Name: `BarPod-XXXX`, from the low 16 bits of `NRF_FICR->DEVICEADDR[0]`. It matches the MAC suffix that Android and bluetooth-internals show.
- No pairing in v1.

Byte 0 of every frame is its type (DATA 0x01, STATUS 0x10, INFO 0x20, CAL 0x30), so every frame can go through one ordered decoder. All fields are little-endian.

### DATA frame, 14 + 14n bytes, n = 1..16

| offset | type | field |
|---|---|---|
| 0 | u8 | type 0x01 |
| 1 | u8 | n |
| 2 | u16 | pkt_seq: +1 per successful notify() since boot, wraps at 16 bits (the roadmap's per-packet counter) |
| 4 | u32 | first_idx: sample index of sample 0 |
| 8 | u32 | t0_ticks: RTC2 tick of sample 0, extended to 32 bits (32768 Hz, wraps after 36.4 h) |
| 12 | u8 | flags: b0 RESYNC (first packet after a STREAM command), b1 OVERFLOW, b2 IMU_RESTART, b3 FINAL |
| 13 | u8 | reserved 0 |
| 14 + 14k | u16 | dt_ticks from t0_ticks (sample 0 = 0) |
| +2 | i16 x3 | gx gy gz raw |
| +8 | i16 x3 | ax ay az raw (the same order as the 12-byte burst from OUTX_L_G) |

**Sizing:**
- `mtu_eff = min(conn->getMtu(), 247)` and `n_max = min(16, (mtu_eff - 3 - 14) / 14)`. MTU 247 gives 16 samples.
- If `n_max < 4` (MTU below 73), the pod streams nothing and sets STATUS `mtu_too_small`.
- Why the clamp: after a pod-initiated MTU exchange, Bluefruit stores the peer's `server_rx_mtu`, for example 517 (BLEConnection.cpp:353), while the real ATT MTU is 247. notify() then does not split anything (max_payload 514). But a packer sized from 517 would be cut silently to `setMaxLen(244)` (BLECharacteristic.cpp:715). This corrects the explanation in design 1.

**Phone-side scaling:**
- a = raw x 0.0023928226 m/s^2 (0.061 mg x 4 / 1000 x 9.80665, at +-8 g)
- g = raw x 0.035 deg/s (4.375 mdps x 8 / 1000, at +-1000 dps)
- t from ticks / 32768

### STATUS, 20 bytes

Sent every 1 s while connected; at once on a USB or charging change; before any data after a STREAM or STOP command; and before sleep. Battery mV is the cached 5 s reading, so there is no ADC read every second.

| offset | type | field |
|---|---|---|
| 0 | u8 | type 0x10 |
| 1 | u8 | flags: b0 USB, b1 charging, b2 batt_low (< 3.50 V), b3 streaming, b4 going_to_sleep, b5 stream_stopped, b6 mtu_too_small, b7 boot_mismatch |
| 2 | u16 | battery mV |
| 4 | u32 | ticks_now (extended RTC2 at send time) |
| 8 | u32 | head: samples produced since boot = the next index |
| 12 | u16 | lost_overflow since boot (saturating) |
| 14 | u16 | missed_drdy since boot (saturating) |
| 16 | u8 | sleep_reason: 0 none, 1 idle, 2 lid-down, 3 low battery, 4 command, 5 IMU lost |
| 17 | u8 | n_per_packet at the current MTU |
| 18 | u16 | connection interval, 1.25 ms units |

### INFO, 32 bytes

| offset | type | field |
|---|---|---|
| 0 | u8 | type 0x20 |
| 1 | u8 | proto 1 |
| 2 | u16 | fw_build |
| 4 | u32 | boot_id (from `sd_rand_application_vector_get` after `begin()`) |
| 8 | u32 | serial (`NRF_FICR->DEVICEID[0]`) |
| 12 | f32 | accel_lsb 0.0023928226 |
| 16 | f32 | gyro_lsb 0.035 |
| 20 | u16 | odr_nominal (208) |
| 22 | u16 | ring_n |
| 24 | u8 | wake reason: 0 power-on/upload, 1 motion, 2 USB, 3 reset pin (from `readResetReason()`) |
| 25 | u8 | reserved |
| 26 | u16 | tick_hz (32768) |
| 28 | u32 | reserved |

### CTRL ops (byte 0 = op)

The write callback only does `xQueueSend` to a 4-deep queue. loop() acts on it.

| op | args | action |
|---|---|---|
| 0x01 STREAM_FROM | u32 boot_id, u32 from_idx | If boot_id isn't ours: STATUS with boot_mismatch, and no stream. Otherwise `lo = (head > RING_N) ? head - RING_N : 0`, `cursor = clamp(from_idx, lo, head)`, OVERFLOW set if it was clamped up. Then STATUS, then DATA with RESYNC on the first packet. Sets the session hold. |
| 0x02 STREAM_LIVE | u16 back | `cursor = head - min(back, head, RING_N)`. A fresh session sends back = 600 (2.8 s of still lead-in from before Start). Sets the session hold. |
| 0x03 STOP | | Send everything up to the current head (last packet FINAL), then STATUS with stream_stopped and that head, then stop streaming. Releases the session hold. |
| 0x10 SLEEP | u8 mode | 0 = motion wake, 1 = IMU off. Goes through the sleep prologue. |

The `lo` expression is the fix for the clamp underflow: `head - RING_N` in u32 wraps during the first 38.6 s after every boot, which is exactly when `STREAM_FROM(new boot, 0)` wants the pickup.

### Ring and sender

**The ring:**
- `struct Sample { u32 ticks; i16 g[3]; i16 a[3]; }` is 16 B. `RING_N = 8192`: a 128 KiB static array, 38.6 s at 212 Hz. App RAM is 0x20006000 to 0x20040000 (232 KiB).
- At boot the pod prints the free heap. Fall back to 4096 (19 s) if the headroom is under about 16 KiB or `Bluefruit.begin()` fails.
- `head` has a single writer. It is written after the slot, with a compiler barrier.
- Every ring comparison is written as `(head - cursor) > RING_N` in u32.
- There are two cursors, `bleCursor` and `usbCursor` (step 8), and each handles its own overflow: `lost += (head - cursor) - RING_N; cursor = head - RING_N;` and set the flag.
- Copies are lock-free: copy, then re-check `(head - cursor) <= RING_N`. If the writer lapped the copy, it counts as an overflow.

**`bleSend()` in loop():**
- It runs only while streaming, connected, DATA notify enabled and `n_max >= 4`.
- It sends a packet when any of these holds: pending >= n_max; the oldest pending sample is 80 ms old (2621 ticks); the next dt would pass 60000 ticks (IMU stall); or a STOP or sleep drain is under way. When behind, it sends up to 4 packets per pass.
- **It never blocks.**
  - Every notify, DATA and STATUS alike, goes through one `sendNotify()`. It only calls `notify()` when `inFlight = (int32_t)(hvnQueued - hvnDone)` is below 3 (a negative value counts as 0).
  - `hvnQueued` is written only by loop(). `hvnDone` is written only by the `Bluefruit.setEventCallback` handler, which adds `hvn_tx_complete.count` on `BLE_GATTS_EVT_HVN_TX_COMPLETE`. Two monotonic counters with one writer each means no lost-update race.
  - Bluefruit releases its own semaphore before our callback runs (bluefruit.cpp:934), so our count is conservative. The 100 ms `getHvnPacket` wait (BLEConnection.cpp:232-235) never runs inside the sampling loop.
- `bleCursor` and `pkt_seq` advance only when `notify()` returns true.
- On disconnect (a flag, handled in loop): streaming stops and `hvnQueued = hvnDone`. The pod doesn't track what arrived; the phone says where to resume.
- **Capacity:** `configPrphBandwidth(BANDWIDTH_MAX)` gives MTU 247, a 125 ms event length and 3 HVN slots. At a 30-50 ms interval that is 60-100 packets/s against 13.2 needed, so a full 38 s backlog drains in about 6-11 s.

### Advertising and connection

**Before `begin()`:**
- `autoConnLed(false)`. The core drives the blue LED with LED_STATE_ON=1, but the XIAO's LEDs are active LOW.
- `configPrphBandwidth(BANDWIDTH_MAX)`.
- Check the result of `begin()` and print it: `# ble begin ok`, or `# BLE failed to start (SoftDevice RAM?)`, after which USB keeps working.

**After `begin()`:** read boot_id, `setTxPower(4)`, set the name, set the connect, disconnect and event callbacks, start the service and characteristics, write INFO and CAL.

**Advertising:** flags + the 128-bit service UUID (21 of 31 B), the name in the scan response, `restartOnDisconnect(true)`, `setInterval(32, 244)` (20 ms fast, 152.5 ms slow), `setFastTimeout(30)`, `start(0)`.

**PHY:** stay on 1M and don't request 2M. The data rate is tiny, and 1M has about 3 dB better sensitivity. Requests from the phone are still accepted.

**Callbacks** (connect, disconnect, CTRL write, events) only copy values, set flags or queue bytes. No Serial, no I2C, no cursor changes. They run in Bluefruit's tasks, so a print would splice into a CSV line, and log_serial.py would drop that line as a false gap.

**Link setup timeline, driven from loop()** and spaced out so the SoftDevice calls don't return BUSY (on BUSY, retry once after 0.5 s):
1. Connect + 0.2 s: `requestDataLengthUpdate()`.
2. Connect + 1 s: if `getMtu()` is still 23, `requestMtuExchange(247)`. This covers Chrome on Android, which may never ask; Android native and Windows ask by themselves.
3. DATA CCCD on + 2 s (after the phone's fast discovery interval): `sd_ble_gap_conn_param_update` with a named `ble_gap_conn_params_t p = {24, 40, 0, 400}` (30-50 ms, latency 0, 4 s).
   - Not `Periph.setConnInterval`: that only sets PPCP, which phones ignore (BLEPeriph.cpp).
   - Not `requestConnectionParameter`: it forces min = max (BLEConnection.cpp:183-195).
   - Not a compound literal `&(ble_gap_conn_params_t){...}`, which g++ rejects as taking the address of a temporary.
4. On every CONN_PARAM_UPDATE, PHY_UPDATE, DATA_LENGTH_UPDATE and MTU event, loop() prints `# link mtu 247 interval 30.0 ms latency 0 timeout 4000 ms phy 1M dl 251` and STATUS is refreshed.
5. The pod streams nothing until it receives a STREAM command.

### Sampling

**Steps 4-7:** today's busy poll stays (pod.ino:332-357). The sample is stamped at the status read (D25), burst-read, pushed to the ring and printed as a CSV line as today. The sender never blocks, so BLE can't stall the poll.

**Step 8: data-ready interrupt and sampler task.**
- **Register setup.** `imuStart()` writes `INT1_CTRL 0x0D = 0x02` (INT1_DRDY_G) and `DRDY_PULSE_CFG_G 0x0B` bit 7 = 1 (pulsed, about 75 us). Latched DRDY with an edge interrupt deadlocks after one late read. Both callers re-apply this, because `imuStart()` power-cycles the IMU: setup and the restart after 50 misses.
  - Confirm 0x0B in the LSM6DS3TR-C datasheet first. The Seeed header calls 0x0B ORIENT_CFG_G, which is the plain LSM6DS3's register.
- **ISR.** `attachInterrupt(PIN_LSM6DS3TR_C_INT1, onDrdy, RISING)`, not deferred. GPIOTE runs at priority 3, where FromISR calls are allowed. The ISR does only: `drdyTicks = NRF_RTC2->COUNTER; drdyCount++; vTaskNotifyGiveFromISR(sampler); portYIELD_FROM_ISR`.
- **Sampler task at `TASK_PRIO_HIGHEST` (4).** That is above BLE/USB (3) and Callback/Timer (2), with time slicing off. Each pass:
  1. `n = ulTaskNotifyTake(pdTRUE, ms2tick(20))`.
     - n == 0 runs the existing miss logic: 50 misses in a row restart the IMU and re-apply the routing, with the IMU_RESTART flag set. On battery, a failed restart sets sleepRequest.
     - n > 1 means `missed_drdy += n - 1`.
  2. Snapshot `drdyCount` and `drdyTicks`.
  3. Check STATUS_REG bits 0x03, polling up to 1 ms; `xlLag++` if accel was late.
  4. Burst-read 12 B.
  5. If `drdyCount` changed during the read, the burst may hold the next sample. Drop it and count `lateRead++`; the pending notification reads the next sample at once. That way a late read can never pair the next sample's data with this sample's stamp.
  6. `rtcExtend()` unwraps the 24-bit counter. It is the only place that does this; STATUS's ticks_now uses the same helper.
  7. Write `ring[head & (RING_N-1)]`, then `head++`. The motion and lid-down math stays as today. It sets `sleepRequest` and never calls `goToSleep()` itself.
- **The sampler owns all Wire1 I2C** (Wire busy-waits and has no mutex). Anything else that needs I2C calls `samplerPark()`:
  1. Set `parkReq`, notify the sampler, and wait up to 500 ms for `parked`.
  2. The sampler checks `parkReq` only at the top of its loop: never inside a transfer, and never inside `imuStart()`'s `delay(20)`/`delay(40)`.
  3. Then it detaches INT1, sets `parked` and blocks for good. Sleep and `w` both end in a reset, so it never needs to resume.
  This replaces "vTaskSuspend anywhere", which could freeze the sampler between `imu.begin()`'s transactions.
- **loop():** `handleCommand(); housekeeping(); usbDrain(); bleSend(); delay(5);`. The delay lets tickless idle sleep the CPU.
- **USB:** `usbDrain()` prints the unchanged 7-column CSV from `usbCursor`, only while Serial is open, and starts live when the port opens. A host that holds the port open without reading now stalls only USB (counted). It never stalls sampling or BLE.

### Sleep interaction (amends D26)

- **Session hold.** It is set by STREAM_FROM or STREAM_LIVE and cleared by STOP or a reboot. A dropout keeps it.
  - While the hold is on, the idle limit is `IDLE_SLEEP_STREAMING_S = 1800`, and the lid-down gesture needs `!Serial && !hold`, for the same reason D26 blocks it with a port open.
  - A forgotten session still ends after 30 min still. The 3.30 V cutoff is unchanged.
  - Both guards land in step 5.
- **`goToSleep()` prologue (step 9):**
  1. `samplerPark()` (from step 8 on).
  2. If streaming, drain `bleSend()` to head for up to 1.5 s; the last packet is FINAL.
  3. Send STATUS with going_to_sleep, sleep_reason and the final head, and wait up to 300 ms for its TX complete.
  4. `Bluefruit.disconnect(connHdl)` (reason 0x13, so the phone sees a deliberate disconnect instead of a 4 s timeout), then `Advertising.stop()` and `delay(50)`.
  5. Over I2C: `INT1_CTRL = 0`, `DRDY_PULSE_CFG_G = 0`, then the existing wake-up setup (pod.ino:164-180), flash 0xB9 and `systemOff`. The existing code already uses `sd_power_system_off` with the SoftDevice on (pod.ino:215-221).
  GPIOTE is detached from P0.11 before SENSE is armed there. That is the same class of problem as the open instant re-wake bug.
- `LOW_BATT_WARN_V = 3.50` sets STATUS batt_low.
- **Wake is a reset:** new boot_id, index 0 and ticks 0. The ring fills from the first sample, so `STREAM_FROM(new boot, 0)` delivers the pickup itself.

### setup() order

1. P0.14 LOW and the charge pins, as today.
2. LEDs, `readResetReason()`.
3. Bluefruit config and `begin()`, boot_id, INFO and CAL.
4. `imuStart()`, plus the DRDY routing from step 8.
5. Start RTC2 after `begin()`, so the SoftDevice taking over the LF clock can't glitch it.
6. Sampler task and `attachInterrupt` (step 8).
7. Start advertising.
8. `Serial.begin` and the 2 s USB wait (USB power only).
9. Banners, including ring size and free heap.

New `#` lines, all printed from loop(): ble connected / disconnected with the reason, link, sleeping with the reason, and every 30 s `# ring head H lost L missed M late X xllag Y sent S failed F heap B`.

## 3. Phone design

### PodStream in `webapp/sources.js` (reference: `analysis/podlink.py`)

**Where it lives.** Pure exports with no DOM at module top, so Node can import them. It stays inside sources.js (rule 2). `podlink.py` (stdlib only) has the same names. `analysis/make_fake_pod.py` writes the test vectors to `analysis/data/fake_pod_frames.csv` and `fake_pod_expected.csv`; the existing `fake_*.csv` rule already gitignores them.

**API.** Exports: the POD constants, `decodeFrame(dv)`, and `class PodStream(onSample, {onEvent})`. Every received frame of any type goes through `stream.frame(dv, arrivalMs)` in arrival order. `onSample` is called synchronously, in index order, with D4 samples.

**Scaling.** From INFO, with the constants as a fallback. Axes are the pod's own. No phone calibration is applied.

**Epochs, one per boot_id.**
- INFO with the same boot_id is a reconnect, and the epoch continues.
- A new boot_id starts a new epoch. So does a tick drop larger than the ring depth within one boot (a defensive check).
- Within a boot, an index below `nextIdx` is a duplicate and is dropped, never treated as a restart. This fixes the duplicate and mis-timing bug that design 1 had between its reconnect (G2) and backlog (G4) steps.

**Ticks.** `ticks = (t0_ticks + dt_k) >>> 0`, then `ticks64 = ref64 + ((ticks - ref32) | 0)`. This handles the u32 wrap and backlog samples older than the last STATUS.

**Clock mapping.**
- `wallBase` = the minimum over the last 32 STATUS frames of `arrivalMs - ticks64(ticks_now) / 32.768`. The minimum picks the lowest-latency frame.
- `tOffset` is frozen at the epoch's first emitted sample.
  - First epoch: t starts at 0.
  - Later epochs: `prev.tOffset + (wallBase_e - wallBase_prev) / 1000`.
- If an epoch would start at or before `lastT + 1 ms`, the whole epoch moves up and a `clock-clamped` event is raised. A new epoch's DATA waits for its first STATUS; the pod always sends STATUS first.

**Arrival clock.**
- Browser: `Date.now()`. It keeps counting through phone suspend, unlike `performance.now()`. An NTP step only matters at the moment of a rebase, and the clamp catches it.
- App: the plugin's `SystemClock.elapsedRealtime()`, which is monotonic and counts through suspend.
- `wallAt(t)` maps t to wall-clock ms. app.js uses it for `session.t0Wall`, so the 2.8 s lead-in doesn't shift set times.

**Gaps and sequence.**
- An index jump records a gap with a reason:
  - `overflow` (OVERFLOW flag set);
  - `resync` (a RESYNC packet started later than asked);
  - `unexplained`, which means a protocol bug. Tests require 0.
- A time hole with no index jump (dt over 3 periods) is an `imu` or `imu-restart` gap, checked against missed_drdy.
- pkt_seq must be prev + 1 unless RESYNC is set.
- Within an epoch, a sample with a non-increasing t is dropped and counted. It is never clamped and emitted.
- Holes are never filled; SessionTracker sees them as quiet blocks, as today.
- The footer gap count uses log_serial's rule: dt > 1.5 x the median dt per 2 s window.

**`command()`.** Returns `STREAM_LIVE(600)` for a fresh session, `STREAM_FROM(boot, nextIdx)` after a reconnect, and `STREAM_FROM(newBoot, 0)` after a reboot.

**Events and stats.**
- Events:
  - `link` (connecting, streaming, reconnecting, stopped)
  - `gap`
  - `epoch`
  - `sleep {reason}`
  - `battery`
  - `fault`: MTU too small, boot mismatch, unexplained gap, no data in 10 s
- A `stopped` promise resolves on the STATUS with stream_stopped.
- Stats: received, lost by reason, gaps, dupes, pktSeqGaps, reconnects, epochs, MTU, interval, n_per_packet.

### Browser: `PodWebSource` (desktop Chrome, Android Chrome; GitHub Pages or http://localhost)

**Picking the pod.**
- A "Connect pod" click calls `navigator.bluetooth.requestDevice({filters: [{services: [POD.SERVICE]}]})` inside the gesture.
- It is separate from Start because the resume path awaits `restoreSets` before `start()`, which can outlive the gesture.
- The device is kept at module scope. A page reload needs one more tap, because `getDevices()` is still behind a flag.
- Without `navigator.bluetooth`: "This browser has no Web Bluetooth. Use Chrome (desktop or Android) or the app."

**`setup()`:**
1. `gatt.connect()`, then get the service and its 5 characteristics.
2. Feed `await info.readValue()`, then `await status.readValue()`, into the stream.
3. Start notifications on DATA only (it carries the STATUS frames too, D28), with a handler calling `stream.frame(e.target.value, Date.now())`. Read the value synchronously in the handler.
4. `writeValueWithResponse(stream.command())`.

`start()` resolves on the first DATA frame. After 10 s it fails with the reason from STATUS when there is one.

**Reconnect (step 10).** On `gattserverdisconnected`, run `setup()` again with backoff 0.5, 1, 2, 4 s, then every 5 s. It uses the same PodStream, so the clock and index continue. This only works while the page is visible.

**`stop()`:**
1. Write STOP and await `stopped` (1.5 s).
2. Record a `stop-tail` gap if `nextIdx != head`.
3. `stopNotifications`, then `gatt.disconnect()`.
This meets endSession's "flushed before resolve" contract.

**Debug option `{capture: true}`.** Keeps raw frames and decoded samples; `barDebug.downloadPodCapture()` saves them.

### Android app: `PodLinkPlugin.java` + `PodNativeSource`

**Plugin shape.**
- `@CapacitorPlugin(name = "PodLink", permissions = @Permission(alias = "bluetooth", strings = {BLUETOOTH_SCAN, BLUETOOTH_CONNECT}))`, registered in MainActivity before `super.onCreate`.
- Android 12+ (API 31) only in v1; the Pixel is API 36. Older versions get a clear rejection.
- **Java is a dumb pipe and never parses a frame.** The JS PodStream is the only decoder besides Python, so no third decoder can drift (judge 1 must_fix).

**`connect()`:**
1. Await the permission with `requestPermissionForAlias` and a `@PermissionCallback`, not fire-and-forget like `record()` at BarSensorsPlugin.java:110-113. Reject with "Turn Bluetooth on" if the adapter is off.
2. With a saved address: on API 33+ use `getRemoteLeDevice(addr, ADDRESS_TYPE_RANDOM)`, otherwise `getRemoteDevice`. Then `connectGatt(ctx, false, cb, TRANSPORT_LE)` with a 10 s timeout.
3. Fallback: an 8 s `SCAN_MODE_LOW_LATENCY` scan filtered on the service UUID. Take the strongest RSSI and save the address. It comes from FICR, so it stays the same across reboots.

**GATT state machine.** One operation at a time, each step started from the previous callback:
1. connected
2. `discoverServices`
3. `requestMtu(247)`
4. read INFO
5. read STATUS (both forwarded as frames)
6. The DATA CCCD, the only one (STATUS frames ride on DATA, D28) (API 33+ `writeDescriptor(d, value)`; older: `setValue` + `writeDescriptor`)
7. resolve `{name, address, mtu, wallMs, elapsedMs}`

**Other methods and events.**
- `write({b})` writes CTRL with response.
- Handle both `onCharacteristicChanged` overloads and both `onCharacteristicRead` overloads. **The API 33 byte[] overloads don't call super**, or frames would be delivered twice.
- Event `pod {f: [base64], w: [elapsedRealtime ms]}`, coalesced every 50 ms. Event `podState {state: connecting|ready|lost|closed, status, mtu}`.
- Status 133: close and retry up to 3 times, 500 ms apart.
- **Reconnect:** on an unrequested disconnect, emit `lost`, `close()`, then `connectGatt(ctx, true /*autoConnect*/, ...)`. That works with the screen off. Rerun the chain (CCCDs don't persist without bonding) and emit `ready`. JS answers with `write(command())`. JS keeps running with the screen off because RecordingService (health FGS + wake lock) keeps the process alive, which is how the hidden runLive buzzes work today. Step 12 checks this; if it fails, the fallback is a Java-side resume write with a vector test.
- No `requestConnectionPriority` call; it would override the pod's request.
- `disconnect()` posts the last batch, then disconnects, closes and resolves, the same way stopSensors works. `handleOnDestroy` does the same.

**Manifest:**
- `uses-feature android.hardware.bluetooth_le required="false"`
- `BLUETOOTH_SCAN` with `usesPermissionFlags="neverForLocation"`
- `BLUETOOTH_CONNECT`
RecordingService stays type health; add connectedDevice before any Play Store build.

**`PodNativeSource` in sources.js.** Uses the same PodStream.
- `start()`: listen to `pod` (base64 to DataView, then `stream.frame(dv, w[i])`) and `podState`; `connect()`; `write(command())`; wait for the first DATA.
- `stop()`: STOP, await `stopped`, `disconnect()`, remove the listeners.

### app.js and index.html

- **index.html:** a "Connect pod" button next to `startBtn` (:53), a way back to the phone source, and a hero sub text that says "Clip the pod (or the phone) to the bar...". The sticky source goes in localStorage `source`, wrapped in try/catch.
- **startSession** (app.js:192-195):
  - The pod source gets `onSample` directly, not the `applyCalibration` feed (:194).
  - `sourceName = "pod BarPod-XXXX"`.
  - Write `pod.json` (INFO and scales) instead of `calibration.json` (:203).
  - The "No sensor data" and HTTPS messages apply to the phone only, or get pod wording.
  - `session.t0Wall ??= source.wallAt?.(s.t) ?? Date.now()` (:58).
- **newSession:** `lastHiddenRun = -Infinity`. This is a pre-existing bug (:48): every new PodStream restarts t, and it already affects NativeSensorSource.
- The calibration card is disabled while the pod is the source.
- **updateFooter** (:692): `pod BarPod-1A2B · 211 Hz · 16/pkt · MTU 247 · 30 ms · lost 0 · gaps 0 · 3.95 V`.
- **Step 10 additions:**
  - Banners: "Pod link lost, reconnecting (the pod buffers 38 s)", "Pod asleep (idle): bump the bar to wake it", "Pod battery low".
  - A set-card flag: "data gap 1.2 s at 0:14: reps near it unreliable".
  - `link.json` (epochs, gaps, stats, MTU, interval, reconnects) next to the set CSVs at every set save and at session end (P2), plus `barDebug.link()`.
- Downstream code (sets.js, reps.js, fileio.js, store.js) is untouched. Set CSVs keep `toCSV`'s columns.

### Python

- `analysis/podlink.py`: the reference decoder, test encoders, and a `--usb` mode for step 4.
- `analysis/make_fake_pod.py`: the test vectors.
- `analysis/dt_stats.py <csv>`: rate, gaps by the median rule, and dt std, for every firmware regression.
- `analysis/log_ble.py` (bleak), the roadmap 1.4 receiver:
  - finds the pod by its service UUID;
  - reads INFO and STATUS, subscribes, sends `STREAM_LIVE(0)`, and reconnects with `STREAM_FROM`;
  - writes `data/bar-xiao-YYYY-MM-DD-HHMM-ble.csv` with log_serial's HEADER, t at 6 decimals;
  - writes a `.pkt.txt` dump (type, arrival ms, hex);
  - prints a 2 s report: Hz, lost, gaps, MTU, interval.
- `analysis/compare_ble_usb.py usb.csv ble.csv`:
  - aligns on the first BLE sample's six values (within print precision) and confirms on the next 50;
  - pairs every sample by relative time;
  - reports missing samples both ways, duplicates and max |da|, |dg| and |dt|;
  - when the window holds reps, compares reps.analyze MCV and ROM.

## 4. Steps (one sitting each)

**1. Spec and decision (docs only).**
- Write `docs/ble.md`: UUIDs, the LE byte array, the DATA/STATUS/INFO/CAL/CTRL tables, scale factors, sizing, and the resume, epoch and sleep rules. It is the single spec that podlink.h, podlink.py and sources.js point at.
- Write D27 in decisions.md. Check the next free number when writing; other agents edit the repo.
- Rewrite android.md A4, which still names the community plugin.
- **Verify:**
  - Reread against pod.ino: ODR 208, +-8 g, +-1000 dps, and the burst order gyro then accel (:352-357).
  - Check the scale factors against LSM6DS3.cpp:671-673 and :732-739.
  - Check the frame sizes: DATA 14 + 14n <= 238, STATUS 20, INFO 32.

**2. Python reference.**
- Write `podlink.py` and `make_fake_pod.py` with 9 scenarios:
  1. clean stream
  2. reconnect overlap with duplicates
  3. RESYNC with an overflow gap
  4. pkt_seq hole
  5. u32 tick wrap
  6. reboot with STATUS mapping
  7. sleep tail 0
  8. small n with an IMU_RESTART hole
  9. `STREAM_FROM(new boot, 0)` with head < RING_N (the underflow case)
- **Verify:** `analysis\.venv\Scripts\python analysis/make_fake_pod.py` prints PASS for every scenario:
  - t strictly increasing;
  - lost equal to the injected loss;
  - exact gap t0/t1;
  - duplicates dropped;
  - reboot rebase within 1 ms of the injected wall gap;
  - the wrap is continuous.

**3. JS port.**
- Port the POD constants, `decodeFrame` and `PodStream` into sources.js, line for line.
- Add `webapp/test/pod.mjs`: t within 1e-9 s, values within 1e-6, stats exact.
- Add both commands to CLAUDE.md rule 1 and update the sources.js header comment.
- **Verify:** `node webapp/test/pod.mjs` is all PASS, and `node webapp/test/parity.mjs` is still all PASS.

**4. Firmware F1: ring and packer over USB, no radio.**
- Add podlink.h, the ring and the packer, with the poll unchanged. With `PKT_OVER_USB 1`, each DATA frame prints as `P <hex>` next to the CSV; `log_serial.parse` drops lines without 7 fields. Add `dt_stats.py`.
- **Verify:**
  - `python firmware/flash.py pod`, then `analysis\.venv\Scripts\python analysis/podlink.py --usb COMx --seconds 600`:
    - every CSV sample appears exactly once in the frames;
    - `ticks*15625//512 == t_us` exactly;
    - values within print precision;
    - first_idx and pkt_seq continuous;
    - the run crosses the 512 s RTC2 wrap.
  - log_serial.py for 120 s: 209-212 Hz, 0 gaps, dt std <= 0.05 ms.
  - Then set `PKT_OVER_USB 0`.

**5. Firmware F2: the BLE service.**
- Bluefruit config with the `begin()` check, the frozen GATT table, advertising, INFO/CAL, STATUS at 1 Hz, the CTRL queue, STREAM_LIVE/FROM/STOP, the never-block sender, the link-setup timeline, `#` lines printed only from loop(), and the session-hold guards (30 min idle limit, no lid-down).
- **Verify:**
  - Banner `# ble begin ok`, the ring size and free heap; note the compile RAM line.
  - In chrome://bluetooth-internals (built in, zero install):
    - BarPod-XXXX advertises 9ba40001 with 5 characteristics;
    - INFO reads `20 01 ...`, and boot_id changes after a reset;
    - after subscribing to DATA, STATUS frames (type 0x10) arrive on it at 1 Hz with the USB flag;
    - subscribe to DATA and write `02 58 02` to CTRL: 238 B values starting `01 10`, about 13 per second;
    - write `03`: a STATUS with stream_stopped.
  - USB shows `# link mtu 247 ...` for what Windows grants.
  - log_serial.py for 600 s while advertising, then while streaming: 209-212 Hz, 0 gaps, dt std <= 0.05 ms. Above 0.1 ms is a fail.

**6. Browser MVP: the first real pod samples in the app.**
- PodWebSource (setup, stop handshake, capture), the Connect pod button with the sticky source, and the app.js changes except the step 10 items. The lastHiddenRun fix lands here.
- **Verify on the laptop.** `analysis\.venv\Scripts\python -m http.server 8000 -d webapp`, then Chrome on http://localhost:8000:
  - the live plot moves;
  - the footer shows about 211 Hz, 16/pkt and lost 0;
  - |a| at rest is about 10.1 (raw pod, no phone cal);
  - 5 slow reps with pauses give a set card;
  - End session: stop resolves, stop-tail 0, last index + 1 = STATUS head;
  - a second session in the same tab works;
  - parity.mjs passes.
- **Then the Pixel.** `adb reverse tcp:8000 tcp:8000` and Chrome on http://localhost:8000. Run the same checks and record the MTU and n_per_packet the pod got. That answers whether Chrome on Android exchanges the MTU by itself.
- Push to GitHub Pages only when you say so.

**7. Roadmap 1.4 proof: the bleak receiver against USB.**
- Uncomment bleak in `analysis/requirements.txt` and run `analysis\.venv\Scripts\pip install bleak`. Write `log_ble.py` (with STREAM_FROM reconnect) and `compare_ble_usb.py`.
- **Verify:**
  - Pod on USB, log_serial.py and log_ble.py together for 10 min:
    - 0 missing either way;
    - |da| <= 6e-5 m/s^2 and |dg| <= 6e-4 deg/s;
    - |dt| <= 3 us;
    - pkt_seq gaps 0 and unexplained gaps 0.
  - Outage matrix (done with `log_ble.py --outage`, deliberate drops; see Progress):
    - 3, 10 and 30 s: lost 0, no time hole;
    - 60 s: lost equals the pod's lost_overflow delta exactly, the reason is overflow, and the hole matches the USB timeline.
  - Repeat the 10 min run with laptop Chrome capture as a second receiver.
  - Keep all captures in analysis/data (P2).

**8. Firmware F3: data-ready interrupt and sampler task.**
- First confirm 0x0B bit 7 in the LSM6DS3TR-C datasheet.
- Add the DRDY routing, ISR, sampler at priority 4, late-read drop, xlLag, park handshake, `usbDrain`, `delay(5)` in loop and the `# ring` counters.
- **Verify:**
  - On each board, log_serial.py for 600 s while log_ble.py streams:
    - rate within 0.1 Hz of the README table;
    - 0 gaps and dt std <= 0.05 ms;
    - missed 0 and late 0.
  - Step 7's compare gives 0 missing, and the 30 s outage gives lost 0.
  - The D26 bench set:
    - `w` 5/5 quiet;
    - `s` plus a pickup 3/3 with no instant re-wake;
    - lid-down with the port closed sleeps;
    - `o` stays off.
  - Current is unmeasured here (no meter); report it as such.

**9. Firmware F4: sleep with a phone connected.**
- Add the prologue (drain, STATUS, disconnect, INT1 teardown), the BLE SLEEP op and `LOW_BATT_WARN_V`.
- **Verify** while streaming to log_ble.py, 3 times each for `s` over USB and the BLE SLEEP op:
  - the reason arrives;
  - sleep-tail loss is 0;
  - the disconnect arrives at once (0x13);
  - the pod stays asleep;
  - a pickup gives a new boot_id, log_ble reconnects into a new epoch, t is monotonic, and the first samples contain the pickup.
  - Lid-down while the hold is on does nothing; after STOP it sleeps.
  - The idle and low-battery paths need a battery, so they are skipped until 1.5, and the report says so.

**10. Web reconnect, epochs and gap reporting.**
- Add the PodWebSource reconnect loop, banners, set-card gap flags, link.json, `barDebug.link()` and the footer's reconnects and epochs.
- **Verify** on laptop Chrome:
  - Reset button mid-rest: t keeps rising and the epoch gap is within 0.2 s of a stopwatch.
  - Bluetooth off for 10 s mid-set: no gap.
  - Bluetooth off for 60 s mid-set: the card is flagged, and link.json lists the gap with its exact count.
  - BLE SLEEP plus a pickup: "Pod asleep", then back without a tap, and the next set is detected.

**11. Android plugin (Java only).**
- PodLinkPlugin.java, the MainActivity registration and the manifest.
- **Verify:**
  - `npx cap sync android`, `cd android && gradlew.bat assembleDebug` (Java 25, no new dependency), `adb install -r app\build\outputs\apk\debug\app-debug.apk`.
  - In chrome://inspect: `Capacitor.Plugins.PodLink.connect()` shows the Nearby devices prompt once, waits for the answer, and resolves with mtu 247.
  - After a STREAM_LIVE write, a `pod` listener counts about 13 frames/s.
  - adb logcat shows the MTU and the DATA CCCD write.

**12. App source and screen-off soak.**
- PodNativeSource and the native branch in app.js.
- **Verify on the Pixel:**
  - A full session: sets autosaved to Documents/BarTracker, footer lost 0, MTU 247, 16/pkt.
  - A 35-45 min locked session with walk-aways of about 20 s and about 90 s, one BLE SLEEP plus pickup mid-rest, and test sets. From link.json and `node android/pull-recording.mjs`:
    - reconnects happened with the screen off;
    - the 20 s walk-away lost 0;
    - the 90 s walk-away lost exactly the pod's count;
    - the epoch rebase was clean;
    - sets were found and buzzed while the screen was dark.
  - Two sessions back to back.
  - One set through compare_ble_usb.py against a simultaneous USB log, with a time tolerance of half the CSV's last digit plus 3 us.
  - Pixel Chrome with the screen off for 1 min: does the link survive? Write the answer into android.md (D12 says it doesn't).

**13. Real sets and docs.**
- Do a few light squat/bench sets with the pod on the bar through the app. Put the CSVs and link.json in analysis/data as `bar-<lift>-YYYY-MM-DD-HHMM.csv`, and add one set to parity.mjs's defaults.
- Update firmware/README.md (BLE section and the link values each central granted), android.md A4, the CLAUDE.md layout table, and today's session log. Tick roadmap 1.4, 4.1 and A4.
- **Verify:** the full CLAUDE.md parity command list is all PASS, and reps.py on the BLE set matches the app.

**Later, each its own decision:**
- currents once a meter and cell are wired;
- 2M PHY after the loaded-bar range test;
- DC/DC, only if Seeed's KiCad shows the DCC inductor;
- a process-level GATT holder and the connectedDevice FGS type;
- pod calibration through CAL (1.3);
- bonding or a CTRL token;
- a Python-first "a hole ends the segment" rule if real gaps show up inside sets.

## 5. End-to-end test plan

**T0 Unit.** `make_fake_pod.py`, `node webapp/test/pod.mjs` and `node webapp/test/parity.mjs` are all PASS after every phone-side change.

**T1 Firmware regression** (after every firmware step):
- log_serial.py for 600 s: 209-212 Hz (within 0.1 Hz of the board's README rate after step 8), 0 gaps (median rule), dt std <= 0.05 ms (fail above 0.1 ms), missed and late 0.
- The boot banner shows the heap.
- The D26 bench set: `w` 5/5, `s` plus pickup 3/3, lid-down, `o`.

**T2 Packer before radio (step 4).** The `P <hex>` frames decode to exactly the CSV samples: same ticks (`ticks*15625//512 == t_us`), values within print precision, continuous index and pkt_seq, and a clean 512 s wrap.

**T3 BLE against USB on the same board (the core proof).**
- **Setup:** the pod streams the USB CSV and BLE at the same time. Both channels carry the same samples with the same device ticks. Receivers:
  1. log_ble.py (bleak);
  2. laptop Chrome capture on localhost;
  3. Pixel Chrome via adb reverse;
  4. the Pixel app (a set pulled with pull-recording.mjs).
- **Pass:**
  - 0 samples missing in either direction over the overlap, or exactly the counted overflow;
  - |da| <= 6e-5 m/s^2 and |dg| <= 6e-4 deg/s (USB prints 4 and 3 decimals, plus float rounding);
  - |dt| <= 3 us for the 6-decimal CSV, or half the last printed digit + 3 us for the app's `toPrecision(7)` sets;
  - pkt_seq gaps 0 and unexplained gaps 0;
  - on windows with reps, reps.analyze gives MCV within 0.005 m/s and ROM within 1 mm.

**T4 Outage matrix.**
- Bluetooth off for 3, 10, 30 and 60 s; the pod in a metal pot; a walk-away with the Pixel.
- Inside the ring: lost 0, no time hole, catch-up time logged.
- Beyond the ring: lost equals the pod's lost_overflow delta exactly, and the hole matches the USB timeline sample for sample.

**T5 Epochs and sleep.**
- Reset button, BLE SLEEP plus pickup, and `s` plus pickup, during a live session.
- t strictly increasing, the epoch gap within 0.2 s of a stopwatch, the sleep reason shown, sleep-tail loss 0, and the new epoch's first samples contain the pickup.
- The 31 min idle sleep waits for a battery (1.5).

**T6 Stop flush.** End session after a set: `stop()` resolves, last index + 1 = STATUS head, the last set's CSV ends at the final sample, and stop-tail is 0.

**T7 Pixel app with the screen off.**
- A locked session over 30 min with 20 s and 90 s walk-aways, a pod sleep and wake, and test sets.
- Reconnects with the screen off, exact loss counts, sets found, buzzes fired while hidden, and two sessions back to back (the lastHiddenRun fix).

**T8 Pixel Chrome.** Record the MTU and n_per_packet; a 10 min stream with lost 0 (screen on); and the 1 min screen-off result, written into android.md.

**T9 RF stress.**
- The pod in its enclosure on a loaded bar, the phone in a pocket at 2, 4 and 6 m, earbuds playing, a 30 min session.
- Record lost, gaps, reconnects and detected sets against sets done, plus the granted interval and timeout from the `# link` lines.

**T10 Real sets.** Pod set CSVs go into analysis/data and one goes into the parity defaults. reps.py matches the app, and the footer showed lost 0.

**T11 Power** (needs a meter and a cell; otherwise reported as unmeasured). Average current for: the busy poll, the step 8 build, advertising, streaming, and System OFF.

Every skip and failure is reported before roadmap 1.4, 4.1 or A4 is ticked.

## 6. Risks and open questions

### Needs your call

1. **D26 amendment:** from a STREAM command until STOP, a 30 min idle limit and no lid-down gesture. A phone that dies without sending STOP keeps the pod awake for up to 30 min.
2. **One pip install of bleak** into the venv for roadmap 1.4. Laptop Chrome stays a zero-install second receiver.
3. **The app supports Android 12+ (API 31) only in v1.**
4. **No pairing in v1.** Anyone in range can connect to the pod or put it to sleep, and only one phone can connect at a time.
5. **Pod data stays uncalibrated until 1.3.** |a| reads about 3% high, which goes straight into MCV and ROM. Consider doing 1.3 before trusting pod numbers.

### Risks

- **Timestamp jitter.** SoftDevice radio interrupts (priority 0) can delay the GPIOTE ISR that latches RTC2. T1 gates dt std. Fallback: hardware capture, GPIOTE to PPI to TIMER CAPTURE, with the HFXO held on.
- **RTC2 continuity.** RTC2 starts after `begin()` so the LF clock hand-over can't freeze it. Check the first-sample dt after a wake.
- **RAM.** The 128 KiB ring plus the SoftDevice with BANDWIDTH_MAX, TinyUSB and task stacks. `begin()` failing is otherwise silent, so the plan prints its result and the free heap. Fallback: 4096 samples.
- **Link parameters.** Chrome on Android may never exchange the MTU, and phones may ignore the requested interval or timeout. Mitigated by the pod's own MTU request, the hard "MTU too small" fault, and the `# link` lines.
- **DRDY register and INT1 reuse.** 0x0B needs the datasheet check. INT1 now carries both data-ready and wake, which is the instant re-wake class of bug; mitigated by the teardown in the prologue and the 3/3 sleep tests.
- **I2C ownership.** One missed path that touches Wire1 outside the sampler gives rare corrupt register writes or a stuck bus. All of it funnels through `samplerPark()`.
- **Loss beyond the ring inside a set.** reps.js bridges the hole with its trapezoid. v1 only flags it (set card, link.json). The real fix is a Python-first algorithm decision.
- **Epoch rebase accuracy.** It depends on STATUS latency (tens of ms), phone clock drift and NTP steps. It is clamped so t keeps rising, and it only matters during rests.
- **First set after a wake.** Sampling starts at the wake, so there is no still before a pickup that wakes the pod. The 30 min hold makes this rare during a session.
- **Catch-up burst.** After a long outage, up to 38 s of backlog arrives at 4-7x real time. With the screen off that means many runLive calls back to back and a CPU spike, and the rest timer freezes, then jumps. The banner explains it.
- **Screen-off unknowns.**
  - Web Bluetooth with a hidden page on Android Chrome is untested; D12 says it needs the screen on.
  - App JS staying alive with the screen off beyond the 20 s android.md tested.
  - autoConnect reconnect latency.
  - Doze over 30+ min.
  T7 decides. The fallback is a native resume write with a Java test against the shared vectors.
- **RF in a gym.** Steel, the lifter's body, and earbuds sharing the Pixel's radio. Mitigated by +4 dBm, 1M PHY, the 4 s timeout and the ring; measured in T9.
- **GATT caching.** The table is frozen at step 5, including the CAL stub. Any later change needs a Bluetooth toggle on each phone.
- **USB stalls in steps 4-7.** In poll mode, a host that holds the port open without reading (a paused Serial Monitor) stalls sampling. Close other serial tools; step 8 removes the problem.
- **Plugin lifetime.** The GATT client dies with the Activity, the same as BarSensors. A process-level holder comes later.
- **Concurrent edits.** Other agents are editing the repo: take the decision number at write time and re-read android.md A4 before rewriting it.
- **Current figures are estimates.** There is no meter or battery on this laptop yet.

## 7. Checked in the code for this plan (read-only)

- **BLEConnection.cpp:**
  - :183-195: `requestConnectionParameter` sets min = max.
  - :353: stores `server_rx_mtu` after a pod-initiated exchange.
  - :232-235: `getHvnPacket` waits `BLE_GENERIC_TIMEOUT` = 100 ms (bluefruit_common.h:47).
- **BLECharacteristic.cpp:715/722:** notify truncates to `_max_len` and only splits above `getMtu() - 3`.
- **bluefruit.cpp:** :159 has `service_changed = 1` by default; :638 has `setEventCallback`.
- **analysis:**
  - log_serial.py:45 uses the median gap rule;
  - `parse()` drops lines without 7 fields;
  - pyserial is in the venv;
  - bleak is commented out in requirements.txt;
  - `.gitignore` covers `analysis/data/fake_*.csv`.
- **app.js:**
  - :48 `lastHiddenRun` is never reset in `newSession`;
  - :58 `t0Wall`;
  - :194 the calibration feed;
  - :203 `calibration.json`;
  - :692 `updateFooter`;
  - index.html:53 `startBtn`.
- **pod.ino:** the poll is at :332-336, the burst at :352-357, the lid-down check at :383, and `systemOff` with the SoftDevice at :215-221.
- **Docs:** D27 is the next free decision number today, and android.md A4 still names `@capacitor-community/bluetooth-le`.