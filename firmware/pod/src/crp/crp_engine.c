/*
 * CRP — Channel Register Protocol
 * Protocol engine implementation.
 *
 * Handles packet dispatch, session management, TxnID tracking,
 * heartbeat/liveness, subscription ticks, and bulk transfer pumping.
 */

#include "crp/crp_engine.h"
#include <string.h>

/*============================================================
 * Internal helpers
 *============================================================*/

static int engine_send_packet(crp_engine_t *engine, const crp_packet_t *pkt)
{
    /* Check transport readiness if the transport supports it */
    if (engine->transport->ready &&
        !engine->transport->ready(engine->transport->ctx)) {
        return -1;
    }

    uint32_t len = crp_packet_serialize(pkt, engine->scratch,
                                         engine->scratch_size);
    if (len == 0) return -1;

    int rc = engine->transport->send(engine->transport->ctx,
                                      engine->scratch, len);
    if (rc == 0) {
        engine->last_send_ms = engine->now_ms;
    }
    return rc;
}

/* Send a packet that has an allocated TxnID. Frees the TxnID on failure.
 * Returns txn on success, 0 on failure. */
static uint8_t engine_send_txn_packet(crp_engine_t *engine,
                                       const crp_packet_t *pkt, uint8_t txn)
{
    if (engine_send_packet(engine, pkt) != 0) {
        crp_engine_free_txn(engine, txn);
        return 0;
    }
    return txn;
}

/* Send a simple txn+u32 request packet (QUERY_GROUP, QUERY_REG, etc.).
 * Allocates a TxnID, builds the packet, sends it. Returns txn or 0. */
static uint8_t engine_send_txn_u32(crp_engine_t *engine,
                                    crp_packet_type_t type, uint32_t addr)
{
    uint8_t txn = crp_engine_alloc_txn(engine, addr);
    if (txn == 0) return 0;

    crp_packet_t pkt = {0};
    pkt.type = type;
    /* All txn+u32 packet structs have identical layout {txn_id, u32}.
     * Write through query_group; serializer reads matching member at same offset. */
    pkt.pkt.query_group.txn_id = txn;
    pkt.pkt.query_group.base   = addr;

    return engine_send_txn_packet(engine, &pkt, txn);
}

static void send_response(crp_engine_t *engine, uint8_t txn_id,
                            uint8_t status, uint32_t reg,
                            const uint8_t *payload, uint16_t payload_len)
{
    crp_packet_t pkt = {0};
    pkt.type = CRP_PKT_RESPONSE;
    pkt.pkt.response.txn_id      = txn_id;
    pkt.pkt.response.status      = status;
    pkt.pkt.response.channel     = CRP_CHANNEL_SESSION; /* 0xFF = not channel-specific */
    pkt.pkt.response.reg         = reg;
    pkt.pkt.response.payload     = payload;
    pkt.pkt.response.payload_len = payload_len;
    engine_send_packet(engine, &pkt);
}

/* Send an error response with a channel number (for channel-level errors). */
static void send_error(crp_engine_t *engine, uint8_t txn_id,
                         uint8_t channel, uint8_t code, uint32_t reg)
{
    crp_packet_t pkt = {0};
    pkt.type = CRP_PKT_RESPONSE;
    pkt.pkt.response.txn_id      = txn_id;
    pkt.pkt.response.status      = code;
    pkt.pkt.response.channel     = channel;
    pkt.pkt.response.reg         = reg;
    pkt.pkt.response.payload     = NULL;
    pkt.pkt.response.payload_len = 0;
    engine_send_packet(engine, &pkt);
}

/*============================================================
 * TxnID management
 *============================================================*/

/* Find a txn slot by wire TxnID. Returns NULL if not found. */
static crp_txn_t *find_txn(crp_engine_t *engine, uint8_t txn_id)
{
    for (uint8_t i = 0; i < CRP_MAX_TXNS; i++) {
        if (engine->txns[i].txn_id == txn_id) {
            return &engine->txns[i];
        }
    }
    return NULL;
}

uint8_t crp_engine_alloc_txn(crp_engine_t *engine, uint32_t reg_addr)
{
    /* Find a free slot in the pool */
    crp_txn_t *slot = NULL;
    for (uint8_t i = 0; i < CRP_MAX_TXNS; i++) {
        if (engine->txns[i].txn_id == 0) {
            slot = &engine->txns[i];
            break;
        }
    }
    if (!slot) return 0;  /* Pool full */

    /* Allocate a wire TxnID (round-robin 1-255) */
    for (uint16_t i = 0; i < 255; i++) {
        uint8_t id = engine->next_txn_id;
        engine->next_txn_id++;
        if (engine->next_txn_id == 0) engine->next_txn_id = 1;

        /* Make sure this ID isn't already in use */
        if (!find_txn(engine, id)) {
            slot->txn_id   = id;
            slot->state    = CRP_TXN_PENDING;
            slot->reg_addr = reg_addr;
            slot->sent_ms  = engine->now_ms;
            slot->retries  = 0;
            slot->channel  = 0;
            return id;
        }
    }
    return 0;
}

void crp_engine_free_txn(crp_engine_t *engine, uint8_t txn_id)
{
    if (txn_id == 0) return;
    crp_txn_t *t = find_txn(engine, txn_id);
    if (t) memset(t, 0, sizeof(*t));
}

/*============================================================
 * Initialization and reset
 *============================================================*/

void crp_engine_init(crp_engine_t *engine, const crp_engine_config_t *config,
                      crp_transport_t *transport,
                      crp_region_table_t *regions,
                      uint8_t *scratch, uint16_t scratch_size)
{
    memset(engine, 0, sizeof(*engine));
    engine->config       = *config;
    engine->transport    = transport;
    engine->regions      = regions;
    engine->scratch      = scratch;
    engine->scratch_size = scratch_size;
    engine->next_txn_id  = 1;

    crp_channel_table_init(&engine->channels, config->max_channels);
}

void crp_engine_set_callbacks(crp_engine_t *engine,
                               const crp_callbacks_t *callbacks)
{
    engine->callbacks = *callbacks;
}

void crp_engine_reset(crp_engine_t *engine)
{
    /* Clean up active channels before releasing — call region cleanup callbacks
     * so regions can clear their internal state (e.g., bulk.active flags). */
    for (uint8_t i = 0; i < engine->channels.max_channels; i++) {
        crp_channel_t *ch = &engine->channels.channels[i];
        if (ch->state != CRP_CH_IDLE && ch->region) {
            crp_chan_base_type_t base = crp_chantype_base(ch->chan_type);
            if (base == CRP_CHAN_BULK_READ && ch->region->ops->bulk_read_close) {
                ch->region->ops->bulk_read_close(ch->region->ctx, ch->reg_addr);
            }
            if (base == CRP_CHAN_BULK_WRITE && ch->region->ops->bulk_write_close) {
                ch->region->ops->bulk_write_close(ch->region->ctx, ch->reg_addr);
            }
        }
    }

    crp_channel_release_all(&engine->channels);
    memset(engine->txns, 0, sizeof(engine->txns));
    engine->session_active = false;
    engine->next_txn_id = 1;
}

/*============================================================
 * Packet handlers — device-side (responding to requests)
 *============================================================*/

