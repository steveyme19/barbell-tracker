// pod: the bar tracker's device firmware (XIAO nRF52840 Sense, Seeed nRF52 non-mbed core).
//
// Streams the IMU like imu_serial (same CSV over USB, so analysis/log_serial.py works), plus
// power management so a battery lasts:
//   - auto-sleep after IDLE_SLEEP_S without motion (not on USB power); the IMU's wake-up
//     detector (INT1 -> P0.11) wakes it when the bar moves again
//   - sleep now: lay the pod lid-down (chip side down), still, for FACE_DOWN_S. Only when no
//     PC has the serial port open, so it can't end a recording or the chip-down calibration face
//   - low battery: below LOW_BATT_V it sleeps with the IMU off; only USB power or reset wakes it,
//     so the cell never gets run flat
//   - charging at 100 mA (FAST_CHARGE) instead of the default 50 mA
// System OFF is a reset: waking runs setup() again. Board facts (pins, divider) and the bench
// results are in firmware/README.md.
//
// One line per IMU sample:  t_us,ax,ay,az,gx,gy,gz  (t_us from RTC2 on the 32.768 kHz crystal,
// D25; m/s^2 incl. gravity; deg/s). Lines starting with # are messages.
// Bench commands over USB: s = sleep (wake on motion), o = sleep with the IMU off,
// w = dry run of s (no System OFF; reports whether INT1 fires falsely).

#include <Adafruit_TinyUSB.h>  // USB serial on the Seeed nRF52 core
#include <Wire.h>
#include <LSM6DS3.h>

// ---- settings ----
const uint16_t ODR_HZ = 208;           // IMU sample rate: 104, 208, 416 or 833
const uint32_t IDLE_SLEEP_S = 600;     // sleep after this long without motion (long rests between sets are fine)
const uint32_t FACE_DOWN_S = 6;        // lid-down and still this long -> sleep now (long: a rolled
                                       // bar on the shaft saddle can leave the pod lid-down between sets)
const float LOW_BATT_V = 3.30;         // below this (3 readings in a row, on battery) -> sleep for good
const bool FAST_CHARGE = true;         // true: 100 mA (P0.13 LOW), false: 50 mA (P0.13 floating)
const uint8_t WAKE_THS = 2;            // wake-up threshold, 31.25 mg per step at +-2 g. It acts on the slope
                                       // (a[n] - a[n-1]) / 2 at 12.5 Hz, i.e. on jerk, not on acceleration.
                                       // Real pickups (9 recordings) peak at 114 to 437 mg: 2 = 62.5 mg gives
                                       // 1.8x margin on the gentlest, 3 would give only 1.2x.

const float G = 9.80665;               // m/s^2 per g

// ---- IMU registers used directly (LSM6DS3TR-C datasheet) ----
const uint8_t REG_WHO_AM_I = 0x0F;
const uint8_t REG_CTRL1_XL = 0x10;
const uint8_t REG_CTRL2_G = 0x11;
const uint8_t REG_CTRL3_C = 0x12;      // bit 6 BDU
const uint8_t REG_CTRL6_C = 0x15;      // bit 4 XL_HM_MODE: 1 = accel low-power (at <= 52 Hz)
const uint8_t REG_WAKE_UP_SRC = 0x1B;  // reading it clears a latched wake-up interrupt
const uint8_t REG_TAP_CFG = 0x58;      // bit 7 INTERRUPTS_ENABLE, bit 0 LIR (latch)
const uint8_t REG_WAKE_UP_THS = 0x5B;
const uint8_t REG_WAKE_UP_DUR = 0x5C;
const uint8_t REG_MD1_CFG = 0x5E;      // bit 5 INT1_WU: wake-up on INT1

// ---- board pins (variant numbers; firmware/README.md "Board facts") ----
const uint8_t PIN_CHG = 23;            // P0.17 ~CHG: LOW while the BQ25101 is charging
const uint8_t PIN_IMU_SCL = 16;        // P0.27, the IMU's own I2C bus (Wire1)
const uint8_t PIN_IMU_SDA = 17;        // P0.07

// ---- battery: VBAT -1M- P0.31 -510k- P0.14. P0.14 LOW -> P0.31 = VBAT * 510/1510 ----
const float VBAT_DIV = 1510.0 / 510.0;
const float ADC_REF_V = 3.0;           // AR_INTERNAL_3_0

LSM6DS3 imu(I2C_MODE, 0x6A);           // the Sense's built-in IMU (LSM6DS3TR-C)

