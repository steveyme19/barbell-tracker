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
//   - Bluetooth (ble.ino): the PodLink v1 service, docs/ble.md. Every sample also goes into a
//     38 s ring, and the phone streams from it and resumes after a dropout without losing samples
//   - sampling (sampler.ino): the IMU's data-ready interrupt wakes a top-priority task that
//     stamps, reads and stores each sample; loop() only sends (USB CSV, Bluetooth) from the ring,
//     so neither the radio nor a stuck USB host can delay or drop a sample
//   - a watchdog: if loop() stops for 8 s (a hung I2C bus, a bug), the board resets
// System OFF is a reset: waking runs setup() again. Board facts (pins, divider) and the bench
// results are in firmware/README.md.
//
// One line per IMU sample:  t_us,ax,ay,az,gx,gy,gz  (t_us from RTC2 on the 32.768 kHz crystal,
// D25; m/s^2 incl. gravity; deg/s). Lines starting with # are messages.
// Bench commands over USB: s = sleep (wake on motion), o = sleep with the IMU off,
// w = dry run of s (no System OFF; reports whether INT1 fires falsely), i = stop the IMU's
// sampling (no data-ready; after ~1 s the sampler restarts it: tests IMU_RESTART),
// h = hang loop() (tests the watchdog: a reset after 8 s).

#include <Adafruit_TinyUSB.h>  // USB serial on the Seeed nRF52 core
#include <Wire.h>
#include <LSM6DS3.h>
#include <bluefruit.h>   // Bluetooth (Bluefruit52Lib, ships with the core)
#include "podlink.h"   // PodLink v1 frames and UUIDs (docs/ble.md)

// ---- settings ----
const uint16_t ODR_HZ = 208;           // IMU sample rate: 104, 208, 416 or 833
const uint32_t IDLE_SLEEP_S = 600;     // sleep after this long without motion (long rests between sets are fine)
const uint32_t IDLE_SLEEP_STREAMING_S = 1800;   // the same while a phone holds a session (docs/ble.md 6)
const uint32_t FACE_DOWN_S = 6;        // lid-down and still this long -> sleep now (long: a rolled
                                       // bar on the shaft saddle can leave the pod lid-down between sets)
const float LOW_BATT_V = 3.30;         // below this (3 readings in a row, on battery) -> sleep for good
const bool FAST_CHARGE = true;         // true: 100 mA (P0.13 LOW), false: 50 mA (P0.13 floating)
const uint8_t WAKE_THS = 2;            // wake-up threshold, 31.25 mg per step at +-2 g. It acts on the slope
                                       // (a[n] - a[n-1]) / 2 at 12.5 Hz, i.e. on jerk, not on acceleration.
                                       // Real pickups (9 recordings) peak at 114 to 437 mg: 2 = 62.5 mg gives
                                       // 1.8x margin on the gentlest, 3 would give only 1.2x.

const float G = 9.80665;               // m/s^2 per g
const uint16_t FW_BUILD = 8;           // INFO fw_build: the BLE plan step this firmware is from

// ---- IMU registers used directly (LSM6DS3TR-C datasheet) ----
const uint8_t REG_DRDY_PULSE_CFG_G = 0x0B;   // bit 7 DRDY_PULSED: data-ready as a ~75 us pulse
                                            // (LSM6DS3TR-C; Seeed's header has the plain LSM6DS3 name)
const uint8_t REG_INT1_CTRL = 0x0D;    // bit 1 INT1_DRDY_G: gyro data-ready on INT1
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

// ---- PodLink ring (docs/ble.md, docs/ble-plan.md step 4) ----
const bool PKT_OVER_USB = false;       // step 4 bench test: DATA / STATUS / INFO frames as P / S / I
                                       // hex lines next to the CSV (analysis/podlink.py --usb checks them)
