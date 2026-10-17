# Experiments

Ideas being tested before they go into `reps.py` / `reps.js`. Nothing here is used by the
app. When an experiment wins on data with a known answer, it gets ported (Python first,
then JS, then the parity test).

## RTS smoother + adaptive stillness (2026-10-17)

`smoother.py`: one Kalman model for the whole set instead of "straight line between
stills" + "polynomial through the lockouts". State: height, velocity, accelerometer bias
(allowed to wander slowly), lockout height, bottom height. It runs forward through the
set, then backward (Rauch-Tung-Striebel smoother), so a correction found late in the set
fixes the samples before it too. Measurements:

- still periods: velocity = 0
- lockouts: height = lockout height (bottoms too for bench, deadlift, machine)
- barbell lifts: stills at lockout or floor height count as lockouts or bottoms
- **adaptive stillness**: once the last still is 2+ s back, shorter and shakier pauses
  (0.15 s, wiggle up to 0.2 m/s² instead of 0.25 s / 0.15) also count, as weaker
  velocity = 0 hints, if they pass the coasting and hump checks.

Idea sources: zero-velocity updates with a smoother from foot-mounted navigation
(Wahlström & Skog, arXiv 2008.09208), geometry as soft constraints in an RTS smoother
(Fang et al. 2020, ski jumping, doi 10.3390/s20071995).

**Results** (`eval_smoother.py`, fakes with known answers, worst rep per set, mean of 6):

| fake set | ROM error now | ROM error RTS | MCV error now | MCV error RTS |
|---|---|---|---|---|
| phone no-pause, wobbly drift 0.03 | 5.07 cm | 0.23 cm | 9.3% | 0.5% |
| phone no-pause, wobbly drift 0.1 | 16.51 | 0.71 | 30.8% | 1.7% |
| bench no-pause, wobble 0.05 | 2.81 | 0.22 | 5.2% | 0.6% |
| squat no-pause, wobble 0.05 | 6.34 | 0.65 | 8.6% | 1.2% |
| deadlift no-pause, wobble 0.05 | 4.98 | 0.24 | 9.0% | 0.6% |
| squat paused at top, wobble 0.05 | 0.55 | 0.25 | 1.0% | 0.5% |
| bench J-curve + 2 m/s² sideways shake | 0.99 | 1.19 | 2.5% | 2.6% |
| bench, last rep a 2.5 s grinder | 0.70 | 0.32 | 0.7% | 1.7% |

On your real phone recordings it's a tie: ROM spread within about ±0.3 cm of the current
method on every file, sometimes better, sometimes worse. The adaptive pauses are mixed
too (box squats 2134: 2.2 → 1.3 cm spread; 1920: 2.0 → 2.2). Spread can't settle it,
because hand-held squats really do vary by a cm or two.

Settings came from `tune_smoother.py` (a grid over the three noise levels). The fakes
like a smaller `h_sd` (they have perfectly equal lockouts), so it's left at 1 cm.

**Decision gate:** port it only if it also wins on a recording with a known ROM: the
55.9 cm paper test, paused and no-pause, recorded in the Android app. Otherwise the
fakes may just be built the way the smoother assumes.
