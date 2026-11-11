// podcrp.c: the pod's CRP glue (docs/ble.md section 2, D45; the API and the threads: podcrp.h).
// C11, no Arduino. The vendored engine (src/crp/crp_engine.c) is the authority on CRP behavior;
// this file is its transport, the pod's register region and the session edges, per link.
//
// The send rules (ble.md 5), on both links:
//   - engine packets (the HELLO reply, RESPONSEs, discovery answers) go straight out, or wait in
//     the spool; a full spool, or a packet the link can't carry, drops them and counts TX_DROPS;
//   - a NOP only ever goes straight out (USB: into an empty spool). If it can't, it's dropped and
//     reported sent: anything else queued already proves the link alive;
//   - a push (STATUS, DATA) goes only with PUSH on, and on BLE only into an empty spool, a free
//     slot and a packet that fits; on USB only with 128 B to spare. A refused push sends nothing;
//   - a disconnect empties the spool, and on Bluetooth so do BYE and END 0xFF. A HELLO mid-session
//     doesn't, and a USB session end (BYE, END, silence) keeps its bytes: the port may hold the
//     start of a packet, and the text resumes after them.
// The region defines no bulk ops, ever: the engine turns any 4-byte WRITE to a region with
// bulk_write_open into a bulk channel, and STREAM_LIVE is 4 bytes.
#include "podcrp.h"
#include <string.h>

_Static_assert((PODCRP_RXQ_N & (PODCRP_RXQ_N - 1)) == 0, "the RX queue indices wrap at 256");
_Static_assert(PODCRP_BLE_SPOOL_LEN <= 255 && PODCRP_RXQ_LEN <= 255, "lengths are kept in a byte");
_Static_assert(PODCRP_BLE_MAX_CHUNK + 3 == PODCRP_MTU_MAX, "a 244 B packet is exactly MTU 247 - 3");

enum { MODE_ENGINE = 0, MODE_PUSH, MODE_LOG };   // link->mode: what the transport is sending

static void put16(uint8_t *p, uint16_t v) { p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); }
static void put32(uint8_t *p, uint32_t v) {
  p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); p[2] = (uint8_t)(v >> 16); p[3] = (uint8_t)(v >> 24);
}

// The biggest packet this link can send now: MTU - 3 on BLE, 1024 on USB (mtu_eff is 1027 there).
static uint32_t max_pkt(const podcrp_link_t *l) { return (uint32_t)l->mtu_eff - 3; }

// ================================================================================================
// The register table (docs/ble.md 2.3). size is what QUERY_REG reports (0 = variable); room is
// the most a read can give. Offset 0 of a group is its base, so QUERY_GROUP counts leave it out
// (coil's patch 5) and discovery, which starts at offset 1, still finds every register.
// ================================================================================================
typedef struct {
  uint32_t addr;
  const char *name;
  uint8_t type, access, flags, size, room;
} pod_reg_t;

