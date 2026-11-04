// podcrp.h: the pod's CRP glue (docs/ble.md section 2, D45). C11, no Arduino.
//
// One podcrp_link_t per link (Bluetooth, USB): the vendored engine (src/crp), its transport, the
// pod's register region, an RX packet queue, the TX spool (BLE: 4 packets; USB: a 2 KB byte spool
// with level 1 framing and its deframer), PUSH and the session edges.
//
// Nothing in here calls into the sketch by name: everything goes through podcrp_hooks_t, so
// podcrp.c links with or without a caller. firmware/pod/test/host_test.c runs it on the PC.
//
// Threads: every function runs in loop() except podcrp_rx_packet, which the BLE task may call
// (a single-producer single-consumer queue). The hooks are called from loop() only.
//
// A pass, from loop(), in this order (docs/ble.md 5):
//   podcrp_spool_drain(link);            the engine packets that waited for a slot go first
//   podcrp_process_all(link, now);       every queued client packet through the engine
//   ... the sketch's own link work and its sendPass (podcrp_push for STATUS and DATA) ...
//   podcrp_spool_drain(link);
//   podcrp_tick(link, now);              heartbeat NOP, USB liveness, the session edges
// podcrp_pass() does the four podcrp steps in that order with no link work between them.
#pragma once
#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {   // the .ino files are C++, podcrp.c and the engine are C
#endif

#include "src/crp/crp/crp_engine.h"

// ---- kinds, identity, sizes (docs/ble.md 2) ----
#define PODCRP_BLE 1
#define PODCRP_USB 2
#define PODCRP_DEVICE_ID 0x444F5042u          // "BPOD", 42 50 4F 44 on the wire
#define PODCRP_PROTO_VALUE 1                  // the PROTO register: the frames are still PodLink v1

#define PODCRP_MTU_MIN 23
#define PODCRP_MTU_MAX 247                    // mtu_eff = min(ATT MTU, 247), ble.md 5
#define PODCRP_BLE_MAX_CHUNK 244              // HELLO max_chunk on Bluetooth (= 247 - 3)
#define PODCRP_USB_MAX_PKT 1024               // level 1 length limit, and HELLO max_chunk on USB
#define PODCRP_RXQ_N 8                        // RX queue: 8 packets of up to 64 B
#define PODCRP_RXQ_LEN 64
#define PODCRP_BLE_SPOOL_N 4                  // BLE TX spool: 4 engine packets of up to 128 B
#define PODCRP_BLE_SPOOL_LEN 128
#define PODCRP_USB_SPOOL 2048                 // USB TX spool, bytes (level 1 framed)
#define PODCRP_USB_RESERVE 128                // a push (or LOG) must leave this much room for RESPONSEs
#define PODCRP_CTRL_MAX 10                    // CTRL values are 1..10 bytes (ble.md 4)

// ---- registers (docs/ble.md 2.3) ----
#define PODCRP_GROUP_MASK 0xFFFF0000u
#define PODCRP_GROUP_STREAM 0x00010000u
#define PODCRP_GROUP_STATE 0x00020000u
#define PODCRP_GROUP_LINK 0x00030000u
#define PODCRP_GROUP_BENCH 0x00040000u        // USB only

#define PODCRP_REG_PROTO 0x00010000u
#define PODCRP_REG_INFO 0x00010001u
#define PODCRP_REG_STATUS 0x00010002u
#define PODCRP_REG_CAL 0x00010003u
#define PODCRP_REG_DATA 0x00010004u
#define PODCRP_REG_CTRL 0x00010005u
#define PODCRP_REG_PUSH 0x00010006u

#define PODCRP_REG_BATTERY_MV 0x00020001u
#define PODCRP_REG_FLAGS 0x00020002u
#define PODCRP_REG_HEAD 0x00020003u
#define PODCRP_REG_TICKS 0x00020004u
#define PODCRP_REG_BOOT_ID 0x00020005u
#define PODCRP_REG_SERIAL 0x00020006u
#define PODCRP_REG_FW_BUILD 0x00020007u
#define PODCRP_REG_WAKE_REASON 0x00020008u
#define PODCRP_REG_SLEEP_REASON 0x00020009u
#define PODCRP_REG_LOST 0x0002000Au
#define PODCRP_REG_MISSED_DRDY 0x0002000Bu
#define PODCRP_REG_BATT_V 0x0002000Cu
#define PODCRP_REG_RESETREAS 0x0002000Du
#define PODCRP_REG_NAME 0x0002000Eu