static void handle_hello(crp_engine_t *engine, const crp_pkt_hello_t *hello)
{
    /* Version check — reject incompatible peers */
    if (hello->proto_ver < CRP_PROTO_MIN_VERSION) {
        /* Send BYE with version mismatch — peer is too old */
        crp_engine_send_bye(engine);
        return;
    }

    /* HELLO resets all state */
    crp_engine_reset(engine);

    /* Negotiate session parameters (minimum of ours and peer's) */
    engine->session_max_channels = (hello->max_channels < engine->config.max_channels)
                                    ? hello->max_channels : engine->config.max_channels;
    engine->session_max_chunk    = (hello->max_chunk < engine->config.max_chunk)
                                    ? hello->max_chunk : engine->config.max_chunk;
    engine->session_flags        = hello->flags & engine->config.flags;
    engine->peer_device_id       = hello->device_id;
    engine->peer_proto_ver       = hello->proto_ver;
    engine->session_active       = true;

    /* Re-init channel table with negotiated max */
    crp_channel_table_init(&engine->channels, engine->session_max_channels);

    /* Send our HELLO back */
    crp_engine_send_hello(engine);

    if (engine->callbacks.on_hello) {
        engine->callbacks.on_hello(engine->callbacks.user_ctx, hello);
    }
}

static void handle_bye(crp_engine_t *engine, const crp_pkt_bye_t *bye)
{
    crp_engine_reset(engine);

    if (engine->callbacks.on_bye) {
        engine->callbacks.on_bye(engine->callbacks.user_ctx, bye);
    }
}

/**
 * Generic region lookup with error response.
 * Returns the region, or NULL (after sending UNKNOWN_REG response).
 */
static crp_region_t *lookup_region(crp_engine_t *engine, uint8_t txn_id,
                                    uint32_t reg)
{
    crp_region_t *r = crp_region_lookup(engine->regions, reg);
    if (!r) {
        send_response(engine, txn_id, CRP_STATUS_UNKNOWN_REG, reg, NULL, 0);
    }
    return r;
}

/**
 * Generic read-style region op: lookup region, check op, call, respond.
 * The op reads into a buffer and returns a status + length.
 * Returns the region pointer (for further use), or NULL if handled.
 */
typedef crp_status_t (*reg_read_op_t)(void *ctx, uint32_t reg,
                                       uint8_t *buf, uint32_t *len);

static crp_region_t *handle_read_op(crp_engine_t *engine, uint8_t txn_id,
                                      uint32_t reg, reg_read_op_t op)
{
    crp_region_t *r = lookup_region(engine, txn_id, reg);
    if (!r) return NULL;

    if (!op) {
        send_response(engine, txn_id, CRP_STATUS_ACCESS_DENIED, reg, NULL, 0);
        return NULL;
    }

    /* Read into engine scratch buffer (after RESPONSE header space).
     * RESPONSE header: type(1)+txn(1)+status(1)+ch(1)+reg(4) = 8 bytes.
     * This allows zero-copy: read data lands where serialize needs it. */
    uint8_t *read_buf = engine->scratch + 8;
    uint32_t read_len = engine->scratch_size - 8;
    crp_status_t st = op(r->ctx, reg, read_buf, &read_len);

    if (st != CRP_STATUS_OK) {
        send_response(engine, txn_id, st, reg, NULL, 0);
    } else {
        send_response(engine, txn_id, CRP_STATUS_OK, reg,
                       read_buf, (uint16_t)read_len);
    }
    return r;
}

static void handle_write(crp_engine_t *engine, const crp_pkt_write_t *pkt)
{
    bool wants_ack = (pkt->txn_id != 0);

    crp_region_t *r = wants_ack
        ? lookup_region(engine, pkt->txn_id, pkt->reg)
        : crp_region_lookup(engine->regions, pkt->reg);
    if (!r) return;

    /* LOCAL (2026-05-12): Bulk write initiation.
     *
     * Upstream v0.0B added bulk_write_open / bulk_write_chunk / bulk_write_close
     * region ops and the handle_bulk_data path for the chunk stream, but never
     * wired up the *initiation* — host sends WRITE with 4-byte total_size
     * payload, device opens a channel, replies with CHANNEL_OPEN, then host
     * streams BULK_DATA. This branch fills that gap. Should be upstreamed.
     *
     * Allocate channel BEFORE calling bulk_write_open so that a failed alloc
     * cannot leave the region in an orphaned "open" state. */
    if (r->ops->bulk_write_open && pkt->payload_len == 4) {
        uint32_t total_size = crp_read_u32(pkt->payload);

        int ch_num = crp_channel_alloc(&engine->channels);
        if (ch_num < 0) {
            send_error(engine, pkt->txn_id, 0, CRP_STATUS_NO_CHANNELS, pkt->reg);
            return;
        }

        crp_status_t st = r->ops->bulk_write_open(r->ctx, pkt->reg, total_size);
        if (st != CRP_STATUS_OK) {
            crp_channel_release(&engine->channels, (uint8_t)ch_num);
            send_response(engine, pkt->txn_id, st, pkt->reg, NULL, 0);
            return;
        }

        crp_channel_t *ch = crp_channel_get(&engine->channels, (uint8_t)ch_num);
        ch->state            = CRP_CH_ACTIVE;
        ch->chan_type        = CRP_CHANTYPE_BULK_WR;
        ch->txn_id           = pkt->txn_id;
        ch->reg_addr         = pkt->reg;
        ch->region           = r;
        ch->bulk.total_size  = total_size;
        ch->offset           = 0;
        ch->last_activity_ms = engine->now_ms;

        if (pkt->txn_id > 0) {
            crp_txn_t *t = find_txn(engine, pkt->txn_id);
            if (t) { t->state = CRP_TXN_CHANNEL; t->channel = (uint8_t)ch_num; }
        }

        /* Send CHANNEL_OPEN back to client */
        crp_packet_t open_pkt = {0};
        open_pkt.type = CRP_PKT_CHANNEL_OPEN;
        open_pkt.pkt.channel_open.txn_id      = pkt->txn_id;
        open_pkt.pkt.channel_open.channel     = (uint8_t)ch_num;
        open_pkt.pkt.channel_open.chan_type   = CRP_CHANTYPE_BULK_WR;
        open_pkt.pkt.channel_open.reg         = pkt->reg;
        open_pkt.pkt.channel_open.total_size  = total_size;
        memset(open_pkt.pkt.channel_open.nonce, 0, 12);
        engine_send_packet(engine, &open_pkt);
        return;
    }

    if (!r->ops->write) {
        if (wants_ack) {
            send_response(engine, pkt->txn_id, CRP_STATUS_ACCESS_DENIED,
                           pkt->reg, NULL, 0);
        }
        return;
    }

    crp_status_t st = r->ops->write(r->ctx, pkt->reg, pkt->payload,
                                     pkt->payload_len);
    if (wants_ack) {
        send_response(engine, pkt->txn_id, st, pkt->reg, NULL, 0);
    }
}

