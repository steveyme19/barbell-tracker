# Sensor options beyond the IMU

Researched October 2026. Short version: an IMU + laser fusion works in simulation and
fixes the IMU's worst case (slow reps in sets with no pauses). The open question is
whether a real laser on a real bar in a real gym behaves like the simulation, which a
cheap bench test can answer early. A string encoder is very accurate and makes a great
homemade reference for validation.

## The key idea: complementary errors

| | short term (one rep, fast changes) | long term (seconds and up) |
|---|---|---|
| **IMU** (integrate accel) | great: smooth, fast, detailed | bad: drifts |
| **Distance sensor** (measure position directly) | bad: noisy, and getting velocity means *differentiating*, which amplifies noise | great: no drift |

Their weaknesses are opposites. Combine them: take the IMU for fast changes and the
distance sensor for the slow truth. That's a **complementary filter**: high-pass the
IMU-integrated position, low-pass the measured distance, add them. (A Kalman filter
is the fancier version.)

You've already met this idea: Madgwick does the same thing for orientation. The gyro
handles the short term, the accelerometer's gravity direction handles the long term.

For this project, the distance sensor wouldn't need to be precise sample to sample. It
would just need to be right *on average over a second or two*, which is exactly what
fixes ROM drift on slow grinders.

## Options

### 1. String encoder (homemade LPT): very accurate, tethered

What GymAware, Tendo, and Vitruve are. A string from a floor unit clips to the bar. The
string winds on a spring-loaded spool, and a rotary encoder reads the spool angle.

- **Resolution is silly good.** An AS5600 magnetic encoder (12-bit, 4096 counts per turn)
  on a 20 mm spool gives about 0.015 mm per count.
- **Forgiving of horizontal motion**, for the same cos-vs-sin reason as before. If the bar
  drifts 5 cm sideways at 1 m height, the string length only changes by ~1 mm. If the
  base is 10 cm off from directly under the bar, velocity reads 0.5% low; at 20 cm,
  2% low.
- **Real error sources:** string stretch (use Dyneema or Kevlar), spool radius changing
  as string winds in layers (use a single-layer grooved spool, or calibrate against a tape
  measure), and base placement.
- **Downsides:** tethered to the floor, vertical only (no bar path without an angle
  sensor), and something to trip over.

**Best use:** your own reference instrument for phase 3. Cheaper than borrowing lab time,
less tedious than tracking video by hand, and a good build for your PCB and Fusion
skills. Validate it against a tape measure first, the same way step 0.1 validates the
IMU.

### 2. Laser distance sensor on the bar, pointing at the floor: fusion candidate

The GymAware FLEX approach (it uses a floor mat as the target). FLEX had poorer validity
than LPTs, especially at high velocities (see prior-art.md). Fusing with an IMU
sidesteps that: the IMU handles the fast part.

| sensor | range | beam | rate | accuracy | notes |
|---|---|---|---|---|---|
| VL53L1X (ST) | up to 4 m | 27° cone (narrower with a programmable ROI) | up to 50 Hz | not specified on the product page | at 1.4 m the full cone is ~67 cm wide: it'll see plates, rack feet, your shoes |
| TF-Luna (Benewake) | 0.2 to 8 m (only 2.5 m on 10%-reflective targets) | 2° | 1 to 250 Hz | ±6 cm (0.2 to 3 m), 1 cm resolution | 35 × 21 × 12.5 mm, under 5 g, ~70 mA, needs 3.7 to 5.2 V supply |

Problems to solve if you try it:

- **Random vs. systematic error is the big unknown.** The spec says ±6 cm and 1 cm
  steps. Random noise mostly averages out (see the simulation below). Systematic error,
  like reading 2% long or changing with distance or floor color, passes straight
  through, even with fusion. Calibrate it like step 0.1: known heights, tape measure.