const uint32_t RING_N = 8192;          // samples, a power of two: 128 KiB, ~38.8 s at 211 Hz
const uint32_t RING_MASK = RING_N - 1;
PodSample ring[RING_N];
volatile uint32_t head = 0;            // samples stored since boot = the next index. Only the sampler
                                       // task writes it, after the slot; readers copy it once per pass
uint32_t restartIdx[40];               // first index after each recent IMU restart
uint8_t nRestart = 0;                  // how many of restartIdx are used (oldest dropped first)
uint32_t bootId = 0;                   // random per boot (INFO)
uint8_t wakeReason = 0;                // INFO: 0 power-on / upload, 1 motion, 2 USB, 3 reset pin
uint32_t wakeLatch0 = 0, wakeLatch1 = 0;   // GPIO LATCH at boot: the pins that met their SENSE level
uint32_t usbCursor = 0;                // next index the USB packer sends
uint16_t usbPktSeq = 0;
uint8_t usbFlags = 0;                  // RESYNC / OVERFLOW pending for the next USB frame
uint32_t usbLost = 0;                  // USB-side loss: only the '# ring' line, never STATUS
uint32_t lostBle = 0;                  // STATUS lost_overflow: the BLE stream's loss (step 5 on)
bool sessionHold = false;              // a phone's STREAM command until STOP: long idle limit, no lid-down
struct CtrlMsg { uint8_t len; uint8_t b[16]; };   // one CTRL write, queued from Bluefruit's task (ble.ino)
const float LOW_BATT_WARN_V = 3.50;    // STATUS batt_low below this, on battery power only
uint32_t lastRingLineMs = 0;
bool usbWasOpen = false;
uint32_t lastPodStatusMs = 0, lastPodInfoMs = 0;
volatile uint32_t lastMotionMs = 0;    // idle timer (the sampler sets it)
uint32_t lastBattMs = 0;
float lastBattV = 0;
uint8_t lowBattCount = 0;

// ---- shared with the sampler task (sampler.ino; declared here because Arduino joins the tabs
// after this one). Written there, read by loop() and ble.ino, or the other way round. ----
volatile uint8_t sleepReq = 0;         // a sleep the sampler asks loop() for (STATUS sleep_reason codes)
volatile bool imuStopReq = false;      // bench command i: the sampler stops the IMU (tests restarts)
volatile bool usbOpen = false;         // loop()'s (bool)Serial: the sampler must not touch Serial
volatile uint32_t missedDrdy = 0;      // samples the IMU produced that never reached the ring (STATUS)
volatile uint32_t lateRead = 0;        // of those: a newer sample arrived while one was being read
volatile uint32_t xlLag = 0;           // the accel's ready bit came after the gyro's pulse
volatile uint32_t readErrors = 0;      // IMU reads that failed (NACK or timeout)
volatile uint32_t imuRestarts = 0;
volatile uint32_t holes = 0;           // stored samples > 1.5 periods apart (a cross-check of missedDrdy)
volatile uint32_t maxLatency = 0;      // RTC2 ticks from a data-ready pulse to its sample stored
const uint8_t SLEEP_LID_DOWN = 2, SLEEP_IMU_LOST = 5;   // STATUS sleep_reason codes (docs/ble.md 3)

uint32_t csvCursor = 0;                // next ring index the USB CSV prints
bool csvWasOpen = false;
uint32_t csvLost = 0;                  // CSV lines the ring overwrote before USB took them (a slow host)
uint32_t seenRestarts = 0;

// A '#' line for USB, sent whole or not at all: a host that holds the port open without reading
// must never block loop() (Serial.write would wait for it forever).
class MsgLine : public Print {
 public:
  size_t write(uint8_t c) override {
    if (n < sizeof(buf)) buf[n++] = c;
    return 1;
  }
  using Print::write;
  void send() {
    if (n && usbOpen && Serial.availableForWrite() >= (int)n) Serial.write(buf, n);
    else if (n) dropped++;
    n = 0;
  }
  uint32_t dropped = 0;
 private:
  char buf[200];
  uint16_t n = 0;
};
MsgLine msg;

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

