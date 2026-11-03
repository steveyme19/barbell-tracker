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

## Local patches (grep `LOCAL PATCH`)
None yet. Planned (D45):
- J1: rejected transactions carry `code` and `reg`.
- J2: a transport `send` that returns a rejected promise rejects its transaction at once.