static void handle_read(crp_engine_t *engine, const crp_pkt_read_t *pkt)
{
    crp_region_t *r = crp_region_lookup(engine->regions, pkt->reg);
    if (!r) {
        send_response(engine, pkt->txn_id, CRP_STATUS_UNKNOWN_REG,
                       pkt->reg, NULL, 0);
        return;
    }

    /* Check register size to decide inline vs bulk. Variable-size BLOB
     * endpoints declare size=0 (the host picks the length at read time),
     * which is the bulk-channel convention — always route those through
     * bulk_read_open, never the inline path. */
    bool use_bulk = false;
    if (r->ops->bulk_read_open && r->ops->query_reg) {
        crp_reg_info_t info;
        if (r->ops->query_reg(r->ctx, pkt->reg, &info) == CRP_STATUS_OK) {
            use_bulk = (info.size == 0) || (info.size > engine->session_max_chunk);
        }
    } else if (r->ops->bulk_read_open && !r->ops->read) {
        use_bulk = true;
    }

    /* Bulk read — open a streaming channel */
    if (use_bulk) {
        int ch_num = crp_channel_alloc(&engine->channels);
        if (ch_num < 0) {
            send_error(engine, pkt->txn_id, 0, CRP_STATUS_NO_CHANNELS, pkt->reg);
            return;
        }

        /* LOCAL (2026-05-19): if the READ packet carries a 4-byte payload,
         * treat it as the host's requested total_size — needed for
         * variable-size BLOB endpoints (HBM data drain, RX capture, etc.)
         * where the device can't know the length on its own. Falls back
         * to "let the region decide" when no payload is present. */
        uint32_t requested_size = (pkt->payload && pkt->payload_len >= 4)
                                     ? crp_read_u32(pkt->payload) : 0;
        uint32_t total_size = requested_size;
        crp_status_t st = r->ops->bulk_read_open(r->ctx, pkt->reg,
                                                   0, requested_size,
                                                   &total_size);
        if (st != CRP_STATUS_OK) {
            crp_channel_release(&engine->channels, (uint8_t)ch_num);
            send_response(engine, pkt->txn_id, st, pkt->reg, NULL, 0);
            return;
        }

        crp_channel_t *ch = crp_channel_get(&engine->channels, (uint8_t)ch_num);
        ch->state       = CRP_CH_ACTIVE;
        ch->chan_type    = CRP_CHANTYPE_BULK_RD;
        ch->txn_id      = pkt->txn_id;
        ch->reg_addr    = pkt->reg;
        ch->region      = r;
        ch->bulk.total_size  = total_size;
        ch->offset      = 0;
        ch->last_activity_ms = engine->now_ms;

        if (pkt->txn_id > 0) {
            crp_txn_t *t = find_txn(engine, pkt->txn_id);
            if (t) { t->state = CRP_TXN_CHANNEL; t->channel = (uint8_t)ch_num; }
        }

        crp_packet_t open_pkt = {0};
        open_pkt.type = CRP_PKT_CHANNEL_OPEN;
        open_pkt.pkt.channel_open.txn_id         = pkt->txn_id;
        open_pkt.pkt.channel_open.channel        = (uint8_t)ch_num;
        open_pkt.pkt.channel_open.chan_type       = CRP_CHANTYPE_BULK_RD;
        open_pkt.pkt.channel_open.reg            = pkt->reg;
        open_pkt.pkt.channel_open.total_size     = total_size;
        memset(open_pkt.pkt.channel_open.nonce, 0, 12);
        engine_send_packet(engine, &open_pkt);
        return;
    }

    /* Scalar/small register — inline in RESPONSE via generic read op */
    handle_read_op(engine, pkt->txn_id, pkt->reg, r->ops->read);
}

static void handle_response(crp_engine_t *engine, const crp_pkt_response_t *pkt)
{
    /* We received a RESPONSE — we must be the client side.
     * Both success and error responses come through here (ERROR merged into RESPONSE). */
    if (pkt->txn_id > 0) {
        crp_txn_t *txn = find_txn(engine, pkt->txn_id);
        /* LOCAL PATCH (coil, 2026-07-16): find_txn returns NULL for an unknown
         * txn_id (late/duplicate reply, or a hostile host) — upstream derefs it
         * unconditionally, which hard-faults the MCU. */
        if (txn && txn->state == CRP_TXN_PENDING) {
            crp_engine_free_txn(engine, pkt->txn_id);
        }
    }

    if (engine->callbacks.on_response) {
        engine->callbacks.on_response(engine->callbacks.user_ctx, pkt);
    }
}

static void handle_subscribe(crp_engine_t *engine,
                               const crp_pkt_subscribe_t *pkt)
{
    if (pkt->reg_count == 0) {
        send_error(engine, pkt->txn_id, 0, CRP_STATUS_INVALID_VALUE, 0);
        return;
    }

    /* Validate all registers are subscribable */
    for (uint8_t i = 0; i < pkt->reg_count; i++) {
        uint32_t reg = crp_read_u32(&pkt->regs_raw[i * 4]);
        crp_region_t *r = crp_region_lookup(engine->regions, reg);
        if (!r) {
            send_error(engine, pkt->txn_id, 0, CRP_STATUS_UNKNOWN_REG, reg);
            return;
        }
        if (!r->ops->subscribe_read) {
            send_error(engine, pkt->txn_id, 0, CRP_STATUS_NOT_SUBSCRIBABLE, reg);
            return;
        }
        /* Also check the register's subscribable flag via query_reg */
        if (r->ops->query_reg) {
            crp_reg_info_t info;
            crp_status_t st = r->ops->query_reg(r->ctx, reg, &info);
            if (st != CRP_STATUS_OK) {
                send_error(engine, pkt->txn_id, 0, CRP_STATUS_UNKNOWN_REG, reg);
                return;
            }
            if (!(info.flags & CRP_REG_SUBSCRIBABLE)) {
                send_error(engine, pkt->txn_id, 0, CRP_STATUS_NOT_SUBSCRIBABLE, reg);
                return;
            }
        }
    }

    /* LOCAL PATCH (coil, 2026-07-16): duplicate-SUBSCRIBE idempotency. The
     * browser client retries a timed-out SUBSCRIBE with the SAME txn_id (its
     * CHANNEL_OPEN may have been lost to TX backpressure); the stock engine
     * would open a second channel — double pushes plus one leaked channel per
     * retry. If an active subscription channel already matches this exact
     * request (txn + interval + reg list), re-answer with it instead. */
    if (pkt->txn_id > 0) {
        crp_channel_t *dup = crp_channel_find_by_txn(&engine->channels, pkt->txn_id);
        if (dup && dup->state == CRP_CH_ACTIVE &&
            (crp_chantype_base(dup->chan_type) == CRP_CHAN_SUBSCRIPTION ||
             crp_chantype_base(dup->chan_type) == CRP_CHAN_PUSH) &&
            dup->sub.interval_us == pkt->interval_us) {
            uint8_t want = (pkt->reg_count > CRP_MAX_SUB_REGS)
                            ? CRP_MAX_SUB_REGS : pkt->reg_count;
            bool same = (dup->sub.reg_count == want);
            for (uint8_t i = 0; same && i < want; i++) {
                if (dup->sub.regs[i] != crp_read_u32(&pkt->regs_raw[i * 4]))
                    same = false;
            }
            if (same) {
                crp_packet_t open_pkt;
                open_pkt.type = CRP_PKT_CHANNEL_OPEN;
                open_pkt.pkt.channel_open.txn_id     = pkt->txn_id;
                open_pkt.pkt.channel_open.channel    = dup->channel_num;
                open_pkt.pkt.channel_open.chan_type  = dup->chan_type;
                open_pkt.pkt.channel_open.reg        = dup->reg_addr;
                open_pkt.pkt.channel_open.total_size = 0;
                memset(open_pkt.pkt.channel_open.nonce, 0, 12);
                engine_send_packet(engine, &open_pkt);
                return;
            }
        }
    }

    /* Allocate channel */
    int ch_num = crp_channel_alloc(&engine->channels);
    if (ch_num < 0) {
        send_error(engine, pkt->txn_id, 0, CRP_STATUS_NO_CHANNELS, 0);
        return;
    }

    crp_channel_t *ch = crp_channel_get(&engine->channels, (uint8_t)ch_num);
    ch->state       = CRP_CH_ACTIVE;
    ch->chan_type    = CRP_CHANTYPE_SUB;
    ch->txn_id      = pkt->txn_id;
    ch->reg_addr    = crp_read_u32(&pkt->regs_raw[0]);
    ch->region      = crp_region_lookup(engine->regions, ch->reg_addr);
    ch->sub.interval_us = pkt->interval_us;
    ch->sub.last_tick_ms = engine->now_ms;
    ch->last_activity_ms = engine->now_ms;

    /* Copy register list and cache region pointers */
    ch->sub.reg_count = (pkt->reg_count > CRP_MAX_SUB_REGS)
                         ? CRP_MAX_SUB_REGS : pkt->reg_count;
    for (uint8_t i = 0; i < ch->sub.reg_count; i++) {
        ch->sub.regs[i] = crp_read_u32(&pkt->regs_raw[i * 4]);
        ch->sub.reg_regions[i] = crp_region_lookup(engine->regions,
                                                     ch->sub.regs[i]);
    }

    /* Send CHANNEL_OPEN */
    crp_packet_t open_pkt;
    open_pkt.type = CRP_PKT_CHANNEL_OPEN;
    open_pkt.pkt.channel_open.txn_id         = pkt->txn_id;
    open_pkt.pkt.channel_open.channel        = (uint8_t)ch_num;
    open_pkt.pkt.channel_open.chan_type       = CRP_CHANTYPE_SUB;
    open_pkt.pkt.channel_open.reg            = ch->reg_addr;
    open_pkt.pkt.channel_open.total_size     = 0;
    memset(open_pkt.pkt.channel_open.nonce, 0, 12);
    engine_send_packet(engine, &open_pkt);
}

