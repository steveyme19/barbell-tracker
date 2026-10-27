# Web app

Live rep tracking on your phone. Plain HTML + JS, no framework, no build step (P3).
Hosted on GitHub Pages.

**What it does now (session mode, D17):** start once and lift. It finds each set by
itself (`sets.js`, same as `analysis/sets.py`), runs the rep algorithm on it (same as
`analysis/reps.py`) and shows one big card: the live set (last rep's MCV, reps, ROM,
velocity loss, RPE estimate), then a big rest timer after the set. Every set gets a card
with its numbers, a lift picker to fix the lift, and boxes for weight and your RPE.

## Use it

Android app (best: 400 Hz, auto-save): see `docs/android.md`. Browser: open
`https://<your-username>.github.io/<repo-name>/webapp/` in Chrome on your phone.

1. Type the exercise and weight for the next set (both stick until you change them;
   recent names are suggested). The line under them says how it'll be tracked: squat /
   bench / deadlift by name, anything else as a weight-stack machine (phone strapped
   to the top plate). Clip the phone on, tap **Start session**. The screen stays on.
2. Lift. Hold the bar still for a second before and after each set (racked is fine).
3. Rest: the card shows the timer. Type the next exercise / weight if it changes, and
   your RPE on the set card if you like. Wrong name on a set? Fix it on its card.
4. **End session** when you're done.

Saving: the app writes every set to `Documents/BarTracker/session-<stamp>/` as it
finishes (`bar-<lift>-<stamp>-setNN.csv`, raw) plus `session.csv` (one row per set:
lift, reps, rest, MCVs, velocity loss, RPE estimate, weight, your RPE; no raw data).
**Share** sends `session.csv`; a set's **Save CSV** sends its raw file. In the browser
those buttons download instead (no auto-save).

Look: classic and restrained (warm off-white, ink, oxblood for a set in progress, navy
for rest; sans for the UI, serif for the title and big numbers). Colors are CSS
variables at the top of `style.css`; device fonts only, so it works offline.

History (below the sets): every session is also kept in the app (IndexedDB). Open an old
one to see or edit it; if the app was killed mid-session, the next start offers **Resume**
(rest across the gap is timed with the wall clock). The CSVs in Documents stay the real
backup: browser storage can be wiped.

Velocity-loss alert: **Stop at loss** (default 20 %, 0 = off). When a set's last rep is that
much slower than its best, the phone buzzes long-short-long and the card turns red.

Calibration (Tools > Sensor calibration): lay the phone on each of its 6 sides, still for 2 s
each; it buzzes per side. Live sessions then use corrected acceleration (D23).

Tools (at the bottom): replay a recording (a long file shows up as sets; real time at
1/10/30x, or all at once), the live acceleration plot and the sample rate.

The pod (Chrome on a laptop or Android, https or localhost): tap **Connect pod**, pick
`BarPod-XXXX` in the browser's list, then **Start session** as usual. The choice sticks
(**Use the phone** switches back), but after a page reload the browser needs the Connect
tap once more. The footer shows `pod BarPod-1A2B · 211 Hz · 16/pkt · 45 ms · lost 0 · gaps 0
· 3.95 V`. Pod data is raw (no phone calibration) until roadmap 1.3. A dropped link ends the
stream for now (End, then Start); reconnecting by itself is ble-plan step 10. Laptop: turn
Bluetooth headphones off, they starve the scan (D28).

## Files

