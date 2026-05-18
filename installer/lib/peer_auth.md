# `peer_auth.py`

**Module purpose.** Ed25519 identity for inter-node authenticated
API calls. Each node holds a long-lived keypair at
`/etc/bedrock/peer.key` (private, mode 0600) and
`/etc/bedrock/peer.key.pub`; the public key is registered in
rqlite (`nodes.bedrock_pubkey`) when the node joins.

Used by the `/api/peer/*` endpoints in `mgmt/app.py`: any node
can ask any other node for its loopback IP, drbd state, etc.,
with the requester signing a per-request nonce + body.

Distinct from `cluster.key` (the witness HMAC) — peer_auth is
asymmetric (signed by sender, verified by receiver against
registered pubkey), witness uses symmetric HMAC because the
Echo can't track per-node pubkeys.

## Constants

- `KEY_PATH = /etc/bedrock/peer.key` — private (mode 0600).
- `PUB_PATH = /etc/bedrock/peer.key.pub` — public hex.

## Functions

- `ensure_keypair() -> tuple[Ed25519PrivateKey, str]` — load
  the existing keypair, or generate + write + return a fresh
  one. Idempotent. Returns `(priv_key, pub_hex)`.
- `pubkey_hex() -> str` — short read of `PUB_PATH`. Used by
  `mgmt_install` + `agent_install` to populate
  `nodes.bedrock_pubkey` at init/join.
- `sign(payload: bytes) -> str` — Ed25519 sign + base64-url
  encode the signature. Caller assembles the canonical request
  body and signs it.
- `verify(pub_hex: str, payload: bytes, sig_b64: str) -> bool`
  — inverse: decode + verify against the supplied pubkey. Used
  by `/api/peer/*` request handlers.
- `sign_request(method, path, body_bytes, ts_ms) -> dict` —
  build the canonical header set:
  `{"X-Bedrock-Node": <node_name>, "X-Bedrock-Sig":
  <sig_b64>, "X-Bedrock-Ts": <ts_ms>}`. The canonical body is
  `f"{method} {path}\n{ts_ms}\n".encode() + body_bytes`.
- `verify_request(headers, body_bytes, *, max_age_ms=10_000)
  -> str | None` — server side. Looks up `nodes` row by
  `X-Bedrock-Node`, fetches `bedrock_pubkey`, calls `verify()`.
  Rejects with `None` if signature invalid or `ts_ms` too old
  (replay protection). Returns the node_name on success.

## Lifecycle

- `bedrock bootstrap` calls `ensure_keypair()` so the pubkey
  exists before `bedrock init/join`.
- `bedrock init`: the master's `mgmt_install.install_full`
  registers its own pubkey via `bs.node_register`.
- `bedrock join`: agent_install's `_register` POSTs its
  `pubkey_hex()`; on approval the master writes
  `nodes.bedrock_pubkey` for the joiner via `bs.node_register`.
- After join, every `/api/peer/*` call from this node carries
  signed headers; the receiver verifies against the registered
  pubkey.

## Why this layer exists

Without it, every inter-node API call would have to fall back
to SSH (which Bedrock uses for explicit operator-level
fan-outs like `bedrock node leave`'s "stop services on target"
step). SSH is heavyweight (TCP + handshake + paramiko); for
fast cluster queries (drbd status, mesh state, witness state)
HTTPS+Ed25519 is much cheaper and the rate-limit story is
clearer.