// RTC2 counts 24 bits (wraps every 512 s): extend it. The one place that unwraps the counter.
// The sampler task and loop() both call it, so the read and the wrap count update are one
// critical section. raw24: the counter value it read (the sampler steps back from it to a
// data-ready stamp; a stamp itself never goes into the wrap compare, docs/ble-plan.md 0).
uint64_t rtcExtendRaw(uint32_t* raw24) {
  taskENTER_CRITICAL();
  uint32_t ticks = NRF_RTC2->COUNTER;
  if (ticks < rtcLast) rtcWraps++;
  rtcLast = ticks;
  uint64_t ext = ((uint64_t)rtcWraps << 24) + ticks;
  taskEXIT_CRITICAL();
  if (raw24) *raw24 = ticks;
  return ext;
}

uint64_t rtcExtend() { return rtcExtendRaw(NULL); }

// The sampler writes the restart table (sampler.ino), loop() reads it: critical section.
bool isRestart(uint32_t idx) {
  bool found = false;
  taskENTER_CRITICAL();
  for (uint8_t i = 0; i < nRestart && !found; i++) found = restartIdx[i] == idx;
  taskEXIT_CRITICAL();
  return found;
}

// The watchdog: 8 s without loop() feeding it resets the board (a hung I2C bus, a bug). It
// pauses while a debugger halts the CPU and keeps counting in sleep; System OFF stops it. Once
// started only a power-on, a pin reset or a wake from System OFF stops it: a soft reset into
// another sketch (an upload) keeps it running, so firmware/imu_serial feeds it too.
// (The bootloader feeds it while it waits in DFU.)
void wdtStart() {
  NRF_WDT->CONFIG = (WDT_CONFIG_HALT_Pause << WDT_CONFIG_HALT_Pos) | (WDT_CONFIG_SLEEP_Run << WDT_CONFIG_SLEEP_Pos);
  NRF_WDT->CRV = 8 * 32768;
  NRF_WDT->RREN = WDT_RREN_RR0_Msk;
  NRF_WDT->TASKS_START = 1;
}

void wdtFeed() {
  NRF_WDT->RR[0] = WDT_RR_RR_Reload;
}

// Print a frame as "<tag> <hex>" in one write (a P line is ~480 characters).
void printFrame(char tag, const uint8_t* f, uint16_t len) {
  static char line[3 + 2 * POD_DATA_MAX + 2];
  static const char hx[] = "0123456789abcdef";
  line[0] = tag;
  line[1] = ' ';
  for (uint16_t i = 0; i < len; i++) {
    line[2 + 2 * i] = hx[f[i] >> 4];
    line[3 + 2 * i] = hx[f[i] & 15];
  }
  line[2 + 2 * len] = '\n';
  Serial.write(line, 3 + 2 * len);
}

