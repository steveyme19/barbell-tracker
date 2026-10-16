# Algorithm spec (v0)

What the per-rep pipeline does and why. Python (`analysis/`) is the reference
implementation of this. Any JS or C port gets checked against it.

Numbers marked *start* are first guesses to tune with real bar data in phase 2.

```
raw IMU → 1 calibrate → 2 orientation → 3 lever arm → 4 vertical accel
        → 5 stillness → 6 integrate still-to-still → 7 turnarounds
        → 8 concentric phases → 9 metrics → 10 quality flags
        → 11 bar path (2D) → 12 bar tilt
```

Cross-rep and cross-session stuff (consistency, RPE) lives in
[analytics.md](analytics.md).

---

## 1. Calibrate

Subtract each axis's bias and divide by its scale factor (from the 6-position
calibration in step 1.3).

**Why:** scale error is invisible to drift correction. If the sensor reads 2% high,
velocity still ends at zero and everything comes out 2% high.

Same for the gyro. Its bias gets re-estimated at every still period, but its scale
doesn't. A gyro that reads 1% off turns a 180° sleeve spin into a 1.8° orientation
error, which is about 3 cm of bar path error (§11). Calibrate gyro scale per axis:
turn the device exactly one full turn and back to the same resting orientation, and
the integrated gyro angle should be exactly 360°. (A record player at 33⅓ rpm spins
at exactly 200 °/s if you have one.)

Gyro *noise* doesn't matter here. At 5 mdps/√Hz it adds about 0.007° of angle error
over a 2 s rep. Same lesson as step 0.1: bias and scale are the enemy, not noise.

## 2. Orientation

6-axis Madgwick filter (accel + gyro) gives the device's tilt relative to gravity.

- **Never assume a fixed sensor axis points up.** Olympic bar sleeves spin on their
  own bearings or bushings, and the shaft rolls in your hands or on your back. Wherever
  the device is mounted, it can roll around the bar axis mid-rep.
- No magnetometer needed. Heading (which way the bar points on the floor) doesn't
  matter for vertical motion. Only tilt does, and accel + gyro is enough for tilt.

## 3. Lever-arm compensation (only if the IMU is off the bar axis)

If the IMU sits a distance `r` from the bar's roll axis, it isn't just riding along
with the bar. When the bar rolls, the IMU swings in a little circle and feels extra
acceleration that the bar itself doesn't have:

- **centripetal:** ω²·r, pointing toward the bar axis
- **tangential:** α·r, from roll speeding up or slowing down

Orientation tracking doesn't remove these. They're real accelerations of the
*sensor*. Sizes with the IMU 35 mm off the axis (on the sleeve surface):

| roll rate | centripetal error |
|---|---|
| 30 °/s  | 0.01 m/s² |
| 90 °/s  | 0.09 m/s² |
| 180 °/s | 0.35 m/s² |
| 360 °/s | 1.4 m/s² |

Slow rolls don't matter. A fast spin would wreck the rep.

**Two fixes:**
- Mount the IMU **on** the roll axis (r = 0), e.g., centered on the sleeve end. Then the
  terms vanish. (See decision O3.)
- Or subtract them in software. The gyro measures ω, its derivative gives α, and r is
  fixed by your mount geometry: `a_bar = a_sensor - α×r - ω×(ω×r)`. This is standard
  in inertial navigation.

## 4. Vertical acceleration

Rotate the calibrated acceleration into the world frame using the orientation from
step 2, then subtract g. Result: vertical acceleration of the bar, up = positive.

## 5. Stillness detection (the drift anchors)

A sample is "still" when, for at least ~150 ms (*start*):

- gyro magnitude < ~15 °/s (*start*), and
- | |accel| - g | < ~0.3 m/s² (*start*)

**Why these tests:** they use raw sensor magnitudes only. They don't depend on
integrated velocity, so there's no chicken-and-egg problem.

**Lessons from the first real data (step 0.2, `analysis/reps.py`):**

- **Coasting looks exactly like stillness.** An accelerometer reads zero
  acceleration both at rest and when moving at constant speed, and the gyro
  doesn't help (a bar moving straight up doesn't rotate). Mid-rep stretches of
  near-constant speed passed the test above. Fix: confirm each candidate by
  integrating from the last confirmed still period. If the speed during the
  candidate is above ~0.1 m/s, it's coasting, so throw it out. Drift over a few
  seconds is far smaller than that.
- **A rolling window shrinks the still period.** A sample only counts as still once
  its whole window is quiet, so detected periods start half a window late and end
  half a window early. Widen them back by half a window. Without this, rep end times
  came out ~0.1 s late.
- **Hand-held pauses wiggle more than a phone on a table.** The phone version uses a
  0.15 m/s² wiggle limit. The bar will probably allow tighter.
