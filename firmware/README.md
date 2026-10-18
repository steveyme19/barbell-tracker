# Firmware

Target: Seeed XIAO nRF52840 Sense, Arduino IDE. Board package **Seeed nRF52 Boards**
(non-mbed, D24), board "Seeed XIAO nRF52840 Sense", library "Seeed Arduino LSM6DS3".

| sketch | what |
|---|---|
| `imu_serial/` | IMU over USB at 208 Hz: `t_us,ax,ay,az,gx,gy,gz` per line (m/s^2, deg/s). Record it with `python analysis/log_serial.py COMx` |

## Ground rules (from the roadmap)

- One new thing at a time: blink → IMU over USB serial → BLE.
- Every sample gets a device timestamp. Every BLE packet gets a sequence counter.
- Sample timing comes from a timer, the IMU data-ready signal, or the IMU FIFO.
  Never `delay()`.
