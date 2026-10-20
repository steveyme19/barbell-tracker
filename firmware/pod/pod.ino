// pod: the bar tracker's device firmware (XIAO nRF52840 Sense, Seeed nRF52 non-mbed core).
//
// Streams the IMU like imu_serial (same CSV over USB, so analysis/log_serial.py works), plus
// power management so a battery lasts:
//   - auto-sleep after IDLE_SLEEP_S without motion; the IMU's wake-up detector (INT1 -> P0.11)
//     wakes it when the bar moves again
//   - sleep now: lay the pod lid-down (chip side down) and keep it still for FACE_DOWN_S
//   - low battery: below LOW_BATT_V it sleeps with the IMU off; only USB power or reset wakes it,
//     so the cell never gets run flat
//   - charging at 100 mA (FAST_CHARGE) instead of the default 50 mA
// System OFF is a reset: waking runs setup() again. Board facts (pins, divider) are in
// firmware/README.md.
//
// One line per IMU sample:  t_us,ax,ay,az,gx,gy,gz  (t_us from RTC2 on the 32.768 kHz crystal,
// D25; m/s^2 incl. gravity; deg/s). Lines starting with # are messages.

#include <Adafruit_TinyUSB.h>  // USB serial on the Seeed nRF52 core
#include <Wire.h>
#include <LSM6DS3.h>

// ---- settings ----
const uint16_t ODR_HZ = 208;           // IMU sample rate: 104, 208, 416 or 833
const uint32_t IDLE_SLEEP_S = 600;     // sleep after this long without motion (long rests between sets are fine)
const uint32_t FACE_DOWN_S = 3;        // lid-down and still this long -> sleep now
const float LOW_BATT_V = 3.30;         // below this (3 readings in a row, on battery) -> sleep for good
const bool FAST_CHARGE = true;         // true: 100 mA (P0.13 LOW), false: 50 mA (P0.13 floating)
const uint8_t WAKE_THS = 3;            // wake-up threshold, 31.25 mg per step at +-2 g (3 = ~0.9 m/s^2)

const float G = 9.80665;               // m/s^2 per g

// ---- IMU registers used directly (LSM6DS3TR-C datasheet) ----
const uint8_t REG_WHO_AM_I = 0x0F;
const uint8_t REG_CTRL1_XL = 0x10;
const uint8_t REG_CTRL2_G = 0x11;
const uint8_t REG_CTRL6_C = 0x15;      // bit 4 XL_HM_MODE: 1 = accel low-power (at <= 52 Hz)
const uint8_t REG_WAKE_UP_SRC = 0x1B;  // reading it clears a latched wake-up interrupt
const uint8_t REG_TAP_CFG = 0x58;      // bit 7 INTERRUPTS_ENABLE, bit 0 LIR (latch)
const uint8_t REG_WAKE_UP_THS = 0x5B;
const uint8_t REG_WAKE_UP_DUR = 0x5C;
const uint8_t REG_MD1_CFG = 0x5E;      // bit 5 INT1_WU: wake-up on INT1

// ---- battery: VBAT -1M- P0.31 -510k- P0.14. P0.14 LOW -> P0.31 = VBAT * 510/1510 ----
const float VBAT_DIV = 1510.0 / 510.0;
const float ADC_REF_V = 3.0;           // AR_INTERNAL_3_0
const uint8_t PIN_CHG = 23;            // P0.17 ~CHG: LOW while the BQ25101 is charging

LSM6DS3 imu(I2C_MODE, 0x6A);           // the Sense's built-in IMU (LSM6DS3TR-C)

uint32_t rtcWraps = 0, rtcLast = 0;    // RTC2 timestamp unwrapping (D25)
uint32_t lastMotionMs = 0;             // idle timer
uint32_t faceDownSinceMs = 0;          // 0 = not lid-down right now
float aRef[3] = {0, 0, G};             // slow average of the accel vector (motion = change from it)
uint32_t lastBattMs = 0;
uint8_t lowBattCount = 0;

