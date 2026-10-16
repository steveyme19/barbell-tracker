# Step 1: Measure a fake rep with your phone

**Goal for this sitting:** lift your phone from the floor onto a table and get
Python to tell you the height within about 2 cm, from accelerometer data alone.

**Time:** ~1.5 hours. Part A (fake data) is 30 min and needs no phone.

This step looks like a toy, but it is the core of the whole project. One rep is
"still, move, still". So is this. Every idea here carries straight into phase 2.

---

## The physics

### 1. What an accelerometer actually measures

An accelerometer is a tiny mass on a tiny spring. It measures how hard the spring
has to push to make the mass move along with the case.

Gravity pulls on the mass and the case equally, so on its own gravity doesn't
stretch the spring. The sensor can't feel gravity. What it feels is every *other*
force: the table pushing up, your hand pushing up.

So:

- Sitting on a table, it reads **+9.81 m/s² pointing up**. The table is pushing it up with 1 g.
- In free fall, it reads **0**. Nothing is pushing on it.

This reading is called **specific force**. For the vertical axis with up as positive:

```
reading = a + g        (g = 9.81)
a       = reading - g
```

That subtraction is "gravity removal" in its simplest form. The catch: you have to
know which phone axis points up. Phone flat, screen up: it's z. On a barbell the
device rotates, so you'll need orientation tracking. That's the entire reason
Madgwick is in phase 2.

### 2. Why integrating twice is dangerous

```
velocity = ∫ acceleration dt
position = ∫ velocity dt
```

Say your acceleration has a small constant error `b` (bias). Then:

```
velocity error = b·t
position error = ½·b·t²
```

With b = 0.05 m/s² (half a percent of g, totally normal for a phone sensor):

| integrate for | velocity error | position error |
|---|---|---|
| 1 s  | 0.05 m/s | 2.5 cm |
| 3 s  | 0.15 m/s | 22 cm |
| 10 s | 0.5 m/s  | 2.5 m |
| 60 s | 3 m/s    | 90 m |

Position error grows with t². You can never integrate a whole set and expect
sane numbers. You integrate short windows and reset between them.

Random noise is much less of a problem, because it partly cancels when you add it
up. Noise of 0.03 m/s² at 200 Hz, integrated for 1 s, gives a velocity error of
roughly `σ · dt · √N = 0.03 × 0.005 × √200 ≈ 0.002 m/s`. Tiny. **Bias is the enemy,
not noise.**

### 3. Zero-velocity correction (the trick that makes this work)

The phone is still before the lift and still after. True velocity at both ends is
zero. After integrating, your velocity at the end comes out as some `v_end ≠ 0`.
That number is pure error, and you know it exactly.

If the error came from a constant bias `b`, then `v_end = b·T`, so:

```
b = v_end / T
v_corrected(t) = v_raw(t) - b·t
```

Subtract that straight line and velocity now starts and ends at zero. Then
integrate again for position.

Neat consequence: if the error really is constant, this removes it **perfectly**.
You don't even need an accurate value for g. Exercise 2 proves it.

What it **can't** fix:

- **Errors that change during the motion**, like tilting the phone partway through.
  A straight line can only cancel a constant. (Exercise 3.)
- **Sudden errors**, like a hard landing that the sensor doesn't sample cleanly. The
  correction assumes the error built up evenly over the whole window, so an error
  that really happened in one instant at the end gets smeared backwards across the
  entire lift. In my testing a fake landing glitch worth 0.05 m/s cost about 3.5 cm.
  Remember this for the bottom of a bench (bar hits chest) and deadlifts (plates hit floor).
- **Scale error.** If the sensor reads 2% high, every acceleration is 2% too big,
  velocity still ends at zero, and the correction never notices. Height comes out
  2% high. Fix that with calibration later, not here.
- **Wrong endpoints.** If your "still" moment isn't actually still, you're forcing
  the answer to be wrong.

### 4. Why this matters for mean concentric velocity

Average velocity over an interval is just distance over time:

```
MCV = ROM / concentric duration
```

So MCV is only as good as two things: the ROM (this step) and *when* you decide the
concentric starts and stops. If a concentric takes 0.5 s and your boundary is off by
20 ms, that's a 4% MCV error even with a perfect ROM. Remember this in phase 2.

---

## Setup (10 min)

From the repo root:

