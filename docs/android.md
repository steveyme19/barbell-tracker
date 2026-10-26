# Android app (Capacitor wrapper)

Decision D12. The app stays the same HTML/JS in `webapp/`. Capacitor wraps it in a real
Android app and lets JS call native code where the browser falls short:

| | browser (now) | native wrapper |
|---|---|---|
| phone sensors | ~60 Hz, every value rounded to 0.1 | 200 Hz (more with a permission), full precision, sensor timestamps |
| Bluetooth to the XIAO | Web Bluetooth: screen on, app open, reconnect by tapping | native BLE plugin: steadier, can run with the screen off later |
| saving CSVs | browser download | write to the phone's storage, share sheet |
| install | open a link | an app with an icon (sideload now, Play Store later) |

The algorithm doesn't change. `reps.js` runs the same inside the app, and the parity
test still runs in Node on the PC.

Work on this happens on Quentin's PC with Claude Code, because it needs Android Studio,
Gradle and the phone on USB. Do the steps in order and stop after each one so Quentin
can check it on the phone.

## A0. Setup (Quentin, once)

1. Install **Android Studio** (it brings the Android SDK and a JDK). Open it once and let
   it finish downloading the SDK.
2. Install **Node.js** LTS if `node --version` doesn't work in a terminal.
3. On the phone: Settings → About phone → tap **Build number** 7 times. Then Settings →
   System → Developer options → turn on **USB debugging**. Plug into the PC and accept
   the "Allow USB debugging?" prompt.
4. Install **Claude Code** in PowerShell: `irm https://claude.ai/install.ps1 | iex`, then
   open a new terminal. Git for Windows is recommended so it gets a Bash shell.
5. In the repo folder: `claude`, and start with:
   *"Read CLAUDE.md and docs/android.md, then do step A1. Stop when the app runs on my phone."*

## A1. Capacitor shell

- Root `package.json` (private) with `@capacitor/core`, `@capacitor/cli`,
  `@capacitor/android`. Add `node_modules/` to `.gitignore`.
- `capacitor.config.json`: appId `io.github.steveyme19.bartracker`, appName
  `Bar Tracker`, `webDir: "webapp"`. No bundler: `webapp/` is already the finished app.
- `npx cap add android`, `npx cap sync`, run it on the phone from Android Studio (or
  `npx cap run android`). Commit the generated `android/` folder (its own `.gitignore`
  keeps build output out).
- **Done when:** the app opens on the phone, Replay works on a file from
  `analysis/data/`, and Start with the phone sensor still works (inside the app it's the
  same browser sensor, ~60 Hz, until A2).
- Watch for: the Save CSV download doesn't work in the app's WebView. That's A3.
- Build notes (2026-10-16, laptop): Capacitor 8.5. The Gradle wrapper is bumped to
  9.1.0 so it runs on Java 25 (D13). `android/local.properties` (gitignored) holds
  `sdk.dir=C:/Users/quent/AppData/Local/Android/Sdk`, with forward slashes: single
  backslashes are escapes in a properties file and the build fails with "The filename,
  directory name, or volume label syntax is incorrect". Build and install without
  opening Android Studio:
  ```
  npx cap sync android
  cd android && gradlew.bat assembleDebug
  adb install -r app\build\outputs\apk\debug\app-debug.apk
  ```
  `adb` is `%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe`. Run `cap sync` after
  every change in `webapp/`, because the app ships a copy of it.

## A2. Native sensors

- A small custom plugin (Java or Kotlin) in `android/app/src/main/java/...`:
  - `SensorManager` with `TYPE_ACCELEROMETER` (includes gravity, which is what the
    algorithm wants) and `TYPE_GYROSCOPE`, at the fastest rate the phone allows.
  - Android 12+ caps apps at 200 Hz unless the manifest declares
    `android.permission.HIGH_SAMPLING_RATE_SENSORS`. Declare it.
  - Timestamps from `SensorEvent.timestamp` (nanoseconds, sensor clock), converted to
    seconds since start. Never the time the event reached JS.
  - Accel and gyro arrive as separate events. Emit one sample per accel event with the
    most recent gyro reading. Gyro comes in rad/s; convert to deg/s.
  - Batch about 50 ms of samples per `notifyListeners` call, so JS isn't flooded.
  - The stock `@capacitor/motion` plugin won't help: it's built on the same browser
    events.
- `NativeSensorSource` in `webapp/sources.js`, same interface and sample format as
  `PhoneSource`. `app.js` uses it when `Capacitor.isNativePlatform()`, else `PhoneSource`.
  The footer already shows the real rate.