// ---------------------------------------------------------------------------------------------

bool usbPowered() {
  return NRF_POWER->USBREGSTATUS & POWER_USBREGSTATUS_VBUSDETECT_Msk;
}

bool charging() {
  return digitalRead(PIN_CHG) == LOW;
}

float batteryVolts() {
  // The divider is ~340 kOhm seen from the ADC: it needs the long 40 us sample time, the
  // default 3 us reads low.
  analogReference(AR_INTERNAL_3_0);
  analogReadResolution(12);
  analogSampleTime(40);
  uint32_t sum = 0;
  for (int i = 0; i < 8; i++) sum += analogRead(PIN_VBAT);
  return sum / 8.0 * ADC_REF_V / 4096.0 * VBAT_DIV;
}

void blink(uint8_t led, int times) {
  // The XIAO's RGB LED is active LOW
  for (int i = 0; i < times; i++) {
    digitalWrite(led, LOW);  delay(80);
    digitalWrite(led, HIGH); delay(120);
  }
}

uint8_t readReg(uint8_t reg) {
  uint8_t v = 0;
  imu.readRegister(&v, reg);
  return v;
}

// Put the 2 MB QSPI flash into deep power-down (command 0xB9), as Seeed's low-power example
// does: otherwise it idles at standby current through System OFF. Bit-banged, it's one byte.
void flashDeepPowerDown() {
  const uint8_t SCK = PIN_QSPI_SCK, CS = PIN_QSPI_CS, SI = PIN_QSPI_IO0;
  pinMode(PIN_QSPI_IO2, OUTPUT); digitalWrite(PIN_QSPI_IO2, HIGH);  // WP off
  pinMode(PIN_QSPI_IO3, OUTPUT); digitalWrite(PIN_QSPI_IO3, HIGH);  // HOLD off
  pinMode(CS, OUTPUT);  digitalWrite(CS, HIGH);
  pinMode(SCK, OUTPUT); digitalWrite(SCK, LOW);
  pinMode(SI, OUTPUT);
  digitalWrite(CS, LOW);
  for (int bit = 7; bit >= 0; bit--) {           // SPI mode 0, MSB first
    digitalWrite(SI, (0xB9 >> bit) & 1);
    digitalWrite(SCK, HIGH);
    digitalWrite(SCK, LOW);
  }
  digitalWrite(CS, HIGH);                        // stays HIGH through System OFF (pins keep state)
  delayMicroseconds(10);
}

// Sleep (System OFF, about 1 uA for the nRF). motionWake: the IMU keeps watching at 26 Hz in
// low-power mode and its wake-up interrupt wakes us. Otherwise the IMU is powered off and only
// USB power or the reset button wakes the board.
void goToSleep(bool motionWake, const char* why) {
  Serial.print("# sleeping: ");
  Serial.println(why);
  Serial.flush();
  blink(motionWake ? LED_BLUE : LED_RED, 3);

  if (motionWake) {
    imu.writeRegister(REG_CTRL2_G, 0x00);                    // gyro off
    imu.writeRegister(REG_CTRL1_XL, 0x20);                   // accel 26 Hz, +-2 g
    imu.writeRegister(REG_CTRL6_C, readReg(REG_CTRL6_C) | 0x10);  // accel low-power mode
    imu.writeRegister(REG_WAKE_UP_DUR, 0x00);
    imu.writeRegister(REG_WAKE_UP_THS, WAKE_THS & 0x3F);
    imu.writeRegister(REG_TAP_CFG, 0x81);                    // interrupts on, latched
    imu.writeRegister(REG_MD1_CFG, 0x20);                    // wake-up -> INT1
    delay(200);                                              // let the filter settle,
    readReg(REG_WAKE_UP_SRC);                                // then clear anything it latched
  } else {
    imu.writeRegister(REG_CTRL1_XL, 0x00);
    imu.writeRegister(REG_CTRL2_G, 0x00);
    digitalWrite(PIN_LSM6DS3TR_C_POWER, LOW);                // IMU off completely
  }

  digitalWrite(LED_RED, HIGH); digitalWrite(LED_GREEN, HIGH); digitalWrite(LED_BLUE, HIGH);
  flashDeepPowerDown();
  // P0.14 stays LOW (battery safety, and the divider only costs VBAT / 1.51 MOhm ~ 2.8 uA).
  // P0.13 keeps its state too, so charging still runs at the chosen current while asleep.

  if (motionWake) {
    systemOff(PIN_LSM6DS3TR_C_INT1, HIGH);                   // core helper: sense HIGH, then OFF
  } else {
    uint8_t sd_en = 0;
    (void)sd_softdevice_is_enabled(&sd_en);
    if (sd_en) sd_power_system_off();
    else NRF_POWER->SYSTEMOFF = 1;
  }
  while (true) {}                                            // not reached
}

