// host_test.c: podcrp.c and the vendored CRP engine (src/crp) on the PC, with fake hooks (D45).
// Build and run: bash firmware/pod/test/build.sh. One PASS/FAIL line per check, exit 1 on any FAIL.
//
// The fake SoftDevice takes `slots` more notifications (-1 = any number); the fake USB port takes
// `usb_room` more bytes. Every byte the link sends lands in one log, which the checks read back
// packet by packet (USB through the level 1 length).
//
// With a file argument (analysis/crp.py --vectors, which build.sh passes once crp.py exists) it
// also replays the Python reference's pod packets through the engine and requires the same bytes
// (P1), and requires the client packets this test feeds to be crp.py's.
#include "../podcrp.h"
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

// ---- the client packets fed below, as crp.py --vectors builds them ----
#define C_HELLO "01 0B 04 00 02 00 00 00 00 00"           // hello(0x0B, 4, 512, 0, 0)
#define C_READ_INFO "06 01 01 00 01 00"                    // read(1, INFO)
#define C_WRITE_CTRL "04 02 05 00 01 00 02 01 58 02"       // write(2, CTRL, STREAM_LIVE cmd_seq 1 back 600)
#define C_NOP "00"                                         // nop()
#define C_BYE "02 00 00 00 00 00 00 00 00"                 // bye()
#define C_HELLO_V0A "01 0A 04 00 02 00 00 00 00 00"       // hello(0x0A, ...): too old
#define C_END "16 FF"                                      // end(0xFF): the session
// ---- what the pod must answer (docs/ble.md 2.2) ----
#define P_HELLO_BLE "01 0B 01 F4 00 01 42 50 4F 44"        // ver 0x0B, 1 channel, 244, PUSH, "BPOD"
#define P_HELLO_USB "01 0B 01 00 04 01 42 50 4F 44"        // max_chunk 1024 on USB
#define P_BYE "02 00 00 00 00 42 50 4F 44"
#define P_BUSY_INFO_3 "0C 03 08 FF 01 00 01 00"            // response(3, 8, INFO)

// ================================================================================================
// Checks and hex
// ================================================================================================
static int n_pass, n_fail;

static void check(bool ok, const char *fmt, ...) {
  va_list ap;
  va_start(ap, fmt);
  printf("%s  ", ok ? "PASS" : "FAIL");
  vprintf(fmt, ap);
  printf("\n");
  va_end(ap);
  if (ok) n_pass++;
  else n_fail++;
}

static size_t unhex(const char *s, uint8_t *out, size_t max) {
  size_t n = 0;
  int hi = -1;
  for (; *s; s++) {
    int v;
    if (*s >= '0' && *s <= '9') v = *s - '0';
    else if (*s >= 'a' && *s <= 'f') v = *s - 'a' + 10;
    else if (*s >= 'A' && *s <= 'F') v = *s - 'A' + 10;
    else continue;                                          // spaces
    if (hi < 0) {
      hi = v;
    } else {
      if (n < max) out[n++] = (uint8_t)(hi << 4 | v);
      hi = -1;
    }
  }
  return n;
}

// printf into one of 16 rotating buffers, so a few can be nested in one call
static const char *hx(const char *fmt, ...) {
  static char buf[16][4096];
  static int k;
  char *b = buf[k++ & 15];
  va_list ap;
  va_start(ap, fmt);
  vsnprintf(b, sizeof buf[0], fmt, ap);
  va_end(ap);
  return b;
}

static const char *tohex(const uint8_t *p, size_t n) {
  static char buf[4][4096];
  static int k;
  char *b = buf[k++ & 3];
  size_t o = 0;
  b[0] = 0;
  for (size_t i = 0; i < n && o + 4 < sizeof buf[0]; i++) o += (size_t)snprintf(b + o, sizeof buf[0] - o, i ? " %02X" : "%02X", p[i]);
  return b;
}

static const char *rg(uint32_t reg) {                       // a register address, little-endian
  return hx("%02X %02X %02X %02X", reg & 0xFF, (reg >> 8) & 0xFF, (reg >> 16) & 0xFF, reg >> 24);
}
static const char *u32h(uint32_t v) { return rg(v); }
static const char *str_hex(const char *s) {
  uint8_t b[256];
  size_t n = strlen(s);
  memcpy(b, s, n);
  return hx("%s", tohex(b, n));
}
static const char *rd(int txn, uint32_t reg) { return hx("06 %02X %s", txn, rg(reg)); }
static const char *wr(int txn, uint32_t reg, const char *val) { return hx("04 %02X %s %s", txn, rg(reg), val); }
static const char *resp(int txn, int st, uint32_t reg, const char *val) {
  return hx("0C %02X %02X FF %s %s", txn, st, rg(reg), val);
}

static void le16(uint8_t *p, uint16_t v) { p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); }
static void le32(uint8_t *p, uint32_t v) { for (int i = 0; i < 4; i++) p[i] = (uint8_t)(v >> (8 * i)); }

// ================================================================================================
// The fake sketch
// ================================================================================================
typedef struct {
  int slots;                          // BLE: notifications the fake SoftDevice still takes, -1 = any
  long usb_room;                      // USB: bytes the fake port still takes, -1 = any
  uint8_t out[65536];                 // every byte the link sent, in order
  uint32_t out_len, rd;               // USB: bytes sent, bytes read back
  uint32_t ends_at[4096];             // BLE: where each notification ends in out
  int npkt, rdpkt;
  uint8_t info[32];
  uint32_t head;                      // what STATUS and HEAD report
  const uint8_t *frame;               // a vector's value for frame_reg, if set
  uint32_t frame_reg, frame_len;
  int info_reads, status_reads;
  bool no_stack_free;
  uint8_t ctrl[16];
  uint32_t ctrl_len;
  int ctrl_calls;
  uint8_t ctrl_answer;
  bool push_in_hook, push_in_hook_ok;
  int bench_calls;
  uint8_t bench_c;
  int starts, ends;
  uint8_t end_why;                    // the last on_session_end's PODCRP_END_*
} fake_t;

static podcrp_link_t L;
static fake_t F;
static uint32_t now;

static uint32_t f_send(podcrp_link_t *l, const uint8_t *p, uint32_t len) {
  fake_t *f = (fake_t *)l->user;
  uint32_t take = len;
  if (l->kind == PODCRP_BLE) {
    if (f->slots == 0) return 0;
    if (f->slots > 0) f->slots--;
  } else if (f->usb_room >= 0) {
    if ((long)take > f->usb_room) take = (uint32_t)f->usb_room;
    f->usb_room -= (long)take;
  }
  if (f->out_len + take > sizeof f->out || f->npkt >= 4096) {
    fprintf(stderr, "the fake's log is full\n");
    exit(2);
  }
  memcpy(f->out + f->out_len, p, take);
  f->out_len += take;
  if (l->kind == PODCRP_BLE) f->ends_at[f->npkt++] = f->out_len;
  return take;
}

static uint32_t f_read(podcrp_link_t *l, uint32_t reg, uint8_t *buf, uint32_t room) {
  fake_t *f = (fake_t *)l->user;
  if (f->frame && reg == f->frame_reg) {
    uint32_t n = f->frame_len < room ? f->frame_len : room;
    memcpy(buf, f->frame, n);
    return n;
  }
  switch (reg) {
    case PODCRP_REG_INFO:
      f->info_reads++;
      memcpy(buf, f->info, 32);
      return 32;
    case PODCRP_REG_STATUS:                 // built now, with the head of now
      f->status_reads++;
      buf[0] = 0x10;
      le16(buf + 2, 3950);
      le32(buf + 4, now * 33);
      le32(buf + 8, f->head);
      buf[17] = 16;
      le16(buf + 18, 24);
      return 20;
    case PODCRP_REG_CAL:
      buf[0] = 0x30;
      return 64;
    case PODCRP_REG_HEAD:
      le32(buf, f->head);
      return 4;
    case PODCRP_REG_NAME:
      memcpy(buf, "BarPod-TEST", 11);
      return 11;
    case PODCRP_REG_STACK_FREE:
      if (f->no_stack_free) return 0;
      le32(buf, 1234);
      return 4;
    default:
      return room;                          // every other mirror: zeros at full size
  }
}

static uint8_t f_ctrl(podcrp_link_t *l, const uint8_t *p, uint32_t len) {
  fake_t *f = (fake_t *)l->user;
  f->ctrl_calls++;
  memcpy(f->ctrl, p, len);
  f->ctrl_len = len;
  if (f->push_in_hook) {                    // the mistake the glue must refuse
    uint8_t st[20] = {0x10};
    f->push_in_hook_ok = podcrp_push(l, PODCRP_REG_STATUS, st, 20);
  }
  return f->ctrl_answer;
}

