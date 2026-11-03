/*
 * CRP — Channel Register Protocol
 * Packet structures and parse/serialize API.
 *
 * Parsed packets are represented as tagged unions. The parser reads raw bytes
 * into these structs; the serializer writes them back to bytes. Payloads are
 * not copied — the parsed struct holds a pointer into the original buffer.
 */

#ifndef CRP_PACKET_H
#define CRP_PACKET_H

#include "crp_types.h"

/*============================================================
 * Parsed packet structures (one per packet type)
 *============================================================*/

typedef struct {
    uint8_t  proto_ver;
    uint8_t  max_channels;
    uint16_t max_chunk;
    uint8_t  flags;
    uint32_t device_id;
} crp_pkt_hello_t;

typedef struct {
    uint32_t base_addr;
    uint32_t device_id;
} crp_pkt_bye_t;

typedef struct {
    uint8_t  txn_id;
    uint32_t reg;
    const uint8_t *payload;        /* Points into source buffer */
    uint16_t payload_len;
} crp_pkt_write_t;

/* No separate WRITE_NOACK — use WRITE with txn_id=0 for fire-and-forget. */

typedef struct {
    uint8_t  txn_id;
    uint32_t reg;
    /* LOCAL (2026-05-19): optional 4-byte length payload for bulk-read.
     * Host sends READ with a 4-byte total-size suffix to tell the device
     * how many bytes to drain from a variable-size BLOB endpoint. When
     * the payload is absent (payload_len==0), the device picks total_size
     * from its own region state (legacy / fixed-size behavior). Mirrors
     * the bulk_write local-mod, where WRITE carries a 4-byte total_size. */
    const uint8_t *payload;
    uint16_t payload_len;
} crp_pkt_read_t;

typedef struct {
    uint8_t  txn_id;
    uint8_t  status;               /* 0x00=OK, 0x01-0x09=error codes */
    uint8_t  channel;              /* 0xFF if not channel-specific */
    uint32_t reg;
    const uint8_t *payload;
    uint16_t payload_len;
} crp_pkt_response_t;

typedef struct {
    uint8_t  txn_id;
    uint32_t interval_us;
    const uint32_t *regs;          /* Points into source buffer (LE encoded) */
    uint8_t  reg_count;            /* Computed: (pkt_len - 6) / 4 */
    /* Note: regs pointer is to raw bytes; use crp_read_u32 to read each */
    const uint8_t *regs_raw;       /* Raw pointer for reading LE u32s */
} crp_pkt_subscribe_t;

/* PUSH_SETUP has same layout as SUBSCRIBE */
typedef crp_pkt_subscribe_t crp_pkt_push_setup_t;

typedef struct {
    uint8_t  txn_id;
    uint8_t  channel;
    uint8_t  chan_type;
    uint32_t reg;
    uint32_t total_size;           /* 0 = unknown/streaming */
    uint8_t  nonce[12];            /* All zeros if unencrypted */
} crp_pkt_channel_open_t;

typedef struct {
    uint8_t  channel;
    uint32_t offset;
    const uint8_t *payload;
    uint16_t payload_len;
} crp_pkt_bulk_data_t;

typedef struct {
    uint8_t  channel;
    uint32_t offset;
} crp_pkt_seek_t;

typedef struct {
    uint8_t  txn_id;
    uint32_t base;
} crp_pkt_query_group_t;

typedef struct {
    uint8_t  txn_id;
    uint32_t base;
} crp_pkt_query_device_t;

typedef struct {
    uint8_t  txn_id;
    uint32_t reg;
} crp_pkt_query_reg_t;

/* ERROR is merged into RESPONSE — no separate struct needed */

typedef struct {
    uint8_t  channel;
} crp_pkt_end_t;

typedef struct {
    uint8_t        level;           /* CRP_LOG_ERROR .. CRP_LOG_DEBUG */
    uint32_t       timestamp_ms;    /* Monotonic milliseconds */
    const uint8_t *module;          /* Points into source buffer, not null-terminated */
    uint8_t        module_len;
    const uint8_t *message;         /* Points into source buffer, not null-terminated */
    uint16_t       message_len;
} crp_pkt_log_t;

typedef struct {
    uint8_t  txn_id;
    uint32_t group;
    const uint8_t *proof;       /* 32 bytes HMAC-SHA256 proof */
} crp_pkt_unlock_t;

/* NOP has no fields */

/*============================================================
 * Tagged union for any parsed packet
 *============================================================*/

typedef struct {
    crp_packet_type_t type;
    union {
        crp_pkt_hello_t        hello;
        crp_pkt_bye_t          bye;
        crp_pkt_write_t        write;
        crp_pkt_read_t         read;
        crp_pkt_response_t     response;
        crp_pkt_subscribe_t    subscribe;
        crp_pkt_push_setup_t   push_setup;
        crp_pkt_channel_open_t channel_open;
        crp_pkt_bulk_data_t    bulk_data;
        crp_pkt_seek_t         seek;
        crp_pkt_query_group_t  query_group;
        crp_pkt_query_device_t query_device;
        crp_pkt_query_reg_t    query_reg;
        /* ERROR merged into response — use response.status > 0 */
        crp_pkt_log_t          log;
        crp_pkt_end_t          end;
        crp_pkt_unlock_t       unlock;
        /* NOP has no data */
    } pkt;
} crp_packet_t;

/*============================================================
 * Parse result
 *============================================================*/

typedef enum {
    CRP_PARSE_OK = 0,
    CRP_PARSE_TOO_SHORT,      /* Packet shorter than minimum for its type */
    CRP_PARSE_UNKNOWN_TYPE,    /* Unrecognized type byte */
} crp_parse_result_t;

/*============================================================
 * API
 *============================================================*/

/**
 * Parse a raw packet buffer into a crp_packet_t.
 *
 * The parsed struct may contain pointers into `buf` (payloads, register lists,
 * device name). The caller must keep `buf` valid while using the parsed packet.
 *
 * Returns CRP_PARSE_OK on success. On CRP_PARSE_UNKNOWN_TYPE, the type field
 * is set but the union is not populated (caller should silently discard per spec).
 */
crp_parse_result_t crp_packet_parse(const uint8_t *buf, uint32_t len,
                                     crp_packet_t *out);

/**
 * Serialize a crp_packet_t into a byte buffer.
 *
 * Writes the packet to `buf` (max `buf_size` bytes). Returns the number of
 * bytes written, or 0 on error (buffer too small).
 *
 * For packets with payloads (WRITE, RESPONSE, BULK_DATA, etc.), the payload
 * pointer in the struct must be valid and payload_len set.
 */
uint32_t crp_packet_serialize(const crp_packet_t *pkt, uint8_t *buf,
                               uint32_t buf_size);

/**
 * Get the minimum valid size for a packet type, or 0 if unknown.
 */
uint32_t crp_packet_min_size(crp_packet_type_t type);

#endif /* CRP_PACKET_H */
