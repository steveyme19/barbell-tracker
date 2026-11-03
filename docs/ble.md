# PodLink v1 over CRP: the pod's Bluetooth and USB protocol

The one spec for the link between the pod (XIAO nRF52840 Sense, `firmware/pod`) and the phone or PC.
`firmware/pod/podlink.h`, `firmware/pod/podcrp.c`, `analysis/podlink.py` and `analysis/crp.py` (the
reference, P1) and the pod classes in `webapp/sources.js` all follow this file byte for byte.
Change it here first.
- Why it's built this way: D27, and D45 for CRP, in `docs/decisions.md`.
- Build order and tests: `docs/ble-plan.md` (where it disagrees with this file, this file wins).

**Two layers.** The PodLink frames (sections 3, 4, 6 and 7) carry the samples and the link's
story; CRP 0x0B (section 2, D45) carries the frames: Ansync Labs' Channel Register Protocol, over
Bluetooth and over USB.
- **Status:** the CRP binding lands in stages (D45: USB at S6, Bluetooth at S7). Until then the pod
  runs the PodLink v1 GATT table, which this file described up to commit e584069.

**The idea.** The pod keeps the record; the radio only carries it. Every IMU sample gets a
32-bit sample index and its own crystal timestamp when it's read, and waits in a RAM ring
(38 s). After every connect or reconnect the phone says where to resume. A dropout shorter
than the ring loses nothing; a longer one loses an exact, counted number of samples, which is
never filled in. (Exceptions, marked as lower bounds: the pod sleeps or resets while
disconnected, or a STOP is never answered; the phone can only count up to the last head it
heard; section 7.)

Audited 2026-10-19 (3 auditors + skeptics, then a coverage check and a fresh-eyes implementer
pass); every confirmed finding is folded in.

## 1. Basics

- All multi-byte fields are **little-endian**. Byte 0 of every frame is its **type**, so every
  frame (pushed or read) can go through one decoder in arrival order.

  | type | frame |
  |---|---|
  | 0x01 | DATA |
  | 0x10 | STATUS |
  | 0x20 | INFO |
  | 0x30 | CAL |

- **Raw values travel; the phone scales them.** Samples are the IMU's int16 counts, exactly the
  12-byte burst `pod.ino` reads from OUTX_L_G (gyro x, y, z, then accel x, y, z). Scale factors
  (INFO, with these as the fallback) equal Seeed's `calcAccel` / `calcGyro`
  (LSM6DS3.cpp:671-673, 732-739) at the pod's ranges (decision O5), times G for accel:

  | | range | per count |
  |---|---|---|
  | accel | +-8 g | 0.061 mg x 4 / 1000 x 9.80665 = **0.0023928226 m/s^2** |
  | gyro | +-1000 deg/s | 4.375 mdps x 8 / 1000 = **0.035 deg/s** |

  So BLE samples equal the USB CSV (`imu_serial`, `pod`) to its print rounding.
- **Time is device time** (D4, D25): RTC2 ticks on the 32.768 kHz crystal, 1 tick = 1/32768 s,
  carried as 32 bits (wraps after 2^32 / 32768 s = 36.4 h). Nothing assumes 208 Hz: each board
  runs 209 to 212 Hz on the IMU's own clock.
- **Sample index**: u32, 0 at boot, +1 per stored sample. `head` = the next index = samples
  stored since boot. A wake from sleep is a reboot: new boot_id, index and ticks back to 0.
- **Axes are the pod's own** (hardware/enclosure.md "XIAO axes"). Calibration is not D23's phone
  one: the app applies the pod's own (D42, `webapp/pod-calibration.json`), matched on INFO's serial.
  CAL stays a stub.

## 2. Transport: CRP (D45)

The frames travel inside CRP 0x0B, Ansync Labs' Channel Register Protocol:
- the engine is vendored in `firmware/pod/src/crp`, the JS client in `webapp/crp`;
- the specs live in `C:\Users\quent\crp-study\spec`. They still say 0x0A, and where they disagree with
  the 0x0B engine, the engine wins;
- the Python side is ours: `analysis/crp.py`.

The frames themselves didn't change, so INFO still says proto 1.

### 2.1 Packets the pod uses
Little-endian; byte 0 is the packet type. One packet per Bluetooth write or notification (CRP
framing level 0); on USB each packet is `[len u16][packet]` (level 1, section 2.5).

| packet | bytes | from | use |
|---|---|---|---|
| NOP 0x00 | `00` | both | heartbeat: the pod sends one after 1 s without sending anything; USB clients every 1 s |
| HELLO 0x01 | `01 ver max_ch max_chunk:u16 flags device_id:u32` (10 B) | both | opens a session; the client sends first, the pod answers with its own |
| BYE 0x02 | `02 base:u32 device_id:u32` (9 B) | both | ends the session |
| WRITE 0x04 | `04 txn reg:u32 value` | both | client: a register write (txn 1..255, answered by a RESPONSE). Pod: a **push** (txn 0, no answer) |
| READ 0x06 | `06 txn reg:u32` (6 B) | client | a register read |
| RESPONSE 0x0C | `0C txn status ch reg:u32 value` (8 + N) | pod | answers a READ (with the value) or a WRITE (without); ch = 0xFF |
| LOG 0x18 | `18 level ts_ms:u32 modlen module msg` | pod, USB only | the `#` lines in CRP mode |

- **Discovery:** the pod answers CRP's QUERY_DEVICE / QUERY_GROUP / QUERY_REG, so Ansync's dashboard
  can list the registers.
