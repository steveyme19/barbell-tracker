/*
 * CRP — Standard Region Handler
 *
 * Generic ops implementation for crp_std_region_t. Supports data-pointer
 * registers (auto-serialized) and function-pointer registers (custom I/O).
 */

#include "crp/crp_std_region.h"
#include <string.h>

/*============================================================
 * Descriptor lookup
 *============================================================*/

const crp_reg_desc_t *crp_std_reg_find(const crp_std_region_t *region,
                                        uint32_t address)
{
    for (uint16_t i = 0; i < region->reg_count; i++) {
        if (region->regs[i].address == address) {
            return &region->regs[i];
        }
    }
    return NULL;
}

/*============================================================
 * Data-pointer read: type-aware LE serialization
 *============================================================*/

static crp_status_t data_read(const crp_reg_desc_t *d,
                               uint8_t *buf, uint32_t *len)
{
    if (d->data_type == CRP_TYPE_STRING) {
        const char *s = (const char *)d->data;
        uint32_t slen = 0;
        while (s[slen]) slen++;
        if (*len < slen) slen = *len;
        memcpy(buf, s, slen);
        *len = slen;
        return CRP_STATUS_OK;
    }

    uint32_t sz = d->size;
    if (*len < sz) return CRP_STATUS_BUSY;
    *len = sz;

    switch (d->data_type) {
    case CRP_TYPE_UINT8: case CRP_TYPE_INT8: case CRP_TYPE_BOOL:
        buf[0] = *(const uint8_t *)d->data;
        break;
    case CRP_TYPE_UINT16: case CRP_TYPE_INT16:
        crp_write_u16(buf, *(const uint16_t *)d->data);
        break;
    case CRP_TYPE_UINT32: case CRP_TYPE_INT32:
        crp_write_u32(buf, *(const uint32_t *)d->data);
        break;
    case CRP_TYPE_FLOAT32: {
        uint32_t bits;
        memcpy(&bits, d->data, 4);
        crp_write_u32(buf, bits);
        break;
    }
    default:
        memcpy(buf, d->data, sz);
        break;
    }
    return CRP_STATUS_OK;
}

/*============================================================
 * Data-pointer write: LE deserialization + validation + callback
 *============================================================*/

static crp_status_t data_write(const crp_reg_desc_t *d,
                                const uint8_t *buf, uint32_t len)
{
    if (len < d->size) return CRP_STATUS_INVALID_VALUE;

    if (d->max_val > 0) {
        uint32_t v = 0;
        switch (d->data_type) {
        case CRP_TYPE_UINT8:  v = buf[0]; break;
        case CRP_TYPE_UINT16: v = crp_read_u16(buf); break;
        case CRP_TYPE_UINT32: v = crp_read_u32(buf); break;
        default: break;
        }
        if (v > d->max_val) return CRP_STATUS_INVALID_VALUE;
    }

    /*
     * write_mask support: if nonzero, only the masked bits are modified.
     * This allows firmware to own certain bitfields (e.g., state, error)
     * while the external client controls others (e.g., enabled bit).
     * Only supported for integer types where bitwise merge makes sense.
     */
    if (d->write_mask != 0 && d->data) {
        uint32_t written = 0;
        uint32_t current = 0;
        switch (d->data_type) {
        case CRP_TYPE_UINT8:
            written = buf[0];
            current = *(uint8_t *)d->data;
            *(uint8_t *)d->data = (uint8_t)((current & ~d->write_mask) |
                                             (written & d->write_mask));
            break;
        case CRP_TYPE_UINT16:
            written = crp_read_u16(buf);
            current = *(uint16_t *)d->data;
            *(uint16_t *)d->data = (uint16_t)((current & ~d->write_mask) |
                                               (written & d->write_mask));
            break;
        case CRP_TYPE_UINT32: case CRP_TYPE_INT32:
            written = crp_read_u32(buf);
            current = *(uint32_t *)d->data;
            *(uint32_t *)d->data = (current & ~d->write_mask) |
                                   (written & d->write_mask);
            break;
        default:
            memcpy(d->data, buf, d->size);
            break;
        }
        if (d->on_write) d->on_write(d->address);
        return CRP_STATUS_OK;
    }

    switch (d->data_type) {
    case CRP_TYPE_UINT8: case CRP_TYPE_INT8: case CRP_TYPE_BOOL:
        *(uint8_t *)d->data = buf[0];
        break;
    case CRP_TYPE_UINT16: case CRP_TYPE_INT16:
        *(uint16_t *)d->data = crp_read_u16(buf);
        break;
    case CRP_TYPE_UINT32: case CRP_TYPE_INT32:
        *(uint32_t *)d->data = crp_read_u32(buf);
        break;
    case CRP_TYPE_FLOAT32: {
        uint32_t bits = crp_read_u32(buf);
        memcpy(d->data, &bits, 4);
        break;
    }
    default:
        memcpy(d->data, buf, d->size);
        break;
    }

    if (d->on_write) d->on_write(d->address);
    return CRP_STATUS_OK;
}