static uint8_t f_bench(podcrp_link_t *l, uint8_t c) {
  fake_t *f = (fake_t *)l->user;
  f->bench_calls++;
  f->bench_c = c;
  return CRP_STATUS_OK;
}

static void f_start(podcrp_link_t *l) { ((fake_t *)l->user)->starts++; }
static void f_end(podcrp_link_t *l, uint8_t why) {
  fake_t *f = (fake_t *)l->user;
  f->ends++;
  f->end_why = why;
}

static const podcrp_hooks_t HOOKS = {f_send, f_read, f_ctrl, f_bench, f_start, f_end};

// A fresh link and fake at t = 1000 ms.
static podcrp_link_t *fresh(uint8_t kind) {
  memset(&F, 0, sizeof F);
  F.slots = -1;
  F.usb_room = -1;
  float al = 0.0023928226f, gl = 0.035f;    // a real INFO frame (docs/ble.md 3)
  F.info[0] = 0x20;
  F.info[1] = 1;
  le16(F.info + 2, 10);
  le32(F.info + 4, 0x11223344);
  le32(F.info + 8, 0xCAFEF00D);
  memcpy(F.info + 12, &al, 4);
  memcpy(F.info + 16, &gl, 4);
  le16(F.info + 20, 208);
  le16(F.info + 22, 8192);
  le16(F.info + 26, 32768);
  podcrp_init(&L, kind, &HOOKS, PODCRP_DEVICE_ID, "BarPod-TEST");
  L.user = &F;
  now = 1000;
  return &L;
}

static void feed_bytes(podcrp_link_t *l, const uint8_t *b, size_t n) {
  if (l->kind == PODCRP_USB) {              // level 1 on USB
    uint8_t fr[1100];
    fr[0] = (uint8_t)n;
    fr[1] = (uint8_t)(n >> 8);
    memcpy(fr + 2, b, n);
    podcrp_rx_bytes(l, fr, (uint32_t)n + 2);
  } else {
    podcrp_rx_packet(l, b, (uint32_t)n);
  }
}
static void feed(podcrp_link_t *l, const char *hexs) {
  uint8_t b[1100];
  feed_bytes(l, b, unhex(hexs, b, sizeof b));
}
static void pass(podcrp_link_t *l) { podcrp_pass(l, now); }
static void req(podcrp_link_t *l, const char *hexs) {
  feed(l, hexs);
  pass(l);
}

// The next packet the link sent (0 = none yet).
static size_t next_pkt(podcrp_link_t *l, uint8_t *buf) {
  fake_t *f = (fake_t *)l->user;
  if (l->kind == PODCRP_BLE) {
    if (f->rdpkt >= f->npkt) return 0;
    uint32_t start = f->rdpkt ? f->ends_at[f->rdpkt - 1] : 0, end = f->ends_at[f->rdpkt];
    f->rdpkt++;
    memcpy(buf, f->out + start, end - start);
    return end - start;
  }
  if (f->out_len - f->rd < 2) return 0;
  uint32_t n = f->out[f->rd] | (uint32_t)f->out[f->rd + 1] << 8;
  if (f->out_len - f->rd < 2 + n) return 0;
  memcpy(buf, f->out + f->rd + 2, n);
  f->rd += 2 + n;
  return n;
}
static void skip_pkt(podcrp_link_t *l) {
  uint8_t b[1100];
  next_pkt(l, b);
}

static bool expect(podcrp_link_t *l, const char *want_hex, const char *fmt, ...) {
  uint8_t want[1100], got[1100];
  size_t nw = unhex(want_hex, want, sizeof want), ng = next_pkt(l, got);
  bool ok = ng == nw && memcmp(got, want, nw) == 0;
  char name[400];
  va_list ap;
  va_start(ap, fmt);
  vsnprintf(name, sizeof name, fmt, ap);
  va_end(ap);
  check(ok, "%s", name);
  if (!ok) printf("        want %s\n        got  %s\n", tohex(want, nw), ng ? tohex(got, ng) : "(nothing)");
  return ok;
}

static void expect_none(podcrp_link_t *l, const char *name) {
  uint8_t got[1100];
  size_t ng = next_pkt(l, got);
  bool ok = ng == 0 && (l->kind == PODCRP_BLE || F.rd == F.out_len);
  check(ok, "%s", name);
  if (ng) printf("        got  %s\n", tohex(got, ng));
}

// The status byte of the next packet, a RESPONSE (-1 if none).
static int next_status(podcrp_link_t *l) {
  uint8_t b[1100];
  size_t n = next_pkt(l, b);
  return n >= 8 && b[0] == 0x0C ? b[2] : -1;
}

static void open_session(podcrp_link_t *l, uint16_t mtu) {
  podcrp_set_mtu(l, mtu);                   // nothing on USB
  req(l, C_HELLO);
  skip_pkt(l);
}
static void push_on(podcrp_link_t *l) {
  req(l, wr(0x7E, PODCRP_REG_PUSH, "01"));
  skip_pkt(l);
}

// A DATA frame of n samples (14 + 14n B), deterministic bytes.
static uint32_t data_frame(uint8_t *f, int n) {
  uint32_t len = 14 + 14 * (uint32_t)n;
  for (uint32_t i = 0; i < len; i++) f[i] = (uint8_t)(i * 7 + n);
  f[0] = 0x01;
  f[1] = (uint8_t)n;
  return len;
}
static void status_frame(uint8_t *f) {
  memset(f, 0, 20);
  f[0] = 0x10;
  f[1] = 0x08;
  le32(f + 8, 4242);
}

// ================================================================================================
// The checks
// ================================================================================================
static void t_hello(void) {
  printf("-- HELLO and the session start\n");
  podcrp_link_t *l = fresh(PODCRP_BLE);
  req(l, C_READ_INFO);
  expect_none(l, "a READ before HELLO is ignored, nothing sent");
  req(l, C_NOP);
  req(l, wr(5, PODCRP_REG_PUSH, "01"));
  expect_none(l, "NOP and WRITE PUSH before HELLO: nothing sent");
  check(L.push == 0, "the WRITE PUSH before HELLO changed nothing");
  now = 6000;
  pass(l);
  expect_none(l, "no heartbeat NOP before a session");
  req(l, C_HELLO_V0A);
  expect(l, P_BYE, "a HELLO with version 0x0A gets BYE 02 00000000 42504F44");
  check(!podcrp_in_session(l) && F.starts == 0, "and no session");
  req(l, C_HELLO);
  expect(l, P_HELLO_BLE, "HELLO reply on Bluetooth = %s", P_HELLO_BLE);
  check(podcrp_in_session(l) && F.starts == 1 && F.ends == 0, "on_session_start once, no on_session_end");
  check(!podcrp_pushes_on(l), "PUSH is 0 in a new session");

  l = fresh(PODCRP_USB);
  req(l, C_HELLO);
  check(F.out_len == 12 && F.out[0] == 0x0A && F.out[1] == 0x00, "USB: the reply is level 1 framed (0A 00 ...)");
  expect(l, P_HELLO_USB, "HELLO reply on USB = %s", P_HELLO_USB);
}

static void t_info_busy(void) {
  printf("-- INFO read and the BUSY rule (docs/ble.md 5)\n");
  podcrp_link_t *l = fresh(PODCRP_BLE);
  open_session(l, 247);
  req(l, C_READ_INFO);
  expect(l, resp(1, 0, PODCRP_REG_INFO, tohex(F.info, 32)), "READ INFO at MTU 247 = 0C 01 00 FF 01000100 + the 32 B frame");

  int busy_ok = 0, ok_ok = 0, reads_before = F.info_reads, oks = 0;
  for (int mtu = 23; mtu <= 247; mtu++) {
    podcrp_set_mtu(l, (uint16_t)mtu);
    req(l, rd(3, PODCRP_REG_INFO));
    uint8_t b[300];
    size_t n = next_pkt(l, b);
    if (mtu <= 42) {
      busy_ok += n == 8 && b[2] == CRP_STATUS_BUSY;
    } else {
      ok_ok += n == 40 && b[2] == CRP_STATUS_OK && memcmp(b + 8, F.info, 32) == 0;
      oks++;
    }
  }
  check(busy_ok == 20, "INFO answered BUSY at every MTU 23..42 (%d/20)", busy_ok);
  check(ok_ok == 205, "INFO answered OK (40 B) at every MTU 43..247 (%d/205)", ok_ok);
  check(F.info_reads - reads_before == oks, "a BUSY read builds no INFO (hook calls %d, OK reads %d)",
        F.info_reads - reads_before, oks);
  podcrp_set_mtu(l, 23);
  req(l, rd(3, PODCRP_REG_INFO));
  expect(l, P_BUSY_INFO_3, "the BUSY answer is 8 B: %s", P_BUSY_INFO_3);

  podcrp_set_mtu(l, 300);
  req(l, rd(4, PODCRP_REG_MTU));
  expect(l, resp(4, 0, PODCRP_REG_MTU, "F7 00"), "an MTU of 300 counts as 247 (MTU register)");
  podcrp_set_mtu(l, 10);
  req(l, rd(5, PODCRP_REG_MTU));
  expect(l, resp(5, 0, PODCRP_REG_MTU, "17 00"), "an MTU of 10 counts as 23, and the MTU read (10 B) fits there");
}

