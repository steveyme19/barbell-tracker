# Gym day: the pod on a real bar (first session, squat + deadlift)

The first time the assembled pod rides a loaded bar (roadmap phase 2). Its job is raw data: the
app's numbers are relative until phase 3 gives a reference. Written 2026-10-31 from a readiness
audit (pod build 9, the app installed 10-31 13:34).

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

- No pod calibration is applied. On the sleeve end gravity falls on the uncalibrated x/y axes,
  so |a| at rest shifts a little with the sleeve's roll; enough roll between the stills of one set
  can drop it (its raw is kept). D37 and D38 hold the follow-ups.
- Never tested before: the pod on its cell for a whole session, a real radio dropout in the app,
  the screen-off path for more than a minute, a loaded bar at all.