// Step 4: pack the ring into DATA frames the same way the BLE sender will (docs/ble.md 5) and
// print them on USB. Full frames of 16, or a part frame once its oldest sample is 80 ms old;
// a frame always starts at an IMU restart and never spans more than 60000 ticks.
void usbPack() {
  bool open = (bool)Serial;
  if (open && !usbWasOpen) {                                 // a PC just opened the port
    usbCursor = head;
    usbFlags = POD_F_RESYNC;
  }
  usbWasOpen = open;
  if (!open) return;
  for (int frames = 0; frames < 4; frames++) {
    uint32_t h = head;
    if (h - usbCursor > RING_N) {                            // the ring lapped us
      usbLost += (h - RING_N) - usbCursor;
      usbCursor = h - RING_N;
      usbFlags |= POD_F_OVERFLOW;
    }
    uint32_t pending = h - usbCursor;
    if (pending == 0) return;
    uint8_t n = pending < POD_MAX_N ? pending : POD_MAX_N;
    uint32_t t0 = ring[usbCursor & RING_MASK].ticks;
    bool cut = false;
    for (uint8_t k = 1; k < n; k++) {
      uint32_t i = usbCursor + k;
      if (isRestart(i) || ring[i & RING_MASK].ticks - t0 > POD_DT_MAX) { n = k; cut = true; break; }
    }
    uint32_t age = (uint32_t)rtcExtend() - t0;
    if (n < POD_MAX_N && !cut && age < POD_AGE_TICKS) return;   // wait for a full frame
    uint8_t flags = usbFlags | (isRestart(usbCursor) ? POD_F_IMU_RESTART : 0);
    uint8_t f[POD_DATA_MAX];
    uint16_t len = podPackData(f, ring, RING_MASK, usbCursor, n, usbPktSeq, flags, 0);
    __asm__ volatile("" ::: "memory");                       // the copy happens before the check below
    if (head - usbCursor > RING_N) continue;                 // overwritten during the copy: the lap check takes it
    // A P line (up to 479 B) is bigger than the 256 B USB FIFO, so it can't wait for room for all
    // of it: this bench mode (PKT_OVER_USB, a reading PC) may block for the rest of a line.
    if (Serial.availableForWrite() < 64) return;
    printFrame('P', f, len);
    usbCursor += n;
    usbPktSeq++;
    usbFlags = 0;
  }
}

void podStatusInfo(uint32_t now);

// Everything the link does per loop pass, with or without a new sample (like the simulator's
// send pass): the BLE sender, and on the USB bench the P / S / I lines.
void podLinkTick(uint32_t now) {
  bleTick(now);
  if (!PKT_OVER_USB) return;
  usbPack();
  podStatusInfo(now);
}

void podStatusInfo(uint32_t now) {
  if (!usbOpen) return;
  if (now - lastRingLineMs >= 30000) {                       // USB-side diagnostics, not STATUS
    lastRingLineMs = now;
    msg.print("# ring head ");
    msg.print(head);
    msg.print(" usblost ");
    msg.print(usbLost);
    msg.print(" restarts ");
    msg.println(nRestart);
    msg.send();
  }
  if (now - lastPodStatusMs >= 1000) {
    lastPodStatusMs = now;
    uint8_t flags = (usbPowered() ? POD_S_USB : 0) | (charging() ? POD_S_CHARGING : 0) |
                    (lastBattV < LOW_BATT_WARN_V && !usbPowered() ? POD_S_BATT_LOW : 0);
    uint8_t f[20];
    podPackStatus(f, flags, (uint16_t)(lastBattV * 1000), (uint32_t)rtcExtend(), head, lostBle, missedDrdy, 0,
                  POD_MAX_N, 0);
    if (Serial.availableForWrite() >= 43) printFrame('S', f, 20);
  }
  if (now - lastPodInfoMs >= 5000) {
    lastPodInfoMs = now;
    uint8_t f[32];
    podPackInfo(f, FW_BUILD, bootId, NRF_FICR->DEVICEID[0], ODR_HZ, RING_N, wakeReason);
    if (Serial.availableForWrite() >= 67) printFrame('I', f, 32);
  }
}

// Read IMU registers with the whole I2C transaction sequenced by the TWIM hardware: address
// write, repeated start, len bytes, stop, with no CPU step in between (shortcuts LASTTX ->
// STARTRX and LASTRX -> STOP). Wire's own transfer needs the CPU to trigger STOP after the last
// byte; when the SoftDevice or a BLE task preempts loop() right then, the bus stays held (SCL
// low) for up to ~2 ms and the IMU skips that output update: about one lost sample every 2 s
// with the radio on (measured 2026-10-23, misses tracked radio events, not TX power).
// Same peripheral as Wire1 (TWIM1, already set up by imu.begin()); only the sampler task uses it.
// Wire waits on these events without clearing them first: a stale LASTTX would let its next
// transfer trigger STOP during the address byte and lose the write without an error.
void twimClearEvents(NRF_TWIM_Type* tw) {
  tw->EVENTS_TXSTARTED = 0;
  tw->EVENTS_RXSTARTED = 0;
  tw->EVENTS_LASTTX = 0;
  tw->EVENTS_LASTRX = 0;
  tw->EVENTS_SUSPENDED = 0;
  tw->EVENTS_STOPPED = 0;
  tw->EVENTS_ERROR = 0;
}