static void t_status_read(void) {
  printf("-- STATUS and CAL reads\n");
  podcrp_link_t *l = fresh(PODCRP_BLE);
  open_session(l, 247);
  F.head = 100;
  feed(l, rd(9, PODCRP_REG_STATUS));
  check(F.status_reads == 0, "a queued READ STATUS builds nothing yet");
  F.head = 200;
  pass(l);
  uint8_t b[300];
  size_t n = next_pkt(l, b);
  check(F.status_reads == 1, "the STATUS is built once, when the READ is processed");
  check(n == 28 && b[2] == 0 && b[8] == 0x10 && b[16] == 200 && b[17] == 0,
        "and carries the head of that moment (200, not the 100 at queue time)");
  podcrp_set_mtu(l, 30);
  req(l, rd(10, PODCRP_REG_STATUS));
  check(next_status(l) == CRP_STATUS_BUSY && F.status_reads == 1, "STATUS at MTU 30: BUSY, no frame built");
  podcrp_set_mtu(l, 31);
  req(l, rd(11, PODCRP_REG_STATUS));
  check(next_status(l) == CRP_STATUS_OK, "STATUS at MTU 31: OK (8 + 20 = 31 - 3)");
  podcrp_set_mtu(l, 74);
  req(l, rd(12, PODCRP_REG_CAL));
  check(next_status(l) == CRP_STATUS_BUSY, "CAL at MTU 74: BUSY");
  podcrp_set_mtu(l, 75);
  req(l, rd(13, PODCRP_REG_CAL));
  n = next_pkt(l, b);
  check(n == 72 && b[2] == 0 && b[8] == 0x30, "CAL at MTU 75: OK, 72 B");
}

static void t_push_gating(void) {
  printf("-- PUSH gating (docs/ble.md 5)\n");
  uint8_t fr[300], st[20];
  uint32_t n16 = data_frame(fr, 16);
  status_frame(st);
  podcrp_link_t *l = fresh(PODCRP_BLE);
  podcrp_set_mtu(l, 247);
  check(!podcrp_push(l, PODCRP_REG_DATA, fr, n16), "push refused before a session");
  open_session(l, 247);
  check(!podcrp_push(l, PODCRP_REG_DATA, fr, n16) && !podcrp_pushes_on(l), "push refused at PUSH 0");
  expect_none(l, "and nothing sent");
  req(l, wr(5, PODCRP_REG_PUSH, "01"));
  expect(l, resp(5, 0, PODCRP_REG_PUSH, ""), "WRITE PUSH 1 answered OK = 0C 05 00 FF 06000100");
  check(podcrp_pushes_on(l), "pushes on");
  req(l, rd(6, PODCRP_REG_PUSH));
  expect(l, resp(6, 0, PODCRP_REG_PUSH, "01"), "READ PUSH = 1");
  req(l, wr(7, PODCRP_REG_PUSH, "02"));
  expect(l, resp(7, 3, PODCRP_REG_PUSH, ""), "WRITE PUSH 2 = INVALID_VALUE");
  req(l, wr(8, PODCRP_REG_PUSH, "01 00"));
  expect(l, resp(8, 3, PODCRP_REG_PUSH, ""), "WRITE PUSH with 2 bytes = INVALID_VALUE");
  check(podcrp_pushes_on(l), "PUSH still 1 after the bad writes");

  check(podcrp_push(l, PODCRP_REG_DATA, fr, n16), "DATA push (n 16, 238 B) accepted at MTU 247");
  expect(l, hx("04 00 %s %s", rg(PODCRP_REG_DATA), tohex(fr, n16)), "its bytes: 04 00 04000100 + the frame (244 B = 247 - 3)");
  check(podcrp_push(l, PODCRP_REG_STATUS, st, 20), "STATUS push accepted");
  expect(l, hx("04 00 %s %s", rg(PODCRP_REG_STATUS), tohex(st, 20)), "its bytes: 04 00 02000100 + the frame");

  F.slots = 0;
  check(!podcrp_push(l, PODCRP_REG_DATA, fr, n16), "push refused with no free slot");
  expect_none(l, "and nothing sent");
  req(l, C_READ_INFO);                      // no slot: the RESPONSE waits in the spool
  F.slots = -1;
  check(!podcrp_push(l, PODCRP_REG_DATA, fr, n16), "push refused behind a spooled RESPONSE, slot free");
  expect_none(l, "and the push didn't drain the spool either");
  podcrp_spool_drain(l);
  expect(l, resp(1, 0, PODCRP_REG_INFO, tohex(F.info, 32)), "the drain sends the spooled RESPONSE");
  check(podcrp_push(l, PODCRP_REG_DATA, fr, n16), "then the push goes");
  skip_pkt(l);

  uint8_t f4[100], f5[100];
  uint32_t n4 = data_frame(f4, 4), n5 = data_frame(f5, 5);
  podcrp_set_mtu(l, 79);
  check(!podcrp_push(l, PODCRP_REG_DATA, f5, n5), "MTU 79: a 5-sample frame (6 + 84 > 76) refused");
  check(podcrp_push(l, PODCRP_REG_DATA, f4, n4), "MTU 79: a 4-sample frame (6 + 70 = 76) accepted");
  expect(l, hx("04 00 %s %s", rg(PODCRP_REG_DATA), tohex(f4, n4)), "its bytes, 76 B");
  podcrp_set_mtu(l, 28);
  check(!podcrp_push(l, PODCRP_REG_STATUS, st, 20), "MTU 28: STATUS push (26 B > 25) refused");
  podcrp_set_mtu(l, 29);
  check(podcrp_push(l, PODCRP_REG_STATUS, st, 20), "MTU 29: STATUS push (26 B) accepted");
  skip_pkt(l);
  expect_none(l, "refused pushes sent nothing");

  podcrp_set_mtu(l, 247);
  req(l, wr(0, PODCRP_REG_PUSH, "00"));
  expect_none(l, "a txn-0 WRITE PUSH 0 gets no RESPONSE");
  check(!podcrp_pushes_on(l), "but turns pushes off");
}

static void t_spool_fifo(void) {
  printf("-- the BLE spool: FIFO, before pushes\n");
  uint8_t st[20];
  status_frame(st);
  podcrp_link_t *l = fresh(PODCRP_BLE);
  open_session(l, 247);
  push_on(l);
  F.slots = 0;
  feed(l, rd(10, PODCRP_REG_INFO));
  feed(l, rd(11, PODCRP_REG_STATUS));
  feed(l, rd(12, PODCRP_REG_CAL));
  pass(l);
  expect_none(l, "no slot: three RESPONSEs wait, nothing sent");
  check(L.u.ble.n == 3 && !podcrp_spool_empty(l), "three packets spooled");
  check(!podcrp_push(l, PODCRP_REG_STATUS, st, 20), "push refused while they wait");
  F.slots = 2;
  podcrp_spool_drain(l);
  uint8_t b[300];
  next_pkt(l, b);
  check(b[0] == 0x0C && b[1] == 10, "two slots: txn 10 goes first");
  next_pkt(l, b);
  check(b[0] == 0x0C && b[1] == 11, "then txn 11");
  check(!podcrp_push(l, PODCRP_REG_STATUS, st, 20), "push still refused: txn 12 waits");
  F.slots = -1;
  podcrp_spool_drain(l);
  next_pkt(l, b);
  check(b[1] == 12 && b[8] == 0x30, "then txn 12 (CAL)");
  check(podcrp_push(l, PODCRP_REG_STATUS, st, 20), "and only then the push");
  expect(l, hx("04 00 %s %s", rg(PODCRP_REG_STATUS), tohex(st, 20)), "which comes last on the wire");

  F.slots = 0;
  req(l, rd(40, PODCRP_REG_PROTO));
  F.slots = -1;
  req(l, rd(41, PODCRP_REG_PROTO));
  expect(l, resp(40, 0, PODCRP_REG_PROTO, "01"), "a pass drains the spool before it processes: txn 40 first");
  expect(l, resp(41, 0, PODCRP_REG_PROTO, "01"), "then the new answer, txn 41");

  F.slots = 0;
  for (int t = 20; t < 25; t++) feed(l, rd(t, PODCRP_REG_PROTO));
  pass(l);
  check(L.u.ble.n == 4 && L.tx_drops == 1, "a full spool (4) drops the fifth packet and counts it (TX_DROPS %u)",
        (unsigned)L.tx_drops);
  F.slots = -1;
  podcrp_spool_drain(l);
  bool order = true;
  for (int t = 20; t < 24; t++) {
    next_pkt(l, b);
    order = order && b[1] == t;
  }
  check(order, "the four spooled go out in order (20..23)");
  req(l, rd(26, PODCRP_REG_TX_DROPS));
  expect(l, resp(26, 0, PODCRP_REG_TX_DROPS, "01 00 00 00"), "TX_DROPS reads 1");
  podcrp_set_mtu(l, 23);
  req(l, hx("0A 1E %s", rg(PODCRP_REG_INFO)));   // QUERY_REG INFO: 8 + 12 + 4 = 24 B > 20
  expect_none(l, "MTU 23: a QUERY_REG answer that doesn't fit is dropped");
  check(L.tx_drops == 2, "and counted (TX_DROPS 2)");
}