// ---------------------------------------------------------------------------------------------

void setup() {
  // Battery safety (CLAUDE.md): P0.14 must never be HIGH while charging, or P0.31 sees the
  // full battery voltage. LOW is the safe state (it also switches on the battery reading).
  pinMode(VBAT_ENABLE, OUTPUT);
  digitalWrite(VBAT_ENABLE, LOW);
  if (FAST_CHARGE) {
    pinMode(PIN_CHARGING_CURRENT, OUTPUT);
    digitalWrite(PIN_CHARGING_CURRENT, LOW);                 // 100 mA
  } else {
    pinMode(PIN_CHARGING_CURRENT, INPUT);                    // floating: 50 mA
  }
  pinMode(PIN_CHG, INPUT);
  pinMode(LED_RED, OUTPUT); pinMode(LED_GREEN, OUTPUT); pinMode(LED_BLUE, OUTPUT);
  digitalWrite(LED_RED, HIGH); digitalWrite(LED_GREEN, HIGH); digitalWrite(LED_BLUE, HIGH);

  uint32_t why = NRF_POWER->RESETREAS;                       // why did we start?
  NRF_POWER->RESETREAS = 0xFFFFFFFF;                         // (write 1s to clear)

  Serial.begin(115200);
  uint32_t t0 = millis();
  while (!Serial && millis() - t0 < 2000) delay(10);        // don't wait long: on battery there's no PC

  imu.settings.accelRange = 8;                               // decision O5: +-8 g, +-1000 deg/s
  imu.settings.gyroRange = 1000;
  imu.settings.accelSampleRate = ODR_HZ;
  imu.settings.gyroSampleRate = ODR_HZ;
  if (imu.begin() != 0) {
    while (true) {
      Serial.println("# IMU not found. Is this board the Sense version?");
      blink(LED_RED, 1);
      delay(1000);
    }
  }
  Wire1.setClock(400000);
  imu.writeRegister(0x12, readReg(0x12) | 0x40);             // CTRL3_C: block data update
  // Undo the sleep setup (the IMU kept power through System OFF, and begin() doesn't touch these)
  imu.writeRegister(REG_MD1_CFG, 0x00);
  imu.writeRegister(REG_TAP_CFG, 0x00);
  imu.writeRegister(REG_WAKE_UP_THS, 0x00);
  imu.writeRegister(REG_CTRL6_C, readReg(REG_CTRL6_C) & ~0x10);
  readReg(REG_WAKE_UP_SRC);

  // RTC2 at 32768 Hz for timestamps (RTC0 = SoftDevice, RTC1 = FreeRTOS)
  NRF_RTC2->TASKS_STOP = 1;
  NRF_RTC2->PRESCALER = 0;
  NRF_RTC2->TASKS_CLEAR = 1;
  NRF_RTC2->TASKS_START = 1;

  Serial.print("# pod firmware, IMU WHO_AM_I 0x");
  Serial.print(readReg(REG_WHO_AM_I), HEX);
  Serial.print(", woke by: ");
  if (why & POWER_RESETREAS_OFF_Msk) Serial.println("motion (from sleep)");
  else if (why & POWER_RESETREAS_VBUS_Msk) Serial.println("USB power (from sleep)");
  else if (why & POWER_RESETREAS_RESETPIN_Msk) Serial.println("reset button");
  else Serial.println("power on / upload");
  Serial.print("# battery ");
  Serial.print(batteryVolts(), 2);
  Serial.println(usbPowered() ? " V (on USB: with no battery fitted this is the charger, not a cell)" : " V");
  Serial.println("# t_us,ax,ay,az,gx,gy,gz");

  blink(LED_GREEN, 1);
  lastMotionMs = millis();
}