- **What does the beam hit?** It has to see floor, not plates, rack feet, or plate
  storage. That needs a narrow beam (TF-Luna's 2° is ~5 cm wide at 1.4 m) and a clear
  spot under the sleeve end.
- **Black rubber gym floors reflect poorly.** TF-Luna's range drops to 2.5 m on 10%
  reflective targets. A squat lockout is well under that, but it's less margin.
- **Bar roll tilts the beam.** The IMU knows the tilt, so correct with
  `height = distance × cos(tilt)`. Small tilts barely matter (cos again). Big rolls point
  the beam away entirely, so the IMU has to cover those gaps.
- **Deadlifts start too close.** TF-Luna's minimum range is 0.2 m, and the sleeve is
  about 22 cm off the floor with standard plates.
- **Motion during a measurement.** At 1 m/s the bar moves 2 cm during a 20 ms reading.
  Timestamp each reading at the middle of its window.
- **Power and size.** ~70 mA is a lot next to the XIAO. The supply range means you'd need a
  boost converter from a LiPo. And it roughly doubles the device size.

#### Does the fusion actually work? (simulation)

`analysis/demos/imu_laser_fusion.py`: 3 squat reps with **no pause at lockout**, the last
one a 0.15 m/s grinder. Fake IMU with bias, wandering bias, 1% scale error, and noise.
Fake laser at 100 Hz with an offset, 1 cm noise (assumed), 1 cm steps, and a 0.4 s
dropout. Grinder rep results:

| method | MCV error | peak velocity error |
|---|---|---|
| IMU only (corrected only at the set's start and end) | +38% | +29% |
| laser only (smooth + differentiate) | -1% | +114% |
| fused, 1 Hz crossover | -1% | +12% |

Why each one behaves that way:

- **IMU only** drifts over the whole ~12 s set because there's nowhere to anchor
  between reps.
- **Laser only** gets MCV right because MCV is just distance over time, and random
  noise averages out over a whole rep. But peak velocity needs the *instantaneous*
  slope, and differentiating noise blows up.
- **Fused** gets both. The laser stops the drift, the IMU supplies the fast detail, and
  the filter even learns the IMU's bias from the laser.

What the simulation also shows:

- **Fusion doesn't fix a laser's systematic errors.** Make the laser read 2% long and
  the fused MCV comes out ~2% long. Long-term, the filter trusts the laser.
- **The crossover has to match the laser's noise.** With 3 cm of laser noise, a 1 Hz
  crossover lets that noise into peak velocity (+69% on the grinder). Dropping it to
  0.3 Hz fixes peak (+1%) but lets more IMU drift into MCV (-6%). Measure your laser's
  real noise before picking it.
- **It removes the need for stillness anchors.** With a laser, sets with no pause
  at lockout (algorithm.md's known gap) stop being a special case.

### 3. Camera + printed marker: validation upgrade

Stick an AprilTag (or ArUco marker) of known size on the device and film at 240 fps.
OpenCV detects it automatically every frame. Since you know the tag's real size, it
gives you scale *and* distance to the camera, which fixes the parallax problem with
manual tracking. Good upgrade to phase 3.3.

### Ruled out

- **Barometer.** Air pressure changes about 12 Pa per meter, so 1 Pa ≈ 8.5 cm. Just moving
  the sensor through air at 1 m/s creates ~0.6 Pa of dynamic pressure, which looks like
  ~5 cm of fake height. Add drafts and HVAC and it's hopeless for cm-level ROM.
- **Ultrasonic.** Wide beam, echoes off plates and the rack, slow, and sensitive to air
  temperature.
- **UWB radio ranging.** Decimeter-level accuracy. Way too coarse.
- **Optical flow (mouse-style) sensors.** Measure motion *across* a surface, not distance
  to it.

## Plan

- Build the IMU-only pipeline first. Fusion needs it anyway, since the IMU half of the
  filter is the same code.
- Optional step 1.6: laser bench test. Measure the TF-Luna's real noise, linearity,
  and black-rubber-floor behavior. Cheap and quick, and it answers the big unknown early.
- Optional step 2.8: if the bench test looks good, try fusion on real bar data.
- Optional step 3.0: build the string encoder as your reference.
- Use an AprilTag for video tracking in 3.3.
- Decision O8: whether the laser goes into the final hardware gets decided after
  phase 3, with IMU-only vs. fused validated side by side.

## Sources

- TF-Luna specs (Mouser): https://www.mouser.fr/new/benewake/benwake-tf-luna-lidar-sensors
- VL53L1X (ST): https://www.st.com/en/imaging-and-photonics-solutions/vl53l1x
