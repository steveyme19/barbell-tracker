# Decisions

A running log of design choices and why. When you change your mind, don't delete
the old entry. Add a new one that says what changed and why.

## Decided

**D1 (2026-10-14): Phone tests use phyphox "Acceleration with g".**
The "without g" version is Android's own sensor fusion with unknown filtering. The
barbell device only gives raw data, so learn on raw data.

**D2 (2026-10-14): Drift correction anchors only on detected stillness. The bottom of
a rep is computed, never forced to zero.**
*Was:* zero-velocity correction at the top and bottom of each rep.
*Problem:* the bottom of a touch-and-go rep isn't still. Velocity crosses zero for an
instant, and the only way to find that instant is from the drifting velocity, which is
already zero there by definition. Forcing it to zero adds no information and dumps the
drift that already built up onto the concentric. On a fake squat with 0.05 m/s² bias,
it made MCV about 6% high while top-to-top correction was exact
(`analysis/demos/bottom_anchor.py`).
*Now:* anchors come from stillness (gyro quiet + |accel| ≈ g), which is independent
evidence. Bottoms are found as velocity zero crossings after correction. See
algorithm.md §5 to §7.

**D3 (2026-10-14): Full orientation tracking on all bar data. No fixed-axis shortcuts.
Lever-arm compensation for any off-axis mount.**
*Problem:* sleeves spin on their own bearings and the shaft rolls. A device on either
can roll around the bar axis mid-rep, so no sensor axis stays pointing up. If the IMU
is also off the roll axis, rolling adds centripetal and tangential acceleration that
orientation tracking doesn't remove (1.4 m/s² at one rev per second, 35 mm off axis).
*Now:* 6-axis Madgwick always (algorithm.md §2). Either mount the IMU on the roll axis
or subtract the lever-arm terms using the gyro and the known mount geometry (§3).
Phase 0.3 tests rolling, not just tilting. Phase 2.1 logs peak gyro rate to confirm
the range.

**D4 (2026-10-14): The web app is source → algorithm → UI, with one shared sample
format. Algorithm tuning happens only in Python.**
*Problem:* Chrome's DeviceMotion is rate-limited (typically ~60 Hz). Fine for UI work,
too slow to tune an algorithm on. And if the phone sensor is wired straight into the
app, switching to Bluetooth later means rewriting it.
*Now:* every source (phone sensor, CSV replay, BLE) emits the same sample format. The
`ReplaySource` lets the JS algorithm run on full-rate recorded files, and a parity test
holds it to Python's results (roadmap 0.5). Swapping to BLE in 4.1 touches one file.

**D5 (2026-10-14): The custom PCB is a carrier board for the XIAO. No chip-down nRF52840.**
*Problem:* designing your own nRF52840 board means RF matching and antenna layout, a
deep rabbit hole with a high chance of a board that barely transmits.
*Now:* solder the XIAO onto the carrier by its castellated pads. The carrier handles
battery, power, and mounting. If the XIAO is ever too big, step to a pre-certified
nRF52840 module, still not chip-down.

**D6 (2026-10-14): Bar path is the 2D *shape* of the bar's movement, in the plane
perpendicular to the bar. It never claims where the bar is relative to your body.**
*Why:* the IMU only knows how the bar moved from where it started, not where your feet
are, so "bar over midfoot" isn't measurable. The plane comes from the bar axis (mount
geometry or roll gesture), so no magnetometer is needed. Horizontal is far more
sensitive to orientation error than vertical (~1.5 cm of path error per degree on a
2 s rep), so it gets validated on its own in phase 3. See algorithm.md §11.

**D7 (2026-10-14): Consistency is measured against your own reps, in real units,
above a measured noise floor.**
*Why:* "matches your usual rep" is measurable, "matches ideal technique" mostly isn't
(see D6). Fatigue is kept separate (speed is reported as velocity loss, not
inconsistency). No 0-100 score until the noise floor (Smith machine, roadmap 3.5) and
real-fault tests (3.6) show what size differences mean anything. Depth is the one
"correct" check, using a filmed reference rep. See analytics.md §2 and §3.

**D8 (2026-10-14): RPE is estimated from an individual, per-lift RIR-velocity
relationship, shown as a range. Squat and bench first, deadlift low confidence.
Logging starts at the first bar session.**
*Why:* research says group equations don't work and individual ones get within about
1 to 2 reps, best near failure, with weak results for deadlift. The model needs months
of labeled sets, so the log (`analysis/data/sets_log.csv`) has to start long before the
model gets built. See analytics.md §5 and §6.