#define R CRP_ACCESS_R
#define W CRP_ACCESS_W
#define PUSHED CRP_REG_PUSH_ON_CHANGE
static const pod_reg_t REGS[] = {
  {PODCRP_REG_PROTO, "PROTO", CRP_TYPE_UINT8, R, 0, 1, 1},
  {PODCRP_REG_INFO, "INFO", CRP_TYPE_BLOB, R, 0, 32, 32},
  {PODCRP_REG_STATUS, "STATUS", CRP_TYPE_BLOB, R, PUSHED, 20, 20},
  {PODCRP_REG_CAL, "CAL", CRP_TYPE_BLOB, R, 0, 64, 64},
  {PODCRP_REG_DATA, "DATA", CRP_TYPE_BLOB, 0, PUSHED, 0, 0},          // pushed only: a READ is denied
  {PODCRP_REG_CTRL, "CTRL", CRP_TYPE_BLOB, W, 0, 0, PODCRP_CTRL_MAX},
  {PODCRP_REG_PUSH, "PUSH", CRP_TYPE_UINT8, R | W, 0, 1, 1},

  {PODCRP_REG_BATTERY_MV, "BATTERY_MV", CRP_TYPE_UINT16, R, 0, 2, 2},
  {PODCRP_REG_FLAGS, "FLAGS", CRP_TYPE_UINT8, R, 0, 1, 1},
  {PODCRP_REG_HEAD, "HEAD", CRP_TYPE_UINT32, R, 0, 4, 4},
  {PODCRP_REG_TICKS, "TICKS", CRP_TYPE_UINT32, R, 0, 4, 4},
  {PODCRP_REG_BOOT_ID, "BOOT_ID", CRP_TYPE_UINT32, R, 0, 4, 4},
  {PODCRP_REG_SERIAL, "SERIAL", CRP_TYPE_UINT32, R, 0, 4, 4},
  {PODCRP_REG_FW_BUILD, "FW_BUILD", CRP_TYPE_UINT16, R, 0, 2, 2},
  {PODCRP_REG_WAKE_REASON, "WAKE_REASON", CRP_TYPE_UINT8, R, 0, 1, 1},
  {PODCRP_REG_SLEEP_REASON, "SLEEP_REASON", CRP_TYPE_UINT8, R, 0, 1, 1},
  {PODCRP_REG_LOST, "LOST", CRP_TYPE_UINT32, R, 0, 4, 4},
  {PODCRP_REG_MISSED_DRDY, "MISSED_DRDY", CRP_TYPE_UINT32, R, 0, 4, 4},
  {PODCRP_REG_BATT_V, "BATT_V", CRP_TYPE_FLOAT32, R, 0, 4, 4},
  {PODCRP_REG_RESETREAS, "RESETREAS", CRP_TYPE_UINT32, R, 0, 4, 4},
  {PODCRP_REG_NAME, "NAME", CRP_TYPE_STRING, R, 0, 0, 32},

  {PODCRP_REG_KIND, "KIND", CRP_TYPE_UINT8, R, 0, 1, 1},
  {PODCRP_REG_MTU, "MTU", CRP_TYPE_UINT16, R, 0, 2, 2},
  {PODCRP_REG_N_MAX, "N_MAX", CRP_TYPE_UINT8, R, 0, 1, 1},
  {PODCRP_REG_INTERVAL, "INTERVAL", CRP_TYPE_UINT16, R, 0, 2, 2},
  {PODCRP_REG_PHY, "PHY", CRP_TYPE_UINT8, R, 0, 1, 1},
  {PODCRP_REG_DL, "DL", CRP_TYPE_UINT16, R, 0, 2, 2},
  {PODCRP_REG_FRAMES, "FRAMES", CRP_TYPE_UINT32, R, 0, 4, 4},
  {PODCRP_REG_TX_DROPS, "TX_DROPS", CRP_TYPE_UINT32, R, 0, 4, 4},
  {PODCRP_REG_RX_DROPS, "RX_DROPS", CRP_TYPE_UINT32, R, 0, 4, 4},
  {PODCRP_REG_STACK_FREE, "STACK_FREE", CRP_TYPE_UINT32, R, 0, 4, 4},

  {PODCRP_REG_BENCH, "BENCH", CRP_TYPE_UINT8, W, 0, 1, 1},           // USB only
};
#undef R
#undef W
#undef PUSHED
#define N_REGS (sizeof REGS / sizeof REGS[0])

// The bench group exists on the USB link only (its region isn't registered on BLE either).
static bool reg_here(const podcrp_link_t *l, const pod_reg_t *d) {
  return (d->addr & PODCRP_GROUP_MASK) != PODCRP_GROUP_BENCH || l->kind == PODCRP_USB;
}

static const pod_reg_t *find_reg(const podcrp_link_t *l, uint32_t reg) {
  for (size_t i = 0; i < N_REGS; i++)
    if (REGS[i].addr == reg) return reg_here(l, &REGS[i]) ? &REGS[i] : NULL;
  return NULL;
}

