# `witness.py`

**Module purpose.** Talk to a BedRock Echo device on the LAN over
UDP/12321. The Echo is a **passive per-node K/V slot store** — each
node owns one slot keyed by `node_id` (the last octet of its
`100.X.Y.N/32` loopback), writes its own slot every 1 s, and reads
every other node's slot from the Echo's reply. The Echo has NO
arbitration logic: it stores last-write per slot and returns all
slots on every reply.

See `docs/cluster-quorum-spec.md` for the load-bearing protocol —
this file is just the implementation reference. The takeover logic
that USES the slots lives in `installer/lib/cluster_arbiter.py`, not
here.

The wire protocol is intentionally tiny so an ESP32 firmware can
implement it:
```
b"BREC" || nonce(12) || ChaCha20-Poly1305(cluster_key, nonce, plaintext)
```
Anything that doesn't AEAD-verify is silently dropped, so multiple
Bedrock clusters can share an Echo on the same LAN without leaking.
Crypto: ChaCha20-Poly1305 with a 32-byte cluster_key from
`/etc/bedrock/cluster.key`, 12-byte nonces, 16-byte Poly1305 tags.

Module owns no I/O scheduling — the caller (netd's election tick)
drives `broadcast_probe`, `heartbeat_all`, `drain_replies`,
`set_own_slot` once per tick. State lives on a single `WitnessState`
dataclass passed in.

## Constants

- `WITNESS_PORT = 12321` — UDP port the Echo binds; cluster nodes
  broadcast probes here and unicast heartbeats to discovered
  endpoints here. Matches the canonical bedrock-echo firmware.
- `MAGIC = b"BREC"` — 4-byte packet prefix. Lets receivers drop
  garbage on the broadcast socket cheaply before invoking AEAD.
- `NONCE_LEN = 12` — ChaCha20-Poly1305 nonce length.
- `WITNESS_FRESHNESS_S = 12.0` — election treats the witness vote
  as valid only if a reply landed within the last 12 s.
- `DISCOVERY_REPROBE_S = 30.0` — re-broadcast probes at this
  cadence when every cached endpoint has gone stale.
- `SLOT_STALE_MS = 15_000` — reader's threshold for "slot is stale":
  `now_local_ms - slot.ts_writer_ms ≥ 15_000`. 14 missed ticks + 1
  grace.
- `NODE_ID_MIN = 1`, `NODE_ID_MAX = 250` — valid slot keys. 251–253
  reserved; 254 = arbiter VIP marker; 255 = broadcast; 0 unused.
- `TAG_LMS = 0x01` — bit 0 of the slot `tag` field, set when this
  node is operating last-man-standing.
- `MARKER_KIND_DRBD_ARBITER_UUID = 1` — `kind` value identifying a
  slot whose `marker` carries the tier-critical DRBD current-uuid.

## Dataclasses

- **`Slot(node_id, ts_writer_ms, tag, marker, kind, seen_at_monotonic)`**
  — a decoded slot. `lms` property checks `tag & TAG_LMS`.
  `is_stale(now_local_ms, threshold_ms)` compares the reader's
  clock against `ts_writer_ms`.
- **`EchoEndpoint(addr, echo_id, last_reply_ms)`** — one discovered
  Echo. `addr` is the `(ip, port)` tuple; `echo_id` is the Echo's
  self-identifier from the reply.
- **`WitnessState`** — held by `Daemon` (netd) for the duration of
  bedrock-d's run. Fields:
  - `cluster_uuid` (16-byte string used as msgpack `cu` value)
  - `cluster_key` (32-byte AEAD key)
  - `my_node_id` (1–250)
  - `my_node_name` (informational; not on the wire)
  - `sock` (non-blocking UDP socket)
  - `discovered: dict[str, EchoEndpoint]`
  - `last_probe_at`, `last_alive_at` (monotonic timestamps)
  - `slots: dict[node_id, Slot]` — wholesale-replaced cache from
    the most recent `drain_replies` pass.
  - `own_marker: bytes` / `own_kind: int` / `own_tag: int` —
    what THIS node will publish on its next heartbeat. Set by
    `set_own_slot()`.

## Functions

### Crypto helpers (internal)

- `_aead_seal(key, plaintext) -> bytes` — `nonce(12) ||
  ChaCha20-Poly1305(key, nonce, plaintext)`. Returns the
  concatenation; the 16-byte Poly1305 tag is appended to the
  ciphertext by the library.
- `_aead_open(key, blob) -> bytes | None` — inverse; returns
  plaintext or None on auth fail.
- `_encode_slot(ws, ts_writer_ms) -> bytes` — msgpack-encode the
  Slot plaintext, AEAD-seal with `ws.cluster_key`. Returns the
  opaque blob the witness will store.
- `_decode_slot(key, blob) -> Slot | None` — inverse. Validates
  `NODE_ID_MIN ≤ n ≤ NODE_ID_MAX`.
- `_encode_envelope(ws, *, t, include_own_slot) -> bytes` — build
  the on-wire envelope: `MAGIC || AEAD(msgpack({v, t, cu, n, slot?}))`.
- `_decode_envelope(key, data) -> dict | None` — inverse;
  validates MAGIC prefix, AEAD-decrypts, msgpack-decodes.

### Public API (called by netd's election tick)

- `open_socket() -> socket.socket` — non-blocking UDP socket bound
  to `("", WITNESS_PORT)` with `SO_REUSEADDR` and `SO_BROADCAST`.
- `broadcast_probe(ws, broadcast_addrs)` — sends a `t="probe"`
  envelope (no slot payload) to each broadcast address. Used at
  discovery and when every cached endpoint has gone stale.
- `heartbeat_all(ws)` — unicasts a `t="hb"` envelope (containing
  our own slot, AEAD-sealed) to every discovered Echo. No-op when
  `ws.discovered` is empty.
- `drain_replies(ws, max_packets=32)` — non-blocking recvfrom loop.
  For each accepted reply: upserts the `EchoEndpoint`, updates
  `last_alive_at`, decodes every slot blob in the reply, and
  wholesale-replaces `ws.slots`. Election tick reads from
  `ws.slots` after this call.
- `is_alive(ws) -> bool` — `True` iff `last_alive_at` is within
  `WITNESS_FRESHNESS_S`. The election uses this for the +1 witness
  vote.
- `needs_reprobe(ws) -> bool` — `True` if no endpoint has ever
  replied OR if the witness has gone stale (used as the toggle
  between `broadcast_probe` and `heartbeat_all`).
- `set_own_slot(ws, *, marker, tag=0, kind=MARKER_KIND_DRBD_ARBITER_UUID)`
  — update what this node publishes on its next heartbeat. Caller
  responsibility: every 1 s, set `marker = drbdadm current-uuid
  tier-critical` and `tag = TAG_LMS` iff hosting `.254` AND no peer
  is logged_up; else `tag = 0`. `heartbeat_all` picks these up on
  the next tick.
- `read_slot(ws, node_id) -> Slot | None` — most recent decoded
  Slot for `node_id`, or None.
- `own_slot(ws) -> Slot | None` — convenience for the takeover
  protocol's step-5 readback check (see
  `docs/cluster-quorum-spec.md`).
