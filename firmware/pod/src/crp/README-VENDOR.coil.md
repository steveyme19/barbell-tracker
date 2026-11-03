# crp — vendored CRP device engine

Vendored copy of the CRP (Channel Register Protocol) **device engine** for the
coil driver's ALIMP→CRP migration (`frequency_generator`, 2026-07-16).

- **Origin:** https://gitlab.com/a2575/ansyncdev/channel-register-protocol.git
  (`device/engine/{src,include}`), local clone `C:\Users\quent\crp-study`
- **Vendored at commit:** `61869ad` "crp: READ packet gains optional total_size
  payload for variable-size bulk reads" (protocol version 0x0B)
- The MSR side (`dev_jcMsr23`) consumes the same engine as a git **submodule** at
  `firmware/msr/library/crp`; a source copy was chosen here to keep the coil
  worktree flow simple. Switching to the submodule later is mechanical.

## Local patches (grep `LOCAL PATCH` in src/)
1. **handle_response NULL-deref fix** (crp_engine.c) — upstream dereferences
   `find_txn()` without a NULL check; an unknown txn_id in a RESPONSE
   hard-faults the MCU.
2. **Subscription/push BULK_DATA wire offset = 0** (crp_engine.c) — matches the
   coil-webdriver client + sim (contract `coil-webdriver/CRP-REGISTER-MAP.md`);
   bulk channels keep real offsets.
3. **Duplicate-SUBSCRIBE idempotency** (crp_engine.c, handle_subscribe) — the
   client retries a timed-out SUBSCRIBE with the same txn; upstream opened a
   second channel (double pushes + a leaked channel per retry). An active
   subscription matching txn+interval+reg-list is re-answered instead.
4. **QUERY_REG miss → status UNKNOWN_REG** (crp_engine.c) — upstream replies
   OK + 12 zero bytes, which the client's discovery walk counts as a found
   register, terminating the walk before the map's tail (FAULT/BUS_V).
   Matches the coil-webdriver sim.
5. **QUERY_GROUP count excludes the group-base register** (crp_std_region.c) —
   the client's walk starts at offset 1, so VER at offset 0 must not inflate
   the count. Matches the coil-webdriver sim.

All belong upstream (1 and 3 unconditionally; 2/4/5 as config or spec
clarifications) — flag to maxman before mainlining.

## Build note
Only `crp_engine.c, crp_packet.c, crp_region.c, crp_channel.c, crp_std_region.c`
are needed by the coil firmware (`crp_device_store.c` is unused; upstream's
CMakeLists also omits `crp_std_region.c` — our Makefile lists sources explicitly).