**D9 (2026-10-16): Phone recordings use total acceleration magnitude as the
vertical signal, not the z axis.**
*Why:* phyphox's "Acceleration with g" has no gyro, so there's no orientation
tracking. Magnitude `sqrt(x² + y² + z²)` doesn't care how the phone is tilted, as
long as the motion is mostly vertical. On the real lifts it fixed the worst one
(+10.5 cm → -1.0 cm) and fixed the fake 30° tilt case completely. The bar device
will have a gyro and do proper orientation tracking instead (D3).

**D10 (2026-10-16): No-pause sets level the lockouts.**
With 3+ lockouts between two still periods, the drift correction assumes every lockout
is at the same height and fits a cubic velocity drift to make that true (algorithm.md
§6b). *Why:* on your no-pause phone set the bottoms (not in the fit) went from 12.2 cm
spread to 2.8 cm, and on fake sets with curved drift the worst MCV error went from 18%
to 0.4%. Reps that miss the common height by 2+ cm get flagged "uneven lockout"
instead of being trusted. Tops only for now, because squat depth varies. Bench and
deadlift could add their bottoms later.

**D11 (2026-10-16): The app asks which lift, and the drift fix uses it.**
Phone test / squat / bench / deadlift. Every lift repeats its lockout height. Bench
(chest) and deadlift (floor) also repeat their bottom, and barbell sets start and end
at lockout or floor height. *Why:* on fake no-pause sets with wobbly drift, the bench
profile cut the worst-rep error from 7.0 to 2.8 cm and deadlift from 8.9 to 4.9 cm,
better in 10/10 sets each. Squat bottoms stay free because depth is something to
measure, not assume. The lift goes in the CSV file name so Python knows it too, and
the per-lift load-velocity and RPE models need it later anyway.

**D12 (2026-10-17): Android app = Capacitor wrapper around `webapp/`, built on the PC
with Claude Code.**
*Why:* the browser gives the phone sensors at ~60 Hz rounded to 0.1, and Web Bluetooth
needs the screen on and a tap to reconnect. A native wrapper gets full-rate sensors and
native BLE while keeping the same JS, so `reps.js` and the Python parity test stay as
they are. A full Kotlin rewrite would throw that away. The `webapp/` code itself stays
plain JS with no build step (P3); only the Android shell has a build. Building needs
Android Studio, Gradle and the phone on USB, so that work runs in Claude Code on
Quentin's PC. Plan: `docs/android.md`.

**D13 (2026-10-16): The Android shell's Gradle wrapper is 9.1.0, not Capacitor's 8.14.3.**
*Why:* the only JDK on this PC is Java 25 (Temurin, and Android Studio's bundled JBR is
25 too). Gradle 8.14 can't run on Java 25; 9.1 is the first release that can. AGP stays
at Capacitor's 8.13.0 and builds fine on it. The alternative was installing a second JDK
(21) and pointing `JAVA_HOME` at it per build, which is one more thing to forget. If a
later `npx cap` update rewrites the wrapper, set it back to 9.1.0 or newer.

**D14 (2026-10-16): Rep boundaries treat |v| <= 1e-9 m/s as zero (`V_ZERO`).**
*Why:* on the browser squat set, v at an anchored window end was -5.6e-17 in JS and
+5.6e-17 in Python. `v <= 0` then ended rep 2 one sample apart (17 ms at 60 Hz), and MCV
differed by 0.0054 m/s, just over the parity tolerance. 1e-9 m/s is far below any real
speed, so it's not a threshold in the §8 sense, just "zero up to roundoff".

**D15 (2026-10-16): Still-period hump limit 4.5 -> 12 cm/s.**
*Why:* on the native squat set, the lockouts of reps 2 and 4 were held 1 to 2 s and
failed the 4.5 cm/s hump test (7.5, 8.9), so they weren't anchors. That left a 9.6 s
window whose drift showed up as (1) lockouts 9 cm off and (2) velocity creeping at +2
cm/s while sitting on the box, so reps started ~0.5 s early and MCV read 0.31 and 0.38
instead of ~0.45 and 0.61. With the limit at 12, only those two stills change across
every recording on file. Coasts are still rejected by the coasting check, and the sim
grinder (21) by the hump. No start-rule change was needed: with the tops anchored,
every rep starts within 0.12 s / 0.5 cm of the drive.

**D16 (2026-10-16): The app saves CSVs with its own plugin method, not @capacitor/filesystem.**
*Why:* @capacitor/filesystem 8 is Kotlin and asks Gradle for a Java 21 toolchain
(`jvmToolchain(21)`); this PC only has Java 25 (D13), so the build failed. The options
were Gradle downloading a JDK 21, installing one, or writing the file ourselves. Saving
one text file to Documents/BarTracker is ~15 lines of Java in `BarSensorsPlugin`, needs
no permission on Android 11+, and adds no dependency. `@capacitor/share` (plain Java)
stays for the share sheet.