// ================================================================================================
// The region ops. Called from inside crp_engine_process: they validate, copy or set flags, and
// never send (the engine sends the RESPONSE right after they return).
// ================================================================================================
static crp_status_t r_read(void *ctx, uint32_t reg, uint8_t *buf, uint32_t *len) {
  podcrp_link_t *l = (podcrp_link_t *)ctx;
  const pod_reg_t *d = find_reg(l, reg);
  if (!d) return CRP_STATUS_UNKNOWN_REG;
  if (!(d->access & CRP_ACCESS_R)) return CRP_STATUS_ACCESS_DENIED;   // DATA, CTRL, BENCH
  // A RESPONSE (8 B + the value) that wouldn't fit in one packet is answered BUSY (8 B), ble.md 5.
  // A fixed size is known up front, so a BUSY read builds nothing.
  if (d->size && 8u + d->size > max_pkt(l)) return CRP_STATUS_BUSY;
  uint32_t room = d->room < *len ? d->room : *len;
  memset(buf, 0, room);
  uint32_t n;
  switch (reg) {
    case PODCRP_REG_PROTO: buf[0] = PODCRP_PROTO_VALUE; n = 1; break;
    case PODCRP_REG_PUSH: buf[0] = l->push; n = 1; break;
    case PODCRP_REG_KIND: buf[0] = l->kind; n = 1; break;
    case PODCRP_REG_MTU:                      // USB has no ATT MTU: 0, as make_fake_pod.py's on_read
      put16(buf, l->kind == PODCRP_USB ? 0 : l->mtu_eff);
      n = 2;
      break;
    case PODCRP_REG_TX_DROPS: put32(buf, l->tx_drops); n = 4; break;
    case PODCRP_REG_RX_DROPS: put32(buf, l->rx_drops); n = 4; break;
    default:                                  // INFO, STATUS, CAL and the sketch's values
      n = l->hooks.read ? l->hooks.read(l, reg, buf, room) : 0;
      if (n == 0) return CRP_STATUS_UNKNOWN_REG;
      if (n > room) n = room;
      break;
  }
  if (d->size) n = d->size;                   // fixed sizes go out whole, as QUERY_REG said
  if (8u + n > max_pkt(l)) return CRP_STATUS_BUSY;   // a variable one (NAME) is known only now
  *len = n;
  return CRP_STATUS_OK;
}

static crp_status_t r_write(void *ctx, uint32_t reg, const uint8_t *buf, uint32_t len) {
  podcrp_link_t *l = (podcrp_link_t *)ctx;
  const pod_reg_t *d = find_reg(l, reg);
  if (!d) return CRP_STATUS_UNKNOWN_REG;
  if (!(d->access & CRP_ACCESS_W)) return CRP_STATUS_ACCESS_DENIED;
  switch (reg) {
    case PODCRP_REG_PUSH:                     // 0 or 1, one byte
      if (len != 1 || buf[0] > 1) return CRP_STATUS_INVALID_VALUE;
      l->push = buf[0];
      return CRP_STATUS_OK;
    case PODCRP_REG_CTRL:                     // the op table is the sketch's (ble.md 4)
      if (len < 1 || len > PODCRP_CTRL_MAX) return CRP_STATUS_INVALID_VALUE;
      return l->hooks.ctrl ? (crp_status_t)l->hooks.ctrl(l, buf, len) : CRP_STATUS_ACCESS_DENIED;
    case PODCRP_REG_BENCH:                    // only the bench characters (pod.ino, top)
      if (len != 1 || buf[0] == 0 || !memchr(PODCRP_BENCH_CHARS, buf[0], sizeof PODCRP_BENCH_CHARS - 1))
        return CRP_STATUS_INVALID_VALUE;
      return l->hooks.bench ? (crp_status_t)l->hooks.bench(l, buf[0]) : CRP_STATUS_ACCESS_DENIED;
    default:
      return CRP_STATUS_ACCESS_DENIED;
  }
}

