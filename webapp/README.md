# Web app

Live rep tracking on your phone. Plain HTML + JS, no framework, no build step (P3).
Hosted on GitHub Pages.

**What it does now (v0):** reads your phone's motion sensor, finds reps live with the
same algorithm as `analysis/reps.py`, shows the last rep's mean velocity big, plus ROM,
peak velocity, velocity loss, a height plot and a rep table. Save CSV exports the session
in a format the Python scripts read. Replay a recording runs a phyphox `.zip` or `.csv`
through the same pipeline.

## Use it

Open `https://<your-username>.github.io/<repo-name>/webapp/` in Chrome on your phone
(setup below). Hold the phone flat and still, tap **Start**, wait a second, do reps,
pausing briefly at the top or bottom of each. Tap **Stop**, then **Save CSV** if you
want the data in `analysis/data/`.

## Files

| file | what |
|---|---|
| `index.html`, `style.css` | the page |
| `app.js` | UI wiring: source → algorithm → UI |
| `sources.js` | `PhoneSource` (DeviceMotion), `ReplaySource` (recordings). `BleSource` comes in phase 4. |
| `reps.js` | the rep algorithm, ported line for line from `analysis/reps.py` |
| `fileio.js` | read phyphox CSV/zip, write CSV |
| `plot.js` | tiny canvas plotter |
| `test/parity.mjs` | checks `reps.js` against `reps.py` |

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
`analysis/data/README.md`. Last run (2026-10-16, with provisional mid-set leveling): all
eight test files, full and cut off, passed with 0.000 mm difference. Rerun it any time
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
