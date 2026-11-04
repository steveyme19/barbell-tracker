# crp: the vendored CRP JS client (Ansync Labs)

The app talks to the pod in CRP, Ansync Labs' Channel Register Protocol (decision D45,
`docs/ble.md`). These files are Ansync's browser client, plain ES modules with no build step.
Vendored with Ansync's OK into this public repo (Quentin, 2026-11-02).

- **Origin:** https://gitlab.com/a2575/ansyncdev/channel-register-protocol.git
  (`clients/js/browser/`), local clone `C:\Users\quent\crp-study`, commit `61869ad` (protocol 0x0B).
- **Files:**
  - `crp-client.js` (CRPClient: HELLO, READ/WRITE transactions, NOP heartbeat);
  - `event-emitter.js`;
  - `byte-utils.js`.
  Byte-identical to coil-webdriver's copy of the same commit.
- **Not used:**
  - their transports. `webapp/sources.js` has its own, because the pod's decoder needs every packet's
    arrival time, in order, which the stock transports don't give.
  - discovery and the data codec.

## How the app uses it
CRPClient only runs the session and transactions:
- `txnRetries: 0`, because the engine re-runs a retried WRITE;
- `heartbeatMs: 0` on Bluetooth, because the pod ignores liveness there.

PodStream gets its frames from the transport's tap, not from CRPClient's events.

The pod's side of it is `PodCrpLink` in `webapp/sources.js`; `webapp/test/crp_wire.mjs` checks the
client's bytes against `analysis/crp.py --vectors` and the patches below.

## Local patches (grep `LOCAL PATCH (pod, D45)`)
Only `crp-client.js` is patched; the other two files are as vendored.
- **J1** (`_onResponse`): a transaction rejected by a RESPONSE status gets an Error with `code` (the
  status number, BUSY = 8) and `reg` (the register), so `PodCrpLink.readFrame` can retry a BUSY
  without parsing the message. The message text is unchanged.
- **J2** (`_send`, `_watchSend`, `_sendWithTxn`, the retry in `_onTxnTimeout`): `_send` returns what
  the transport's `send` returned, and if that's a promise that rejects (a Bluetooth write the link
  refused), the transaction fails at once with that error instead of timing out 5 s later. Only
  while the txn id still belongs to that transaction.
  - Sends without a transaction (HELLO, NOP, BYE, `writeNoack`, END) still drop the result, as
    upstream: a transport whose `send` can reject must catch those itself.

## Gotchas in the vendored code (left as they are)
- `Status.BUSY` is 4, a leftover from an older protocol. The engine's BUSY is 8: use
  `ErrorCode.BUSY`.
- A rejection from J1 and a `DOMException` both have a numeric `code`; only J1's also has `reg`.
- `close()` is final (`_closed` stays set): a new session needs a new CRPClient.
- LOG (0x18) has no handler: it comes out as an `unknown-packet` event.