static crp_status_t r_query_reg(void *ctx, uint32_t reg, crp_reg_info_t *info) {
  const pod_reg_t *d = find_reg((const podcrp_link_t *)ctx, reg);
  if (!d) return CRP_STATUS_UNKNOWN_REG;      // the engine answers a miss UNKNOWN_REG (coil's patch 4)
  info->data_type = d->type;
  info->access = d->access;
  info->flags = d->flags;
  info->size = d->size;
  info->min_interval_us = 0;
  info->name = d->name;
  return CRP_STATUS_OK;
}

static crp_status_t r_query_group(void *ctx, uint32_t base, crp_group_info_t *info) {
  const podcrp_link_t *l = (const podcrp_link_t *)ctx;
  uint32_t g = base & PODCRP_GROUP_MASK;
  uint32_t n = 0;
  uint8_t flags = 0;
  for (size_t i = 0; i < N_REGS; i++) {
    const pod_reg_t *d = &REGS[i];
    if ((d->addr & PODCRP_GROUP_MASK) != g || !reg_here(l, d)) continue;
    if (d->addr != g) n++;                    // the base isn't counted (coil's patch 5)
    if (d->access & CRP_ACCESS_R) flags |= CRP_GROUP_READABLE;
    if (d->access & CRP_ACCESS_W) flags |= CRP_GROUP_WRITABLE;
  }
  if (!n) return CRP_STATUS_UNKNOWN_REG;
  info->mask = PODCRP_GROUP_MASK;
  info->flags = flags;
  info->count = n;
  return CRP_STATUS_OK;
}

static const crp_region_ops_t POD_OPS = {
  .read = r_read,
  .write = r_write,
  .query_reg = r_query_reg,
  .query_group = r_query_group,
};

// ================================================================================================
// The transport
// ================================================================================================

// BLE: one notification, all or nothing.
static bool ble_direct(podcrp_link_t *l, const uint8_t *p, uint32_t len) {
  return l->hooks.send && l->hooks.send(l, p, len) == len;
}

// BLE: the spooled engine packets, oldest first, while slots take them.
static void ble_drain(podcrp_link_t *l) {
  while (l->u.ble.n) {
    uint8_t i = l->u.ble.first;
    uint32_t len = l->u.ble.len[i];
    if (len <= max_pkt(l)) {
      if (!ble_direct(l, l->u.ble.pkt[i], len)) return;
    } else {
      l->tx_drops++;                          // can't happen: the MTU only grows within a link
    }
    l->u.ble.first = (uint8_t)((i + 1) % PODCRP_BLE_SPOOL_N);
    l->u.ble.n--;
  }
}

// USB: hand the port as much of the spool as it takes, in order.
static void usb_drain(podcrp_link_t *l) {
  while (l->u.usb.used) {
    uint32_t t = l->u.usb.tail;
    uint32_t run = PODCRP_USB_SPOOL - t;      // contiguous up to the ring's end
    if (run > l->u.usb.used) run = l->u.usb.used;
    uint32_t took = l->hooks.send ? l->hooks.send(l, l->u.usb.spool + t, run) : 0;
    if (took > run) took = run;
    l->u.usb.tail = (uint16_t)((t + took) % PODCRP_USB_SPOOL);
    l->u.usb.used = (uint16_t)(l->u.usb.used - took);
    if (took < run) return;
  }
}

static void usb_put(podcrp_link_t *l, const uint8_t *p, uint32_t n) {
  uint32_t h = (l->u.usb.tail + l->u.usb.used) % PODCRP_USB_SPOOL;
  for (uint32_t i = 0; i < n; i++) {
    l->u.usb.spool[h] = p[i];
    h = (h + 1) % PODCRP_USB_SPOOL;
  }
  l->u.usb.used = (uint16_t)(l->u.usb.used + n);
}

