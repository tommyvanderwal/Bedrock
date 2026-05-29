# installer/lib/mdns_responder.py

A tiny stdlib-only multicast-DNS responder that lets every cluster node answer
the name `bedrock.local`. It runs as its own service (`bedrock-mdns`) and is a
pure responder — it listens on UDP/5353, and when a query for `bedrock.local`
(type A, TXT, or ANY) arrives it replies unicast with this node's LAN IP and/or
a TXT record carrying the cluster identity. A fresh joiner uses this to find an
existing cluster on the LAN and tell different Bedrock clusters apart. The
identity TXT is sourced from `/etc/bedrock/state.json`.

## Functions / Classes

### `own_lan_ip() -> str`
Single-IP helper for callers outside the responder (e.g. log lines): the
address most likely to reach the wider network.
- **In:** none.
- **Out:** an IPv4 string. Prefers a non-link-local address; falls back to a
  link-local one; returns `"0.0.0.0"` if no usable address is found. Runs
  `ip -4 -o addr show` (via `_interface_ipv4_map`).

### `cluster_identity() -> dict`
Read this node's cluster identity from `/etc/bedrock/state.json`.
- **In:** none.
- **Out:** dict with keys `cluster_uuid`, `cluster_name`, `node_name`. Each is
  the string from state.json or `""` if the file is missing/incomplete/invalid.
  Reads the file only (no writes).

### `open_mdns_socket() -> socket.socket`
Create and configure the bound mDNS listening socket.
- **In:** none.
- **Out:** a UDP socket bound to `("", 5353)` with `SO_REUSEADDR` (and
  `SO_REUSEPORT` if available), joined to the multicast group `224.0.0.251`,
  multicast loopback disabled, and `IP_PKTINFO` enabled so `recvmsg` reports the
  receiving interface. The `SO_REUSEPORT` and `IP_PKTINFO` setsockopts are
  best-effort (silently skipped if unsupported).

### `read_qname(buf, pos) -> tuple[Optional[bytes], int]`
Decode a DNS QNAME from a wire buffer.
- **In:** `buf` raw query bytes; `pos` byte offset where the name starts.
- **Out:** `(name, next_pos)` with `name` lowercased and labels joined by `.`;
  or `(None, pos)` if the name is malformed or uses a compression pointer
  (high bits `0xC0` set) — compression isn't followed.

### `parse_question(query) -> tuple[Optional[int], Optional[int]]`
Inspect the first question of an mDNS query to see whether it asks about us.
- **In:** `query` raw query bytes.
- **Out:** `(qtype, qclass)` of the first question if its name is exactly
  `bedrock.local`; otherwise `(None, None)`. The QU bit (`0x8000`) is stripped
  from the returned qclass. Returns `(None, None)` on short/empty queries.

### `build_response(qtype, ip_bytes, identity) -> bytes`
Assemble the mDNS answer packet for `bedrock.local`.
- **In:** `qtype` the requested record type (A, TXT, or ANY); `ip_bytes` the
  4-byte packed IPv4 to put in the A record (may be `b""` to omit it);
  `identity` the dict from `cluster_identity()` used to build the TXT record.
- **Out:** the response packet bytes (header `QR=1, AA=1`, transaction ID 0,
  TTL 120, `CLASS_IN` with the cache-flush bit unset), or `b""` if no answer
  would be produced (e.g. an A-only request with no IP).

### `run() -> int`
The service entry point — the responder loop.
- **In:** none.
- **Out:** never returns under normal operation (infinite loop); the module's
  `__main__` passes its result to `sys.exit`. Side effects: opens the mDNS
  socket, prints status/change lines to stderr, periodically shells out to
  `ip -4 -o addr show` and reads `/etc/bedrock/state.json`, and sends unicast
  UDP reply packets to queriers.

Private helpers: `_interface_ipv4_map()` builds `{ifindex: ipv4}` for UP
interfaces (excluding `127.0.0.0/8` and cluster identity `100.x/32`, preferring
non-link-local per interface); `_parse_pktinfo(ancdata)` pulls `ipi_ifindex`
out of the `recvmsg` ancillary data; `_qname_wire()` encodes the label-prefixed
`bedrock.local`; `_txt_rdata(identity)` encodes the identity dict as
length-prefixed TXT strings (empty TXT becomes a single zero-length string).

## How it works

The whole point is that `bedrock.local` is a **shared** record: every node
answers it, so losing one node still leaves the alias reachable. The
cache-flush bit (`0x8000` in the answer's CLASS field) is the flag that would
mark the record *unique* and trigger conflict resolution — it is always left
unset (`CLASS_IN = 1`).

`run()` opens the socket, snapshots the interface→IP map and the cluster
identity, then loops on `recvmsg`:

```
recvmsg(data, ancdata)
        |
   parse_question(data)
        |  qtype is None?        ── drop (not bedrock.local)
        |  qclass != IN?         ── drop
        |  qtype not A/TXT/ANY?  ── drop
        v
   [every >60s] refresh iface_map + identity
        |   (only logs to stderr when they actually change)
        v
   ifidx = _parse_pktinfo(ancdata)        # which NIC received the query
   my_ip = iface_map[ifidx]  ──miss──►  own_lan_ip()   # kernel-preferred fallback
        |
   ip_bytes = inet_aton(my_ip)  (or b"" if "0.0.0.0")
        v
   build_response(qtype, ip_bytes, identity)
        |  empty?  ── drop
        v
   sock.sendto(resp, addr)        # unicast back to the querier
```

The interface-aware reply is the load-bearing detail: `IP_PKTINFO` tells the
loop which NIC the query landed on, so the A record carries *this node's address
on that same NIC* — an address the querier can actually route to. If the
ancillary data is missing or the receiving interface has no IPv4 the loop knows
about, it falls back to `own_lan_ip()`.

Failure handling is intentionally lenient: a failed `recvmsg` (`OSError`)
`continue`s the loop, a failed `sendto` is swallowed, and a non-matching or
malformed query is simply dropped. The 60-second refresh picks up DHCP renewals,
USB4 hot-plug, and post-init/post-join identity changes without restarting the
service.

The response packet layout:

```
header  : ID=0 | flags=0x8400 | QD=0 | AN=count | NS=0 | AR=0
A   rr  : bedrock.local | TYPE_A(1)   | CLASS_IN(1) | TTL=120 | rdlen=4 | <ip>
TXT rr  : bedrock.local | TYPE_TXT(16)| CLASS_IN(1) | TTL=120 | rdlen=N | <pairs>
```

TXT rdata is the non-empty `cluster_uuid` / `cluster_name` / `node_name` fields,
each encoded as a length-prefixed `key=value` string (truncated to 255 bytes);
if all are empty a single zero-length string is emitted, which signals "name
exists, no data" rather than "name doesn't exist".

## Why

Pure stdlib instead of Avahi: Avahi treats A-records as unique and would rename
all-but-one node to `bedrock-2.local` via conflict probes, defeating the goal of
a node-survivable alias. Replies are unicast (not multicast) because every
modern mDNS client accepts unicast answers and it avoids extra multicast chatter.
