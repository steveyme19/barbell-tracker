# Web app

Nothing here yet. Starts at roadmap step 0.4.

Plain HTML + JS, no framework, no build step. Hosted on GitHub Pages.

## Architecture (decision D4)

```
source  →  algorithm  →  UI
```

Every source emits the **same sample format**:

```js
{ t, ax, ay, az, gx, gy, gz }
// t: seconds, from the source's own clock (device timestamp for BLE)
// a: m/s², including gravity (what a raw accelerometer gives you)
// g: deg/s
```

| source | when | what it's for |
|---|---|---|
| `PhoneSource` | 0.4 | Live DeviceMotion. Building the UI with no hardware. |
| `ReplaySource` | 0.4 | Plays a recorded CSV (phyphox now, XIAO later). Real timing for UI demos, as fast as possible for tests. |
| `BleSource` | 4.1 | Web Bluetooth from the XIAO. |

The algorithm and UI never know which source they're getting. Switching from phone to
BLE touches one file.

## Parity test (roadmap 0.5)

Python is where the algorithm gets tuned. JS only has to match it.

1. Python runs on a recorded file and writes its per-rep results.
2. The JS algorithm runs on the same file through `ReplaySource`.
3. Compare. *Start* tolerance: ROM within 1 mm, MCV within 0.005 m/s.

Any time the Python algorithm changes, port the change and rerun this.

## History (phase 5)

Session → Set → Rep, stored in IndexedDB. Details and gotchas in
`docs/analytics.md` §7. Short version: call `navigator.storage.persist()` and ship an
export button on day one, because browser storage can get wiped.

## Things to know

- **DeviceMotion and Web Bluetooth both need a secure context**: HTTPS or `localhost`.
  GitHub Pages is HTTPS, so that's covered.
- For faster testing than pushing to Pages every time: run a local server on your
  laptop, plug in your phone, and use Chrome's port forwarding (`chrome://inspect` on
  the laptop). The phone then loads `localhost:8000`, which counts as secure.
- **DeviceMotion sample rate in Chrome is limited**, typically around 60 Hz. Check
  `event.interval` on your phone. That's why tuning happens on full-rate recordings,
  never on live phone data.
- DeviceMotion gives `acceleration`, `accelerationIncludingGravity`, and `rotationRate`
  (deg/s). `PhoneSource` should use `accelerationIncludingGravity` + `rotationRate` so
  it matches what the IMU will give you.
- Use the Screen Wake Lock API so the phone screen doesn't sleep mid-set.
