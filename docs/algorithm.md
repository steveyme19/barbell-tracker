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

*Phone, 2026-10-17 (D23): Tools > Sensor calibration does the 6-face accelerometer part
(`analysis/calib.py`, `webapp/calib.js`); live samples are corrected before anything else.*

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
- **Built in Python** (`analysis/orientation.py`, roadmap 0.3's algorithm side, D33):
  Madgwick's reference IMU update, the gyro bias from the stills, a strong accel pull
  (beta 0.2) only at a still whose accel agrees with the estimate (within 3 degrees, or a
  still of 1 s or more), the gyro alone while moving, the trapezoid between gyro samples;
  then world acceleration, the bar axis from the roll gesture, bar path and tilt (sections
  11, 12). Stills for it use the accel *vector* and the gyro, not |a| (|a| can't see a slide).
  On `make_fake_motion.py`'s exact-physics scenarios (a flat lift, the bar tilted and spun
  mid-lift, a table slide, a diagonal, a J-curve, tilted then spun): attitude within 0.17
  degrees while moving, vertical moves within 2.3 mm, forward within 5.3 mm, tilt within
  0.25 degrees (`python orientation.py --selftest`). **Not in the rep pipeline**, and not
  usable on hand-held data yet: on the 10-23 hand reps its stills and path are off (hand
  pauses wiggle, no roll gesture in the file, the accel 3% high until 1.3).

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

**Why not just the magnitude** (what the phone does, D9)? Magnitude counts sideways
acceleration as upward: `|a| ≈ g + a_v + a_h²/2g`, and that last term is always positive.
It grows with the *square* of sideways acceleration, so gentle sideways motion barely
matters and hard shake matters a lot. `analysis/demos/bench_jcurve.py` (fake bench sets,
real pipeline):

| case | magnitude | orientation, 1° off | orientation, 3° off |
|---|---|---|---|
| touch-and-go, 10 cm J-curve | 1.5 mm, 0.4% | 2.8 mm, 0.7% | 6.8 mm, 1.9% |
| paused on chest, 10 cm J-curve | 4.4 mm, 0.9% | 5.9 mm, 1.2% | 9.9 mm, 2.2% |
| 15 cm J-curve, last rep a 2.5 s grinder | 6.7 mm, 0.8% | 6.5 mm, 1.1% | 12.3 mm, 2.5% |
| 10 cm J + 1 m/s² sideways shake | 2.7 mm, 0.7% | 2.2 mm, 0.6% | 6.0 mm, 1.7% |
| 10 cm J + 2 m/s² sideways shake | 9.0 mm, 2.3% | 2.2 mm, 0.6% | 6.5 mm, 1.9% |

(worst ROM error, worst MCV error over the set; bench profile from §6b)

So the J-curve itself isn't the problem: a smooth 10 cm sweep only has ~0.5 m/s² of
sideways acceleration. Shake is. Orientation tracking only wins if it's accurate to
about 1 to 2°, and it's needed anyway for bar path (§11). Orientation error turns
sideways *displacement* into vertical error (about 10 cm × sin 3° ≈ 5 mm), so the
gyro calibration in roadmap 1.3 matters.

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
  1 s or more are accepted without the coasting check.
- **Slow, smooth grinders look still.** A fake 2.5 s bench grinder only accelerates
  about 0.3 m/s², so the whole concentric passed the stillness test, got accepted as a
  1 s+ still, and the rep vanished. Fix, applied to every candidate: integrate velocity
  across the candidate itself and remove a straight line (a steady sensor bias makes a
  straight line). What's left is how much velocity rose and fell inside it. Real stills
  on every recording: 0.2 to 3 cm/s. Coasts: 6 to 7 cm/s. The grinder: 21 cm/s. Cutoff
  was 4.5 cm/s, now **12 cm/s** (D15): a squat lockout held 1 to 2 s measured 7.5 and
  8.9 cm/s, almost all of it the next descent leaking into the last ~0.1 s of the
  window, and got thrown out. Coasts don't need the hump test: every coast on file is
  also rejected by the coasting check above. 12 sits between real stills (up to 9) and
  the grinder (21). A grinder slower and smoother than the sim's could now slip under
  it; watch for that on real heavy sets.
- **Still open:** the same fake grinder eases into lockout so gently that the end still
  starts ~0.07 s early, while the bar is still moving at 2 cm/s. Forcing v = 0 there costs
  that rep 3 cm of ROM with lockouts-only leveling, 0.7 cm with the bench profile
  (§6b). Real lockouts usually stop sharper, and the bar should sit much stiller than a
  hand-held phone, so tighter thresholds on bar data (roadmap 2.2) are the likely fix.
  Check it with real slow reps in 3.4.
- **Smooth lowering on a machine looks still (D20).** Phone lying on a cable stack, lat
  raises: the stack rides on guide rods, so the lowering is so smooth that most of it
  passed the stillness test and got zeroed. Height climbed like a staircase (to 115 cm),
  3 "reps" of 77, 34 and 39 cm. The tell: at rest on the stack the sensor wiggles 0.016
  m/s² (median 0.2 s rolling std), the fake stills 0.08 to 0.12, about 6x more. A phone
  in your hand wiggles 0.08 to 0.13 even when you hold still, so a fixed limit can't
  serve both. Fix: **noise-matched limits.** Measure the wiggle inside the longest still,
  then a still has to stay under 4x that wiggle and its average within 5x of g, clamped
  between 0.06 / 0.08 and the old 0.15 / 0.3. Hand-held recordings hit the old limits,
  so they're unchanged. The lat raise set now gives 8 reps of 16 to 22 cm (plus a 6 cm
  one at the end). This is also what the real bar device will need: it'll sit as still
  as the stack.

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
the fit can't be forced through every point. Code: `level_heights()` in `reps.py`,
`levelHeights()` in `reps.js`.

