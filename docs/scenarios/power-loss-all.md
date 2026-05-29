# Scenario: all nodes power loss (site outage)

Every cluster node dies simultaneously — power outage, site-wide event.
No graceful shutdown, no orderly DRBD demotion. Recovery is about coming
back up without losing data or triggering a split-brain.

## State before

```
   node1 (P)   ═════   node2 (S)   ═════   node3 (S)
   UpToDate            UpToDate            UpToDate
                       [ALL POWER]
```

## What happens

Power cuts. All three kernels die in indeterminate order over milli-
seconds. DRBD on each node wrote its **activity log** to the external
meta-disk LV on every completed transaction, so on restart each node
knows exactly which extents *might* have been mid-write.

No data loss occurs for writes that were ACKed to the guest VM — those
were acknowledged by DRBD only after peer ACK arrived, so the peer has
them on disk. Writes in flight at the moment of outage are lost (same
as any power-cut disk).

## Boot-up sequence (hands-off)

1. **Power returns.** BIOS POST, boot.
2. **systemd starts** on each node. Order between nodes is not
   coordinated; whichever boots first starts trying to reach peers.
3. **Auto-start units come up at `multi-user.target`** on every node:
   `bedrock-d` (the unified daemon), `bedrock-rqlited` (per-node rqlite),
   `bedrock-mdns`, `bedrock-redirect`, plus `libvirtd`. libvirtd does
   **not** auto-start any VM — Bedrock leaves VM autostart off so a guest
   can't open a `/dev/drbdN` that isn't up yet.
4. **`kmod-drbd9x` loads** (via `/etc/modules-load.d/drbd.conf`). The
   DRBD units themselves stay disabled at boot (quorum-aware boot);
   `drbdadm up` is driven by bedrock-d once the cluster role is known.
5. **rqlite re-forms quorum.** Each node's `bedrock-rqlited` reconnects
   to its peers and re-establishes the Raft leader once a majority is
   back. Until then there is no quorum and no recorded mgmt-master.
6. **`bedrock-d`'s boot_orchestrator waits for a clear role.** It polls
   rqlite for a quorum-recorded `mgmt_master` (up to ~120 s). While the
   cluster is `noquorum`, it holds off — no DRBD promote, no VM start.
7. **`cluster_arbiter` brings up the singleton and `.254`.** Once a
   master is settled, the arbiter runs `drbdadm up`/`primary` on the
   `cluster` singleton resource, mounts `/var/lib/bedrock/cluster`,
   takes the `.254` VIP, and starts the arbiter rqlite + SeaweedFS
   filer/s3. The takeover protocol checks witness slots and does an
   exact generation-UUID match before promoting — no split-brain.
8. **boot_orchestrator brings up this node's VMs.** For each VM the
   rqlite `vms` table says belongs here, bedrock-d runs `drbdadm up` on
   its resource, starts libvirtd, then `virsh start`s the VM. No manual
   `drbdadm primary` is normally needed.
9. **SeaweedFS** comes back: the filer + its s3 gateway on `.254` are
   started by `cluster_arbiter` (step 7) once the singleton is mounted.
   The per-node volume + s3 (`:8080` / `:8333`, every node) and master
   (Raft-3 lowest-octet set) are restarted by `boot_orchestrator` in step 8
   via `seaweedfs.promote_to_master_volume_host` — these units are
   `WantedBy=` empty (role-aware, not blanket boot-enabled), so bedrock-d
   re-starts them on every boot rather than relying on a systemd symlink
   (`bedrock-weed-volume/-master/-s3.service`).
10. **VictoriaMetrics (:8428) / VictoriaLogs (:9428)** persist their data
    to `/opt/bedrock/data/{vm,vl}`, so history before the outage is
    preserved. The `node-exporter` (:9100) and `vm-exporter` (:9177) are
    their own `WantedBy=multi-user.target` units, so they auto-start at
    boot.

If quorum never returns (e.g. too few nodes power back on), boot_orchestrator
logs `boot: role=noquorum` and starts nothing local until enough nodes are
back; the `no_quorum_responder` then drives recovery once a majority returns.

