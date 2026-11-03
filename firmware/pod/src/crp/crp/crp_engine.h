/*
 * CRP — Channel Register Protocol
 * Protocol engine — top-level API.
 *
 * Usage:
 *   1. Allocate a crp_engine_t and a scratch buffer (static, no malloc)
 *   2. Call crp_engine_init() with config, transport, regions, scratch
 *   3. Call crp_engine_tick() periodically with current time
 *   4. Call crp_engine_process() to handle one incoming packet
 */

#ifndef CRP_ENGINE_H
#define CRP_ENGINE_H

#include "crp_types.h"
#include "crp_packet.h"
#include "crp_region.h"
#include "crp_channel.h"
#include "crp_transport.h"

/*============================================================
 * TxnID tracking (small pool, TxnID as key)
 *============================================================*/

typedef enum {
    CRP_TXN_FREE = 0,
    CRP_TXN_PENDING,
    CRP_TXN_CHANNEL,
} crp_txn_state_t;

typedef struct {
    uint8_t         txn_id;     /* Wire TxnID (1-255), 0 = free slot */
    crp_txn_state_t state;
    uint8_t         channel;
    uint32_t        reg_addr;
    uint32_t        sent_ms;
    uint8_t         retries;
} crp_txn_t;

/*============================================================
 * Application callbacks
 *============================================================*/

typedef struct {
    void (*on_hello)(void *user_ctx, const crp_pkt_hello_t *hello);
    void (*on_bye)(void *user_ctx, const crp_pkt_bye_t *bye);
    void (*on_response)(void *user_ctx, const crp_pkt_response_t *resp);
    void (*on_subscription_data)(void *user_ctx, uint8_t channel,
                                  const uint8_t *data, uint32_t len);
    void (*on_bulk_complete)(void *user_ctx, uint8_t channel, uint32_t total);
    void (*on_channel_closed)(void *user_ctx, uint8_t channel);
    void (*on_liveness_timeout)(void *user_ctx);
    void (*on_txn_timeout)(void *user_ctx, uint8_t txn_id, uint32_t reg);
    void (*on_log)(void *user_ctx, const crp_pkt_log_t *log);
    void *user_ctx;
} crp_callbacks_t;

/*============================================================
 * Engine configuration
 *============================================================*/

typedef struct {
    uint8_t     max_channels;
    uint16_t    max_chunk;
    uint8_t     flags;
    uint32_t    device_id;
    const char *device_name;
    uint8_t     device_name_len;
} crp_engine_config_t;

/*============================================================
 * Engine state
 *============================================================*/

typedef struct {
    /* Configuration */
    crp_engine_config_t config;

    /* Session (negotiated) */
    uint8_t  session_max_channels;
    uint16_t session_max_chunk;
    uint8_t  session_flags;
    uint32_t peer_device_id;
    uint8_t  peer_proto_ver;
    bool     session_active;

    /* Subsystems — regions are shared (pointer), rest are per-engine */
    crp_region_table_t  *regions;
    crp_channel_table_t  channels;
    crp_txn_t            txns[CRP_MAX_TXNS];

    /* Transport */
    crp_transport_t *transport;

    /* Timing */
    uint32_t now_ms;
    uint32_t last_send_ms;
    uint32_t last_recv_ms;

    /* Callbacks */
    crp_callbacks_t callbacks;

    /* Scratch buffer (caller-provided) */
    uint8_t *scratch;
    uint16_t scratch_size;

    /* TxnID allocator (round-robin 1-255) */
    uint8_t next_txn_id;
} crp_engine_t;

/*============================================================
 * API
 *============================================================*/

/**
 * Initialize engine. Regions and scratch buffer are external.
 * scratch must remain valid for the engine's lifetime.
 */
void crp_engine_init(crp_engine_t *engine, const crp_engine_config_t *config,
                      crp_transport_t *transport,
                      crp_region_table_t *regions,
                      uint8_t *scratch, uint16_t scratch_size);

void crp_engine_set_callbacks(crp_engine_t *engine,
                               const crp_callbacks_t *callbacks);

/** Process one incoming packet. Returns true if a packet was handled. */
bool crp_engine_process(crp_engine_t *engine);

/** Periodic tick — heartbeat, liveness, subscriptions, txn timeouts. */
void crp_engine_tick(crp_engine_t *engine, uint32_t now_ms);

void crp_engine_send_hello(crp_engine_t *engine);
void crp_engine_send_bye(crp_engine_t *engine);

uint8_t crp_engine_alloc_txn(crp_engine_t *engine, uint32_t reg_addr);
void    crp_engine_free_txn(crp_engine_t *engine, uint8_t txn_id);

uint8_t crp_engine_read(crp_engine_t *engine, uint32_t reg);
uint8_t crp_engine_read_bulk(crp_engine_t *engine, uint32_t reg,
                              uint32_t offset, uint32_t length);
uint8_t crp_engine_write(crp_engine_t *engine, uint32_t reg,
                           const uint8_t *data, uint16_t len);

/** WRITE with txn_id=0 (fire-and-forget). */
bool crp_engine_write_noack(crp_engine_t *engine, uint32_t reg,
                              const uint8_t *data, uint16_t len);

uint8_t crp_engine_subscribe(crp_engine_t *engine,
                               const uint32_t *regs, uint8_t reg_count,
                               uint32_t interval_us);

uint8_t crp_engine_query_group(crp_engine_t *engine, uint32_t base);
uint8_t crp_engine_query_reg(crp_engine_t *engine, uint32_t reg);

bool crp_engine_end_channel(crp_engine_t *engine, uint8_t channel);
bool crp_engine_end_session(crp_engine_t *engine);
bool crp_engine_seek(crp_engine_t *engine, uint8_t channel, uint32_t offset);
bool crp_engine_pause_channel(crp_engine_t *engine, uint8_t channel);
bool crp_engine_resume_channel(crp_engine_t *engine, uint8_t channel);

void crp_engine_send_nop(crp_engine_t *engine);
void crp_engine_send_log(crp_engine_t *engine, uint8_t level,
                           const char *module, uint8_t module_len,
                           const char *message, uint16_t message_len);

/** Push unsolicited WRITE(txn=0) for a changed register value.
 *  LOCAL PATCH (pod, D45): returns 0 if sent, -1 if not (no session, not ready, refused). */
int crp_engine_push_change(crp_engine_t *engine, uint32_t reg,
                              const uint8_t *data, uint32_t len);

void crp_engine_reset(crp_engine_t *engine);

typedef uint32_t (*crp_get_ms_fn)(void);
crp_status_t crp_engine_read_sync(crp_engine_t *engine, uint32_t reg,
                                    uint8_t *buf, uint32_t *len,
                                    uint32_t timeout_ms,
                                    crp_get_ms_fn get_ms);

#endif /* CRP_ENGINE_H */