- **Done when:** the footer shows 200+ Hz, and a recording replays in Python with the
  same results as the app (add one to `analysis/data/` and to the parity list).
- Then compare honestly: the 55.9 cm paper test and a box-squat set, native vs browser.
- Status (2026-10-16): built and running. `BarSensorsPlugin.java` + `NativeSensorSource`.
  On the Pixel 9 Pro XL (ICM45631) it measures **402 Hz** from sensor timestamps, spacing
  2.46 to 2.51 ms, |a| = 9.79 m/s^2 at rest. A 5-rep phone test recorded in the app,
  `analysis/data/bar-phone-2026-10-16-2123.csv`, gives the same ROM/MCV/peak/flags in
  Python as on screen (0.000 mm ROM difference) and is in the parity list. **Done.**
  Native vs browser squat (2026-10-16, same box squat about 30 s apart, ROM by eye ~ the
  same, no tape number):

  | | native 402 Hz (`bar-squat-...-2131`) | browser 60 Hz (`...-2134-browser`) |
  |---|---|---|
  | ROM per rep (cm) | 66.6 69.5 63.4 67.7 63.3 | 65.1 66.6 64.1 63.9 61.9 |
  | ROM mean / spread | 66.1 / 2.4 cm | 64.3 / 1.5 cm |
  | MCV (m/s) | .52 **.31** .57 **.38** .65 | .41 .46 .62 .57 .63 |
  | peak (m/s) | .98 .91 .95 1.13 1.17 | 1.13 1.11 1.26 1.06 1.20 |
  | after D15: ROM (cm) | 66.6 64.0 61.7 63.0 61.9 (63.5 / **1.8**) | unchanged |
  | after D15: MCV (m/s) | .52 .44 .57 .61 .65 | unchanged |

  Native did *not* come out better, and the reason isn't the sensor. Native's signal
  is cleaner (still-period std 0.035 vs 0.07 m/s^2), but two algorithm problems hit that
  set harder: (1) on reps 2 and 4 the start lands on a small bump while sitting on the
  box, ~1 s early, halving MCV; (2) the tops of reps 2 and 4 were held 1-2 s, and
  `velocity_hump` grows with still length (sway adds up), so they failed the 0.045 m/s
  hump test and weren't anchors; the browser set's ~0.5 s tops passed. Both are
  sample-rate independent. Until those are fixed, the error budget is segmentation and
  drift, not sample rate, so 400 Hz can't show its benefit yet. Re-run this comparison
  after fixing them, with a tape-measured ROM.
  **Fixed by D15** (hump limit; the early starts were a side effect of the same missing
  anchors). Native now: all lockouts level, no "uneven lockout", longest window 4.5 s
  (was 10.0), ROM spread 1.8 cm. Two different sets, so this is no per-rep proof that
  native beats browser; that needs a tape ROM or a paper test.
- Notes from building it:
  - No bundler means no `@capacitor/core`, so no `registerPlugin`. Use the proxy the
    bridge builds, `window.Capacitor.Plugins.BarSensors`.
  - Samples wait for the first gyro event, so none carries a fake 0 deg/s.
  - Stop waits for the plugin's last batch, so the final analysis and the CSV agree.
  - At 400 Hz one analysis pass over a 60 s set takes ~170 ms in the WebView (60 ms at
    60 Hz). Live passes now wait max(500 ms, 2x the last pass). Same algorithm, same input.
  - Getting a recording to the PC before A3: Stop the set, then
    `node android/pull-recording.mjs` saves it to `analysis/data/` with the usual name
    (reads `window.barDebug` in the debug WebView over adb).

## A3. Files and screen

- Save CSV: `@capacitor/filesystem` to write the file, `@capacitor/share` to send it on
  (Drive, a chat, email). Keep the browser download path for the web version.
- Keep the screen on during a set. Check whether the Screen Wake Lock API works in the
  WebView; if not, use a keep-awake plugin.
- Status (2026-10-16): built, mostly verified over USB.
  - Save CSV in the app: `BarSensorsPlugin.saveText` writes
    `Documents/BarTracker/bar-<lift>-....csv` (no storage permission on Android 11+),
    then `@capacitor/share` opens the share sheet. Closing the sheet keeps the file.
    Code in `webapp/native.js`; the browser keeps its download. Verified: Quentin saved
    a set and the share sheet opened and shared; the file
    (`analysis/data/bar-phone-2026-10-16-2206.csv`) pulled with adb reads in Python at
    402 Hz and passes parity. **Done.**
  - Not `@capacitor/filesystem`: it's Kotlin with `jvmToolchain(21)` and this PC only
    has Java 25 (D16). Writing one text file is ~15 lines in our own plugin.
  - Screen: `BarSensorsPlugin.keepScreenOn` sets `FLAG_KEEP_SCREEN_ON` on the window
    from Start to Stop (checked with `dumpsys window`: set during, cleared after). The
    WebView does have `navigator.wakeLock`, but whether it holds the screen depends on
    the host app, and the window flag is certain. The browser still uses Wake Lock.
  - Saved sets can also come off the phone with
    `adb pull /sdcard/Documents/BarTracker/<file> analysis/data/`.

