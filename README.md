# Barbell Velocity Tracker

A small IMU that clips onto a barbell and reports, per rep:

- **Mean concentric velocity** (main metric)
- **Peak velocity**
- **Range of motion**

Later:

- **Bar path** (side view) and bar tilt per rep
- **Consistency** rep to rep, set to set, and session to session, against your own reps
- **Depth check** against a filmed reference rep
- **Sticking point** location and how it moves over time
- **RPE estimate** from a personal reps-in-reserve vs. velocity model
- Load-velocity profile for estimating 1RM from submax sets
- Alert when velocity drops past a set threshold

How the analytics work and their limits: [docs/analytics.md](docs/analytics.md).
What existing products get wrong and where this one aims to do better:
[docs/prior-art.md](docs/prior-art.md). Sensors other than the IMU and when they'd help:
[docs/sensor-options.md](docs/sensor-options.md).

## How it works (short version)

1. The IMU measures acceleration and rotation rate.
2. Orientation tracking (Madgwick) figures out which way is up, even as the bar or
   sleeve rolls, so gravity can be subtracted.
3. Stillness detection finds moments when the bar is truly stopped (lockout, floor).
4. Acceleration is integrated to velocity between still moments. Velocity must be zero
   at each one, so any leftover is drift and gets removed.
5. The bottom of each rep is found where corrected velocity crosses zero. It's an
   output, not a correction point.
6. Per-rep metrics come from the concentric (upward) phase.

Full spec: [docs/algorithm.md](docs/algorithm.md).

## Repo layout

| folder | what's in it |
|---|---|
| `docs/` | Roadmap, decisions log, and a lesson write-up for each step |
| `analysis/` | Python: data analysis, algorithm reference implementation, plots |
| `firmware/` | Arduino code for the XIAO nRF52840 Sense |
| `webapp/` | Phone web app (plain JS, Web Bluetooth, hosted on GitHub Pages) |
| `hardware/` | Parts list for every phase ([BOM.md](hardware/BOM.md)), KiCad PCB, Fusion 360 enclosure files |

## Status

**Phase 0, step 0.1:** measuring a phone lift with phyphox data.
Start at [docs/01-phone-lift.md](docs/01-phone-lift.md). Full plan in
[docs/roadmap.md](docs/roadmap.md).

## Stack

- Seeed XIAO nRF52840 Sense (LSM6DS3TR-C IMU, BLE), Arduino IDE, 3.7 V LiPo
- Python: numpy, pandas, matplotlib, bleak
- Web app: JavaScript, DeviceMotion → Web Bluetooth, Chrome on Android, GitHub Pages
- KiCad, Fusion 360