| file | what |
|---|---|
| `index.html`, `style.css` | the page |
| `app.js` | UI wiring: source → session tracker → algorithm → UI |
| `sets.js` | session mode: finds sets + rest live, ported from `analysis/sets.py`; RPE estimate v0 |
| `native.js` | Android-app-only extras: save/share files, keep the screen on |
| `calib.js` | 6-face accelerometer calibration (D23), ported from `analysis/calib.py` |
| `lv.js` | load-velocity profile + e1RM (D22), ported from `analysis/lv.py` |
| `store.js` | session history in IndexedDB: a small record per session + each set's raw samples (history list, resume after the app was killed, export, import, delete) |
| `export.js` | the session export file (D30): JSON with the raw sets as base64 typed arrays, bit-exact both ways; `analysis/sessions.py` reads it |
| `sources.js` | `PhoneSource` (DeviceMotion), `NativeSensorSource` (app), `ReplaySource` (recordings), `PodStream` (the pod's decoder, a port of `analysis/podlink.py`), `PodSource` (the link logic both pod sources share: epochs, outages, the STOP handshake, link.json), `PodWebSource` (the pod over Web Bluetooth; reconnects by itself after a dropout, a reset or a sleep) and `PodNativeSource` (the app: the pod over `PodLinkPlugin`, which reconnects by itself, also with the screen off) |
| `gaps.js` | the set cards' data-gap flags from the pod's decoder (a dropout longer than its ring, a restart, a link that never came back) |
| `reps.js` | the rep algorithm, ported line for line from `analysis/reps.py` |
| `fileio.js` | read phyphox CSV/zip, write CSV |
| `plot.js` | tiny canvas plotter |
| `test/parity.mjs` | checks `reps.js` against `reps.py` |
| `test/pod.mjs` | checks `PodStream` against `podlink.py` (36 simulator scenarios, bit for bit) |
| `test/pod_web.mjs` | `PodWebSource`'s failure and reconnect paths against a fake Bluetooth device that behaves like Chrome |
| `test/pod_native.mjs` | `PodNativeSource` against a fake `PodLinkPlugin` (start, reconnects, a sleep, End mid-reconnect, a retried write) |
| `test/gaps.mjs` | `gaps.js` on made-up sets |
| `test/export.mjs` | `export.js`: a bit-exact round trip, malformed files refused, and `analysis/sessions.py` reading the same file |
| `test/pod_browser.mjs` | bench: the real pod in real Chrome over the DevTools protocol (`--reps` for hand reps) |
| `test/pod_reconnect.mjs` | bench: dropouts, a reset and a sleep in real Chrome (`--hands-free` with `analysis/usb_clock.py`) |
| `test/cdp.mjs` | the DevTools-protocol client and Chrome launcher the two bench scripts share |

## Architecture (decision D4)

```
source  →  algorithm  →  UI
```

Every source emits the same sample format:

```js
{ t, ax, ay, az, gx, gy, gz }
// t: seconds, from the source's own clock (device timestamp for BLE)
// a: m/s², including gravity (what a raw accelerometer gives you)
// g: deg/s
```

The algorithm and UI never know which source they're getting. Switching from phone to
BLE touches one file.

## Parity test (roadmap 0.5)

Python is where the algorithm gets tuned. JS only has to match it. From the repo root,
with the Python venv active and [Node](https://nodejs.org) installed:

```
node webapp/test/parity.mjs
```

Pass = ROM within 1 mm and MCV within 0.005 m/s on every rep, plus the same flags. Each
file is also checked cut off partway through, which is what the live app sees mid-set.
Missing fake files get skipped; make them with the three commands in
`analysis/data/README.md`. The lift comes from the file name, same as the app. Last run
(2026-10-16, with per-lift leveling): all twelve test files, full and cut off, passed
with 0.000 mm difference. Rerun it any time
either version changes.

## GitHub Pages setup (once)

1. Repo on GitHub → **Settings → Pages**.
2. Source: **Deploy from a branch**. Branch: **main**, folder: **/ (root)**. Save.
3. Wait a minute, then open `https://<your-username>.github.io/<repo-name>/webapp/`.

On a free GitHub account, Pages only works on **public** repos. The GitHub Student
Developer Pack gives you Pro for free, which allows private repos too.

The empty `.nojekyll` file in the repo root tells Pages to serve files as-is.

## Things to know

- **DeviceMotion and Web Bluetooth both need a secure context**: HTTPS or `localhost`.
  GitHub Pages is HTTPS. A laptop server opened from your phone over Wi-Fi is *not*.
- For faster phone testing than pushing every time: run `python -m http.server` in the
  repo root, plug the phone into the laptop over USB, and use Chrome's port forwarding
  (`chrome://inspect` on the laptop). The phone then loads `localhost:8000/webapp/`,
  which counts as secure.
- **DeviceMotion is rate-limited**, typically about 60 Hz in Chrome, with timestamps that
  jitter (a few ms to 40+ ms apart in testing). The app uses the real timestamps, so
  jitter is handled. The footer shows the rate you're actually getting.
- Uses the Screen Wake Lock API so the screen doesn't sleep mid-set.
- **Pick the lift** above the Start button. It tells the drift fix which heights repeat
  every rep (algorithm.md §6b, "Per lift"). Phone test assumes only the lockouts repeat.
  The choice is remembered, goes in the saved file name (`bar-bench-...csv`), and a
  replayed file with a lift in its name switches the picker.
- **Hold still for a full second before tapping Stop.** Stills that long always count as
  anchors. A shorter one at the end of a long no-pause set has to pass the coasting
  check, and if it doesn't, the whole set goes uncorrected.
- Browser motion data comes in at about 60 Hz with every value rounded to 0.1 (m/s² and
  °/s). Fine for this, but it's why the phone is a prototype and not the product.
- Live results update every 0.5 s. Everything since your last pause is **provisional**
  (gray dashes on the height plot, gray number on top): the drift fix needs a still
  moment at the end to finish. From the 2nd lockout of a no-pause set it already levels
  the lockouts, so the numbers are close (about 2 to 3 cm), then they settle when you
  pause or rack.
- No-pause sets: with 3+ lockouts between two still moments, the algorithm also assumes
  every lockout is at the same height and removes curved drift with that (algorithm.md
  §6b). Reps that miss the common height by 2+ cm get an "uneven lockout" flag.

## History (phase 5)

Session → Set → Rep, stored in IndexedDB. Details in `docs/analytics.md` §7. Short
version: call `navigator.storage.persist()` and ship an export button on day one,
because browser storage can get wiped.