static void handle_channel_open(crp_engine_t *engine,
                                  const crp_pkt_channel_open_t *pkt)
{
    /* We received CHANNEL_OPEN — we're the client side.
     * Set up our channel tracking. */
    if (pkt->channel >= engine->channels.max_channels) return;

    crp_channel_t *ch = crp_channel_get(&engine->channels, pkt->channel);
    if (!ch) return;

    ch->state            = CRP_CH_ACTIVE;
    ch->channel_num      = pkt->channel;
    ch->chan_type         = pkt->chan_type;
    ch->txn_id           = pkt->txn_id;
    ch->reg_addr         = pkt->reg;
    ch->bulk.total_size       = pkt->total_size;
    ch->offset           = 0;
    ch->bulk.expected_offset  = 0;
    ch->last_activity_ms = engine->now_ms;
    ch->paused           = false;
    memcpy(ch->nonce, pkt->nonce, 12);

    /* Update TxnID tracking */
    if (pkt->txn_id > 0) {
        crp_txn_t *t = find_txn(engine, pkt->txn_id);
        if (t) { t->state = CRP_TXN_CHANNEL; t->channel = pkt->channel; }
    }
}

static void handle_bulk_data(crp_engine_t *engine,
                               const crp_pkt_bulk_data_t *pkt)
{
    crp_channel_t *ch = crp_channel_get(&engine->channels, pkt->channel);
    if (!ch || ch->state != CRP_CH_ACTIVE) return;

    ch->last_activity_ms = engine->now_ms;

    /* Device side: receiving bulk write data */
    if (crp_chantype_base(ch->chan_type) == CRP_CHAN_BULK_WRITE) {
        if (ch->region && ch->region->ops->bulk_write_chunk) {
            ch->region->ops->bulk_write_chunk(ch->region->ctx, ch->reg_addr,
                                               pkt->offset, pkt->payload,
                                               pkt->payload_len);
        }
        ch->offset = pkt->offset + pkt->payload_len;
    }

    /* Client side: receiving bulk read data */
    if (crp_chantype_base(ch->chan_type) == CRP_CHAN_BULK_READ) {
        /* Gap detection */
        if (pkt->offset != ch->bulk.expected_offset) {
            /* Gap detected — send SEEK to request retransmission */
            crp_engine_seek(engine, pkt->channel, ch->bulk.expected_offset);
            return;
        }
        ch->offset          = pkt->offset + pkt->payload_len;
        ch->bulk.expected_offset = ch->offset;

        /* Deliver to callback */
        if (engine->callbacks.on_subscription_data) {
            engine->callbacks.on_subscription_data(
                engine->callbacks.user_ctx, pkt->channel,
                pkt->payload, pkt->payload_len);
        }
    }

    /* Subscription/push: deliver data */
    if (crp_chantype_base(ch->chan_type) == CRP_CHAN_SUBSCRIPTION ||
        crp_chantype_base(ch->chan_type) == CRP_CHAN_PUSH) {
        ch->offset = pkt->offset + pkt->payload_len;

        if (engine->callbacks.on_subscription_data) {
            engine->callbacks.on_subscription_data(
                engine->callbacks.user_ctx, pkt->channel,
                pkt->payload, pkt->payload_len);
        }
    }
}

static void handle_seek(crp_engine_t *engine, const crp_pkt_seek_t *pkt)
{
    crp_channel_t *ch = crp_channel_get(&engine->channels, pkt->channel);
    if (!ch) return;
    /* Allow SEEK on ACTIVE or CLOSING (gap recovery after END) */
    if (ch->state != CRP_CH_ACTIVE && ch->state != CRP_CH_CLOSING) return;

    ch->last_activity_ms = engine->now_ms;

    if (pkt->offset == CRP_OFFSET_PAUSE) {
        /* Pause — stop sending data on this channel */
        ch->paused = true;
    } else {
        /* Resume or reposition */
        ch->paused = false;
        ch->offset = pkt->offset;
        /* Reactivate closing channels for gap recovery */
        if (ch->state == CRP_CH_CLOSING) {
            ch->state = CRP_CH_ACTIVE;
        }
    }
}

