// imu_serial: stream the XIAO nRF52840 Sense's IMU over USB (roadmap 1.1, first bench test).
//
// One line per IMU sample:  t_us,ax,ay,az,gx,gy,gz
//   t_us      the XIAO's 32.768 kHz crystal (RTC2) in microseconds, 30.5 us steps, read the
//             moment each sample is ready; never the PC's arrival time (D4)
//   ax ay az  m/s^2, including gravity
//   gx gy gz  deg/s
// Lines starting with # are messages, not data. analysis/log_serial.py saves the stream
// as a CSV that reps.py and the app can read.
//
// Arduino IDE: board package "Seeed nRF52 Boards" (not the mbed one), board
// "Seeed XIAO nRF52840 Sense", library "Seeed Arduino LSM6DS3".

#include <Adafruit_TinyUSB.h>  // USB serial on the Seeed nRF52 core
#include <Wire.h>
#include <LSM6DS3.h>

const uint16_t ODR_HZ = 208;   // IMU sample rate, on the chip's own clock: 104, 208, 416 or 833
const float G = 9.80665;       // m/s^2 per g

const uint8_t REG_WHO_AM_I = 0x0F;   // 0x6A = LSM6DS3TR-C (the Sense), 0x69 = plain LSM6DS3

// Clock: micros() on this core is the 1024 Hz RTOS tick (977 us steps), and the IMU's own
// timestamp counter runs ~2.8% off real time (measured, D25). RTC2 is a spare counter on
// the 32.768 kHz crystal (RTC0 = SoftDevice, RTC1 = FreeRTOS): 30.5 us steps, ~20 ppm.
uint32_t rtcWraps = 0;   // RTC2 counts 24 bits (wraps every 512 s); count the wraps
uint32_t rtcLast = 0;

LSM6DS3 imu(I2C_MODE, 0x6A);   // the Sense's built-in IMU (LSM6DS3TR-C)

void setup() {
  // Battery safety (CLAUDE.md): P0.14 must never be HIGH while charging, or P0.31 sees the
  // full battery voltage. LOW is the safe state (it also switches on the battery reading).
  pinMode(VBAT_ENABLE, OUTPUT);
  digitalWrite(VBAT_ENABLE, LOW);

  Serial.begin(115200);
  uint32_t t0 = millis();
  while (!Serial && millis() - t0 < 5000) delay(10);   // wait up to 5 s for the PC to connect

  // Ranges (decision O5): accel +-8 g, gyro +-1000 deg/s, both sampling at ODR_HZ.
  imu.settings.accelRange = 8;
  imu.settings.gyroRange = 1000;
  imu.settings.accelSampleRate = ODR_HZ;
  imu.settings.gyroSampleRate = ODR_HZ;
  if (imu.begin() != 0) {
    while (true) {
      Serial.println("# IMU not found. Is this board the Sense version?");
      delay(1000);
    }
  }
  Wire1.setClock(400000);   // the IMU's own I2C bus: 400 kHz, so one read takes ~0.4 ms

  // Block data update: the chip can't change a reading while we're halfway through it.
  uint8_t ctrl3;
  imu.readRegister(&ctrl3, LSM6DS3_ACC_GYRO_CTRL3_C);
  imu.writeRegister(LSM6DS3_ACC_GYRO_CTRL3_C, ctrl3 | 0x40);

  // Start RTC2 at the full 32768 Hz (prescaler 0). The low-frequency clock is already
  // running on the crystal (FreeRTOS uses it), so this just starts the counter.
  NRF_RTC2->TASKS_STOP = 1;
  NRF_RTC2->PRESCALER = 0;
  NRF_RTC2->TASKS_CLEAR = 1;
  NRF_RTC2->TASKS_START = 1;

  uint8_t who = 0;
  imu.readRegister(&who, REG_WHO_AM_I);
  Serial.print("# IMU WHO_AM_I 0x");
  Serial.print(who, HEX);
  Serial.println(", timestamps from RTC2 (32.768 kHz crystal)");

  Serial.println("# t_us,ax,ay,az,gx,gy,gz");
}

void loop() {
  // Wait until the chip has a new sample (status bit 0 = accel ready, bit 1 = gyro ready).
  // Polling sends every sample exactly once, at the chip's own rate. Stay in this loop
  // while waiting, so each sample is timestamped within one status read (~0.1 ms).
  uint8_t status;
  do {
    if (imu.readRegister(&status, LSM6DS3_ACC_GYRO_STATUS_REG) != 0) return;
  } while ((status & 0x03) != 0x03);

  // Timestamp right away, the moment the sample shows up. RTC2 is 24 bits: unwrap it,
  // then ticks -> us (1e6 / 32768 = 15625 / 512), in 64 bits so nothing overflows.
  uint32_t ticks = NRF_RTC2->COUNTER;
  if (ticks < rtcLast) rtcWraps++;
  rtcLast = ticks;
  uint64_t total = ((uint64_t)rtcWraps << 24) + ticks;
  uint32_t t = (uint32_t)(total * 15625 / 512);   // wraps after ~71 min, like micros()

  // One burst read of 12 bytes: gyro x, y, z then accel x, y, z, low byte first.
  uint8_t b[12];
  if (imu.readRegisterRegion(b, LSM6DS3_ACC_GYRO_OUTX_L_G, 12) != 0) return;
  int16_t raw[6];
  for (int k = 0; k < 6; k++) raw[k] = (int16_t)(b[2 * k] | (b[2 * k + 1] << 8));

  Serial.print(t);
  for (int k = 3; k < 6; k++) { Serial.print(','); Serial.print(imu.calcAccel(raw[k]) * G, 4); }
  for (int k = 0; k < 3; k++) { Serial.print(','); Serial.print(imu.calcGyro(raw[k]), 3); }
  Serial.println();
}