- **No channels:** it opens none. There are no subscriptions or bulk transfers, and HELLO says
  max_channels 1.
- **Status codes used:**
  - 0x00 OK
  - 0x01 UNKNOWN_REG
  - 0x02 ACCESS_DENIED
  - 0x03 INVALID_VALUE
  - 0x08 BUSY

### 2.2 Session
- **HELLO.** The client sends HELLO. The pod answers HELLO {ver 0x0B, max_channels 1, max_chunk 244
  on Bluetooth or 1024 on USB, flags 0x01 (push), device_id 0x444F5042 "BPOD"}.
  - A version below 0x0B gets BYE.
  - Before a HELLO the pod ignores everything but HELLO and NOP, and sends nothing.
  - A HELLO during a session starts a new one.
- **The end of a session:** BYE, END 0xFF, a disconnect, or (USB only) liveness. A session that ends
  on a live link ends that link's stream exactly as a disconnect does (section 5): streaming off,
  queued STATUS frames and the built frame dropped, PUSH off. The session hold (section 6) stays.
- **Liveness.**
  - On Bluetooth the pod ignores the client's silence: the link's supervision timeout (4 s) is the
    signal, and a phone's WebView timers throttle with the screen off. So Bluetooth clients send no
    heartbeat (CRPClient `heartbeatMs: 0`).
  - On USB, 3 s without receiving anything ends the session, and USB clients send a NOP every 1 s.
- **No transaction is ever retried** (CRPClient `txnRetries: 0`). The engine runs a repeated WRITE
  again, and a STOP run twice answers twice.

### 2.3 Registers
Groups of 64 K (mask 0xFFFF0000). Registers start at offset 1, so CRP discovery, which starts each
group at offset 1, finds all of them.

| address | name | type | access | value |
|---|---|---|---|---|
| 0x00010000 | PROTO | u8 | R | 1 (the group base) |
| 0x00010001 | INFO | blob 32 | R | the INFO frame (section 3) |
| 0x00010002 | STATUS | blob 20 | R, pushed | **read:** a plain STATUS built when the READ is processed, with no event bits. **Push:** every STATUS of section 3 |
| 0x00010003 | CAL | blob 64 | R | the CAL frame |
| 0x00010004 | DATA | blob 28..238 | pushed | every DATA frame; a READ gets ACCESS_DENIED |
| 0x00010005 | CTRL | blob 1..10 | W | the CTRL ops of section 4, byte for byte |
| 0x00010006 | PUSH | u8 | RW | 1 = this session takes pushes. 0 at every HELLO, session end and disconnect |

The other groups are read-only mirrors for Ansync's dashboard; the app doesn't read them.

- **State, 0x0002xxxx** (read only):

  | offset | name | type | notes |
  |---|---|---|---|
  | 01 | BATTERY_MV | u16 | |
  | 02 | FLAGS | u8 | the STATUS state bits |
  | 03 | HEAD | u32 | |
  | 04 | TICKS | u32 | |
  | 05 | BOOT_ID | u32 | |
  | 06 | SERIAL | u32 | |
  | 07 | FW_BUILD | u16 | |
  | 08 | WAKE_REASON | u8 | |
  | 09 | SLEEP_REASON | u8 | 0 while awake |
  | 0A | LOST | u32 | this link's lost_overflow, not saturated |
  | 0B | MISSED_DRDY | u32 | |
  | 0C | BATT_V | f32 | |
  | 0D | RESETREAS | u32 | |
  | 0E | NAME | string | |

- **Link, 0x0003xxxx** (read only; describes the link the read arrives on):

  | offset | name | type | notes |
  |---|---|---|---|
  | 01 | KIND | u8 | 1 Bluetooth, 2 USB |
  | 02 | MTU | u16 | |
  | 03 | N_MAX | u8 | |
  | 04 | INTERVAL | u16 | 1.25 ms units |
  | 05 | PHY | u8 | |
  | 06 | DL | u16 | |
  | 07 | FRAMES | u32 | |
  | 08 | TX_DROPS | u32 | |
  | 09 | RX_DROPS | u32 | |
  | 0A | STACK_FREE | u32 | |

- **Bench, 0x0004xxxx** (USB only): 01 BENCH, u8, write only. It takes the bench characters 's',
  'o', 'w', 'i' and 'h' (`pod.ino`, top); anything else gets INVALID_VALUE.

How writes are answered:

| write | answer |
|---|---|
| CTRL, op taken | OK. A STREAM_FROM with the wrong boot_id counts as taken: its real answer is the boot_mismatch STATUS |
| CTRL, unknown op or wrong length | INVALID_VALUE, and nothing changes |
| CTRL, from the SLEEP op on | BUSY |
| a read-only register | ACCESS_DENIED |
| an unknown register | UNKNOWN_REG |