static void t_nop(void) {
  printf("-- the heartbeat NOP: straight out or not at all\n");
  podcrp_link_t *l = fresh(PODCRP_BLE);
  open_session(l, 247);                     // the HELLO reply went at 1000
  now = 1999;
  pass(l);
  expect_none(l, "no NOP 999 ms after the last send");
  now = 2000;
  pass(l);
  expect(l, C_NOP, "a NOP 1000 ms after the last send");
  int nops = 0;
  for (now = 2100; now <= 3000; now += 100) {
    pass(l);
    uint8_t b[64];
    while (next_pkt(l, b)) nops += now == 3000 ? 1 : 100;
  }
  check(nops == 1, "one NOP per second, not one per pass");
  F.slots = 0;
  now = 4000;
  pass(l);
  check(L.u.ble.n == 0 && L.tx_drops == 0, "no slot: the NOP is dropped, not spooled, not counted");
  F.slots = -1;
  now = 4500;
  pass(l);
  expect_none(l, "and it counts as sent (no NOP at 4500)");
  now = 5000;
  pass(l);
  expect(l, C_NOP, "the next one at 5000");
  F.slots = 0;
  now = 5100;
  req(l, C_READ_INFO);                      // spooled at 5100
  now = 6100;
  pass(l);
  check(L.u.ble.n == 1 && L.tx_drops == 0, "behind a spooled packet the NOP is dropped, the spool unchanged");
  F.slots = -1;
  pass(l);
  check(next_status(l) == 0, "the spooled RESPONSE goes once a slot frees");
  expect_none(l, "and no stale NOP after it");

  l = fresh(PODCRP_USB);
  open_session(l, 0);
  now = 2000;
  pass(l);
  expect(l, C_NOP, "USB: a NOP after 1 s, framed 01 00 00");
  F.usb_room = 0;
  req(l, C_READ_INFO);                      // stuck in the spool: 2 + 40 B
  now = 3000;
  pass(l);
  check(L.u.usb.used == 42, "USB: a NOP behind queued bytes is dropped (spool %u B)", (unsigned)L.u.usb.used);
}

static void t_ctrl(void) {
  printf("-- CTRL\n");
  podcrp_link_t *l = fresh(PODCRP_BLE);
  open_session(l, 247);
  push_on(l);
  F.ctrl_answer = CRP_STATUS_OK;
  req(l, C_WRITE_CTRL);
  uint8_t want[4] = {0x02, 0x01, 0x58, 0x02};
  check(F.ctrl_calls == 1 && F.ctrl_len == 4 && memcmp(F.ctrl, want, 4) == 0, "WRITE CTRL 02 01 58 02 reaches the hook byte for byte");
  expect(l, resp(2, 0, PODCRP_REG_CTRL, ""), "answered OK = 0C 02 00 FF 05000100");
  F.ctrl_answer = CRP_STATUS_INVALID_VALUE;
  req(l, wr(3, PODCRP_REG_CTRL, "7F"));
  expect(l, resp(3, 3, PODCRP_REG_CTRL, ""), "the hook's INVALID_VALUE is the answer (unknown op)");
  F.ctrl_answer = CRP_STATUS_BUSY;
  req(l, wr(4, PODCRP_REG_CTRL, "03"));
  expect(l, resp(4, 8, PODCRP_REG_CTRL, ""), "the hook's BUSY is the answer (sleep prologue)");
  int calls = F.ctrl_calls;
  req(l, wr(5, PODCRP_REG_CTRL, "01 01 00 00 00 00 00 00 00 00 00"));
  expect(l, resp(5, 3, PODCRP_REG_CTRL, ""), "an 11-byte CTRL = INVALID_VALUE");
  req(l, wr(6, PODCRP_REG_CTRL, ""));
  expect(l, resp(6, 3, PODCRP_REG_CTRL, ""), "an empty CTRL = INVALID_VALUE");
  check(F.ctrl_calls == calls, "neither reached the hook");
  F.ctrl_answer = CRP_STATUS_OK;
  F.push_in_hook = true;
  F.push_in_hook_ok = true;
  req(l, wr(7, PODCRP_REG_CTRL, "03"));
  check(!F.push_in_hook_ok, "a push from inside the CTRL hook is refused");
  expect(l, resp(7, 0, PODCRP_REG_CTRL, ""), "so the RESPONSE is the next packet");
  uint8_t st[20];
  status_frame(st);
  check(podcrp_push(l, PODCRP_REG_STATUS, st, 20), "and the answering STATUS goes after it, from the pass");
  skip_pkt(l);
}