- **Long sets break the coasting check.** After 10+ s without a pause, drift alone can
  push the integrated speed past 0.1 m/s, so the real still at the end of the set got
  thrown out as "coasting" and the whole set went uncorrected (first web app recording,
  2026-10-16: reps came out 78 to 131 cm). Drift can also cancel a real coast and let a
  fake still in. Fix, used only when the last anchor is more than 4 s back: look
  locally instead of integrating. A coast is entered by speeding up and left by slowing
  down (or the reverse on the way down), so the speed change over 0.5 s before and
  after has opposite signs. A real stop is entered and left the same way (stop at the
  top, then go down) or from/to rest. On every recording so far this agrees with the
  old check wherever drift was small, and it fixed that set (68.9 ± 1.6 cm). Stills of
  1 s or more are always accepted. Known blind spot: a slow grinder that coasts for a
  while before the candidate, so there's no speed-up inside the 0.5 s look-back.

Where you'll find stillness: before the first rep, at lockout between reps,
deadlifts resting on the floor, after racking.

## 6. Integrate still-to-still

For each gap between two still periods: integrate vertical acceleration to velocity,
then remove the straight-line drift so velocity is zero at both ends. This is exactly
step 0.1. Then integrate to position.

## 6b. No-pause sets: level the lockouts

The straight-line fix in §6 only knows one thing: velocity is 0 at both ends of the
window. That cancels a constant bias exactly, but drift that changes during the set
(it does, see the end of this file) is left over and grows over a long window.

With 3+ lockouts inside one window there's more to use: **the bar gets back to the
same height every lockout.** We don't know that height, but we know it's the same. So:

1. Model the velocity drift as a smooth curve, `e(τ) = d1·τ + d2·τ² + d3·τ³`, with
   τ = time / window length. Position drift is its integral.
2. Velocity is 0 at the end still, so `e(1)` = the raw end velocity (fixes d1).
3. Pick d2, d3 and the lockout height H by least squares so every lockout lands on H.
4. Subtract the curve. With d2 = d3 = 0 this is exactly §6.

Fewer lockouts get fewer powers (always at least one more lockout than unknowns), so
the fit can't be forced through every point. Code: `level_tops()` in `reps.py`,
`levelTops()` in `reps.js`.

**Live, mid-set.** While you're still moving there's no end still yet, so the newest
stretch can't use the end-velocity constraint. From the 2nd lockout on, it gets the same
fit with d1 fitted too (one power for 2 to 3 lockouts, up to three after that), and
only lockouts the bar has already come back down from count. It's provisional: the app
draws it gray and dashed, and redoes it properly once you pause. Simulated live on the
recordings (cut off every 0.5 s), the reps shown before the next pause went from a median
of 24 to 46 cm off their final value on the web app sets to about 2 to 3 cm. The first
rep of a set still has nothing to level against until the 2nd lockout.

**Why this isn't the bottom-anchor mistake from §7:** that one pinned v = 0 where the
drifting velocity already said it was, so it added nothing. This adds a fact the
sensor doesn't know: lockout height repeats. Same reason stillness works.

**Results (2026-10-16):**

| set | ROM spread (std) | bottoms spread | worst MCV error |
|---|---|---|---|
| your phone, no pause, §6 only | 0.9 cm | 12.2 cm | (no truth) |
| your phone, no pause, leveled | 0.4 cm | 2.8 cm | (no truth) |
| fake, curved drift, §6 only | 3.7 cm | 65 cm | 18% |
| fake, curved drift, leveled | 0.1 cm | 0.2 cm | 0.4% |
| fake, drift wobbling every 9 s, leveled | 3.6 cm | 11.6 cm | 10% (was 15%) |

The bottoms aren't in the fit, so their spread is an independent check. On the real set
they went from 12.2 cm to 2.8 cm without being touched.

**Limits:**

- It assumes lockout height repeats. The fit absorbs part of a real miss, so the
  "uneven lockout" flag (miss > 2 cm) fires at about 4 cm off. It's more forgiving than
  that sounds: in a fake set with one lockout 3 to 6 cm short, that rep's ROM still came
  out within 1 mm and the others within 1 cm, because a cubic can't bend fast enough to
  chase one rep.
- Drift that changes faster than every couple of reps can't be followed. A more
  flexible curve (up to τ⁵) cut the 9 s wobble case from 33% to 6% error, but with
  only lockouts to fit it overfits: a clean set with one short lockout went from 0.8%
  to 2% worst error. More constraints first (see "Other drift ideas" below), then more
  flexibility.
- Only the tops are used. Squat depth varies, so bottoms can't be trusted there. For
  bench (chest) and touch-and-go deadlifts (floor) the bottoms are fixed too and could
  go in the fit.

## 7. Turnarounds are outputs, never anchors

