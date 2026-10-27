# Analytics spec (v0): consistency, RPE, history

Everything here works *across* reps, sets, and sessions. It's built on the per-rep
outputs from [algorithm.md](algorithm.md). Like everything else, it gets prototyped in
Python first and ported to the app after.

## What "standard" means here

It could mean two things:

- **Consistent:** this rep matches your usual rep.
- **Correct:** this rep matches an ideal technique.

The sensor can measure the first well. It can barely measure the second, because it
doesn't know where your body is (algorithm.md §11). So the plan is: consistency against
your own reps, plus the one "correct" check that *is* measurable, depth (§3 below).

---

## 1. Per-rep features

Everything below is computed from these.

| feature | from (algorithm.md) | notes |
|---|---|---|
| ROM | §9 | for squat and bench, also your depth |
| concentric / eccentric duration | §8 | tempo |
| MCV, MPV, peak velocity | §9 | speed |
| velocity profile | §6 | velocity at every 5% of concentric height, divided by MCV. Shape, not speed. |
| sticking point | velocity profile | height (% of ROM) where velocity dips after its first peak, if it does |
| path | §11 | forward offset at every 5% of height, separately for down and up |
| horizontal excursion | §11 | max forward minus max back |
| max bar tilt | §12 | |
| quality flags | §10 | |

**Why resample by % of height instead of time:** two reps that trace the exact same
path at different speeds look identical when sampled by height and different when
sampled by time. You want to compare shape, so sample by height.

## 2. Consistency

Compare each rep's *shape* features to a reference built from your own reps. Report the
difference in real units (cm, %), not a made-up score.

| level | what gets compared | reference |
|---|---|---|
| rep to rep | each rep in a set | median rep of that set |
| set to set | each set's median rep | median rep of that lift in this session |
| session to session | this session's median rep | your baseline: median of the last ~4 sessions of that lift at similar load |

Deviations:

- **path:** RMS distance between the two paths (resampled by height), in cm
- **depth:** ROM difference, in cm
- **tempo:** duration difference, in %
- **velocity profile:** RMS difference of the normalized profiles

**Keep fatigue out of it.** Speed is *supposed* to drop across a hard set. That's not
inconsistency. So the consistency numbers only use shape features, and speed is
reported separately as velocity loss.

**Keep load in mind.** Path legitimately changes with load. Session-to-session
comparisons only use sets within ±10% of the same load (*start*).

**Speed across sessions is its own metric.** Velocity at a fixed load compared to your
baseline is a readiness and strength trend. Report it separately from technique.

**Noise floor first.** A difference only counts if it's bigger than what the sensor
reports for reps that are actually identical. Measure that on a **Smith machine**: the
bar rides on rails, so every rep follows the same straight line. Any path spread the
device reports there is pure sensor error. Only show deviations bigger than 2× that
floor (*start*).

**Prove it catches real faults.** Do sets with deliberate faults: let the bar drift
forward, cut depth by 5 cm, hitch mid-rep. If the numbers can't see a fault you did on
purpose, they can't see one you didn't.

**No 0-100 score yet.** A score hides the units and invents thresholds. Once you know
the noise floor and which deviations actually matter in your data, map cm to a score.
Start with cm.