/*============================================================
 * Region ops
 *============================================================*/

static crp_status_t std_read(void *ctx, uint32_t reg,
                              uint8_t *buf, uint32_t *len)
{
    const crp_std_region_t *r = (const crp_std_region_t *)ctx;
    const crp_reg_desc_t *d = crp_std_reg_find(r, reg);
    if (!d) return CRP_STATUS_UNKNOWN_REG;
    if (!(d->access & CRP_ACCESS_R)) return CRP_STATUS_ACCESS_DENIED;
    if (d->read) return d->read(buf, len);
    if (d->data) return data_read(d, buf, len);
    return CRP_STATUS_ACCESS_DENIED;
}

static crp_status_t std_write(void *ctx, uint32_t reg,
                               const uint8_t *buf, uint32_t len)
{
    const crp_std_region_t *r = (const crp_std_region_t *)ctx;
    const crp_reg_desc_t *d = crp_std_reg_find(r, reg);
    if (!d) return CRP_STATUS_UNKNOWN_REG;
    if (!(d->access & CRP_ACCESS_W)) return CRP_STATUS_ACCESS_DENIED;
    if (d->write) return d->write(buf, len);
    if (d->data) return data_write(d, buf, len);
    return CRP_STATUS_ACCESS_DENIED;
}

static crp_status_t std_query_reg(void *ctx, uint32_t reg,
                                   crp_reg_info_t *info)
{
    const crp_std_region_t *r = (const crp_std_region_t *)ctx;
    const crp_reg_desc_t *d = crp_std_reg_find(r, reg);
    if (!d) return CRP_STATUS_UNKNOWN_REG;
    info->data_type = d->data_type;
    info->access    = d->access;
    info->flags     = d->flags;
    info->size      = d->size;
    info->name      = d->name;
    return CRP_STATUS_OK;
}

static crp_status_t std_query_group(void *ctx, uint32_t base,
                                     crp_group_info_t *info)
{
    const crp_std_region_t *r = (const crp_std_region_t *)ctx;
    if ((base & r->group_mask) != r->group_base)
        return CRP_STATUS_UNKNOWN_REG;
    info->mask  = r->group_mask;
    /* LOCAL PATCH (coil, 2026-07-16): report the count of DISCOVERABLE
     * registers. The client's walk starts at offset 1 (offset 0 = the group
     * itself), so a register parked at the group base (VER) must not inflate
     * the count or the walk under-runs the tail. Matches the coil-webdriver sim. */
    {
        uint32_t n = 0;
        for (uint16_t i = 0; i < r->reg_count; i++)
            if (r->regs[i].address != r->group_base) n++;
        info->count = n;
    }
    uint8_t flags = 0;
    for (uint16_t i = 0; i < r->reg_count; i++) {
        if (r->regs[i].access & CRP_ACCESS_R) flags |= CRP_GROUP_READABLE;
        if (r->regs[i].access & CRP_ACCESS_W) flags |= CRP_GROUP_WRITABLE;
        if (r->regs[i].flags & CRP_REG_SUBSCRIBABLE) flags |= CRP_GROUP_SUBSCRIBABLE;
    }
    info->flags = flags;
    return CRP_STATUS_OK;
}

static crp_status_t std_subscribe_read(void *ctx, uint32_t reg,
                                        uint8_t *buf, uint32_t *len)
{
    return std_read(ctx, reg, buf, len);
}

const crp_region_ops_t crp_std_region_ops = {
    .read           = std_read,
    .write          = std_write,
    .query_reg      = std_query_reg,
    .query_group    = std_query_group,
    .subscribe_read = std_subscribe_read,
};

crp_status_t crp_std_region_register(crp_region_table_t *table,
                                      crp_std_region_t *region)
{
    return crp_region_register(table, region->group_mask, region->group_base,
                                &crp_std_region_ops, region);
}