## Session mode in the app (D17)

- Start once; the screen stays on for the whole session (`keepScreenOn`). Each set is
  saved to `Documents/BarTracker/session-<stamp>/` as it finishes, plus `session.csv`.
  Pull a whole session with `adb pull /sdcard/Documents/BarTracker/session-<stamp> analysis/data/`.
- **If you lock the phone yourself mid-session**, Capacitor pauses the WebView: JS
  timers stop (rest timer display, live analysis), but sensor batches still arrive as
  events, so sets are still found and saved. Everything catches up when you wake it.
  (Since 2026-10-17 RecordingService keeps the session running with the screen off, below.)
- Testing over USB with the phone locked: timer-paced replay doesn't run (frozen
  timers). Use the all-at-once replay, set `barDebug.autosaveReplays = true` to
  exercise the save path, and use a plugin round trip as the clock.
  Verified 2026-10-16: fake session -> 4 sets, rests 90/120/150 s, 4 set files +
  session.csv written; changing a set's lift re-saved it under the new name and deleted
  the old file.

## Recording with the screen off (2026-10-17)

- `RecordingService.java`: a foreground service (type `health`, needs
  HIGH_SAMPLING_RATE_SENSORS, which we declare) plus a partial wake lock (4 h cap), with a
  "Recording session" notification (POST_NOTIFICATIONS asked once on Android 13+; a pod session
  asks after the pod's connect: Android cancels a second permission prompt that comes while Nearby
  devices is up). Started at the Start tap, before a pod's connect (which can take 30 s, and the
  phone may be locked by then), stopped by End session (`BarSensors.record`).
- Android only lets an app start it while the app is in front. Over USB with the phone
  locked it's refused (`mAllowStartForeground false`); the session then still runs, with a
  message to keep the screen on.
- JS timers stop when the screen is off, sensor batches don't: while hidden, the live
  analysis runs from the data (every 0.5 s of samples), so the set-start and velocity-loss
  buzzes still happen. Buzzes are native (`BarSensors.vibrate`); a background app's
  vibration is ignored by Android, the service should count as foreground.
- Verified 2026-10-17 on the Pixel over USB: started from the app in front (allowed,
  PROC_STATE_TOP), screen turned off, sensor time kept advancing (13.8 s -> 34.0 s in 20 s
  dark), service foreground type health with its notification, wake lock held; End
  session removed both. The buzz played its pattern but was `ignored_for_settings`: the
  phone had **Use vibration & haptics** off (Settings > Sound & vibration), which drops
  every app's vibration. Turn it on for the set and velocity-loss buzzes.
- Checked 2026-10-25 with the switch on (`adb shell settings get system vibrate_on` reads it;
  0 = off): no session and the phone locked, Android drops the buzz as `ignored_background`;
  a session running (RecordingService) and the screen off, it plays (`finished`). Android's
  verdict on each buzz: `adb shell dumpsys vibrator_manager`, "Recent vibrations".

## A4. Bluetooth to the XIAO (roadmap 1.4 / 4.1)

Protocol: PodLink v1, `docs/ble.md` (D27). Build order: `docs/ble-plan.md`.

- **Browser** (desktop Chrome, Android Chrome): Web Bluetooth, `PodWebSource` in sources.js.
  A "Connect pod" tap picks the pod (Web Bluetooth needs a user gesture). Reconnects only
  while the page is visible.
- **App**: our own plain-Java Capacitor plugin `PodLinkPlugin` (no new dependency, Java 25 safe)
  and `PodNativeSource` in sources.js. Java is a dumb pipe: it connects, subscribes and hands
  raw frames to JS in batches (base64, every 50 ms); it never parses them. The one decoder is
  the `PodStream` in sources.js. Reconnects with `autoConnect` work with the screen off while
  RecordingService keeps the process alive (checked 2026-10-25, step 12, below).
- **Not** `@capacitor-community/bluetooth-le` (named here before): Kotlin, needs a bundler
  (P3), one event per notification, reconnects from JS.
