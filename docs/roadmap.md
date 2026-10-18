# Roadmap

Each step is sized for one sitting. Check it off when it's done *and* you can
explain why it works. Per-rep processing is specified in
[algorithm.md](algorithm.md), cross-rep and cross-session stuff in
[analytics.md](analytics.md). Why things are the way they are is in
[decisions.md](decisions.md). What to buy for each step is in
[hardware/BOM.md](../hardware/BOM.md).

## Phase 0: Phone prototyping (no hardware needed)

- [x] **0.1 Phone lift height.** phyphox CSV → Python → height within ~2 cm.
      See [01-phone-lift.md](01-phone-lift.md). *Done 2026-10-16: 5 lifts to 27.9 cm,
      4 of 5 within 2 cm using the tilt-proof magnitude signal (D9).*
- [x] **0.2 Fake reps.** *Done 2026-10-16 with `analysis/reps.py`: paused set ROM
      25.2 ± 0.5 cm, no-pause set 27.3 ± 0.8 cm.*
      Original plan: Move the phone up and down like reps. Record one set with a
      pause at the top of each rep, one set without, and a few slow "grinder" reps where
      you stall halfway up (algorithm.md §8). Implement stillness detection
      (algorithm.md §5), integrate still-to-still (§6), find the bottoms as zero
      crossings (§7), get per-rep ROM.
      Also run `analysis/demos/bottom_anchor.py` and explain the result in your own words.
- [ ] **0.3 Orientation + 2D.** Record accel + gyro together (a phyphox custom
      experiment, or an app like Sensor Logger). Implement 6-axis Madgwick in Python,
      rotate acceleration into the world frame, subtract g. Tests:
      - tilt the phone mid-lift, and **roll it around its long axis** mid-lift (like a
        sleeve spinning). Both should match a flat lift.
      - **slide it flat along a table** a measured distance: the horizontal version of 0.1.
      - lift it diagonally onto the table and plot the 2D path (§11). Measure the true
        horizontal and vertical distance and compare.
- [x] **0.4 Web app v0.** *Done 2026-10-16: `webapp/`, phone sensor + replay + live
      reps + CSV export. Tested headless with fake sensor events.* Static page on GitHub Pages, built as
      `source → algorithm → UI` from day one (see `webapp/README.md`). Two sources:
      - `PhoneSource`: live DeviceMotion
      - `ReplaySource`: plays a recorded CSV back at its real timing
      Live plot, record button, download CSV. Measure the sample rate your phone
      actually gives you (`event.interval`).
- [x] **0.5 Algorithm in JS + parity test.** *Done 2026-10-16: `webapp/reps.js` matches
      `reps.py` to 0.000 mm on all four test files (`webapp/test/parity.mjs`).* Port 0.2 to the browser. Run the JS version
      on recorded files through `ReplaySource` and compare against Python's output on
      the same files. They have to agree (*start*: ROM within 1 mm, MCV within
      0.005 m/s). Algorithm tuning happens in Python only. JS just has to match it.
- [x] **0.7 No-pause drift fix.** *Done 2026-10-16: lockouts leveled (algorithm.md §6b,
      D10), in both `reps.py` and `reps.js`, parity 0.000 mm on five files. Also fixed
      the coasting check throwing out the real end-of-set still after long sets.*
- [ ] **0.6 (optional) Fake squats.** Tape the phone to a PVC pipe or an empty bar
      and squat. First taste of real bar motion before hardware shows up.

## Android app (alongside phases 1 to 4)

Capacitor wrapper around the web app (D12). Steps A0 to A4 in
[android.md](android.md): setup, app shell, native sensors, files and screen, then
Bluetooth to the XIAO (same time as 4.1).

- [ ] **Session mode** (2026-10-16, D17/D18): start once, sets + rest found
  automatically, set cards with weight / RPE, RPE estimate v0, auto-save per set +
  `session.csv`. Built and tested on fake + joined real data; **first real workout
  still to do**.
- [x] **A0** setup · [x] **A1** shell runs on the phone · [x] **A2** native sensors
  200+ Hz · [x] **A3** save/share CSV, screen on · [ ] **A4** BLE

## Phase 1: Hardware prototype (XIAO nRF52840 Sense)

- [ ] **1.1 Hello board.** Blink + Serial print. Pick the board package (decision O4).
- [ ] **1.2 IMU over USB.** Read accel + gyro at a fixed rate, print over serial, log
      to CSV in Python. No BLE yet. One new thing at a time.
