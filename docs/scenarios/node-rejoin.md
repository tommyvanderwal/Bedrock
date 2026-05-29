# Scenario: node rejoin after outage

A previously-registered cluster node comes back up after any outage
(power, hardware, planned maintenance). Bedrock is designed so this
requires zero manual steps beyond powering the node on — the services
come up from systemd, DRBD re-converges, the node rejoins consensus and
starts being scraped again. There is no split-brain risk: a returning
node does not steal the mgmt-master role back from a peer that took over
during the outage (see "Returning master does not steal back" below).

## Starting state

```
  Cluster topology lives in rqlite (the `nodes`, `vms`, `drbd_resources`,
  `cluster_info` tables), Raft-replicated across the live nodes. node1
  and node3 still form quorum and carry node2's row.

  node2 is down (powered off, rebooting, whatever). node1 and node3 are
  healthy, running all VMs that were on node2's shoulders (failover
  completed or node2 was Secondary-only during the outage).
```

## Automated boot-up sequence

```
  T=0    node2 POST, kernel boot.
         │
  T+~15s systemd multi-user.target reached.
         │   Parallel starts (auto-start units):
         │     bedrock-d        (unified daemon: netd thread + mgmt/orch asyncio)
         │     bedrock-rqlited  (per-node rqlite — rejoins Raft)
         │     bedrock-mdns     (mDNS responder)
         │     bedrock-redirect (:80 → :8443)
         │     libvirtd         (no VMs auto-start because disks are DRBD)
         │     kmod-drbd9x      (loaded via /etc/modules-load.d/drbd.conf)
         │     NetworkManager   bedrock-drbd connection up → eth1 = 100.X.Y.Z
         │     chronyd          time sync
         │
         │ (bedrock-d runs on every node. node-exporter :9100 and
         │  vm-exporter :9177 are their own systemd units, installed by
         │  exporters.install() with WantedBy=multi-user.target, so they
         │  auto-start at boot alongside bedrock-d.)
         │
  T+~20s  bedrock-d's netd thread rejoins the mesh; rqlite reconnects to
         │    the quorum and catches up its local replica.
         │
         │    bedrock-d's boot_orchestrator waits for a clear cluster role
         │    (level='strong' read of mgmt_master from rqlite), then runs
         │    `drbdadm up` for the VM resources this node should host and
         │    starts libvirtd + those VMs. The DRBD units stay disabled at
         │    boot (quorum-aware boot), so nothing comes up before the role
         │    is settled.
         │
         │    Each DRBD resource:
         │      - reads its external meta-disk for last generation UUID
         │      - opens TCP connection to each peer over 100.X.Y.Z
         │      - DRBD handshake: compares generations
         │      - if self is older → resync as SyncTarget
         │      - if equal      → no resync, peer-disk=UpToDate
         │
  T+~25s  rqlite subscriber + orchestrator reactor on the master tick:
         │    node2's row reflects it as online again.
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
3. `/hosts` table: node2 row fills in with load, memory, kernel,
   running VMs count.

## Returning master does not steal back

If node2 *was* the mgmt-master before the outage and a peer (say node1)
took over while it was down, node2 does **not** reclaim the role on
return. `cluster_arbiter`'s takeover protocol defers when a peer is
already claiming master with a fresh heartbeat — it will not steal the
role back from the live survivor. node2 comes up as a follower, its
`cluster` singleton DRBD resource resyncs as Secondary, and the `.254`
arbiter stays on node1. (See `installer/lib/cluster_arbiter.py`
`_run_takeover_protocol`.)

## If node2 missed `bedrock join` previously

The rejoin flow assumes node2 was **already** registered — its row
exists in rqlite (`nodes` table) and its `/etc/bedrock/state.json` has a
cluster_uuid. A rejoin is just "services start, rqlite catches up, talk
to peers". No re-registration needed.

If the operator is adding node2 **for the first time** after its
disks were zeroed, follow [`join-cluster.md`](../actions/join-cluster.md)
instead — that runs the join saga and registers the node in rqlite.

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

## Mgmt-master node restart

If the node being restarted is the one that holds the **mgmt-master**
role, the sequence has one extra wrinkle: every browser WebSocket was
disconnected during the outage. On reconnect, each browser is fired the
cached cluster state immediately (the WS handler sends `_last_state` on
accept), then the normal push cycle resumes. Note that if a peer took
over the master role during the outage, this node comes back as a
follower and the dashboard is served from the new master at `:8443`.

VictoriaMetrics and VictoriaLogs data persists across restarts
(`/opt/bedrock/data/`), so metrics history is unbroken except for the
gap during downtime.

## Log lines

The systemd journal captures the node coming back:

- `bedrock-d.service: Started Bedrock daemon`
- `bedrock-rqlited.service: Started` (rqlite rejoins Raft)
- kernel: `drbd vm-foo-disk0/0 <peer>: Connected` (replication
  re-established)
- bedrock-d boot log: `boot: role=follower; starting local services`
  followed by `services: drbdadm up ...` / `services: virsh start ...`
  for any VMs this node should host.

## Related

- [`power-loss-secondary.md`](power-loss-secondary.md) — what caused
  the outage in the first place.
- [`power-loss-all.md`](power-loss-all.md) — full-cluster variant.
- [`../actions/join-cluster.md`](../actions/join-cluster.md) — distinct
  case where the node is new, not rejoining.
