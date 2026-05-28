# `netd.py`

**Module purpose.** The single Python daemon (`bedrock-net`) that
every Bedrock node runs. Three jobs in one process, one tick loop:

1. **Mesh discovery + routing.** Probes peers on every NIC, ranks
   paths by an EIGRP-style local metric (bandwidth + latency +
   age), installs `/32` host routes to peer loopback IPs so
   inter-node traffic (DRBD, libvirt, SeaweedFS, SSH) uses the
   best wire instead of falling back to the LAN router.
2. **Witness + 100/1 vote election.** Discovers BedRock Echo on the
   LAN, heartbeats it (each beat carries this node's own AEAD-sealed
   slot), AND exchanges a node-to-node **election heartbeat**
   (protocol 4 — distinct from the mesh discovery probe) once per
   second carrying `{believed_master, transitioning, arbiter_uuid,
   ack_target}`. Runs `lib.election.compute()` over its peer-liveness
   table + the per-peer ack map (derived from those heartbeats) + the
   rqlite snapshot, then acts: `set_mgmt_master(self)` on Leader-
   promote, `set_no_quorum_marker` + `cluster_arbiter.demote_arbiter_host()`
   on NoQuorum. Each tick `witness.set_own_slot(marker=drbdadm
   current-uuid)` refreshes the published marker; the LMS **tag** is
   NOT recomputed here — it is an explicit decision owned by
   `cluster_arbiter` (set on go-solo takeover, cleared on self-demote;
   never auto-flipped from a steady-state heuristic — Q-01/BAD-4).
   There is no "claim/bless" — see `docs/cluster-quorum-spec.md`.
3. **Leader-loss + self-demote (single 10-miss detector).** A
   candidate counts consecutive election ticks with no fresh
   heartbeat from the believed master; at `MASTER_LOSS_MISSES=10`
   (~10 s) it treats the master as gone and a quorate candidate
   promotes. An old master that has itself lost quorum self-demotes
   at `SELF_DEMOTE_MISSES=9` (~9 s, 1 s before survivors promote —
   INV-1 margin): it calls `cluster_arbiter.demote_arbiter_host()`
   so the master VIP + arbiter rqlite + filer/s3 come down BEFORE
   qemu can write stale data through the DRBD device.
   `cluster_arbiter.converge()` can't help here because rqlite is by
   definition unreachable in NoQuorum, so we bypass the rqlite-
   subscriber path. (Replaces the old `DOWN_HYSTERESIS_S`-driven
   election liveness + `NOQUORUM_HOLDDOWN_TICKS` streak + the disabled
   `LONE_MASTER_WATCHDOG_S`.)

Replaces the deleted `bedrock-rust` daemon. Reads `state.json` for
the cluster_uuid + node_name + loopback_ip; reads `cluster.json`
for the membership it should know about; reads `cluster.key`
(32-byte AEAD key) for witness ChaCha20-Poly1305 encryption.
Writes routes, `/run/bedrock-no-quorum`, and
`cluster_info.mgmt_master` in rqlite.

Higher-level mesh-design rationale (why a per-cluster CGNAT /24,
why RFC 3927 link-local for ARP-target binding, three protocols
one-job-each) lives in `docs/06-mesh-network.md`. This file is the
implementation reference.

## Constants (selected)

- `PROBE_GROUP = "239.7.7.7"` / `PROBE_PORT = 7732` — multicast
  group + port for cross-NIC discovery probes (protocol 1).
- `PROBE_INTERVAL = 1.0`, `TICK_INTERVAL = 0.25` — outer loop
  cadences.
- `ELECTION_INTERVAL_S = 1.0` — election tick rate; also the
  node-to-node election-heartbeat (protocol 4) send cadence.
- `HB_PORT = 7734` — unicast election-heartbeat port (complements
  `PROBE_PORT=7732` discovery + `ADV_PORT=7733` routing adv).
- `MASTER_LOSS_MISSES = 10` — consecutive missed election heartbeats
  from the believed master before a survivor treats it as gone
  (~10 s). The single leader-loss detector; also absorbs the
  first-second startup transient (neighbours=0 looks like NoQuorum).
- `SELF_DEMOTE_MISSES = 9` — an old master that has lost quorum
  self-demotes after this many NoQuorum ticks (~9 s), 1 s before
  survivors promote (INV-1 release-before-promote margin).
- `DOWN_HYSTERESIS_S = 10.0` — silent-this-long-before-LINK_DOWN.
  Drives mesh routing only; leader-loss is the `MASTER_LOSS_MISSES`
  heartbeat counter, not this knob.