**D17 (2026-10-16): Session mode. The app finds sets and rest by itself.**
*Why:* Quentin wants to clip the sensor on, do the whole workout and walk away, not tap
Start/Stop/Save per set. A set = motion with 15 s of quiet around it that the rep
algorithm finds reps in (algorithm.md section 13). Python is still the reference
(`sets.py`), and the live JS tracker must match it (session parity). Saving: every set's
raw CSV on its own (`bar-<lift>-<stamp>-setNN.csv`, so Python reads the lift from the
name as always), plus `session.csv` per workout with one row per set (lift, reps, rest,
best/last MCV, velocity loss, RPE estimate, weight and your RPE) and no raw data. Rest
time between sets isn't stored: at 400 Hz an hour is ~85 MB. The lift is a sticky picker,
fixable per set afterwards (the set gets re-analyzed and re-saved under the new name).

**D18 (2026-10-16): RPE estimate v0, uncalibrated, until failure sets exist.**
*Why:* Quentin asked for an RPE estimate now; Model v1 (analytics.md section 5) needs
his own sets to failure first. v0: RIR = (last rep MCV - v1RM) / DV_PER_REP, v1RM from
the research in section 5 (squat 0.23, bench 0.10, deadlift 0.14 m/s), DV_PER_REP a
rough starting guess (0.04, 0.035, 0.04 m/s per rep in reserve). Shown as a range of
+/-1, clamped to 5..10 ("<=6" when it's light). Phone tests get none. Expect it to be
off for you specifically; it gets replaced by your own fit, and comparing it with your
own RPE calls (the "Your RPE" box) is exactly the data that fit needs.

**D19 (2026-10-16): You type the exercise; its name picks the tracking profile.**
*Why:* Quentin wants to log real exercise names and weights, and to try session mode on
gym machines before the hardware exists. The algorithm still needs to know which heights
repeat (LIFTS), so the name maps to a profile: contains "squat" / "bench" / "deadlift"
-> that lift (RPE estimate on), "phone" -> phone test, anything else -> **machine**: a
weight stack, which rests on the stack every rep (a hard stop, like the floor) and before
and after the set, so `bottoms=True, ends=True` like deadlift, no RPE estimate. The
12 cm rack-move floor now covers machines too (a fake 1-rep "set" from bumping the
stack showed up in testing without it). Set files are `bar-<profile>-<stamp>-setNN-<exercise>.csv`;
`lift_from_name` reads the profile from the word after `bar-` first, so the exercise
part can't confuse it. Known misfits: an RDL maps to deadlift (its bottom isn't a hard
stop), and hack squat / leg press map to squat / machine but move on an incline, which
the vertical-only tracking can't measure right.

**D20 (2026-10-17): Stillness limits match the sensor's own noise; reps can start at the
usual bottom.**
*Why:* Quentin's cable lat raise set (phone on the weight stack) came out as 3 reps of
77, 34 and 39 cm, with height climbing to 115 cm. The lowering was smooth enough to
pass the fixed stillness limits (made for a shaky hand) and got zeroed. At rest on the
stack the sensor wiggles ~0.016 m/s², those fake stills 0.08 to 0.12. Limits are now 4x
(wiggle) and 5x (average) the wiggle inside the longest still, between 0.06 / 0.08 and
the old 0.15 / 0.3. Hand-held recordings sit at the old limits, so nothing changes for
them; the fakes and the phone-on-a-table tests got slightly better. Separately, rep 1
included taking up the cable (24.5 cm vs ~19 for the rest), so for bench, deadlift and
machine a rep may start at a stop within 5 cm of the usual (median) bottom. The bar
device will sit as still as the stack, so it needs the tighter limits too.

**D21 (2026-10-17): The sensor decides machine or free weight; real use is never hand-held.**
*Why:* Quentin: the app should work out on its own whether it's on a machine, and in
real use the sensor is never in a hand. Since D19 the typed name decided: squat, bench
and deadlift by name, everything else "machine", so a barbell row, OHP or curl got the
machine's "bottoms repeat" assumption. Now the gyro decides for every name except
squat, bench, deadlift and phone (those still win: a Smith squat rides on rods but the
depth is still yours). A stack moves in a straight line without turning (lat raise: 0.7
deg/s, sideways ratio 0.20); every hand-held recording turns 10 to 25 deg/s. Threshold
3 deg/s and 0.35. Rest stillness can't do this (phone-on-a-table tests rest as quietly
as the stack), so it stays what it is since D20: the source of the stillness limits.
Unknown free weights get a new `free` profile (lockouts and end stills only). No gyro
or under 1 s of motion: the name decides, as before. All recordings give the same reps
as D20. Untested on a mounted bar; revisit with the first bar sessions (2.1).