**Per lift (2026-10-16).** Lockouts repeat in every lift. Some lifts give more:

| lift | bottoms repeat? | still before/after the set |
|---|---|---|
| bench | yes, the chest | at lockout |
| deadlift | yes, the floor | on the floor (a bottom) |
| squat | no, depth varies (and that's worth measuring) | standing, at lockout |
| phone test | no | no: hand-held, you don't hold still at the turnaround height |
| machine (D19) | yes: the stack rests on the stack | yes: at rest on the stack |
| free (D21): a free weight with another name (row, OHP, curl) | no | at lockout or the bottom |

The app has a lift picker, and Python reads the lift from the file name
(`bar-bench-...csv`) or `--lift=`. Squat, bench and deadlift come from the name; for
anything else the sensor decides between machine and free (below, D21). Bottoms are a
second group with their own height, so the fit gets twice the points and can safely
bend more (up to τ⁵). The stills count
as a lockout or a bottom depending on whether the bar left them going down or up. On
fake no-pause sets with wobbly drift (`make_fake_reps.py lifts` style, 10 sets each,
worst-rep ROM error):

| lift | drift wobble | lockouts only | lift profile | better in |
|---|---|---|---|---|
| bench | 0.02 m/s² | 2.8 cm | **1.2 cm** | 10/10 |
| bench | 0.05 m/s² | 7.0 cm | **2.8 cm** | 10/10 |
| deadlift | 0.02 m/s² | 3.6 cm | **2.0 cm** | 10/10 |
| deadlift | 0.05 m/s² | 8.9 cm | **4.9 cm** | 10/10 |
| squat | 0.02 m/s² | 2.9 cm | 2.5 cm | 7/10 |
| squat | 0.05 m/s² | 7.2 cm | 6.3 cm | 7/10 |

**One rep between two stills at lockout** (paused-at-the-top sets): that's only two
points, start and end, but they're at the same height, which is enough for one extra
bend (an exact fit). Fake paused squats with wobbly drift, 10 sets: worst-rep error
0.9 → 0.4 cm and 2.2 → 0.6 cm, better in 10/10. Your hand-held squat set where every
rep had the same ROM (`bar-2026-10-16-1920.csv`): spread 3.3 → 1.7 cm with the squat
profile, and the one rep that came out 80 cm is now 74. Phone test profile: unchanged.

Squat only gets the stills, a small gain on no-pause sets. Leveling squat bottoms would shrink real depth
differences, which the depth check (analytics.md §3) needs. The phone profile stays at
lockouts only: on your first web app set, counting the stills made it worse (ROM spread
1.6 → 1.9 cm). Recheck all of this on bar data (roadmap 2.6).

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
- Bottoms are only used for bench and deadlift (see "Per lift" above).

### Machine or free weight, from the sensor (D21)

Real use is a sensor fixed to a bar or sitting on a weight stack, never a hand. Whether
it's on a machine doesn't need typing in; the gyro shows it. A weight stack (also a Smith
machine or a sled on rails) runs on guide rods: it goes straight up and down and doesn't
turn. A bar tilts, rolls and drifts forward and back. `detect_mount()` looks at the
motion after the first still:

- **rot**: median turning speed while moving, after taking off the gyro's offset (its
  average during the stills). A median, so a bump or a setup move doesn't count.