bool imuRead(uint8_t reg, uint8_t* buf, uint8_t len) {
  static uint8_t regBuf;                                     // EasyDMA reads from RAM
  NRF_TWIM_Type* tw = NRF_TWIM1;
  regBuf = reg;
  tw->ADDRESS = 0x6A;
  tw->TXD.PTR = (uint32_t)&regBuf;
  tw->TXD.MAXCNT = 1;
  tw->RXD.PTR = (uint32_t)buf;
  tw->RXD.MAXCNT = len;
  twimClearEvents(tw);
  tw->SHORTS = TWIM_SHORTS_LASTTX_STARTRX_Msk | TWIM_SHORTS_LASTRX_STOP_Msk;
  tw->TASKS_RESUME = 1;                                      // in case Wire left it suspended
  tw->TASKS_STARTTX = 1;
  uint32_t t0 = NRF_RTC2->COUNTER;
  while (!tw->EVENTS_STOPPED) {
    if (tw->EVENTS_ERROR || ((NRF_RTC2->COUNTER - t0) & 0xFFFFFF) > 66) {   // NACK, or 2 ms
      tw->TASKS_STOP = 1;
      uint32_t t1 = NRF_RTC2->COUNTER;
      while (!tw->EVENTS_STOPPED && ((NRF_RTC2->COUNTER - t1) & 0xFFFFFF) < 66) {}
      break;
    }
  }
  bool ok = !tw->EVENTS_ERROR && tw->EVENTS_STOPPED && tw->RXD.AMOUNT == len;
  if (tw->EVENTS_ERROR) tw->ERRORSRC = tw->ERRORSRC;         // write 1s to clear
  tw->SHORTS = 0;                                            // Wire expects none
  twimClearEvents(tw);
  return ok;
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
      // Data-ready as a pulse (a latched level with an edge interrupt deadlocks after one late
      // read), then the gyro's data-ready on INT1: the sampler's interrupt (sampler.ino)
      imu.writeRegister(REG_DRDY_PULSE_CFG_G, readReg(REG_DRDY_PULSE_CFG_G) | 0x80);
      imu.writeRegister(REG_INT1_CTRL, 0x02);
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
  samplerPark();                                             // the I2C bus is loop()'s from here on
  wdtFeed();
  msg.print("# sleeping: ");
  msg.println(why);
  msg.send();
  Serial.flush();
  blink(motionWake ? LED_BLUE : LED_RED, 3);
  // INT1 carries the wake-up next, not data-ready (the sampler detached its interrupt)
  imu.writeRegister(REG_INT1_CTRL, 0x00);
  imu.writeRegister(REG_DRDY_PULSE_CFG_G, readReg(REG_DRDY_PULSE_CFG_G) & 0x7F);
  // The pin's input buffer is disconnected after reset and after detachInterrupt(): without this,
  // digitalRead(INT1) is always LOW (the re-wake guard and the dry run below read it). Found in the
  // step 8 review: before it, both read nothing, and the D26 "w quiet" results proved nothing.
  pinMode(PIN_LSM6DS3TR_C_INT1, INPUT);

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
    while (millis() - t0 < 10000 && digitalRead(PIN_LSM6DS3TR_C_INT1) == LOW) wdtFeed();
    msg.print(digitalRead(PIN_LSM6DS3TR_C_INT1) ? "# dry run: INT1 fired after " : "# dry run: INT1 quiet for ");
    msg.print(millis() - t0);
    msg.print(" ms; CTRL1_XL 0x");                           // proves the sleep setup's writes landed
    msg.print(readReg(REG_CTRL1_XL), HEX);                   // (motion wake: 0x10 and 0x0)
    msg.print(" CTRL2_G 0x");
    msg.print(readReg(REG_CTRL2_G), HEX);
    msg.print(" INT1_CTRL 0x");
    msg.println(readReg(REG_INT1_CTRL), HEX);
    msg.send();
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
  uint32_t idleS = sessionHold ? IDLE_SLEEP_STREAMING_S : IDLE_SLEEP_S;
  // signed: the sampler task can stamp lastMotionMs a millisecond after loop() read millis(), and
  // an unsigned difference would wrap to "idle for 49 days" and sleep in the middle of a set
  if ((int32_t)(now - lastMotionMs) > (int32_t)(idleS * 1000) && !usbPowered()) goToSleep(true, "idle");

  if (now - lastBattMs > 5000) {
    lastBattMs = now;
    lastBattV = batteryVolts();
    if (!usbPowered()) {
      lowBattCount = lastBattV < LOW_BATT_V ? lowBattCount + 1 : 0;
      if (lowBattCount >= 3) goToSleep(false, "low battery, charge me");
    } else {
      lowBattCount = 0;
    }
    if (usbOpen && now % 30000 < 5000) {                     // a status line about every 30 s
      msg.print("# battery ");
      msg.print(lastBattV, 2);
      msg.print(" V, USB ");
      msg.print(usbPowered() ? "yes" : "no");
      msg.print(", charging ");
      msg.println(charging() ? "yes" : "no");
      msg.send();
    }
  }
}