**D22 (2026-10-17): Load-velocity profile from best-rep MCV; e1RM only for squat, bench and deadlift.**
*Why:* roadmap 4.3. One point per set, (weight, best-rep MCV): the fastest rep is the
least fatigued, the usual way to build a profile. MCV, not MPV (settles O2 for now): it's
what the app computes and what the 1RM velocities in analytics.md section 5 are. Straight
line by least squares over the same exercise name in the last 60 days plus today, needs 2+
weights. e1RM = where the line reaches v1RM (squat 0.23, bench 0.10, deadlift 0.14 m/s);
machines and unknown free weights have no published v1RM, so they get the profile only
("usual speed at this weight", fitted without the set being compared). No e1RM past 2x
the heaviest weight used: that far out a 0.02 m/s error moves it by tens of pounds.
Reference `analysis/lv.py`, port `webapp/lv.js`, parity-tested.

**D23 (2026-10-17): Phone accelerometer calibration, applied at the source.**
*Why:* algorithm.md section 1: a scale error goes straight into velocity and ROM and the
drift fix can't see it. 6 still faces (Tools > Sensor calibration), per axis
scale = (up - down) / 2g and bias = (up + down) / 2 with g = 9.80 (West Lafayette), fit
rejected if it's off by more than 0.1 m/s^2 or 10 %. Reference `analysis/calib.py`, port
`webapp/calib.js`, parity-tested; the collector was checked on a synthetic stream with
planted errors (recovered to 0.01 %). It's applied to every live sample before anything
else, so live numbers, saved sets and Python agree. That bends P2 (raw files): each
session folder gets the `calibration.json` it used, so raw = calibrated x scale + bias.
Gyro scale needs a known full turn, not this.
First run on the Pixel 9 Pro XL (2026-10-17, in its case): scale -0.07 / -0.14 / -0.04 %, offsets
under 0.04 m/s^2, fit error 0.029 m/s^2. The corrections are the size of the fit error (case
and edges not perfectly flat): this phone is factory-calibrated well, so scale isn't what
limits its accuracy (drift and rep boundaries are). It matters for the XIAO (roadmap 1.3).

**D24 (2026-10-18): XIAO firmware uses the "Seeed nRF52 Boards" package (non-mbed). Closes O4.**
*Why:* BLE later (A4, roadmap 4.1) is easiest with the Adafruit Bluefruit library that
comes with it, and it's the low-power one. The IMU works on it too: its Sense variant
defines `TARGET_SEEED_XIAO_NRF52840_SENSE`, so the Seeed LSM6DS3 library (2.0.7) talks to
the IMU's own bus (Wire1) and powers it up (pin 15). Sketches need
`#include <Adafruit_TinyUSB.h>` for USB serial. First sketch `firmware/imu_serial`:
+-8 g, +-1000 deg/s, 208 Hz on the IMU's own clock (polls the data-ready bits, one
burst read per sample), device timestamps from `micros()`; `analysis/log_serial.py`
saves it as a normal recording. (Timestamps changed to RTC2 the same day, D25.)

**D25 (2026-10-18): XIAO timestamps come from RTC2 on the 32.768 kHz crystal.**
*Why:* velocity is the integral of acceleration over time, so a clock that's x% off
makes MCV x% off (and ROM about 2x% off). Tried three clocks on board 1:
- `micros()`: on this core it's the 1024 Hz RTOS tick (`delay.h`), so every dt came out
  as 3.906 or 4.883 ms. Right on average, ~0.5 ms jitter per sample.
- The IMU's own timestamp counter (TIMER_HR, "25 us"): smooth, but it said 206.0 Hz
  while the crystal and the PC both said 211.8 Hz. It runs ~2.8% slow (untrimmed RC
  oscillator), which would put MCV ~2.8% high.
- RTC2 at prescaler 0 (RTC0 is the SoftDevice's, RTC1 FreeRTOS's, RTC2 is free; the
  variant runs the LF clock on the crystal, `USE_LFXO`): 30.5 us steps, ~20 ppm.
  211.81 Hz against ~211.9 Hz by the PC clock, dt std 0.034 ms.
The sketch also waits for data-ready in a tight loop inside `loop()`, so each sample is
stamped within one status read (~0.1 ms) of being ready. The IMU's sample rate itself
differs per board (211.8, 209.3, 210.4 Hz on boards 1 to 3, all "208"), which is fine
because every sample carries its own time (D4); nothing should assume 208 Hz.
Recordings made before this: `-1950-desk` (micros, quantized) and `-1954-timing` (IMU
counter, 2.8% slow).

**D26 (2026-10-19): device power management in firmware/pod.**
- *Sleep = System OFF* (a reset on wake), woken by the IMU's own wake-up detector on INT1 (P0.11)
  at 12.5 Hz low-power (~9 uA): the nRF needs nothing running to watch for the bar moving.
