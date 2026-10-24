// sampler.ino: the IMU sampler (docs/ble-plan.md step 8, section 2 "Sampling").
//
// The IMU pulses INT1 (~75 us, pulsed data-ready, DRDY_PULSE_CFG_G bit 7) at every new gyro
// sample. The ISR stamps the pulse with RTC2 and wakes the sampler task, which runs above
// everything else in the firmware (priority 4: above Bluefruit's BLE task, USB and loop()),
// reads the sample with hardware-sequenced I2C (imuRead, D28) and stores it in the ring.
// So a sample's time is when the IMU said it was ready, not when something got round to it,
// and nothing loop() does (the radio, USB, a host that stops reading) can delay or lose one.
//
// The sampler owns the IMU's I2C bus from setup() on. Whatever else needs the bus (sleep, the
// bench commands) goes through it: samplerPark() before sleep, imuStopReq for command i.
// Nothing here prints (it would splice into a CSV line): loop() reports the counters.

// The counters and requests loop() and ble.ino share with the sampler are in pod.ino's globals
// (Arduino joins the tabs after pod.ino, so a variable declared here would come too late).
TaskHandle_t samplerHandle = NULL;
volatile uint32_t drdyTicks = 0;        // RTC2 (24 bits) at the last data-ready pulse
volatile uint32_t drdyCount = 0;        // data-ready pulses since boot
volatile bool parkReq = false, parked = false;

// GPIOTE, priority 3 (not deferred): stamp, count, wake the sampler. Nothing else.
void onDrdy() {
  drdyTicks = NRF_RTC2->COUNTER;
  drdyCount++;
  BaseType_t woken = pdFALSE;
  vTaskNotifyGiveFromISR(samplerHandle, &woken);
  portYIELD_FROM_ISR(woken);
}

// Start the sampler (from setup(), after the IMU and RTC2 are running).
void samplerStart() {
  xTaskCreate(samplerTask, "sampler", 512, NULL, TASK_PRIO_HIGHEST, &samplerHandle);
  attachInterrupt(digitalPinToInterrupt(PIN_LSM6DS3TR_C_INT1), onDrdy, RISING);
}

// Take the I2C bus back for good (sleep, the dry run: both end in a reset). The sampler stops at
// the top of its loop, never inside a transfer or inside imuStart(). Waits up to 500 ms.
void samplerPark() {
  if (samplerHandle == NULL || parked) return;
  parkReq = true;
  xTaskNotifyGive(samplerHandle);
  uint32_t t0 = millis();
  while (!parked && millis() - t0 < 500) delay(1);
}

// No pulse for 50 x 20 ms: power-cycle the IMU and start it again. Frames start at the next
// sample with IMU_RESTART (the restart table, docs/ble.md 3).
void restartImu() {
  imuRestarts++;
  if (imuStart()) {
    // No duplicates (a restart that gave no sample yet), forget restarts that already left the
    // ring, and if it's still full drop the oldest. loop() reads the table too: critical section.
    taskENTER_CRITICAL();
    uint32_t h = head;
    uint32_t lo = h > RING_N ? h - RING_N : 0;
    uint8_t keep = 0;
    for (uint8_t i = 0; i < nRestart; i++) if (restartIdx[i] >= lo) restartIdx[keep++] = restartIdx[i];
    nRestart = keep;
    if (nRestart == 0 || restartIdx[nRestart - 1] != h) {
      if (nRestart == 40) { memmove(restartIdx, restartIdx + 1, 39 * 4); nRestart--; }
      restartIdx[nRestart++] = h;
    }
    taskEXIT_CRITICAL();
  } else if (!usbPowered()) {
    sleepReq = SLEEP_IMU_LOST;          // on battery: sleep with the IMU off (loop() does it)
  }
  ulTaskNotifyTake(pdTRUE, 0);          // pulses from the power cycle mean nothing
}

