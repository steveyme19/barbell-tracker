/*
 * CRP — Channel Register Protocol
 * Core type definitions, constants, and enumerations.
 *
 * Protocol version: 0x0B
 * All multi-byte values are little-endian on the wire.
 *
 * Acknowledgment convention:
 *   txn_id > 0 → sender expects a RESPONSE
 *   txn_id = 0 → fire-and-forget (no RESPONSE)
 *   Each side independently tracks its own pending TxnIDs.
 */

#ifndef CRP_TYPES_H
#define CRP_TYPES_H

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

/*============================================================
 * Protocol constants
 *============================================================*/

#define CRP_PROTO_VERSION       0x0B
#define CRP_PROTO_MIN_VERSION   0x0B    /* Minimum peer version we accept */

#define CRP_MAX_CHANNELS        16
#define CRP_MAX_TXNS            16      /* Pending transaction pool size */
#define CRP_MAX_REGIONS         32
#define CRP_CHANNEL_SESSION     0xFF    /* END 0xFF = end entire session */

#define CRP_OFFSET_PAUSE        0xFFFFFFFF
#define CRP_SIZE_STREAMING      0xFFFFFFFF

/* Timeouts (milliseconds) */
#define CRP_TIMEOUT_RESPONSE_MS 1000
#define CRP_TIMEOUT_LIVENESS_MS 3000
#define CRP_HEARTBEAT_MS        1000
#define CRP_MAX_RETRIES         3

/* Maximum subscription registers per channel */
#define CRP_MAX_SUB_REGS        16

/*============================================================
 * Packet type IDs
 *
 * No LSB pairing convention. Each type is independent.
 * TxnID=0 means "no response expected" for any request type.
 *============================================================*/

typedef enum {
    CRP_PKT_NOP          = 0x00,
    CRP_PKT_HELLO        = 0x01,
    CRP_PKT_BYE          = 0x02,

    CRP_PKT_WRITE        = 0x04,   /* txn>0: ack; txn=0: fire-and-forget */
    CRP_PKT_READ         = 0x06,
    CRP_PKT_SUBSCRIBE    = 0x08,   /* Client requests periodic data */
    CRP_PKT_PUSH_SETUP   = 0x09,   /* Device declares push-on-change */
    CRP_PKT_QUERY_REG    = 0x0A,   /* Query single register metadata */
    CRP_PKT_QUERY_GROUP  = 0x0B,   /* Query group at base address */

    CRP_PKT_RESPONSE     = 0x0C,
    CRP_PKT_CHANNEL_OPEN = 0x0E,
    CRP_PKT_BULK_DATA    = 0x10,
    CRP_PKT_SEEK         = 0x12,
    CRP_PKT_END          = 0x16,
    CRP_PKT_LOG          = 0x18,
    CRP_PKT_QUERY_DEVICE = 0x1A,
    CRP_PKT_UNLOCK       = 0x1C,
} crp_packet_type_t;

/*============================================================
 * LOG severity levels
 *============================================================*/

typedef enum {
    CRP_LOG_ERROR   = 0,
    CRP_LOG_WARN    = 1,
    CRP_LOG_INFO    = 2,
    CRP_LOG_DEBUG   = 3,
} crp_log_level_t;

/*============================================================
 * Status codes (unified — used in RESPONSE status field)
 *
 * Codes 0x01-0x03 are register-level errors.
 * Codes 0x04-0x09 are channel/session-level errors.
 *============================================================*/

typedef enum {
    CRP_STATUS_OK              = 0x00,

    /* Register-level */
    CRP_STATUS_UNKNOWN_REG     = 0x01,
    CRP_STATUS_ACCESS_DENIED   = 0x02,
    CRP_STATUS_INVALID_VALUE   = 0x03,

    /* Channel/session-level */
    CRP_STATUS_NO_CHANNELS     = 0x04,
    CRP_STATUS_BAD_CHANNEL     = 0x05,
    CRP_STATUS_NOT_SUBSCRIBABLE= 0x06,
    CRP_STATUS_INTERVAL_TOO_FAST=0x07,
    CRP_STATUS_BUSY            = 0x08,
    CRP_STATUS_TRANSFER_FAILED = 0x09,

    /* Protocol-level */
    CRP_STATUS_VERSION_MISMATCH= 0x0A,
} crp_status_t;

/*============================================================
 * Register data types
 *============================================================*/

typedef enum {
    CRP_TYPE_UINT8    = 0x01,
    CRP_TYPE_UINT16   = 0x02,
    CRP_TYPE_UINT32   = 0x03,
    CRP_TYPE_INT8     = 0x04,
    CRP_TYPE_INT16    = 0x05,
    CRP_TYPE_INT32    = 0x06,
    CRP_TYPE_FLOAT32  = 0x07,
    CRP_TYPE_FLOAT64  = 0x08,
    CRP_TYPE_BOOL     = 0x09,
    CRP_TYPE_STRING   = 0x0A,
    CRP_TYPE_BLOB     = 0x0B,
    CRP_TYPE_STRUCT   = 0x0C,
    CRP_TYPE_PROTOBUF = 0x0D,
} crp_data_type_t;

