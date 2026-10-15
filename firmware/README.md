# Firmware

Nothing here yet. Starts at roadmap phase 1, once the board shows up.

Target: Seeed XIAO nRF52840 Sense, Arduino IDE.

## Ground rules (from the roadmap)

- One new thing at a time: blink → IMU over USB serial → BLE.
- Every sample gets a device timestamp. Every BLE packet gets a sequence counter.
- Sample timing comes from a timer, the IMU data-ready signal, or the IMU FIFO.
  Never `delay()`.