- Permissions: Android 13+ only (`BLUETOOTH_SCAN` with `neverForLocation`, `BLUETOOTH_CONNECT`),
  `bluetooth_le` feature not required. Same D4 sample format, device timestamps. 13, not 12 (the
  plan's v1): the pod's address is random static and only `getRemoteLeDevice` (API 33) can say so;
  on 12 a reconnect after a Bluetooth restart waits forever for a public address (step 11 review).
- **`PodLinkPlugin.java`** (step 11, built 2026-10-24): `connect({any?})` asks for Nearby
  devices the first time, tries the saved pod (10 s), else scans 8 s for the PodLink service: the
  saved pod the moment it's heard, with none saved the strongest, and never another pod in place of
  the saved one unless `any: true` (picking a new pod). Then the connection order (services, MTU
  247, read INFO, read STATUS, the DATA CCCD), the address saved, and it resolves `{name, address,
  mtu, link, wallMs, elapsedMs}`. `write({b, link?})` (base64, CTRL with response, queued; a command
  computed for an earlier link is refused), `reread()` (INFO and STATUS again, for the decoder's
  need-info), `disconnect()`. Events `pod {f: [base64], w: [elapsedRealtime ms]}` every ~50 ms and
  `podState {state: connecting|linking|ready|lost|closed, status, mtu, link}`. An unrequested drop
  reconnects with autoConnect and runs the chain again ("linking" once its link is up, then "ready"
  with link + 1); JS answers with its command. Bluetooth switched off counts as a drop (Android can drop the client without a callback:
  an adapter-state receiver, and a 5 s no-frame watchdog, since the pod sends a STATUS every second);
  the reconnect waits until it's back on.
  Check it on the phone: `node android/podlink_check.mjs --install [--drop]` (the frames also go
  through the app's own PodStream; `--drop` makes the pod sleep-and-reset over USB to test the
  reconnect). Passed on the Pixel 9 Pro XL 2026-10-25: mtu 247, 13.2 frames/s, 210.7 samples/s with
  0 gaps, a reconnect by itself in 19 s.
  A debug build's Capacitor logs every event at verbose level: read logcat early, the 256 KiB buffer
  wraps in minutes.
- **`PodNativeSource`** (step 12, 2026-10-25): the app's pod source on the plugin, sharing the link
  logic (epochs, outages reported once, the STOP handshake, link.json) with `PodWebSource` through
  `PodSource`. The pod button picks the pod: the saved one, or after a second tap the nearest one
  next Start ("Pod: the nearest one"). At "ready" after a reconnect it sends the decoder's command
  for that link (a failed write on a live link is tried again, 1, 2, 4 s, then every 5 s); End during
  a reconnect still gets its STOP to the pod (or the pod keeps its session hold): during the
  plugin's order ("linking") it waits for "ready", at most 2 s. Each part of a resumed session
  writes its own `pod-<k>.json` / `link-<k>.json`. Test without a phone: `node webapp/test/pod_native.mjs`.
  On the phone: `node android/pod_session_check.mjs --install` (a session, the screen off 25 s, the
  pod's `w` with the screen dark, End, the files, the capture replayed through podlink.py, a second
  session; the screen-off step locks the phone, so unlock it when it asks; `--two` = only two short
  sessions back to back). Passed on the Pixel 2026-10-25: 210.6 samples/s with the page hidden; the
  pod asleep and back as a new boot by itself in 14.9 s with the screen still dark, nothing lost;
  STOP answered; link.json + pod.json saved; replay 9,762 = 9,762 samples; two sessions back to back.
  Plugin events (the frames) do reach the hidden page; only `requestAnimationFrame` stops, so the
  footer doesn't redraw while dark.
- **The soak** (ble-plan step 12): the pod on the laptop's USB the whole time, recorded by
  `python analysis/usb_clock.py <dir>/clock.json --samples=<dir>/usb` (every sample and each boot's
  clock: the truth). The app's session in parts, the phone on USB only for each command:
  `node android/soak.mjs start` (unlocked; the session with the capture on), `status`, `sleep` (the
  BLE SLEEP op), `end` (End, then the app's files, the capture, the buzz log and Android's vibration
  history into `analysis/data/soak-<stamp>/`). Then `python analysis/check_soak.py
  analysis/data/soak-<stamp> <dir>/clock.json`: the capture replays bit-exact, each epoch matches
  the USB samples (compare_ble_usb.py), each woken epoch's placement, each outage with its backlog
  and what it lost, the buzzes played with the screen off, the process and its service survived.

## Later

- Play Store: signing key, app bundle, store listing.
