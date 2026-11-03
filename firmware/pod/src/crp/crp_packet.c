/*
 * CRP — Channel Register Protocol
 * Packet parser and serializer.
 *
 * All multi-byte fields are little-endian on the wire.
 * Payload pointers in parsed structs reference the original buffer (zero-copy).
 */

#include "crp/crp_packet.h"
#include <string.h>

/*============================================================
 * Minimum size table
 *============================================================*/

uint32_t crp_packet_min_size(crp_packet_type_t type)
{
    switch (type) {
    case CRP_PKT_NOP:          return CRP_MIN_NOP;
    case CRP_PKT_HELLO:        return CRP_MIN_HELLO;
    case CRP_PKT_BYE:          return CRP_MIN_BYE;
    case CRP_PKT_WRITE:        return CRP_MIN_WRITE;
    case CRP_PKT_READ:         return CRP_MIN_READ;
    case CRP_PKT_SUBSCRIBE:    return CRP_MIN_SUBSCRIBE;
    case CRP_PKT_PUSH_SETUP:   return CRP_MIN_SUBSCRIBE;
    case CRP_PKT_QUERY_REG:    return CRP_MIN_QUERY_REG;
    case CRP_PKT_QUERY_GROUP:  return CRP_MIN_QUERY_GROUP;
    case CRP_PKT_RESPONSE:     return CRP_MIN_RESPONSE;
    case CRP_PKT_CHANNEL_OPEN: return CRP_MIN_CHANNEL_OPEN;
    case CRP_PKT_BULK_DATA:    return CRP_MIN_BULK_DATA;
    case CRP_PKT_SEEK:         return CRP_MIN_SEEK;
    case CRP_PKT_END:          return CRP_MIN_END;
    case CRP_PKT_LOG:          return CRP_MIN_LOG;
    case CRP_PKT_QUERY_DEVICE: return CRP_MIN_QUERY_DEVICE;
    case CRP_PKT_UNLOCK:       return CRP_MIN_UNLOCK;
    default:                   return 0;
    }
}

/*============================================================
 * Parser — individual packet types
 *============================================================*/

static crp_parse_result_t parse_hello(const uint8_t *buf, uint32_t len,
                                       crp_pkt_hello_t *out)
{
    if (len < CRP_MIN_HELLO) return CRP_PARSE_TOO_SHORT;

    out->proto_ver    = buf[1];
    out->max_channels = buf[2];
    out->max_chunk    = crp_read_u16(&buf[3]);
    out->flags        = buf[5];
    out->device_id    = crp_read_u32(&buf[6]);

    return CRP_PARSE_OK;
}

static crp_parse_result_t parse_bye(const uint8_t *buf, uint32_t len,
                                     crp_pkt_bye_t *out)
{
    if (len < CRP_MIN_BYE) return CRP_PARSE_TOO_SHORT;

    out->base_addr = crp_read_u32(&buf[1]);
    out->device_id = crp_read_u32(&buf[5]);
    return CRP_PARSE_OK;
}

/* WRITE packet parser (txn_id=0 means fire-and-forget) */
static crp_parse_result_t parse_write(const uint8_t *buf, uint32_t len,
                                       crp_pkt_write_t *out)
{
    if (len < CRP_MIN_WRITE) return CRP_PARSE_TOO_SHORT;

    out->txn_id      = buf[1];
    out->reg         = crp_read_u32(&buf[2]);
    out->payload     = (len > 6) ? &buf[6] : NULL;
    out->payload_len = (uint16_t)(len - 6);
    return CRP_PARSE_OK;
}

/* READ parser. Wire format mirrors WRITE: 1 byte type, 1 byte txn_id,
 * 4 bytes reg, and an OPTIONAL payload (typically 4 bytes total_size to
 * size a variable-length bulk read). When the payload is absent, the
 * device picks total_size from its own region state.
 *
 * LOCAL (2026-05-19): added optional length payload to plug the v0.0B
 * bulk-read sizing gap. Was: parse_txn_u32 (txn + reg only). */
static crp_parse_result_t parse_read(const uint8_t *buf, uint32_t len,
                                       crp_pkt_read_t *out)
{
    if (len < CRP_MIN_READ) return CRP_PARSE_TOO_SHORT;

    out->txn_id      = buf[1];
    out->reg         = crp_read_u32(&buf[2]);
    out->payload     = (len > 6) ? &buf[6] : NULL;
    out->payload_len = (uint16_t)(len - 6);
    return CRP_PARSE_OK;
}