- **side**: rms sideways acceleration / rms vertical acceleration while moving, with the
  3 axes smoothed over 0.2 s first (a stack rattles on its rods but can't travel
  sideways). "Sideways" is off the gravity direction measured during the stills, so
  tilting counts too.

Machine when rot < 3 deg/s and side < 0.35, else free. Needs 1 s of motion and gyro
data; otherwise the name decides.

| recording | rot (deg/s) | side | result |
|---|---|---|---|
| cable lat raise, phone on the stack (1334) | 0.7 | 0.20 | machine (from 45% of the set on) |
| hand-held squats and phone tests (9 files) | 10 to 25 | 0.36 to 1.9 | free |

**How still it is at rest doesn't tell them apart.** The phone tests 2123 and 2206 sat on
a table between reps as quietly as the stack (rest wiggle 0.010 to 0.013 vs 0.016 m/s²),
and a racked bar or a bar on the floor will too. The rest wiggle sets the stillness
limits (D20, §5), the motion decides the profile.

`choose_lift()`: squat, bench, deadlift and phone by name always win, because they say
more than the sensor can (a Smith squat rides on rods, but you still pick the depth).
Anything else (an unknown exercise name, which D19 mapped to machine) goes by the sensor:
machine, or `free` = lockouts and the end stills only, the safe choice for a barbell row,
OHP or curl. The result says which profile it used and why (`lift`, `mount`).

**Not tested on a mounted bar yet.** The free side of the line comes from hand-held
data. A bar should turn and drift more than a stack, but the sleeve-end mount (O3) may
be calm on a smooth bench. If a real bar set ever reads "machine", the name still wins
for squat/bench/deadlift; raise `MACHINE_ROT` only with that recording in hand.

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
this problem. The one exception is `V_ZERO` = 1e-9 m/s: "zero" means within roundoff.
A drift-corrected window ends at v = 0 only up to ±1e-17, and Python and JS round it
with opposite signs, which moved a rep end by a sample (D14). 1e-9 m/s biases nothing.

**Pauses that aren't stills (D32).** A hold at lockout or on the chest that's too short
(under 0.25 s) or too shaky to be a still isn't an anchor, so v there is left-over drift:
true 0, read a hair above it. "First v = 0" then ran through the whole pause and the rep
took it in: on the fake session's 0.25 s holds, 7 of 20 reps read 5 to 24% low, and up
to half too low on known curves. Now the search starts where v <= 3% of the rep's own
peak (`V_STOP_FRAC`) and **walks on while v keeps falling**: to v = 0 when it gets there,
which is the same boundary as before (no threshold bias, unlike a plain 3% threshold at
+1 to +6%), or to where the pause's drift floor begins. If the drift is over 3% of the
peak, the walk can't see the plateau and the boundary is today's. Bar and stack lifts at
`WALK_MIN_FS` = 150 Hz or more: hand-held phone tests and Chrome's jittery 60 Hz stay as
they were. Numbers: `analysis/demos/boundary_pauses.py`; the fake session's true MCVs
are a known answer in parity.mjs (worst -2.3%).

**Grinder stalls.** On a max-effort rep the bar can nearly stop at the sticking point,
or even sink a little before it keeps going. Velocity crosses zero, which could split
one rep into two. §7's wiggle filter handles it: a sink shorter than 5 cm isn't a
turnaround, so the rep stays whole. Log where the stall happened as the sticking point
(analytics.md §4).

**Setup moves before rep 1 (D20).** On the cable lat raise, taking up the cable lifted
the stack 6 cm, it settled back 1 cm, then rep 1 went up. That 1 cm dip isn't a
turnaround (under 5 cm), and the stop sat more than 5 cm above where the move began, so
rep 1 started on the resting stack: 24.5 cm instead of 19.4. For lifts whose bottom
repeats (bench, deadlift, machine), a rep may now start at a stop within 5 cm of the
*usual* bottom (median of all the turnaround bottoms), not just its own. Deadlift and
bench are unaffected (their usual bottom is the floor / chest anyway), and grinder
stalls well above the usual bottom still don't split a rep.

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

## 13. Sets and rest (session mode)

One recording can be a whole workout: clip the sensor on, lift, walk away. A layer on
top of sections 5 to 10 finds the sets (`analysis/sets.py`, ported to
`webapp/sets.js` as a live, sample-by-sample tracker; parity-tested). D17.

1. **Blocks.** Time is cut into 0.25 s blocks. A block is active when the std of |a|
   inside it is over 0.15 m/s^2 (the stillness threshold, coarser). Coarse on purpose:
   it only has to tell "lifting" from "bar in the rack".
