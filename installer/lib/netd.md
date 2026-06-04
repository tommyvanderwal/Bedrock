# installer/lib/netd.py

The mesh-network daemon. It runs on every node and owns the layer between
"L2 cable is up" and "DRBD / libvirt / SeaweedFS can reach a peer's loopback IP".
It is started as a thread inside `bedrock-d` via `run_daemon(shared_state)` (it
also runs standalone with `run_daemon()` / `main()`). Concretely it: probes every
usable NIC to discover directly-cabled peers, measures per-link latency, gossips a
path-vector routing table, installs kernel routes so each peer's `/32` loopback
exits the right physical wire, and runs the cluster's weighted-vote election +
witness heartbeat that drives `cluster_arbiter` to promote/demote the `.254`
arbiter on master changes. It also passively records the switches each NIC is
plugged into (LLDP/CDP/MNDP).

The single source of truth for a node's identity is its `/32` loopback in
`100.64.0.0/10` (RFC 6598), set at init/join. Per-NIC IPs are `169.254/16`
link-local (RFC 3927). Four signed UDP protocols ride the mesh, all sharing one
`msgpack({v, body, sig})` HMAC-SHA256 wrap over `cluster.key`:

```
  proto 1  discovery     multicast 239.7.7.7:7732  "node X loopback Y reachable on link Z"
  proto 2  latency       ICMP echo (no UDP port)   smoothed per-neighbour RTT
  proto 3  advertisement unicast :7733             path-vector routes (BGP-shaped)
  proto 4  election HB   unicast :7734             believed master / transitioning / arbiter-UUID / ack
```

## Functions / Classes

### `encode_probe(cluster_uuid, node, nic, loopback, link_addr, ts, *, key) -> bytes` / `decode_probe(buf, *, key) -> dict | None`
Codec for the protocol-1 discovery probe.
- **In:** identity fields + `key` (32-byte cluster key). `decode` takes the raw datagram.
- **Out:** packed datagram / verified body dict. `decode` returns `None` (silently) on any MAC or schema failure.

### `encode_advertisement(*, cluster_uuid, advertiser, seq, ts, paths, key) -> bytes` / `decode_advertisement(buf, *, key) -> dict | None`
Codec for the protocol-3 routing advertisement. Same wrap layout as the probe.
- **Out:** `decode` requires `paths` to be a list; `None` on failure.