#define PODCRP_REG_KIND 0x00030001u
#define PODCRP_REG_MTU 0x00030002u
#define PODCRP_REG_N_MAX 0x00030003u
#define PODCRP_REG_INTERVAL 0x00030004u
#define PODCRP_REG_PHY 0x00030005u
#define PODCRP_REG_DL 0x00030006u
#define PODCRP_REG_FRAMES 0x00030007u
#define PODCRP_REG_TX_DROPS 0x00030008u
#define PODCRP_REG_RX_DROPS 0x00030009u
#define PODCRP_REG_STACK_FREE 0x0003000Au

#define PODCRP_REG_BENCH 0x00040001u
#define PODCRP_BENCH_CHARS "sowih"            // the only values BENCH takes (pod.ino, top)

typedef struct podcrp_link podcrp_link_t;

// The sketch's side. Every hook is called from loop(); none may call podcrp_push or podcrp_log
// (they're refused while the engine runs: a RESPONSE must go out before anything it causes).
typedef struct {
  // Send bytes. BLE: one packet as one notification on TX, all or nothing: return len if the
  // SoftDevice took it, 0 if no slot is free or notifications are off. USB: any prefix, return
  // how many bytes the port took. NULL = nothing ever goes out.
  uint32_t (*send)(podcrp_link_t *link, const uint8_t *p, uint32_t len);
  // Fill reg's value into buf (room bytes, zeroed) and return its length, 0 = this link has none
  // (answered UNKNOWN_REG). Asked for INFO, STATUS, CAL, the 0x0002 state mirrors and N_MAX,
  // INTERVAL, PHY, DL, FRAMES, STACK_FREE; the glue answers PROTO, PUSH, KIND, MTU and the drop
  // counters itself. A STATUS is built here, when the READ is processed (ble.md 2.3), with no
  // event bits. Fixed-size registers always go out at their full size.
  uint32_t (*read)(podcrp_link_t *link, uint32_t reg, uint8_t *buf, uint32_t room);
  // A CTRL write of 1..10 bytes (ble.md 4). Return its CRP status: OK when the op was taken,
  // INVALID_VALUE for an unknown op or a wrong length, BUSY from the SLEEP op on. Set state and
  // queue the answering STATUS for the next sendPass; never send from here.
  uint8_t (*ctrl)(podcrp_link_t *link, const uint8_t *p, uint32_t len);
  // A BENCH write (USB only), one of PODCRP_BENCH_CHARS (the glue answers anything else
  // INVALID_VALUE). Return its status. NULL = BENCH answers ACCESS_DENIED.
  uint8_t (*bench)(podcrp_link_t *link, uint8_t c);
  // A session started: the engine took a HELLO and answered it. Optional.
  void (*on_session_start)(podcrp_link_t *link);
  // A session ended on a live link; why is a PODCRP_END_* (a HELLO mid-session: on_session_start
  // follows). End the link's stream exactly as a disconnect does (ble.md 2.2). Called once per
  // session. podcrp_reset (a disconnect) doesn't call it. The glue has already emptied the TX
  // spool (not at HELLO: the new session's answer may be in it), so a live BLE link needs no
  // podcrp_reset here, which would also put its MTU back to 23. USB goes back to text at BYE, END
  // and SILENCE (ble.md 2.5), not at HELLO.
  void (*on_session_end)(podcrp_link_t *link, uint8_t why);
} podcrp_hooks_t;

#define PODCRP_END_BYE 1                      // the client sent BYE
#define PODCRP_END_END 2                      // END 0xFF (or any other engine reset)
#define PODCRP_END_HELLO 3                    // a HELLO over an open session
#define PODCRP_END_SILENCE 4                  // USB: 3 s without receiving

typedef struct {
  uint8_t len;
  uint8_t p[PODCRP_RXQ_LEN];
} podcrp_rxslot_t;

