// podlink.h: PodLink v1, the pod's Bluetooth protocol (docs/ble.md is the spec; this mirrors it
// byte for byte, and so do analysis/podlink.py and the PodStream in webapp/sources.js).
// All multi-byte fields are little-endian. Byte 0 of every frame is its type.
#pragma once
#include <stdint.h>
#include <string.h>

// ---- frame types, flags, ops (docs/ble.md sections 3 and 4) ----
const uint8_t POD_T_DATA = 0x01, POD_T_STATUS = 0x10, POD_T_INFO = 0x20, POD_T_CAL = 0x30;
const uint8_t POD_F_RESYNC = 0x01, POD_F_OVERFLOW = 0x02, POD_F_IMU_RESTART = 0x04, POD_F_FINAL = 0x08;
const uint8_t POD_S_USB = 0x01, POD_S_CHARGING = 0x02, POD_S_BATT_LOW = 0x04, POD_S_STREAMING = 0x08,
              POD_S_GOING_TO_SLEEP = 0x10, POD_S_STREAM_STOPPED = 0x20, POD_S_MTU_TOO_SMALL = 0x40,
              POD_S_BOOT_MISMATCH = 0x80;
const uint8_t POD_OP_STREAM_FROM = 0x01, POD_OP_STREAM_LIVE = 0x02, POD_OP_STOP = 0x03, POD_OP_SLEEP = 0x10;

const uint8_t POD_PROTO = 1;
const uint16_t POD_TICK_HZ = 32768;
const float POD_ACCEL_LSB = 0.061f * 4 / 1000 * 9.80665f;    // m/s^2 per count at +-8 g
const float POD_GYRO_LSB = 4.375f * 8 / 1000;                // deg/s per count at +-1000 deg/s
const uint8_t POD_MAX_N = 16;                                // samples per DATA frame at MTU 247
const uint16_t POD_DATA_MAX = 14 + 14 * POD_MAX_N;           // 238 B
const uint32_t POD_AGE_TICKS = 2621;                         // send a part frame after 80 ms
const uint32_t POD_DT_MAX = 60000;                           // no dt past 60000 ticks (1.83 s)

// ---- UUIDs: 9ba4xxxx-6de5-429c-b225-8134b9953e9d, Bluefruit's little-endian byte order.
// One array per role: BLEUuid keeps a pointer, never a copy, so never patch a shared buffer.
#define POD_UUID(role) {0x9D, 0x3E, 0x95, 0xB9, 0x34, 0x81, 0x25, 0xB2, 0x9C, 0x42, 0xE5, 0x6D, role, 0x00, 0xA4, 0x9B}
static const uint8_t POD_UUID_SERVICE[16] = POD_UUID(0x01);
static const uint8_t POD_UUID_DATA[16] = POD_UUID(0x02);
static const uint8_t POD_UUID_CTRL[16] = POD_UUID(0x03);
static const uint8_t POD_UUID_STATUS[16] = POD_UUID(0x04);
static const uint8_t POD_UUID_INFO[16] = POD_UUID(0x05);
static const uint8_t POD_UUID_CAL[16] = POD_UUID(0x06);

// One IMU sample in the ring: its RTC2 ticks and the raw burst, gyro then accel (16 B).
struct PodSample {
  uint32_t ticks;
  int16_t g[3];
  int16_t a[3];
};

static inline void podPut16(uint8_t* p, uint16_t v) { p[0] = v; p[1] = v >> 8; }
static inline void podPut32(uint8_t* p, uint32_t v) { p[0] = v; p[1] = v >> 8; p[2] = v >> 16; p[3] = v >> 24; }

// DATA frame from n consecutive ring samples starting at index first (docs/ble.md 3).
// ring is a power-of-two array, mask = its length - 1. Returns the frame length, 14 + 14n.
static inline uint16_t podPackData(uint8_t* out, const PodSample* ring, uint32_t mask, uint32_t first,
                                   uint8_t n, uint16_t pktSeq, uint8_t flags, uint8_t cmdSeq) {
  const PodSample& s0 = ring[first & mask];
  out[0] = POD_T_DATA;
  out[1] = n;
  podPut16(out + 2, pktSeq);
  podPut32(out + 4, first);
  podPut32(out + 8, s0.ticks);
  out[12] = flags;
  out[13] = cmdSeq;
  for (uint8_t k = 0; k < n; k++) {
    const PodSample& s = ring[(first + k) & mask];
    uint8_t* p = out + 14 + 14 * k;
    podPut16(p, (uint16_t)(s.ticks - s0.ticks));               // dt_ticks, sample 0 = 0
    for (int j = 0; j < 3; j++) podPut16(p + 2 + 2 * j, (uint16_t)s.g[j]);
    for (int j = 0; j < 3; j++) podPut16(p + 8 + 2 * j, (uint16_t)s.a[j]);
  }
  return 14 + 14 * n;
}

// STATUS frame, 20 B (docs/ble.md 3).
static inline void podPackStatus(uint8_t* out, uint8_t flags, uint16_t mv, uint32_t ticksNow, uint32_t head,
                                 uint32_t lost, uint32_t missed, uint8_t sleepReason, uint8_t nPerPacket,
                                 uint16_t interval) {
  out[0] = POD_T_STATUS;
  out[1] = flags;
  podPut16(out + 2, mv);
  podPut32(out + 4, ticksNow);
  podPut32(out + 8, head);
  podPut16(out + 12, lost > 0xFFFF ? 0xFFFF : lost);         // saturating counters
  podPut16(out + 14, missed > 0xFFFF ? 0xFFFF : missed);
  out[16] = sleepReason;
  out[17] = nPerPacket;
  podPut16(out + 18, interval);
}

// INFO frame, 32 B (docs/ble.md 3).
static inline void podPackInfo(uint8_t* out, uint16_t fwBuild, uint32_t bootId, uint32_t serial,
                               uint16_t odr, uint16_t ringN, uint8_t wake) {
  memset(out, 0, 32);
  out[0] = POD_T_INFO;
  out[1] = POD_PROTO;
  podPut16(out + 2, fwBuild);
  podPut32(out + 4, bootId);
  podPut32(out + 8, serial);
  float al = POD_ACCEL_LSB, gl = POD_GYRO_LSB;
  memcpy(out + 12, &al, 4);                                   // the nRF52 is little-endian
  memcpy(out + 16, &gl, 4);
  podPut16(out + 20, odr);
  podPut16(out + 22, ringN);
  out[24] = wake;
  podPut16(out + 26, POD_TICK_HZ);
}
