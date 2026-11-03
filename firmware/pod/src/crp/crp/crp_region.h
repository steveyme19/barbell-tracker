/*
 * CRP — Channel Register Protocol
 * Region handler interface.
 *
 * A decode region is a {mask, base} pair that routes register accesses to a
 * handler. The handler is a function pointer table (crp_region_ops_t) plus an
 * opaque context pointer. Multiple regions can share the same ops table with
 * different contexts.
 *
 * Address matching: (address & mask) == base
 */

#ifndef CRP_REGION_H
#define CRP_REGION_H

#include "crp_types.h"

/*============================================================
 * Register and group metadata (returned by query ops)
 *============================================================*/

typedef struct {
    uint8_t     data_type;      /* CRP_TYPE_UINT8 .. CRP_TYPE_PROTOBUF */
    uint8_t     access;         /* CRP_ACCESS_R, CRP_ACCESS_W, CRP_ACCESS_RW */
    uint8_t     flags;          /* CRP_REG_SUBSCRIBABLE, CRP_REG_UPDATED */
    uint32_t    size;           /* Fixed byte size, or 0 for variable-length */
    uint32_t    min_interval_us;/* Hint: minimum useful subscribe interval (0 = no limit) */
    const char *name;           /* Optional human-readable name, NULL if unnamed */
} crp_reg_info_t;

typedef struct {
    uint32_t mask;
    uint8_t  flags;             /* CRP_GROUP_READABLE, _WRITABLE, _READ_LOCKED, etc. */
    uint32_t count;             /* Number of registers in this group */
    uint8_t  nonce[16];         /* Challenge nonce (non-zero when locked) */
} crp_group_info_t;

/*============================================================
 * Region ops — function pointer table ("vtable")
 *
 * All functions receive `ctx` as the first argument — this is the
 * opaque per-region private data set during registration.
 *
 * Optional functions may be NULL. The engine checks before calling.
 *============================================================*/

typedef struct crp_region_ops {
    /* Simple read: write up to *len bytes into buf, set *len to actual */
    crp_status_t (*read)(void *ctx, uint32_t reg,
                         uint8_t *buf, uint32_t *len);

    /* Simple write: apply len bytes from buf */
    crp_status_t (*write)(void *ctx, uint32_t reg,
                          const uint8_t *buf, uint32_t len);

    /* Query single register metadata */
    crp_status_t (*query_reg)(void *ctx, uint32_t reg,
                              crp_reg_info_t *info);

    /* Query group at base address */
    crp_status_t (*query_group)(void *ctx, uint32_t base,
                                crp_group_info_t *info);

    /* Bulk read: open a read stream */
    crp_status_t (*bulk_read_open)(void *ctx, uint32_t reg,
                                    uint32_t offset, uint32_t length,
                                    uint32_t *total_size);

    /* Bulk read: get next chunk. Set *len to bytes written (0 = done). */
    crp_status_t (*bulk_read_next)(void *ctx, uint32_t reg,
                                    uint32_t offset,
                                    uint8_t *buf, uint32_t *len);

    /* Bulk read: close stream */
    void (*bulk_read_close)(void *ctx, uint32_t reg);

    /* Bulk write: prepare to receive a stream */
    crp_status_t (*bulk_write_open)(void *ctx, uint32_t reg,
                                     uint32_t total_size);

    /* Bulk write: receive a chunk */
    crp_status_t (*bulk_write_chunk)(void *ctx, uint32_t reg,
                                      uint32_t offset,
                                      const uint8_t *buf, uint32_t len);

    /* Bulk write: close stream */
    crp_status_t (*bulk_write_close)(void *ctx, uint32_t reg);

    /* Subscription read: called when engine needs fresh values for a tick */
    crp_status_t (*subscribe_read)(void *ctx, uint32_t reg,
                                    uint8_t *buf, uint32_t *len);

    /* Tick: called periodically (update generators, check timeouts, etc.) */
    void (*tick)(void *ctx, uint32_t now_ms);

    /* Access control: verify HMAC proof and unlock group for this session */
    crp_status_t (*unlock)(void *ctx, uint32_t group,
                            const uint8_t proof[32]);
} crp_region_ops_t;

/*============================================================
 * Decode region: mask/base pair + handler
 *============================================================*/

typedef struct {
    uint32_t                mask;
    uint32_t                base;
    void                   *ctx;    /* Opaque private data for this instance */
    const crp_region_ops_t *ops;    /* Shared function pointer table */
    bool                    active; /* Slot in use */
} crp_region_t;

/*============================================================
 * Region dispatch table (pre-allocated, fixed size)
 *============================================================*/

typedef struct {
    crp_region_t regions[CRP_MAX_REGIONS];
    uint8_t      count;
} crp_region_table_t;

/*============================================================
 * API
 *============================================================*/

/**
 * Initialize a region table (zero all slots).
 */
void crp_region_table_init(crp_region_table_t *table);

/**
 * Register a new decode region.
 * Returns CRP_STATUS_OK on success, CRP_STATUS_BUSY if table is full.
 */
crp_status_t crp_region_register(crp_region_table_t *table,
                                  uint32_t mask, uint32_t base,
                                  const crp_region_ops_t *ops, void *ctx);

/**
 * Unregister a region by mask/base. Sets it inactive.
 * Active channels referencing this region should be ended first.
 */
void crp_region_unregister(crp_region_table_t *table, uint32_t mask,
                            uint32_t base);

/**
 * Look up the region that handles `address`.
 * Returns pointer to region, or NULL if no match.
 * Searches in registration order; first match wins.
 */
crp_region_t *crp_region_lookup(crp_region_table_t *table, uint32_t address);

/**
 * Tick all regions that have a tick handler.
 */
void crp_region_tick_all(crp_region_table_t *table, uint32_t now_ms);

#endif /* CRP_REGION_H */