// ---------------------------------------------------------------------------------------------

void setup() {
  wdtStart();                                                // first: nothing before it may hang unguarded
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
  wakeLatch0 = NRF_P0->LATCH;                                // what woke us, before anything changes it
  wakeLatch1 = NRF_P1->LATCH;
  NRF_P0->LATCH = wakeLatch0;                                // write 1s to clear
  NRF_P1->LATCH = wakeLatch1;
  wakeReason = (why & POWER_RESETREAS_OFF_Msk) ? 1 : (why & POWER_RESETREAS_VBUS_Msk) ? 2
             : (why & POWER_RESETREAS_RESETPIN_Msk) ? 3 : 0;
  Serial.begin(115200);
  uint32_t t0 = millis();
  while (!Serial && usbPowered() && millis() - t0 < 2000) delay(10);   // no PC on battery

  if (!imuStart()) {
    // On battery, don't sit here awake: sleep with the IMU off (USB or reset wakes it)
    if (!usbPowered()) goToSleep(false, "IMU not found");
    while (true) {
      wdtFeed();
      Serial.println("# IMU not found. Is this board the Sense version?");
      blink(LED_RED, 1);
      delay(1000);
    }
  }

  wdtFeed();
  analogCalibrateOffset();                                   // SAADC offset, once
  lastBattV = batteryVolts();

  Serial.print("# pod firmware, IMU WHO_AM_I 0x");
  Serial.print(readReg(REG_WHO_AM_I), HEX);
  Serial.print(", woke by: ");
  if (why & POWER_RESETREAS_OFF_Msk) Serial.println("motion (from sleep)");
  else if (why & POWER_RESETREAS_VBUS_Msk) Serial.println("USB power (from sleep)");
  else if (why & POWER_RESETREAS_RESETPIN_Msk) Serial.println("reset button");
  else if (why & POWER_RESETREAS_DOG_Msk) Serial.println("WATCHDOG (loop() stopped for 8 s)");
  else Serial.println("power on / upload");
  Serial.print("# RESETREAS 0x");
  Serial.println(why, HEX);
  if (why & POWER_RESETREAS_OFF_Msk) {
    // Which pin woke us: LATCH keeps the pins that met their SENSE level (INT1 is P0.11, 0x800)
    Serial.print("# wake: GPIO latch P0 0x");
    Serial.print(wakeLatch0, HEX);
    Serial.print(" P1 0x");
    Serial.println(wakeLatch1, HEX);
  }
  Serial.print("# battery ");
  Serial.print(lastBattV, 2);
  Serial.println(usbPowered() ? " V (on USB: with no battery fitted this is the charger, not a cell)" : " V");

  // Bluetooth (ble.ino), then the clock: the SoftDevice takes over the low-frequency clock as it
  // starts, so RTC2 starts after it and its count never pauses under a running stream.
  wdtFeed();
  bleSetup();                                                // the first boot formats a small filesystem (~1 s)
  wdtFeed();

  // RTC2 at 32768 Hz for timestamps (RTC0 = SoftDevice, RTC1 = FreeRTOS)
  NRF_RTC2->TASKS_STOP = 1;
  NRF_RTC2->PRESCALER = 0;
  NRF_RTC2->TASKS_CLEAR = 1;
  NRF_RTC2->TASKS_START = 1;

  Serial.println("# t_us,ax,ay,az,gx,gy,gz");

  lastMotionMs = millis();
  lastBattMs = millis();
  samplerStart();                                            // from here the sampler owns the IMU
  if (!fromSleep) blink(LED_GREEN, 1);                       // the ring fills meanwhile
  wdtFeed();
}