static crp_parse_result_t parse_response(const uint8_t *buf, uint32_t len,
                                          crp_pkt_response_t *out)
{
    if (len < CRP_MIN_RESPONSE) return CRP_PARSE_TOO_SHORT;

    out->txn_id      = buf[1];
    out->status      = buf[2];
    out->channel     = buf[3];
    out->reg         = crp_read_u32(&buf[4]);
    out->payload     = (len > 8) ? &buf[8] : NULL;
    out->payload_len = (uint16_t)(len - 8);
    return CRP_PARSE_OK;
}

/* Shared parser for SUBSCRIBE / PUSH_SETUP (LSB pair 0x08/0x09) */
static crp_parse_result_t parse_subscribe(const uint8_t *buf, uint32_t len,
                                           crp_pkt_subscribe_t *out)
{
    if (len < CRP_MIN_SUBSCRIBE) return CRP_PARSE_TOO_SHORT;

    out->txn_id      = buf[1];
    out->interval_us = crp_read_u32(&buf[2]);
    out->regs        = NULL;
    out->regs_raw    = &buf[6];
    out->reg_count   = (uint8_t)((len - 6) / 4);
    return CRP_PARSE_OK;
}

static crp_parse_result_t parse_channel_open(const uint8_t *buf, uint32_t len,
                                              crp_pkt_channel_open_t *out)
{
    if (len < CRP_MIN_CHANNEL_OPEN) return CRP_PARSE_TOO_SHORT;

    out->txn_id    = buf[1];
    out->channel   = buf[2];
    out->chan_type  = buf[3];
    out->reg       = crp_read_u32(&buf[4]);
    out->total_size = crp_read_u32(&buf[8]);
    memcpy(out->nonce, &buf[12], 12);

    return CRP_PARSE_OK;
}

static crp_parse_result_t parse_bulk_data(const uint8_t *buf, uint32_t len,
                                           crp_pkt_bulk_data_t *out)
{
    if (len < CRP_MIN_BULK_DATA) return CRP_PARSE_TOO_SHORT;

    out->channel     = buf[1];
    out->offset      = crp_read_u32(&buf[2]);
    out->payload     = (len > 6) ? &buf[6] : NULL;
    out->payload_len = (uint16_t)(len - 6);
    return CRP_PARSE_OK;
}

static crp_parse_result_t parse_seek(const uint8_t *buf, uint32_t len,
                                      crp_pkt_seek_t *out)
{
    if (len < CRP_MIN_SEEK) return CRP_PARSE_TOO_SHORT;

    out->channel = buf[1];
    out->offset  = crp_read_u32(&buf[2]);
    return CRP_PARSE_OK;
}

/* Shared parser for txn+u32 packets: QUERY_REG, QUERY_GROUP, QUERY_DEVICE */
static crp_parse_result_t parse_txn_u32(const uint8_t *buf, uint32_t len,
                                         uint8_t *out_txn, uint32_t *out_val)
{
    if (len < 6) return CRP_PARSE_TOO_SHORT;
    *out_txn = buf[1];
    *out_val = crp_read_u32(&buf[2]);
    return CRP_PARSE_OK;
}

static crp_parse_result_t parse_log(const uint8_t *buf, uint32_t len,
                                     crp_pkt_log_t *out)
{
    if (len < CRP_MIN_LOG) return CRP_PARSE_TOO_SHORT;

    out->level        = buf[1];
    out->timestamp_ms = crp_read_u32(&buf[2]);
    out->module_len   = buf[6];

    if (len < (uint32_t)(7 + out->module_len)) return CRP_PARSE_TOO_SHORT;

    out->module      = (out->module_len > 0) ? &buf[7] : NULL;
    out->message     = &buf[7 + out->module_len];
    out->message_len = (uint16_t)(len - 7 - out->module_len);

    return CRP_PARSE_OK;
}

static crp_parse_result_t parse_unlock(const uint8_t *buf, uint32_t len,
                                        crp_pkt_unlock_t *out)
{
    if (len < CRP_MIN_UNLOCK) return CRP_PARSE_TOO_SHORT;

    out->txn_id = buf[1];
    out->group  = crp_read_u32(&buf[2]);
    out->proof  = &buf[6];
    return CRP_PARSE_OK;
}