### 2.4 Bluetooth
- **One service with two characteristics.** The service is `00005090-0000-1000-8000-00805f9b34fb`,
  the one Ansync's JS transport and dashboard use.
  - **RX** `000050b0-0000-1000-8000-00805f9b34fb`: write and write without response, `setMaxLen(244)`.
    One write is one packet.
  - **TX** `000050b1-0000-1000-8000-00805f9b34fb`: notify, `setMaxLen(244)`, read permission
    `SECMODE_OPEN` (Bluefruit copies it to the CCCD's write permission, BLECharacteristic.cpp:296).
    One notification is one packet.
- **One notification channel.** Every packet the pod sends goes out on TX, so it arrives in the
  order sent. Windows (WinRT, under bleak and Chrome) can deliver notifications of two
  characteristics out of order, and the decoder depends on arrival order (D28).
- **Set every length.** Bluefruit's default max length is 20 B (BLECharacteristic.cpp:43), and
  `write()` / `notify()` silently cut anything longer (:582, :715). `notify()` also splits anything
  above MTU - 3 into several notifications, which would break one packet per notification. The pod
  never uses it: it calls `sd_ble_gatts_hvx` and never hands that more than MTU - 3.
- **UUIDs** are 16-bit values on the Bluetooth base. They are Ansync's convention, not values the
  Bluetooth SIG assigned. `BLEUuid` keeps a pointer, not a copy (BLEUuid.cpp:185-187), so use
  separate static values.
- **Telling the pod apart.** Every Ansync CRP device shares the service, so clients also require all
  three of: the name prefix `BarPod-`, HELLO device_id "BPOD", and, as before, the session's INFO
  serial.
- **Address.** The pod uses a static random address of its own: `NRF_FICR->DEVICEADDR` XOR a fixed
  mask, top two bits 11, set with `Bluefruit.setAddr`.
  - So phones and PCs see a new device, with no cached PodLink v1 attribute table. Without bonding,
    a cached table survives a firmware change, and a table change would otherwise need Bluetooth
    toggled on every phone.
  - The CRP table is frozen from its first build: registers can change, the characteristics don't.
- **Name** `BarPod-XXXX`: XXXX is the low 16 bits of that address in hex, the end of the MAC the
  phone shows. It changed once, with D45.
- **No pairing:** anyone in range can connect, one at a time.

### 2.5 USB
- **Text mode**, from boot, as before D45: the CSV, the `#` lines and the bench characters. It stays
  the independent truth for `usb_clock.py`, `compare_ble_usb.py` and `check_soak.py`. Those tools
  check the frame path, so they must not share it.
- **CRP mode.** The bytes `0A 00 01 0B` (a level-1 HELLO: length 10, type 0x01, version 0x0B) switch
  the port to CRP.
  - **Framing** is level 1, `[len u16][packet]`, with len 1..1024. A bad length drops one byte and
    rescans. Text can't look like a length: the second byte of any text pair is at least 0x0A.
  - **The stream** is the same as on Bluetooth (sections 3 to 6), with its own cursor, n_max 16 and
    interval 0. It never touches the session hold.
  - **`#` lines** go out as LOG packets (module "pod").
- **Back to text** when the port closes, at BYE or END, after 3 s without receiving, or when no HELLO
  completes within 3 s of the switch. Then:
  - the CSV restarts live (cursor = head);
  - the bench characters are ignored for 3 s, and after that only a 1-byte read counts as one,
    because CRP packets contain 'h' and 'w'.

## 3. Frames

### DATA (0x01), 14 + 14n bytes, n = 1..16 (never 0)

| offset | type | field |
|---|---|---|
| 0 | u8 | type 0x01 |
| 1 | u8 | n, samples in this frame, 1..16 |
| 2 | u16 | pkt_seq: +1 per DATA frame the link accepted since boot (STATUS doesn't count), per link, wraps at 16 bits |
| 4 | u32 | first_idx: sample index of sample 0 |
| 8 | u32 | t0_ticks: RTC2 ticks of sample 0 |
| 12 | u8 | flags: b0 RESYNC, b1 OVERFLOW, b2 IMU_RESTART, b3 FINAL; b4-b7 0 |
| 13 | u8 | cmd_seq: echo of the cmd_seq byte of the STREAM command this stream answers |
| 14 + 14k | u16 | dt_ticks of sample k from t0_ticks (sample 0: 0) |
| 16 + 14k | i16 x3 | gx, gy, gz raw |
| 22 + 14k | i16 x3 | ax, ay, az raw |

- Sample k: index `first_idx + k`, ticks `(t0_ticks + dt_ticks) mod 2^32`.
- The samples in a frame are consecutive indices, so **index holes only fall between frames.**
  A **time hole** (a missed data-ready, an IMU restart) can fall inside a frame: check every
  consecutive pair of samples.
- The pod never lets a dt pass 60000 ticks (1.83 s): it ends the frame early instead.
- **A frame is built once** and, if the link doesn't accept its push (section 5), resent
  byte-identical (same pkt_seq, same samples) until it's accepted. The same goes for STATUS. A
  STREAM command discards a built-but-unsent DATA frame (it belonged to the old stream).
- Flags:
  - **RESYNC**: the first frame sent after a STREAM command. pkt_seq continuity restarts here.
    Stays pending until a frame carrying it is accepted by notify().
  - **OVERFLOW**: the samples between the pod's cursor and this frame's first_idx left the ring
    before they were sent, either at a STREAM_FROM clamp or mid-stream when sampling lapped the
    cursor; so it can be on any frame, RESYNC or not. The jump is counted loss, not a protocol
    error. Stays pending until a frame carrying it is accepted. A STREAM command clears any
    pending OVERFLOW before applying its own clamp.
  - **IMU_RESTART**: sample 0 of this frame is the first sample after an IMU restart (50
    data-ready waits of 20 ms timed out in a row: about 1 s with no samples, pod.ino). The time
    hole is just before it, with no index jump. The pod remembers the first index after every
    restart still in the ring (index >= lo), in a table of 40 without duplicates, and always
    starts a frame there, also when resending the ring after an outage, so a resent backlog
    carries the flag again. Best effort: an IMU that restarts more than 40 times inside the
    ring loses the oldest entries, and the phone then calls those holes `imu` instead of
    `imu-restart` (still counted).
  - **FINAL**: the last DATA frame of a STOP or a sleep drain, if one is sent, and only on a
    frame built after the drain started (best effort). No empty frame is
    ever sent to carry it: with nothing pending, or a sleep drain cut short, there is no FINAL
    frame. The STATUS that follows (stream_stopped or going_to_sleep, with head) is the end
    marker. **Decoders never wait for FINAL.**

### STATUS (0x10), 20 bytes

When it's pushed (a WRITE txn 0 to STATUS, while the session has PUSH on):
- every 1 s while connected;
- at once when USB or charging changes;
- in answer to every STREAM command, before its first DATA;
- after a STOP has drained, or its drain was capped;
- before sleep.

A READ of STATUS gets a plain STATUS built when the READ is processed, so its head is at least that
of any DATA already sent on that link. Per link: lost_overflow, n_per_packet (16 on USB) and the
interval (0 on USB) are that link's.

| offset | type | field |
|---|---|---|
| 0 | u8 | type 0x10 |
| 1 | u8 | flags, see below |
| 2 | u16 | battery, mV (the cached 5 s reading) |
| 4 | u32 | ticks_now: RTC2 ticks when this STATUS was built |
| 8 | u32 | head: the next sample index. In the stream_stopped / going_to_sleep STATUS: the end of the drained stream (stop_head / the final head). |
| 12 | u16 | lost_overflow: samples the BLE stream skipped since boot (STREAM_FROM clamp-ups plus ring overruns while streaming; each index counted once; USB-side losses not included), saturates at 65535 |
| 14 | u16 | missed_drdy: samples the IMU produced but the pod didn't store, since boot (from the data-ready firmware on; always 0 in the polling firmware; IMU-restart holes not included), saturates at 65535 |
| 16 | u8 | sleep_reason: 0 none, 1 idle, 2 lid-down, 3 low battery, 4 command, 5 IMU lost |
| 17 | u8 | n_per_packet: samples per DATA frame at the current MTU (n_max) |
| 18 | u16 | connection interval, 1.25 ms units |

Flags:
- **State bits**, set in every STATUS while they hold: b0 USB power, b1 charging, b2 batt_low
  (battery < 3.50 V while on battery power; never set on USB power, where the reading can be
  the charger's; in the going_to_sleep STATUS it also means the sleep is IMU-off whatever its
  reason, so a bump won't wake it, D38), b3 streaming, b6 mtu_too_small.
- **Event bits**, set only in the one STATUS that reports the event: b4 going_to_sleep,
  b5 stream_stopped, b7 boot_mismatch.

### INFO (0x20), 32 bytes, read at every connect

| offset | type | field |
|---|---|---|
| 0 | u8 | type 0x20 |
| 1 | u8 | proto, 1 |
| 2 | u16 | fw_build |
| 4 | u32 | boot_id: random per boot, never 0. The pod waits (`sd_rand_application_bytes_available_get`, at most 100 ms) until 4 bytes are available, requires `sd_rand_application_vector_get` to return NRF_SUCCESS, and redraws a 0. If that fails it prints `# boot_id RNG failed` and doesn't advertise (USB keeps working), so a repeated id never reaches a phone |
| 8 | u32 | serial: `NRF_FICR->DEVICEID[0]` |
| 12 | f32 | accel_lsb, m/s^2 per count (0.0023928226) |
| 16 | f32 | gyro_lsb, deg/s per count (0.035) |
| 20 | u16 | odr_nominal, Hz (208; the real rate comes from the ticks) |
| 22 | u16 | ring_n, samples the ring holds (8192, or 4096 in a low-RAM build) |
| 24 | u8 | wake reason: 0 power-on / upload, 1 motion, 2 USB, 3 reset pin |
| 25 | u8 | reserved, 0 |
| 26 | u16 | tick_hz (32768) |
| 28 | u32 | reserved, 0 |

### CAL (0x30), 64 bytes, read

Stub until roadmap 1.3: byte 0 = 0x30, byte 1 = 0 (no calibration), the rest 0. A real
calibration (per-axis bias and scale) gets its layout in 1.3 without changing the table.

## 4. CTRL ops (written to the CTRL register, section 2.3; byte 0 = op)

| op | bytes after op | what the pod does |
|---|---|---|
| 0x01 STREAM_FROM | u8 cmd_seq, u32 boot_id, u32 from_idx (10 B in all) | If boot_id isn't the current one: send a STATUS with boot_mismatch and change nothing else (stream, cursor, hold and the current cmd_seq stay as they were). Otherwise `lo = head > ring_n ? head - ring_n : 0`, `cursor = clamp(from_idx, lo, head)`; if it was clamped up, the skipped samples are counted loss and the first frame has OVERFLOW. Then a STATUS, then DATA carrying this cmd_seq (first frame RESYNC). Sets the session hold. |
| 0x02 STREAM_LIVE | u8 cmd_seq, u16 back (4 B) | `cursor = head - min(back, head, ring_n)`. A fresh session sends back = 600 (~2.8 s of still lead-in from before Start). Then a STATUS, then DATA carrying this cmd_seq (first frame RESYNC). Sets the session hold. |
| 0x03 STOP | none (1 B) | Releases the session hold at once. Snapshot `stop_head = head` and drain every sample below it; the frame ending at stop_head has FINAL (no FINAL frame if nothing was pending). Then STATUS with stream_stopped and head = stop_head, and streaming ends. The drain is capped: if DATA can't flow (n_max < 4, or DATA notifications off), or it runs past `1.5 s + backlog / (60 x n_max) s` (backlog = stop_head - cursor at the STOP), the pod sends the stream_stopped STATUS at once and the rest is the phone's stop-tail. If not streaming: the stream_stopped STATUS goes out at once (head = current head). A disconnect or a STREAM command during the drain ends it without a stream_stopped STATUS. |
| 0x10 SLEEP | u8 mode (2 B) | 0 = sleep with motion wake, 1 = sleep with the IMU off. Runs the sleep prologue (section 6). |

- Unknown ops and wrong lengths change nothing; their WRITE is answered INVALID_VALUE.
- **cmd_seq** is the phone's: +1 per STREAM command (wraps at 8 bits). The pod copies it into
  byte 13 of every DATA frame of that stream, so the phone can tell frames of the stream it
  asked for from frames of an earlier one still in flight, without matching STATUS frames.
- **A STREAM command while streaming** re-anchors the cursor by the same rules and starts a new
  stream with the new cmd_seq. Frames of the old stream already in flight still arrive first.
- **The `lo` form matters:** `head - ring_n` in u32 would wrap during the first ~38 s after
  every boot, which is exactly when the phone asks for `STREAM_FROM(new boot, 0)` to get the
  pickup.
- **Loss is counted once per index:** the pod keeps the end of the last counted range and only
  adds samples beyond it, so a repeated STREAM_FROM over the same range doesn't double-count.
  Known gap (review, 2026-10-23): if the ring laps the cursor while frames are in flight and the
  link then drops before they arrive, the phone's STREAM_FROM asks below that mark, and those
  frames (at most 3 x n_max) are skipped without being counted. Only a slow link (MTU < 247)
  laps while connected; the phone's own overflow gap still counts every sample exactly. To fix
  with counted intervals instead of one mark, in make_fake_pod.py first (P1).

## 5. Link

- **Sizing.** `mtu_eff = min(ATT MTU, 247)` and `n_max = min(16, (mtu_eff - 3 - 6 - 14) / 14)`,
  recomputed for every frame from the current MTU. The 6 is the push's WRITE header.
  - MTU 247 gives 16 samples: a 238 B frame in a 244 B packet, exactly MTU - 3, at 13.2 frames/s
    (211 Hz) and 3.2 kB/s.
  - MTU 185 gives 11, MTU 79 gives 4, MTU 23 gives 0. USB always gives 16.
  - Keeping up needs 211 / n_max frames/s: 13 at n_max 16, but 53 at n_max 4. A slow link (40 to
    80 ms per notification) can't do that, so the ring laps the cursor over and over and every lost
    sample is counted as overflow (test bench, stop-capped-tail).
  - Real phones give MTU 247. The clamp to 247 is needed because after a pod-initiated exchange
    Bluefruit stores the peer's server_rx_mtu (e.g. 517, BLEConnection.cpp:353), while the real ATT
    MTU is 247.
- **MTU too small: DATA.** While `n_max < 4` (MTU below 79) the pod sends no DATA and sets STATUS
  mtu_too_small, so there's no quiet degrading.
  - A STREAM command is still accepted (cursor, RESYNC and hold set as usual).
  - DATA starts on its own, after a fresh STATUS, once an MTU exchange raises n_max. The phone doesn't
    resend the command.
  - mtu_too_small is normal for a moment at every connect, until the pod's own exchange at
    connect + 50 ms completes. The phone reports it as a fault only if it's still set 3 s after
    connect.
- **MTU too small: everything else.** A packet the pod sends must fit in MTU - 3.
  - A READ whose RESPONSE (8 B + the value) wouldn't fit gets BUSY instead (8 B).
  - A STATUS push that doesn't fit (it needs MTU 29) waits in the queue like one with no free slot.
  - Any other engine packet that doesn't fit (discovery answers) is dropped and counted.
  - The client retries a BUSY read every 200 ms for 3 s, then reads the MTU register (10 B, which
    fits at 23) and reports the link's MTU as too small.
- **When a DATA frame goes out:** n_max samples pending, or the oldest pending sample is 80 ms
  old (2621 ticks), or the next dt would pass 60000 ticks, or a STOP / sleep drain is running,
  or the next sample starts after an IMU restart. Behind (after an outage), up to 4 frames per
  pass; at a 30-50 ms interval with 3 notify slots that's 60-100 frames/s, so a full ring
  drains in about 6-11 s.
- **The pod never blocks on the radio.** Every packet the pod sends shares one budget of 3
  notifications in flight, counted as sent minus TX-complete. Only loop() sends, so packets go out
  in program order. Sampling never waits for the link.
  - **The engine's packets** (HELLO, RESPONSEs) go straight out, or wait in a spool of 4.
    - A full spool drops the packet and counts it in TX_DROPS.
    - A NOP only ever goes straight out: if it can't, it's dropped, since anything else queued
      already proves liveness.
  - **A push** (DATA or STATUS) goes only when all of these hold:
    - PUSH is on;
    - the spool is empty;
    - a slot is free;
    - the packet fits.
    Otherwise its frame waits where it is and is tried again on the next pass. The push is
    `[04][00][reg][frame]`, so a retry is byte-identical.
  - **Order.** A due STATUS goes before any further DATA, so the 1 s STATUS still gets out during
    catch-up, and the STATUS answering a STREAM command goes after the command's RESPONSE and before
    its RESYNC frame. The stream_stopped and going_to_sleep STATUS go after the last DATA.
  - **PUSH off** (or TX notifications off): a STATUS counts as sent and DATA waits.
  - **USB:** a 2 KB byte spool replaces the slots. Engine packets go in if there's room; pushes
    need room plus 128 B.
- **A disconnect or a session end ends the stream, not the session hold.**
  - The pod stops streaming (STATUS b3 = 0), empties the spool and forgets its in-flight count (a
    dead link reports no TX-complete).
  - On the next session it sends no DATA until a STREAM command arrives, whose first frame has RESYNC.
  - Frames sent but never delivered are lost from the link only: the phone's
    `STREAM_FROM(boot_id, next index)` gets them back from the ring.
- **Requested link** (the pod asks, the phone decides; the pod prints what it got as a
  `# link ...` line on USB, and STATUS carries the interval):
  - 1M PHY (about 3 dB more range than 2M; the data rate is tiny), +4 dBm.
  - MTU exchange (247) at connect + 50 ms if the phone hasn't done one (Chrome on Android may
    never ask), then the data length update. The MTU comes first because CRP's INFO read needs it.
  - Connection interval 30-50 ms, latency 0, supervision timeout 4 s, requested 2 s after the
    session turns PUSH on (`sd_ble_gap_conn_param_update` with {24, 40, 0, 400}).
- **Advertising:**
  - contents: flags, the 16-bit service UUID 0x5090 and the name;
  - interval: 20 ms for the first 30 s, then 152.5 ms;
  - restarts after every disconnect except the one before sleep.

## 6. Session hold and sleep (amends D26)

- **Session hold:** set by STREAM_FROM / STREAM_LIVE, cleared by STOP or a reboot. A dropout
  does not clear it. While it's on: idle sleep after 30 min still (instead of 10), and no
  lid-down gesture. A phone that dies without STOP keeps the pod awake at most 30 min. The
  3.30 V low-battery cutoff always applies.
- **Sleep prologue** (any reason): sampling stops first, so head is final. If streaming and
  connected: drain DATA up to that head for at most 1.5 s; the frame ending at the final head
  has FINAL (a drain cut short sends no FINAL frame). Then, if connected: STATUS with
  going_to_sleep, the sleep reason and head = the final head; wait at most 300 ms until nothing
  is in flight or queued (spool and STATUS queue empty); turn off restart-on-disconnect and disconnect deliberately (reason 0x13, so the
  phone sees it at once instead of after the 4 s timeout), waiting at most 500 ms for the
  disconnect to complete; then the D26 sleep. Samples between the last sent frame and the final
  head are the phone's `sleep-tail` gap.
- During the prologue the pod answers CTRL writes BUSY and does nothing (from the SLEEP op on). A disconnect during
  the sleep drain skips the STATUS and goes straight to sleep. Advertising stops as the prologue
  starts, so no phone connects while the IMU is set up for sleep.
- A sleep during a STOP drain takes it over: the drain runs to the final head instead of
  stop_head, but keeps the STOP's cap if that is sooner than 1.5 s (the phone's stop wait is sized
  on it), and a frame built but not yet sent is rebuilt, so FINAL only ever marks the frame
  ending at the final head. The going_to_sleep STATUS answers the phone's stop wait; no
  stream_stopped is sent. (make_fake_pod.py: stop-then-sleep-at-cap, stop-final-then-sleep.)
- **Wake** is a reboot (new boot_id, index 0). The ring fills from the first sample, so
  `STREAM_FROM(new boot, 0)` delivers the pickup that woke it. On USB power too: sampling starts
  before the pod's 2 s wait for a PC, and the link is served during that wait.

## 7. Phone side (the PodStream contract)

`analysis/podlink.py` is the reference (P1) and the JS PodStream ports it line for line. For the
same input (frames, commands, STOP and timer calls, in the same order) they give the same
samples, gaps, stats, faults, events and command bytes; `analysis/make_fake_pod.py` writes the
vectors that prove it.

- **Connection order, every connect and reconnect:**
  1. Bring the link up: on Bluetooth, TX notifications on (the app's plugin also requests MTU
     247 first); on USB, open the port.
  2. HELLO. Await the pod's HELLO and check device_id "BPOD".
  3. READ INFO, then READ STATUS, awaiting each. A BUSY is retried (section 5, MTU too small).
  4. WRITE PUSH = 1, and await its RESPONSE.
  5. WRITE CTRL with the STREAM command, if `command()` returns one.
- **Feeding the decoder.** Feed every packet the pod sends, in arrival order, with its arrival time
  in ms (browser `Date.now()`, app `SystemClock.elapsedRealtime()`), through `crp_frame()`:
  - an OK RESPONSE to a READ of INFO, STATUS or CAL gives its value as a frame;
  - a WRITE with txn 0 to DATA or STATUS gives its value as a frame;
  - anything else gives nothing and isn't counted.
  The frames go into one decoder.
- **Why the order matters:**
  - a STATUS pushed before the reconnect INFO would belong to the old epoch, and a woken pod's small
    head would then look like a reboot without a new boot_id (below). PUSH = 1 after the reads is
    what prevents it.
  - READ STATUS stays in the order so that `command()` already knows the pod's head when it builds
    the command.
- **Captures** (the make_fake_pod.py vector format, `scenario,step,kind,arrival_ms,payload`):
  - `pkt` rows: a packet received, hex;
  - `tx` rows: a packet sent (for reading only, not replayed);
  - `cmd`, `stop` and `call` rows as before;
  - captures from before D45 carry `frame` rows, which still replay
    (`python analysis/podlink.py --replay-all` against `analysis/data/pod-capture-manifest.json`).
- **Input checks.** A frame with an unknown type, or the wrong length for its type (DATA not
  14 + 14n with n = 1..16, STATUS not 20, INFO not 32, CAL not 64), is dropped and counted
  (`bad`). CAL is ignored in v1. Reserved bytes and unused flag bits are ignored. Scale:
  INFO's accel_lsb / gyro_lsb, or the section 1 constants when INFO's value is not finite or
  <= 0.
- **Ignoring.** The decoder ignores (and counts, `ignored`) every STATUS and DATA, and
  `command()` returns None, from any of these until the next good INFO (proto 1, boot_id != 0,
  the session's serial): an INFO with a different serial (fault `another pod`), an INFO with
  proto != 1 or boot_id 0 (fault `bad INFO (proto or boot_id)`), a boot_mismatch STATUS (below).
  The first good INFO sets the session's serial.
- **Epochs**, one per boot_id. STATUS and DATA carry no boot_id: they belong to the epoch of
  the last INFO fed in; frames before the first INFO are dropped and counted (`pre_info`). INFO
  with the boot_id of the newest epoch = a reconnect (`reconnects` + 1; the epoch continues); a
  new boot_id starts a new epoch.
- **Defensive reboot check.** Within one epoch, a STATUS whose head is below nextIdx, or whose
  ticks64(ticks_now) is more than 2 x 32768 ticks below the epoch's last sample, means the pod
  rebooted without a new boot_id (the pod sends STATUS and DATA in build order on one characteristic, so a real head
  is never below what was already delivered). That STATUS starts a new epoch (same boot_id, a
  reboot) and is its first STATUS; fault `reboot without a new boot_id`, event `need-command`
  (the app answers with `command()`, unless it's about to write one anyway in the connection
  order).
- **boot_mismatch.** A STATUS with boot_mismatch means the last INFO was stale, so everything
  since it came from another boot: restore the epoch's latest STATUS, tick reference and clock
  window to what they were at that INFO (a reconnect INFO keeps a snapshot until the first DATA
  of the next command), keep nextIdx, raise fault `boot mismatch` and event `need-info`, and
  ignore the link until INFO is read again.
- **Ticks.** Per epoch, the first STATUS sets `ref32 = ticks_now`, `ref64 = ticks_now`. Each
  later STATUS moves it: `ref64 += int32(ticks_now - ref32)`, then `ref32 = ticks_now` (int32 of
  the u32 difference: `(x | 0)` in JS). Every sample tick and every ticks_now is unwrapped as
  `ticks64 = ref64 + int32(ticks - ref32)`. DATA arriving before its epoch's first STATUS is
  held, in order, until it arrives.
- **Clock.** Each epoch keeps its own window of its last 32 STATUS frames (empty when the epoch
  starts), and `wallBase = min(arrival_ms - ticks64(ticks_now) / 32.768)` over that window (the
  lowest-latency frame wins). Every sample: `t = tOffset + ticks64 / 32768`. tOffset is set
  once, when the epoch emits its first sample, and never changes:
  - the first epoch that emits: `tOffset = -ticks64(first sample) / 32768`, so t = 0 there;
  - a later epoch: `tOffset = prev.tOffset + (wallBase - prev.wallBase) / 1000`, where prev is
    the most recent epoch that emitted a sample, with its latest wallBase. If that puts the
    epoch's first sample at or before `lastT + 0.001`, set tOffset so it lands exactly at
    `lastT + 0.001`, count `clock_clamped` and raise event `clock-clamped`.
  - Expect the new epoch to land ~10-20 ms late: right after a reconnect its window holds only
    2-3 STATUS frames, so its lowest latency is higher than the old epoch's (32 frames). The
    test bench measures +12 and +20 ms with a 7.5-37.5 ms link. It only shifts time across a
    wake (the rest timer), never samples within a boot.
  - `wallAt(t) = wallBase + (t - tOffset) x 1000` with the latest wallBase of the epoch the
    sample came from.
- **Streams.** `command()` builds each STREAM command with the next cmd_seq (the first one is
  1) and makes it the current command; it also cancels a pending STOP wait. A stream is
  **open** from the first DATA frame carrying the current cmd_seq until a STATUS with
  stream_stopped or going_to_sleep closes it.
  - DATA with any other cmd_seq is stale (a stream the phone already replaced): dropped and
    counted (`stale`), no gap, pkt_seq untouched.
  - The first frame of the current cmd_seq sets the start. After `STREAM_FROM(boot, X)`: nextIdx
    = X. After `STREAM_LIVE`: nextIdx = that frame's first_idx if the epoch has no nextIdx yet
    (no gap); otherwise it keeps its nextIdx (a jump forward is a `resync` gap, below).
  - Within an open stream, samples whose index is below nextIdx are duplicates: dropped per
    sample (`dupes`), the rest of the frame kept. Each kept sample sets nextIdx = its index + 1.
  - DATA of the current cmd_seq after its stream closed is dropped and counted (`after_close`),
    without a pkt_seq check: everything below the close's head was already counted as a tail,
    so these can only be samples given up on (a pod bug if it ever happens).
  - A sample whose t is not above the last emitted t is a pod bug: dropped, counted
    (`t_backwards`), fault `time went backwards` (once); nextIdx still moves past it.
- **Gaps are never filled.** A gap record is `{kind, n, idx, t0, t1}` (plus `lower_bound: true`
  where marked below).
  - A forward index jump (first kept sample's index > nextIdx) is a gap with n = the jump and
    idx = nextIdx (the first missing index): kind `overflow` if the frame has OVERFLOW (it wins
    over resync); `resync` if it's the RESYNC frame of a STREAM_LIVE in an epoch that already
    had a nextIdx (a deliberate skip); otherwise `unexplained`, which also raises fault
    `unexplained gap` (a protocol bug: every scenario requires 0 except the one that injects a
    lost frame).
  - A time hole with no index jump is a pair of kept samples with consecutive indices (idx =
    the previous kept index + 1), inside a frame or across frames, more than
    `1.5 x 32768 / odr_nominal` ticks apart (236 ticks at 208: one missed data-ready is a hole).
    Kind `imu-restart` if the later sample is sample 0 of an IMU_RESTART frame, otherwise `imu`.
    n = floor(dt x odr_nominal / 32768 + 0.5) - 1 (an estimate; dt in ticks; .5 rounds up, like
    JS `Math.round`), idx = the later sample's index.
  - t0 and t1 are the t of the kept samples on either side. With no earlier kept sample in the
    epoch, t0 = the previous emitting epoch's last t, or t1 if there is none.
  - **Closing.** A STATUS with going_to_sleep always closes the open stream (the ring dies with
    the sleep): if `head - nextIdx > 0`, a `sleep-tail` gap. A STATUS with stream_stopped closes
    it with a `stop-tail` gap the same way, except when it belongs to a stream the phone already
    replaced: if the current command hasn't started, the open stream was opened by an older
    cmd_seq, and a STOP is still owed its answer after a later STREAM command overtook it
    (`stale_due` > 0: +1 when command() cancels a pending STOP; reset to 0 at a same-boot
    reconnect INFO, a new epoch and the current command's first DATA), it's dropped and counted
    (`stale_stops`, `stale_due` - 1) and changes nothing (the new STREAM_FROM fetches those
    samples again). Without that STOP it is the answer to a STOP sent after the command: after a
    reconnect a capped STOP (n_max < 4 before the pod's own MTU exchange) is answered before the
    new stream's first DATA, and closes it (step 10 review). A tail gap has idx = nextIdx, n = head - nextIdx, t0 = the
    last kept sample's t, t1 = the t of that STATUS's ticks_now (unwrapped and mapped like a
    sample); if the epoch never emitted, t0 = t1 = null. Then nextIdx = head. Every closing
    STATUS counts `stopped` and raises event `stopped` (`stop` / `sleep`); going_to_sleep also
    raises `sleep` with the reason.
  - **Lost tail.** A new epoch starting while the old epoch's stream is open (the pod slept or
    reset while disconnected) records a `lost-tail` gap in the old epoch from nextIdx to its
    latest STATUS head, `lower_bound: true` (the phone can't know how far the pod got).
  - **Owed tail.** A reboot epoch (a woken or reset pod) that never streamed (no nextIdx, not
    open) but had a STATUS still owes everything from index 0, its pickup: when a new epoch
    replaces it, or at `finish()`, it records a tail gap idx 0, n = its latest STATUS head,
    t0 = t1 = null, `lower_bound: true` (`stop-tail` at finish if a STOP was pending, else
    `lost-tail`), then nextIdx = head. Without it, a link that drops between a woken pod's
    STREAM_FROM(new boot, 0) and its first DATA would lose the pickup uncounted (step 10 review).
- **pkt_seq**: kept per epoch, starting undefined. For every non-stale DATA frame of an open
  stream (fully duplicated ones included): a RESYNC frame or an undefined prev just sets prev.
  Otherwise pkt_seq must equal `(prev + 1) & 0xFFFF`; if not, count one `pkt_seq_gaps` and add
  `(pkt_seq - prev - 1) & 0xFFFF` to `pkt_seq_lost`. Then prev = pkt_seq.
- **What to send** (`command()`): a fresh session `STREAM_LIVE(600)`; after a reconnect
  `STREAM_FROM(boot_id, nextIdx)`, or `STREAM_LIVE(600)` again if the epoch has no nextIdx yet
  (the link dropped before the first frame); after a reboot `STREAM_FROM(new boot_id, 0)`.
  Returns None while ignoring (above).
- **Stop.** `stop_command()` returns the STOP bytes and starts the stop wait. The wait ends on
  the next closing STATUS (event `stopped`) or when a STREAM command cancels it. Wait
  `2.5 s + (max(0, head - nextIdx) + 1.2 x odr_nominal) / (60 x n_per_packet) s`, with head and
  n_per_packet from the latest STATUS (the pod's cap plus 1 s, with the head up to 1 s stale);
  2.5 s when n_per_packet < 4 (the pod answers at once) or nextIdx is unset. On timeout
  (`stop_timed_out()`): fault `stop timed out`, and the stream **stays open**, so a late
  stream_stopped still closes it exactly. `finish()` (the app ends the session or tears the link
  down) closes a still-open stream with a tail up to the latest STATUS head, `lower_bound:
  true`: kind `stop-tail` if a STOP was pending, else `lost-tail` (and records an owed tail, see
  Closing).
- **Counters** (all start at 0): samples, bad, pre_info, ignored, stale, stale_stops,
  after_close, dupes, t_backwards, pkt_seq_gaps, pkt_seq_lost, reconnects, epochs,
  clock_clamped, stopped. **Faults** (strings, in order): another pod, bad INFO (proto or
  boot_id), boot mismatch, reboot without a new boot_id, unexplained gap, time went backwards,
  stop timed out. **Events**: epoch, gap, fault, stopped, sleep, need-command, need-info,
  mtu-too-small, clock-clamped. The app, not the decoder, turns a mtu_too_small still set 3 s
  after connect into a user-facing fault (the decoder has no connect time).
- **Output:** D4 samples `{t, ax, ay, az, gx, gy, gz}` in index order. t is in seconds on the
  device clock, t = 0 at the first sample the PodStream emits (for STREAM_LIVE(600) the oldest
  lead-in sample, about 2.8 s before Start, not the Start press).