- `UP_HYSTERESIS_S = 5.0` — link must be up this long before
  LINK_UP. Avoids declaring a flapping link "up" on every blip.
- `METRIC_DIRECT_BASE = 10`, `METRIC_TRANSIT_BASE = 100`,
  `METRIC_PANIC = 999` — route metric bands.

## Dataclasses

- `Neighbour` — one (peer_node, peer_nic, my_nic) tuple's state:
  link_addr, peer_loopback, rtt, rtt_var, last_seen, logged_up,
  blip counters.
- `Daemon` — process-wide state. Includes the neighbour dict,
  socket fds, route signature cache, ARP-defense cooldown table,
  ICMP pinger state, routing advertisement (path-vector) state,
  switch-neighbour table (CDP/MNDP), and `ever_seen_peers`
  (the persistent set used by the election layer so silenced peers
  still count for quorum after they age out of `d.neighbours`).

## Functions (grouped)

### Bootstrap + main loop

- `load_state() -> (cluster_key, cluster_uuid, my_node, my_loopback)`
  — reads `/etc/bedrock/state.json` + `cluster.key`. Returns
  `(b"", "", "", "")` if state isn't ready yet (pre-init/join).
- `ensure_loopback_ip(loopback_ip)` — idempotent `ip addr add
  <ip>/32 dev lo`; also sweeps stale `100.X.Y.Z/32`s from a
  previous cluster_uuid.
- `ensure_routing_sysctls()` — `net.ipv4.conf.all.rp_filter=2`,
  `ip_forward=1`, `fib_multipath_hash_policy=1`. Idempotent.
- `run_daemon()` — outer tick loop. Initialises witness state via
  `lib.witness.WitnessState`, opens probe sockets, polls
  receivers at every TICK_INTERVAL, sends probes at
  PROBE_INTERVAL, runs ICMP latency at ICMP_INTERVAL_S, runs
  advertisement at ADV_INTERVAL_S, runs the election tick at
  ELECTION_INTERVAL_S, emits routes when the route signature
  changes, prints a status line every 30 s.
- `tick(d, last_probe, last_route_emit)` — non-blocking drain of
  the multicast probe socket. Each accepted probe updates the
  matching Neighbour's last_seen (or creates one) and adds
  `peer_node` to `d.ever_seen_peers`.

### Probes + L2 discovery (protocol 1)

- `send_probes(d, now)` — broadcasts a probe on every local NIC,
  signed with `cluster_key`.
- `open_recv_socket() / open_adv_recv_socket() / open_adv_send_socket()`
  — socket setup helpers.
- `recv_with_ifindex(sock)` — `recvmsg + IP_PKTINFO` so we know
  which local NIC each multicast packet arrived on (kernel doesn't
  tell us otherwise when multiple NICs share the same /16).
- `l2disc_drain(d, now)` — passively listens for CDP/LLDP/MNDP
  on every NIC to populate `d.switch_neighbors`. Read-only, used
  for the status line.

### ICMP latency (protocol 2)

- `icmp_send_round(d, now_mono_ns)` — fires one ICMP echo per
  peer link, kernel-timestamped.
- `icmp_drain_replies(d, now_mono_ns)` — recv kernel-timestamped
  replies, computes per-link RTT, updates Neighbour's
  rolling-variance + blip counters.

### Routing advertisement (protocol 3, path-vector)

- `adv_send_round(d, now)` — broadcasts each direct neighbour's
  path-info to direct peers so they can install transit /32s.
- `adv_drain(d, now)` — receives others' adverts, populates
  `d.adv_table`.
- `recompute_best_transit_paths(d, now)` — collapses adv_table
  rows into a single best-next-hop per dest, used by
  `compute_routes` for transit /32s.

### Route emission

- `compute_routes(d) -> list[str]` — pure function over the live
  neighbour table + cluster.json. Emits three classes:
  1. Per-peer-link `/32` link-local routes (ARP-target binding so
     DRBD's per-link `host A address 169.254.x.y` resolves to the
     right physical NIC).
  2. Per-peer loopback `/32` host routes with multipath ECMP
     across all observed links to that peer. **Note**: iproute2
     requires `metric N` BEFORE the `nexthop` list — putting it
     after silently rejects the route. The current emit puts
     metric before nexthops.
  3. Transit `/32` for peers we haven't directly heard from but
     learned via another neighbour's advertisement.
  4. A `100.X.Y.0/24` panic catch-all at metric 999 via the
     master's best path (or freshest neighbour at bootstrap).
