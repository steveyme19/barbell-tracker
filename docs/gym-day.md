# Gym day: the pod on a real bar (first session, squat + deadlift)

The first time the assembled pod rides a loaded bar (roadmap phase 2). Its job is raw data: the
app's numbers are relative until phase 3 gives a reference. Written 2026-10-31 from a readiness
audit (pod build 9, the app installed 10-31 13:34). Updated 2026-11-01: the app with D40 to D42
installed 11-01 11:30 (on the phone, the 10-31 sets replay as 8 / 6 / 3 / 5 / 10 reps), the pod on D38's
firmware (bench-checked 11-01), and a bench section.

## Before leaving

- **Charge both.** The pod has plenty at 3.99 V (90 min awake is roughly 15 mAh of 400), but top it
  up if there's time (`charging no` on the USB status line means full). Charge the phone to 100%:
  the app keeps its screen on for the whole session.
- **Vibration on:** Settings > Sound & vibration > Vibration & haptics. The double buzz is how you
  know a set closed.
- **Pack:** a USB-C cable and a power bank, a paperclip or SIM pin (the reset pinhole), a fridge
  magnet, paper or a notes app.
- **Don't** use `o` or the phone's SLEEP mode 1 for the trip: only USB or the reset pinhole wakes
  the pod from those. In a bag it just wakes and sleeps on its own.

## At the bar

1. **Test the bar's end faces** with the fridge magnet and wipe them. The puck needs flat, clean
   steel (about 4 kg sideways hold). Check the clearance to the J-hooks, uprights and walls at
   that end.
2. **Plates and collars first, then the pod** on the sleeve end. It sticks out past the sleeve, so
   it comes off for every plate change. Hold it in your hand or set it down still. Never leave it
   on the floor near a loaded bar.
3. **Phone** face-up on the pod's side of the bar, 1 to 2 m away, the app in front. Don't pocket
   it, don't switch apps (film on another device), no Bluetooth earbuds this time.
4. **Bump the pod, then Start session.** Type the exercise exactly: `Squat`, later `Deadlift`.
   Change it only after the last squat set's double buzz.
5. **Weight (lb)** for the next set once the previous set's card has appeared.

## Every set

- **Before the first rep:** after the walkout (squat) or the setup (deadlift), stand still for
  2 s. Skip the old roll-the-bar gesture.
- **After the last rep:** squat, hold lockout 1 s, then walk in. Deadlift, leave the bar still on
  the floor for 2 to 3 s. Then **hands off the pod until the double buzz** (about 15 s), then take it
  off for plates. Touching it sooner adds a fake slow rep (a 10-31 test set did exactly this).
- **Deadlifts:** dead stop, about 1 s on the floor each rep, lowered under control. No drops: the
  IMU clips at 8 g and the magnets can let go. Don't roll the bar between reps.
- **RPE:** decide it before looking at the phone, then type it on the set's card. 10 only for a
  true set to failure (squat only, safeties set). No deadlift sets to failure.
- **Notes on paper:** anything odd (a set with no card within about 20 s, a missed rep, a
  phantom set). **Don't tap "Not a set"**: it deletes the raw file. Since 10-31 a segment the app
  drops is saved anyway as `drop-*.csv`.

## Bench (added 2026-11-01, from synthetic sets: no real bench recording yet)

Touch-and-go bench counted right in every synthetic run (48 of 48, set means within 1.4 to 3.7%) when
the rerack clangs. Paused bench is the weak spot: a chest pause of 0.75 s or more reads rep 1 38 to 64%
low, and heavy paused triples and paused singles can lose reps or the card (D40's open notes). Until
that's fixed:

- **Name:** `Bench` for touch-and-go, `Paused bench` for paused sets. Both track as bench, and separate
  names keep their load-velocity lines apart. A name without "bench" in it (Larsen, Spoto, floor
  press) is left to the sensor's machine or free-weight guess.
- **Safeties** below your lowest touch point. A jolt over about 2 g after the first lockout is taken
  for the rerack and closes the set.
- **Unrack** with the smallest lift that clears the hooks, carry, lock out, hold 1 to 2 s, and keep
  still for the last second before the first descent.
- **Touch the chest, don't bounce.** A bounce over about 2 g also reads as the rerack and empties the card.
- **Touch-and-go:** stop at lockout about 0.5 s (one breath) before each descent. Don't ease down out
  of lockout without a stop. Longer breaths at the top are fine.
- **Paused:** keep the chest pause to 0.5 s or less when you want numbers, and the lockout breath
  between paused reps to 1 s or less. A 1 s competition pause still counts, but don't trust rep 1's
  MCV or the card's loss and RPE estimate. Do heavy triples and singles touch-and-go for now (the raw
  data is saved either way).
- **Last rep:** hold lockout about 0.5 s, then carry back and set the bar firmly into the hooks in one
  motion, so it clangs. Don't place it gently or hover over the hooks. Missed a hook: lower it in from
  there, don't lift it above lockout to re-aim. Then hands off the pod until the double buzz.
- **Read the closed card,** not the live numbers: until the rerack they can run a rep ahead and read
  reps 1 and 2 far off.
- **Missed rep:** the spotter takes it straight to the hooks, or lower it onto the safeties. Write
  "miss" and the true count.
- **Film every set** on a second phone propped so it can't move: the lens at bar height (between chest
  and lockout), square to the plate face on the pod's side (the plate looks round), the 1x lens,
  landscape, the highest frame rate. The whole plate rim to rim through the whole set (its 450 mm is
  the scale; on 10-31 only its top edge was in frame), its logo in view or a strip of tape near the
  rim. Start before the unrack, stop after the double buzz, say the set number.
- **Notes per set:** load, the true rep count, touch-and-go or paused (and about how long), lockout
  breaths, the card's count, anything odd (a bounce, a soft rerack, a missed hook, a spot).

## If something goes wrong

- "Pod link lost, reconnecting": wait for it to clear before the next set (the pod buffers 38 s).
- "Pod asleep": bump the bar and wait for the banner to clear. Asleep with the IMU off (low
  battery, IMU lost): plug in USB or press reset once through the pinhole (twice enters the
  bootloader).
- The app died: reopen it and tap Resume. Finished sets are already saved.
- The pod fell: check the case. Cracked or swollen means stop using it (a LiPo).

## After

End session. Bring the phone back to the laptop; the session folder is
`/sdcard/Documents/BarTracker/session-<stamp>/` (set CSVs, `drop-*.csv`, `session.csv`,
`pod.json`, `link.json`), pulled into `analysis/data/` (P2).

## Known limits this session (not bugs to chase at the gym)

- Since D40 (after the 10-31 session) bar lifts use the gyro-tracked vertical and anchor each
  rep on its turnarounds, so touch-and-go squats and floor-to-floor deadlifts count without a
  still between reps. Still: leave the bar still for 2 to 3 s after the last rep (the set's end
  anchor), and hands off the pod until the double buzz.
- No pod calibration is applied yet (the app can since D42: `calib_pod.py`, then `--install` and a
  rebuild); squat MCV reads about 3% high against video (D40), and only 0.3 of that is the pod's scale.
  11-01's estimate pins z, which barely matters on the bar: keep recording raw.
- Seen on 10-31: the pod ran 40 min on its cell (4.06 V throughout), the camera in front of the
  app cost nothing (0 lost), one set lost 72 samples (flagged). Still untested: a real radio
  dropout in the app and long screen-off stretches.
