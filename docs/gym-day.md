# Gym day: the pod on a real bar (first session, squat + deadlift)

The first time the assembled pod rides a loaded bar (roadmap phase 2). Its job is raw data: the
app's numbers are relative until phase 3 gives a reference. Written 2026-10-31 from a readiness
audit (pod build 9, the app installed 10-31 13:34). Updated 2026-11-01: the app with D40 to D42
installed 11-01 11:30 (on the phone, the 10-31 sets replay as 8 / 6 / 3 / 5 / 10 reps), the pod on D38's
firmware (bench-checked 11-01), and a bench section. Updated 2026-11-02: the bench section for D43.

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

- **Before the first rep:** squat, stand still 2 s after the walkout; deadlift, the bar still on
  the floor 2 s before you grip it (see Deadlift below). Skip the old roll-the-bar gesture.
- **After the last rep:** squat, hold lockout 1 s, then walk in. Deadlift, leave the bar still on
  the floor for 2 to 3 s. Then **hands off the pod until the double buzz** (about 15 s), then take it
  off for plates. Touching it sooner adds a fake slow rep (a 10-31 test set did exactly this).
- **Deadlifts:** lowered under control, no drops: the IMU clips at 8 g and the magnets can let go.
  Don't roll the bar between reps. More below.
- **RPE:** decide it before looking at the phone, then type it on the set's card. 10 only for a
  true set to failure (squat only, safeties set). No deadlift sets to failure.
- **Notes on paper:** anything odd (a set with no card within about 20 s, a missed rep, a
  phantom set). **Don't tap "Not a set"**: it deletes the raw file. Since 10-31 a segment the app
  drops is saved anyway as `drop-*.csv`.

## Deadlift (updated 2026-11-03, D44)

Since D44 both filmed 11-02 sets read right against the video. The app reads raw today (no pod
calibration installed): set 1 -1/+3/+11%, set 2 +1/-4/+2% a rep (calibrated -1/+1/+6% and
+3/-2/-3%). That covers hands on the bar between reps or letting go (11-02's set 2, which logged no
set before), slow heavy pulls, a hitch at lockout and a grind through two stalls. What's left:
- Raw, a rep after a set-down that turns the sleeve can read about 11% high and 5 cm long (11-02
  set 1 rep 3: the accel's x/y bias), until a real calibration run (puck off) is installed.
- A heavy single reads right on synthetic sets calibrated, but raw about +15% (ROM +10 cm).
- Touch-and-go is synthetic only and only fast: slow touch-and-go, or touch-and-go after letting go
  of the bar, can miscount.

So:
- **Dead stops, hands on or off between reps:** both read right. Let go of the bar between reps if
  you want to; reset your grip as usual. Touch-and-go only fast, and check the count.
- **Before the first rep:** leave the bar still on the floor for 2 s before you grip it. A long setup
  with your hands on the bar and no still is corrected now, but the still is the cleaner start.
- **After the last rep:** as before, 2 to 3 s still on the floor, hands off the pod until the double
  buzz.
- **Film the sets** as for bench (below): the plate square to the camera, rim to rim, the whole set.
- **Read the closed card,** not the live numbers.

## Bench (added 2026-11-01, from synthetic sets: no real bench recording yet)

Since D43 (2026-11-01, still on synthetics) paused bench, heavy paused triples and singles, hard chest
bounces, soft reracks and a fumbled rerack read right on the card. What's left: fast touch-and-go
reps with a lockout pause read low (4 to 8% at 0.8 m/s, up to 14% on a 1 m/s warm-up); touch-and-go
with no lockout stop, or an ease-down out of lockout, can still read one rep 20% low or count the
unrack; a very soft chest touch can lose a paused single; and the live view is rougher than the
card. So:

- **Name:** `Bench` for touch-and-go, `Paused bench` for paused sets. Both track as bench, and separate
  names keep their load-velocity lines apart. A name without "bench" in it (Larsen, Spoto, floor
  press) is left to the sensor's machine or free-weight guess.
- **Safeties** below your lowest touch point. A knock the bar takes there (a jolt the gyro sees) is
  taken for the rerack and closes the set.
- **Unrack** with the smallest lift that clears the hooks (under 15 cm), carry, lock out, hold 1 to 2 s,
  and keep still for the last second before the first descent (no still at all before it: no card).
- **Bounce or pause as you like.** Chest pauses of 0.3 to 2 s and lockout breaths up to 8 s read
  right; a chest bounce is fine unless it twists the sleeve hard. On a paused rep let the bar settle
  into the chest (a touch that barely sinks can lose a paused single).
- **Touch-and-go:** stop at lockout about 0.5 s before each descent; don't ease down out of lockout
  without a stop (no stop: one rep can read 20% low; a short ease-down can count the unrack as a rep).
- **Tempo:** a 3 to 4 s lowering still gives no card. Lower in under 2 s for now.
- **Last rep:** hold lockout about 0.5 s, then rerack. Gently is fine now, and so is re-aiming after a
  missed hook. Then hands off the pod until the double buzz (about 15 s): only after a clanging rerack
  is 5 s enough; after a gentle one, taking it off sooner loses the last rep or adds one.
- **Read the closed card,** not the live numbers: while a paused rep is the newest it can show 36 to 50%
  low for a few seconds.
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