## What the dashboard shows during recovery

The dashboard is served at `https://<node>:8443` from whichever node
holds the mgmt-master role. Assuming node1 ends up master and is among
the last to boot:

| T | Dashboard state |
|---|---|
| 0 (outage) | Browser WS disconnects, page falls back to cached state and shows 3 red dots. |
| +1 min | Operator visits `https://node1:8443` — "connection refused" until bedrock-d is back. |
| quorum re-forms | rqlite elects a leader and records a mgmt-master; dashboard becomes reachable on the master. |
| boot_orchestrator runs | Once role is settled, the arbiter brings up `.254` + the singleton; per-node VMs start. |
| nodes responsive | Green dots fill in as peers rejoin the mesh; VM tiles show each VM running on its assigned host. |

## If you need to recover by hand

The hands-off path above is the normal case. If quorum is genuinely
stuck (e.g. nodes diverged, or fewer than a majority will power on),
inspect and recover manually:

```bash
# 1. Check rqlite/cluster role on each node
for n in node1 node2 node3; do
  echo === $n ===
  ssh $n 'systemctl status bedrock-rqlited --no-pager; drbdadm status'
done

# Expected per node once stable: each resource Secondary/UpToDate until
# the arbiter/boot_orchestrator promotes the owner.

# 2. If quorum will not form (split-brain or too few nodes), see the
#    bedrock-rqlited recovery notes (single-node bootstrap) and
#    split-brain.md before forcing any DRBD primary.

# 3. Only as a last resort, promote + start a single VM at a time:
ssh <owner-host> 'drbdadm primary vm-foo-disk0'
ssh <owner-host> 'virsh start foo'

# 4. Repeat per VM, watching `drbdadm status` for UpToDate peers.
```

The authoritative record of which host ran which VM is the rqlite `vms`
table (read via `cluster_state.load_cluster()`), not a local file.

## Why recovery waits for quorum

A full-cluster outage cannot be distinguished from a network partition
(from the perspective of any single node) without a witness observing
from a different power domain. Automatic promotion before quorum would
risk split-brain in the partition case, so boot_orchestrator holds off
until rqlite re-forms a majority and a mgmt-master is recorded. The
BedRock Echo witness (UDP 12321, ChaCha20-Poly1305 AEAD) breaks ties for
partial outages (see [`power-loss-primary.md`](power-loss-primary.md)),
but a site-wide outage loses the witness too unless it's off-site —
which is why a true all-nodes-down event recovers via quorum, not the
witness.

## Data that survives the outage

- **DRBD resources**: intact on every node (external meta-disk). Writes
  ACKed to the guest VM are on at least 2 disks (pet) or 3 disks
  (vipet). In-flight writes lost as usual.
- **VM XML definitions**: `/etc/libvirt/qemu/*.xml` intact.
- **Cluster state**: topology lives in rqlite, whose data dir is on the
  `cluster` singleton DRBD resource (replicated, mounted at
  `/var/lib/bedrock/cluster`) — it survives the outage on the surviving
  disks. Per-node identity in `/etc/bedrock/state.json` is written
  crash-durably (fsync + atomic rename + dir fsync), so it survives a
  hard power cut intact.
- **Metrics + logs history**: `/opt/bedrock/data/{vm,vl}` intact.
- **DRBD activity log**: used on restart to recover any in-flight block
  ranges deterministically.

Nothing needs to be re-downloaded, re-installed, or re-configured.

## Cattle VMs

A cattle VM's disk survives the outage (the local thin LV is untouched),
but there is no replica. When the node comes back and quorum returns,
boot_orchestrator starts it like any other VM the `vms` table assigns to
this host — there's just no DRBD resource to bring up first. If the
node's storage was corrupted (failed disk, not just power cut), cattle
data is lost — which is the contract with the operator.

## Related

- [`power-loss-secondary.md`](power-loss-secondary.md) — single-node case.
- [`power-loss-primary.md`](power-loss-primary.md) — primary-only outage.
- [`node-rejoin.md`](node-rejoin.md) — bringing a single node back.
- [`split-brain.md`](split-brain.md) — if generation UUIDs diverged.