### `encode_heartbeat(*, cluster_uuid, node, ts, believed_master, transitioning, arbiter_uuid, ack_target, key) -> bytes` / `decode_heartbeat(buf, *, key) -> dict | None`
Codec for the protocol-4 election heartbeat carrying this node's election stance.
- **In:** `believed_master` (who sender follows, `""` if none), `transitioning` (sender claims master-to-be), `arbiter_uuid` (sender's `cluster`-singleton DRBD current-UUID), `ack_target` (candidate the sender votes for).

### `is_bridge_slave(nic) -> bool`
True if `nic` is enslaved to a bridge (`/sys/class/net/<nic>/master` symlink or `/brport`); such NICs are never addressed by the mesh.

### `list_interfaces() -> list[str]`
All up, non-blocklisted, non-bridge-slave NICs usable as mesh path endpoints.
- **Out:** sorted NIC names. Reads `/sys/class/net/*/operstate`.

### `get_mac(nic) -> str` / `nic_speed_mbps(nic) -> int` / `bucket_speed(mbps) -> int` / `bucket_rtt(us) -> int`
NIC attribute readers + coarse bucketers. `nic_speed_mbps` walks bridge slaves (min physical speed), reports 15000 for `thunderbolt-net` (kernel exposes no speed), else the `/sys` value (0 = unknown). `bucket_speed`/`bucket_rtt` round to coarse tiers so jitter doesn't perturb routing decisions.

### `first_inet_addr(nic) -> str`
First IPv4 on `nic`, preferring real (DHCP/static) over link-local; `""` if none.
- **Out:** dotted address. Shells out to `ip -4 -o addr show dev`.

### `ensure_link_local(nic) -> str`
Idempotently give `nic` an IPv4 if it has none.
- **Out:** the resulting address, or `""`. **Side effects:** if no IPv4 and `nmcli` exists, creates/up's a `bedrock-mesh-<nic>` NetworkManager profile with `ipv4.method=link-local` and waits up to ~15 s; if `nmcli` is absent, falls back to `ip addr add` of a MAC-hashed `169.254.x.y/16`.

### `Neighbour` (dataclass)
One per `(peer_node, peer_nic, my_nic)` — the discriminator is `my_nic`, so a peer seen on multiple of our NICs is multiple entries. Holds last/first-seen, bucketed speed, smoothed RTT + variance + outlier-streak + blip telemetry, and `logged_up` (this link has emitted a LINK_UP not yet superseded by LINK_DOWN).

### `Daemon` (dataclass)
The whole run-loop state: identity (`cluster_key`, `cluster_uuid`, `my_node`, `my_loopback`), the `neighbours` map, `ever_seen_peers`, per-protocol sockets, the advertisement table + computed `best_transit_paths`, election heartbeat state (`peer_hb`, `peer_acks`, `missed_master_beats`, the four `hb_*` fields we publish), L2-discovery socket maps + `switch_neighbors`, and a `stopped` flag.

### socket openers + `recv_with_ifindex` / `ifname_for_index` / `join_group_on` / `leave_group_on`
`open_send_socket(nic)` binds multicast egress to a NIC by index (packs `ip_mreqn` with `4s4sI`). `open_recv_socket()` binds `:7732`, enables `IP_PKTINFO`. `open_adv_*`/`open_hb_*` are plain non-blocking unicast sockets. `recv_with_ifindex(sock)` does `recvmsg` + parses `IP_PKTINFO` → `(data, sender_addr, ifindex)` or `(None,None,None)`. `join/leave_group_on` add/drop a NIC from the multicast group (idempotent).

### `load_state() -> (cluster_key, cluster_uuid, my_node, my_loopback)`
Read identity from `/etc/bedrock/cluster.key` (must be 32 bytes) and `/etc/bedrock/state.json` (loaded via `state.load_or_recover()`, self-healing from cluster.json on a truncated file).
- **Out:** the 4-tuple. Raises `RuntimeError` if the key/state files or `cluster_uuid` are missing.

### `ensure_routing_sysctls() -> None`
Idempotently set the routing sysctls the mesh depends on. **Side effects:** writes `/proc/sys/net/ipv4/...`: `fib_multipath_hash_policy=1` (L4 ECMP hashing), `conf.{all,default}.arp_ignore=1` + `arp_announce=2` (per-NIC ARP for shared-`/16` links), `conf.{all,default}.rp_filter=2` (loose RPF so asymmetric cross-NIC mesh traffic isn't dropped). Silent if `/proc` is read-only.

### `ensure_loopback_ip(loopback_ip) -> None`
Idempotently add our cluster `/32` to `lo` and delete any other stale `100.*/32` on `lo`. **Side effects:** `ip addr add/del ... dev lo`.

### `run_daemon(shared_state=None) -> None`
The main entry. Loads state (waiting in-loop when `shared_state` is given, since `bedrock-d` starts before `bedrock init`/`join`), assigns the loopback + sysctls, opens all sockets, builds `WitnessState`, then runs the tick loop until `stopped`/`stop_event`. Publishes the live `Daemon` + witness state + last election outcome onto `shared_state` so in-process FastAPI/orchestrator readers see them without re-reading `/run` files.

### `tick(d, last_probe, last_route_emit) -> None`
One ~250 ms loop iteration: drains probes, refreshes the loopback from rqlite if not yet set, brings new NICs up (assign LL, join group, open send/LLDP/CDP sockets) and tears down vanished ones, then runs `sweep_hysteresis`.

### `send_probes(d, now)` / `process_probe(d, body, sender_link_addr, my_nic_hint="")`
Multicast one signed probe out each NIC / record an incoming probe as a `Neighbour` (upsert keyed by `(peer_node, peer_nic, my_nic)`). `process_probe` does not emit log events — the hysteresis sweep decides.

### `nic_for_sender` / `_nic_in_subnet` / `_peer_in_local_subnet` (helpers)
Map an incoming source IP to a local NIC by shared subnet (the fallback when `IP_PKTINFO` is missing); `_peer_in_local_subnet` decides whether a peer is on our LAN `/24` (so we skip a redundant `/32`).

### `arp_force_renumber(target_addr, dev) -> None`
RFC 3927 §2.5 countermeasure for a cross-segment link-local collision. **Side effects:** opens an `AF_PACKET` raw socket and broadcasts 3 gratuitous ARP announcements (op=reply, our MAC, sender_ip==target) 0.5 s apart on `dev`, forcing the colliding peer to renumber.

### `_detect_and_handle_ll_collision(routes, seen, n, d) -> bool` (helper)
Per-sweep dedup + collision classifier: first sighting appends the `/32` and records it; same peer-interface on another of our NICs = benign L2 bridge merge (skip, log); same address from a *different* peer interface on a different NIC = real collision → fire `arp_force_renumber` (30 s per-`(addr,nic)` cooldown).

### `sweep_hysteresis(d) -> None`
Walk every neighbour and emit LINK_UP / LINK_DOWN / LINK_QUALITY at the hysteresis thresholds; drop silent entries. Flips `logged_up` and adds the peer to `ever_seen_peers` on first sustained up. (Detail below.)

### ICMP latency (protocol 2)
- `icmp_checksum` / `build_icmp_echo` / `parse_icmp_reply_seq` — packet helpers (unprivileged `SOCK_DGRAM`/`IPPROTO_ICMP`; seq survives the kernel's identifier remap, so it keys pending sends).
- `IcmpPinger` (dataclass) — one per local NIC: a non-blocking ICMP socket + seq counter + `pending` map.
- `_ensure_icmp_socket(d, my_nic)` — lazily opens/binds the per-NIC socket; `None` if unprivileged ICMP is disallowed.
- `icmp_send_round(d, now_mono_ns)` — one echo per logged-up neighbour via its NIC. **Side effects:** `sendto`, stashes send-time in `pending`.
- `icmp_drain_replies(d, now_mono_ns)` — match replies by seq, feed `_update_neighbour_rtt`, expire timed-out pendings.
- `_update_neighbour_rtt(d, neigh_key, sample_us)` — RFC 6298 EWMA with pre-smoothing outlier rejection; counts rejected samples as "blips" (telemetry + rate-limited journal line).

### Routing advertisement (protocol 3)
- `_direct_neighbour_by_node(d)` — best (lowest `local_metric`) logged-up neighbour per peer.
- `_cluster_node_loopbacks(my_node)` — `{node: loopback_ip}` from rqlite `nodes` (level `none`); membership-of-record, used only to *address* advertisements, never to make routing decisions.
- `build_advertisement_paths(d)` — the `paths[]` list: one direct entry per logged-up peer + one per selected transit dest (prepending us to its `via_chain`); direct beats transit. The mgmt master also appends the cluster VIP as a `@vip` entry (connected: `via_chain=[me]`, `∞` bw, `0` lat) so `.254` propagates as an ordinary `/32` — no extra packet. Withdrawn by simply not emitting it on demote.
- `adv_send_round(d, now_ts)` — one signed unicast per known peer (direct neighbours ∪ rqlite nodes); kernel picks the NIC. **Side effects:** `sendto`.
- `adv_drain(d, now_ts) -> bool` — ingest advertisements; True if `adv_table` changed.
- `process_advertisement(d, body, sender_addr, now_ts) -> bool` — validate (advertiser must be a direct neighbour; seq must advance, wrap-aware) + store.
- `recompute_best_transit_paths(d, now_ts)` — per destination, pick the lowest-metric loop-free path through a direct neighbour; composed `bw=min(adv_bw, my_bw)`, `lat=adv_lat+my_rtt`. Stale advertisements (`> ADV_STALE_S`) are skipped.

### Election heartbeat (protocol 4)
- `hb_send_round(d, now_ts)` — one signed heartbeat per known peer carrying the `d.hb_*` stance.
- `hb_drain(d)` — ingest peers' heartbeats into `d.peer_hb` (with monotonic receive time).
- `_failover_ack_target(d, node_loopbacks, peer_liveness) -> str` — once the master is lost, choose the lowest-loopback-octet candidate (self + reachable peers advertising `transitioning`) whose advertised arbiter-UUID is eligible (`state.is_uuid_eligible`); `""` if none (abstain → stay NoQuorum).
- `_election_tick(d, ws, witness, election, prev_outcome) -> str` — the per-second election. (Detail below.)
- `_read_cluster_uuid() -> str` — the `cluster` resource's DRBD current-UUID from debugfs (`data_gen_id`), falling back to `drbdadm dump-md`; `""` if unattached.

### L2 neighbour discovery (LLDP / CDP / MNDP)
- `l2disc_drain(d, now_ts) -> bool` — drain per-NIC LLDP/CDP sockets + the shared MNDP socket, decode via `l2disc`, update `switch_neighbors`; True on a new/swapped chassis.
- `_record_switch(d, nic, info, now_ts) -> bool` — upsert the `(nic, protocol)` entry; emit a `NIC_SWITCH` journal line on first-seen / chassis swap / daily refresh.
- `_emit_nic_switch_log(nic, entry, *, reason)` — structured `key=value` journal line for LogsQL.
- `write_switch_state_file(d)` / `write_mesh_state_file(d)` — atomic (tmp+rename) writes of `/run/bedrock/switch_neighbors.json` and `/run/bedrock/mesh_neighbors.json`; per-node local files the mgmt master scrapes for the topology view (never replicated).

### `local_metric(bw_mbps, latency_us, loss_rate=0.0, age_s=1e9) -> int`
EIGRP-style composite path cost (lower is better): `1_000_000/Mbps` bandwidth term + `max(0, us-1000)/100` latency term (floored below 1 ms) + `+50` flap penalty (`age_s < 60`) + graded loss penalty. The single ranking function for every routing decision.

### `i_am_mgmt_master(d) -> bool`
True if `state.json["role"]` contains `mgmt`. Gates the single-writer log discipline.

### `emit_link_event(kind, d, n, reason="") -> bool`
Persist a LINK_UP/DOWN/QUALITY observation. **Side effects:** the mgmt master writes the `paths` row via `bedrock_state.link_up/link_down/link_quality`; a follower writes nothing and returns `True` immediately (so its local hysteresis advances). Returns `False` only on a master-side rqlite error (caller retries next sweep).

### `emit_routes(d) -> None`
Compute desired routes (`compute_routes`), diff against the kernel's current cluster routes (`current_cluster_routes`), and apply only the delta. **Side effects:** `ip route del` for removals, `ip route replace` for additions; caches a signature so an unchanged set is a no-op.

### `compute_routes(d) -> list[str]`
Build the full route spec list. (Detail below.) Calls `_detect_and_handle_ll_collision`, `local_metric`, `_cluster_node_loopbacks`, `_loopback_octet`, `cluster_addr.cluster_loopback_net`, `cluster_addr.cluster_vip`. **Reads nothing from rqlite** — fully master-independent. The cluster VIP (`.254`) is installed from the `@vip` advertised `/32` (see `build_advertisement_paths`), and the `/24` panic catch-all points at the lowest-octet lower-than-self neighbour (loop-free; the global-lowest node installs none and sinks). See `docs/vip-route-decoupling.md`.

### `_loopback_octet(loopback_ip) -> int`
Last octet of a cluster loopback `/32` (== `node_index`, 1..254), or `0` if unparseable. The node's stable rank in the lowest-octet catch-all total order.

### `current_cluster_routes(cluster_uuid) -> list[str]` / `_normalize_route_line(line) -> str`
Read the kernel routes this daemon owns (cluster `/24`, its `/32`s, and `169.254.*/32 scope link`), joining multipath continuation lines. `_normalize_route_line` rewrites `ip route show` output into the exact form `compute_routes` emits (add `/32`, strip `proto`/`pref`/`src`/`table`/`linkdown`/`onlink`, move `metric` before the nexthop list) so set-diffs round-trip without churn.

### `run_silent(cmd)` / `_run_silent_capture(cmd)` / `main()`
`subprocess.run` wrappers (rc, or rc+stdout+stderr). `main()` runs `run_daemon()` standalone and exits 1 on `RuntimeError`.

## How it works

**Main loop (~250 ms tick).** `run_daemon` opens sockets and loops calling `tick`,
then runs each protocol on its own cadence off a wall-clock comparison:

```
  every tick (0.25s)  drain probes/ICMP/adv/HB/L2, reconcile NIC set, sweep hysteresis
  1.0s                send probes
  2.0s                ICMP round; advertisement round; recompute transit paths
  1.0s                emit routes
  5.0s                write /run state files
  1.0s                election tick + send our heartbeat
  30s                 status line to journal
```

**Discovery → hysteresis.** Probes upsert `Neighbour`s but never log. `sweep_hysteresis`
is the state machine that gates durable events:

```
  first probe ──► neighbour exists, logged_up=False
                  │  (continuously seen ≥ UP_HYSTERESIS_S = 5s)
                  ▼
                LINK_UP  ── logged_up=True, peer added to ever_seen_peers
                  │  (silent > DOWN_HYSTERESIS_S = 10s)        │ (speed bucket
                  ▼                                            │  changed ≥25%,
                LINK_DOWN ── entry dropped                     ▼  ≥QUALITY_REFRESH_S=60s)
                                                            LINK_QUALITY
```

`logged_up` is set locally regardless of whether the rqlite write succeeds —
local routing must never wait on the rqlite leader (which needs reachable peers,
which need routes). The `paths` table is written only by the mgmt master
(`emit_link_event` → `i_am_mgmt_master` guard); followers short-circuit to keep
the log single-writer, and the master's reciprocal observation records the
follower's side.

**Why `ever_seen_peers` is gated on `logged_up`, not first probe:** counting a
one-way probe sender toward quorum would jump `n_nodes` to 2 before the 5 s
handshake completes, dropping a fresh daemon into NoQuorum on every restart. A
peer counts for quorum only after it has reached `logged_up` at least once, then
persists even if its link later goes silent.

**Routing.** `compute_routes` emits three route classes, then a catch-all:

```
  1. link /32s     169.254.X.Y/32 dev <my_nic> scope link
                   (per peer-link ARP target; longest-prefix wins over the
                    auto /16; collisions handled here)
  2. loopback /32s 100.A.B.C/32 via 169.254.X.Y dev <my_nic> metric 10+i
                   (per peer, one tier per tied local_metric; tied paths
                    become an ECMP multipath route, kernel L4-hashes flows)
  3. transit /32s  100.D.E.F/32 via <next-hop> dev <my_nic> metric 100+i
                   (peers not directly reachable, learned via protocol 3;
                    includes the .254 VIP via the @vip advertisement)
  ──────────────────────────────────────────────────────────────────────
  panic catch-all  100.A.B.0/24 via <lowest-octet lower-than-self nbr> 999
                   (master-independent, no rqlite; loop-free by octet
                    well-ordering; the global-lowest node installs none
                    and sinks unknown traffic)
```

Monotonic metrics let the kernel fail a peer's traffic over to a backup path for
free on link-down. `emit_routes` only writes the diff against `current_cluster_routes`,
which is scoped strictly to this cluster's prefix (derived from `cluster_uuid`) so
operator routes are never touched.

**Election tick (1 Hz, `_election_tick`).** Ordering matters:

```
  1. witness IO (probe/heartbeat/drain) — best-effort
  2. peer_liveness: seed every ever-seen peer False, set True per logged_up link
  3. load cluster snapshot from local rqlite (level none); active-nodes-only
     denominator; count valid+confirmed witnesses; read+record our arbiter UUID
  3c. missed_master_beats: ++ each tick with no fresh HB from the believed
      master; a fresh HB (within ~1.5 ticks) keeps the master "alive" so a
      brief mesh flap never demotes it
  3d. peer_acks: a peer acks us iff its fresh ack_target == my_node
  4. election.compute(...) → LEADER / FOLLOWER / NO_QUORUM
  4b. publish our own HB stance (believed_master / transitioning / ack_target)
  5. log transitions; persist believed-master (cold-boot readable)
  6. act on outcome
```

The single leader-loss detector is the missed-beat counter:

```
  master gone ──► survivor promotes at MASTER_LOSS_MISSES = 10 (~10s)
                  isolated old master self-demotes at SELF_DEMOTE_MISSES = 9 (~9s)
                  └─ releases .254 / arbiter rqlite 1s BEFORE a survivor takes it,
                     so the arbiter VIP is never live on two nodes at once
```

On **LEADER**, netd drives `cluster_arbiter.promote_to_arbiter_host()` (which
brings up DRBD primary + `.254` + arbiter rqlite + filer using witness + local
commands only — no rqlite on that path — then writes `mgmt_master` as a result)
and `ensure_lms_if_last_standing(ws)`. netd never writes `mgmt_master` itself.
On **NO_QUORUM**, after the self-demote streak it sets the no-quorum marker and,
once per episode, demotes the singletons it was hosting. Each tick netd refreshes
the witness slot marker (the current DRBD UUID) but never flips the LMS tag —
that bit is owned solely by `cluster_arbiter`.

## Why

The mesh keeps cluster identity on a loopback `/32` (not a NIC IP) so changing or
losing a cable never moves a node's address — the kernel just re-routes the `/32`
out a surviving wire. Link-local `169.254/16` for per-NIC addresses uses an
IANA-reserved block, so an operator's real LAN can never collide with mesh
addressing. A witness counts toward quorum only when reachable *and* reflecting
our write, which raises the bar and biases toward "don't fail over" — safety over
availability for a 2-node split.
