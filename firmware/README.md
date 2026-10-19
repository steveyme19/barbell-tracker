# Firmware

Target: Seeed XIAO nRF52840 Sense, Arduino IDE. Board package **Seeed nRF52 Boards**
(non-mbed, D24), board "Seeed XIAO nRF52840 Sense", library "Seeed Arduino LSM6DS3".

| sketch | what |
|---|---|
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
