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

Where you'll find stillness: before the first rep, at lockout between reps,
deadlifts resting on the floor, after racking.

## 6. Integrate still-to-still

For each gap between two still periods: integrate vertical acceleration to velocity,
then remove the straight-line drift so velocity is zero at both ends. This is exactly
step 0.1. Then integrate to position.

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

- its window is long (*start*: > 4 s), meaning no pause and more drift
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

If you never stop at the top, there's no stillness between reps. One window covers the
whole set and drift has much longer to grow.

**Plan:** first measure how often this happens in your own training (step 2.1). Only
fix it if your data says you need to. Options then:

- relax the stillness thresholds just for lockout
- integrate forward from the starting stillness and backward from the ending one, then
  blend (each half only sees errors from its own side)
- use the fact that each rep's lockout height should be about the same
- add a laser distance sensor and fuse it with the IMU, which removes the need for
  stillness anchors entirely (sensor-options.md §2, roadmap 1.6 and 2.8)