void loop() {
  uint32_t now = millis();
  wdtFeed();
  usbOpen = (bool)Serial;

  if (usbOpen && Serial.available()) {                       // bench commands (top of file)
    char c = Serial.read();
    if (c == 's') goToSleep(true, "command s");
    if (c == 'o') goToSleep(false, "command o");
    if (c == 'w') goToSleep(true, "command w (dry run)", true);
    if (c == 'i') imuStopReq = true;                         // the sampler stops the IMU (it owns the bus)
    if (c == 'h') {                                          // bench: hang; the watchdog resets in 8 s
      msg.println("# hanging loop() on purpose: the watchdog resets in 8 s");
      msg.send();
      Serial.flush();                                        // loop() won't return to send it
      for (;;) {}
    }
  }
  if (sleepReq == SLEEP_LID_DOWN) {                         // still no PC and no session?
    if (!usbOpen && !sessionHold) goToSleep(true, "lid-down gesture");
    sleepReq = 0;
  }
  if (sleepReq == SLEEP_IMU_LOST) goToSleep(false, "IMU lost");
  if (imuRestarts != seenRestarts) {
    seenRestarts = imuRestarts;
    msg.println("# IMU stopped answering, restarted it");
    msg.send();
  }

  housekeeping(now);
  podLinkTick(now);                                          // Bluetooth (and the P / S / I bench lines)
  usbDrain();
  delay(5);                                                  // the CPU sleeps; the sampler wakes on its own
}

// The CSV over USB, from the ring: never blocks (a host that holds the port open without reading
// stalls only USB, counted in csvLost) and starts live when the port opens.
void usbDrain() {
  if (usbOpen && !csvWasOpen) csvCursor = head;
  csvWasOpen = usbOpen;
  if (!usbOpen) return;
  for (int k = 0; k < 32; k++) {
    uint32_t h = head;
    if (h - csvCursor > RING_N) {                            // the host fell 38 s behind
      csvLost += (h - RING_N) - csvCursor;
      csvCursor = h - RING_N;
    }
    if (csvCursor == h || Serial.availableForWrite() < 72) return;   // a line is at most ~65 B
    PodSample smp = ring[csvCursor & RING_MASK];
    __asm__ volatile("" ::: "memory");                       // the copy happens before the check below
    if (head - csvCursor > RING_N) continue;                 // overwritten during the copy: lap check
    Serial.print((uint32_t)((uint64_t)smp.ticks * 15625 / 512));   // ticks -> us (wraps after ~71 min)
    for (int i = 0; i < 3; i++) { Serial.print(','); Serial.print(imu.calcAccel(smp.a[i]) * G, 4); }
    for (int i = 0; i < 3; i++) { Serial.print(','); Serial.print(imu.calcGyro(smp.g[i]), 3); }
    Serial.println();
    csvCursor++;
  }
}
