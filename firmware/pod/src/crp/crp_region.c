/*
 * CRP — Channel Register Protocol
 * Region table: mask/base lookup and dispatch.
 */

#include "crp/crp_region.h"
#include <string.h>

void crp_region_table_init(crp_region_table_t *table)
{
    memset(table, 0, sizeof(*table));
}

crp_status_t crp_region_register(crp_region_table_t *table,
                                  uint32_t mask, uint32_t base,
                                  const crp_region_ops_t *ops, void *ctx)
{
    if (table->count >= CRP_MAX_REGIONS) {
        return CRP_STATUS_BUSY;
    }

    crp_region_t *r = &table->regions[table->count];
    r->mask   = mask;
    r->base   = base;
    r->ops    = ops;
    r->ctx    = ctx;
    r->active = true;
    table->count++;

    return CRP_STATUS_OK;
}

void crp_region_unregister(crp_region_table_t *table, uint32_t mask,
                            uint32_t base)
{
    for (uint8_t i = 0; i < table->count; i++) {
        crp_region_t *r = &table->regions[i];
        if (r->active && r->mask == mask && r->base == base) {
            r->active = false;
            r->ops    = NULL;
            r->ctx    = NULL;
        }
    }
}

crp_region_t *crp_region_lookup(crp_region_table_t *table, uint32_t address)
{
    for (uint8_t i = 0; i < table->count; i++) {
        crp_region_t *r = &table->regions[i];
        if (r->active && (address & r->mask) == r->base) {
            return r;
        }
    }
    return NULL;
}

void crp_region_tick_all(crp_region_table_t *table, uint32_t now_ms)
{
    for (uint8_t i = 0; i < table->count; i++) {
        crp_region_t *r = &table->regions[i];
        if (r->active && r->ops && r->ops->tick) {
            r->ops->tick(r->ctx, now_ms);
        }
    }
}