**Built in Python** (roadmap 5.2, `analysis/consistency.py`): rep to rep, set to set and
session to session (from app exports, D30), on depth (ROM, cm), the velocity profile (RMS
of the normalized profiles) and tempo. **Tempo is the eccentric's duration**: the
concentric's is speed, which fatigue is supposed to cut, so it stays out (the self-test
holds a 35% slower concentric of the same shape to 0% tempo and 0.003 profile). Tempo is
measured per gap, the lowering between two reps, not per rep: whether a rep's lowering is
the one before it (a squat) or after it (a deadlift) the motion around a set can't always
say (a Romanian deadlift, a pin squat, a row's hinge, the bar set down after the set), and
the lowering between two reps is the same event either way; before the first rep and after
the last aren't used. In a gap the lowering is the biggest drop, a stop partway down of any
length with no rise doesn't cut it, and a box sit's wobble or a settle at lockout can't
stand in for it (D35). Sets compare by their typed exercise name, not the tracking profile
(every unnamed exercise is "machine" or "free"). Stored sets go through the closed-set
filter first (D31). With no Smith-machine floor yet every deviation is shown, none judged;
`--floor-depth/tempo/profile` flags those over 2x it. `--selftest` plants the deliberate
faults above (5 cm short, a mid-rep hitch, a slower descent): each stands out on its own
measure by 100x or more, and the slower descent reads its true 35% in the right gap on 11
lifts and endings; each of lowering()'s thresholds, moved either way, fails an exact-number
check, and so does a wrong set-to-set or session tempo. Path deviations wait for orientation on real bar data (0.3, D33).

## 3. Depth check

The one "correct" check the sensor can do. Film one squat at exactly your target depth
and note its ROM. That's your depth line for that stance and bar position. Flag reps
that come up short of it by more than the noise floor.

It works because your lockout height barely changes, so ROM tracks depth. Recalibrate
if you change stance or bar position.

## 4. Sticking point

On a hard concentric, velocity often rises, dips, and rises again. The dip is the
sticking region. Report where it happens (% of ROM) and track it over weeks. For a
mid-range bench sticking point, this shows exactly where it is and whether it moves as
your training changes. It only shows up on heavy reps. Light reps don't dip.

Built (D34, roadmap 5.3): `sticking_point` in reps.py / `stickingPoint` in reps.js, per
rep. The concentric's velocity averaged over 50 ms; the deepest dip with a rise before
and after it, at least 10% of the rep's peak below the lower of those two peaks, between
10% and 90% of the ROM. Reported as where (% of the ROM), the speed there and the dip's
depth; the set card shows each rep's and the set's median ("Sticks at ~41% of the way up,
3 of 5 reps"). Known answer: `make_fake_reps.py sticking` (2 light reps, 3 with a dip,
found within 0.2% of the ROM, and 3 on the rule's edges: an 8% dip, a 12% one, a deep one
at 93% of the ROM, and 3 more: 6.5%, 13%, 87%), which holds STICK_PROM to about 0.08-0.12
and the range's ends to 0.065-0.13 and 0.87-0.93. No smooth fake reads one; three
real sets do, none a barbell grind (two hand-held phone files, the lat raise's odd last
rep: D34). The 10% is provisional until real heavy reps (roadmap 2.x). Not yet: the trend
over weeks (5.5).

## 5. RPE estimation

This uses the reps-in-reserve RPE scale: RPE 10 = 0 reps left, RPE 9 = 1 left, and so
on. So estimating RPE means estimating RIR.

### What the research says

- In powerlifters, mean velocity and RPE are strongly related. Velocity at 1RM differs
  a lot by lift: about 0.23 m/s squat, 0.10 bench, 0.14 deadlift [1].
- One equation for everyone doesn't work. Individual relationships do [2, 3]. In the back
  squat, individual RIR-velocity relationships from one session predicted RIR in a later
  session with mean error under 2 reps [3].
- Accuracy is best close to failure. In one bench study, RIR 2 and 0 were predicted
  within about 1 rep [4].
- It's not a sure thing. One study found velocity-based RIR was off for most reps in
  recreationally active lifters [5]. A 2026 study on the hex bar deadlift found it not
  accurate enough to use [6].

### What that means for this project

- **Individual and per lift.** Calibrate on your own data.
- **Squat and bench first.** For deadlift, show velocity and mark RPE as low confidence.
- **Expect ~±1 rep near failure, fuzzier further away.** RPE 6 vs 7 will be a guess.
  That's OK: autoregulation matters most near failure.
- **The reps that matter most are the hardest to measure.** Grinders are slow, so the
  integration window is long and drift has more time to grow. And bench at 1RM is only
  ~0.10 m/s, so a 0.03 m/s error is 30%. Phase 3 has to check accuracy on slow reps
  specifically.

### Model v0 (in the app now, D18)

Until there are failure sets to fit on: RIR = (last rep MCV - v1RM) / DV_PER_REP per lift,
v1RM from [1], DV_PER_REP a rough guess. Uncalibrated, shown as a range, labeled "RPE
est." Every set also has a "Your RPE" box, and `session.csv` keeps both side by side,
which is the data Model v1 needs.

### Model v1

1. **Calibration sets.** Now and then, take a squat or bench set to failure at a few
   loads (say 65 to 85%). Safeties set, every time.
2. **One set to failure labels every rep in it.** A 9-rep set to failure gives you reps
   with RIR 8, 7, ... 0. Very efficient data.
3. **Fit per lift:** RIR vs rep MCV, straight line first. Check whether one line works
   across loads for you (it did on average in [3]).
4. **Predict:** in a normal set, last-rep MCV → RIR → RPE = 10 - RIR. Show a range
   ("RPE 8 to 9"), not a fake-precise number.
5. **Validate honestly.** Fit on some sessions, test on *other* sessions. Never test on
   reps from a set you fit on. Reps in the same set are near-copies, so that flatters
   the model. Report mean absolute error in reps.
6. **Compare with your own RPE calls.** When you and the model disagree, the failure sets
   tell you who's right.
7. **Refit every training block.** Getting stronger shifts the relationship.

Only if v1 isn't good enough: add features like velocity loss % and first-rep MCV
(→ %1RM through the load-velocity profile). Still plain linear regression. The dataset
is small, so no machine learning.

**Built in Python** (roadmap 5.4, `analysis/rpe.py`, D36), reading app exports (D30): steps
2 to 6 above. A set to failure is one with "Your RPE" 10 (the app has no failure mark yet);
the attempt that failed onto the safeties isn't counted (nor a rerack after it), only the
last mini-set of a rest-pause counts down, and a set with no completed rep, a single from the
bottom, or a grinder that sank and split isn't labeled; a miss near lockout (0.8 of the rep
before or more) is counted, a limit until a rep can be marked as not one. A rep reps.py flags 'uneven lockout' keeps its place in the count
but not its MCV. Per exercise name: RIR from MCV by least squares predicts; your MCV at
failure and per rep in reserve (the two numbers v0 guesses, so a fit can replace them) come
from MCV on RIR, unbiased by MCV noise. Leave one session out, near failure (RIR 0-3) and
further, naming any session the others can't check; step 3's "one line across loads?"
within a session that took 2+ loads to failure (across days a load is also its day); each
normal set's range on the 5..10 scale next to your call. `--selftest`: a synthetic squatter
with a known line and day-to-day offsets, through the raw-signal pipeline, 24 sets to
failure whose completed reps are known (one a real recording with a miss spliced on), and
exact checks of each piece; 34 of 35 mutations (a rep,
a set or a session the fit saw leaking into its check, labels off by one, each labeling rule
off or forced, each fix undone) fail it. Waits for your sets to failure.

