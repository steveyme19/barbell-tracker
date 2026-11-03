/*
 * CRP — Channel Register Protocol
 * Transport abstraction.
 *
 * The engine uses this interface to send and receive framed packets.
 * Each send/recv operates on exactly one complete packet.
 */

#ifndef CRP_TRANSPORT_H
#define CRP_TRANSPORT_H

#include "crp_types.h"

typedef struct crp_transport {
    /**
     * Send a complete packet. Returns 0 on success, non-zero on error.
     */
    int (*send)(void *ctx, const uint8_t *data, uint32_t len);

    /**
     * Receive a packet (non-blocking).
     * Returns packet length, or 0 if nothing available.
     */
    uint32_t (*recv)(void *ctx, uint8_t *buf, uint32_t max_len);

    /**
     * Check if the transport can accept a send right now (optional).
     * Returns true if send() would succeed without blocking.
     * If NULL, the engine assumes always ready.
     */
    bool (*ready)(void *ctx);

    /**
     * Maximum transmission unit (optional, may be NULL).
     */
    uint32_t (*mtu)(void *ctx);

    /**
     * Flush pending output (optional, may be NULL).
     */
    void (*flush)(void *ctx);

    /**
     * Check if data is available for recv (optional, may be NULL).
     */
    uint32_t (*available)(void *ctx);

    void *ctx;
} crp_transport_t;

#endif /* CRP_TRANSPORT_H */