static int usb_send(podcrp_link_t *l, const uint8_t *p, uint32_t len, bool nop) {
  if (len == 0 || len > PODCRP_USB_MAX_PKT) {
    if (l->mode == MODE_ENGINE) l->tx_drops++;
    return -1;
  }
  uint32_t need = 2 + len;                    // level 1: [len u16][packet]
  uint32_t room = PODCRP_USB_SPOOL - l->u.usb.used;
  if (l->mode != MODE_ENGINE) {               // pushes and LOGs leave 128 B for RESPONSEs
    if (room < need + PODCRP_USB_RESERVE) return -1;
  } else if (nop) {
    if (l->u.usb.used) return 0;              // only into an empty spool: dropped, reported sent
  } else if (room < need) {
    l->tx_drops++;
    return -1;
  }
  uint8_t hdr[2];
  put16(hdr, (uint16_t)len);
  usb_put(l, hdr, 2);
  usb_put(l, p, len);
  usb_drain(l);
  return 0;
}

static int t_send(void *ctx, const uint8_t *p, uint32_t len) {
  podcrp_link_t *l = (podcrp_link_t *)ctx;
  bool nop = len == 1 && p[0] == CRP_PKT_NOP;
  int rc;
  if (l->kind == PODCRP_USB) {
    rc = usb_send(l, p, len, nop);
  } else if (l->mode == MODE_PUSH) {          // never spooled: the frame waits in the sketch
    rc = l->u.ble.n == 0 && len <= max_pkt(l) && ble_direct(l, p, len) ? 0 : -1;
  } else if (l->mode == MODE_LOG) {
    rc = -1;                                  // no LOG on Bluetooth
  } else {
    ble_drain(l);                             // keep the order: older engine packets first
    if (len > max_pkt(l)) {                   // a discovery answer at a small MTU, say (a NOP always fits)
      l->tx_drops++;
      rc = -1;
    } else if (l->u.ble.n == 0 && ble_direct(l, p, len)) {
      rc = 0;
    } else if (nop) {
      rc = 0;                                 // a NOP only goes straight out: dropped, reported sent
    } else if (l->u.ble.n == PODCRP_BLE_SPOOL_N || len > PODCRP_BLE_SPOOL_LEN) {
      l->tx_drops++;
      rc = -1;
    } else {
      uint8_t i = (uint8_t)((l->u.ble.first + l->u.ble.n) % PODCRP_BLE_SPOOL_N);
      memcpy(l->u.ble.pkt[i], p, len);
      l->u.ble.len[i] = (uint8_t)len;
      l->u.ble.n++;
      rc = 0;
    }
  }
  l->sent = rc == 0;
  return rc;
}

// The cheap half of the push rule, so a refused push isn't even serialized.
static bool t_ready(void *ctx) {
  const podcrp_link_t *l = (const podcrp_link_t *)ctx;
  if (l->mode == MODE_ENGINE) return true;    // send() spools or drops (and counts) these
  if (l->mode == MODE_PUSH && !l->push) return false;
  if (l->kind == PODCRP_BLE) return l->mode == MODE_PUSH && l->u.ble.n == 0;
  return PODCRP_USB_SPOOL - l->u.usb.used >= PODCRP_USB_RESERVE;
}

// The next queued client packet (loop() is the only consumer).
static uint32_t t_recv(void *ctx, uint8_t *buf, uint32_t max_len) {
  podcrp_link_t *l = (podcrp_link_t *)ctx;
  uint8_t t = l->rx_tail;
  if (t == __atomic_load_n(&l->rx_head, __ATOMIC_ACQUIRE)) return 0;
  const podcrp_rxslot_t *s = &l->rxq[t & (PODCRP_RXQ_N - 1)];
  uint32_t n = s->len <= max_len ? s->len : 0;
  memcpy(buf, s->p, n);
  __atomic_store_n(&l->rx_tail, (uint8_t)(t + 1), __ATOMIC_RELEASE);
  return n;
}

static uint32_t t_mtu(void *ctx) { return max_pkt((const podcrp_link_t *)ctx); }

