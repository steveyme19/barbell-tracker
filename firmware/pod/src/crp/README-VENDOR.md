# crp: the vendored CRP device engine (Ansync Labs)

The pod's Bluetooth and USB links speak CRP, Ansync Labs' Channel Register Protocol (decision D45,
`docs/ble.md`). This folder is a copy of its device engine. Vendored with Ansync's OK into this
public repo (Quentin, 2026-11-02).

- **Origin:** https://gitlab.com/a2575/ansyncdev/channel-register-protocol.git
  (`device/engine/{src,include}`), local clone `C:\Users\quent\crp-study`.
- **Upstream commit:** `61869ad` "crp: READ packet gains optional total_size payload for variable-size
  bulk reads", protocol version 0x0B.
- **Taken from** the coil driver's patched copy,
  `C:\Users\quent\coil-driver-devkit\sw_firmware_libs\modules\crp` (snapshot 2026-08-12). Its five
  local patches come along; its own notes are in `README-VENDOR.coil.md`.
- **Layout for the Arduino builder,** which compiles only the sketch folder and `src/` recursively:
  - the `.c` files sit here and include `"crp/xxx.h"`, so the headers sit in `crp/`;
  - `crp_device_store.*` is left out (unused);
  - nothing in here is Arduino code. The pod's glue is `firmware/pod/podcrp.c`.

## Local patches (grep `LOCAL PATCH` in the sources)

From the coil copy:
1. `handle_response` NULL check (crp_engine.c). Upstream dereferences `find_txn()` unchecked, so an
   unknown txn in a RESPONSE hard-faults the MCU.
2. Subscription BULK_DATA offset 0 (crp_engine.c). This is for coil's client. The pod opens no
   subscriptions (HELLO max_channels 1, no SUBSCRIBABLE registers), so it never runs here.
3. Duplicate-SUBSCRIBE idempotency (crp_engine.c). Never runs here either.
4. QUERY_REG miss answers UNKNOWN_REG (crp_engine.c), which Ansync's discovery walk needs.
5. QUERY_GROUP count excludes the group base (crp_std_region.c). The pod's own region follows the same
   convention.

For the pod (D45):
6. `crp_engine_push_change` returns the send result (0 = sent). The pod's frame pump must know a push
   was accepted: pkt_seq only counts accepted frames, and a refused one is resent byte-identical.
7. The 1 KB channel buffer in `engine_tick_channels` is `static`. The Arduino loop task has a 4 KB
   stack, and both engines (BLE and USB) run on it one after the other.