uint32_t rtcWraps = 0, rtcLast = 0;    // RTC2 timestamp unwrapping (D25)
uint32_t lastMotionMs = 0;             // idle timer
uint32_t faceDownSinceMs = 0;          // 0 = not lid-down right now
float aRef[3];                         // slow average of the accel vector (motion = change from it)
bool aRefSet = false;                  // seeded from the first sample
uint32_t lastBattMs = 0;
float lastBattV = 0;
uint8_t lowBattCount = 0;
uint16_t imuMisses = 0;                // data-ready waits that timed out in a row

// ---------------------------------------------------------------------------------------------

bool usbPowered() {
  return NRF_POWER->USBREGSTATUS & POWER_USBREGSTATUS_VBUSDETECT_Msk;
}

bool charging() {
  return digitalRead(PIN_CHG) == LOW;
}

// A pin back to its reset state: input buffer disconnected, no pull. Nothing leaks through it.
void pinDisconnect(uint8_t pin) {
  uint32_t p = g_ADigitalPinMap[pin];
  NRF_GPIO_Type* port = p >= 32 ? NRF_P1 : NRF_P0;
  port->PIN_CNF[p & 31] = GPIO_PIN_CNF_INPUT_Disconnect << GPIO_PIN_CNF_INPUT_Pos;
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

// Power-cycle the IMU and start it: always from a known state, whatever ran before (an older
// sketch, our own sleep setup, a wedged bus). Returns false if it never answers.
bool imuStart() {
  for (int attempt = 0; attempt < 3; attempt++) {
    pinMode(PIN_LSM6DS3TR_C_POWER, OUTPUT);
    digitalWrite(PIN_LSM6DS3TR_C_POWER, LOW);
    delay(20);
    digitalWrite(PIN_LSM6DS3TR_C_POWER, HIGH);
    delay(40);                                               // boot time after power-up
    imu.settings.accelRange = 8;                             // decision O5: +-8 g, +-1000 deg/s
    imu.settings.gyroRange = 1000;
    imu.settings.accelSampleRate = ODR_HZ;
    imu.settings.gyroSampleRate = ODR_HZ;
    if (imu.begin() == 0) {
      Wire1.setClock(400000);
      imu.writeRegister(REG_CTRL3_C, readReg(REG_CTRL3_C) | 0x40);   // block data update
      return true;
    }
  }
  return false;
}

// Put the 2 MB QSPI flash into deep power-down (command 0xB9), as Seeed's low-power example
// does: otherwise it idles at standby current through System OFF. Bit-banged, it's one byte.
// (Nothing uses the flash yet; if something ever does, send 0xAB first to wake it.)
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

// Sleep (System OFF). motionWake: the IMU keeps watching at 12.5 Hz in low-power mode (~9 uA)
// and its wake-up interrupt wakes us. Otherwise the IMU is powered off and only USB power or the
// reset button wakes the board. dryRun (bench command w): everything but System OFF.
void goToSleep(bool motionWake, const char* why, bool dryRun = false) {
  Serial.print("# sleeping: ");
  Serial.println(why);
  Serial.flush();
  blink(motionWake ? LED_BLUE : LED_RED, 3);

  if (motionWake) {
    imu.writeRegister(REG_CTRL2_G, 0x00);                    // gyro off
    imu.writeRegister(REG_CTRL1_XL, 0x10);                   // accel 12.5 Hz, +-2 g
    imu.writeRegister(REG_CTRL6_C, readReg(REG_CTRL6_C) | 0x10);  // accel low-power mode
    imu.writeRegister(REG_WAKE_UP_DUR, 0x00);
    imu.writeRegister(REG_WAKE_UP_THS, WAKE_THS & 0x3F);
    imu.writeRegister(REG_TAP_CFG, 0x81);                    // interrupts on, latched
    imu.writeRegister(REG_MD1_CFG, 0x20);                    // wake-up -> INT1
    // Bench: entering System OFF right after clearing the latch woke instantly 4 times out of
    // 4; with time in between it slept 2 out of 2 (root cause not pinned down). So: let the
    // filter settle ~10 samples, then clear until INT1 stays low for 2 samples.
    delay(800);
    for (int i = 0; i < 10; i++) {
      readReg(REG_WAKE_UP_SRC);                              // reading clears the latch
      delay(170);                                            // 2 samples at 12.5 Hz
      if (digitalRead(PIN_LSM6DS3TR_C_INT1) == LOW) break;
    }
  } else {
    imu.writeRegister(REG_CTRL1_XL, 0x00);
    imu.writeRegister(REG_CTRL2_G, 0x00);
    // Before the IMU loses power: release its I2C pins. Wire leaves the nRF's internal pull-ups
    // on SDA/SCL, which would feed ~0.3 mA into the dead IMU rail (its pull-ups hang off it)
    // and run the cell flat in days, the exact thing this sleep is for.
    Wire1.end();
    pinDisconnect(PIN_IMU_SDA);
    pinDisconnect(PIN_IMU_SCL);
    pinDisconnect(PIN_LSM6DS3TR_C_INT1);
    digitalWrite(PIN_LSM6DS3TR_C_POWER, LOW);                // IMU off completely
    delay(100);                                              // let USB finish the message
  }

  if (dryRun) {
    // Watch INT1 for 10 s instead of switching off: a false wake-up event shows up as INT1
    // going high while the board sits still.
    uint32_t t0 = millis();
    while (millis() - t0 < 10000 && digitalRead(PIN_LSM6DS3TR_C_INT1) == LOW) {}
    Serial.print(digitalRead(PIN_LSM6DS3TR_C_INT1) ? "# dry run: INT1 fired after " : "# dry run: INT1 quiet for ");
    Serial.print(millis() - t0);
    Serial.println(" ms");
    Serial.flush();
    delay(100);
    NVIC_SystemReset();                                      // start fresh, like a real wake
  }

  digitalWrite(LED_RED, HIGH); digitalWrite(LED_GREEN, HIGH); digitalWrite(LED_BLUE, HIGH);
  pinDisconnect(PIN_CHG);                                    // its pull-up would draw while charging
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

// Idle sleep, battery cutoff, status line. Runs on every pass of loop(), so a stuck IMU or I2C
// bus can't stop the battery protection.
void housekeeping(uint32_t now) {
  if (now - lastMotionMs > IDLE_SLEEP_S * 1000 && !usbPowered()) goToSleep(true, "idle");

  if (now - lastBattMs > 5000) {
    lastBattMs = now;
    lastBattV = batteryVolts();
    if (!usbPowered()) {
      lowBattCount = lastBattV < LOW_BATT_V ? lowBattCount + 1 : 0;
      if (lowBattCount >= 3) goToSleep(false, "low battery, charge me");
    } else {
      lowBattCount = 0;
    }
    if (Serial && now % 30000 < 5000) {                      // a status line about every 30 s
      Serial.print("# battery ");
      Serial.print(lastBattV, 2);
      Serial.print(" V, USB ");
      Serial.print(usbPowered() ? "yes" : "no");
      Serial.print(", charging ");
      Serial.println(charging() ? "yes" : "no");
    }
  }
}

// ---------------------------------------------------------------------------------------------

void setup() {
  // Battery safety (CLAUDE.md): P0.14 must never be HIGH while charging, or P0.31 sees the
  // full battery voltage. LOW is the safe state (it also switches on the battery reading).
  // Note: the core's initVariant() drives it HIGH for a few ms before setup() runs
  // (firmware/README.md); from here on it stays LOW, also through sleep.
  pinMode(VBAT_ENABLE, OUTPUT);
  digitalWrite(VBAT_ENABLE, LOW);
  if (FAST_CHARGE) {
    pinMode(PIN_CHARGING_CURRENT, OUTPUT);
    digitalWrite(PIN_CHARGING_CURRENT, LOW);                 // 100 mA
  } else {
    pinMode(PIN_CHARGING_CURRENT, INPUT);                    // floating: 50 mA
  }
  pinMode(PIN_CHG, INPUT_PULLUP);                            // open drain; idles mid-rail without it
  pinMode(LED_RED, OUTPUT); pinMode(LED_GREEN, OUTPUT); pinMode(LED_BLUE, OUTPUT);
  digitalWrite(LED_RED, HIGH); digitalWrite(LED_GREEN, HIGH); digitalWrite(LED_BLUE, HIGH);

  // Why did we start? The core's init() saves RESETREAS and clears it before setup(), so ask
  // the core (reading the register here would always say "power on").
  uint32_t why = readResetReason();
  bool fromSleep = why & (POWER_RESETREAS_OFF_Msk | POWER_RESETREAS_VBUS_Msk);

  Serial.begin(115200);
  uint32_t t0 = millis();
  while (!Serial && usbPowered() && millis() - t0 < 2000) delay(10);   // no PC on battery

  if (!imuStart()) {
    // On battery, don't sit here awake: sleep with the IMU off (USB or reset wakes it)
    if (!usbPowered()) goToSleep(false, "IMU not found");
    while (true) {
      Serial.println("# IMU not found. Is this board the Sense version?");
      blink(LED_RED, 1);
      delay(1000);
    }
  }

  // RTC2 at 32768 Hz for timestamps (RTC0 = SoftDevice, RTC1 = FreeRTOS)
  NRF_RTC2->TASKS_STOP = 1;
  NRF_RTC2->PRESCALER = 0;
  NRF_RTC2->TASKS_CLEAR = 1;
  NRF_RTC2->TASKS_START = 1;

  analogCalibrateOffset();                                   // SAADC offset, once
  lastBattV = batteryVolts();

  Serial.print("# pod firmware, IMU WHO_AM_I 0x");
  Serial.print(readReg(REG_WHO_AM_I), HEX);
  Serial.print(", woke by: ");
  if (why & POWER_RESETREAS_OFF_Msk) Serial.println("motion (from sleep)");
  else if (why & POWER_RESETREAS_VBUS_Msk) Serial.println("USB power (from sleep)");
  else if (why & POWER_RESETREAS_RESETPIN_Msk) Serial.println("reset button");
  else Serial.println("power on / upload");
  Serial.print("# RESETREAS 0x");
  Serial.println(why, HEX);
  Serial.print("# battery ");
  Serial.print(lastBattV, 2);
  Serial.println(usbPowered() ? " V (on USB: with no battery fitted this is the charger, not a cell)" : " V");
  Serial.println("# t_us,ax,ay,az,gx,gy,gz");

  if (!fromSleep) blink(LED_GREEN, 1);                       // a wake shouldn't cost 200 ms of data
  lastMotionMs = millis();
  lastBattMs = millis();
}

void loop() {
  uint32_t now = millis();

  if (Serial && Serial.available()) {                        // bench commands (top of file)
    char c = Serial.read();
    if (c == 's') goToSleep(true, "command s");
    if (c == 'o') goToSleep(false, "command o");
    if (c == 'w') goToSleep(true, "command w (dry run)", true);
  }

  housekeeping(now);

  // Wait for a new sample (status bit 0 = accel ready, bit 1 = gyro ready), staying in this
  // loop so each sample is timestamped within one status read (~0.1 ms) of being ready. Give up
  // after 20 ms (4 sample periods) so a stuck IMU can't freeze everything; restart it after 1 s
  // of nothing.
  uint8_t status = 0;
  uint32_t w0 = millis();
  do {
    if (imu.readRegister(&status, LSM6DS3_ACC_GYRO_STATUS_REG) != 0) status = 0;
  } while ((status & 0x03) != 0x03 && millis() - w0 < 20);
  if ((status & 0x03) != 0x03) {
    if (++imuMisses >= 50) {
      Serial.println("# IMU stopped answering, restarting it");
      imuMisses = 0;
      if (!imuStart() && !usbPowered()) goToSleep(false, "IMU lost");
    }
    return;
  }
  imuMisses = 0;

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

  // ---- motion (idle timer): the accel vector moved away from its slow average, or turning ----
  if (!aRefSet) { for (int k = 0; k < 3; k++) aRef[k] = a[k]; aRefSet = true; }
  float da = 0, gm = 0, am = 0;
  for (int k = 0; k < 3; k++) {
    da += (a[k] - aRef[k]) * (a[k] - aRef[k]);
    gm += g[k] * g[k];
    am += a[k] * a[k];
    aRef[k] += (a[k] - aRef[k]) * 0.005;                     // ~1 s time constant at 208 Hz
  }
  bool moving = da > 0.6 * 0.6 || gm > 15.0 * 15.0;          // 0.6 m/s^2, 15 deg/s
  if (moving) lastMotionMs = now;

  // ---- sleep now: lid-down (chip side down = -z up, within ~27 deg) and still ----
  // "Still" here doesn't use aRef, so it holds right after the flip. Not while a PC has the
  // port open: that's a recording (or the chip-down calibration face) in progress.
  bool still = gm < 15.0 * 15.0 && fabsf(sqrtf(am) - G) < 1.0;
  if (a[2] < -9.0 && still && !Serial) {
    if (faceDownSinceMs == 0) faceDownSinceMs = now;
    else if (now - faceDownSinceMs > FACE_DOWN_S * 1000) goToSleep(true, "lid-down gesture");
  } else {
    faceDownSinceMs = 0;
  }
}
