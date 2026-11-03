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
  USB or reset wakes it, so the cell can't be run flat. *(Amended by D38: that held only with a
  hard off. Since 10-31 it also runs at every boot, but never while asleep, so a soldered pod
  asleep near the cutoff still drains.)* Charging stays at 100 mA (0.25 C on
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
*Amended the same night (review):* the "2x the other reps" scale ignores the rack bumps among them
(a ~0 m unrack next to a heavy single made the single "handling" and the set vanished), and a closed
set with no rep anchored at all keeps them all, flagged (its 2 s of quiet isn't always a still:
hands left on the racked bar). 7 more rack-filter answers and 2 closed-set answers in parity.mjs.
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

**D34 (2026-10-26): the sticking point is the deepest mid-rep velocity dip with a rise on both
sides, 10% of the rep's peak deep, between 10% and 90% of the ROM.**
- reps.py sticking_point / reps.js stickingPoint, per rep, after find_reps (it doesn't touch the
  boundaries): STICK_SMOOTH 50 ms centered mean (running sums, so JS matches to the bit), STICK_PROM
  0.10 x the rep's peak below the lower of the peaks before and after, STICK_RANGE 0.10-0.90 of the ROM.
*Why:* analytics.md section 4's rise-dip-rise pattern, with a prominence (not an absolute speed: a
grinder's whole curve is slow) so a sensor wiggle isn't a sticking point, and the ends left out
because every rep speeds up and slows down there. The smoothing width truncates (int / Math.floor),
not rounds: at 210 Hz 0.05 x fs is exactly 10.5, and Python's round() and JS's Math.round() split it.
*Provisional:* the 10% and the 50 ms are guesses until real heavy reps; on the fakes, a 40% dip is
found within 0.2% of the ROM and no smooth fake reads one. Three real sets do, none of them a
barbell grind: hand-held phone files 2123 (rep 3, 50% deep at 16%) and 2206 (rep 2, 11.7% deep,
just over the line), and the lat raise's rep 7 (39% deep at 42%, the set's odd rep: 2.6 s, its
profile 0.56 off the median). *Amended 2026-10-27 (review):* the known answer didn't pin either
setting (any STICK_PROM up to 0.45 passed) or the rep count. It now holds 6 more reps on the rule's
edges: an 8% dip reads none and a 12% dip one; deep dips at 6.5% and 93% of the ROM read none, ones
at 13% and 87% read one; and the rep count must match. Changed in both languages, each setting fails
outside a bracket: STICK_PROM about 0.08-0.12, STICK_RANGE's ends 0.065-0.13 and 0.87-0.93 (the
third review found the lower end and a narrowed upper end unpinned by the first 3). The set card's
"~41%" is a true median now (an even count averaged the two middle reps' places; it took the higher).

**D35 (2026-10-27): consistency's tempo is the lowering between two reps, not a rep's own: the
biggest drop in the gap, its pieces joined across any stop with no rise, timed at its own 3%;
sets compare by exercise name.**
- consistency.py `lowering()`, per gap (one rep's top to the next one's start): runs where the bar
  moves down faster than ECC_STOP (0.03) x the gap's peak downward speed; runs dropping under
  ECC_PIECE (0.10 x the smaller neighbour's ROM) are wobbles, settles and sags, never pieces;
  pieces with no rise between them (ECC_RISE, 0.02 x ROM) are one lowering, however long the stop
  partway down. The lowering is the biggest drop (no piece: the run holding the peak), timed at 3%
  of its own peak. The report: "lowerings between reps, against their median: 1-2 +6%, 2-3 +1%...".
- Per gap, not per rep, and the ends (before the first rep, after the last) aren't used. Whether a
  rep's lowering is the one before it (a squat) or after it (a deadlift) is a guess the motion
  around a set can't always make: a Romanian deadlift is tracked as a deadlift but lowers first, a
  pin squat as a squat but rises first, a bent-over row's hinge from the hang looks like a squat's
  first descent, and the bar set down, the pod unclipped or a cut-off rep after the set looks like
  a deadlift's last lowering. The lowering between two reps is the same event either way.
- Set to set and session to session group by the typed exercise name, trimmed, any case (the LV
  profile's sameName), not the tracking profile; unnamed sets fall back to "(profile)". A set's
  tempo there is its median lowering. A session in several exports counts once: the copy from the
  newest export by its own "exported" time (`sessions.exported_at`), not by the files' names, order
  or dates (a one-session export is named by the session and can be made any day), since names,
  loads, RPE and "Not a set" can change after an older export.
*Why:* four reviews in one night. The first rule (the run nearest the concentric) timed a box sit's
bottom wobble (squat 2131: 0.08 s for a 1.46 s descent, tempo +624%) and a deadlift's settle at
lockout (0.028 s for 3.05 s, -98%); the run holding the peak speed then timed the pod lifted off the
stack after the lat raise (faster than its lowering): the drop is what tells them apart. Each rule
for the side (by name; by the lead-in; by both ends with the profile on a tie) met a lift it read
backwards, every rep against its neighbour's lowering, and the third review showed traces no rule
on the ends can split (a row's hinge and an RDL's descent; a set-down and a deadlift's last
lowering). A short stop partway down cut the lowering (a slower descent read -19%), and so did any
stop over the 0.5 s join limit the second rule had; in a gap there's no handling to guard against,
so a stop of any length with no rise joins. Every unnamed exercise is "machine" or "free" (D21), so
grouping by profile held a curl against an OHP. Real files now (each gap against the set's median
lowering): squat 2131 +6/+1/-1/-6% (1.38 s), 2134 +24/+2/-6/-2% (1.29 s), the deadlift
+51/-14/-6/+6% (2.02 s: its first lowering really is 3 s), the lat raise -4/+4/-13/-16/+19/+12%
(2.00 s).
*Guarded by* consistency.py --selftest: a 35% slower descent must read 35 +/- 5% in the right gap
in a squat with a box sit's wobble, a deadlift with a settle, a stack handled after its last rep,
an unnamed RDL, a Romanian deadlift, a pin squat, a row with a hinge, a curl from a high rack, a
Romanian deadlift set down on the floor, a lunge with the pod unclipped, a lunge cut off mid-rep, and
+44% / +126% when it also stops 0.1 / 1.0 s partway; a curl and an OHP at one load must not mix; a
session in three exports keeps its newest copy whatever the names, order and file dates; set to
set and against 4 earlier sessions, a set lowered 35% slower reads +35% (the set's tempo is its
median lowering, the baseline the last 4 sessions). Then lowering() on exact numbers, the 0.03 by
hand, its no-piece fallback included. 30 of 31 mutations fail it (each threshold halved and doubled,
the biggest drop swapped for the peak run or the first or last piece, joining off or time-limited,
the gap's 3% for its own, the fallback changed, a set's or a stack's tempo or the baseline's
sessions wrong, every export sort but the right one); one survives: the smaller neighbour's ROM
against the larger, which no case tells apart and which differ by a few % in practice.
*Open (reps.py, not this):* a dead-still stop of 0.5 s or more partway down, or a grinder's dead-still
stall, is a still to reps.py, and the squat's leveling takes it for a lockout: that rep's ROM reads
~25% long (flagged 'uneven lockout'; item H of the first bug hunt). And a curl from a rack well above
the hang: the lift back onto the hooks is kept as one more rep (the rack filter, D31, can't tell a
0.35 m rerack from a 0.40 m curl). And a 1 1/4 rep: its quarter rises MIN_ROM or more, so reps.py
counts it as a rep (5 squats read 10, the last full one then dropped as handling) and consistency
compares quarters with full reps (depth +41 cm every other rep, the gaps alternating about 60%).

**D36 (2026-10-27): RPE v1 (Python) labels reps from sets with "Your RPE" 10, fits per exercise
name, and is only ever checked on sessions it wasn't fit on.**
- analysis/rpe.py: a set to failure (Your RPE >= 10, or a `toFailure` mark if a set ever has one)
  labels rep k of its N completed reps with N - k in reserve. Which reps completed (`labeled_reps`),
  on heights without the leveling (which stretches a miss's top to lockout):
  - From the bottom (a deadlift, a stack, an OHP, a pin squat; by the data, which a Romanian
    deadlift doesn't share with a deadlift): only the last rep is judged, against the one before; a
    single isn't labeled. A first rep the next starts well above is the bar picked up from the
    floor: it goes, and the rest is judged as a set from lockout (an RDL's depth is its own).
  - From lockout (squat, bench, an RDL or a curl from the rack): a last "rep" rising from a still in
    the hole 2.5 s longer than any pause before (stills at one height across a nudge joined, as for
    rests) is the bar lifted off the safeties into the hooks
    (after a miss stuck in the hole, no rep to reps.py, or the bar set down). Then only the last two
    reps can be a miss (a miss ends the set): one that rose under 0.8 x the rep before and under
    0.8 x its own lowering (a short completed rep rises back up all of it, a miss doesn't, with or
    without a still at lockout) and then came to rest low, half the median rise below the lockout
    just before it (local: plain heights drift window by window; on the real squat 2131 the lockouts
    read up to +0.56 m against the first still, so a miss spliced on read -0.07 m there for a true
    -0.67), ends the set there.
  - A last "rep" over 1.5 x faster than the one before isn't one (a row's stand-up out of the hinge).
  - A still of 6 s or more between two reps (stills at one height across a nudge of under 1.5 s
    joined) is a rest (rest-pause, a cluster: sets.py keeps under 15 s in one set): only the reps
    after the last one count down, the lift out of the hooks after it aside (dropping reps from the
    front never mislabels the rest).
  - A set with no completed rep, or a rep that never went back down or is under 0.8 x the median ROM
    (a grinder that sank and split, a half rep mid-set), isn't labeled, and the report says why. A
    labeled rep flagged 'uneven lockout' keeps its place in the count but its MCV isn't used (a
    dead-still stall, a miss after short lockout holds: heights the leveling couldn't make agree).
- Per exercise (typed name, like consistency.py): RIR = a + b x MCV by least squares predicts; the
  reported pair, the MCV at RIR 0 and per rep in reserve (v0's two numbers), is MCV on RIR, since
  the labels are exact and the noise is in MCV (the predictor inverted reads the MCV at failure low:
  0.227 for a true 0.25 at 0.04 m/s of noise). Needs 2+ sets to failure and a line that slopes up.
- Validation: leave one session out, mean absolute error in reps, near failure (RIR 0-3) and
  further; a session is checked only when the others alone give a line (one set per session: 3+
  sessions), and the report names those that aren't. A normal set's RPE is on the 5..10 scale,
  its range +/- that error (at least 0.5; +/- 1 until there is one), the ends to half points.
- One line across loads: each load's mean residual within the sessions that took 2+ loads to
  failure, the session's own mean taken off (across days, a load's residual is also its day's
  offset); "can't tell yet" otherwise. Loads by number ("225" and "225.0" are one).
*Why:* analytics.md section 5's Model v1. Your RPE 10 because the app has no failure mark and
RPE 10 means 0 reps left, which is the label; a separate "to failure" box would be more objective
(an attempted rep that failed), so the code already reads one. Leave one session out because reps
in one set are near copies and day-to-day shifts are the error that matters.
*Amended 2026-10-27 (five reviews, 13 + 7 + 7 + 8 + 4 findings, 0 refuted):* the failed attempt
was a rep and shifted every label, and each fix met a new ending: lifts from the bottom, a missed single,
a split single or double, a rerack after a miss or after a set-down, a paused or touch-and-go set
with a shallow completed rep, a short last rep set onto the safeties, the drift across a real set,
a rest-pause (also with its rests split by a nudge, or real 8 cm hooks), an RDL or a row pulled from
the floor; the pair was the predictor inverted; validation skipped sessions silently with a wrong
reason; the bias used estimates off the 5..10 scale; the width rounded under the error; the
per-load residuals were day offsets; the self-test's guards missed partial leaks and every case
where a rule must not fire; the rerack rule timed single stills while the rest rule joined them, so
a still on the safeties split by a bump left only the rerack labeled.
*Limits:* a miss near lockout (rising 0.8 x the rep before or more) can't be told from a short
completed rep on these heights and is counted, so that set's labels read one high. A row's slow
stand-up (under 1.5 x the last row's speed) or a curl's rerack is kept as a rep (the D31 rack filter
can't tell them from one). An RDL deep enough to start near the floor keeps its pickup. A rest under
6 s, or a grinder held over 2.5 s longer in the hole than the set's other pauses, is misread. These
are sensor heuristics standing in for what the lifter knows: the reliable fix is in the app, a "to
failure" box and a confirm of the reps completed on a set called RPE 10 (open, Quentin's call).
*Guarded by* rpe.py --selftest: the raw-signal squatter (a failed attempt on the safeties, a
second set to failure in one session, a split grinder, a set called 9.5, a warm-up called 5)
recovers 0.241 / 0.0452 from 44 labels, its held-out errors must equal leave one session out
recomputed independently (0.21 reps), 10 of 10 ranges hold the truth, 20 of them near failure;
42 sets to failure whose completed reps are known (misses at 40-70%, stuck in the hole, reracked
after 1.5, 3, 5 or 8 s or after 6.5 s split by two bumps, a
short last rep racked, on pins or onto squat-height safeties, a Romanian deadlift tracked as a
deadlift reracked or pulled from the floor at varying depths, missed and made singles, a split
single, deadlifts and a stack with and without a miss, a half rep mid-set, a dead-still stall set
aside, paused sets with a shallow pair, touch and go or not, a pause squat held 3 s every rep, a
last rep held 2 s in the hole, 4 s breaths at lockout with and without a pause in the hole, a half
rep second to last, rest-pauses standing, in 8 cm hooks, split by a nudge or by a re-grip, a row
from the floor with its stand-up, a fast last rep near failure, a wandering bias, and the real squat
2131 with a miss spliced in); exact checks of the pair under noise, the width, both clamps, loads
by number and the per-load rule. Of 53 mutations (leave a rep or set out, in-sample, labels +/-1,
RPE 9 as failure, the previous commit's rule, FAIL_FRAC, REST_MAX, SET_DOWN, FAST_FRAC, NEAR and
JOIN_GAP lowered, each branch, window and check of the labeling off, forced or widened, each fix
undone) 51 fail it. Two survive, both read every realistic input the same: "low" by any amount
instead of by half the median rise (after a miss the bar only goes down), and JOIN_GAP raised (in
a gap between two reps there's no rep, so stills at one height around any fidget are one rest). *Open:* the
to-failure box in the app; whether one line fits every load (needs 2+ loads to failure in one
session).

**D37 (2026-10-30): the pod's calibration is one guided USB recording: 20 s of rest, the 6 faces,
then whole turns about each axis; a turn counts only when it's a whole number of turns about its
own axis that ends where it began.**
- analysis/calib_pod.py (roadmap 1.3), live (`python calib_pod.py COM5`, the raw CSV kept, P2) or on
  a recording. Stills: 0.25 s blocks whose accel and gyro wiggle stays under 4x the quietest
  block's and whose gyro sits near the slowest quiet block's (a steady turn is quiet too). The
  rest (the longest still, 10 s+): noise per axis and the gyro bias. Faces: per face its flattest
  still within 10 deg of flat and 2 s+, fitted by calib.py (the phone's fit, D23); if the fit's
  worst face is off by over 0.05 m/s^2 (a face propped t deg adds about g sin^2(t) / 2: 0.09 at
  8 deg), the guide asks for the least flat face again. The USB-C end down can't stand on the
  plugged-in cable, so the guide says to stand it at the table's edge with the plug over it.
- Turns: each move between stills is integrated with the bias of the stills either side. A roll
  about a level axis done a face at a time is one turn once gravity is back within 45 deg of its
  start; a spin about the vertical stopped short and pushed on by under 45 deg is one spin; one
  turned back by over 2 deg after it stopped is refused. The angle is about the turn's own axis
  (the main direction of its rate), not its share on the board's axis. A turn is used when it's
  within 3% of a whole number of turns, under 15% on the other two axes, and has gravity before
  and after within 2 deg; each axis takes the median of its turns. Saves
  data/pod-calibration-*.json: corrected = (raw - bias) / scale.
*Why:* algorithm.md section 1: scale errors are what drift correction can't see (2% high reads
2% fast; 1% of gyro scale is 1.8 deg per sleeve half-turn). Whole turns are the only reference on
hand. Gravity confirms a roll (x, y) ended where it began, but can't see a spin about the
vertical, so those are against a straight edge, 3 turns (1 deg off is 0.1%). 3%, because a gyro's
scale error is spec'd to a few %, and a sloppy 340 deg spin at +1.5% reads 345 (4.2%), which 5%
would take as a turn. The chaining, because a hand stops: a roll regrips at each face, and a spin
read in pieces would take one stopped 15 deg short as a whole turn 1.4% off. A spin turned back
is refused rather than added: that's a hand hunting for flush, and a new spin is cheaper than a
guess at which still was. Its own axis, because a turn 6 deg off the board's axis puts only cos 6
deg of its angle on it (0.55% low). The flattest face and the residual check, because one face
propped on a crumb skews the fit and nothing else would show it.
*Guarded by* calib_pod.py --selftest: a synthetic pod at 208 Hz with planted accel and gyro bias
and scale errors and the pod's noise. The main session (a rest, a 25 deg lean longer than any
face, the 6 faces, spins about z, rolls about x and y) recovers every error (accel scale to 0.1%,
gyro scale to 0.2%, gyro bias to 0.02 deg/s) and refuses a 340 deg spin, a tumble 4 deg short and
a turn about a tilted axis. Then the hand's cases: a roll a quarter at a time with 2 s on each
face; a spin stopped short and pushed on (then a quarter turn back, which isn't a nudge); one over
and turned back (refused); a turn 6 deg off y; a face propped 8 deg (redo asked for, then the flat
one used); three spins, the first 16 deg short (the median); a gyro bias drifting 0.6 deg/s a
minute; a steady 60 deg/s turn like a turntable's. Last, each tolerance just inside and outside.
Mutations: 40 of 42 fail it, among them each tolerance either way (RESID_MAX and NUDGE_MAX
loosened), each chaining rule off or unbounded, the level test either way, the share instead of
the own axis, the longest or first face instead of the flattest, the bias left in or from the
first still, each refusal off, the median of turns as the first or the mean, and the rest reading
as the median of every quiet block (a steady turn longer than the stills became "rest": the bug
the turntable case found). Two pass, unguarded: the accel stillness threshold 10x looser (the
self-test has no sliding, and the gyro gates every still it has) and the reported gyro bias from
the first still instead of the median (only reported: the turns use the stills either side, and
the app re-estimates the bias at every still). Two gravity checks inside the chaining went, since
the turn's own end check covers them. Checked on a real bare-XIAO rest (10-18 desk file: 0.006
m/s^2 and 0.04-0.05 deg/s of noise) and live through a fake port: micros() rollover, the port lost
at 100 s (the result from what came in), no port (a plain message, no empty CSV), the USB-C-end
hint and a refused spin's notice printed as they happen.
*Open:* the first run on the assembled pod; applying it (the firmware's flash or the app's
PodSource, "save both calibrations in firmware" in 1.3); gyro scale by a record player at 33 1/3
rpm (200 deg/s) if one turns up (the steady-turn case already passes).

**D38 (2026-10-30): the pod's LiPo is soldered straight to the XIAO's BAT pads: no connector, no
switch, no hard off.**
- The 10-21 print routes the leads from the cell's far end back over it in `batt_gap` and up
  through the tray's window to the pads, and has no room or way in for a mated JST-PH pair
  (hardware/enclosure.md, assembly checklist, with the soldering steps). Off is the deep sleep
  (D26); for a bag or a shelf, the IMU-off sleep, which only USB or reset wakes (`o` from a
  serial terminal holding the port, or the phone's SLEEP mode 1 from the app's devtools during a
  pod session; no button yet).
- Unplugging USB should no longer be a power cycle: the cell feeds the 3.3 V regulator through
  Q1 as VBUS goes (Seeed's v1.1 schematic). Checked 11-01: an awake pod unplugged runs on (no new
  boot); a sleeping one wakes at the unplug, most likely the handover tripping the IMU's
  wake-on-motion (the one banner caught read `motion`, INT1 latched). The recovery is the reset pinhole: a pin reset, which also
  clears a running watchdog (Nordic's reset table: power-on, brownout, pin and watchdog resets
  clear it; a soft reset, like an upload's, and a CPU lockup don't). So a sketch that doesn't
  feed it resets once, about 8 s after the upload, and then runs unwatched.
- Amends D26: its "the cell can't be run flat" held only with a hard off; see Guarded and Still not guarded.
*Why:* a reprint with a wire hole, and a mated pair ~4.5 mm thick in a 4.7 mm space, would buy
only a hard off, and the deep sleep is close to one: about 16 uA with motion wake and 7 uA with
the IMU off (datasheet budget, not measured), over a year from a full 400 mAh cell either way.
*Guarded (10-31; flashed and bench-checked 11-01, the plan and results in firmware/README.md: every
step passed but the reset-pinhole one, which the puck blocks, and a race run that took the boot's own
USB check)*, the gaps the unplug used to cover: the pod checks the cutoff at every boot
before the radio, the sampler or the lid-down gesture can run (3 readings 20 ms apart; below
3.30 V on battery it takes the IMU-off sleep), so a pod woken near empty, or reset while flat,
blinks red 3 times and sleeps for good instead of running until a lid-down sleep 6 s later beat
housekeeping's first reading; setup()'s IMU-not-found loop rechecks USB every pass and sleeps
with the IMU off once it goes; imu_serial switches itself off (System OFF, the IMU unpowered)
whenever VBUS is absent. VBUS wakes System OFF only on its rising edge (the 10-19 `o` on USB
stayed off), so USB plugged in during a low-battery or IMU-lost sleep's ~0.7 s setup (the red
blinks invite it) restarts the pod instead of leaving it dark on the cable.
*Still not guarded:* a pod asleep with motion wake is only checked when it next wakes, so one
left untouched near empty still drains to the protection board's trip (~16 uA; weeks). Firmware
can slow this but not stop it: System OFF's only analog wake (LPCOMP) compares against fractions
of VDD, and VDD comes off the cell through the 3.3 V regulator, so it sags with the cell near
empty (from the nRF52840 datasheet; unchecked on this board). Whether a sleep entered below
LOW_BATT_WARN_V (3.50 V, the app's battery-low warning) should take the IMU-off sleep instead
(~7 uA, about twice as long, but no wake on a bump until USB or reset) is Quentin's call, open.
Every USB session charges now, so the P0.14 rule is always live (only flash.py builds). Charge on
the app's battery-low warning and store the pod in the IMU-off sleep.
*Open:* both sleep currents, the battery reading against a meter, the cutoff on the real cell,
the reset-pinhole step and a clean race run (bench plan G1 steps 5 and 7), whether an unplug wakes
the IMU-off sleep too (`o`), and the motion-wake call above. Answered 11-01: unplugging an awake pod
gives no new boot, and plugging USB in wakes the IMU-off sleep with the cell on. Whether the bootloader (every upload
and reset) leaves P0.14 floating: through the 1 MOhm that's under 1 uA into P0.31, likely fine,
unchecked.

**D39 (2026-10-31): a segment the tracker drops still gets its raw samples saved, as
`drop-<lift>-<stamp>-<hhmmss>.csv` in the session folder.**
- SessionTracker hands a dropped segment's samples (with its 2 s lead and trail) to the app's
  onDrop, and the app saves them like a set's CSV whenever it autosaves (the Android app; the
  browser has no autosave). Before, a segment with no reps, or one whose analysis failed ("No
  still periods found"), vanished with its raw data.
*Why:* P2, ahead of the first loaded-bar session (docs/gym-day.md). The readiness audit found the
likeliest failure on Adapter A is the tracker missing a real set (uncalibrated x/y offsets make
|a| at rest shift with the sleeve's roll, D37), and a missed set is exactly the data needed to fix
it. `drop-`, not `bar-`, so nothing that reads `bar-*.csv` mistakes it for a set; reps.py still
reads the lift from the name.
*Guarded by* parity.mjs: every dropped segment arrives non-empty, in time order and clear of every
set's reps, and the fake squat session drops exactly its one no-rep rack walk.

**D40 (2026-10-31): on a real bar, reps are anchored on their turnarounds, and bar lifts from the
pod use the gyro-tracked vertical instead of |a|.**
- The first gym session (pod on Adapter A) miscounted every barbell set: 3/8, 1/6, 1/3 squats and
  1/5 paused deadlifts (the ham curl's 10 were right). A loaded bar never stops between reps (the
  2.7 Hz sway at lockout, 20 ms touch-and-go tops, plates ringing at 20 Hz on the floor), so the
  only stills were the rack, one window spanned unrack to rerack, and |a| rectified the clang and
  the sway into metres of fake height.
- reps.py (then reps.js, line for line): (1) for squat, bench, deadlift and free with a gyro at 150
  to 300 Hz, Madgwick (moved here from orientation.py) pulled at stills and quasi-stills gives the
  acceleration along up; (2) a window that's broken (end velocity over 1.0 m/s) or holds a
  gyro-confirmed impact is redone rep by rep: a high-passed coarse pass finds the reps, the lift's
  resting turnarounds (lockouts, the floor) anchor §6's line span by span, a pause on its own mean
  velocity. Constants and their evidence: TA_*, IMPACT_*, QS_* in reps.py; algorithm.md §6c.
*Why:* the video truth (plate tracking synced to the pod at r 0.999; 135: 8 reps, 225: 6, 360: 3;
the deadlift: 5 from the pod's own calibrated forensics). Three designs were prototyped
independently and judged; this graft scored 8/8, 6/6, 3/3, 5/5 with squat MCV +1.9 to +3.9% on
average (worst +6.8%), ROM within 1 to 2 cm, the ham curl bit-identical, and the fewest live
flickers. Turnaround anchors on |a| alone needed |a|'s rectified clang to trigger; the vertical
alone (high-passed) read the 360's grinder 15% low. Not §7's mistake: the anchor times come from a
drift-free coarse pass, pauses anchor on their own mean, and only tops are touch-and-go anchors.
- Review (18 agents, 15 findings, all real) fixed before landing: Madgwick's normalized pull made the
  vertical chaotic in roundoff (SN_FLOOR 0.05 makes it proportional near convergence; JS vs Python
  now agree to 3e-12 per sample, gym MCVs moved under 0.001); a floor landing's close divided 0 by 0
  when a ring reached back to the span's start (NaN, every later rep gone); the rack cut applied to
  nameless free lifts (TA_RACKED: squat and bench only); the tracker had no catch around analyze()
  (a throw now drops the segment with its samples, flagged), the live loop none either; JS's
  Gaussian was 3x slower than needed (same sums, same order).
*Guarded by* parity.mjs: the 5 gym sets JS vs Python (full and cut partway), the paused deadlift also
as a nameless free lift (5 reps), whole sessions through analyze(); known answers from the video
(counts; each rep within 8%, the set's mean within 5%; deadlift reps 2 to 5 within 5%; the ham
curl's MCVs within 1%); a live replay every 0.25 s (final count right, at most one dip, never ahead
of the final count but for the ham curl's known flicker); the vertical's gate (60 Hz and 402 Hz bar
lifts exactly on |a| - g, pod files on the vertical), the vertical JS vs Python per sample within
1e-9, and Python's within 1e-9 under a 1e-12 nudge (chaos); no rep with zero duration or a
non-finite MCV; no drop that threw; and an exact-physics set at the pod's rate (make_fake_motion.py
squat_tng: 6 touch-and-go squats with a walkout, the sway, sleeve roll, the roll-dependent offset and
a ringing rerack; the old code found 2 of them, this finds 6, each within 8%, worst +5.3%). No older
parity expectation changed. Over 716 older results what moved: pod-rate deadlifts (the 10-23
hand-held test, still 5 reps, rep 1 MCV +5.9%, the rest within 1.5%; the 12:10 home set, rep 4 ROM
53 to 44 cm, still 4 reps), the 13:44 handled "rep" (0 in the closed set and live) and
fake_session_squat's whole-session reps.py run as machine (six NaN reps gone). sets.py on the fake
sessions is unchanged.
*Open:* tuned on one day, one lifter, one mount, an uncalibrated pod. The deadlift's rep 1 starts at
its 6 cm hitch (+31%; fixed by D41). Squats read ~3% high (scale: the 6-face calibration, D37). On the vertical
only an impact triggers the redo, so a set with none (a soft rerack, a recording cut before the
rerack) falls back to the old path. Weakly guarded: the quasi-stills and BAR_AXIS barely move any
final number here (only a live dip notices them going); a deadlift-like synthetic set with a gyro
scale error would pin them (D41's make_fake_motion.py deadlift_pins does). Untested on Adapter B and on a
real bench set.
- Bench on synthetics (11-01, before a gym day that didn't happen: 8 agents, exact-physics sets at 208 Hz
  with the pod's mount and errors, each claim re-run by a skeptic; Python and JS agreed on 500+ runs; the
  scripts are kept locally in analysis/out/bench-synth-2026-11-01/, gitignored). Touch-and-go counts: 48 of
  48 cards right, set means within 1.4 to 3.7%, when the rerack clangs. Open, each reproduced there:
  (1) TA_NEAR_FRAC rejects a paused set's first and last holds (the chest pause shrinks the coarse move
  to 19 to 25 cm), so their anchors land in the moving bar: rep 1 38 to 64% low with a 0.75 s+ chest
  pause, the last rep 29 to 35% low after a 1 s+ lockout; 0.5 fixes it there, unchecked against D41.
  (2) Heavy paused grinds and paused singles: 1 of 3 reps, or no card. (3) TA_RACKED has no gyro
  check: a chest bounce over ~2 g closes the set and empties the card. (4) find_pause takes a slow
  drift out of lockout for the pause (anchors 0.3 s late, ROM +5 to +8 cm). (5) The bench ends rule
  (level_heights) puts the rack still at lockout height, 4 to 6 cm off: with a soft rerack the window
  isn't redone and the unrack becomes a rep. (6) A rerack fumble above lockout drops the last rep.
  (7) The pod taken off before the double buzz loses the last rep. docs/gym-day.md's bench section
  works around them. **Fixed by D43** on synthetics, (7) only after a clanging rerack with the pod left on
  3 s or more; the live view only in part (D43 Open).

**D41 (2026-10-31): on the floor a pause may end up to 1.0 s before its coarse turnaround; with no end
still yet, a floor lift's last rest anchors on all of it, and a turnaround the high-pass confirms only in
the window's last 0.75 s waits, unless it's the floor the bar has landed on after the last rep.**
- The 10-31 paused deadlift's rep 1 read +31% (ROM 40 cm, true 53). It's pulled after a 2.2 s setup with
  no set-down jolt to restart the coarse pass, so the 0.75 s high-pass bends the coarse height down into
  the pull and the low lands at the 6 cm hitch, 0.59 s after the floor rest ends. TA_REACH (0.5 s) then
  left only the hitch's own 0.29 s flat run, which became the floor. TA_FLOOR_REACH 1.0 s, on the floor
  and on the side before the low only: the floor rest is a candidate again and wins as the longest. Rep
  1 now -0.7% (ROM 53 cm), the set's worst rep -1.4%; 0.75 to 1.5 s give the same answer.
- That alone took the live replay (every 0.25 s) from 1 dip to 3, two ways the window's open end misleads
  a floor lift. An open window's last floor rest was anchored on its first 0.5 s (a rule for a squat's
  walk-in), so the next pull integrated 3 s of drift and rep 5 came and went: now it anchors on all of
  it. And near the buffer's end the high-pass fakes a low mid-descent, just before the plates land, and
  the coast there became the "floor" (rep 1 at 13.0 s and rep 4 at 22.75 s vanished for a pass; the
  second was D40's one dip): now a floor lift's turnaround confirmed (TA_MIN_MOVE away) only in the
  window's last TA_SIGMA waits. 0 dips, each rep shown 0.25 to 0.75 s after its lockout.
- Review (9 agents; the fixes scored as 6 variants on the reviewers' 30 synthetic floor sets, 6 creeping
  breaks and the real set): the wait first dropped the last low that nothing confirms, the floor after
  the last rep, so a set closed within 1.9 s of its last set-down (End tapped), or whose drift ran the
  other way (+0.08 m/s^2 on the vertical), lost its last rep (HEAD kept it). Now that low is kept once
  the bar has landed (a coarse stop, a jolt it then rests after, between the high before it and TA_REACH
  after it): closed from 29.5 s on, 5 reps (29.0 and 29.25 s, still ringing: 4); the offsets: 5. The
  open last rest first anchored on its last 0.5 s, which a creeping break (1 to 2.6 cm over 0.27 to
  0.48 s, under TA_FLAT_A) runs into: the shown rep read +20 to +45% on most live passes of those sets;
  on all of it, 9 to 17 such passes of 70 (from 69), and the real set is unchanged.
- Considered and left (numbers in the session log): the first floor rest on all of it too fixes a
  creeping break's final rep 1 (19 to 26% high on the synthetic sets, 2 to 3% with it) but moves the
  usual bottom on the real set, where rep 3 then shows +23.5% for 0.75 s; and the window-start merge at
  TA_FLOOR_REACH rescues a set whose floor rest is a true still (the window starting at the pull) with a
  hitch on every pull, but takes a touch-and-go creep from 3.8% to 12%. Both need real slow pulls.
*Why:* the low comes late only on the floor and only before the first rep: every later rep's low is at
its set-down, where the jolt restarts the coarse pass. On the floor the height check (TA_NEAR_FRAC)
still keeps a lockout or a mid-descent hover out. The open-end rules wait for evidence instead of
guessing: a rep in the open tail still counts from the raw velocity until its rest is anchored, flagged
"no end anchor" (the app counts it as the rep in progress).
*Guarded by* parity.mjs: the deadlift's 5 reps each within 5% of the truth and the mean within 3% (rep
1 was left out before); the same set cut at 30.0 s, 1.4 s after its last set-down, must close with 5
reps; its live replay with no dip at all (each live file now has its own allowance), each rep's MCV as
shown while it's the newest within 8% of the truth, and at most one pass (that 14.50 s one) showing the
newest rep over 10% off once its true lockout is 0.3 s past; make_fake_motion.py deadlift_creep (six floor
pulls: four creeping breaks, two 6 cm hitches, rep 1 out of a 5.5 s still): 6 reps, each within 5%, and
at most 8 such passes (7; anchored on the rest's last 0.5 s, 18; without the landed-floor rule, 17). Each
rule, reverted alone, fails one of these. Over 726 older results nothing moved.
make_fake_motion.py deadlift_pins (10 dead-stop pulls, the sleeve spinning 21 to 25 deg/s through every
rest, the gyro 1.5% fast) pins D40's quasi-stills and BAR_AXIS: 10 reps, worst +2.8%; with the
quasi-still pulls off or BAR_AXIS wrong, 7.
*Open:* one paused deadlift, one lifter; untested on real touch-and-go deadlifts, a pull with no hitch,
a failed first pull, a slow pull. Found on the way, not fixed: the gyro's bias is the median over the
stills, and a sleeve spinning on its bearing passes the |a| still test, so when spinning stills are the
majority the bias becomes the spin (deadlift_pins: 37% of still samples, 0.045 deg/s; a prototype with
still lockouts reached 178 deg); and the height check scales with the rep's size while the coarse pass's
error in a long pause doesn't (a 3.3 s rest on the pins in a 30 cm set fails it, the next rep -65%);
and live, at 14.50 s on 10-31, drop_rack_moves takes rep 1 for handling for one pass while rep 2's first
centimetres show (the count stays 1).

**D42 (2026-10-31): the pod's calibration is applied in the app, to the pod it was made on:
`calib_pod.py --install` writes webapp/pod-calibration.json, the build ships it, and that pod's samples
are corrected before anything else, like the phone's (D23).**
- calib_pod.py records the pod's serial before it records (an unplug mid-run can't lose it): its USB
  serial number's last 8 hex digits are FICR DEVICEID[0], the serial in its Bluetooth INFO (D27); a CSV
  analysed again takes it from the earlier result of that CSV, or --pod=. --install (with no file: the
  newest complete result, saying which newer ones it skipped) refuses an incomplete result, a bad face
  fit, a scale more than 5% from 1 either way, anything but 3 finite numbers per bias and scale, no
  "made"; one with no serial gets a warning that it applies to any pod. The app fetches the file at
  startup and checks the same shape (podCalibrationProblem; a broken file is said once, on the card too;
  none at all is silent). Once a pod's INFO says which pod it is, its samples get corrected = (raw - bias)
  / scale per axis, accel and gyro (the gyro bias too: each still re-estimates whatever is left).
  Another pod's calibration isn't applied, and the app says so after the connect (the hint used to wipe
  it) and on the card, serials in hex.
- What was applied is kept with the data (P2: raw = corrected x scale + bias): pod.json and link.json
  (the app's folders), the session record per part (so the D30 export has it; each set says its part),
  and sessions.py --csv writes <session id>-calibration.json next to the set CSVs. Saved recordings are
  corrected ones, like the phone's.
- `reps.py --calib=<json>` applies one to a recording made before (the 10-31 gym sets), and refuses one
  that something next to it says is already corrected. ACC_OFFSET (D40, never set) is gone: the offsets
  come in with the samples.
*Why:* the app, not the firmware (roadmap 1.3 said firmware): no firmware or protocol change, the live
view and every saved file get the same numbers, and a new calibration is a rebuild, not a flash. The
serial check because the app connects to the nearest pod. In the samples, not in the analysis: one
sample format for every source (rule 2).
*Guarded by* calib_pod.py --selftest (the synthetic pod's planted errors come back out through --install
and apply(), accel within 0.01 m/s^2 and gyro 0.05 deg/s; 15 refusals, each for its own reason and writing
nothing; the newest complete pick; the serial from an earlier result and from --pod), webapp/test/calib_pod.mjs
(applyPodCalibration = calib_pod.apply() bit for bit, missing gyro entries, the serial gate, the app's shape
check against --install's on 14 cases, the installed file if any) and webapp/test/export.mjs (the export
and sessions.py's sidecar carry each part's calibration).
- Measured the same night without a calibration run (a 60 s lid-down rest over USB plus every still the
  pod has recorded; analysis/data/pod-calib-estimate-2026-10-31.json, deliberately not --install-able):
  the pod is board 3 of the three tried on 10-18 (gyro bias is the fingerprint: (+0.7, -3.3, +2.2) deg/s;
  the 10-18 axis-* 6-face set is another board). Accel x bias -0.179 m/s^2, scale +0.46% (two methods
  agree to 0.001); y -0.041, +0.45% (moderate); z about +0.20, -0.5%, not pinned: the lid-up reading went
  10.10, 9.93, 9.98 m/s^2 across assembly day, so the case is still settling. Gyro z scale +1.0% +- 0.4%
  (the sleeve's rolls against gravity turning in x-y). Applied with --calib to the 10-31 sets: squats
  +3.18% to +2.84% against the video, so the pod's scale isn't their +3%; the gyro's calibration moves
  MCV under 0.05% (vertical_accel takes the gyro's offset at the stills itself). The USB serial check
  holds on the real pod: 88C8E98A41EF151F, INFO 1106187551 (0x41EF151F).
- 11-01, the puck still on (lid up, lid down and USB-C up over USB, not charging; three agents and a
  judge): **z replaced**, bias +0.281, scale +0.31% (analysis/data/pod-calib-estimate-2026-11-01.json,
  not --install-able either: an equal-weight fit of three lid-ups and three lid-downs over 23 h, each
  within 0.013). The 10-31 z leaned on the lid-ups taken on USB at 12:48 and 13:44, which read 0.13 to
  0.18 low under the new one, while 12:10's (on the cell, in no fit) lands within 0.006. Off charging, z
  repeats to about +-0.01 on both faces over a day; the rest is minute-scale settling after a wake (0.015
  in 80 s). x and y kept: the USB-C-up face reads 0.019 high in |a| under them, against 0.0025 for 10-31's
  five bar stills (it stood on the table with the cable hanging from that end). On Adapter A z is the bar
  axis, so it barely matters there: the new z moves the 10-31 sets' mean MCV under 0.05%, and z left raw
  costs under 0.17%. Recordings stay raw; correct offline with reps.py --calib.
*Open:* the real run (calib_pod.py over USB) once the case has settled, lid-up and lid-down back to back,
and again a few days later to see whether z still moves; then --install, rebuild, a session. The app's
wiring (feed, pod.json) was checked in a browser without a pod; with the pod it's untested.

**D43 (2026-11-01): paused and racked bench, from synthetics. On the bench the coarse pass high-passes at
1.75 s. Squat, bench and free lifts blank, stop on and rack on impacts only, while the floor still lands on every
jolt. A racked set's coarse turnarounds end at the rack. At a lockout the coarse height must not move faster
than 0.03 m/s (between reps a must; before the first and after the last it waives the height check), and
one that falls 5 mm or more had the descent begin in it: between reps it anchors on its first 0.5 s, before
the first rep on the 0.5 s up to its coarse top. A racked lift's move off the hooks under 15 cm isn't a rep. A
closed set's last rep tops out by its last anchor.**
- From D40's open bench synthetics (seven ways paused and racked bench went wrong, each reproduced). A
  design workflow (11-01): a 30-set known-answer suite at the pod's rate (now `analysis/make_fake_bench.py`;
  30 sets x 3 seeds, raw and calibrated: 180 runs), four fix families prototyped against it, one integration,
  then two skeptics, one perturbing the suite the ways a lifter goes (2,224 runs), one reviewing the code.
  The workflow's scripts and results are kept in analysis/out/benchfix-2026-11-01/ (gitignored).
- What each rule fixed, before D43 on the suite: (1) a paused set's first and last holds failed the height
  check (rep 1 -43 to -55%, the last rep lost after 1.5 s on the chest; the bench high-pass, and the outer
  holds' exemption for an 8 s first hold); (2) a heavy paused triple read 1 of 3 and a paused single gave no
  card (the high-pass: a grind's top was never a coarse turnaround); (3) a 2.4 g chest bounce emptied the
  card (impacts); (4) a drift out of lockout anchored 0.3 s late, ROM +11 to +15 cm (TA_HOLD_V; the start rule
  for a hold whose run reaches into the descent's slow start); (5) a soft rerack or a 4 to 6 cm unrack made
  the unrack a rep (the hooks); (6) a rerack fumble re-aimed above lockout dropped the last rep (the rack
  cut); (7) the pod pulled off the sleeve after the rerack dropped it too (the closed-set clip).
- Three corrections to the integrated design, from the skeptics:
  - **The floor scope.** Impacts-only blanking also changed D41's floor path: a set-down the gyro doesn't
    confirm (a soft touch) was no longer blanked or a landing, and the 10-31 deadlift with its gyro damped
    near the set-downs read 7 reps for 5. On the floor every jolt counts again (byte-identical to before
    on every floor file); make_fake_motion.py deadlift_soft pins it (rep 3 +31% without).
  - **TA_HOLD_V 0.05 -> 0.03.** Lower read better at every step on the perturbations (1,174 -> 1,189 cards);
    holds between reps read 0.011 (p95), 0.028 (p99), drifts as slow as 0.019. A hold turned away costs little
    (the coarse zero crossing anchors it), a drift taken for one a lot. 0.02 would turn away the 10-31
    squat 360's hold (0.021).
  - **The start rule's margin.** Its test fired on any drop at all (22 of 164 holds under 1 mm). Unconditional,
    an 8 s breath read -63% and a stress card went; with 5 mm (TA_START_DROP) every total holds, and without the
    rule the perturbations lose 10 cards (the working set with 1.25x harder chest bounces: rep 1 -16%).
  Also: the hooks rule got its own constant (TA_HOOK_MOVE, 0.15 to 0.25 read the same; 0.12, sets.py's
  RACK_MIN_ROM, made 8 unracks reps); jolts() is gone, so a stale call fails instead of blanking a window.
- The review of the result (4 reviewers, a skeptic each) found one more: the first descent eased into out of
  the setup hold. The hold's flat run goes on into the slow start, it passes TA_HOLD_V over its whole length,
  and its end, where the first anchor averages, is the moving bar: a paused single -12% (ROM +12 cm), a heavy
  triple's rep 1 -30%. Before D43 the single read +4% but ROM +23 cm, and the triple 1 of 3. The start rule now
  covers the first hold too, mirrored: a run whose end is 5 mm under its coarse top averages the 0.5 s up to the
  top, its anchor where it was (moved earlier, it pulls the rack still in and the unrack becomes a rep).
  make_fake_bench.py's paused_single_slowstart and heavy_paused_triple_slowstart pin it (within 0.6% and 2.4%).
  It helps beyond them: suite cards 170 -> 174, stress 132 -> 144 (touch-and-go heavy singles +28% -> -0.5%, a
  touch-and-go heavy triple +15% -> -1.2%), the perturbations 1,189 -> 1,216. Its cost: tng_working_r32_pause's
  rep 1 goes -2% -> -5%. Its first anchor had averaged a bar already moving down (-0.097 m/s), which offset
  the anchor after it: that lockout's run goes on into the next descent, TA_HOLD_V turns it away, and it
  anchors on one coarse sample with the hold's tremble in it (up to 0.04 m/s). Judging such a run by its first
  0.5 s fixes that rep (-3.9%) but no total, so it's left (Open).
- Dropped in the integration: a rest filter for sigma's live dips (0 of 180 suite runs and 0 of 156 stress
  runs change without it; the rack cut does its job). Coupled: the bench high-pass needs the rack cut (live ok
  77 -> 26 without it).
- On the suite: count right in 180 of 180 runs (96 before), card right in 174 (58), the live view in 77 (36,
  every 0.25 s: count right, never ahead, no dip, the newest rep's MCV within 8%). Stress (26 long holds,
  singles, grinds, breaths; 156 runs): cards 12 -> 144. Seeds 1 to 10 (600): 203 -> 575; the perturbations
  (1,312): 248 -> 1,216. Every older result is unchanged (71 regression guards byte-identical, 0 of 777 older
  judge results moved, the self-tests).
- The cost: fast touch-and-go reps with lockout pauses read low, about -4 to -8% at 0.8 m/s and up to -14% on a
  1.0 to 1.15 m/s warm-up at a 32 cm ROM (-10% at 44 cm; the light controls' set means -1.5 to -5.9%). The longer high-pass puts a sharp lockout's anchor on the hold (true 0)
  instead of its rising side, and the end walk (D32) then follows the corrected velocity's slowly decaying
  residual (+0.01 m/s) into the pause: rep ends 29 to 47 ms late. Any sigma from 1.25 to 2 does it; 1.5 and up
  are needed for long holds and singles. It flattens the light end of a load-velocity profile.
*Why:* bench only where the reason is the bench's (the slow paused rep; 1.5 s broke the 10-31 squat by +8.4% and
the deadlift to 6 for 5); everywhere else a rule leaves older data exactly as it was, so the real squats and
deadlift keep D40's video scores. The bounce is the bar's own motion, the hooks are where a racked set starts
and ends, and a pause that moves isn't a zero.
*Guarded by* parity.mjs: make_fake_bench.py's GUARDS (16 sets, raw and calibrated, against each file's
_truth.json: count, each rep's MCV within 5% (7% for tng_working_r32_pause, 12% for the fast controls and the
2.4 g bounce), the set mean, ROM within 2 cm on single_first8 and 3 on paused_single_slowstart), the same
calibrated live but pod_lift_10s (never ahead; no dip but the slow-start sets' one at 7.25 s, as before D43), make_fake_motion.py deadlift_soft (6 reps within 5%) and
squat_breath (breaths and eased-down lockouts between squats: 5 reps within 8%; a check that the lockout rules
leave a squat as it was, which they decide nothing on). Each rule reverted alone fails its own guard: the bench
high-pass (paused_single no card, heavy_paused_triple 1 of 3), TA_HOLD_V (drift_working_r32 4 of 6), the start rule
between reps (tng_heavy_triple -15%) and before the first rep (paused_single_slowstart -12%,
heavy_paused_triple_slowstart -30%), the outer exemption (single_first8 -16%, ROM +7 cm), the impact blank and the
rack hit (bounce_24g no card), the rack cut (rerack_fumble_5cm 4 of 5; live, tng_light_r44_nopause dips at the
rerack), the hooks (unrack6_soft 6 for 5; the bottoms filter alone: rep 5 -7%), the clip (pod_lift_10s 4 of 5), the
floor scope (deadlift_soft +31%).
*Open:* the end walk above, next. A lockout between reps whose run goes on into the next descent anchors on one
coarse sample (tng_working_r32_pause rep 1 -5%; judging the run by its first 0.5 s gets -3.9%). No real bench recording: IMPACT_W against a real chest bounce is unmeasured
(squat bottoms read 6 to 18 deg/s, 5x under it; a bounce over 100 deg/s empties the card). The still detector splits
tng_working_r32_nopause (rep 2 -20%). Live: a paused rep shows -36 to -50% while it's the newest; a pod pulled off 1 to
2 s after the rerack empties a raw pod's card in 6 to 8 of 10 runs and pod_lift_10s's live view ends at 0 (the
off-bar rest sets the still limits and g, D20); one set's live result swings with the replay grid's phase (and
before D43 the 10-31 squat at 1410 dips twice at a later phase, the ham curl 0 to 2: parity passes them at its own
start only); deadlift_soft dips once live (as before D43). The closed-set clip runs on redone windows only: after a
soft rerack (no impact, so no redo) the pod pulled off before the double buzz still loses the last rep or adds one
(right in 1 to 2 of 6 runs at 1 to 14 s; after the buzz 6 of 6). Not reached: mid-set partials and spotted reps (only
the set's ends are trimmed), a grip that rattles up to the unrack (no lead still, no card), touch-and-go with no
lockout stop (count right, one rep -19 to -20%) or a 0.3 s ease-down out of each lockout (the unrack counted: 7 of 6),
a very soft chest touch (3 mm in 0.4 s: a paused single no card, the triple's grind lost), tempo eccentrics of 3 to 4
s (no card), grinds at 0.10 m/s or slower over 3.5 s (no card, or the last rep lost), a ROM of 27 cm or less (25 cm: the working sets 3 to 4 of 6; 27 cm: one rep +44%), a 16 to 18 cm unrack with a
soft rerack (a rep, as before).

**D44 (2026-11-03): deadlifts from the floor, from the filmed 11-02 sets. A deadlift rep starts at the pod's own
turnaround, the bottom of the bar-bend dip (Quentin's call), and nine floor rules carry it: a set-down closes at its
first contact; a floor rest is flat to 0.2 m/s² and its height is read at the coarse low; a flat run into the window's
end after a jolt is a coarse stop; a touch anchors where its jolt's blank ends; the start still anchors a window that
has a rep and nothing else on the floor before its first top; live, a held-back turnaround is still a rest's
neighbour and the open tail loses its last still's bias; on the deadlift a stop the bar moves on up out of isn't the
rep's end. TA_FLOOR_REACH 1.0 -> 1.5 s.**
- From the 11-02 gym session (two sets of 3 at 389 lb, both filmed). Set 2 logged no set: the hands came off the bar
  between reps, so each rep had its own window between floor stills, and its end still began 0.4 to 0.7 s after the
  set-down, too late for a coarse stop; the closed window zeroed the rep, and dropRackMoves took the one 9.6 cm rep
  left. Set 1 read 6 to 11 cm long: each landing closed at its last bounce, which put +0.04 to 0.05 m/s on the pull.
  Calibrated, set 1's rep 3 read 13.7 cm: its floor "pause" ran 1.9 s into the slow pull. The video showed the bar
  bending too: the sleeve end (the pod) sinks 6.5 to 10 mm (mean 8.4) as the pull loads the bar, a rigid tilt of 2.2
  to 3.5°, and stays bent through the pull. Truth is the pod's concentric from the dip's bottom (Quentin: the pod's
  turnaround, not plate liftoff, which would read these pulls -1 to +11% faster), synced to the video on the plate's
  spin and the clangs; ROM within 8 mm.
- Three design workflows (11-02/03): root causes by oracle experiments; a known-answer suite at the pod's rate on 11-02's
  measured physics (the dip in two phases, set-downs bouncing in 1 to 5 jolt runs with the rattle reading biased down,
  grip vibration and floor hum, hitches and the grind's two stalls; 29 scenarios x 3 seeds x raw/cal = 174 runs, now
  `analysis/make_fake_deadlift.py`); four fix families, four fixers and an integration; then skeptics (608 runs of
  held-out physics, a sweep of every constant, a mutation table, two code reviews) and a last review of the final with
  an adversarial verifier per lens. Scripts and results are kept in analysis/out/deadliftfix-2026-11-03/ (gitignored).
- The rules (algorithm.md §6c numbers them), what each fixed:
  1. First contact (LAND_GAP 0.3 s): set 1's ROM over-read, +10 to +11 cm on reps 1 and 2, +19 cm on set 2's grind.
  2. The floor's flat test at 0.2 (TA_FLOOR_FLAT_A): the slow pull's middle as a rest (set 2 rep 2 read 7 cm without it,
     and calibrated set 1 lost a rep).
  3. The floor height at the coarse low: the high-pass bends a 4 s floor rest 17 to 37 cm up by its middle, so set 1 raw's
     rests between reps never anchored (the suite's next rep -22 to -40%).
  4. The stop into the window's end: a heavy single's still begins 0.35 s after its set-down; without it the coarse
     velocity kept -0.8 m/s from the blanked landing and the single read 0 reps.
  5. The touch: a touch-and-go anchor at its jolt's blank end, not the coarse low 0.13 to 0.22 s into the pull (-27 to
     -51% on touch-and-go reps).
  6. The start still on the floor, in a window with a coarse top, also in place of a rest whose low is within
     TA_FLOOR_REACH of it: set 2 itself (without it rep 3 is lost raw, and the suite's count falls from 174 to 106).
  7. Held-back neighbours (live): a rest measured against the buffer's end fails its height test while the set-down rings,
     and the count dips for a moment at the set's close.
  8. The open tail's bias (live; the tail and an open window still on its rest): 11.6 s of hands on the bar with no still
     drifted into a 12 to 25 cm phantom rep before set 1's rep 1 (shown 3 s, then a dip), and sweep_v025 raw dipped too.
     The still's mean against the drift it explains: set 2 raw -0.031 / -0.03 m/s², calibrated -0.0052 / -0.0065, set 1
     +0.003 / +0.004 to 0.0055. Not off the rack: a squat's rack still made its walkout a 16 cm rep.
  9. Past a hitch (the deadlift only; HITCH_V 0.1 x the peak, HITCH_ROM 8 mm, from the stop's drift floor): set 1 rep 2's
     lockout hitch (+19% at the hitch) and set 2 rep 3's grind through two stalls (ROM -4 to -6 cm).
- Dropped: a closed floor window's end still anchoring after a landing. Rule 4 does that work first on every file and
  seed, so it changed nothing anywhere and no guard could hold it. Also tried and dropped: a mean or median drift floor
  for rule 9, and a one-move cap on it (below, Open).
- From the skeptics and the last review: TA_FLOOR_REACH 1.0 -> 1.5 (D41's: the coarse low lags the floor when no
  set-down restarts the coarse pass, and after a slow pull's 1.2 s sink into the dip it lands 1.17 to 1.19 s after the
  still, so the set lost its last rep; 1.3 to 2 recover 33 of the 608 held-out runs, the suite's cliff is 0.75 to 0.85);
  rule 6 needs a coarse top in the window (a pre-set bump with no rep was anchored, and a slightly harder one moved
  the set's floor: set 1 raw lost rep 1 at IMPACT_A 24); rule 9 on the deadlift only (a stack's squeeze or a shrug at the
  top stops and rises too, and no stack recording hitches); rule 1 chains only within the span's second half.
  LAND_GAP stays 0.3: 0.8 took a late re-contact (synthetic: at 0.35 s or more the chain broke, ROM +16 to +49 cm) but
  made a jolt in the descent the landing, and the real data have those, not late re-contacts (10-31 rep 3 has a
  15 m/s² spike 0.75 s before its first contact; slightly harder, it read ROM -15 cm at 0.8).
- Against the video (every rep within 8% MCV and 3 cm ROM, each set's mean within 5%): set 1 calibrated -1/+1/+6% (ROM
  -0.5/-1.1/-0.5 cm), set 2 raw +1/-4/+2% (+1.2/-2.8/+0.9 cm) and calibrated +3/-2/-3% (-1.9/-1.6/-1.4 cm); set 1 raw
  -1/+3/+11% (-0.3/+0.7/+5.1 cm). Before D44: set 1 +15/+18/+13% (ROM +6 to +11 cm), calibrated 2 of 3 reps, set 2 no
  set. Set 1 raw's rep 3 (+11%, +5.1 cm) is mostly the uncalibrated pod's accel x/y bias, turned onto the vertical by
  the set-down's roll of the sleeve: correcting that bias alone gives +6.4% (-0.3 cm); HEAD read it +13% (+6.2 cm).
  Live, neither set dips or runs ahead.
- On the suite the count is right in 174 of 174 runs (76 before), the card in 64 (9), the live view in 134 (18); on
  the held-out physics (608 runs, seeds 4 to 7) the count in 560 (126), the card in 149 (0), and live at seed 4 in
  105 of 152 (HEAD 2). The 10-31 deadlift reads as before (worst -1.3%), and no squat, bench or phone result moved
  (the bench canon: 0 of 180 runs). 31 of the 1,117 older judge results changed, all on floor lifts:
  deadlift_creep and deadlift_soft (two reps 2% slower, each closer to its exact answer: +2.7 -> +1.0%, +2.8 -> +1.1%),
  and the open tail's bias on cut files (the 10-23 deadlift's 0 cm sixth "rep" gone, the lat-raise cuts find the reps
  the whole file has and its live count no longer dips, garbage tail reps on fake_session_squat [machine] gone).
- The cost: set 2 raw's rep 2 reads -4% (ROM -2.8 cm) where it read -3% (-2.0 cm) before TA_FLOOR_REACH 1.5, rep 1
  moving from +4% to +1%. A bump of 5 to 14 cm in the setup stays 0 reps, as on HEAD (rule 6 needs a coarse top).
*Why:* the deadlift's floor is where every one of these errors lives, and each rule is the floor's physics: the bar
lands at its first contact, rests flat, rests where the coarse low says, comes to rest when the jolt stops, turns at its
touch, leaves from the still it stood in, and drifts by what its last still read. A hitch is the lifter's, not the
sensor's. Squat and bench never enter a floor branch, so they read exactly as before (bit for bit on 8,508 runs).
*Guarded by* parity.mjs: make_fake_deadlift.py's GUARDS (8 scenarios, a row per sensor: session against each
_truth.json with per-rep MCV and ROM, and live), and both 11-02 sets raw (rep parity whole, cut at 45%, 70% and inside set
2's second window at 42.75 s; session against the video at 8%/3 cm a rep but set 1's rep 3 at 13%/6 cm, the raw sensor;
live with the MCVs, set 1's rep 3 not held). A missing guard file fails. Each rule reverted alone fails guards (the D44
workflow's mutation table, on the guard set's Python side): first contact 20 (reset_fast5 -16 to -35%, set 1 raw
+24%), the flat test 15 (held3_quiet 2 of 3, set 1 raw rep 3 +17%), the floor height 8 (held3_one raw rep 2 -40%,
held3_hitch raw +15%), the stop into the window's end 7 (single_heavy 0 reps, set 2 raw 1 of 3), the touch 3 (tng5's
reps 2 to 5 -27 to -51%), the start still 7 (single_heavy 0 reps, set 2 raw 2 of 3), held-back neighbours 1
(held3_quiet raw live: a dip), the tail's bias 3 (live: sweep_v025 raw a dip and 10 passes off, set 1 raw a dip), past
a hitch 8 (held3_hitch raw +18%, set 1 raw rep 2 +19%) and its drift floor 2 (held3_hitch raw +18%, session and
live); and TA_FLOOR_REACH back at 1.0 fails reset_sink12_hold0 (2 of 3, raw and calibrated; at 1.5 seeds 1 to 7 count
right, 28 of 28).
*Open:* the hitch rule can walk a rep's end through a lockout hold that sways and drifts up (synthetic: a 0.6 to 2 s hold,
drift +0.01 to +0.03 m/s, sway 0.035 to 0.05 m/s at 2.5 Hz: -22 to -48%); the nearest recorded lockout, 10-23 rep 1,
clears HITCH_V (0.12 x its peak) and stops about 3 mm short of HITCH_ROM; a one-move cap halves it but changes nothing
measured, and no local test told a sway from a hitch. A late re-contact past LAND_GAP breaks the first-contact chain
(above); a prototype bound (a step past 0.3 s only to a jolt run at least 0.7 x the chain's hardest) took both it and
the descent spike. TA_FLOOR_FLAT_A has a narrow window (0.19 to 0.23 pass everything). Before D44 and still: a fidgety
floor before the set and a quiet still after it set noise-matched still limits that every rest between reps fails, and
the set is zeroed (16 of 406 suite runs at seeds 4 to 10); a set-down soft enough that the gyro stays under 100 deg/s
never redoes its window (unmeasured: every real one reads 300 to 1130); slow touch-and-go and touch-and-go after a
reset are miscounted; a fast rep after a hands-on rest reads -45% raw (0% calibrated). Rule 5 is unproven on a real
touch-and-go recording. The suite's floor break is 4 to 10x calmer than 11-02's, so its card misses partly overstate
the real error (a still straddling the dip bottom is the largest single cause; the raw x/y bias and a start walk that
halts on a lull, both in the real sets too, come next); with real-sized break vibration one reset set logs 0 reps (not
investigated). Slow reset pulls over-read with pull time (+8 to +14% at 3 s, raw). The end walk (D43 Open) still.

**D45 (2026-11-02, in progress): the pod's link moves onto CRP, Ansync Labs' Channel Register Protocol
(0x0B), on Bluetooth and USB, carrying the PodLink v1 frames unchanged. The frames become register
values and pushes, and the CTRL ops a register write. Ansync's C engine and JS client are vendored.**

*What moves:*
- INFO, STATUS and CAL are readable registers.
- Every DATA and STATUS frame is pushed as a CRP WRITE with txn 0.
- The CTRL ops are written to one CTRL register, byte for byte.
- A PUSH register does the CCCD's old job.
- Bluetooth has one service with RX and TX (Ansync's 0x5090 / 0x50b0 / 0x50b1). USB uses CRP's
  length-prefixed framing.
- Spec: `docs/ble.md` section 2.

*Why (Quentin, 11-02):* one protocol for the pod and Ansync's devices. Their dashboard and tools then
open the pod, and its registers are discoverable rather than a frozen table.

*Decided with Quentin:*
- Vendor Ansync's engine and client. They gave their OK for this public repo.
- Both links speak CRP.
- Keep the frames, pushed, rather than move the samples onto a CRP bulk channel, which was the
  alternative. A bulk channel would have needed about six fixes to the engine's bulk pump (offsets
  advance on a failed send, an empty chunk ends the channel, READ ignores its start offset) and a
  rewritten decoder and simulator. Pushed frames keep the decoder's semantics and its 36 scenarios.

*Choices along the way:*
- **USB keeps its text mode as the independent truth,** and a HELLO switches the port to CRP (coil's
  coexistence model). usb_clock, compare_ble_usb and check_soak check the frame path, so they must
  not share it.
- **PUSH = 1 is written after the INFO and STATUS reads.** It keeps D28's guarantee: a STATUS pushed
  before the reconnect INFO would read as a reboot without a new boot_id.
- **The pod ignores CRP liveness on Bluetooth.** A phone's WebView throttles timers with the screen
  off, and link supervision already covers it.
- **Clients never retry a transaction,** since the engine re-runs a WRITE.
- **A READ that wouldn't fit the MTU is answered BUSY,** and the pod's MTU exchange moves to
  connect + 50 ms.
- **n_max = (MTU - 23) / 14,** for the 6-byte push header. It is still 16 at MTU 247, but 4 samples
  now need MTU 79.
- **CRP builds advertise from a new static random address,** so no phone keeps the cached v1 table.
  The name changes once.

*Engine patches* (`firmware/pod/src/crp/README-VENDOR.md`): coil's five, plus:
- `push_change` returns its result;
- the tick's 1 KB buffer is static, for the 4 KB loop stack.

*Plan:* stages S0 to S10 (session log 11-03).

*Baseline* (S0, PodLink v1 with the engine vendored and unused): 139,472 B flash, 147,008 B RAM
(90,560 free).

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