Inside a window, find where corrected velocity crosses zero. A − to + crossing is the
**bottom** of a squat or bench. A + to − crossing is the **top** of a rep without a
pause. Ignore crossings from tiny wiggles (*start*: the motion on each side has to
cover at least 5 cm).

**Why not force v = 0 at the bottom too?** It sounds like more correction should help.
It doesn't, and here's the logic:

1. You can only find the bottom by looking for where velocity crosses zero.
2. But that's the *drifting* velocity, which is already zero there by definition.
   Forcing it to zero adds no new information about the drift.
3. Worse, it's the wrong zero. By the bottom, drift has already built up, so the true
   velocity there is not zero. The anchor pretends that error doesn't exist, and dumps
   all the remaining correction onto the concentric.

Stillness is different. "Gyro quiet and |accel| ≈ g for 150 ms" is independent
evidence the bar is stopped. That's why it can be an anchor.

Run `analysis/demos/bottom_anchor.py` to see it. With a 0.05 m/s² bias on a fake
squat, top-to-top correction gets MCV exactly right (0.750 m/s). Adding a bottom
anchor at the drifting zero crossing gives 0.795 m/s, about 6% high. With 0.2 m/s²
it's 21% high.

Now flip `USE_TRUE_BOTTOM = True`. Anchoring at the *real* bottom time gives 0.749,
basically perfect. So the anchor idea isn't wrong. The problem is only where its
timing comes from. An anchor works when you know v = 0 at that moment from evidence
*other than the velocity you're trying to fix*.

That's also why paused reps are fine: a paused squat or bench has real stillness at
the bottom, and §5 picks it up as an anchor automatically.

## 8. Concentric phases

The concentric is any stretch where velocity > 0, bounded by turnarounds or window
ends. This one rule covers every lift:

- Squat / bench: still → down → **bottom** → up → still. Concentric = bottom to end.
- Deadlift: floor (still) → up → lockout. Concentric = start to the top (pause or
  turnaround).

**No velocity thresholds for boundaries.** It's tempting to say "the rep starts when
velocity passes 0.05 m/s". That adds a roughly fixed error in m/s (about 0.02 m/s at
that threshold), which is nothing on fast reps and huge on grinders: +19% MCV on a
0.10 m/s rep (`analysis/demos/threshold_bias.py`). Boundaries come from zero crossings
and stillness only. §7's wiggle filter works by distance, not speed, so it doesn't have
this problem.

**Grinder stalls.** On a max-effort rep the bar can nearly stop at the sticking point,
or even sink a little before it keeps going. Velocity crosses zero, which could split
one rep into two. §7's wiggle filter handles it: a sink shorter than 5 cm isn't a
turnaround, so the rep stays whole. Log where the stall happened as the sticking point
(analytics.md §4).

## 9. Metrics (per concentric)

- **ROM** = displacement over the concentric
- **MCV** = ROM / concentric duration
- **MPV** = mean velocity over just the propulsive part: from the start of the
  concentric until acceleration drops below -9.81 m/s² (see decision O2)
- **Peak velocity** = max velocity in the concentric

## 10. Quality flags

