// imu_serial: stream the XIAO nRF52840 Sense's IMU over USB (roadmap 1.1, first bench test).
//
// One line per IMU sample:  t_us,ax,ay,az,gx,gy,gz
//   t_us      the XIAO's own clock in microseconds, never the PC's arrival time (D4)
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

  Serial.println("# t_us,ax,ay,az,gx,gy,gz");
}

void loop() {
  // Wait until the chip has a new sample (status bit 0 = accel ready, bit 1 = gyro ready).
  // Polling like this sends every sample exactly once, at the chip's own rate.
  uint8_t status;
  if (imu.readRegister(&status, LSM6DS3_ACC_GYRO_STATUS_REG) != 0) return;
  if ((status & 0x03) != 0x03) return;
  uint32_t t = micros();

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