// Drop everything waiting in the TX spool. On USB that would cut short a packet the port already
// took part of, so only podcrp_reset does it there: at a port close, or once the spool is out
// (usb.ino).
static void spool_clear(podcrp_link_t *l) {
  if (l->kind == PODCRP_USB) l->u.usb.tail = l->u.usb.used = 0;
  else l->u.ble.first = l->u.ble.n = 0;
}

// ================================================================================================
// Engine callbacks and the session edges
// ================================================================================================
static void cb_hello(void *ctx, const crp_pkt_hello_t *hello) {
  (void)hello;
  ((podcrp_link_t *)ctx)->hello_seen = true;  // the engine reset and answered: a new session
}

static void cb_bye(void *ctx, const crp_pkt_bye_t *bye) {
  (void)bye;
  ((podcrp_link_t *)ctx)->end_why = PODCRP_END_BYE;
}

// The engine calls this on every tick after 3 s without receiving. Bluetooth ignores it (the
// link's supervision timeout is the signal, and a phone's timers throttle with the screen off).
static void cb_liveness(void *ctx) {
  podcrp_link_t *l = (podcrp_link_t *)ctx;
  if (l->kind == PODCRP_USB) l->liveness = true;
}

// After every engine step: tell the sketch about a session that ended (BYE, END 0xFF, a HELLO
// over an open one, liveness) and one that started. PUSH is 0 in every new session.
static void session_edges(podcrp_link_t *l) {
  bool active = l->engine.session_active;
  if (l->session && (l->hello_seen || !active)) {
    uint8_t why = l->hello_seen ? PODCRP_END_HELLO : (l->end_why ? l->end_why : PODCRP_END_END);
    l->session = false;
    l->push = 0;
    // Bluetooth: BYE and END empty the packet spool, as a disconnect does (ble.md 5): with no
    // session the pod sends nothing (2.2), and a stale RESPONSE mustn't go out ahead of the next
    // HELLO's answer. The link stays, so its MTU does too. Not at a HELLO: the engine has already
    // sent the new session's answer, and it may be waiting in the spool (the old RESPONSEs ahead
    // of it still go, in order). USB keeps its byte spool: the port may hold the start of a
    // packet, and cutting the rest would leave half a packet on the wire before the text.
    if (!l->hello_seen && l->kind == PODCRP_BLE) spool_clear(l);
    if (l->hooks.on_session_end) l->hooks.on_session_end(l, why);
  }
  l->end_why = 0;
  if (l->hello_seen) {
    l->hello_seen = false;
    l->push = 0;
    l->session = active;
    if (active && l->hooks.on_session_start) l->hooks.on_session_start(l);
  }
}

// ================================================================================================
// The API (podcrp.h)
// ================================================================================================
void podcrp_init(podcrp_link_t *l, uint8_t kind, const podcrp_hooks_t *hooks, uint32_t device_id,
                 const char *name) {
  memset(l, 0, sizeof *l);
  l->kind = kind == PODCRP_USB ? PODCRP_USB : PODCRP_BLE;
  if (hooks) l->hooks = *hooks;
  bool usb = l->kind == PODCRP_USB;
  l->mtu_eff = usb ? PODCRP_USB_MAX_PKT + 3 : PODCRP_MTU_MIN;

  l->transport.send = t_send;
  l->transport.recv = t_recv;
  l->transport.ready = t_ready;
  l->transport.mtu = t_mtu;
  l->transport.ctx = l;

  crp_region_table_init(&l->regions);
  crp_region_register(&l->regions, PODCRP_GROUP_MASK, PODCRP_GROUP_STREAM, &POD_OPS, l);
  crp_region_register(&l->regions, PODCRP_GROUP_MASK, PODCRP_GROUP_STATE, &POD_OPS, l);
  crp_region_register(&l->regions, PODCRP_GROUP_MASK, PODCRP_GROUP_LINK, &POD_OPS, l);
  if (usb) crp_region_register(&l->regions, PODCRP_GROUP_MASK, PODCRP_GROUP_BENCH, &POD_OPS, l);

  size_t nlen = name ? strlen(name) : 0;
  crp_engine_config_t cfg;
  memset(&cfg, 0, sizeof cfg);
  cfg.max_channels = 1;                       // it opens none: no subscriptions, no bulk
  cfg.max_chunk = usb ? PODCRP_USB_MAX_PKT : PODCRP_BLE_MAX_CHUNK;
  cfg.flags = CRP_FLAG_PUSH;
  cfg.device_id = device_id;
  cfg.device_name = name;
  cfg.device_name_len = (uint8_t)(nlen > 255 ? 255 : nlen);
  if (usb)
    crp_engine_init(&l->engine, &cfg, &l->transport, &l->regions, l->u.usb.scratch, sizeof l->u.usb.scratch);
  else
    crp_engine_init(&l->engine, &cfg, &l->transport, &l->regions, l->u.ble.scratch, sizeof l->u.ble.scratch);

  crp_callbacks_t cb;
  memset(&cb, 0, sizeof cb);
  cb.on_hello = cb_hello;
  cb.on_bye = cb_bye;
  cb.on_liveness_timeout = cb_liveness;
  cb.user_ctx = l;
  crp_engine_set_callbacks(&l->engine, &cb);
}