static crp_parse_result_t parse_end(const uint8_t *buf, uint32_t len,
                                     crp_pkt_end_t *out)
{
    if (len < CRP_MIN_END) return CRP_PARSE_TOO_SHORT;

    out->channel = buf[1];
    return CRP_PARSE_OK;
}

/*============================================================
 * Main parser
 *============================================================*/

crp_parse_result_t crp_packet_parse(const uint8_t *buf, uint32_t len,
                                     crp_packet_t *out)
{
    if (len < 1) return CRP_PARSE_TOO_SHORT;

    out->type = (crp_packet_type_t)buf[0];

    switch (out->type) {
    case CRP_PKT_NOP:
        return CRP_PARSE_OK;

    case CRP_PKT_HELLO:
        return parse_hello(buf, len, &out->pkt.hello);

    case CRP_PKT_BYE:
        return parse_bye(buf, len, &out->pkt.bye);

    case CRP_PKT_WRITE:
        return parse_write(buf, len, &out->pkt.write);

    /* READ pair (LSB=0: simple, LSB=1: bulk) — different wire formats */
    case CRP_PKT_READ:
        return parse_read(buf, len, &out->pkt.read);
    case CRP_PKT_RESPONSE:
        return parse_response(buf, len, &out->pkt.response);

    /* SUBSCRIBE pair (LSB=0: sub, LSB=1: push) — same wire format */
    case CRP_PKT_SUBSCRIBE:
        return parse_subscribe(buf, len, &out->pkt.subscribe);
    case CRP_PKT_PUSH_SETUP:
        return parse_subscribe(buf, len, &out->pkt.push_setup);

    /* QUERY pair (LSB=0: reg, LSB=1: group) — same wire format */
    case CRP_PKT_QUERY_REG:
        return parse_txn_u32(buf, len, &out->pkt.query_reg.txn_id,
                             &out->pkt.query_reg.reg);
    case CRP_PKT_QUERY_GROUP:
        return parse_txn_u32(buf, len, &out->pkt.query_group.txn_id,
                             &out->pkt.query_group.base);

    case CRP_PKT_CHANNEL_OPEN:
        return parse_channel_open(buf, len, &out->pkt.channel_open);

    case CRP_PKT_BULK_DATA:
        return parse_bulk_data(buf, len, &out->pkt.bulk_data);

    case CRP_PKT_SEEK:
        return parse_seek(buf, len, &out->pkt.seek);

    case CRP_PKT_END:
        return parse_end(buf, len, &out->pkt.end);

    case CRP_PKT_LOG:
        return parse_log(buf, len, &out->pkt.log);

    case CRP_PKT_QUERY_DEVICE:
        return parse_txn_u32(buf, len, &out->pkt.query_device.txn_id,
                             &out->pkt.query_device.base);

    case CRP_PKT_UNLOCK:
        return parse_unlock(buf, len, &out->pkt.unlock);

    default:
        return CRP_PARSE_UNKNOWN_TYPE;
    }
}

/*============================================================
 * Serializer — individual packet types
 *============================================================*/

static uint32_t serialize_hello(const crp_pkt_hello_t *pkt, uint8_t *buf,
                                 uint32_t buf_size)
{
    if (buf_size < 10) return 0;

    buf[0] = CRP_PKT_HELLO;
    buf[1] = pkt->proto_ver;
    buf[2] = pkt->max_channels;
    crp_write_u16(&buf[3], pkt->max_chunk);
    buf[5] = pkt->flags;
    crp_write_u32(&buf[6], pkt->device_id);

    return 10;
}

static uint32_t serialize_bye(const crp_pkt_bye_t *pkt, uint8_t *buf,
                               uint32_t buf_size)
{
    if (buf_size < 9) return 0;

    buf[0] = CRP_PKT_BYE;
    crp_write_u32(&buf[1], pkt->base_addr);
    crp_write_u32(&buf[5], pkt->device_id);
    return 9;
}

/* Shared serializer for WRITE pair */
static uint32_t serialize_write(uint8_t type_byte, const crp_pkt_write_t *pkt,
                                 uint8_t *buf, uint32_t buf_size)
{
    uint32_t needed = 6 + pkt->payload_len;
    if (buf_size < needed) return 0;

    buf[0] = type_byte;
    buf[1] = pkt->txn_id;
    crp_write_u32(&buf[2], pkt->reg);

    if (pkt->payload_len > 0 && pkt->payload) {
        memcpy(&buf[6], pkt->payload, pkt->payload_len);
    }
    return needed;
}

