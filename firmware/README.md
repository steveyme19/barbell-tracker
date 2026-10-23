# Firmware

Target: Seeed XIAO nRF52840 Sense, Arduino IDE. Board package **Seeed nRF52 Boards**
(non-mbed, D24), board "Seeed XIAO nRF52840 Sense", library "Seeed Arduino LSM6DS3".

| sketch | what |
|---|---|
| `pod/` | **The device firmware.** Same stream as `imu_serial` plus power management (D26): auto-sleep after 10 min still on battery, wake on motion via the IMU's wake-up interrupt; lid-down and still for 6 s = sleep now (only when no PC has the port open); below 3.30 V on battery it sleeps with the IMU off until USB or reset; 100 mA charging; a battery/charging status line about every 30 s. Bench commands over USB: `s` sleep, `o` sleep with the IMU off, `w` dry run of `s`, `i` stop the IMU's sampling (tests the restart path). Keeps an 8192-sample ring (~38 s) for the BLE link (roadmap 1.4, `docs/ble-plan.md`); `PKT_OVER_USB = true` also prints its frames over USB for `analysis/podlink.py --usb`. |
| `imu_serial/` | IMU over USB at "208 Hz" (really 209 to 212, per board): `t_us,ax,ay,az,gx,gy,gz` per line (m/s^2, deg/s), `t_us` from RTC2 on the crystal (D25). Record it with `python analysis/log_serial.py COMx`, watch it live with `python analysis/live_plot.py` |

## Flashing from the terminal

**Use `python firmware/flash.py [sketch] [port]`** (default `pod`, port found by USB ID). It first
checks the installed core still drives P0.14 LOW at boot (the patch below) and refuses to build
if a core update undid it, then compiles and uploads. The raw arduino-cli command still works:

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
| bootloader / stuck upload | | reset once; if that fails double-tap reset; then replug |

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
- Wake threshold from data: real pickups (9 recordings, decimated to the IMU's 12.5 Hz sleep
  rate) peak at 114 to 437 mg on the slope filter, so `WAKE_THS` 2 (62.5 mg, 1.8x margin on the
  gentlest) instead of 3 (1.2x). Dry runs at 2: 5/5 quiet.
- `woke by` uses the core's `readResetReason()`: the core's `init()` saves RESETREAS and clears
  it before `setup()`.
- Not measured yet (no battery wired, no meter): sleep current, battery reading accuracy, the
  low-battery cutoff on a real cell. Awake, the tight data-ready poll keeps the CPU busy
  (~6 mA more than needed): moving data-ready to an interrupt is part of the BLE rework.
- **P0.14 at boot (patched 2026-10-19):** Seeed's `initVariant()` drove it HIGH on every boot
  and wake until `setup()` set it LOW, a few ms (P0.31 up to ~3.6 V through 1 MOhm, < 1 uA).
  The installed core is patched to drive it LOW: `%LOCALAPPDATA%\Arduino15\packages\Seeeduino\hardware\nrf52\1.1.13\variants\Seeed_XIAO_nRF52840_Sense\variant.cpp`
  (original kept as `variant.cpp.orig`). `flash.py` refuses to build if a core update reverts
  it; redo the one-line change after updating the core. Checked: the guard accepts the patched
  core and refuses Seeed's original.
