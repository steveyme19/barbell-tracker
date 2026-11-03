/*
 * CRP — Standard Region Handler
 *
 * A reusable region implementation for CRP devices. Registers are
 * declared as descriptors with two modes:
 *
 *   Data-pointer: reads/writes directly to a typed variable.
 *     REG_RW_MAX(addr, "name", UINT16, &my_var, apply_fn, 1000)
 *
 *   Function: custom read/write for active I/O (sensors, I2C).
 *     REG_RS_FN(addr, "name", FLOAT32, my_read_fn)
 *
 * Data-pointer mode handles LE serialization, type-aware validation,
 * and post-write callbacks automatically. No per-register boilerplate.
 */

#ifndef CRP_STD_REGION_H
#define CRP_STD_REGION_H

#include "crp_types.h"
#include "crp_region.h"

/*============================================================
 * Register descriptor
 *============================================================*/

typedef crp_status_t (*crp_reg_read_fn)(uint8_t *buf, uint32_t *len);
typedef crp_status_t (*crp_reg_write_fn)(const uint8_t *buf, uint32_t len);
typedef void (*crp_on_write_fn)(uint32_t addr);

typedef struct {
    uint32_t           address;
    const char        *name;
    uint8_t            data_type;   /* CRP_TYPE_* */
    uint32_t           size;        /* Fixed byte size, 0 for variable */
    uint8_t            access;      /* CRP_ACCESS_R, _W, _RW */
    uint8_t            flags;       /* CRP_REG_SUBSCRIBABLE, etc. */
    void              *data;        /* Direct pointer (NULL = use fns) */
    crp_reg_read_fn    read;        /* Override read (NULL = auto from data) */
    crp_reg_write_fn   write;       /* Override write (NULL = auto from data) */
    crp_on_write_fn    on_write;    /* Called after data-pointer write */
    uint32_t           max_val;     /* If nonzero, reject integer writes > max_val */
    uint32_t           write_mask;  /* If nonzero, only these bits are modified by external write */
} crp_reg_desc_t;

/*============================================================
 * Standard region — wraps a descriptor array
 *============================================================*/

typedef struct {
    const crp_reg_desc_t *regs;
    uint16_t              reg_count;     /* widened from uint8_t — region_ad9084 has >100 regs */
    uint32_t              group_mask;
    uint32_t              group_base;
} crp_std_region_t;

/*============================================================
 * Descriptor macros
 *============================================================*/

/* Size lookup (compile-time) */
#define _CSZ_UINT8   1
#define _CSZ_UINT16  2
#define _CSZ_UINT32  4
#define _CSZ_INT8    1
#define _CSZ_INT16   2
#define _CSZ_INT32   4
#define _CSZ_FLOAT32 4
#define _CSZ_FLOAT64 8
#define _CSZ_BOOL    1
#define _CSZ_STRING  0

