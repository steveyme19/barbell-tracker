# Firmware

Target: Seeed XIAO nRF52840 Sense, Arduino IDE. Board package **Seeed nRF52 Boards**
(non-mbed, D24), board "Seeed XIAO nRF52840 Sense", library "Seeed Arduino LSM6DS3".

| sketch | what |
|---|---|
| `pod/` | **The device firmware.** Same stream as `imu_serial` plus power management: auto-sleep after 10 min still (wake on motion via the IMU's wake-up interrupt), lid-down for 3 s = sleep now, low-battery cutoff at 3.30 V (sleeps with the IMU off until USB or reset), 100 mA charging, battery/charging status lines every ~30 s. BLE comes next (roadmap 1.4). |
| `imu_serial/` | IMU over USB at "208 Hz" (really 209 to 212, per board): `t_us,ax,ay,az,gx,gy,gz` per line (m/s^2, deg/s), `t_us` from RTC2 on the crystal (D25). Record it with `python analysis/log_serial.py COMx`, watch it live with `python analysis/live_plot.py` |

## Flashing from the terminal

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