static void t_errors(void) {
  printf("-- UNKNOWN_REG and ACCESS_DENIED\n");
  podcrp_link_t *l = fresh(PODCRP_BLE);
  open_session(l, 247);
  struct { const char *what; bool write; uint32_t reg; int st; } cases[] = {
    {"READ 0x00010007 (past PUSH)", false, 0x00010007u, 1},
    {"READ 0x00050001 (no such group)", false, 0x00050001u, 1},
    {"READ 0x00020000 (a group base without a register)", false, 0x00020000u, 1},
    {"READ 0x0002000F (past NAME)", false, 0x0002000Fu, 1},
    {"READ DATA (pushed only)", false, PODCRP_REG_DATA, 2},
    {"READ CTRL (write only)", false, PODCRP_REG_CTRL, 2},
    {"READ BENCH on Bluetooth", false, PODCRP_REG_BENCH, 1},
    {"WRITE INFO", true, PODCRP_REG_INFO, 2},
    {"WRITE STATUS", true, PODCRP_REG_STATUS, 2},
    {"WRITE DATA", true, PODCRP_REG_DATA, 2},
    {"WRITE PROTO", true, PODCRP_REG_PROTO, 2},
    {"WRITE HEAD (a state mirror)", true, PODCRP_REG_HEAD, 2},
    {"WRITE KIND (a link register)", true, PODCRP_REG_KIND, 2},
    {"WRITE 0x00010009", true, 0x00010009u, 1},
    {"WRITE 0x00060001 (no such group)", true, 0x00060001u, 1},
    {"WRITE BENCH 'w' on Bluetooth", true, PODCRP_REG_BENCH, 1},
  };
  for (size_t i = 0; i < sizeof cases / sizeof cases[0]; i++) {
    int txn = 0x30 + (int)i;
    req(l, cases[i].write ? wr(txn, cases[i].reg, "77") : rd(txn, cases[i].reg));
    expect(l, resp(txn, cases[i].st, cases[i].reg, ""), "%s = %s", cases[i].what,
           cases[i].st == 1 ? "UNKNOWN_REG" : "ACCESS_DENIED");
  }
  F.no_stack_free = true;
  req(l, rd(0x50, PODCRP_REG_STACK_FREE));
  expect(l, resp(0x50, 1, PODCRP_REG_STACK_FREE, ""), "a register the hook has no value for = UNKNOWN_REG");

  req(l, rd(0x51, PODCRP_REG_PROTO));
  expect(l, resp(0x51, 0, PODCRP_REG_PROTO, "01"), "PROTO = 1 (from the glue)");
  req(l, rd(0x52, PODCRP_REG_KIND));
  expect(l, resp(0x52, 0, PODCRP_REG_KIND, "01"), "KIND = 1 on Bluetooth");
  F.head = 0x01020304;
  req(l, rd(0x53, PODCRP_REG_HEAD));
  expect(l, resp(0x53, 0, PODCRP_REG_HEAD, "04 03 02 01"), "HEAD from the hook, u32 little-endian");
  req(l, rd(0x54, PODCRP_REG_NAME));
  expect(l, resp(0x54, 0, PODCRP_REG_NAME, str_hex("BarPod-TEST")), "NAME, a string of its own length");
  req(l, rd(0x55, PODCRP_REG_FLAGS));
  expect(l, resp(0x55, 0, PODCRP_REG_FLAGS, "00"), "FLAGS, one byte");
  req(l, rd(0x56, PODCRP_REG_BATT_V));
  expect(l, resp(0x56, 0, PODCRP_REG_BATT_V, "00 00 00 00"), "BATT_V, four bytes");

  l = fresh(PODCRP_USB);
  open_session(l, 0);
  req(l, rd(0x60, PODCRP_REG_KIND));
  expect(l, resp(0x60, 0, PODCRP_REG_KIND, "02"), "USB: KIND = 2");
  req(l, rd(0x61, PODCRP_REG_MTU));
  expect(l, resp(0x61, 0, PODCRP_REG_MTU, "03 04"), "USB: MTU = 1027 (packets up to 1024)");
  req(l, wr(0x62, PODCRP_REG_BENCH, "77"));
  expect(l, resp(0x62, 0, PODCRP_REG_BENCH, ""), "USB: BENCH 'w' answered by its hook");
  check(F.bench_calls == 1 && F.bench_c == 'w', "the hook got 'w'");
  const char *bad[] = {"78", "00", "77 77", ""};
  for (int i = 0; i < 4; i++) {
    req(l, wr(0x63 + i, PODCRP_REG_BENCH, bad[i]));
    expect(l, resp(0x63 + i, 3, PODCRP_REG_BENCH, ""), "USB: BENCH [%s] = INVALID_VALUE", bad[i]);
  }
  check(F.bench_calls == 1, "none of those reached the hook");
  req(l, rd(0x67, PODCRP_REG_BENCH));
  expect(l, resp(0x67, 2, PODCRP_REG_BENCH, ""), "USB: READ BENCH = ACCESS_DENIED");
}

static const char *G1[] = {"INFO", "STATUS", "CAL", "DATA", "CTRL", "PUSH"};
static const int G1T[] = {0x0B, 0x0B, 0x0B, 0x0B, 0x0B, 0x01};
static const char *G2[] = {"BATTERY_MV", "FLAGS", "HEAD", "TICKS", "BOOT_ID", "SERIAL", "FW_BUILD",
                           "WAKE_REASON", "SLEEP_REASON", "LOST", "MISSED_DRDY", "BATT_V", "RESETREAS", "NAME"};
static const int G2T[] = {0x02, 0x01, 0x03, 0x03, 0x03, 0x03, 0x02, 0x01, 0x01, 0x03, 0x03, 0x07, 0x03, 0x0A};
static const char *G3[] = {"KIND", "MTU", "N_MAX", "INTERVAL", "PHY", "DL", "FRAMES", "TX_DROPS", "RX_DROPS", "STACK_FREE"};
static const int G3T[] = {0x01, 0x02, 0x01, 0x02, 0x01, 0x02, 0x03, 0x03, 0x03, 0x03};
static const char *G4[] = {"BENCH"};
static const int G4T[] = {0x01};

// QUERY_REG offsets 1..count of a group, as Ansync's discovery walks it: names and types per
// docs/ble.md 2.3 (typed in here a second time, so a typo in podcrp.c's table shows), then a miss.
static void walk(podcrp_link_t *l, uint32_t group, const char **names, const int *types, int count, const char *label) {
  int good = 0;
  for (int k = 1; k <= count; k++) {
    req(l, hx("0A 21 %s", rg(group + (uint32_t)k)));
    uint8_t b[300];
    size_t n = next_pkt(l, b);
    size_t nl = strlen(names[k - 1]);
    good += n == 20 + nl && b[2] == 0 && b[8] == types[k - 1] && (size_t)b[19] == nl &&
            memcmp(b + 20, names[k - 1], nl) == 0;
  }
  check(good == count, "discovery: %s, offsets 1..%d named and typed as ble.md 2.3 (%d/%d)", label, count, good, count);
  req(l, hx("0A 22 %s", rg(group + (uint32_t)count + 1)));
  expect(l, hx("0C 22 01 FF %s", rg(group + (uint32_t)count + 1)), "discovery: %s, offset %d is a miss (UNKNOWN_REG)", label, count + 1);
}

static void t_discovery(void) {
  printf("-- discovery (QUERY_DEVICE, QUERY_GROUP, QUERY_REG)\n");
  podcrp_link_t *l = fresh(PODCRP_BLE);
  open_session(l, 247);
  req(l, "1A 01 00 00 00 00");
  expect(l, hx("0C 01 00 FF 00 00 00 00 00 00 00 00 01 03 00 00 00 %s", str_hex("BarPod-TEST")),
         "QUERY_DEVICE: mask 0, flags PUSH, 3 groups, the name");
  req(l, hx("0B 02 %s", rg(PODCRP_GROUP_STREAM)));
  expect(l, hx("0C 02 00 FF %s 00 00 FF FF 03 06 00 00 00", rg(PODCRP_GROUP_STREAM)), "QUERY_GROUP 0x0001: R and W, 6 registers past the base");
  req(l, hx("0B 03 %s", rg(PODCRP_GROUP_STATE)));
  expect(l, hx("0C 03 00 FF %s 00 00 FF FF 01 0E 00 00 00", rg(PODCRP_GROUP_STATE)), "QUERY_GROUP 0x0002: read only, 14 registers");
  req(l, hx("0B 04 %s", rg(PODCRP_GROUP_LINK)));
  expect(l, hx("0C 04 00 FF %s 00 00 FF FF 01 0A 00 00 00", rg(PODCRP_GROUP_LINK)), "QUERY_GROUP 0x0003: read only, 10 registers");
  req(l, hx("0B 05 %s", rg(PODCRP_GROUP_BENCH)));
  expect(l, hx("0C 05 00 FF %s 00 00 00 00 00 00 00 00 00", rg(PODCRP_GROUP_BENCH)), "QUERY_GROUP 0x0004 on Bluetooth: none (the engine's empty answer)");
  walk(l, PODCRP_GROUP_STREAM, G1, G1T, 6, "group 0x0001");
  walk(l, PODCRP_GROUP_STATE, G2, G2T, 14, "group 0x0002");
  walk(l, PODCRP_GROUP_LINK, G3, G3T, 10, "group 0x0003");
  req(l, hx("0A 06 %s", rg(PODCRP_REG_INFO)));
  expect(l, hx("0C 06 00 FF %s 0B 01 00 20 00 00 00 00 00 00 00 04 %s", rg(PODCRP_REG_INFO), str_hex("INFO")),
         "QUERY_REG INFO: blob, R, size 32, \"INFO\"");
  req(l, hx("0A 07 %s", rg(PODCRP_REG_DATA)));
  expect(l, hx("0C 07 00 FF %s 0B 00 04 00 00 00 00 00 00 00 00 04 %s", rg(PODCRP_REG_DATA), str_hex("DATA")),
         "QUERY_REG DATA: no access, pushed, variable size");
  req(l, hx("0A 08 %s", rg(PODCRP_REG_PROTO)));
  expect(l, hx("0C 08 00 FF %s 01 01 00 01 00 00 00 00 00 00 00 05 %s", rg(PODCRP_REG_PROTO), str_hex("PROTO")),
         "QUERY_REG PROTO (the group base) answered too");
  req(l, hx("0A 09 %s", rg(PODCRP_REG_BENCH)));
  expect(l, hx("0C 09 01 FF %s", rg(PODCRP_REG_BENCH)), "QUERY_REG BENCH on Bluetooth = UNKNOWN_REG");

  l = fresh(PODCRP_USB);
  open_session(l, 0);
  req(l, "1A 01 00 00 00 00");
  expect(l, hx("0C 01 00 FF 00 00 00 00 00 00 00 00 01 04 00 00 00 %s", str_hex("BarPod-TEST")), "USB QUERY_DEVICE: 4 groups");
  req(l, hx("0B 02 %s", rg(PODCRP_GROUP_BENCH)));
  expect(l, hx("0C 02 00 FF %s 00 00 FF FF 02 01 00 00 00", rg(PODCRP_GROUP_BENCH)), "USB QUERY_GROUP 0x0004: write only, 1 register");
  walk(l, PODCRP_GROUP_BENCH, G4, G4T, 1, "USB group 0x0004");
}