/* Read-only data pointer */
#define REG_R(a, n, T, ptr) \
    { a, n, CRP_TYPE_##T, _CSZ_##T, CRP_ACCESS_R, 0, ptr, NULL, NULL, NULL, 0, 0 }

/* Read-only subscribable */
#define REG_RS(a, n, T, ptr) \
    { a, n, CRP_TYPE_##T, _CSZ_##T, CRP_ACCESS_R, CRP_REG_SUBSCRIBABLE, ptr, NULL, NULL, NULL, 0, 0 }

/* Read-write + on_write */
#define REG_RW(a, n, T, ptr, cb) \
    { a, n, CRP_TYPE_##T, _CSZ_##T, CRP_ACCESS_RW, 0, ptr, NULL, NULL, cb, 0, 0 }

/* Read-write subscribable + on_write */
#define REG_RWS(a, n, T, ptr, cb) \
    { a, n, CRP_TYPE_##T, _CSZ_##T, CRP_ACCESS_RW, CRP_REG_SUBSCRIBABLE, ptr, NULL, NULL, cb, 0, 0 }

/* Read-write + on_write + max value */
#define REG_RW_MAX(a, n, T, ptr, cb, mx) \
    { a, n, CRP_TYPE_##T, _CSZ_##T, CRP_ACCESS_RW, 0, ptr, NULL, NULL, cb, mx, 0 }

/* Read-write subscribable + max value */
#define REG_RWS_MAX(a, n, T, ptr, cb, mx) \
    { a, n, CRP_TYPE_##T, _CSZ_##T, CRP_ACCESS_RW, CRP_REG_SUBSCRIBABLE, ptr, NULL, NULL, cb, mx, 0 }

/* Read-write with write mask — external writes only modify masked bits.
 * Use for registers where firmware owns some bitfields and the external
 * client controls others (e.g., device headers with enabled/state/error). */
#define REG_MASKED(a, n, T, ptr, cb, wmask) \
    { a, n, CRP_TYPE_##T, _CSZ_##T, CRP_ACCESS_RW, CRP_REG_SUBSCRIBABLE, ptr, NULL, NULL, cb, 0, wmask }

/* Write-only with custom function */
#define REG_W_FN(a, n, T, wfn) \
    { a, n, CRP_TYPE_##T, _CSZ_##T, CRP_ACCESS_W, 0, NULL, NULL, wfn, NULL, 0, 0 }

/* Read-only with custom function */
#define REG_R_FN(a, n, T, rfn) \
    { a, n, CRP_TYPE_##T, _CSZ_##T, CRP_ACCESS_R, 0, NULL, rfn, NULL, NULL, 0, 0 }

/* Read-only subscribable with custom function */
#define REG_RS_FN(a, n, T, rfn) \
    { a, n, CRP_TYPE_##T, _CSZ_##T, CRP_ACCESS_R, CRP_REG_SUBSCRIBABLE, NULL, rfn, NULL, NULL, 0, 0 }

/* Read-write with custom functions */
#define REG_RW_FN(a, n, T, rfn, wfn) \
    { a, n, CRP_TYPE_##T, _CSZ_##T, CRP_ACCESS_RW, 0, NULL, rfn, wfn, NULL, 0, 0 }

/* Read-only string (null-terminated, variable-length) */
#define REG_STR(a, n, ptr) \
    { a, n, CRP_TYPE_STRING, 0, CRP_ACCESS_R, 0, ptr, NULL, NULL, NULL, 0, 0 }

/* Read-only blob (fixed size, data pointer) */
#define REG_BLOB_R(a, n, ptr, sz) \
    { a, n, CRP_TYPE_BLOB, sz, CRP_ACCESS_R, 0, ptr, NULL, NULL, NULL, 0, 0 }

/* Read-write blob (fixed size, data pointer + on_write) */
#define REG_BLOB_RW(a, n, ptr, sz, cb) \
    { a, n, CRP_TYPE_BLOB, sz, CRP_ACCESS_RW, 0, ptr, NULL, NULL, cb, 0, 0 }

/* Read-only blob with custom read function (dynamic size) */
#define REG_BLOB_R_FN(a, n, rfn) \
    { a, n, CRP_TYPE_BLOB, 0, CRP_ACCESS_R, 0, NULL, rfn, NULL, NULL, 0, 0 }

/* Read-write blob with custom functions */
#define REG_BLOB_RW_FN(a, n, rfn, wfn) \
    { a, n, CRP_TYPE_BLOB, 0, CRP_ACCESS_RW, 0, NULL, rfn, wfn, NULL, 0, 0 }

/* Region struct initializer */
#define CRP_STD_REGION(name, base, regs_arr) \
    static crp_std_region_t name = { \
        .regs = regs_arr, \
        .reg_count = sizeof(regs_arr) / sizeof(regs_arr[0]), \
        .group_mask = 0xFFFF0000, \
        .group_base = base, \
    }

/*============================================================
 * API
 *============================================================*/

/** Shared ops table — pass to crp_region_register(). */
extern const crp_region_ops_t crp_std_region_ops;

/** Register a standard region with the engine's region table. */
crp_status_t crp_std_region_register(crp_region_table_t *table,
                                      crp_std_region_t *region);

/** Look up a register descriptor by address. */
const crp_reg_desc_t *crp_std_reg_find(const crp_std_region_t *region,
                                        uint32_t address);

#endif /* CRP_STD_REGION_H */