// Motion (the idle timer) and the lid-down gesture, from each stored sample (D26).
void motionCheck(const PodSample& s) {
  static float aRef[3];                 // slow average of the accel vector (motion = change from it)
  static bool aRefSet = false;
  static uint32_t faceDownSinceMs = 0;  // 0 = not lid-down right now
  float a[3], g[3];
  for (int k = 0; k < 3; k++) {
    a[k] = imu.calcAccel(s.a[k]) * G;
    g[k] = imu.calcGyro(s.g[k]);
  }
  if (!aRefSet) { for (int k = 0; k < 3; k++) aRef[k] = a[k]; aRefSet = true; }
  float da = 0, gm = 0, am = 0;
  for (int k = 0; k < 3; k++) {
    da += (a[k] - aRef[k]) * (a[k] - aRef[k]);
    gm += g[k] * g[k];
    am += a[k] * a[k];
    aRef[k] += (a[k] - aRef[k]) * 0.005;                     // ~1 s time constant at 208 Hz
  }
  uint32_t now = millis();
  if (da > 0.6 * 0.6 || gm > 15.0 * 15.0) lastMotionMs = now;   // 0.6 m/s^2, 15 deg/s

  // Sleep now: lid-down (chip side down = -z up, within ~27 deg) and still. "Still" doesn't use
  // aRef, so it holds right after the flip. Not while a PC has the port open (a recording, or
  // the chip-down calibration face) or a phone holds a session.
  bool still = gm < 15.0 * 15.0 && fabsf(sqrtf(am) - G) < 1.0;
  if (a[2] < -9.0 && still && !usbOpen && !sessionHold) {
    if (faceDownSinceMs == 0) faceDownSinceMs = now;
    else if (now - faceDownSinceMs > FACE_DOWN_S * 1000) sleepReq = SLEEP_LID_DOWN;
  } else {
    faceDownSinceMs = 0;
  }
}

void samplerTask(void* arg) {
  (void)arg;
  uint16_t misses = 0;                  // passes in a row that stored nothing (no pulse in 20 ms,
                                        // or pulses but reads that fail): 50 restart the IMU
  uint32_t lastTicks = 0;
  for (;;) {
    if (parkReq) {                      // loop() takes the bus: stop here for good (a reset follows)
      detachInterrupt(digitalPinToInterrupt(PIN_LSM6DS3TR_C_INT1));
      parked = true;
      for (;;) vTaskSuspend(NULL);
    }
    if (imuStopReq) {                   // bench command i: no more data-ready, so a restart follows
      imuStopReq = false;
      imu.writeRegister(REG_CTRL1_XL, 0x00);
      imu.writeRegister(REG_CTRL2_G, 0x00);
    }
    uint32_t n = ulTaskNotifyTake(pdTRUE, ms2tick(20));
    if (parkReq) continue;
    if (n == 0) rtcExtend();            // keep counting RTC2 wraps while the IMU is silent
    if (++misses >= 50) {               // ~1 s silent, or ~0.25 s of pulses that never gave a sample
      misses = 0;
      restartImu();
      continue;
    }
    if (n == 0) continue;
    taskENTER_CRITICAL();               // the pulse's count and stamp, as one pair
    uint32_t cnt = drdyCount, tk = drdyTicks;
    taskEXIT_CRITICAL();
    if (n > 1) missedDrdy += n - 1;     // pulses that came while we were busy: those samples are gone

    // The gyro's pulse woke us; the accel's sample is ready with it or just after (up to 1 ms).
    uint8_t st = 0;
    uint32_t w0 = NRF_RTC2->COUNTER;
    bool lagged = false;
    for (;;) {
      if (!imuRead(LSM6DS3_ACC_GYRO_STATUS_REG, &st, 1)) { st = 0; readErrors++; }
      if ((st & 0x03) == 0x03) break;
      lagged = true;
      if (((NRF_RTC2->COUNTER - w0) & 0xFFFFFF) > 33) break;   // 1 ms
    }
    if (lagged) xlLag++;
    uint8_t b[12];                      // gyro x, y, z then accel x, y, z
    if ((st & 0x03) != 0x03 || !imuRead(LSM6DS3_ACC_GYRO_OUTX_L_G, b, 12)) {
      if ((st & 0x03) == 0x03) readErrors++;
      missedDrdy++;
      continue;
    }
    if (drdyCount != cnt) {             // a newer sample arrived during the read: the burst may
      lateRead++;                       // already be that one, so this stamp would be wrong. Drop
      missedDrdy++;                     // it; the pending pulse reads the newer one at once.
      continue;
    }

    // The pulse's time on the unwrapped clock: a fresh read does the unwrap, never the stamp.
    uint32_t raw24;
    uint64_t ext = rtcExtendRaw(&raw24);
    uint32_t lat = (raw24 - tk) & 0xFFFFFF;
    if (lat > maxLatency) maxLatency = lat;
    uint32_t ticks = (uint32_t)(ext - lat);                    // u32: wraps after 36.4 h

    PodSample& smp = ring[head & RING_MASK];
    smp.ticks = ticks;
    for (int k = 0; k < 3; k++) {
      smp.g[k] = (int16_t)(b[2 * k] | (b[2 * k + 1] << 8));
      smp.a[k] = (int16_t)(b[6 + 2 * k] | (b[7 + 2 * k] << 8));
    }
    __asm__ volatile("" ::: "memory");  // the slot is written before head says it exists
    head = head + 1;
    misses = 0;                         // a sample stored: the IMU is fine
    if (lastTicks && ticks - lastTicks > 236 && !isRestart(head - 1)) holes++;   // > 1.5 periods
    lastTicks = ticks;
    motionCheck(smp);
  }
}