// Producer side: the BLE task (or the deframer, in loop()). Copy, then publish the slot.
bool podcrp_rx_packet(podcrp_link_t *l, const uint8_t *p, uint32_t len) {
  uint8_t h = l->rx_head;
  uint8_t used = (uint8_t)(h - __atomic_load_n(&l->rx_tail, __ATOMIC_ACQUIRE));
  if (len == 0 || len > PODCRP_RXQ_LEN || used >= PODCRP_RXQ_N) {
    l->rx_drops++;
    return false;
  }
  podcrp_rxslot_t *s = &l->rxq[h & (PODCRP_RXQ_N - 1)];
  memcpy(s->p, p, len);
  s->len = (uint8_t)len;
  __atomic_store_n(&l->rx_head, (uint8_t)(h + 1), __ATOMIC_RELEASE);
  return true;
}

// Level 1: [len u16][packet], len 1..1024. A bad length drops its first byte and the second one
// becomes the next length's low byte (the rescan). Text can't pass for a length: the second byte
// of any text pair is at least 0x0A, so its length is above 1024. A packet longer than the RX
// queue's 64 B is read through and dropped.
void podcrp_rx_bytes(podcrp_link_t *l, const uint8_t *p, uint32_t len) {
  if (l->kind != PODCRP_USB) return;
  for (uint32_t i = 0; i < len; i++) {
    uint8_t b = p[i];
    switch (l->u.usb.df_state) {
      case 0:
        l->u.usb.df_lo = b;
        l->u.usb.df_state = 1;
        break;
      case 1: {
        uint16_t n = (uint16_t)(l->u.usb.df_lo | (b << 8));
        if (n == 0 || n > PODCRP_USB_MAX_PKT) {
          l->u.usb.df_lo = b;                 // drop one byte, rescan
          break;
        }
        l->u.usb.df_len = n;
        l->u.usb.df_got = 0;
        l->u.usb.df_state = 2;
        break;
      }
      default:
        if (l->u.usb.df_got < PODCRP_RXQ_LEN) l->u.usb.df_buf[l->u.usb.df_got] = b;
        if (++l->u.usb.df_got == l->u.usb.df_len) {
          if (l->u.usb.df_len > PODCRP_RXQ_LEN) l->rx_drops++;
          else podcrp_rx_packet(l, l->u.usb.df_buf, l->u.usb.df_len);
          l->u.usb.df_state = 0;
        }
        break;
    }
  }
}

void podcrp_spool_drain(podcrp_link_t *l) {
  if (l->kind == PODCRP_USB) usb_drain(l);
  else ble_drain(l);
}