Flag a rep (show it in the app, don't hide it) when:

- its window is long (*start*: > 4 s), meaning no pause and more drift. Leveled windows
  (§6b) skip this one and get the next one instead
- its lockout misses the common lockout height by more than 2 cm (§6b, "uneven
  lockout"). Either the lockout really was different or the drift was too wobbly for
  the fit. Both mean the number is less trustworthy
- the drift correction was big (*start*: raw end velocity > 0.15 m/s)
- the sensor clipped (hit its range limit) inside the window
- there's an impact spike inside the window (bench touch, plates hitting floor). The
  straight-line correction smears sudden errors across the whole window.

## 11. Bar path (2D)

Bar path = the bar's trajectory in the plane you'd see filming from the side:
up/down and forward/back.

**Finding the plane.** Vertical comes from orientation (§2). For forward/back you
need the bar's long axis. Forward/back is the horizontal direction perpendicular to
the bar: `forward = up × bar_axis`.

No magnetometer needed, because the bar axis is tied to the device, not to compass
north. Two ways to know it:

- **From the mount.** If the mount forces one sensor axis to line up with the bar
  (sleeve end face, or a V-groove seat on the shaft), it's known by design.
- **From a roll gesture.** Before the set, roll the bar back and forth in the hooks for
  about 2 s. Rolling happens *around* the bar axis, so the gyro vector during the roll
  points along it. This also works with a sloppy temporary mount.

Rolling the bar mid-rep doesn't move the bar axis in the sensor frame (you're spinning
around it), so this stays valid even with sleeve spin.

Sign (which way is toward your toes, or toward your face on bench) depends on which
side of the bar the device is on. That's a setting: "device on my left / right".

**Integrating.** Same as vertical: integrate forward acceleration still-to-still and
remove the straight-line drift. Horizontal velocity is also zero when the bar is still,
so the same anchors work.

**Why bar path is the hardest metric.** An orientation error θ leaks gravity into
your measurement. Vertical only loses g·(1 - cos θ), which is tiny. Horizontal picks up
g·sin θ, which isn't:

| orientation error | vertical error | horizontal error |
|---|---|---|
| 0.5° | 0.0004 m/s² | 0.09 m/s² |
| 1°   | 0.0015 m/s² | 0.17 m/s² |
| 2°   | 0.006 m/s²  | 0.34 m/s² |

cos is flat near 0, sin is steep near 0. Rough rule from simulation: each 1° of
orientation error that comes and goes during a 2 s rep puts ~1.5 cm of error in the
path. It grows with the square of rep duration, so a 4 s grinder is ~6 cm per degree.
Horizontal bar movement in a good squat or bench is only a few cm, so treat small path
differences with suspicion until phase 3 proves the noise floor.

**What tricks Madgwick here.** The filter uses the accelerometer as its "which way is
down" reference, which is only true when nothing else is accelerating the sensor. It
normalizes the accel vector, so only direction matters. Pure vertical acceleration only
changes the vector's length, not its direction, so it doesn't fool the filter.
Horizontal acceleration tips the vector and does. Good news for lifts, where horizontal
acceleration is small.

**What bar path can't tell you.** Where the bar is relative to your body. The IMU only
knows how the bar moved from where it started. "Bar over midfoot" is not measurable.
Path *shape* is.

## 12. Bar tilt

Angle of the bar's long axis from horizontal: `asin(bar_axis · up)`. Positive = the
device's end is higher. Comes straight from orientation with no integration, so it's
accurate. Shows uneven lockouts and shifting to one side.

---

## Known gap: sets with no pause at lockout

Mostly closed by §6b for sets of 3+ reps. Still open: 1 to 2 rep sets with no pause
(rare, and short windows drift less), lockouts that really do vary, and drift that
wobbles faster than the reps.

(Forward-backward integration was on the old list. Dropped: integrate forward from the
first still, backward from the last, blend linearly, and the math collapses to exactly
the §6 straight-line fix.)

## Other drift ideas, ranked

**Where the drift comes from matters.** On your phone sets, the still periods differ by
at most 0.03 m/s² from each other, and a model where bias ramps between the still
values predicted the wrong end velocity (+0.13 m/s predicted, -0.02 m/s measured). So
the error mostly appears *during motion*, not from the sensor slowly drifting. On the
phone, one likely cause is the magnitude trick (D9): `|a|` adds sideways acceleration
squared, `a_h² / 2g`, which is always positive. 1 m/s² of side-to-side shake adds
about 0.05 m/s². The bar device tracks orientation with the gyro (§2), so it shouldn't
have this one.

Software, free:

1. **More constraints in the §6b fit.** Bottoms for bench and touch-and-go deadlifts.
   Rack height: the bar starts and ends on the same J-hooks, a deadlift starts and ends
   on the floor, so start height = end height. Each is one more row in the same least
   squares. More rows make a more flexible drift curve safe to use. *Tried on the first
   web app set:* counting the still at each end of the window as a lockout made it
   worse (ROM spread 1.6 → 1.9 cm), because holding the phone still you don't hold it
   at the exact height you turn around at. A racked bar might be different, so retest
   on bar data.
2. **Catch micro-pauses.** Most "no-pause" lockouts still stop for ~0.1 s. With the gyro
   confirming the bar isn't rotating, MIN_STILL can probably drop from 0.25 s to about
   0.1 s, which turns each lockout into a full anchor. Needs bar data (roadmap 2.2).
3. **Kalman smoother** (states: height, velocity, accel bias). Stills, lockouts and
   laser readings all become measurements, and the bias is allowed to wander instead of
   being a fixed cubic. More work, but it's the natural home for the laser (2.8).

Fix it at the source:

4. **Orientation tracking + calibration on the real device** (roadmap 1.3, 2.2). The
   LSM6DS3TR-C's factory accel offset is ±40 mg (±0.39 m/s²) and it drifts ±0.5 mg/°C,
   so the 6-position calibration and a fresh g estimate at every still both matter.

Extra sensors:

5. **Laser** (sensor-options.md §2, roadmap 1.6, 2.8). Measures height directly, so it
   covers exactly the cases §6b can't: uneven lockouts, wobbly drift, 1-rep sets.
6. **Barometer** (e.g. BMP581: 0.08 Pa RMS in high-res mode, about 0.7 cm of height).
   Cheap, but moving the sensor through air at 1 m/s adds ½ρv² ≈ 0.6 Pa, about 5 cm,
   and doors and HVAC move the room pressure too. Only useful for slow drift, if at
   all. Low priority.