- [ ] **1.3 Characterize the IMU.** (Print the pod first: it's the calibration cube,
      [hardware/enclosure.md](../hardware/enclosure.md).) Set sample rate and ranges (O5). Measure noise and
      bias at rest. Do a 6-position calibration (each face down, each axis should read
      ±g) to get per-axis bias and scale. This is the fix for the scale error that
      zero-velocity correction can't catch. Then calibrate **gyro scale** per axis
      with full-turn tests (algorithm.md §1). Save both calibrations in firmware.
- [ ] **1.4 BLE streaming.** Custom service, several samples batched per
      notification, each batch with a sequence counter and device timestamp. Receive
      with bleak. Prove you aren't dropping samples. Output files use the same columns
      as phone recordings so every Python script works on both.
- [ ] **1.5 Battery.** LiPo wiring, charging, runtime test. Read Seeed's battery docs
      carefully before wiring anything.
- [ ] **1.6 (optional) Laser bench test.** TF-Luna on the XIAO, powered from USB 5 V.
      Point it at the floor from known heights (tape measure): noise at a fixed height,
      error vs. height (linearity), black rubber vs. a lighter surface. Gives you real
      numbers for `demos/imu_laser_fusion.py` (sensor-options.md §2).

**Phase 1 rules**

- Sample timestamps come from the **device**, never from when the data arrived on the
  laptop or phone. BLE delivers in bursts with gaps.
- Don't time samples with `delay()`. Use a timer, the IMU's data-ready signal, or
  its FIFO. Uneven sampling is fixable; *unknown* sampling isn't.

## Phase 2: Algorithm on a real bar

- [ ] **2.1 Data collection.** Temporary mount (velcro or zip ties) at **both**
      candidate spots from decision O3, alternating sets. Light sets of squat, bench,
      deadlift. Before each set, do the 2 s **bar roll gesture** (algorithm.md §11).
      Log **every set** in `analysis/data/sets_log.csv`, including your RPE call
      (analytics.md §6). Log the peak gyro rate you see (confirms the range from O5).
      Keep every raw file forever. **Keep logging like this for the rest of the project.**
- [ ] **2.2 Segmentation on real data.** Algorithm.md §5 to §8 on the real files. Tune
      the stillness thresholds. Count how many of your normal sets actually pause at
      lockout.
- [ ] **2.3 Metrics + quality flags.** §9 and §10: MCV, MPV, peak velocity, ROM, and
      flags per rep.
- [ ] **2.4 Bar path + bar tilt.** §11 and §12. Get the bar axis from the roll gesture,
      plot the 2D path for every rep.
- [ ] **2.5 Lever arm.** If an off-axis mount is still in the running, add §3
      compensation and check it with a set where you roll the bar on purpose.