/* READ serializer — wire format mirrors WRITE (1 type, 1 txn, 4 reg, N
 * optional payload). LOCAL (2026-05-19): payload carries 4-byte total_size
 * for bulk reads; see parse_read + crp_pkt_read_t. */
static uint32_t serialize_read(const crp_pkt_read_t *pkt,
                                uint8_t *buf, uint32_t buf_size)
{
    uint32_t needed = 6 + pkt->payload_len;
    if (buf_size < needed) return 0;

    buf[0] = CRP_PKT_READ;
    buf[1] = pkt->txn_id;
    crp_write_u32(&buf[2], pkt->reg);

    if (pkt->payload_len > 0 && pkt->payload) {
        memcpy(&buf[6], pkt->payload, pkt->payload_len);
    }
    return needed;
}

static uint32_t serialize_response(const crp_pkt_response_t *pkt, uint8_t *buf,
                                    uint32_t buf_size)
{
    uint32_t needed = 8 + pkt->payload_len;
    if (buf_size < needed) return 0;

    buf[0] = CRP_PKT_RESPONSE;
    buf[1] = pkt->txn_id;
    buf[2] = pkt->status;
    buf[3] = pkt->channel;
    crp_write_u32(&buf[4], pkt->reg);

    if (pkt->payload_len > 0 && pkt->payload) {
        memcpy(&buf[8], pkt->payload, pkt->payload_len);
    }
    return needed;
}

/* Shared serializer for SUBSCRIBE pair */
static uint32_t serialize_subscribe(uint8_t type_byte,
                                     const crp_pkt_subscribe_t *pkt,
                                     uint8_t *buf, uint32_t buf_size)
{
    uint32_t needed = 6 + (uint32_t)pkt->reg_count * 4;
    if (buf_size < needed) return 0;

    buf[0] = type_byte;
    buf[1] = pkt->txn_id;
    crp_write_u32(&buf[2], pkt->interval_us);

    if (pkt->reg_count > 0 && pkt->regs_raw) {
        memcpy(&buf[6], pkt->regs_raw, (uint32_t)pkt->reg_count * 4);
    }
    return needed;
}

static uint32_t serialize_channel_open(const crp_pkt_channel_open_t *pkt,
                                        uint8_t *buf, uint32_t buf_size)
{
    if (buf_size < 24) return 0;

    buf[0] = CRP_PKT_CHANNEL_OPEN;
    buf[1] = pkt->txn_id;
    buf[2] = pkt->channel;
    buf[3] = pkt->chan_type;
    crp_write_u32(&buf[4], pkt->reg);
    crp_write_u32(&buf[8], pkt->total_size);
    memcpy(&buf[12], pkt->nonce, 12);
    return 24;
}

static uint32_t serialize_bulk_data(const crp_pkt_bulk_data_t *pkt,
                                     uint8_t *buf, uint32_t buf_size)
{
    uint32_t needed = 6 + pkt->payload_len;
    if (buf_size < needed) return 0;

    buf[0] = CRP_PKT_BULK_DATA;
    buf[1] = pkt->channel;
    crp_write_u32(&buf[2], pkt->offset);

    if (pkt->payload_len > 0 && pkt->payload) {
        memcpy(&buf[6], pkt->payload, pkt->payload_len);
    }
    return needed;
}

static uint32_t serialize_seek(const crp_pkt_seek_t *pkt, uint8_t *buf,
                                uint32_t buf_size)
{
    if (buf_size < 6) return 0;

    buf[0] = CRP_PKT_SEEK;
    buf[1] = pkt->channel;
    crp_write_u32(&buf[2], pkt->offset);
    return 6;
}

/* Shared serializer for txn+u32 packets: QUERY_REG, QUERY_GROUP, QUERY_DEVICE */
static uint32_t serialize_txn_u32(uint8_t type_byte, uint8_t txn_id,
                                   uint32_t val, uint8_t *buf,
                                   uint32_t buf_size)
{
    if (buf_size < 6) return 0;
    buf[0] = type_byte;
    buf[1] = txn_id;
    crp_write_u32(&buf[2], val);
    return 6;
}