static void t_deframer(void) {
  printf("-- the USB level 1 deframer\n");
  uint8_t hello[12] = {0x0A, 0x00};
  unhex(C_HELLO, hello + 2, 10);
  podcrp_link_t *l = fresh(PODCRP_USB);
  podcrp_rx_bytes(l, (const uint8_t *)"a,b\n", 4);
  podcrp_rx_bytes(l, hello, 12);
  pass(l);
  expect(l, P_HELLO_USB, "text before HELLO (\"a,b\\n\" then 0A 00 01 0B ...): the HELLO is found");
  check(L.rx_drops == 0, "and the text isn't counted as a dropped packet");

  l = fresh(PODCRP_USB);
  bool early = false;
  for (int i = 0; i < 12; i++) {
    podcrp_rx_bytes(l, hello + i, 1);
    pass(l);
    if (i < 11) early = early || F.out_len;
  }
  check(!early, "a HELLO fed one byte per pass: nothing before its last byte");
  expect(l, P_HELLO_USB, "and the reply after it");

  const char *junk[] = {"00 00", "FF FF FF", "00 0A", "01 04"};
  const char *what[] = {"length 0", "garbage FF FF FF", "00 0A (length 2560)", "length 1025"};
  for (int i = 0; i < 4; i++) {
    l = fresh(PODCRP_USB);
    uint8_t b[16];
    size_t n = unhex(junk[i], b, sizeof b);
    podcrp_rx_bytes(l, b, (uint32_t)n);
    podcrp_rx_bytes(l, hello, 12);
    pass(l);
    expect(l, P_HELLO_USB, "%s before HELLO: drop a byte, rescan, the HELLO is found", what[i]);
  }

  l = fresh(PODCRP_USB);
  open_session(l, 0);
  uint8_t big[2 + 1024];
  memset(big, 0, sizeof big);
  big[0] = 100;
  podcrp_rx_bytes(l, big, 2 + 100);
  req(l, C_READ_INFO);
  check(L.rx_drops == 1, "a 100 B packet (too long for the RX queue) is read through and counted");
  check(next_status(l) == 0, "and the READ after it still answered");
  big[0] = 0x00;
  big[1] = 0x04;                            // 1024: the longest legal length
  podcrp_rx_bytes(l, big, 2 + 1024);
  req(l, C_READ_INFO);
  check(L.rx_drops == 2 && next_status(l) == 0, "a 1024 B packet likewise");

  uint8_t p64[64], p65[65];
  memset(p64, 0, sizeof p64);
  memset(p65, 0, sizeof p65);
  uint8_t hdr[6] = {0x04, 0x31, 0x05, 0x00, 0x01, 0x00};   // WRITE txn 0x31 to CTRL
  memcpy(p64, hdr, 6);
  memcpy(p65, hdr, 6);
  p65[1] = 0x32;
  feed_bytes(l, p64, 64);
  pass(l);
  expect(l, resp(0x31, 3, PODCRP_REG_CTRL, ""), "a 64 B packet reaches the engine (a 58 B CTRL: INVALID_VALUE)");
  feed_bytes(l, p65, 65);
  pass(l);
  expect_none(l, "a 65 B packet doesn't");
  check(L.rx_drops == 3, "and is counted (RX_DROPS 3)");

  uint8_t burst[9 * 8];
  for (int i = 0; i < 9; i++) {
    uint8_t *p = burst + 8 * i;
    p[0] = 6;
    p[1] = 0;
    unhex(rd(0x40 + i, PODCRP_REG_PROTO), p + 2, 6);
  }
  podcrp_rx_bytes(l, burst, sizeof burst);
  pass(l);
  int answered = 0;
  while (next_status(l) == 0) answered++;
  check(answered == 8 && L.rx_drops == 4, "9 packets in one burst: 8 queued and answered, 1 dropped and counted");

  l = fresh(PODCRP_BLE);
  uint8_t z[65] = {0};
  check(!podcrp_rx_packet(l, z, 0) && !podcrp_rx_packet(l, z, 65), "BLE: an empty write and a 65 B write are dropped");
  int queued = 0;
  for (int i = 0; i < 9; i++) queued += podcrp_rx_packet(l, z, 1);
  check(queued == 8 && L.rx_drops == 3, "BLE: the queue holds 8, the 9th is dropped (RX_DROPS 3)");
}

static void t_usb_reserve(void) {
  printf("-- the USB spool: pushes leave 128 B for RESPONSEs\n");
  uint8_t fr[300], f190[300], f191[300];
  uint32_t n16 = data_frame(fr, 16);
  memset(f190, 0x5A, sizeof f190);
  memset(f191, 0x5B, sizeof f191);
  podcrp_link_t *l = fresh(PODCRP_USB);
  open_session(l, 0);
  check(!podcrp_push(l, PODCRP_REG_DATA, fr, n16), "USB: push refused at PUSH 0");
  push_on(l);
  uint32_t tail0 = L.u.usb.tail;            // the HELLO reply and the PUSH answer already went
  F.usb_room = 0;                           // the host stops reading
  int ok = 0;
  while (ok < 20 && podcrp_push(l, PODCRP_REG_DATA, fr, n16)) ok++;
  check(ok == 7 && L.u.usb.used == 1722, "7 pushes of 2 + 244 B fit, the 8th doesn't (%d, %u B queued)", ok, (unsigned)L.u.usb.used);
  check(!podcrp_push(l, PODCRP_REG_DATA, f191, 191), "326 B free: a push of 2 + 197 B is refused (327 with the 128 B)");
  check(podcrp_push(l, PODCRP_REG_DATA, f190, 190), "a push of 2 + 196 B goes (exactly 326 with the 128 B)");
  check(!podcrp_push(l, PODCRP_REG_DATA, fr, 1), "128 B free: even a 1 B push is refused");
  req(l, C_READ_INFO);
  check(L.u.usb.used == 1962, "but a RESPONSE (2 + 40 B) still goes in");
  req(l, rd(9, PODCRP_REG_CAL));
  check(L.u.usb.used == 2036 && L.tx_drops == 0, "and a CAL answer (2 + 72 B) too");
  req(l, rd(10, PODCRP_REG_CAL));
  check(L.u.usb.used == 2036 && L.tx_drops == 1, "another doesn't fit: dropped and counted");
  F.usb_room = -1;
  podcrp_spool_drain(l);
  bool order = true;
  uint8_t b[1100];
  for (int i = 0; i < 7; i++) order = order && next_pkt(l, b) == 6 + n16 && memcmp(b + 6, fr, n16) == 0;
  order = order && next_pkt(l, b) == 196 && b[6] == 0x5A;
  order = order && next_pkt(l, b) == 40 && b[1] == 1;
  order = order && next_pkt(l, b) == 72 && b[1] == 9;
  check(order && podcrp_spool_empty(l), "the port reads again: everything comes out whole, in order");
  check(tail0 + 2036 > PODCRP_USB_SPOOL && L.u.usb.tail == (tail0 + 2036) % PODCRP_USB_SPOOL,
        "and that fill wrapped the ring's end (tail %u to %u)", (unsigned)tail0, (unsigned)L.u.usb.tail);
  while (L.u.usb.tail <= PODCRP_USB_SPOOL - (2 + 6 + n16)) {   // drained at once: walk the tail up
    podcrp_push(l, PODCRP_REG_DATA, fr, n16);
    skip_pkt(l);
  }
  uint32_t tail1 = L.u.usb.tail;
  check(podcrp_push(l, PODCRP_REG_DATA, fr, n16) && L.u.usb.tail < tail1, "a push that starts %u B before the end",
        (unsigned)(PODCRP_USB_SPOOL - tail1));
  expect(l, hx("04 00 %s %s", rg(PODCRP_REG_DATA), tohex(fr, n16)), "comes out intact");
  F.usb_room = 5;
  check(podcrp_push(l, PODCRP_REG_DATA, fr, n16), "a push the port takes 5 B of");
  F.usb_room = -1;
  podcrp_spool_drain(l);
  expect(l, hx("04 00 %s %s", rg(PODCRP_REG_DATA), tohex(fr, n16)), "the rest follows on the next drain, intact");
}

