# installer/lib/witness.py

BedRock witness client — the per-node side of the BedRock Echo K/V slot
protocol. Each node owns exactly one slot (keyed by its `node_id`, a single
byte 1-250), publishes that slot to every reachable Echo once per second, and
reads back every other node's slot to decide arbiter takeover. The Echo itself
is dumb storage: it keeps the last write per slot and returns all slots on every
reply. This module owns the wire format (two layers of ChaCha20-Poly1305 AEAD
over msgpack), the local slot cache, and the validity/confirmation predicates
that `netd`'s election tick folds into the vote tally. `netd` drives the I/O
(probe / heartbeat / drain / set-own-slot once per tick on a single
`WitnessState`); `election` consumes the predicates. See
`docs/cluster-quorum-spec.md` for the load-bearing protocol.

## Functions / Classes

### `Slot` (dataclass)
A decoded slot read back from a witness reply (plaintext).
- **Fields:** `node_id`, `ts_writer_ms` (writer's epoch-ms clock), `tag`
  (bitflag, bit 0 = LMS), `marker` (bytes; DRBD current-UUID for the arbiter
  slot), `kind` (default `MARKER_KIND_DRBD_ARBITER_UUID`), `seen_at_monotonic`.
- `.lms` → bool: tag bit 0 (`TAG_LMS`) set.
- `.is_stale(now_local_ms=None, threshold_ms=SLOT_STALE_MS)` → bool: true when
  `(reader's clock) - ts_writer_ms >= threshold`. The reader uses its own clock,
  not the writer's; defaults `now` to `time.time()*1000`.

### `EchoEndpoint` (dataclass)
One discovered Echo. Fields: `addr` (`(ip, port)`), `echo_id`, `last_reply_ms`,
a per-endpoint decoded slot cache `slots`, and `last_reply_monotonic`. The
per-endpoint cache is what `count_valid_confirmed` validates individually so
each configured witness can contribute to the tally.

### `WitnessState` (dataclass)
All live witness state for this node. Fields: `cluster_uuid` (msgpack `cu`
value), `cluster_key` (32-byte AEAD key), `my_node_id` (1-250), `my_node_name`
(informational, off-wire), `sock`, `discovered: dict[str, EchoEndpoint]`,
`last_probe_at`, `last_alive_at` (monotonic of most recent decryptable reply),
the merged latest-reply cache `slots: dict[int, Slot]`, the outgoing slot fields
`own_marker` / `own_kind` / `own_tag`, and `member_ids: Optional[set[int]]` (the
current active node set; `None` = membership not yet known).

### `open_socket() -> socket.socket`
Open the witness UDP socket.
- **Out:** an `AF_INET`/`SOCK_DGRAM` socket with `SO_BROADCAST` +
  `SO_REUSEADDR`, bound to `("", WITNESS_PORT)` (12321), non-blocking. Caller
  assigns it to `ws.sock`.

### `broadcast_probe(ws, broadcast_addrs) -> None`
Send a slot-less `probe` envelope to every broadcast address (discovery / re-discovery).
- **In:** `ws`; `broadcast_addrs` → iterable of broadcast IP strings.
- **Out:** None. Sends one UDP packet per address to `:WITNESS_PORT`; sets
  `ws.last_probe_at`. No-op if `ws.sock is None`. Per-send `OSError` is swallowed.

### `heartbeat_all(ws) -> None`
Unicast a `hb` envelope carrying this node's own slot to every discovered Echo. Called once per election tick.
- **In:** `ws`.
- **Out:** None. One UDP packet per discovered endpoint. No-op if no socket or
  nothing discovered. Per-send `OSError` swallowed. The slot is only attached
  when `ws.own_marker` is set.

### `drain_replies(ws, max_packets=32) -> None`
Non-blocking receive loop that absorbs `ack` replies and refreshes local state.
- **In:** `ws`; `max_packets` → loop bound per call.
- **Out:** None. Updates `ws.discovered` (adds/refreshes endpoints, keyed by
  `echo_id`), `ws.last_alive_at`, each endpoint's `slots` + `last_reply_monotonic`,
  and the merged `ws.slots`. Drops packets that fail AEAD, mismatch
  `cluster_uuid`, or aren't type `ack`; drops slots whose `node_id` isn't in
  `ws.member_ids` (when known) or whose decoded `node_id` disagrees with the
  map key.

### `is_alive(ws) -> bool`
Witness reachable iff a decryptable reply landed within `WITNESS_FRESHNESS_S`
(12 s). False before any reply (`last_alive_at == 0.0`).

### `is_valid(ws) -> bool`
True iff the merged cache holds a slot for every active member (`ws.member_ids`,
which must be known and non-empty). A missing member's slot ⇒ invalid ⇒ the
witness contributes 0 votes. Merged-cache (single-witness) view.

### `is_confirmed(ws, now_local_ms=None) -> bool`
Readback proof for this node's own takeover: true iff our own slot is present in
the merged cache, carries our current `own_marker`, and is not stale. The
predicate the takeover step-5 readback relies on, surfaced so the election can
fold it into the tally.

### `count_valid_confirmed(ws, n_configured, now_local_ms=None) -> int`
Multi-witness tally: how many configured Echoes are individually valid AND confirmed right now.
- **In:** `ws`; `n_configured` → number of configured witnesses (the cap);
  `now_local_ms` → staleness clock override.
- **Out:** int in `[0, n_configured]`. Iterates `ws.discovered`, skips endpoints
  whose last reply is older than `WITNESS_FRESHNESS_S`, validates each
  endpoint's own `slots` cache (`_slots_valid` + `_slots_confirmed`), and caps
  the result at `n_configured` so a rogue extra Echo can't inflate the vote.
  Returns 0 if `n_configured <= 0` or `member_ids` is unknown.

### `needs_reprobe(ws) -> bool`
Whether to re-broadcast a probe: ~1 s cadence while nothing is discovered, never
while `is_alive`, else every `DISCOVERY_REPROBE_S` (30 s) once cached endpoints
have gone stale.

### `set_own_slot(ws, *, marker, tag=0, kind=MARKER_KIND_DRBD_ARBITER_UUID) -> None`
Stage what this node publishes on its next heartbeat.
- **In:** `marker` → bytes (the `cluster` singleton's DRBD current-UUID);
  `tag` → set `TAG_LMS` when operating last-man-standing, else 0; `kind` →
  marker kind.
- **Out:** None. Mutates `ws.own_marker` / `ws.own_tag` / `ws.own_kind` (tag and
  kind masked to one byte). `heartbeat_all` picks them up on the next tick.

### `read_slot(ws, node_id) -> Optional[Slot]`
Most recent decoded `Slot` for `node_id` from the merged cache, or None.

### `own_slot(ws) -> Optional[Slot]`
This node's own slot from the merged cache (the takeover step-5 readback).

### `load_cluster_key(path=Path("/etc/bedrock/cluster.key")) -> bytes`
Read the shared 32-byte AEAD key from disk.
- **In:** `path` → key file.
- **Out:** raw key bytes, or `b""` on `OSError` (caller decides if fatal).
  Strips a single trailing `\n` only when the file is exactly 33 bytes; never
  `strip()`s, because ~5% of random keys start or end with a byte
  (`0x09`-`0x0D`/`0x20`) that `bytes.strip()` would eat.

### Constants
`WITNESS_PORT = 12321`, `MAGIC = b"BREC"`, `NONCE_LEN = 12`,
`WITNESS_FRESHNESS_S = 12.0`, `DISCOVERY_REPROBE_S = 30.0`,
`SLOT_STALE_MS = 10_000`, `NODE_ID_MIN = 1`, `NODE_ID_MAX = 250`,
`TAG_LMS = 0x01`, `MARKER_KIND_DRBD_ARBITER_UUID = 1`.

### Private helpers
- `_aead()` / `_msgpack()` — lazy imports (keep module import cheap).
- `_aead_seal(key, plaintext)` → `nonce(12) || ChaCha20Poly1305(...)`.
- `_aead_open(key, blob)` → plaintext or None on auth fail / blob shorter than
  `NONCE_LEN + 16`.
- `_encode_slot` / `_decode_slot` — seal/open one opaque slot blob (decode does
  bounds + `NODE_ID_MIN..MAX` checks).
- `_encode_envelope` / `_decode_envelope` — build/parse the outer wire envelope.
- `_slots_valid` / `_slots_confirmed` — validity / confirmation over a given
  slot cache; used by both the merged and per-endpoint predicates.

## How it works

Each tick `netd` calls `set_own_slot` (publishing the cluster DRBD UUID + LMS
bit), then `heartbeat_all` (unicast own slot to known Echoes), `drain_replies`
(absorb acks), and reads the predicates. Discovery is bootstrapped by
`broadcast_probe` whenever `needs_reprobe` says so.

Two layers of ChaCha20-Poly1305, both under the same `cluster_key`:

```
UDP packet:
  "BREC" || nonce(12) || AEAD( msgpack{ v,t,cu,n[,slot][,slots][,echo_id] } )
                                                  │            │
                          hb only ────────────────┘            └──── ack only
  slot / each slots[nid] value is itself an opaque blob:
      nonce(12) || AEAD( msgpack{ n, ts_writer, tag, marker, kind } )
```

The Echo never sees plaintext slots — it stores and echoes opaque blobs. All
decode happens here on read. Anything that doesn't AEAD-verify or whose `cu`
doesn't match is silently dropped, so multiple clusters can share one Echo on a
LAN without crosstalk.

Reply ingestion (`drain_replies`) is a chain of guards: AEAD must open → `cu`
must match our cluster → type must be `ack`. For each `slots[nid]` entry the
slot must decrypt, its decoded `node_id` must equal the map key, and (when
membership is known) `nid` must be a current member. The member filter is the
stuck-LMS escape: `bedrock node leave` drops a node from rqlite, so its stale
`lms=1` slot stops counting and a blocked takeover can proceed. The per-endpoint
cache and the merged `ws.slots` are swapped wholesale, so a reader always sees
one consistent reply, never a half-updated map.

Voting predicates compose as:

```
        is_alive ── decryptable reply within the 12 s freshness window
                          │
   valid ── slot present for EVERY active member (member_ids known, non-empty)
                          │
 confirmed ── our own slot present, carries current marker, not stale (readback)
                          │
   election counts a witness only when valid AND confirmed → +1
```

`is_valid` / `is_confirmed` read the merged cache (single-witness takeover path).
`count_valid_confirmed` validates each discovered Echo's own cache independently
and caps the result at `n_configured`. When `member_ids` is `None` (early boot,
before netd plumbs the rqlite `nodes` set) nothing is valid — every predicate
returns 0/false, which raises the vote bar and biases toward "do not fail over".

Staleness is reader-side: `Slot.is_stale` compares the reader's wall clock to the
writer's claimed `ts_writer_ms` against `SLOT_STALE_MS` (10 000 ms), matching
netd's 10-missed-heartbeat leader-loss detector. The LMS tag bit itself never
expires by time — only the member filter or a fresher slot clears it.

## Why

Two AEAD layers let the Echo be a trivial, untrusted store (small enough for
ESP32 firmware) while keeping slot contents confidential and authenticated
end-to-end between nodes. Reader-clock staleness avoids trusting a possibly-dead
writer's timestamp. The "unknown membership ⇒ 0 votes" stance ensures a node
that can't yet certify the witness never uses it to justify a takeover.