static void handle_end(crp_engine_t *engine, const crp_pkt_end_t *pkt)
{
    if (pkt->channel == CRP_CHANNEL_SESSION) {
        /* END 0xFF = end entire session */
        crp_engine_reset(engine);
        return;
    }

    crp_channel_t *ch = crp_channel_get(&engine->channels, pkt->channel);
    if (!ch) return;

    if (ch->state == CRP_CH_ACTIVE || ch->state == CRP_CH_OPENING) {
        /* Peer wants to close — send our END back, then release */
        crp_packet_t end_pkt;
        end_pkt.type = CRP_PKT_END;
        end_pkt.pkt.end.channel = pkt->channel;
        engine_send_packet(engine, &end_pkt);

        /* Close any bulk streams */
        if (ch->region) {
            crp_chan_base_type_t base = crp_chantype_base(ch->chan_type);
            if (base == CRP_CHAN_BULK_READ && ch->region->ops->bulk_read_close) {
                ch->region->ops->bulk_read_close(ch->region->ctx, ch->reg_addr);
            }
            if (base == CRP_CHAN_BULK_WRITE && ch->region->ops->bulk_write_close) {
                ch->region->ops->bulk_write_close(ch->region->ctx, ch->reg_addr);
            }
        }

        /* Free TxnID if still held */
        if (ch->txn_id > 0) {
            crp_engine_free_txn(engine, ch->txn_id);
        }

        if (engine->callbacks.on_channel_closed) {
            engine->callbacks.on_channel_closed(engine->callbacks.user_ctx,
                                                 pkt->channel);
        }

        crp_channel_release(&engine->channels, pkt->channel);

    } else if (ch->state == CRP_CH_CLOSING) {
        /* We sent END already, this is the confirmation */
        if (ch->txn_id > 0) {
            crp_engine_free_txn(engine, ch->txn_id);
        }

        if (engine->callbacks.on_channel_closed) {
            engine->callbacks.on_channel_closed(engine->callbacks.user_ctx,
                                                 pkt->channel);
        }

        crp_channel_release(&engine->channels, pkt->channel);
    }
    /* If IDLE, silently ignore (duplicate END) */
}

static void handle_query_group(crp_engine_t *engine,
                                 const crp_pkt_query_group_t *pkt)
{
    uint8_t payload[25];  /* 9 bytes + optional 16-byte nonce */
    crp_region_t *r = crp_region_lookup(engine->regions, pkt->base);
    if (!r || !r->ops->query_group) {
        memset(payload, 0, 9);
        send_response(engine, pkt->txn_id, CRP_STATUS_OK,
                       pkt->base, payload, 9);
        return;
    }

    crp_group_info_t info;
    memset(&info, 0, sizeof(info));
    crp_status_t st = r->ops->query_group(r->ctx, pkt->base, &info);
    if (st != CRP_STATUS_OK) {
        memset(payload, 0, 9);
        send_response(engine, pkt->txn_id, CRP_STATUS_OK,
                       pkt->base, payload, 9);
        return;
    }

    /* Encode: [mask(4)] [flags(1)] [count(4)] [nonce(16) if locked] */
    crp_write_u32(&payload[0], info.mask);
    payload[4] = info.flags;
    crp_write_u32(&payload[5], info.count);

    uint16_t plen = 9;
    if (info.flags & (CRP_GROUP_READ_LOCKED | CRP_GROUP_WRITE_LOCKED)) {
        memcpy(&payload[9], info.nonce, 16);
        plen = 25;
    }

    send_response(engine, pkt->txn_id, CRP_STATUS_OK, pkt->base,
                   payload, plen);
}

static void handle_unlock(crp_engine_t *engine, const crp_pkt_unlock_t *pkt)
{
    crp_region_t *r = crp_region_lookup(engine->regions, pkt->group);
    if (!r || !r->ops->unlock) {
        send_response(engine, pkt->txn_id, CRP_STATUS_ACCESS_DENIED,
                       pkt->group, NULL, 0);
        return;
    }

    crp_status_t st = r->ops->unlock(r->ctx, pkt->group, pkt->proof);
    send_response(engine, pkt->txn_id, st, pkt->group, NULL, 0);
}

static void handle_query_device(crp_engine_t *engine,
                                  const crp_pkt_query_device_t *pkt)
{
    /* Build RESPONSE payload:
     * [mask(4LE)] [flags(1)] [group_count(4LE)] [device_name(var)] */
    uint8_t payload[256];
    uint16_t plen = 0;

    /* Mask: 0x00000000 for a direct device */
    crp_write_u32(&payload[plen], 0x00000000);
    plen += 4;

    /* Flags: use our config flags */
    payload[plen++] = engine->config.flags;

    /* GroupCount: count active regions */
    uint32_t group_count = 0;
    for (uint8_t i = 0; i < engine->regions->count; i++) {
        if (engine->regions->regions[i].active) {
            group_count++;
        }
    }
    crp_write_u32(&payload[plen], group_count);
    plen += 4;

    /* DeviceName (variable length) */
    if (engine->config.device_name && engine->config.device_name_len > 0) {
        uint8_t nlen = engine->config.device_name_len;
        if (plen + nlen > sizeof(payload)) nlen = (uint8_t)(sizeof(payload) - plen);
        memcpy(&payload[plen], engine->config.device_name, nlen);
        plen += nlen;
    }

    send_response(engine, pkt->txn_id, CRP_STATUS_OK, pkt->base,
                   payload, plen);
}

static void handle_query_reg(crp_engine_t *engine,
                               const crp_pkt_query_reg_t *pkt)
{
    /* LOCAL PATCH (coil, 2026-07-16): a QUERY_REG miss replies with status
     * UNKNOWN_REG instead of the upstream OK + 12 zero bytes. The browser
     * client's discovery walk counts every status-OK reply against the group
     * count, so zero-stuffed misses at the map's address gaps made the walk
     * terminate before the tail registers (FAULT/BUS_V). Matches the
     * coil-webdriver sim byte-for-byte. */
    crp_region_t *r = crp_region_lookup(engine->regions, pkt->reg);
    if (!r || !r->ops->query_reg) {
        send_error(engine, pkt->txn_id, 0xFF, CRP_STATUS_UNKNOWN_REG, pkt->reg);
        return;
    }

    crp_reg_info_t info;
    memset(&info, 0, sizeof(info));
    crp_status_t st = r->ops->query_reg(r->ctx, pkt->reg, &info);
    if (st != CRP_STATUS_OK) {
        send_error(engine, pkt->txn_id, 0xFF, CRP_STATUS_UNKNOWN_REG, pkt->reg);
        return;
    }

    /* Encode reg info as RESPONSE payload:
     * [data_type(1)] [access(1)] [flags(1)] [size(4)] [min_interval(4)] [name_len(1)] [name...] */
    uint8_t payload[256];
    uint16_t plen = 0;
    payload[plen++] = info.data_type;
    payload[plen++] = info.access;
    payload[plen++] = info.flags;
    crp_write_u32(&payload[plen], info.size);
    plen += 4;
    crp_write_u32(&payload[plen], info.min_interval_us);
    plen += 4;

    if (info.name) {
        uint8_t nlen = 0;
        while (info.name[nlen] && nlen < 200) nlen++;
        payload[plen++] = nlen;
        memcpy(&payload[plen], info.name, nlen);
        plen += nlen;
    } else {
        payload[plen++] = 0;
    }

    send_response(engine, pkt->txn_id, CRP_STATUS_OK, pkt->reg,
                   payload, plen);
}

/*============================================================
 * Dispatch table — maps packet type byte to handler function
 *============================================================*/

typedef void (*pkt_handler_t)(crp_engine_t *, const crp_packet_t *);

