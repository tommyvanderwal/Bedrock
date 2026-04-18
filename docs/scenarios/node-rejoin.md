# Scenario: node rejoin after outage

A previously-registered cluster node comes back up after any outage
(power, hardware, planned maintenance). Bedrock is designed so this
requires zero manual steps beyond powering the node on — the services
come up from systemd, DRBD re-converges, exporters start being scraped.

## Starting state

```
  cluster.json on mgmt node:
    { nodes: { node1: {...}, node2: {...}, node3: {...} } }

  node2 is down (powered off, rebooting, whatever). node1 and node3 are
  healthy, running all VMs that were on node2's shoulders (failover
  completed or node2 was Secondary-only during the outage).
```

## Automated boot-up sequence

```
  T=0    node2 POST, kernel boot.
         │
  T+~15s systemd multi-user.target reached.
         │   Parallel starts:
         │     libvirtd         (no VMs auto-start because disks are DRBD)
         │     kmod-drbd9x      (loaded via /etc/modules-load.d/drbd.conf)
         │     NetworkManager   bedrock-drbd connection up → eth1 = 10.99.0.X
         │     chronyd          time sync
         │     node-exporter    :9100
         │     vm-exporter      :9177
         │
         │ (compute-only node — no bedrock-mgmt/vm/vl on node2)
         │
  T+~20s  drbd resources: implicit `drbdadm up all` if a DRBD service
         │    file is present; otherwise this is a manual step today.
         │    (Follow-up: ship a drbd-resources.service that enumerates
         │     /etc/drbd.d/*.res and runs `drbdadm up`.)
         │
         │    Each resource:
         │      - reads its external meta-disk for last generation UUID
         │      - opens TCP connection to each peer over 10.99.0.x
         │      - DRBD handshake: compares generations
         │      - if self is older → resync as SyncTarget
         │      - if equal      → no resync, peer-disk=UpToDate
         │
  T+~25s  mgmt node's state_push_loop tick:
         │    get_node_info("node2") now succeeds (SSH works again).
         │    Dashboard: node2 dot turns green, memory/load tiles populate.
         │
  T+~25s  VictoriaMetrics next scrape (≤ 10 s cadence):
         │    node2:9100 and node2:9177 respond → up=1
         │    Metrics charts for node2 start filling in.
         │
  (async) DRBD partial resync continues until all resources return to
          UpToDate. For short outages this is seconds; for long outages
          minutes. During resync the VMs running on peers remain fully
          operational; the Primary sees no I/O degradation.
```

## Operator perspective

Nothing to do. The dashboard shows:

1. node2 flips from Offline to Online within ~20 s of boot.
2. The VM tiles that had `backup_node=node2` now show an active peer in
   their DRBD state (no longer shows "waiting for peer").
3. Recent Logs: no explicit "node rejoined" entry today (future:
   `push_log` from state loop on transition).
4. `/hosts` table: node2 row fills in with load, memory, kernel,
   running VMs count.

## If node2 misses `bedrock join` previously

The rejoin flow assumes node2 was **already** registered in
`cluster.json`. That means `bedrock join` ran at some point in the
past, and node2's state.json has a cluster_uuid. A rejoin is just
"services start, talk to peers". No re-registration needed.

If the operator is adding node2 **for the first time** after its
disks were zeroed, follow [`join-cluster.md`](../actions/join-cluster.md)
instead — it re-runs exporters.install and re-registers with mgmt.

## If DRBD on node2 is stuck after boot

Symptom: `drbdadm status` on node2 shows all resources as
`Connecting` that never progresses to `Established`.

Likely causes:

| Cause | Fix |
|---|---|
| `bedrock-drbd` NM connection not up (eth1 not configured) | `nmcli con up bedrock-drbd` |
| Peer host keys not in known_hosts (for migration, not DRBD itself, but often co-occurs) | `ssh-keyscan -H <peer-drbd-ip> >> /root/.ssh/known_hosts` on node2 |
| Firewall blocking port 7000+minor (shouldn't — firewalld is off by bootstrap) | `systemctl stop firewalld; systemctl disable firewalld` |
| Generation UUID mismatch beyond simple partial resync (= split-brain) | See [`split-brain.md`](split-brain.md) |

## Mgmt node restart

If the node being restarted is the **mgmt node itself**, the sequence
has one extra wrinkle: every browser WebSocket was disconnected during
the outage. On reconnect, each browser gets fired `ws.on('cluster',
_last_state)` immediately (the WS handler sends the cached state on
accept). Then the normal 3 s push cycle resumes.

VictoriaMetrics and VictoriaLogs data persists across restarts
(`/opt/bedrock/data/`), so metrics history is unbroken except for the
gap during downtime.

## Log lines

Nothing explicit from Bedrock when a node comes back (today). The
systemd journal captures:

- `node-exporter.service: Started Prometheus node_exporter`
- `bedrock-mgmt.service: Started Bedrock Management Dashboard`
- kernel: `drbd vm-foo-disk0/0 <peer>: Connected` (replication
  re-established)
- VM journal (on mgmt): `static_configs: added targets: 0, removed
  targets: 0; total targets: 6` — config unchanged, existing targets
  responding again.

A future `push_log` from the state-push-loop transition detector would
make this visible in Recent Logs.

## Related

- [`power-loss-secondary.md`](power-loss-secondary.md) — what caused
  the outage in the first place.
- [`power-loss-all.md`](power-loss-all.md) — full-cluster variant.
- [`../actions/join-cluster.md`](../actions/join-cluster.md) — distinct
  case where the node is new, not rejoining.
