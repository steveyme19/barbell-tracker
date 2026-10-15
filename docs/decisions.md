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

**O4: Arduino board package for the XIAO nRF52840.**
Seeed ships two (mbed-enabled and non-mbed). Their BLE libraries and examples differ.
Pick one in 1.1 and stick with it.

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