/* Thin wrappers: adapt (engine, packet_t*) → (engine, specific_pkt_t*) */
static void d_hello(crp_engine_t *e, const crp_packet_t *p)       { handle_hello(e, &p->pkt.hello); }
static void d_bye(crp_engine_t *e, const crp_packet_t *p)         { handle_bye(e, &p->pkt.bye); }
static void d_write(crp_engine_t *e, const crp_packet_t *p)       { handle_write(e, &p->pkt.write); }
/* WRITE_NOACK removed — txn_id=0 in WRITE handles fire-and-forget */
static void d_read(crp_engine_t *e, const crp_packet_t *p)        { handle_read(e, &p->pkt.read); }
static void d_subscribe(crp_engine_t *e, const crp_packet_t *p)   { handle_subscribe(e, &p->pkt.subscribe); }
static void d_push(crp_engine_t *e, const crp_packet_t *p)        { handle_subscribe(e, &p->pkt.push_setup); }
static void d_response(crp_engine_t *e, const crp_packet_t *p)    { handle_response(e, &p->pkt.response); }
static void d_chan_open(crp_engine_t *e, const crp_packet_t *p)    { handle_channel_open(e, &p->pkt.channel_open); }
static void d_bulk_data(crp_engine_t *e, const crp_packet_t *p)   { handle_bulk_data(e, &p->pkt.bulk_data); }
static void d_seek(crp_engine_t *e, const crp_packet_t *p)        { handle_seek(e, &p->pkt.seek); }
static void d_query_reg(crp_engine_t *e, const crp_packet_t *p)   { handle_query_reg(e, &p->pkt.query_reg); }
static void d_query_grp(crp_engine_t *e, const crp_packet_t *p)   { handle_query_group(e, &p->pkt.query_group); }
static void d_end(crp_engine_t *e, const crp_packet_t *p)         { handle_end(e, &p->pkt.end); }
static void d_query_dev(crp_engine_t *e, const crp_packet_t *p)   { handle_query_device(e, &p->pkt.query_device); }
static void d_unlock(crp_engine_t *e, const crp_packet_t *p)     { handle_unlock(e, &p->pkt.unlock); }
static void d_log(crp_engine_t *e, const crp_packet_t *p) {
    if (e->callbacks.on_log) e->callbacks.on_log(e->callbacks.user_ctx, &p->pkt.log);
}

/* Table indexed by packet type byte. Max type is UNLOCK = 0x1C. */
#define DISPATCH_TABLE_SIZE (CRP_PKT_UNLOCK + 1)

static const pkt_handler_t dispatch_table[DISPATCH_TABLE_SIZE] = {
    [CRP_PKT_NOP]          = NULL,          /* no-op: receipt updated last_recv_ms */
    [CRP_PKT_HELLO]        = d_hello,
    [CRP_PKT_BYE]          = d_bye,
    [CRP_PKT_WRITE]        = d_write,
    /* 0x05 removed — WRITE with txn=0 replaces WRITE_NOACK */
    [CRP_PKT_READ]         = d_read,
    [CRP_PKT_SUBSCRIBE]    = d_subscribe,
    [CRP_PKT_PUSH_SETUP]   = d_push,
    [CRP_PKT_QUERY_REG]    = d_query_reg,
    [CRP_PKT_QUERY_GROUP]  = d_query_grp,
    [CRP_PKT_RESPONSE]     = d_response,
    [CRP_PKT_CHANNEL_OPEN] = d_chan_open,
    [CRP_PKT_BULK_DATA]    = d_bulk_data,
    [CRP_PKT_SEEK]         = d_seek,
    [CRP_PKT_END]          = d_end,
    [CRP_PKT_LOG]          = d_log,
    [CRP_PKT_QUERY_DEVICE] = d_query_dev,
    [CRP_PKT_UNLOCK]       = d_unlock,
};

/*============================================================
 * Main packet dispatch
 *============================================================*/

bool crp_engine_process(crp_engine_t *engine)
{
    /* Receive into scratch buffer (shared with send — not re-entrant) */
    uint32_t len = engine->transport->recv(engine->transport->ctx,
                                            engine->scratch, engine->scratch_size);
    if (len == 0) return false;

    engine->last_recv_ms = engine->now_ms;

    /* Parse */
    crp_packet_t pkt = {0};
    crp_parse_result_t result = crp_packet_parse(engine->scratch, len, &pkt);

    if (result == CRP_PARSE_UNKNOWN_TYPE) {
        return true;  /* Silently discard unknown types per spec */
    }
    if (result == CRP_PARSE_TOO_SHORT) {
        return true;  /* Silently discard truncated packets */
    }

    /* Require active session for everything except HELLO and NOP */
    if (!engine->session_active &&
        pkt.type != CRP_PKT_HELLO && pkt.type != CRP_PKT_NOP) {
        return true;  /* Ignore pre-session packets */
    }

    /* Dispatch via function pointer table */
    if (pkt.type < sizeof(dispatch_table) / sizeof(dispatch_table[0])) {
        pkt_handler_t handler = dispatch_table[pkt.type];
        if (handler) {
            handler(engine, &pkt);
        }
    }

    return true;
}

/*============================================================
 * Periodic tick
 *============================================================*/

/* ---- Tick helpers ---- */

static void engine_tick_heartbeat(crp_engine_t *engine, uint32_t now_ms)
{
    if (now_ms - engine->last_send_ms >= CRP_HEARTBEAT_MS) {
        crp_engine_send_nop(engine);
    }

    if (engine->last_recv_ms > 0 &&
        now_ms - engine->last_recv_ms >= CRP_TIMEOUT_LIVENESS_MS) {
        if (engine->callbacks.on_liveness_timeout) {
            engine->callbacks.on_liveness_timeout(engine->callbacks.user_ctx);
        }
    }
}

static void engine_tick_transactions(crp_engine_t *engine, uint32_t now_ms)
{
    for (uint16_t i = 1; i < CRP_MAX_TXNS; i++) {
        crp_txn_t *txn = &engine->txns[i];
        if (txn->state != CRP_TXN_PENDING) continue;

        if (now_ms - txn->sent_ms >= CRP_TIMEOUT_RESPONSE_MS) {
            /* Timeout — free the TxnID and notify.
             * NOTE: The engine does not store original packets, so retry
             * re-send is the client's responsibility. */
            uint32_t reg = txn->reg_addr;
            crp_engine_free_txn(engine, (uint8_t)i);
            if (engine->callbacks.on_txn_timeout) {
                engine->callbacks.on_txn_timeout(
                    engine->callbacks.user_ctx, (uint8_t)i, reg);
            }
        }
    }
}

/* Gather subscription data into a buffer. Returns bytes written. */
static uint16_t channel_read_subscription(crp_engine_t *engine,
                                           crp_channel_t *ch,
                                           uint8_t *buf, uint32_t buf_size)
{
    (void)engine;  /* Region pointers cached on channel — no lookup needed */
    uint16_t plen = 0;
    for (uint8_t r = 0; r < ch->sub.reg_count; r++) {
        crp_region_t *region = ch->sub.reg_regions[r];
        if (!region || !region->ops->subscribe_read) continue;

        uint32_t rlen = buf_size - plen;
        crp_status_t st = region->ops->subscribe_read(
            region->ctx, ch->sub.regs[r], &buf[plen], &rlen);
        if (st == CRP_STATUS_OK) {
            plen += (uint16_t)rlen;
        }
    }
    return plen;
}