- `emit_routes(d)` — diff `compute_routes(d)` against
  `current_cluster_routes(d.cluster_uuid)` (which scans
  `ip -4 route show` and normalises the form to match). Logs
  `+N -M` summary on change. Captures + logs any `ip route
  replace` failure (previously these were silent).
- `current_cluster_routes(cluster_uuid) -> list[str]` — reads the
  kernel route table, filters to routes that bedrock-net owns
  (cluster CGNAT /24 prefix + 169.254.x.y scope-link /32s).
  Joins multipath continuation lines back into single-string form.
- `_normalize_route_line(line) -> str` — canonicalises `ip route
  show` output: adds /32 suffix to bare-IP destinations, drops
  kernel-added flags (`proto`, `src`, `linkdown`), and moves
  `metric N` to BEFORE the nexthop list so the diff against
  `compute_routes` round-trips cleanly.

### Election + witness

- `_election_tick(d, ws, witness_module, election_module,
  prev_outcome) -> str` — one election tick:
  1. Witness IO: re-probe if needed, else heartbeat known
     endpoints; drain replies.
  2. Build `peer_liveness` from `d.neighbours` (live state)
     overlaid on `d.ever_seen_peers` (silent-but-once-seen, used
     for quorum math).
  3. Read cluster.json for `node_loopbacks` + current
     `mgmt_master`.
  4. Call `lib.election.compute(...)`.
  5. Log transitions.
  6. Act on outcome: NoQuorum streak hold-down + write the
     no-quorum marker + `demote_arbiter_host` (once per cycle
     via `demoted_in_cycle` flag); Leader + `should_set_mgmt_master`
     → `bs.set_mgmt_master(self_name)` to rqlite.
  7. Update our own witness slot: `_read_tier_critical_uuid()`
     gives the current DRBD generation marker;
     `witness.set_own_slot(ws, marker=uuid, tag=TAG_LMS iff
     hosting-and-alone, kind=DRBD_ARBITER_UUID)` is queued for
     the NEXT `heartbeat_all` packet. The witness has no concept
     of master/claim; this just publishes this node's view.
- `_read_tier_critical_uuid() -> str` — parses `drbdadm dump-md
  tier-critical` for `current-uuid 0xABCDEF…`, returns the hex.
  Empty string when DRBD isn't up (N=1, pre-promote).
- `_run_silent_capture(cmd) -> (rc, stdout, stderr)` — helper.

### Self-demote on NoQuorum

The self-demote path is part of `_election_tick`'s NoQuorum branch.
Trigger: 5 consecutive NoQuorum ticks. Action: drop the no-quorum
marker + `cluster_arbiter.demote_arbiter_host()` once per cycle.
Reset: `demoted_in_cycle` flag clears on any non-NoQuorum tick.

### Misc

- `sweep_hysteresis(d)` — once per tick, ages Neighbour entries.
  Logs `down` event after `DOWN_HYSTERESIS_S` of silence and
  drops the entry from `d.neighbours`. The election layer still
  counts the peer for quorum via `d.ever_seen_peers`.
- `write_switch_state_file(d) / write_mesh_state_file(d)` —
  dumps the live tables to `/run/bedrock/*.json` for the dashboard
  and ad-hoc debugging.
- `i_am_mgmt_master(d) -> bool` — short cluster.json read for the
  status-line and panic-route logic.
- `_mgmt_master_loopback(my_node) -> (master_name, master_lo)` —
  cluster.json lookup for the panic-via-master route.

## Failure modes

| Symptom | Where to look |
|---------|---------------|
| ip route replace fails silently | `journalctl -u bedrock-net | grep emit_routes` — captures rc + stderr |
| Master flaps Leader↔NoQuorum | `SELF_DEMOTE_MISSES` NoQuorum counter + `demoted_in_cycle` flag |
| Self-demote too slow | `SELF_DEMOTE_MISSES` (~9 s) — counts NoQuorum ticks once compute() can't see a majority |
| Failover too slow / never fires | `MASTER_LOSS_MISSES` (~10 s); check peers' election heartbeats arrive on `HB_PORT=7734` |
| Joiner sees stale quorum count | `ever_seen_peers` not yet populated → joiner-grace ✓ |
| Witness vote not counted | check `lib/witness.is_alive()`; reply must be ≤12 s old |
| Slot writes silently dropped at Echo | AEAD verify-fail (wrong cluster_key) — Echo doesn't tell, packet is just gone |
| Own slot stale on every readback | `set_own_slot` not being called per tick; or `heartbeat_all` failing send |
| ECMP route rejected by iproute2 | `metric N` must come BEFORE first nexthop |