static inline uint32_t crp_type_fixed_size(crp_data_type_t type)
{
    switch (type) {
    case CRP_TYPE_UINT8:  case CRP_TYPE_INT8:  case CRP_TYPE_BOOL: return 1;
    case CRP_TYPE_UINT16: case CRP_TYPE_INT16: return 2;
    case CRP_TYPE_UINT32: case CRP_TYPE_INT32: case CRP_TYPE_FLOAT32: return 4;
    case CRP_TYPE_FLOAT64: return 8;
    default: return 0;
    }
}

static inline bool crp_type_is_fixed(crp_data_type_t type)
{
    return crp_type_fixed_size(type) > 0;
}

/*============================================================
 * ChanType bit layout
 *============================================================*/

#define CRP_CHANTYPE_MASK       0x1F
#define CRP_CHANTYPE_ENCRYPTED  0x40
#define CRP_CHANTYPE_SEEK       0x80

typedef enum {
    CRP_CHAN_SUBSCRIPTION = 1,
    CRP_CHAN_PUSH         = 2,
    CRP_CHAN_BULK_READ    = 3,
    CRP_CHAN_BULK_WRITE   = 4,
} crp_chan_base_type_t;

#define CRP_CHANTYPE_SUB        0x01
#define CRP_CHANTYPE_PUSH       0x02
#define CRP_CHANTYPE_BULK_RD    0x83
#define CRP_CHANTYPE_BULK_WR    0x84
#define CRP_CHANTYPE_BULK_RD_E  0xC3
#define CRP_CHANTYPE_BULK_WR_E  0xC4

static inline crp_chan_base_type_t crp_chantype_base(uint8_t ct) {
    return (crp_chan_base_type_t)(ct & CRP_CHANTYPE_MASK);
}
static inline bool crp_chantype_has_seek(uint8_t ct) {
    return (ct & CRP_CHANTYPE_SEEK) != 0;
}
static inline bool crp_chantype_is_encrypted(uint8_t ct) {
    return (ct & CRP_CHANTYPE_ENCRYPTED) != 0;
}
static inline bool crp_chantype_has_total_size(uint8_t ct) {
    uint8_t b = ct & CRP_CHANTYPE_MASK;
    return b == CRP_CHAN_BULK_READ || b == CRP_CHAN_BULK_WRITE;
}

/*============================================================
 * Register access flags
 *============================================================*/

#define CRP_ACCESS_R            0x01
#define CRP_ACCESS_W            0x02
#define CRP_ACCESS_RW           (CRP_ACCESS_R | CRP_ACCESS_W)

/*============================================================
 * Register flags
 *============================================================*/

#define CRP_REG_SUBSCRIBABLE    0x01
#define CRP_REG_UPDATED         0x02
#define CRP_REG_PUSH_ON_CHANGE  0x04

/*============================================================
 * HELLO flags
 *============================================================*/

#define CRP_FLAG_PUSH           0x01
#define CRP_FLAG_ASCII          0x04
#define CRP_FLAG_ENCRYPTION     0x08

/*============================================================
 * QUERY_GROUP response flags
 *============================================================*/

#define CRP_GROUP_READABLE      0x01
#define CRP_GROUP_WRITABLE      0x02
#define CRP_GROUP_SUBSCRIBABLE  0x04
#define CRP_GROUP_HAS_SUBGROUPS 0x08
#define CRP_GROUP_READ_LOCKED   0x10
#define CRP_GROUP_WRITE_LOCKED  0x20

/*============================================================
 * Minimum packet sizes (for validation)
 *============================================================*/

#define CRP_MIN_HELLO           10
#define CRP_MIN_BYE             9
#define CRP_MIN_WRITE           6   /* type(1)+txn(1)+reg(4), payload may be 0 */
#define CRP_MIN_READ            6
#define CRP_MIN_READ_BULK       14
#define CRP_MIN_RESPONSE        8
#define CRP_MIN_SUBSCRIBE       10
#define CRP_MIN_CHANNEL_OPEN    24
#define CRP_MIN_BULK_DATA       6
#define CRP_MIN_SEEK            6
#define CRP_MIN_QUERY_GROUP     6
#define CRP_MIN_QUERY_DEVICE    6
#define CRP_MIN_QUERY_REG       6
#define CRP_MIN_UNLOCK          38
#define CRP_MIN_LOG             7
#define CRP_MIN_END             2
#define CRP_MIN_NOP             1

/*============================================================
 * Utility: little-endian read/write
 *============================================================*/

static inline uint16_t crp_read_u16(const uint8_t *p) {
    return (uint16_t)p[0] | ((uint16_t)p[1] << 8);
}
static inline uint32_t crp_read_u32(const uint8_t *p) {
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8)
         | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}
static inline void crp_write_u16(uint8_t *p, uint16_t v) {
    p[0] = (uint8_t)(v); p[1] = (uint8_t)(v >> 8);
}
static inline void crp_write_u32(uint8_t *p, uint32_t v) {
    p[0] = (uint8_t)(v); p[1] = (uint8_t)(v >> 8);
    p[2] = (uint8_t)(v >> 16); p[3] = (uint8_t)(v >> 24);
}

#endif /* CRP_TYPES_H */