/* Send a BULK_DATA packet on a channel and advance its offset. */
static void channel_send_data(crp_engine_t *engine, crp_channel_t *ch,
                                const uint8_t *data, uint16_t len,
                                uint32_t now_ms)
{
    crp_packet_t data_pkt = {0};
    /* LOCAL PATCH (coil, 2026-07-16): subscription/push samples go out with wire
     * offset 0 — matches the coil-webdriver client + sim (the SDK records chunks
     * per channel, so a monotonic counter grows client memory unboundedly on a
     * long-lived subscription and buys nothing for periodic telemetry). Bulk
     * channels keep real offsets (reassembly + SEEK gap recovery need them). */
    crp_chan_base_type_t base_t = crp_chantype_base(ch->chan_type);
    bool periodic = (base_t == CRP_CHAN_SUBSCRIPTION || base_t == CRP_CHAN_PUSH);
    data_pkt.type = CRP_PKT_BULK_DATA;
    data_pkt.pkt.bulk_data.channel     = ch->channel_num;
    data_pkt.pkt.bulk_data.offset      = periodic ? 0 : ch->offset;
    data_pkt.pkt.bulk_data.payload     = data;
    data_pkt.pkt.bulk_data.payload_len = len;
    engine_send_packet(engine, &data_pkt);

    ch->offset += len;
    ch->last_activity_ms = now_ms;
}

static void engine_tick_channels(crp_engine_t *engine, uint32_t now_ms)
{
    for (uint8_t i = 0; i < engine->channels.max_channels; i++) {
        crp_channel_t *ch = &engine->channels.channels[i];
        if (ch->state != CRP_CH_ACTIVE || ch->paused) continue;

        crp_chan_base_type_t base = crp_chantype_base(ch->chan_type);
        /* LOCAL PATCH (pod, D45): static, not on the stack. The Arduino loop task has a 4 KB
         * stack and both of the pod's engines run on it one after the other. */
        static uint8_t buf[1024];

        if (base == CRP_CHAN_SUBSCRIPTION || base == CRP_CHAN_PUSH) {
            /* Subscription / push — pump data at configured interval */
            uint32_t interval_ms = ch->sub.interval_us / 1000;
            if (interval_ms == 0) interval_ms = 1;
            if (now_ms - ch->sub.last_tick_ms < interval_ms) continue;

            ch->sub.last_tick_ms = now_ms;
            uint16_t plen = channel_read_subscription(engine, ch, buf, sizeof(buf));
            if (plen > 0) {
                channel_send_data(engine, ch, buf, plen, now_ms);
            }

        } else if (base == CRP_CHAN_BULK_READ && ch->region) {
            /* Bulk read — pump next chunk */
            if (!ch->region->ops->bulk_read_next) continue;

            uint32_t chunk_len = engine->session_max_chunk;
            /* Use transport MTU if available and smaller */
            if (engine->transport->mtu) {
                uint32_t mtu = engine->transport->mtu(engine->transport->ctx);
                if (mtu > 6 && mtu - 6 < chunk_len) chunk_len = mtu - 6; /* minus BULK_DATA header */
            }
            if (chunk_len > sizeof(buf)) chunk_len = sizeof(buf);

            crp_status_t st = ch->region->ops->bulk_read_next(
                ch->region->ctx, ch->reg_addr, ch->offset,
                buf, &chunk_len);

            if (st == CRP_STATUS_OK && chunk_len > 0) {
                channel_send_data(engine, ch, buf, (uint16_t)chunk_len, now_ms);

                if (ch->bulk.total_size != CRP_SIZE_STREAMING &&
                    ch->offset >= ch->bulk.total_size) {
                    crp_engine_end_channel(engine, ch->channel_num);
                }
            } else if (chunk_len == 0) {
                crp_engine_end_channel(engine, ch->channel_num);
            }
        }
    }
}

void crp_engine_tick(crp_engine_t *engine, uint32_t now_ms)
{
    engine->now_ms = now_ms;

    if (!engine->session_active) return;

    engine_tick_heartbeat(engine, now_ms);
    engine_tick_transactions(engine, now_ms);
    engine_tick_channels(engine, now_ms);
    crp_region_tick_all(engine->regions, now_ms);

    /* Flush transport after burst of sends from tick */
    if (engine->transport->flush) {
        engine->transport->flush(engine->transport->ctx);
    }
}

/*============================================================
 * Outbound packet senders
 *============================================================*/

void crp_engine_send_hello(crp_engine_t *engine)
{
    crp_packet_t pkt = {0};
    pkt.type = CRP_PKT_HELLO;
    pkt.pkt.hello.proto_ver       = CRP_PROTO_VERSION;
    pkt.pkt.hello.max_channels    = engine->config.max_channels;
    pkt.pkt.hello.max_chunk       = engine->config.max_chunk;
    pkt.pkt.hello.flags           = engine->config.flags;
    pkt.pkt.hello.device_id       = engine->config.device_id;
    engine_send_packet(engine, &pkt);
}

void crp_engine_send_bye(crp_engine_t *engine)
{
    crp_packet_t pkt = {0};
    pkt.type = CRP_PKT_BYE;
    pkt.pkt.bye.base_addr = 0;
    pkt.pkt.bye.device_id = engine->config.device_id;
    engine_send_packet(engine, &pkt);
}

uint8_t crp_engine_read(crp_engine_t *engine, uint32_t reg)
{
    uint8_t txn = crp_engine_alloc_txn(engine, reg);
    if (txn == 0) return 0;

    crp_packet_t pkt = {0};
    pkt.type = CRP_PKT_READ;
    pkt.pkt.read.txn_id = txn;
    pkt.pkt.read.reg    = reg;

    return engine_send_txn_packet(engine, &pkt, txn);
}

uint8_t crp_engine_read_bulk(crp_engine_t *engine, uint32_t reg,
                              uint32_t offset, uint32_t length)
{
    /* Bulk read is just a normal READ — the device decides whether to
     * inline the response or open a streaming channel based on register size.
     * offset/length are ignored (kept in API for source compatibility). */
    (void)offset;
    (void)length;
    return crp_engine_read(engine, reg);
}

uint8_t crp_engine_write(crp_engine_t *engine, uint32_t reg,
                           const uint8_t *data, uint16_t len)
{
    uint8_t txn = crp_engine_alloc_txn(engine, reg);
    if (txn == 0) return 0;

    crp_packet_t pkt = {0};
    pkt.type = CRP_PKT_WRITE;
    pkt.pkt.write.txn_id      = txn;
    pkt.pkt.write.reg         = reg;
    pkt.pkt.write.payload     = data;
    pkt.pkt.write.payload_len = len;

    return engine_send_txn_packet(engine, &pkt, txn);
}

bool crp_engine_write_noack(crp_engine_t *engine, uint32_t reg,
                              const uint8_t *data, uint16_t len)
{
    /* TxnID=0 means no response expected — unified with WRITE */
    crp_packet_t pkt = {0};
    pkt.type = CRP_PKT_WRITE;
    pkt.pkt.write.txn_id      = 0;
    pkt.pkt.write.reg         = reg;
    pkt.pkt.write.payload     = data;
    pkt.pkt.write.payload_len = len;

    return engine_send_packet(engine, &pkt) == 0;
}

