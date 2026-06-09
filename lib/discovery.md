# installer/lib/discovery.py

Cluster discovery for the LAN: with no prior knowledge, find Bedrock clusters a
joining node can reach. The primary mechanism is an mDNS multicast query; a
fallback subnet scan plus a set of known witness IPs cover networks where mDNS is
blocked. It returns reachable HTTPS endpoints the joiner uses to start its
handshake, plus identity (cluster name/uuid) parsed from responder TXT records.
Called by the `bedrock join` flow to pick a node to talk to. Pure stdlib — no
third-party deps.

## Functions / Classes

### `ClusterCandidate` (dataclass)
One Bedrock cluster found via discovery, identified by the LAN IP of whichever
node answered (any cluster member is a valid entry point).
- **Fields:** `ip` (responder's LAN IP); `cluster_uuid`, `cluster_name`,
  `node_name` (from the responder's TXT record; empty strings if the responder
  advertised no TXT).
- `label() -> str`: human-readable summary line, e.g.
  `at 10.0.0.5  name='prod'  uuid=ab12cd34  node=blade1`. The uuid is truncated
  to its first 8 chars; empty fields are omitted.

### `discover_clusters(timeout=2.0) -> list[ClusterCandidate]`
Send one multicast mDNS ANY query for `bedrock.local` and collect every response
within `timeout` seconds.
- **In:** `timeout` — seconds to keep listening after the query is sent.
- **Out:** deduplicated, sorted list of `ClusterCandidate`. Side effect: one UDP
  socket opened (multicast send to 224.0.0.251:5353, ephemeral bind for replies),
  closed before return. One candidate per `(ip, cluster_uuid)` pair, so a node
  advertising several A records appears once per IP.

### `first_reachable(candidates, port=8443, timeout=0.5) -> ClusterCandidate | None`
Return the first candidate whose `port` accepts a TCP connection.
- **In:** `candidates` (ordered list); `port` (default mgmt HTTPS 8443);
  `timeout` per connect attempt.
- **Out:** the first reachable candidate, or `None`. Side effect: one TCP
  connect-test per candidate until one answers. Used by `bedrock join --yes` to
  auto-pick among A records for the same cluster (e.g. LAN vs USB4 link-local).

### `find_witness() -> str | None`
Single-IP convenience entry point.
- **Out:** the IP of the first discovered cluster, else an IP found via the
  fallback paths, else `None`. Side effects: runs `discover_clusters()`; if that
  is empty, TCP/HTTP-probes the known witness IPs on 9443 (`/health`) and the
  first 50 hosts of the local /24 on 8443/8444 (`/cluster-info`).

### `query_cluster(host) -> dict | None`
Fetch cluster identity/membership from a host.
- **In:** `host` — IP or hostname.
- **Out:** parsed JSON dict, or `None` if every endpoint fails. Side effects:
  HTTP(S) GETs in order — HTTPS `:8443/cluster-info`, HTTP `:8444/cluster-info`,
  HTTP `:9443/cluster-info`, then HTTP `:9443/status` (from which it synthesises
  a `{cluster_name, cluster_uuid, nodes, witness_host}` dict).

### `register(witness, my_name, my_ip) -> bool`
POST a name/IP registration to a witness `:9443/register`.
- **In:** `witness` host; `my_name`, `my_ip` to register.
- **Out:** `True` on HTTP 200; also `True` on any exception (best-effort, never
  blocks the caller). Side effect: one HTTP POST. The actual join is driven by
  the `node_join` saga's `/api/join/request` handshake; this call is a
  convenience for callers that only need to announce a name/IP.

### Private helpers
- `_open(url, timeout=2.0)` — `urllib` open; HTTPS uses the insecure SSL context.
- `_can_reach(host, port, timeout=1.0)` — TCP connect test, bool.
- `_build_mdns_query(qtype=255)` — pack a multicast DNS query for `bedrock.local`.
- `_parse_txt_rdata(rdata)` — decode length-prefixed `key=value` TXT strings into a dict (RFC 6763 §6).
- `_read_name(buf, pos)` — read a DNS name, following one level of compression pointer.
- `_parse_mdns_response(buf)` — pull A + TXT records for our name out of a response.
- `_get_local_subnet_hosts()` — enumerate the /24 around `br0`'s IPv4 as host strings (via `ip -o -br addr show br0`).

## How it works

`discover_clusters` is the load-bearing path:

```
build mDNS ANY query for "bedrock.local"   (header ID=0 flags=0 QD=1; qtype 255; class IN)
        │
        ▼
UDP socket: SO_REUSEADDR, multicast TTL 4, bind ("", 0)  (ephemeral src port → replies)
        │  sendto 224.0.0.251:5353
        ▼
loop until monotonic() >= now + timeout, recvfrom(4096) with 0.3s socket timeout
        │
        ├─ _parse_mdns_response(data)
        │     ├ header: must have QR (response) bit set (flags & 0x8000), else drop
        │     ├ skip the question section (qd entries, +4 bytes each)
        │     └ for each answer RR whose name == b"bedrock.local":
        │          A   (type 1, rdlen 4) → inet_ntoa → append unique IP
        │          TXT (type 16)         → merge key=value pairs
        │     parsed → {ips: [...], txt: {...}}
        │
        └─ ips = parsed ips or [sender addr]   ;   txt = parsed txt or {}
             for ip in ips:
                 key = (ip, cluster_uuid)        # dedup
                 seen[key] = ClusterCandidate(ip, uuid, name, node_name)
```

Every received datagram is parsed; if the body doesn't match our name the sender
address still seeds a bare candidate (so an up-but-uninitialised node, which
answers with an A record and empty TXT, is still surfaced to the operator).
Dedup key is `(ip, cluster_uuid)` — a node on two NICs yields two candidates
sharing one uuid.

Results are sorted so the joiner can walk them top-down:

```
sort key = (ip.startswith("169.254.") , cluster_name , ip)
            └ False(0) first → LAN paths ahead of link-local, then name, then IP
```

`first_reachable` then TCP-tests each in that order and takes the first live one,
so a LAN address is preferred and a USB4-only link-local is used only when the
LAN address fails to connect.

`_read_name` handles DNS name compression with a single jump: on a pointer
(`len & 0xC0`) it records `end_pos` once (the position after the 2-byte pointer)
and follows the offset; the returned cursor is `end_pos`, so the caller keeps
walking past the pointer rather than into the jumped-to region. Malformed input
(truncated buffer, out-of-range length) returns `None`, which aborts parsing of
that response cleanly.

Fallback chain when mDNS yields nothing (`find_witness`):

```
discover_clusters()  ── non-empty ──▶ return candidates[0].ip
        │ empty
        ▼
known witness IPs (192.168.2.253/.252/.254) :9443  → TCP test → GET /health == 200
        │ none
        ▼
first 50 hosts of br0's /24, each on :8443 https then :8444 http → GET /cluster-info == 200
        │ none
        ▼
return None
```

`query_cluster` probes the same family of endpoints in descending preference
(8443 HTTPS, 8444 bootstrap HTTP, 9443 witness `/cluster-info`) and, as a final
attempt, derives cluster info from a witness `:9443/status` payload.

All HTTPS uses `_INSECURE_CTX` (`check_hostname=False`, `verify_mode=CERT_NONE`):
node certs are issued for a `<dashed-ip>.my.local-ip.co` name, not the bare IP
being scanned, so name verification can't succeed; the only data fetched is a
public `/cluster-info` JSON, so disabling verification carries no secret.

## Why
- Ephemeral source-port bind means responders reply straight back to this socket,
  so a single send collects the whole cluster's answers within `timeout`.
- mDNS multicast TTL is 4, scoped to the local segment(s) rather than a single
  hop, covering directly-bridged links without leaking off-LAN.
- 8444 is a dedicated cert-less bootstrap HTTP port (distinct from 8080, which is
  the SeaweedFS volume server), so a joiner can read `/cluster-info` before the
  first TLS cert exists.