2. **Segments.** Active blocks with less than 15 s of quiet between them are one
   segment. A segment closes after 15 s of quiet, so a breather at lockout inside a set
   doesn't split it, and the shortest real rest between sets (~1 min) always does.
3. **Each segment, plus 2 s of still before and after, goes through the per-set
   pipeline** (`analyze()`). No reps = not a set: loading plates, bumping the bar.
   Each set gets its own machine-or-free check (D21), so a session can mix them.
4. **Rack moves.** Unracking lifts the bar ~5 to 8 cm off the hooks, enough to pass
   `MIN_ROM` as a tiny "rep". Reps at either end of a set shorter than half the set's
   median ROM go, and (except phone tests) anything under 12 cm: that also handles an
   unrack + rerack with no reps, bumping a machine's stack, and the first seconds of a
   set before a real rep.
   **A guess until real bar sessions exist** (roadmap 2.x): walkouts and reracks may
   look different on a real bar.
   **The sensor handled** (D31): unclipping the pod, or picking up the phone, before the
   set's 15 s of quiet is up adds one huge end "rep" (1.98 m on a real pod set). End reps
   over a per-lift cap (squat and deadlift 1.0 m, bench 0.8 m) or over 2x the median of
   the set's other reps go first; the small-rep floor is then taken from what's left. And a
   closed set keeps only anchored reps: it always has 2 s of still after its last active
   block, so a rep with no end anchor only comes from motion running up to the cut (the
   sensor carried, End tapped mid-move). The live view keeps the rep in progress.
   Moving the pod to another bar can still make an anchored, normal-looking phantom set:
   no threshold tells it from a real single. **Not a set** on its card takes it out (the
   session, the live tracker's rest for the next set, its stored samples and CSV).
5. **Rest** = first rep start of a set minus the last rep end of the set before. Live,
   the app shows the rest timer 3 s after the last rep (provisional: more reps within
   15 s and it's still the same set).

**Saved set files** (`bar-<lift>-<stamp>-setNN.csv`) include the unrack. Analyze them
with `python sets.py <file>` (rack filter applied, same reps as the app), not
`reps.py` alone, which counts the unrack as a tiny first rep.

The live app only ever analyzes the set in progress, and during rest it keeps a few
seconds of samples, so an hour-long session costs the same per pass as one set.

Known-answer test: `make_fake_session.py` (4 squat sets with unrack, walkout, rerack,
plates, a 10 s breather, and an unrack + rerack with no reps): 4 sets, rests exact.
`make_fake_session.py handled`: the same, then the phone picked up 4 s after the last
rerack and held while End is tapped: still 4 x 5 reps (it gave the last set a 6th rep at
0.17 m/s, velocity loss 72%, before D31). Real
data: two real 402 Hz sets joined with real still noise give the same reps as each file
alone (ROM within 0.4 cm, MCV within 0.001 m/s).

**Racked starts and the `ends` rule (6b).** Squat and bench assume the still before the
set is at lockout height. With a rack it's the hooks, 5 to 8 cm lower. The rule only
uses the start still as a lockout when the bar leaves it going *down*; an unrack goes up
first, so the hook still is left alone. A still after the walkout (settle before the
first rep) then starts the set's own window. Unracking and squatting with no settle at
all hasn't been tested on real data yet.

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

1. ~~More constraints in the §6b fit~~ **Done** (2026-10-16): bottoms for bench and
   deadlift, and the stills before and after the set, picked per lift. See "Per lift"
   in §6b.
2. **Catch micro-pauses.** Most "no-pause" lockouts still stop for ~0.1 s. With the gyro
   confirming the bar isn't rotating, MIN_STILL can probably drop from 0.25 s to about
   0.1 s, which turns each lockout into a full anchor. Needs bar data (roadmap 2.2).
3. **Kalman smoother** (states: height, velocity, accel bias). Stills, lockouts and
   laser readings all become measurements, and the bias is allowed to wander instead of
   being a fixed cubic. More work, but it's the natural home for the laser (2.8).
   *Prototyped 2026-10-17* in `analysis/experiments/` (forward filter + backward RTS
   pass, plus adaptive stillness: shakier, shorter pauses count as weak zero-velocity
   hints once the last still is 2+ s back). On fakes with wobbly drift the worst-rep ROM
   error drops from 2.5 to 16.5 cm to 0.1 to 0.7 cm. On the real phone recordings it's a
   tie. Port it only if it also wins on a recording with a known ROM (the 55.9 cm paper
   test); see `analysis/experiments/README.md`.

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