- *Auto-sleep after 10 min still*, not less: rests between heavy sets run 3 to 5 min.
- *Manual sleep = lid-down and still for 6 s* instead of a double-tap: a rerack clang or a plate
  hitting the bar can look like a double-tap, but the pod is never lid-down on a sleeve end
  (Adapter A). On the shaft saddle (Adapter B) a rolled bar could do it, hence the long hold; and
  it's off while a PC has the port open (recordings, calibration faces).
- *Low battery: 3.30 V, 3 readings 5 s apart, on battery only* -> sleep with the IMU off; only
  USB or reset wakes it, so the cell can't be run flat. Charging stays at 100 mA (0.25 C on
  400 mAh) so a flat pod is full in ~4 h.
- *Wake threshold 2* (62.5 mg on the slope filter), from real pickup data, not the datasheet
  alone. Details and bench results: firmware/README.md.

**D27 (2026-10-19): the pod talks BLE with PodLink v1 (spec: docs/ble.md).**
*What:* a custom GATT service (DATA, CTRL, STATUS, INFO, CAL stub), raw int16 samples with a
32-bit sample index and crystal ticks, 16 samples per notification at MTU 247. The pod keeps a
38 s ring; after every (re)connect the phone asks to resume from an index, so short dropouts
lose nothing and longer ones lose an exact, counted number of samples.
*Why:*
- Roadmap 1.4 asks to prove no samples are dropped, and reps.js would quietly integrate across a
  hole, so loss has to be counted and flagged at the source, not guessed from timing.
- Raw counts + device ticks keep D4/D25 exact: BLE data equals the USB CSV, nothing assumes 208 Hz.
- Phone side: one decoder (`PodStream` in sources.js, `podlink.py` as the reference, P1). The app
  gets its own plain-Java Capacitor plugin, a dumb pipe that never parses frames. The community
  plugin (`@capacitor-community/bluetooth-le`, named in android.md A4 before) is out: Kotlin, needs
  a bundler (P3), one event per notification, reconnects from JS.
- Link: 1M PHY, +4 dBm, 30-50 ms interval, 4 s timeout (range in a gym full of steel over speed).
  The pod never blocks on the radio; sampling never waits for BLE.
- Amends D26: from a STREAM command until STOP (a dropout keeps it), idle sleep after 30 min
  (not 10) and no lid-down gesture; sleep first drains what it can in 1.5 s, tells the phone why
  and where the stream ended, and disconnects.
- Each STREAM command carries a cmd_seq that every DATA frame echoes, so the phone never has to
  guess which frames answer which command.
*Calls made with Quentin:* the 30 min hold; `pip install bleak` for the laptop receiver; app on
Android 12+ only; no pairing in v1; pod data uncalibrated (~3% high) until roadmap 1.3.
Build order and tests: docs/ble-plan.md.

**D28 (2026-10-23): every BLE notification goes out on DATA; the pod reads the IMU with
hardware-sequenced I2C.** Two findings from the first BLE build (ble-plan step 5), both measured
on the bench with the laptop as the receiver.
*What:*
- STATUS frames are notified on the DATA characteristic. STATUS stays readable (read only, no
  notify). The phone subscribes to DATA only. Decoders unchanged: every frame already starts with
  its type and goes through one decoder in arrival order.
- The pod's sample poll reads the IMU through `imuRead()` in pod.ino: TWIM1 runs the whole
  transaction (address, repeated start, read, stop) on hardware shortcuts. Wire stays for setup.
*Why:*
- Windows (WinRT: bleak, and Chrome on Windows) handed over a STATUS after a DATA frame the pod
  had sent later, about 1 in 1500 notifications. The decoder read the old head as a reboot
  without a new boot_id and asked for index 0: 17354 samples counted as lost that weren't. Over
  the air the order was right (one SoftDevice queue). Within one characteristic Windows kept the
  order: thousands of frames, 0 pkt_seq breaks.
- With the radio on, the poll lost about 1 sample every 2 s, while only advertising too. Measured:
  no CPU stall (status polls never more than 2.4 ms apart), no I2C error; one of the IMU's two
  data-ready flags skipped a period. The rate followed radio activity (20 ms fast advertising vs
  152.5 ms slow) and not TX power (+4 and -20 dBm the same). Cause: Wire triggers STOP from the
  CPU after the last byte; a SoftDevice or BLE-task preemption right then holds the bus with SCL
  low, and the IMU skips that output update. With the shortcuts: 0 misses.