// Every packet queued when it starts (at most the queue's 8), each through the engine, with the
// session edges after each one. now_ms goes in first: the engine stamps last_recv_ms (USB
// liveness) with its own clock, which otherwise only moves at the tick.
void podcrp_process_all(podcrp_link_t *l, uint32_t now_ms) {
  l->engine.now_ms = now_ms;
  for (int k = 0; k < PODCRP_RXQ_N; k++) {
    l->in_engine = true;
    bool got = crp_engine_process(&l->engine);
    l->in_engine = false;
    if (!got) break;
    session_edges(l);
  }
}

// The heartbeat NOP after 1 s without sending, and on USB the end of a session after 3 s without
// receiving (once: the reset stops the engine's tick from asking again).
void podcrp_tick(podcrp_link_t *l, uint32_t now_ms) {
  l->in_engine = true;
  crp_engine_tick(&l->engine, now_ms);
  l->in_engine = false;
  if (l->liveness) {
    l->liveness = false;
    if (l->engine.session_active) {
      crp_engine_reset(&l->engine);
      l->end_why = PODCRP_END_SILENCE;
    }
  }
  session_edges(l);
}

void podcrp_pass(podcrp_link_t *l, uint32_t now_ms) {
  podcrp_spool_drain(l);
  podcrp_process_all(l, now_ms);
  podcrp_spool_drain(l);
  podcrp_tick(l, now_ms);
}

bool podcrp_pushes_on(const podcrp_link_t *l) { return l->session && l->engine.session_active && l->push; }
bool podcrp_in_session(const podcrp_link_t *l) { return l->session && l->engine.session_active; }

bool podcrp_spool_empty(const podcrp_link_t *l) {
  return l->kind == PODCRP_USB ? l->u.usb.used == 0 : l->u.ble.n == 0;
}

bool podcrp_push(podcrp_link_t *l, uint32_t reg, const uint8_t *frame, uint32_t len) {
  if (l->in_engine || !podcrp_pushes_on(l)) return false;
  l->mode = MODE_PUSH;
  int rc = crp_engine_push_change(&l->engine, reg, frame, len);   // LOCAL PATCH 6: 0 = sent
  l->mode = MODE_ENGINE;
  return rc == 0;
}

void podcrp_reset(podcrp_link_t *l) {
  crp_engine_reset(&l->engine);
  l->session = l->hello_seen = l->liveness = false;
  l->push = 0;
  l->end_why = 0;
  l->mode = MODE_ENGINE;
  spool_clear(l);
  if (l->kind == PODCRP_USB) l->u.usb.df_state = 0;
  else l->mtu_eff = PODCRP_MTU_MIN;           // the next connection starts at 23
  // Drop what's queued, from the consumer's side: a packet the BLE task adds after this stays.
  __atomic_store_n(&l->rx_tail, __atomic_load_n(&l->rx_head, __ATOMIC_ACQUIRE), __ATOMIC_RELEASE);
}

void podcrp_set_mtu(podcrp_link_t *l, uint16_t att_mtu) {
  if (l->kind != PODCRP_BLE) return;
  l->mtu_eff = att_mtu > PODCRP_MTU_MAX ? PODCRP_MTU_MAX : (att_mtu < PODCRP_MTU_MIN ? PODCRP_MTU_MIN : att_mtu);
}

bool podcrp_log(podcrp_link_t *l, uint8_t level, const char *module, const char *msg) {
  if (l->kind != PODCRP_USB || l->in_engine || !podcrp_in_session(l)) return false;
  size_t ml = module ? strlen(module) : 0;
  if (ml > 32) ml = 32;
  size_t n = msg ? strlen(msg) : 0;
  if (7 + ml + n > PODCRP_USB_MAX_PKT) n = PODCRP_USB_MAX_PKT - 7 - ml;   // [18 lvl ts:4 modlen]
  l->mode = MODE_LOG;
  l->sent = false;
  crp_engine_send_log(&l->engine, level, module, (uint8_t)ml, msg, (uint16_t)n);
  l->mode = MODE_ENGINE;
  return l->sent;
}
