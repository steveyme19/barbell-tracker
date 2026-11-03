/*
 * CRP — Channel Register Protocol
 * Channel array management and state transitions.
 */

#include "crp/crp_channel.h"
#include <string.h>

void crp_channel_table_init(crp_channel_table_t *table, uint8_t max_channels)
{
    memset(table, 0, sizeof(*table));
    table->max_channels = (max_channels > CRP_MAX_CHANNELS)
                          ? CRP_MAX_CHANNELS : max_channels;
}

int crp_channel_alloc(crp_channel_table_t *table)
{
    for (uint8_t i = 0; i < table->max_channels; i++) {
        if (table->channels[i].state == CRP_CH_IDLE) {
            table->channels[i].channel_num = i;
            return (int)i;
        }
    }
    return -1;  /* No free channels */
}

crp_channel_t *crp_channel_get(crp_channel_table_t *table, uint8_t channel_num)
{
    if (channel_num >= table->max_channels) {
        return NULL;
    }
    return &table->channels[channel_num];
}

void crp_channel_release(crp_channel_table_t *table, uint8_t channel_num)
{
    if (channel_num >= table->max_channels) return;

    memset(&table->channels[channel_num], 0, sizeof(crp_channel_t));
    table->channels[channel_num].channel_num = channel_num;
    /* state is CRP_CH_IDLE (0) after memset */
}

void crp_channel_release_all(crp_channel_table_t *table)
{
    for (uint8_t i = 0; i < table->max_channels; i++) {
        crp_channel_release(table, i);
    }
}

crp_channel_t *crp_channel_find_by_txn(crp_channel_table_t *table,
                                        uint8_t txn_id)
{
    for (uint8_t i = 0; i < table->max_channels; i++) {
        crp_channel_t *ch = &table->channels[i];
        if (ch->state != CRP_CH_IDLE && ch->txn_id == txn_id) {
            return ch;
        }
    }
    return NULL;
}

uint8_t crp_channel_active_count(const crp_channel_table_t *table)
{
    uint8_t count = 0;
    for (uint8_t i = 0; i < table->max_channels; i++) {
        if (table->channels[i].state != CRP_CH_IDLE) {
            count++;
        }
    }
    return count;
}