```bash
cd analysis
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Part A: test on fake data (30 min)

`make_fake_lift.py` builds a fake phone recording where you know the right answer
(0.74 m). It includes noise, a sensor bias, and uneven sample timing like a real
phone.

```bash
python make_fake_lift.py
python phone_lift.py data/fake_lift_tilt0.csv 0.74
```

You should get a height within a millimeter or two of 0.740 m. Open the plot and
make sure you understand all three panels before moving on.

Now read through `phone_lift.py` top to bottom. Every numbered comment matches a
section above.

> **Note (2026-10-16):** `phone_lift.py` now defaults to `AXIS = "magnitude"`, which
> is tilt-proof (decision D9). To reproduce the tilt exercises below, set
> `AXIS = "z"` at the top of the script first.

### Exercise 1: tilt (predict first, then run)

The fake generator can tilt the phone mid-lift. **Write down your prediction
before running:** with a 10° tilt and a 30° tilt, will the measured height be
too high or too low? Roughly how much, raw vs. corrected?

Hint: the z axis reads `(g + a)·cos θ`.

```bash
python make_fake_lift.py 10
python phone_lift.py data/fake_lift_tilt10.csv 0.74
python make_fake_lift.py 30
python phone_lift.py data/fake_lift_tilt30.csv 0.74
```

<details><summary>What I got</summary>

| tilt | raw height | corrected height | corrected error |
|---|---|---|---|
| 0°  | 0.739 m | 0.739 m | -0.1 cm |
| 10° | 0.669 m | 0.735 m | -0.5 cm |
| 30° | 0.116 m | 0.703 m | -3.7 cm |

Tilting makes z read low, so the phone "thinks" it's decelerating and height comes
out short. Small tilts barely matter because cos is flat near 0: cos 5° = 0.996,
so you lose only 0.04 m/s². At 30°, cos = 0.866 and you lose about 1.3 m/s². That's
huge. Raw integration nearly loses the whole lift.
</details>

### Exercise 2: use the wrong gravity

In `phone_lift.py`, temporarily replace

```python
g_est = a_raw[still].mean()
```

with `g_est = 9.81` and run it on `fake_lift_tilt0.csv`. **Predict first:** what
happens to the raw height? The corrected height? (The fake sensor has a 0.05 m/s²
bias, so it really reads about 9.86 at rest.)

<details><summary>What I got</summary>

Raw jumps to 0.793 m (+5.3 cm). Corrected stays at 0.739 m, unchanged. And the
"implied accel error" it prints is 0.049 m/s², which is the fake bias it just
discovered on its own. A constant error gets removed exactly.

Put the line back when you're done.
</details>

### Exercise 3: why isn't 30° fixed?

Look at the 30° plot. The correction helped a ton but left 3.7 cm. Explain why in
one or two sentences. (Answer this one yourself. It's the key idea for phase 2.)

---

## Part B: real phone lifts (45 min)

### Measure the truth

Tape-measure from the floor to the tabletop. The phone sits flat on both, so its
thickness cancels. Write it down to the millimeter.

### Record with phyphox

1. Use **"Acceleration with g"**, not "Acceleration (without g)". The "without g"
   version is Android's own sensor fusion. It's a black box with its own filtering,
   and your barbell device won't have it. Learn on the raw signal.
2. The script needs the first second to be perfectly still, and tapping the
   screen ruins that. Use phyphox's **timed run** (in the ⋮ menu: start delay ~3 s,
   duration ~12 s), or turn on **remote access** and start it from your laptop browser.
3. Phone flat on the floor, screen up. Start the timed run. Hands off.
4. Once it's recording, count to 3. Pick it up like a tray (keep it flat), lift it
   smoothly onto the table, set it down flat and **gently**, let go.
5. Hands off until the recording ends.

Do 10 trials:

- 5 at a normal, smooth speed
- 2 fast
- 2 slow
- 1 where you deliberately tilt it about 30° partway up

### Export

⋮ menu → **Export Data** → **CSV (Comma, decimal point)**. You get a zip. Inside is
`Raw Data.csv`. Rename it to `lift_01.csv`, `lift_02.csv`, etc. and drop it into
`analysis/data/`. (Google Drive or emailing yourself is the fastest way to get
files from your phone to your laptop.)

### Run

```bash
python phone_lift.py data/lift_01.csv 0.74     # use YOUR measured height
```

Fill in the results table below as you go.

---

## What good looks like

- Normal lifts: within ~2 cm (under ~3%).
- Raw and corrected close together = you held it flat. Far apart = something
  changed mid-lift (tilt, usually).
- If every trial is off by about the same *percentage* in the same direction,
  suspect scale error (or your tape measurement).

## Troubleshooting

| symptom | likely cause |
|---|---|
| "Couldn't find column" | Check the printed column names. Change `AXIS` if needed. |
| Height is negative | Phone was screen-down, so z pointed down. |
| WARNING about still period | You touched it in the first second. Use the timed run. |
| Motion window way too long | Bumped the table or phone after setting it down. |
| Spike at the end of the accel plot | The phone landing on the table. Small is normal. |
| Big landing spike and height off by several cm | Hard landing. Set it down more gently. |

## Results

| file | true (m) | raw (m) | corrected (m) | error (cm) | peak v (m/s) | notes |
|---|---|---|---|---|---|---|
| lift_01 | | | | | | |
| lift_02 | | | | | | |
| lift_03 | | | | | | |
| lift_04 | | | | | | |
| lift_05 | | | | | | |
| lift_06 | | | | | | fast |
| lift_07 | | | | | | fast |
| lift_08 | | | | | | slow |
| lift_09 | | | | | | slow |
| lift_10 | | | | | | tilted |

## Bring back to the next session

1. The results table.
2. The plot from one normal lift and the tilted one.
3. Your answers:
   - Why didn't the wrong gravity value change the corrected height?
   - Why can't the correction fully fix a 30° tilt?
   - If all your errors are about +3%, what's the likely cause, and why can't
     zero-velocity correction catch it?
