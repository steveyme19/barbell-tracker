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