static void t_session_end(void) {
  printf("-- the end of a session (docs/ble.md 2.2)\n");
  uint8_t st[20];
  status_frame(st);
  podcrp_link_t *l = fresh(PODCRP_BLE);
  open_session(l, 247);
  push_on(l);
  req(l, C_BYE);
  check(F.ends == 1 && F.end_why == PODCRP_END_BYE && !podcrp_in_session(l), "BYE: on_session_end(BYE) once, no session");
  check(!podcrp_pushes_on(l) && !podcrp_push(l, PODCRP_REG_STATUS, st, 20), "pushes off and refused");
  req(l, C_READ_INFO);
  expect_none(l, "a READ after BYE is ignored");
  req(l, C_HELLO);
  skip_pkt(l);
  req(l, rd(2, PODCRP_REG_PUSH));
  expect(l, resp(2, 0, PODCRP_REG_PUSH, "00"), "the next session starts with PUSH 0");
  push_on(l);
  req(l, C_END);
  check(F.ends == 2 && F.end_why == PODCRP_END_END && !podcrp_in_session(l) && !podcrp_pushes_on(l),
        "END 0xFF: on_session_end(END) once, pushes off");
  req(l, C_HELLO);
  skip_pkt(l);
  push_on(l);
  int starts = F.starts;
  req(l, C_HELLO);
  expect(l, P_HELLO_BLE, "a HELLO mid-session is answered");
  check(F.ends == 3 && F.end_why == PODCRP_END_HELLO && F.starts == starts + 1 && podcrp_in_session(l),
        "and ends the old session (once, HELLO) before the new one");
  req(l, rd(3, PODCRP_REG_PUSH));
  expect(l, resp(3, 0, PODCRP_REG_PUSH, "00"), "PUSH back to 0");
  push_on(l);
  req(l, "16 00");
  check(F.ends == 3 && podcrp_pushes_on(l), "END of channel 0 isn't the session: nothing ends");

  F.slots = 0;
  req(l, C_READ_INFO);                      // spooled
  feed(l, C_READ_INFO);                     // queued, not processed
  podcrp_reset(l);                          // the link went
  F.slots = -1;
  check(F.ends == 3 && !podcrp_in_session(l) && !podcrp_pushes_on(l), "podcrp_reset (a disconnect): no hook, no session, pushes off");
  check(podcrp_spool_empty(l), "the spool is cleared");
  pass(l);
  expect_none(l, "the queued READ is gone");
  req(l, C_BYE);
  check(F.ends == 3, "a BYE without a session ends nothing");
  req(l, C_HELLO);
  skip_pkt(l);
  req(l, rd(4, PODCRP_REG_MTU));
  expect(l, resp(4, 0, PODCRP_REG_MTU, "17 00"), "a new link starts at MTU 23");

  // A session end on a live link empties the spool, as a disconnect does (ble.md 5): nothing the
  // old session left there goes out after it (ble.md 2.2: no session, nothing sent), nor ahead of
  // the next HELLO's answer. The link itself stays, so the MTU does too.
  l = fresh(PODCRP_BLE);
  open_session(l, 247);
  push_on(l);
  F.slots = 0;                              // every notification slot busy
  feed(l, rd(5, PODCRP_REG_STATUS));
  feed(l, C_BYE);
  pass(l);                                  // both in one podcrp_process_all
  check(F.ends == 1 && F.end_why == PODCRP_END_BYE && podcrp_spool_empty(l),
        "BYE behind a spooled RESPONSE: the session ends and the spool is emptied");
  F.slots = -1;
  pass(l);
  expect_none(l, "nothing goes out once the slots free up");
  req(l, C_HELLO);
  expect(l, P_HELLO_BLE, "the next HELLO's answer is the first thing out");
  push_on(l);
  F.slots = 0;
  feed(l, C_READ_INFO);
  feed(l, C_END);
  pass(l);
  check(F.ends == 2 && F.end_why == PODCRP_END_END && podcrp_spool_empty(l),
        "END 0xFF behind a spooled RESPONSE: the spool is emptied too");
  F.slots = -1;
  pass(l);
  expect_none(l, "and nothing goes out after it");
  req(l, C_HELLO);
  skip_pkt(l);
  req(l, C_READ_INFO);
  check(next_status(l) == CRP_STATUS_OK && F.info_reads == 2, "the link keeps its MTU: INFO still reads OK");
  F.slots = 0;
  req(l, C_HELLO);
  check(F.ends == 3 && F.end_why == PODCRP_END_HELLO && !podcrp_spool_empty(l),
        "a HELLO mid-session with no slot free: its answer stays spooled");
  F.slots = -1;
  pass(l);
  expect(l, P_HELLO_BLE, "and goes out once a slot frees");

  l = fresh(PODCRP_USB);
  open_session(l, 0);
  push_on(l);
  req(l, C_BYE);
  check(F.ends == 1 && F.end_why == PODCRP_END_BYE && !podcrp_pushes_on(l), "USB: BYE ends the session once (BYE)");
  req(l, C_HELLO);
  skip_pkt(l);
  F.usb_room = 0;                           // the host stops reading
  feed(l, C_READ_INFO);
  feed(l, C_BYE);
  pass(l);
  check(F.ends == 2 && !podcrp_spool_empty(l), "USB: BYE behind a spooled RESPONSE keeps its bytes (no half packet)");
  F.usb_room = -1;
  pass(l);
  check(next_status(l) == CRP_STATUS_OK, "USB: the RESPONSE then goes out whole");
  expect_none(l, "USB: nothing goes out after it");
  req(l, C_HELLO);
  skip_pkt(l);
  F.usb_room = 0;
  req(l, C_READ_INFO);                      // spooled, and the last packet received
  now += 3000;
  podcrp_tick(l, now);
  check(F.ends == 3 && F.end_why == PODCRP_END_SILENCE && !podcrp_spool_empty(l),
        "USB: 3 s of silence behind a spooled RESPONSE keeps it too");
  F.usb_room = -1;
  pass(l);
  check(next_status(l) == CRP_STATUS_OK, "USB: and it goes out whole");
  expect_none(l, "USB: nothing goes out after the silence");
}

static void t_liveness(void) {
  printf("-- liveness: ignored on Bluetooth, 3 s on USB\n");
  podcrp_link_t *l = fresh(PODCRP_BLE);
  open_session(l, 247);
  for (now = 1100; now <= 11000; now += 100) pass(l);
  check(podcrp_in_session(l) && F.ends == 0, "Bluetooth: 10 s without receiving, still in session");

  l = fresh(PODCRP_USB);
  open_session(l, 0);                       // received at 1000
  push_on(l);
  for (now = 1100; now <= 3999; now += 1) podcrp_tick(l, now);
  check(podcrp_in_session(l) && F.ends == 0, "USB: still in session 2999 ms after the last packet");
  now = 4000;
  podcrp_tick(l, now);
  check(!podcrp_in_session(l) && F.ends == 1 && F.end_why == PODCRP_END_SILENCE && !podcrp_pushes_on(l),
        "USB: 3000 ms without receiving ends it (SILENCE), pushes off");
  for (now = 4001; now <= 10000; now += 7) podcrp_tick(l, now);
  check(F.ends == 1, "on_session_end once, not once per tick");
  req(l, C_HELLO);
  skip_pkt(l);
  check(podcrp_in_session(l), "a new HELLO opens a new session");

  l = fresh(PODCRP_USB);
  open_session(l, 0);
  for (now = 2000; now <= 12000; now += 1000) req(l, C_NOP);
  check(podcrp_in_session(l) && F.ends == 0, "USB: a client NOP every 1 s keeps it for 11 s");
}

static void t_log(void) {
  printf("-- LOG (USB, the # lines)\n");
  podcrp_link_t *l = fresh(PODCRP_USB);
  check(!podcrp_log(l, CRP_LOG_INFO, "pod", "boot"), "refused before a session");
  open_session(l, 0);
  now = 1500;
  pass(l);
  check(podcrp_log(l, CRP_LOG_INFO, "pod", "boot"), "taken in a session");
  expect(l, hx("18 02 %s 03 %s %s", u32h(1500), str_hex("pod"), str_hex("boot")), "LOG = 18 02 <ts 1500> 03 \"pod\" \"boot\"");
  uint8_t fr[300];
  uint32_t n16 = data_frame(fr, 16);
  push_on(l);
  F.usb_room = 0;
  while (podcrp_push(l, PODCRP_REG_DATA, fr, n16)) {}
  char m[256];
  memset(m, 'x', sizeof m);
  m[187] = 0;                               // 2 + 7 + 3 + 187 = 199 B, + 128 = 327 > 326 free
  check(!podcrp_log(l, CRP_LOG_INFO, "pod", m), "a LOG that would eat the 128 B reserve is refused");
  m[186] = 0;
  check(podcrp_log(l, CRP_LOG_INFO, "pod", m), "one that leaves exactly 128 B goes");
  l = fresh(PODCRP_BLE);
  open_session(l, 247);
  check(!podcrp_log(l, CRP_LOG_INFO, "pod", "boot"), "no LOG on Bluetooth");
}