uint8_t crp_engine_subscribe(crp_engine_t *engine,
                               const uint32_t *regs, uint8_t reg_count,
                               uint32_t interval_us)
{
    if (reg_count == 0 || reg_count > CRP_MAX_SUB_REGS) return 0;

    uint8_t txn = crp_engine_alloc_txn(engine, regs[0]);
    if (txn == 0) return 0;

    /* Encode register list into scratch as LE u32s */
    uint8_t reg_buf[CRP_MAX_SUB_REGS * 4];
    for (uint8_t i = 0; i < reg_count; i++) {
        crp_write_u32(&reg_buf[i * 4], regs[i]);
    }

    crp_packet_t pkt = {0};
    pkt.type = CRP_PKT_SUBSCRIBE;
    pkt.pkt.subscribe.txn_id      = txn;
    pkt.pkt.subscribe.interval_us = interval_us;
    pkt.pkt.subscribe.regs        = NULL;
    pkt.pkt.subscribe.regs_raw    = reg_buf;
    pkt.pkt.subscribe.reg_count   = reg_count;

    return engine_send_txn_packet(engine, &pkt, txn);
}

uint8_t crp_engine_query_group(crp_engine_t *engine, uint32_t base)
{
    return engine_send_txn_u32(engine, CRP_PKT_QUERY_GROUP, base);
}

uint8_t crp_engine_query_reg(crp_engine_t *engine, uint32_t reg)
{
    return engine_send_txn_u32(engine, CRP_PKT_QUERY_REG, reg);
}

bool crp_engine_end_channel(crp_engine_t *engine, uint8_t channel)
{
    crp_channel_t *ch = crp_channel_get(&engine->channels, channel);
    if (!ch || ch->state == CRP_CH_IDLE) return false;

    crp_packet_t pkt = {0};
    pkt.type = CRP_PKT_END;
    pkt.pkt.end.channel = channel;

    if (engine_send_packet(engine, &pkt) != 0) return false;

    ch->state = CRP_CH_CLOSING;
    ch->last_activity_ms = engine->now_ms;
    return true;
}

bool crp_engine_end_session(crp_engine_t *engine)
{
    crp_packet_t pkt = {0};
    pkt.type = CRP_PKT_END;
    pkt.pkt.end.channel = CRP_CHANNEL_SESSION;
    return engine_send_packet(engine, &pkt) == 0;
}

bool crp_engine_seek(crp_engine_t *engine, uint8_t channel, uint32_t offset)
{
    crp_packet_t pkt = {0};
    pkt.type = CRP_PKT_SEEK;
    pkt.pkt.seek.channel = channel;
    pkt.pkt.seek.offset  = offset;
    return engine_send_packet(engine, &pkt) == 0;
}

bool crp_engine_pause_channel(crp_engine_t *engine, uint8_t channel)
{
    return crp_engine_seek(engine, channel, CRP_OFFSET_PAUSE);
}

bool crp_engine_resume_channel(crp_engine_t *engine, uint8_t channel)
{
    crp_channel_t *ch = crp_channel_get(&engine->channels, channel);
    if (!ch) return false;

    return crp_engine_seek(engine, channel, ch->offset);
}

void crp_engine_send_nop(crp_engine_t *engine)
{
    crp_packet_t pkt = {0};
    pkt.type = CRP_PKT_NOP;
    engine_send_packet(engine, &pkt);
}

/* LOCAL PATCH (pod, D45): returns the send result (0 = sent, -1 = no session, not ready or
 * refused). The pod's frame pump counts only accepted frames and resends a refused one.
 * Upstream returns void and drops the result. */
int crp_engine_push_change(crp_engine_t *engine, uint32_t reg,
                              const uint8_t *data, uint32_t len)
{
    if (!engine->session_active) return -1;

    /* Send as WRITE with txn=0 (unsolicited, no response expected) */
    crp_packet_t pkt = {0};
    pkt.type = CRP_PKT_WRITE;
    pkt.pkt.write.txn_id      = 0;
    pkt.pkt.write.reg         = reg;
    pkt.pkt.write.payload     = data;
    pkt.pkt.write.payload_len = (uint16_t)len;
    return engine_send_packet(engine, &pkt);
}

void crp_engine_send_log(crp_engine_t *engine, uint8_t level,
                           const char *module, uint8_t module_len,
                           const char *message, uint16_t message_len)
{
    crp_packet_t pkt = {0};
    pkt.type = CRP_PKT_LOG;
    pkt.pkt.log.level        = level;
    pkt.pkt.log.timestamp_ms = engine->now_ms;
    pkt.pkt.log.module       = (const uint8_t *)module;
    pkt.pkt.log.module_len   = module_len;
    pkt.pkt.log.message      = (const uint8_t *)message;
    pkt.pkt.log.message_len  = message_len;
    engine_send_packet(engine, &pkt);
}

uint8_t crp_engine_unlock(crp_engine_t *engine, uint32_t group,
                            const uint8_t proof[32])
{
    uint8_t txn = crp_engine_alloc_txn(engine, group);
    if (txn == 0) return 0;

    crp_packet_t pkt = {0};
    pkt.type = CRP_PKT_UNLOCK;
    pkt.pkt.unlock.txn_id = txn;
    pkt.pkt.unlock.group  = group;
    pkt.pkt.unlock.proof  = proof;

    return engine_send_txn_packet(engine, &pkt, txn);
}

/*============================================================
 * Synchronous convenience wrappers
 *============================================================*/

typedef struct {
    uint8_t  txn_id;
    bool     done;
    uint8_t  status;
    const uint8_t *payload;
    uint16_t payload_len;
} sync_read_ctx_t;

static void sync_read_callback(void *user_ctx, const crp_pkt_response_t *resp)
{
    sync_read_ctx_t *ctx = (sync_read_ctx_t *)user_ctx;
    if (resp->txn_id == ctx->txn_id) {
        ctx->done        = true;
        ctx->status      = resp->status;
        ctx->payload     = resp->payload;
        ctx->payload_len = resp->payload_len;
    }
}

crp_status_t crp_engine_read_sync(crp_engine_t *engine, uint32_t reg,
                                    uint8_t *buf, uint32_t *len,
                                    uint32_t timeout_ms,
                                    crp_get_ms_fn get_ms)
{
    /* Save and replace the response callback */
    void (*old_cb)(void *, const crp_pkt_response_t *) = engine->callbacks.on_response;
    void *old_ctx = engine->callbacks.user_ctx;

    sync_read_ctx_t sync = {0};

    uint8_t txn = crp_engine_read(engine, reg);
    if (txn == 0) {
        return CRP_STATUS_BUSY;
    }

    sync.txn_id = txn;
    engine->callbacks.on_response = sync_read_callback;
    engine->callbacks.user_ctx    = &sync;

    uint32_t start = get_ms();
    while (!sync.done) {
        uint32_t now = get_ms();
        if (now - start >= timeout_ms) break;

        crp_engine_process(engine);
        crp_engine_tick(engine, now);
    }

    /* Restore original callback */
    engine->callbacks.on_response = old_cb;
    engine->callbacks.user_ctx    = old_ctx;

    if (!sync.done) {
        crp_engine_free_txn(engine, txn);
        return CRP_STATUS_BUSY;  /* Timeout */
    }

    if (sync.status == CRP_STATUS_OK && sync.payload && sync.payload_len > 0) {
        uint32_t copy = sync.payload_len;
        if (copy > *len) copy = *len;
        memcpy(buf, sync.payload, copy);
        *len = copy;
    } else {
        *len = 0;
    }

    return (crp_status_t)sync.status;
}