void loop() {
  // Wait for a new sample (status bit 0 = accel ready, bit 1 = gyro ready), staying in this
  // loop so each sample is timestamped within one status read (~0.1 ms) of being ready.
  uint8_t status;
  do {
    if (imu.readRegister(&status, LSM6DS3_ACC_GYRO_STATUS_REG) != 0) return;
  } while ((status & 0x03) != 0x03);

  uint32_t ticks = NRF_RTC2->COUNTER;                        // RTC2: 24 bits, unwrap it
  if (ticks < rtcLast) rtcWraps++;
  rtcLast = ticks;
  uint32_t t = (uint32_t)((((uint64_t)rtcWraps << 24) + ticks) * 15625 / 512);   // ticks -> us

  uint8_t b[12];                                             // gyro x, y, z then accel x, y, z
  if (imu.readRegisterRegion(b, LSM6DS3_ACC_GYRO_OUTX_L_G, 12) != 0) return;
  float a[3], g[3];
  for (int k = 0; k < 3; k++) {
    g[k] = imu.calcGyro((int16_t)(b[2 * k] | (b[2 * k + 1] << 8)));
    a[k] = imu.calcAccel((int16_t)(b[6 + 2 * k] | (b[7 + 2 * k] << 8))) * G;
  }

  if (Serial) {
    Serial.print(t);
    for (int k = 0; k < 3; k++) { Serial.print(','); Serial.print(a[k], 4); }
    for (int k = 0; k < 3; k++) { Serial.print(','); Serial.print(g[k], 3); }
    Serial.println();
  }

  // ---- motion: the accel vector moved away from its slow average, or it's turning ----
  uint32_t now = millis();
  float da = 0, gm = 0;
  for (int k = 0; k < 3; k++) {
    da += (a[k] - aRef[k]) * (a[k] - aRef[k]);
    gm += g[k] * g[k];
    aRef[k] += (a[k] - aRef[k]) * 0.005;                     // ~1 s time constant at 208 Hz
  }
  bool moving = da > 0.6 * 0.6 || gm > 15.0 * 15.0;          // 0.6 m/s^2, 15 deg/s
  if (moving) lastMotionMs = now;

  // ---- sleep now: lid-down (chip side down = -z up) and still ----
  if (a[2] < -8.0 && !moving) {
    if (faceDownSinceMs == 0) faceDownSinceMs = now;
    else if (now - faceDownSinceMs > FACE_DOWN_S * 1000) goToSleep(true, "lid-down gesture");
  } else {
    faceDownSinceMs = 0;
  }

  // ---- idle: sleep, but not on USB power (that's the bench / charging) ----
  if (now - lastMotionMs > IDLE_SLEEP_S * 1000 && !usbPowered()) goToSleep(true, "idle");

  // ---- battery, every 5 s ----
  if (now - lastBattMs > 5000) {
    lastBattMs = now;
    if (!usbPowered()) {
      float v = batteryVolts();
      lowBattCount = v < LOW_BATT_V ? lowBattCount + 1 : 0;
      if (lowBattCount >= 3) goToSleep(false, "low battery, charge me");
    } else {
      lowBattCount = 0;
    }
    if (Serial && now % 30000 < 5000) {                      // a status line about every 30 s
      Serial.print("# battery ");
      Serial.print(batteryVolts(), 2);
      Serial.print(" V, USB ");
      Serial.print(usbPowered() ? "yes" : "no");
      Serial.print(", charging ");
      Serial.println(charging() ? "yes" : "no");
    }
  }
}