- [ ] **2.6 No-pause sets on the bar.** §6b already handles them on phone data. On bar
      data: check how much your lockout height really varies set to set, see if the
      "uneven lockout" flag fires on good reps, check the per-lift profiles (bottoms for
      bench and deadlift, stills at lockout or floor height) against real bar sets, and
      try shorter micro-pause detection with the gyro (algorithm.md §6b and "Other drift
      ideas").
- [ ] **2.7 Decide where the algorithm runs** (O1).
- [ ] **2.8 (optional, if 1.6 looked good) IMU + laser fusion.** Temporary laser mount
      next to the IMU, pointing at the floor past the plates. Run the complementary filter
      from the demo on real sets, including no-pause sets and grinders. Phase 3 then
      validates IMU-only and fused side by side.

## Phase 3: Validation

- [ ] **3.0 (optional) Build a reference instrument.** Homemade string encoder: spool +
      AS5600 encoder + spring return, string clipped to the bar
      ([sensor-options.md](sensor-options.md) §1). Validate it against a tape measure
      first. Then it runs next to the video in every step below.
- [ ] **3.1 Video setup.** 240 fps slow-mo on a tripod. Camera perpendicular to the
      plane of motion, far back and zoomed in (less perspective error). Reference length
      in the **same plane** as the device, ideally on the device or the plate face.
- [ ] **3.2 Sync.** Sharp tap on the bar at the start of each set. It's a spike in the
      IMU data and a visible frame in the video.
- [ ] **3.3 Track.** Put an AprilTag of known size on the device and track it with OpenCV
      (sensor-options.md §3): automatic, and the tag's size fixes scale and parallax.
      Kinovea or Tracker (both free) as a manual fallback. Track the **device itself** in
      2D, not the bar center. The bar end tilts and whips more than the middle, so compare
      the same point.
- [ ] **3.4 Compare.** Per rep: ROM, MCV, peak velocity, and the 2D path. Mean error and
      spread (Bland-Altman plot). Do it for both mount spots. **Include heavy, slow reps**
      (below 0.2 m/s): those matter most for RPE and they're where commercial IMUs are
      weakest. **Repeat on a second day** to get between-day error, which is what
      session-to-session comparisons depend on (prior-art.md).
- [ ] **3.5 Smith machine test.** The bar rides on rails, so every rep has the same
      straight-line path. Any path spread the device reports is sensor error. That's the
      **noise floor** for consistency (analytics.md §2).
- [ ] **3.6 Deliberate faults.** Sets where you let the bar drift forward, cut depth by
      5 cm, hitch mid-rep. The numbers have to catch them.
- [ ] **3.7 Pick the mount.** Lowest error that also clears your rack. Closes O3.

**Phase 3 notes**

- Set pass/fail targets **before** you look at results so you can't move the goalposts.
  Starting suggestion: ROM within 2 cm, MCV within 0.05 m/s, path within 2 cm. Compare
  them to what published studies found for Enode and GymAware (docs/prior-art.md) so you
  know where the device stands.
- Why 0.05 m/s matters: if velocity drops roughly 0.01 m/s per 1% of 1RM (ballpark for
  squats), a 0.05 m/s error is about 5% of 1RM in your estimate.
- **Beating Enode** means beating it in numbers you define up front: MCV error on reps
  under 0.2 m/s, between-day error, bar path error. Comparing to published numbers is
  loose (different lifters, references, metric definitions). The fair test is the same
  reps against the same reference. If you can get time on an LPT or motion capture at
  Purdue (worth asking a biomechanics lab or the athletics strength staff), run it side
  by side with your video.
- Video MCV = ROM / duration, so it doesn't need differentiation. Video peak velocity
  does, and differentiating position is noisy. Smooth first.

## Phase 4: Real hardware + live app

- [ ] **4.1 BLE in the web app.** Add a `BleSource` (Web Bluetooth) that emits the same
      sample format as the other two. Nothing downstream changes. Rerun the parity test.
- [ ] **4.2 Set view.** Per-rep MCV, velocity loss %, bar path plot, alert (vibrate +
      sound) past a velocity-loss threshold. Show quality flags.
- [ ] **4.3 Load-velocity profile.** *(2026-10-17, D22: in the app from logged weights; check on real sets.)* Log load + MCV (or MPV, decision O2) per set, fit a
      line, extrapolate to a minimum velocity threshold for e1RM.
- [ ] **4.4 Enclosure v1** in Fusion 360 for the mount spot picked in 3.7. XIAO + LiPo +
      magnet seat that fixes which way the device faces along the bar.
- [ ] **4.5 Carrier PCB** in KiCad. The XIAO soldered down by its castellated pads
      (decision D5), battery connector, power plan, mounting holes that match the
      enclosure. See `hardware/README.md`.

**Phase 4 notes**

- Magnets are fine here. The XIAO's IMU (LSM6DS3TR-C) has no magnetometer, so there's
  nothing for them to mess up.
- Racking and plate impacts are violent. The enclosure has to survive drops, and the
  accel range may clip on impacts (flagged by §10 if it happens mid-rep).

## Phase 5: Training history + analytics

By now `sets_log.csv` has months of sets in it. That's what makes this phase possible.

- [ ] **5.1 Storage.** *(2026-10-17: sessions + raw sets in IndexedDB, history list, resume. Not yet: export JSON, delete.)* IndexedDB with the Session → Set → Rep model, `persist()`, export
      and import (analytics.md §7).
- [ ] **5.2 Features + consistency in Python.** Analytics §1 and §2 on your logged data,
      using the noise floor from 3.5. Rep to rep, set to set, session to session.
- [ ] **5.3 Depth line + sticking point.** Analytics §3 and §4.
- [ ] **5.4 RPE v1 in Python.** Calibration sets to failure, fit per lift, validate on
      sessions the model never saw (analytics §5). Squat and bench first.
- [ ] **5.5 Port to the app.** History view, consistency numbers in cm, sticking-point
      trend, RPE as a range. Parity test against Python, same as 0.5.
