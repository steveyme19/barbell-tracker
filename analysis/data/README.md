# Data

Raw recordings live here and get committed. They're small, and the algorithm will
change many times. Raw data lets you re-run old sessions with the new version.

## Naming

- Step 0.1 phone lifts: `lift_01`, `lift_02`, ... (raw phyphox export: .zip, .xlsx or .csv
  all work)
- Step 0.2 phone rep sets: `reps_pause.zip`, `reps_nopause.zip`
- Web app recordings (Save CSV): `bar-<lift>-YYYY-MM-DD-HHMM.csv`, 60 Hz, values rounded
  to 0.1 by the browser. The lift in the name tells `reps.py` which heights repeat
  (older files without one count as phone tests). `bar-2026-10-16-1835.csv` is the first one (6 no-pause reps),
  `-1844` has pauses, `-1845` is 6 no-pause reps (all hand-held squats, from 1844 on), and
  `-1920` is 5 squats with the same ROM every rep (pause at the top after reps 2 and 4).
- Session-mode sets: `bar-<profile>-YYYY-MM-DD-HHMM-setNN-<exercise>.csv`.
  `bar-machine-2026-10-17-1334-set01-lat-raise.csv` is a cable lateral raise with the
  phone lying on the weight stack (D20: the smooth lowering looked still).
- Later bar sessions: `YYYY-MM-DD_<lift>_<load>_<notes>.csv`,
  e.g. `2026-11-03_squat_225_set2.csv`

Fake data from `make_fake_lift.py` and `make_fake_reps.py` (`fake_*.csv`) is ignored by
git since you can regenerate it any time. The parity test wants these five:

```
python make_fake_reps.py
python make_fake_reps.py nopause
python make_fake_reps.py drift
python make_fake_reps.py lifts
python demos/bench_jcurve.py --save
```

## sets_log.csv

One row per set, starting at the first bar session (roadmap 2.1). This is the
training data for consistency and RPE later (docs/analytics.md §6), so don't skip it.

| column | example | notes |
|---|---|---|
| date | 2026-11-03 | |
| file | 2026-11-03_squat_225_set2.csv | raw recording for this set |
| lift | squat | squat / bench / deadlift / smith_squat ... |
| load_lb | 225 | |
| reps | 5 | |
| rpe | 8 | your call right after the set, before looking at any numbers |
| to_failure | n | y only if you actually failed the next rep |
| mount | sleeve_end | sleeve_end / shaft |
| paused_lockout | y | did you stop at the top between reps |
| notes | bar drifted fwd rep 4 | anything weird, deliberate faults, etc. |

Call your RPE **before** you look at the velocity numbers, or you'll anchor on them and
the labels stop being independent.