// ================================================================================================
// The Python reference's vectors (analysis/crp.py --vectors)
// ================================================================================================
static bool json_str(const char *obj, const char *key, char *out, size_t max) {
  const char *p = strstr(obj, key);
  if (!p) return false;
  p = strchr(p + strlen(key), ':');
  if (!p) return false;
  p = strchr(p, '"');
  if (!p) return false;
  p++;
  size_t n = 0;
  while (*p && *p != '"' && n + 1 < max) out[n++] = *p++;
  out[n] = 0;
  return true;
}

static void t_vectors(const char *path) {
  printf("-- crp.py --vectors (%s)\n", path);
  FILE *fp = fopen(path, "rb");
  if (!fp) {
    check(false, "vectors: can't open %s", path);
    return;
  }
  static char js[1 << 20];
  size_t len = fread(js, 1, sizeof js - 1, fp);
  fclose(fp);
  js[len] = 0;
  enum { NV = 128 };
  static uint8_t vb[NV][1100];
  static size_t vn[NV];
  static char vname[NV][96];
  int nv = 0;
  for (char *p = js; (p = strchr(p, '{')) != NULL && nv < NV;) {
    char *end = strchr(p, '}');
    if (!end) break;
    *end = 0;
    char hexs[2400];
    if (json_str(p, "\"hex\"", hexs, sizeof hexs)) {
      if (!json_str(p, "\"name\"", vname[nv], sizeof vname[0])) snprintf(vname[nv], sizeof vname[0], "#%d", nv);
      vn[nv] = unhex(hexs, vb[nv], sizeof vb[0]);
      nv++;
    }
    p = end + 1;
  }
  check(nv > 0, "vectors: %d read", nv);

  const char *client[][2] = {{C_HELLO, "hello(0x0B, 4, 512, 0, 0)"}, {C_READ_INFO, "read(1, INFO)"},
                             {C_WRITE_CTRL, "write(2, CTRL, 02 01 58 02)"}, {C_NOP, "nop()"}, {C_BYE, "bye()"},
                             {C_HELLO_V0A, "hello(0x0A, ...)"}, {C_END, "end(0xFF)"}};
  for (size_t c = 0; c < sizeof client / sizeof client[0]; c++) {
    uint8_t b[64];
    size_t n = unhex(client[c][0], b, sizeof b);
    bool found = false;
    for (int i = 0; i < nv && !found; i++) found = vn[i] == n && memcmp(vb[i], b, n) == 0;
    check(found, "vectors: the %s this test feeds is crp.py's", client[c][1]);
  }

  int replayed = 0;
  bool pod_hello = false;
  for (int i = 0; i < nv; i++) {
    const uint8_t *v = vb[i];
    size_t n = vn[i];
    const char *vh = tohex(v, n);
    char want[3400];
    snprintf(want, sizeof want, "%s", vh);
    if (n == 10 && v[0] == 0x01 && v[6] == 0x42 && v[7] == 0x50 && v[8] == 0x4F && v[9] == 0x44) {
      uint8_t ble[10], usb[10];
      unhex(P_HELLO_BLE, ble, 10);
      unhex(P_HELLO_USB, usb, 10);
      check(!memcmp(v, ble, 10) || !memcmp(v, usb, 10), "vectors: %s is the pod's HELLO, byte for byte", vname[i]);
      pod_hello = true;
      replayed++;
    } else if (n == 9 && v[0] == 0x02 && v[5] == 0x42 && v[6] == 0x50 && v[7] == 0x4F && v[8] == 0x44) {
      podcrp_link_t *l = fresh(PODCRP_BLE);  // the pod's BYE: its answer to a HELLO that's too old
      req(l, C_HELLO_V0A);
      expect(l, want, "vectors: %s, the pod's BYE to a 0x0A HELLO byte for byte", vname[i]);
      replayed++;
    } else if (n >= 8 && v[0] == 0x0C && v[3] == 0xFF) {
      uint32_t reg = (uint32_t)v[4] | (uint32_t)v[5] << 8 | (uint32_t)v[6] << 16 | (uint32_t)v[7] << 24;
      bool frame_reg = reg == PODCRP_REG_INFO || reg == PODCRP_REG_STATUS || reg == PODCRP_REG_CAL;
      if (frame_reg && (v[2] == 0 || v[2] == CRP_STATUS_BUSY)) {   // a READ's answer
        podcrp_link_t *l = fresh(PODCRP_BLE);
        open_session(l, v[2] == 0 ? 247 : 23);
        F.frame = v + 8;
        F.frame_reg = reg;
        F.frame_len = (uint32_t)n - 8;
        req(l, rd(v[1], reg));
        expect(l, want, "vectors: %s, the pod's RESPONSE byte for byte", vname[i]);
        replayed++;
      } else if (n == 8 && (reg == PODCRP_REG_CTRL || reg == PODCRP_REG_PUSH)) {   // a WRITE's answer
        podcrp_link_t *l = fresh(PODCRP_BLE);
        open_session(l, 247);
        F.ctrl_answer = v[2];
        req(l, wr(v[1], reg, reg == PODCRP_REG_CTRL ? "03" : (v[2] == 0 ? "01" : "02")));
        expect(l, want, "vectors: %s, the pod's RESPONSE to a WRITE byte for byte", vname[i]);
        replayed++;
      } else {
        printf("info  vectors: %s, a RESPONSE this test doesn't replay\n", vname[i]);
      }
    } else if (n >= 6 && v[0] == 0x04 && v[1] == 0) {
      uint32_t reg = (uint32_t)v[2] | (uint32_t)v[3] << 8 | (uint32_t)v[4] << 16 | (uint32_t)v[5] << 24;
      if (reg != PODCRP_REG_DATA && reg != PODCRP_REG_STATUS) continue;
      podcrp_link_t *l = fresh(PODCRP_BLE);
      open_session(l, 247);
      push_on(l);
      bool took = podcrp_push(l, reg, v + 6, (uint32_t)n - 6);
      check(took, "vectors: %s, the push is accepted", vname[i]);
      expect(l, want, "vectors: %s, the pod's push byte for byte", vname[i]);
      replayed++;
    } else if (n >= 7 && v[0] == 0x18 && n >= 7u + v[6]) {
      char mod[64] = {0}, msg[1100] = {0};
      size_t ml = v[6] < 63 ? v[6] : 63, sl = n - 7 - v[6];
      if (sl > sizeof msg - 1 || memchr(v + 7, 0, n - 7)) continue;
      memcpy(mod, v + 7, ml);
      memcpy(msg, v + 7 + v[6], sl);
      podcrp_link_t *l = fresh(PODCRP_USB);
      now = (uint32_t)v[2] | (uint32_t)v[3] << 8 | (uint32_t)v[4] << 16 | (uint32_t)v[5] << 24;
      open_session(l, 0);                   // at the LOG's ts_ms, which the engine stamps
      check(podcrp_log(l, v[1], mod, msg), "vectors: %s, the LOG is taken", vname[i]);
      expect(l, want, "vectors: %s, the pod's LOG byte for byte", vname[i]);
      replayed++;
    }
  }
  check(pod_hello, "vectors: the pod's HELLO is among them");
  check(replayed > 0, "vectors: %d pod packets replayed", replayed);
}

int main(int argc, char **argv) {
  printf("podcrp host test: %u B per link (the engine %u B)\n", (unsigned)sizeof(podcrp_link_t), (unsigned)sizeof(crp_engine_t));
  t_hello();
  t_info_busy();
  t_status_read();
  t_push_gating();
  t_spool_fifo();
  t_nop();
  t_ctrl();
  t_errors();
  t_discovery();
  t_deframer();
  t_usb_reserve();
  t_session_end();
  t_liveness();
  t_log();
  if (argc > 1) t_vectors(argv[1]);
  else printf("SKIP  crp.py --vectors not given (build.sh passes them once analysis/crp.py exists)\n");
  printf("\n%d PASS, %d FAIL\n", n_pass, n_fail);
  return n_fail ? 1 : 0;
}