- The GATT table freezes at this build, so the STATUS change had to land now.
*Also measured (Windows 11, Intel radio):* MTU 247, data length 251, 45 ms interval, and Windows
moves the link to 2M PHY by itself; the pod accepts it ("stay on 1M" only means the pod never
asks). Bluetooth audio on the same laptop (headphones) starves its BLE scanning: the pod was
invisible until they were disconnected.

**D29 (2026-10-24): the pod samples on the IMU's data-ready interrupt, in a top-priority task
that owns the IMU's I2C bus; a watchdog resets a stuck loop().**
- The IMU pulses INT1 (P0.11) at every gyro sample (pulsed data-ready, DRDY_PULSE_CFG_G 0x0B bit
  7, checked against ST's `lsm6ds3tr-c_reg.h`; INT1_CTRL 0x0D = 0x02). The GPIOTE interrupt stamps
  RTC2 and wakes `samplerTask` (FreeRTOS priority 4, above Bluefruit's BLE task, USB and loop()),
  which reads the sample with `imuRead()` (D28) and stores it in the ring. A sample's time is the
  pulse's, never when a task got round to it.
- loop() only sends: the USB CSV from its own cursor (only while the host takes it), the BLE
  frames, the `#` lines (non-blocking, dropped and counted when USB is full). Anything else that
  needs the IMU's bus goes through the sampler (`samplerPark()` before sleep).
- The hardware watchdog (8 s, paused while halted for a debugger, running in sleep) is fed only
  from loop(). The reset banner says WATCHDOG.
*Why:*
- The busy poll could only be as good as one status read plus whatever preempted it: dt std 0.042
  to 0.056 ms (steps 4-5), over the 0.05 target with the radio streaming. On the interrupt: 0.0168
  ms over 10 min with BLE streaming and a 30 s link drop, 0 missed, 0 BLE samples missing.
- A host that holds the port open without reading blocked `Serial.print`, so it stopped sampling
  and the radio with it. Now it costs only that host's CSV lines (`csv lost`).
- Step 4's bench hung the board for good once (a powered-down IMU holding the bus, most likely).
  Nothing noticed. The watchdog turns any such hang into an 8 s reset and a new boot the phone
  sees (boot_id).
- The CPU now idles between samples (tickless `delay(5)` in loop()). The current saving is an
  estimate until it's measured.
*Gotcha found on the way:* after reset and after `detachInterrupt()` the nRF leaves a pin's input
buffer disconnected, so `digitalRead(INT1)` always read LOW. D26's dry-run "quiet" results and its
"INT1 stays low" re-wake check saw nothing; the settle delay was the real fix. Fixed with
`pinMode(INT1, INPUT)` before both.

**D30 (2026-10-26): a session exports as one JSON file with its raw sets in base64.**
- `{ format: "bartracker-sessions", version: 1, exported, sessions: [{ record, sets: [{ index, n,
  cols: { t, ax, ..., gz }, tAbs }] }] }`: `record` is exactly what IndexedDB keeps (the history
  list, resume); each column is `{ type: "f64" | "f32", b64 }`, the stored typed array's bytes
  (little-endian). Writer and reader: `webapp/export.js`; Python: `analysis/sessions.py`.
- In the app, Export writes `session.json` into the session's own Documents/BarTracker folder,
  next to its set CSVs; Export all does that for every stored session. In a browser it's a
  download (one file, or all sessions in one). Import adds sessions not stored yet and leaves one
  with the same id as it is; an imported session counts as ended (never offered for resume).
  Delete takes a past session out of IndexedDB only: its files stay.
*Why:*
- Bit-exact: re-importing gives the same stored samples, so a past session re-analyzes to the same
  reps (P2: any session can be re-run with a newer algorithm). Decimal JSON would round, and is
  2.5x the size (205 kB against 506 kB in webapp/test/export.mjs).
- One file per session in its own folder: pulling a session folder into analysis/data is then
  complete, and no 50 MB string crosses the Capacitor bridge for an "export all".
- The format names itself and its version, so a later change can read old exports or refuse them
  with a reason instead of misreading them.
*Note:* the store keeps the sensor readings as float32 (t and tAbs float64), so a past session
re-analyzes within about 1e-8 m/s MCV and 1e-6 mm ROM of what the live session showed (measured on
the fake squat session), same reps.

**D31 (2026-10-26): a closed set drops what handling the sensor adds: end reps too big to be
reps, and reps with no end anchor.**
- `drop_rack_moves` (sets.py, dropRackMoves in sets.js) first drops end reps over RACK_MAX_ROM
  (squat and deadlift 1.0 m, bench 0.8 m) or over RACK_MAX_FRAC = 2x the median of the set's
  other reps (the only test for machine, free and phone), then the small rack bumps as before,
  with the floor from the reps that are left.
- `closed_set_reps` / `closedSetReps`: a closed set (sets.py find_sets, the live tracker's
  closeSegment, and the app re-analyzing a stored set) keeps only reps with an end anchor. The
  live view still shows the rep in progress.
*Why:* a set stays open 15 s after its last rep, and that's when the pod gets unclipped or the
phone picked up. Quentin's real pod set (bar-deadlift-2026-10-23-1857-set01) read 6 reps, the
6th 1.98 m at 0.51 m/s with no end anchor: best MCV 0.51 (really 0.24), velocity loss 0%
(really 21%), RPE <=5 (really 8-10), and that best MCV went into the load-velocity profile. With
the median taken over all reps, a heavy single followed by handling ([0.55, 1.98] m) even dropped
the real single as a "rack move". Now 5 reps, 0.24, 21%, 8-10. A closed segment always has 2 s of
still after its last active block, so an unanchored rep there only comes from motion that ran up
to the cut.
*Left open:* moving the pod to another bar can make an anchored phantom set of normal size, which
no threshold can tell from a real single: that needs a "not a set" control on the set card.
Both cases are in parity.mjs with their known answers (the bug was shared by Python and JS, so
their agreement alone couldn't catch it), plus 7 known answers for the rack filter itself.

**D32 (2026-10-26): a rep's start and end walk past a too-short pause's drift: look where v <=
3% of the rep's peak, then walk on while v keeps falling (to v = 0, or to the pause's start).**
- `find_reps` (reps.py) / `findReps` (reps.js): V_STOP_FRAC = 0.03, for lifts other than "phone"
  and data at WALK_MIN_FS = 150 Hz or more. 0 restores the old boundaries exactly.
*Problem:* a hold at lockout or on the chest under 0.25 s (or shaky) isn't a still, so v there is
left-over drift a hair above 0, and "first v = 0" ran through the pause: the rep's duration took
it in. The repo's own fake session (0.25 s holds): 7 of 20 reps 5 to 24% low against the truth.
Known curves (demos/boundary_pauses.py): -8 to -50% with +0.005 m/s of drift. Quentin's pod set:
rep 5 read 0.186 m/s with a 0.43 s lockout hold in it (the bar moved 5.4 mm).
*Why the walk and not a threshold:* section 8 rules out a velocity threshold (a fixed one biases
grinders: +19% at 0.05 m/s, demos/threshold_bias.py), and a relative one (3% of the peak) still
biases every clean rep +1 to +6%. The walk only uses 3% to know where to start looking; the
boundary is the real zero when v reaches it, so a clean rep, a sticking dip and a lockout stall
come out exactly as before. Where it can't help (drift over 3% of the peak) it gives today's
boundary. On the parity files every change on bar data is a pause: the bar moved 0 to 5.4 mm
between the two boundaries.
*Limits:* hand-held phone tests keep the old rule (their tremor looks like a pause), and so does
data under 150 Hz: on Chrome's 60 Hz with its jittery timestamps the walk stopped short (the same
squats read +34% against the native 402 Hz recording). Phase 3's video is the real check.
*Review this one:* it changes MCV (the main number) on paused reps, made overnight from synthetic
truth plus the parity files; the pod and native recordings support it, but no real paused set
with a reference exists yet.

**D33 (2026-10-26): how the 6-axis Madgwick (D3) runs: the accelerometer only steers it at a real
still; while moving, the bias-corrected gyro alone.**
- analysis/orientation.py: beta = 0.2 at a still (its accel agreeing with the estimate within 3
  degrees, or a still of 1 s or more), 0 while moving; the gyro bias is the median over the stills;
  each step turns by the mean of two gyro samples (the trapezoid). Stills come from the accel vector
  and the gyro, then confirmed per world axis (a mean of ~0, and reps.confirm_still_runs' coast check).
*Why (all measured on make_fake_motion's exact physics):*
- Madgwick's correction is beta times a *normalized* gradient: once the true error is under the
  accel's noise it still pushes at full strength in a noise-picked direction. beta 0.005 while
  moving added 0.45 degrees over a 1.2 s sleeve spin that the bias-corrected gyro alone tracked to
  0.04. A scale error after many turns needs roadmap 1.3's gyro calibration, not a pull.
- The end-point gyro reading lags the attitude by half a sample: 1.4 degrees at 560 deg/s.
- A steady push (a min-jerk slide's acceleration plateau) reads quiet, so a quietness-only still
  test called it still: the filter tipped 4.9 degrees and the path zeroed v mid-slide (283 mm off).
  The agreement gate and the world-axis confirm fixed it (2.7 mm).
- |a| is blind to horizontal motion (D9's table), so a slide had no still periods at all.
*Open:* hand-held data (pauses too shaky for the vector test), real bar data (phase 2), and whether
orientation beats the magnitude for reps.py's vertical (section 4's table) are all still to see.

## Proposed (say yes or no)

**P1: Python is the reference implementation.** Any port (JS in the web app, C on the
device) gets checked against Python on the same recorded files. If they disagree,
Python wins until proven otherwise.

**P2: Keep every raw recording in the repo.** CSVs are small. The algorithm will
change many times, and raw data lets you re-run old sessions with the new version.

**P3: Web app is plain JavaScript, no framework, no build step.** GitHub Pages serves
the files as-is, nothing to configure, and you learn the actual browser APIs.

## Open

**O1: Where does the per-rep algorithm run?**
- Phone (JS): stream raw IMU over BLE, compute in the browser. Faster to iterate (no
  reflashing), one port (Python → JS), raw data is always available to save.
- Device (C): send only per-rep results. Less BLE traffic, works without the phone
  keeping up, but every algorithm change means reflashing.
- Lean: phone first. Decide in roadmap 2.7.

**O2: MCV or MPV as the main metric?**
A lot of the load-velocity research (González-Badillo's group especially) uses mean
*propulsive* velocity: only the part of the concentric where bar acceleration is
above -9.81 m/s². Once the bar slows down faster than gravity alone would slow it,
you're braking (pulling down on the bar), and MPV cuts that part off. Published
velocity-at-1RM numbers depend on which one is used. Compute both. Pick one before 4.3.

**O3: Mount location.** Decided with data: record at both shortlisted spots in 2.1,
validate both in phase 3, pick in 3.7. Candidates:

| spot | pros | cons |
|---|---|---|
| **Sleeve end, IMU centered on the bar axis** | On the roll axis, so no lever-arm error. Out of the way. | Sticks out past the bar end (check J-hooks, safeties, walls). Most tilt and whip. Some bars have end caps that aren't steel. |
| **Shaft, just inside the inner collar** | Closer to the middle, less whip and tilt. Outside normal squat, bench and deadlift grips. | IMU ends up ~20 mm off axis, so needs lever-arm compensation. Magnet seat has to fit a ~28 mm round shaft. |
| Sleeve surface | Easy to stick on. | Off axis *and* spins with the plates. Worst of both. Not shortlisted. |

Check the bars at your gym before 2.1: are the end caps magnetic, and does anything
stick out past the sleeve end when racked?

**O4: Arduino board package for the XIAO nRF52840.** Closed by D24: non-mbed.

**O5: IMU sample rate and ranges.**
Starting guess: ~200 Hz, accel ±8 g, gyro ±1000 dps. Confirm in 1.3, recheck the gyro
range against the peaks logged in 2.1.

**O6: Where training history lives long-term.**
- IndexedDB + export/import (v1, no backend, works offline). Risk: browser storage
  can get wiped, so exports are mandatory.
- Cloud sync (Google Drive, a small backend, etc.): survives a lost phone, adds auth
  and complexity.
- Lean: IndexedDB + export first. Revisit in phase 5.

**O8: Add a laser distance sensor and fuse it with the IMU?**
The IMU is good short-term and drifts long-term. A distance sensor is the opposite:
noisy sample to sample, no drift. Fused (complementary filter), the laser could pin
down ROM on slow grinders, the IMU's weakest spot. Costs: a narrow-beam sensor like the
TF-Luna (2° beam, ±6 cm, 1 to 250 Hz), roughly double the size, ~70 mA, a boost
converter, beam-aiming issues, and a 0.2 m minimum range that's a problem for deadlifts.
- Simulation says it works: on a no-pause set's grinder rep, MCV error went from +38%
  (IMU only) to -1% (fused), assuming 1 cm of random laser noise
  (`analysis/demos/imu_laser_fusion.py`). Fusion can't fix the laser's *systematic*
  errors, so it needs calibrating.
- Plan: bench test early (1.6), try it on the bar (2.8), validate IMU-only vs. fused
  side by side in phase 3, then decide whether it goes in the final hardware.
- See sensor-options.md.

**O7: Keep the XIAO's built-in IMU, or add a newer one on the carrier board?**
The XIAO's LSM6DS3TR-C is an older part (gyro noise ~5 mdps/√Hz vs. 2.8 for a current
part like the ICM-42688-P). But noise isn't what limits accuracy over a 2 to 4 s rep;
bias, scale, orientation during horizontal acceleration, mounting, and segmentation
are. The dedicated device's real advantages over a phone are control (sample rate,
device timestamps, range, filtering), calibration, a rigid mount, and being small
enough to sit on the bar axis.
- Keep it: simplest, nothing to design.
- Add one over SPI on the carrier, with the XIAO still doing radio + MCU (fits D5).
- Decide after phase 3. Only upgrade if validation shows the IMU itself is the limit.