static uint32_t serialize_log(const crp_pkt_log_t *pkt, uint8_t *buf,
                               uint32_t buf_size)
{
    uint32_t total = 7 + pkt->module_len + pkt->message_len;
    if (buf_size < total) return 0;

    buf[0] = CRP_PKT_LOG;
    buf[1] = pkt->level;
    crp_write_u32(&buf[2], pkt->timestamp_ms);
    buf[6] = pkt->module_len;
    if (pkt->module_len > 0 && pkt->module) {
        memcpy(&buf[7], pkt->module, pkt->module_len);
    }
    if (pkt->message_len > 0 && pkt->message) {
        memcpy(&buf[7 + pkt->module_len], pkt->message, pkt->message_len);
    }
    return total;
}

static uint32_t serialize_unlock(const crp_pkt_unlock_t *pkt, uint8_t *buf,
                                  uint32_t buf_size)
{
    if (buf_size < 38) return 0;

    buf[0] = CRP_PKT_UNLOCK;
    buf[1] = pkt->txn_id;
    crp_write_u32(&buf[2], pkt->group);
    if (pkt->proof) {
        memcpy(&buf[6], pkt->proof, 32);
    }
    return 38;
}

static uint32_t serialize_end(const crp_pkt_end_t *pkt, uint8_t *buf,
                               uint32_t buf_size)
{
    if (buf_size < 2) return 0;

    buf[0] = CRP_PKT_END;
    buf[1] = pkt->channel;
    return 2;
}

/*============================================================
 * Main serializer
 *============================================================*/

uint32_t crp_packet_serialize(const crp_packet_t *pkt, uint8_t *buf,
                               uint32_t buf_size)
{
    switch (pkt->type) {
    case CRP_PKT_NOP:
        if (buf_size < 1) return 0;
        buf[0] = CRP_PKT_NOP;
        return 1;

    case CRP_PKT_HELLO:
        return serialize_hello(&pkt->pkt.hello, buf, buf_size);

    case CRP_PKT_BYE:
        return serialize_bye(&pkt->pkt.bye, buf, buf_size);

    /* WRITE pair */
    case CRP_PKT_WRITE:
        return serialize_write(CRP_PKT_WRITE, &pkt->pkt.write, buf, buf_size);
    /* CRP_PKT_WRITE_NOACK removed — WRITE with txn=0 replaces it */

    case CRP_PKT_READ:
        return serialize_read(&pkt->pkt.read, buf, buf_size);

    case CRP_PKT_RESPONSE:
        return serialize_response(&pkt->pkt.response, buf, buf_size);

    /* SUBSCRIBE pair */
    case CRP_PKT_SUBSCRIBE:
        return serialize_subscribe(CRP_PKT_SUBSCRIBE, &pkt->pkt.subscribe,
                                    buf, buf_size);
    case CRP_PKT_PUSH_SETUP:
        return serialize_subscribe(CRP_PKT_PUSH_SETUP, &pkt->pkt.push_setup,
                                    buf, buf_size);

    /* QUERY pair + QUERY_DEVICE */
    case CRP_PKT_QUERY_REG:
        return serialize_txn_u32(CRP_PKT_QUERY_REG,
                                 pkt->pkt.query_reg.txn_id,
                                 pkt->pkt.query_reg.reg, buf, buf_size);
    case CRP_PKT_QUERY_GROUP:
        return serialize_txn_u32(CRP_PKT_QUERY_GROUP,
                                 pkt->pkt.query_group.txn_id,
                                 pkt->pkt.query_group.base, buf, buf_size);
    case CRP_PKT_QUERY_DEVICE:
        return serialize_txn_u32(CRP_PKT_QUERY_DEVICE,
                                 pkt->pkt.query_device.txn_id,
                                 pkt->pkt.query_device.base, buf, buf_size);

    case CRP_PKT_CHANNEL_OPEN:
        return serialize_channel_open(&pkt->pkt.channel_open, buf, buf_size);

    case CRP_PKT_BULK_DATA:
        return serialize_bulk_data(&pkt->pkt.bulk_data, buf, buf_size);

    case CRP_PKT_SEEK:
        return serialize_seek(&pkt->pkt.seek, buf, buf_size);

    case CRP_PKT_END:
        return serialize_end(&pkt->pkt.end, buf, buf_size);

    case CRP_PKT_LOG:
        return serialize_log(&pkt->pkt.log, buf, buf_size);

    case CRP_PKT_UNLOCK:
        return serialize_unlock(&pkt->pkt.unlock, buf, buf_size);

    default:
        return 0;
    }
}
