# Firmware

Target: Seeed XIAO nRF52840 Sense, Arduino IDE. Board package **Seeed nRF52 Boards**
(non-mbed, D24), board "Seeed XIAO nRF52840 Sense", library "Seeed Arduino LSM6DS3".

| sketch | what |
|---|---|
| `pod/` | **The device firmware.** Same stream as `imu_serial` plus power management (D26): auto-sleep after 10 min still on battery, wake on motion via the IMU's wake-up interrupt; lid-down and still for 6 s = sleep now (only when no PC has the port open and no phone holds a session; the pod has to lie within about 20° of flat until the 1.3 calibration, so a USB cable propping it up stops it); below 3.30 V on battery it sleeps with the IMU off until USB or reset, checked at every boot before the radio or the sampler start (so a reset press on a flat pod gives 3 red blinks and it sleeps again) and every 5 s while awake, never while asleep (D38); IMU not found: on battery, or once USB goes, it sleeps with the IMU off; 100 mA charging; a battery/charging status line about every 30 s. Sampling (`sampler.ino`, D29): the IMU's data-ready pulse on INT1 is stamped in an interrupt and wakes a top-priority task that reads the sample and stores it in an 8192-sample ring (~38 s); loop() only sends from the ring, so neither the radio nor a USB host that stops reading can delay or drop a sample. A watchdog resets the board if loop() stops for 8 s. Before any sleep (every reason, and the phone's SLEEP command) a connected phone gets the rest of the stream, the reason and a deliberate disconnect (step 9, docs/ble.md 6). The ring goes out over Bluetooth as `BarPod-XXXX` (`ble.ino`, PodLink v1: `docs/ble.md`, D27/D28; check it from the laptop with `analysis/ble_check.py`, Bluetooth headphones off); `PKT_OVER_USB = true` also prints the frames over USB for `analysis/podlink.py --usb`. Bench commands over USB: `s` sleep, `o` sleep with the IMU off, `w` dry run of `s` (the phone side included, then a reset), `i` stop the IMU (the sampler restarts it after ~1 s: tests IMU_RESTART), `h` hang loop() (the watchdog resets it after 8 s). Every 30 s: `# ble` (link state) and `# ring` (since boot: missed and late samples, time holes, accel lag, I2C read errors, IMU restarts, worst stamp-to-store latency since the last line, CSV lines lost to a slow host, dropped commands and `#` lines). |
| `imu_serial/` | IMU over USB at "208 Hz" (really 209 to 212, per board): `t_us,ax,ay,az,gx,gy,gz` per line (m/s^2, deg/s), `t_us` from RTC2 on the crystal (D25). Record it with `python analysis/log_serial.py COMx`, watch it live with `python analysis/live_plot.py`. **USB only:** without USB power it switches off (System OFF, the IMU unpowered; plugging USB in or reset wakes it, D38), since on the pod the cell is soldered on and this sketch has no battery reading or sleep of its own. Builds before 2026-10-31 ran the cell to the protection board's trip: flash the pod firmware back before unplugging anyway. |

## Flashing from the terminal

**Use `python firmware/flash.py [sketch] [port]`** (default `pod`, port found by USB ID). It first
checks the installed core still drives P0.14 LOW at boot (the patch below) and refuses to build
if a core update undid it, then compiles and uploads. The raw arduino-cli command still works
(not on the pod: with its battery soldered on, D38, it skips the P0.14 check while charging):

arduino-cli ships inside Arduino IDE 2
(`%LOCALAPPDATA%\Programs\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe`):

```
arduino-cli compile --fqbn Seeeduino:nrf52:xiaonRF52840Sense -u -p COMx firmware/imu_serial
```

The running sketch and the bootloader show up on different COM ports, and the number
changes per board and per upload; `arduino-cli` follows it. If the port is missing,
double-tap reset to force the bootloader.

## Bring-up results (2026-10-18, USB, no battery)

| board | rate | gaps | \|a\| at rest | wiggle | gyro offset (deg/s) |
|---|---|---|---|---|---|
| 1 | 211.8 Hz | 0 | 10.12 | 0.006 | +0.76 -1.89 +0.16 |
| 2 | 209.3 Hz | 0 | 10.10 | 0.007 | +0.01 -2.03 +0.58 |
| 3 | 210.4 Hz | 0 | 10.12 | 0.008* | ~+0.5 -3 +1 |

\* still seconds only; it got bumped during the 5 s check. All three report USB PID
0x8045 and their IMU answers on the Sense's internal bus. |a| is ~3% high on all of
them: the 6-position calibration (roadmap 1.3) fixes it. Axis directions:
`hardware/enclosure.md` ("XIAO axes"); `analysis/axis_map.py COMx` redoes them.

## Ground rules (from the roadmap)

- One new thing at a time: blink → IMU over USB serial → BLE.
- Every sample gets a device timestamp. Every BLE packet gets a sequence counter.
- Sample timing comes from a timer, the IMU data-ready signal, or the IMU FIFO.
  Never `delay()`.

## Board facts (Seeed wiki + Seeed's KiCad project for the Sense, 2026-10-19)

| what | pin | notes |
|---|---|---|
| battery read enable | P0.14 (`VBAT_ENABLE`, D14) | **keep LOW**. HIGH or floating lets P0.31 rise past its 3.6 V limit |
| battery voltage | P0.31 (`PIN_VBAT`, D32) | divider VBAT -1M- P0.31 -510k- P0.14: VBAT = V(P0.31) x 1510/510 = x 2.961. ~340 kOhm source: use `analogSampleTime(40)` (default 3 us reads low). LOW P0.14 costs ~2.8 uA |
| charge current | P0.13 (`PIN_CHARGING_CURRENT`, D22) | floating (INPUT) = 50 mA, OUTPUT LOW = 100 mA (R12 2.7k to the BQ25101 ISET). The wiki's "low current" example drives it HIGH; the text says floating, `pod` follows the text |
| charging status | P0.17 (D23) | LOW while charging (also lights the red CHG LED) |
| IMU power | P1.08 (`PIN_LSM6DS3TR_C_POWER`, D15) | the I2C pull-ups hang off it too |
| IMU interrupt 1 | P0.11 (`PIN_LSM6DS3TR_C_INT1`, D18) | wake-up / tap / data-ready; INT2 isn't connected |
| RGB LED | red P0.26 (D11), green P0.30 (D13), blue P0.06 (D12) | active LOW |
| QSPI flash (P25Q16H, 2 MB) | SCK P0.21, CS P0.25, IO0-3 P0.20/24/22/23 | send 0xB9 (deep power-down) before System OFF, as Seeed's sleep example does |
| bootloader / stuck upload | | reset once; if that fails double-tap reset; then replug (with the battery soldered on, D38, a replug only re-enumerates USB: it isn't a power cycle) |

Standby: Seeed quotes < 5 uA. GPIO states hold through System OFF, which is why P0.14 LOW,
P0.13 and the IMU power pin stay put while asleep. Waking from System OFF is a reset
(`setup()` runs again); `NRF_POWER->RESETREAS` says why (OFF = GPIO/motion, VBUS = USB).

### pod bench results (2026-10-19, USB only, no battery fitted)

- Streams at 210 to 211 Hz, crystal timestamps, same CSV as imu_serial.
- Sleep / wake: `s` -> System OFF and stays off (20 s, 3 times); a gentle, slow pickup wakes it
  (`RESETREAS 0x10000`, OFF/GPIO); the lid-down gesture (port closed) sleeps it, picking it up
  wakes it; `o` stays off until reset or USB replug; reset reads `0x1`, replug `0x0`.
- **Instant re-wake bug, fixed empirically:** entering System OFF within ~1 ms of clearing the
  IMU's wake latch woke it immediately 4/4 times (DETECT, INT1 measured low just before); with
  time between the clear and OFF it stayed asleep every time since. The 10 s dry run (`w`)
  never saw INT1 fire, old or new settle, so the root cause is still open. The code now waits
  ~10 samples, then clears until INT1 stays low for 2 samples, then goes off.
  **Correction (step 8, 2026-10-24):** in this build `digitalRead(INT1)` always read LOW (the
  pin's input buffer was never connected), so the dry run's "quiet" and the "stays low" check
  saw nothing: the fix that worked was the ~1 s of settle delay. Since step 8 the pin is an input
  before both, and the dry run catches a real pulse (moved during `w`: fired after 1973 ms).
- Wake threshold from data: real pickups (9 recordings, decimated to the IMU's 12.5 Hz sleep
  rate) peak at 114 to 437 mg on the slope filter, so `WAKE_THS` 2 (62.5 mg, 1.8x margin on the
  gentlest) instead of 3 (1.2x). Dry runs at 2: 5/5 quiet.
- `woke by` uses the core's `readResetReason()`: the core's `init()` saves RESETREAS and clears
  it before `setup()`.
- Not measured yet (no battery wired, no meter): sleep current, battery reading accuracy, the
  low-battery cutoff on a real cell. Awake, the tight data-ready poll keeps the CPU busy
  (~6 mA more than needed, an estimate): step 8 moved data-ready to an interrupt, still unmeasured.
- **P0.14 at boot (patched 2026-10-19):** Seeed's `initVariant()` drove it HIGH on every boot
  and wake until `setup()` set it LOW, a few ms (P0.31 up to ~3.6 V through 1 MOhm, < 1 uA).
  The installed core is patched to drive it LOW: `%LOCALAPPDATA%\Arduino15\packages\Seeeduino\hardware\nrf52\1.1.13\variants\Seeed_XIAO_nRF52840_Sense\variant.cpp`
  (original kept as `variant.cpp.orig`). `flash.py` refuses to build if a core update reverts
  it; redo the one-line change after updating the core. Checked: the guard accepts the patched
  core and refuses Seeed's original.

### Battery guards (D38, 2026-10-31): bench plan

Three guards for the soldered cell: the pod's cutoff at boot, the pod's IMU-not-found loop
sleeping once USB goes, imu_serial switching off without USB. **Compiled clean** (both sketches,
flash.py's core check, `--warnings all`: 0 warnings). **Not flashed or bench-checked yet.**

Before any of it: the pod has to be free (not mid-session), Bluetooth headphones off, and only
`python firmware/flash.py` builds go on it (every USB session charges, so the P0.14 rule is live).
The two test builds below are one-line edits to `firmware/pod/pod.ino` (flash.py builds only from
the repo; both edits compile clean, checked 10-31): revert each before committing anything. Each is
useless on battery: **flash the normal pod build back, on USB, before the pod leaves the bench.** Every step that plugs USB into a sleeping pod also checks a D38 open item:
that USB wakes a System OFF sleep with the cell on (`woke by: USB power (from sleep)`).

**G1, the cutoff at boot (pod).** The real threshold can't be reached on the bench: the cell is at
~4.06 V and soldered on, so there's no bench supply in its place, and running it down awake takes
days. So a test build makes every cell "low": `LOW_BATT_V = 4.50` in pod.ino.
1. Flash it on USB. It boots and streams as usual (no cutoff on USB power), `# battery 4.0x V`.
2. Send `s` (motion-wake sleep: blue x3), then unplug USB. It should stay asleep. If it gives 3 red
   blinks right at the unplug, the unplug rebooted it: note that (D38 open: does unplugging give a
   new boot?) and go to step 4.
3. Pick it up. **Expect 3 red blinks within ~1 s, then dark, and no `BarPod-XXXX` in
   `python analysis/ble_check.py`'s scan.** This is D38's case: woken near empty. Before the
   guard it booted, advertised, and could sleep again lid-down before its first reading.
4. Pick it up again, shake it: nothing (the IMU is off).
5. Press the reset pinhole: 3 red blinks again, instead of a normal boot's one green blink (a flat
   pod answers a reset this way).
6. Plug USB: it wakes and streams; the banner says `woke by: USB power (from sleep)`.
7. The plug-in race: unplug (the awake cutoff sleeps it within ~15 s, red x3), press reset, and
   plug USB in during the red blinks. **Expect it to come up streaming** (banner `power on /
   upload`: the soft reset) instead of staying dark on the cable: VBUS wakes System OFF only on
   its rising edge, so goToSleep() rechecks USB just before System OFF.
8. Flash the normal build back (`LOW_BATT_V = 3.30`).
Can't be checked on the bench: the cutoff at the real 3.30 V on the real cell, and how close
`batteryVolts()` reads to a meter (D38 open).

**G2, the IMU-not-found loop (pod).** Test build: in setup(), `if (!imuStart())` ->
`if (!imuStart() || true)` (the IMU starts, then counts as missing).
1. Flash it on USB: `# IMU not found...` and one red blink about every 1.2 s.
2. Unplug USB. **Expect 3 red blinks within ~1.2 s, then dark**: asleep, the IMU off. Before the
   guard the 1 Hz red blink ran on the cell until the protection board tripped.
3. Plug USB: it wakes (VBUS) and the 1 Hz blink is back.
4. Flash the normal build back.
Fully checkable on the bench: the LED is the evidence.

**G3, imu_serial off without USB.** No test build needed.
1. `python firmware/flash.py imu_serial`, then `python analysis/log_serial.py COMx` (or any serial
   terminal): samples at ~210 Hz, `t_us` counting up from 0 at boot.
2. Unplug USB for ~30 s, plug it back, reopen the port. **Expect `t_us` to start again from a few
   seconds** (a fresh boot: it was off and VBUS woke it). Before the guard it ran on the cell, so
   `t_us` carried on from ~30 s past where it stopped. (The `#` header can be missed if the port
   opens more than 5 s after the plug-in; `t_us` can't.)
3. `python firmware/flash.py` (the pod firmware) before the pod is unplugged again.
Can't be checked on the bench: the off current (imu_serial has no LED, and the cell is soldered on,
so it needs a meter in the cell's lead; D38 open: both sleep currents).