- `load_cluster_key(path) -> bytes` — reads `/etc/bedrock/cluster.key`
  (32 raw random bytes). Returns `b""` if unreadable; caller
  decides whether that's fatal. **Note**: does NOT `strip()` — 32
  random bytes have a ~5 % chance of starting/ending with a
  whitespace-coded byte; stripping silently corrupts the key.

## What this module is NOT responsible for

- **The takeover decision.** That lives in
  `cluster_arbiter.promote_to_arbiter_host()` per
  `cluster-quorum-spec.md`. This module only does I/O + caching.
- **"Blessing" a master.** The old `send_claim` / `blessed_master` /
  `holddown_ms` model is gone (see
  `cluster-quorum-spec.md#what-this-spec-replaces`). The witness
  has no notion of who's master.
- **Witness clock.** All freshness comparisons use the **reader's**
  local clock against the writer's `ts_writer_ms`. The witness
  doesn't generate timestamps.

## Failure modes

| Symptom                          | Where to look                                          |
|----------------------------------|--------------------------------------------------------|
| All slots show stale even fresh  | Reader clock skew vs writers — verify chronyd healthy. |
| No replies from Echo             | `WITNESS_PORT` reachable? Cluster_key mismatch (silent drop on AEAD fail)? |
| `ws.slots` empty after drain     | Cluster_uuid mismatch in reply — Echo serving multiple clusters; envelope.cu doesn't match. |
| Takeover loops on readback fail  | UDP loss; `cluster-quorum-spec.md` step 5 retries 3 ×; check for asymmetric routing. |
| Witness vote stays 0             | `is_alive` requires reply ≤ 12 s old; check `last_alive_at` in `/run/bedrock/witness.json` if dumped. |
