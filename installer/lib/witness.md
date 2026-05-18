# `witness.py`

**Module purpose.** Talk to a BedRock Echo device on the LAN over
UDP/9501. The Echo is the cluster's external tie-breaker — its
heartbeat reply carries one extra vote that lets a 2-node split
decide which side promotes, and its "blessed master + DRBD-UUID"
record prevents a zombie ex-master from re-claiming primacy with
stale data.

The wire protocol is intentionally tiny so an ESP32 firmware can
implement it: msgpack body + HMAC-SHA256-truncated-16 signed with
the cluster's shared `/etc/bedrock/cluster.key`. Anything that
doesn't HMAC-verify is silently dropped, so multiple Bedrock
clusters can share an Echo on the same LAN without leaking.

Module owns no I/O scheduling — the caller (netd's election tick)
drives `broadcast_probe`, `heartbeat_all`, `drain_replies`,
`send_claim` once per tick. State lives on a single `WitnessState`
dataclass passed in.

## Constants

- `WITNESS_PORT = 9501` — UDP port the Echo binds; cluster nodes
  broadcast probes here and unicast heartbeats to discovered
  endpoints here.
- `MAGIC = b"BREC"` — 4-byte packet prefix. Lets us drop garbage
  on the broadcast socket cheaply before invoking msgpack.
- `WITNESS_FRESHNESS_S = 12.0` — election treats the witness vote
  as valid only if a reply landed within the last 12 s.
- `DISCOVERY_REPROBE_S = 30.0` — re-broadcast probes at this
  cadence when every cached endpoint has gone stale.

## Dataclasses

- `EchoEndpoint(addr, echo_id, last_reply_ms)` — one discovered
  Echo. `addr` is the `(ip, port)` tuple; `echo_id` is the Echo's
  self-identifier from the reply.
- `WitnessState` — held by `Daemon` (netd) for the duration of the
  bedrock-net run. Holds the UDP socket, the dict of discovered
  endpoints, the latest "blessed master + drbd_uuid + ts" the
  Echo reported, and the monotonic timestamp of the most recent
  successful reply.

## Functions

- `_pack(body, cluster_key) -> bytes` — internal. Computes
  HMAC-SHA256 over the canonical-sorted body without the `hmac`
  field, stuffs the 16-byte truncated tag back into `body["hmac"]`,
  msgpack-encodes, prepends `MAGIC`.
- `_unpack(data, cluster_key) -> dict | None` — inverse: rejects
  packets missing the MAGIC prefix, msgpack-decodes, recomputes
  the HMAC over the canonical body, and rejects mismatch. Returns
  the body dict on success.
- `open_socket() -> socket.socket` — opens a non-blocking UDP
  socket bound to `("", WITNESS_PORT)` with `SO_REUSEADDR` and
  `SO_BROADCAST` enabled.
- `_build_probe(ws) -> bytes` — packs a `{"t": "probe"}` message
  with the current node's name, the cluster UUID, an 8-byte nonce,
  and a millisecond timestamp.
- `_build_heartbeat(ws) -> bytes` — packs a `{"t": "hb"}` message
  with the same identity fields; sent unicast to already-discovered
  endpoints once per tick.
- `broadcast_probe(ws, broadcast_addrs)` — sends a freshly-built
  probe to each address in `broadcast_addrs` (typically
  `["255.255.255.255"]`). Updates `ws.last_probe_at`.
- `heartbeat_all(ws)` — sends a heartbeat to every discovered
  endpoint. No-op when `ws.discovered` is empty (caller should
  call `broadcast_probe` instead).
- `send_claim(ws, drbd_uuid)` — after this node has been elected
  Leader and successfully ran `cluster_arbiter.promote_to_arbiter_host`,
  publishes a `{"t": "claim", "drbd_uuid": <hex>}` to every
  discovered Echo. The Echo records `(master, drbd_uuid, ts)` and
  returns it on every subsequent reply for the holddown window.
  Returns True if at least one Echo was sent to.
- `drain_replies(ws, max_packets=32)` — non-blocking recvfrom
  loop, up to 32 packets per tick. For every accepted reply:
  upserts the `EchoEndpoint`, updates `ws.last_alive_at` (used by
  `is_alive`), copies `observed_peers` (the Echo's per-node
  last-seen-ms map), and overwrites `blessed_master /
  blessed_drbd_uuid / blessed_at_ms` if the reply carries them.
- `is_alive(ws) -> bool` — `True` iff `last_alive_at` is within
  `WITNESS_FRESHNESS_S`. The election uses this to add the +1
  witness vote to the total.
- `needs_reprobe(ws) -> bool` — `True` if no endpoint has ever
  replied OR if every cached endpoint has gone stale beyond
  `DISCOVERY_REPROBE_S`. Caller toggles between
  `broadcast_probe` and `heartbeat_all` based on this.
- `load_cluster_key(path) -> bytes` — reads `/etc/bedrock/cluster.key`
  (32 bytes). Returns `b""` if unreadable; caller decides whether
  that's fatal. (The election layer treats missing-key as "witness
  unavailable" — degrades gracefully to node-only quorum at N≥3.)
