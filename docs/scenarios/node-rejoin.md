# Scenario: node rejoin after outage

A previously-registered node comes back after any outage (power, hardware,
planned maintenance). Rejoin needs zero operator steps beyond powering the node
on: systemd starts the daemons, DRBD re-converges, the node rejoins consensus
and is scraped again. No split-brain risk — a returning node does not reclaim
the mgmt-master role from a peer that took over during the outage (see
"Returning master does not steal back").

## Starting state

```
  Cluster topology lives in rqlite (the nodes, vms, drbd_resources,
  cluster_info tables), Raft-replicated across the live nodes. node1 and
  node3 hold quorum and carry node2's row.

  node2 is down. node1 and node3 are healthy and run the VMs node2 hosted —
  either failover completed, or node2 was DRBD-Secondary-only during the
  outage.
```

## Automated boot-up sequence

```
  T=0    node2 POST, kernel boot.
         │
  T+~15s systemd multi-user.target. Parallel auto-start units:
         │     bedrock-d        unified daemon: netd thread + mgmt/orch asyncio
         │     bedrock-rqlited  per-node rqlite — rejoins Raft
         │     bedrock-mdns     mDNS responder
         │     bedrock-redirect :80 → :8443
         │     node-exporter    :9100   (own unit, WantedBy=multi-user.target)
         │     vm-exporter      :9177   (own unit, WantedBy=multi-user.target)
         │     libvirtd         (no VMs auto-start — disks are DRBD)
         │     drbd kmod        loaded via /etc/modules-load.d/drbd.conf
         │     NetworkManager   br0 up; lo carries this node's /32
         │     chronyd          time sync
         │
  T+~20s  netd rejoins the mesh; rqlite reconnects to the quorum and
         │    catches up its local replica.
         │
         │    bedrock-d's boot_orchestrator (mgmt/orchestrator.py) waits for a
         │    clear role — a level='strong' read of mgmt_master from rqlite —
         │    then _start_local_services runs `drbdadm up` for the VM
         │    resources this node should host and starts libvirtd + those VMs.
         │    There are no DRBD systemd units; /dev/drbdN does not exist until
         │    boot_orchestrator brings it up, so nothing comes up before the
         │    role is settled (quorum-aware boot).
         │
         │    Each DRBD resource:
         │      - reads its external meta-disk for the last generation UUID
         │      - opens TCP to each peer's loopback /32 (port in 7700-7799)
         │      - handshake compares generations:
         │          self older  → resync as SyncTarget
         │          equal       → no resync, peer-disk=UpToDate
         │
  T+~25s  rqlite subscriber + orchestrator reactor on the master tick:
         │    node2's row reflects it online again. Dashboard: node2 dot
         │    turns green, memory/load tiles populate.
         │
  T+~25s  VictoriaMetrics next scrape (10 s cadence): node2:9100 and
         │    node2:9177 respond → up=1; node2 charts start filling in.
         │
  (async) DRBD partial resync continues until every resource is UpToDate —
          seconds for a short outage, minutes for a long one. VMs running on
          peers stay fully operational throughout; the Primary sees no I/O
          degradation.
```

## Operator perspective

Nothing to do. The dashboard shows:

1. node2 flips Offline → Online within ~20 s of boot.
2. VM tiles whose `backup_node` is node2 show node2's DRBD state again
   (Secondary, resyncing → UpToDate).
3. `/hosts`: node2's row fills in with load, memory, kernel, running-VM count.

## Returning master does not steal back

If node2 was the mgmt-master before the outage and a peer (say node1) took
over while it was down, node2 stays a follower on return. `cluster_arbiter`'s
takeover protocol (`installer/lib/cluster_arbiter.py` `_run_takeover_protocol`)
defers when a peer's fresh heartbeat advertises itself as master — it never
steals the role back from the live survivor. node2's `cluster` singleton DRBD
resource resyncs as Secondary, and the `.254` arbiter VIP stays on node1.

## node2 was never joined

Rejoin assumes node2 is already registered: its row exists in rqlite (`nodes`)
and its `/etc/bedrock/state.json` has a `cluster_uuid`. Then rejoin is just
"daemons start, rqlite catches up, talk to peers" — no re-registration.

To add node2 for the first time (or after its disks were zeroed), follow
[`join-cluster.md`](../actions/join-cluster.md), which runs the join saga and
registers the node in rqlite.

## DRBD stuck after boot

Symptom: `drbdadm status` on node2 shows resources stuck `Connecting`, never
reaching `Established`.

| Cause | Fix |
|---|---|
| Mesh NIC down / loopback /32 missing → no route to a peer | `nmcli con up br0`; check `ip addr show lo` has the cluster /32 |
| Peer host keys not in known_hosts (affects migration, often co-occurs) | `ssh-keyscan -H <peer-loopback-ip> >> /root/.ssh/known_hosts` on node2 |
| firewalld blocking 7700-7799 (it is disabled by bootstrap) | `systemctl disable --now firewalld` |
| Generation-UUID divergence beyond a partial resync = split-brain | see [`split-brain.md`](split-brain.md) |

## Mgmt-master node restart

If the restarted node held the mgmt-master role, one extra wrinkle: every
browser WebSocket dropped during the outage. On reconnect the `/ws` handler
sends each browser the cached `_last_state` immediately, then the 3 s push loop
resumes. If a peer took over the master role during the outage, this node comes
back as a follower and the dashboard is served from the new master at `:8443`.

VictoriaMetrics and VictoriaLogs data lives under `/opt/bedrock/data/`, so
metrics history is unbroken except for the downtime gap.

## Log lines

systemd journal as the node returns:

- `bedrock-d.service: Started Bedrock daemon`
- `bedrock-rqlited.service: Started` (rqlite rejoins Raft)
- kernel: `drbd vm-foo-disk0/0 <peer>: Connected` (replication re-established)
- bedrock-d: `boot: role=follower; starting local services`, then
  `services: drbdadm up …` / `services: virsh start …` for VMs this node hosts.

## Related

- [`power-loss-secondary.md`](power-loss-secondary.md) — outage of a Secondary.
- [`power-loss-all.md`](power-loss-all.md) — full-cluster variant.
- [`../actions/join-cluster.md`](../actions/join-cluster.md) — node is new, not
  rejoining.