// One link. Set user after podcrp_init; read the counters freely; change nothing else directly.
struct podcrp_link {
  crp_engine_t engine;
  crp_transport_t transport;
  crp_region_table_t regions;
  podcrp_hooks_t hooks;
  void *user;                         // the sketch's, for its hooks (podcrp never touches it)
  uint8_t kind;                       // PODCRP_BLE or PODCRP_USB
  uint8_t push;                       // the PUSH register: 1 = this session takes pushes
  uint8_t mode;                       // what the transport is sending now (podcrp.c)
  bool session;                       // a session is open, as far as the hooks were told
  bool hello_seen;                    // the engine took a HELLO in this process step
  bool liveness;                      // USB: the engine reported 3 s without receiving
  uint8_t end_why;                    // the PODCRP_END_* the next session end reports, 0 = END
  bool in_engine;                     // inside crp_engine_process: pushes and LOGs are refused
  bool sent;                          // the transport took the last packet (for podcrp_log)
  uint16_t mtu_eff;                   // BLE: min(ATT MTU, 247). USB: 1027, so packets up to 1024
  uint32_t tx_drops;                  // engine packets dropped: spool full, or too big to send
  volatile uint32_t rx_drops;         // client packets dropped: queue full, too long, bad length
  volatile uint8_t rx_head, rx_tail;  // RX queue: the producer owns head, loop() owns tail
  podcrp_rxslot_t rxq[PODCRP_RXQ_N];
  union {
    struct {
      uint8_t first, n;               // the packet spool: oldest entry, entries queued
      uint8_t len[PODCRP_BLE_SPOOL_N];
      uint8_t pkt[PODCRP_BLE_SPOOL_N][PODCRP_BLE_SPOOL_LEN];
      uint8_t scratch[PODCRP_BLE_MAX_CHUNK + 32];
    } ble;
    struct {
      uint16_t tail, used;            // the byte spool: oldest byte, bytes queued
      uint8_t df_state, df_lo;        // the level 1 deframer: 0 = length low, 1 = high, 2 = body
      uint16_t df_len, df_got;
      uint8_t df_buf[PODCRP_RXQ_LEN];
      uint8_t spool[PODCRP_USB_SPOOL];
      uint8_t scratch[PODCRP_USB_MAX_PKT + 32];
    } usb;
  } u;
};

// Set up a link. hooks is copied; name (the QUERY_DEVICE name, "BarPod-XXXX") must stay valid.
// device_id is PODCRP_DEVICE_ID. BLE links start at MTU 23.
void podcrp_init(podcrp_link_t *link, uint8_t kind, const podcrp_hooks_t *hooks, uint32_t device_id,
                 const char *name);

// A client packet arrived (BLE: one write is one packet). Only queues: false if dropped (queue
// full, empty or longer than 64 B; counted in RX_DROPS). Safe from the BLE task.
bool podcrp_rx_packet(podcrp_link_t *link, const uint8_t *p, uint32_t len);
// Bytes from the USB port in CRP mode, through the level 1 deframer into the RX queue. A bad
// length drops one byte and rescans (ble.md 2.5). loop() only.
void podcrp_rx_bytes(podcrp_link_t *link, const uint8_t *p, uint32_t len);

void podcrp_spool_drain(podcrp_link_t *link);
void podcrp_process_all(podcrp_link_t *link, uint32_t now_ms);
void podcrp_tick(podcrp_link_t *link, uint32_t now_ms);
void podcrp_pass(podcrp_link_t *link, uint32_t now_ms);

// Push a frame as WRITE txn 0 to reg ([04][00][reg][frame], so a retry is byte-identical).
// True if the link took it. It goes only with a session, PUSH on, and: BLE, an empty spool, a
// free slot and 6 + len <= mtu_eff - 3; USB, room in the spool plus 128 B. Otherwise nothing is
// sent and the caller keeps the frame for the next pass.
bool podcrp_push(podcrp_link_t *link, uint32_t reg, const uint8_t *frame, uint32_t len);
// A session is open and its client turned PUSH on. While false, a STATUS counts as sent
// (ble.md 5, "PUSH off") and DATA waits.
bool podcrp_pushes_on(const podcrp_link_t *link);
bool podcrp_in_session(const podcrp_link_t *link);
// Nothing waits in the TX spool (the sleep prologue waits for this, ble.md 6).
bool podcrp_spool_empty(const podcrp_link_t *link);

// The link went (BLE disconnect, USB port closed or back to text): the engine reset, spool, RX
// queue and deframer cleared, PUSH off, BLE back to MTU 23. No hook is called. loop() only.
void podcrp_reset(podcrp_link_t *link);
// The BLE link's ATT MTU changed (clamped to 23..247). Call from loop().
void podcrp_set_mtu(podcrp_link_t *link, uint16_t att_mtu);
// A LOG packet (USB in CRP mode, the `#` lines; module "pod"). Lossy like a push: needs a session
// and room plus 128 B. True if queued.
bool podcrp_log(podcrp_link_t *link, uint8_t level, const char *module, const char *msg);

#ifdef __cplusplus
}
#endif
