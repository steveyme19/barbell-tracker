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

## A4. Bluetooth to the XIAO (roadmap 4.1)

- `@capacitor-community/bluetooth-le` for `BleSource` in the app, Web Bluetooth in the
  browser. Same sample format, timestamps from the device (roadmap phase 1 rules).

## Later

- Recording with the screen off (foreground service).
- Play Store: signing key, app bundle, store listing.