## 6. Data to start collecting now

These models are only as good as the logged data, and the data takes months to build
up. Start the habit at the very first bar session (roadmap 2.1), long before any of this
is built. For every set, log date, lift, load, reps, your RPE, whether it went to
failure, mount spot, paused at lockout or not, and the raw file name.

Template: `analysis/data/sets_log.csv`.

## 7. History and storage (app)

```
Session (date, notes)
  └── Set (lift, load, reps, your RPE, to failure, mount, raw samples)
        └── Rep (features from §1, quality flags)
```

- Storage: IndexedDB in the browser.
- Browser storage can get wiped (clearing site data, low storage). Call
  `navigator.storage.persist()`, and have an **export button** (JSON + raw CSV) from day
  one. Exports go into the repo (P2) so Python sees the same data as the app.
- Same raw file format as everything else, so any session can be re-run with a newer
  algorithm.
- Built (D30): Export / Export all write the session and its raw sets as JSON
  (`session.json` in each session's Documents/BarTracker folder in the app, a download in a
  browser); Import adds them back; Delete takes a past session out of the app.
  `python analysis/sessions.py <export> [--csv=DIR]` lists an export and writes its sets back
  out as the app's set CSVs.
- Cloud sync is a later decision (O6).

---

## References

From PubMed:

1. Helms ER et al. RPE and velocity relationships for the back squat, bench press, and
   deadlift in powerlifters. *J Strength Cond Res* 2017.
   [doi:10.1519/JSC.0000000000001517](https://doi.org/10.1519/JSC.0000000000001517)
2. García-Ramos A et al. Prediction of the maximum number of repetitions and repetitions
   in reserve from barbell velocity. *Int J Sports Physiol Perform* 2018.
   [doi:10.1123/ijspp.2017-0302](https://doi.org/10.1123/ijspp.2017-0302)
3. Jukic I et al. Modeling the repetitions-in-reserve-velocity relationship. *Physiol Rep*
   2024. [doi:10.14814/phy2.15955](https://doi.org/10.14814/phy2.15955)
4. Martínez-Rubio C et al. Estimating repetitions in reserve through movement velocity:
   applicability across bench press exercise modes and sexes. *J Sports Sci* 2025.
   [doi:10.1080/02640414.2025.2481363](https://doi.org/10.1080/02640414.2025.2481363)
5. Mansfield SK et al. The velocity of resistance exercise does not accurately assess
   repetitions-in-reserve. *Eur J Sport Sci* 2023.
   [doi:10.1080/17461391.2023.2235314](https://doi.org/10.1080/17461391.2023.2235314)
6. Chen W et al. Velocity-based monitoring lacks both accuracy and applicability for
   estimating repetitions in reserve during the hexagonal bar deadlift.
   *J Strength Cond Res* 2026.
   [doi:10.1519/JSC.0000000000005549](https://doi.org/10.1519/JSC.0000000000005549)
