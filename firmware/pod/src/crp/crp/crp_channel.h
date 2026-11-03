/*
 * CRP — Channel Register Protocol
 * Channel state machine and management.
 *
 * Channels are pre-allocated in a fixed-size array (CRP_MAX_CHANNELS).
 * Each channel tracks its lifecycle state, associated register, transfer
 * progress, and subscription parameters.
 */

#ifndef CRP_CHANNEL_H
#define CRP_CHANNEL_H

#include "crp_types.h"
#include "crp_region.h"

/*============================================================
 * Channel states
 *============================================================*/

typedef enum {
    CRP_CH_IDLE = 0,        /* Slot available for use */
    CRP_CH_OPENING,         /* CHANNEL_OPEN sent, waiting for peer */
    CRP_CH_ACTIVE,          /* Data flowing */
    CRP_CH_CLOSING,         /* END sent, waiting for peer END */
} crp_channel_state_t;

/*============================================================
 * Channel structure
 *============================================================*/

typedef struct {
    crp_channel_state_t state;
    uint8_t             channel_num;    /* 0–15 */
    uint8_t             chan_type;       /* ChanType byte (bits: type, encrypt, seek) */
    uint8_t             txn_id;         /* TxnID that opened this channel */
    uint32_t            reg_addr;       /* Register this channel operates on */
    crp_region_t       *region;         /* Pointer into region table (no alloc) */

    /* Transfer offset (shared by bulk and subscription) */
    uint32_t            offset;         /* Current transfer offset */

    /* Type-specific state — a channel is subscription OR bulk, never both */
    union {
        struct {
            uint32_t    total_size;     /* Total bytes. 0xFFFFFFFF = streaming */
            uint32_t    expected_offset;/* For gap detection (receiver side) */
        } bulk;
        struct {
            uint32_t        interval_us;    /* Subscription interval in microseconds */
            uint32_t        last_tick_ms;   /* Last time subscription data was sent */
            uint32_t        regs[CRP_MAX_SUB_REGS]; /* Register address list */
            crp_region_t   *reg_regions[CRP_MAX_SUB_REGS]; /* Cached region pointers */
            uint8_t         reg_count;      /* Number of registers in subscription */
        } sub;
    };

    /* Encryption nonce (when ChanType bit 6 set) */
    uint8_t             nonce[12];

    /* Timeout / activity tracking */
    uint32_t            last_activity_ms;

    /* Pause state (SEEK 0xFFFFFFFF) */
    bool                paused;
} crp_channel_t;

/*============================================================
 * Channel array management
 *============================================================*/

typedef struct {
    crp_channel_t channels[CRP_MAX_CHANNELS];
    uint8_t       max_channels;     /* Negotiated limit (may be < CRP_MAX_CHANNELS) */
} crp_channel_table_t;

/*============================================================
 * API
 *============================================================*/

/**
 * Initialize channel table (all IDLE).
 */
void crp_channel_table_init(crp_channel_table_t *table, uint8_t max_channels);

/**
 * Allocate an idle channel slot. Returns channel number (0–15),
 * or -1 if all slots are in use.
 */
int crp_channel_alloc(crp_channel_table_t *table);

/**
 * Get channel by number. Returns NULL if channel_num >= max_channels.
 */
crp_channel_t *crp_channel_get(crp_channel_table_t *table, uint8_t channel_num);

/**
 * Release a channel back to IDLE state. Clears all fields.
 */
void crp_channel_release(crp_channel_table_t *table, uint8_t channel_num);

/**
 * Release all channels (e.g., on HELLO reset).
 */
void crp_channel_release_all(crp_channel_table_t *table);

/**
 * Find channel by TxnID. Returns pointer, or NULL if not found.
 */
crp_channel_t *crp_channel_find_by_txn(crp_channel_table_t *table,
                                        uint8_t txn_id);

/**
 * Count active (non-IDLE) channels.
 */
uint8_t crp_channel_active_count(const crp_channel_table_t *table);

#endif /* CRP_CHANNEL_H */
