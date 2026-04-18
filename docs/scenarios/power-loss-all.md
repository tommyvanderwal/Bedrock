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
3. **`libvirtd` starts**. Any VM with `<on_crash>restart</on_crash>` in
   its XML attempts to start — but the disk is `/dev/drbdN` which is
   **not yet up**, so `virsh start` fails with "Cannot open backing file".
4. **`kmod-drbd9x` loads** (via `/etc/modules-load.d/drbd.conf`).
5. **`drbdadm up <all resources>`** runs (typically from
   `drbd.service` or manually). Each resource reads its activity log,
   determines its generation UUID, and starts trying to connect to its
   configured peers.
6. **Two or more peers reach each other.** DRBD compares generation
   UUIDs — all three are the same (they all had the same state at
   power cut). No split-brain.
7. **No auto-promote.** DRBD9 does not automatically elect a Primary
   after a cluster-wide outage; all resources come up Secondary.
8. **Operator (or orchestrator) promotes.** The node that was last
   Primary is preferred because it holds the most recent activity log,
   but any node with `UpToDate/UpToDate/UpToDate` can become Primary:

```bash
# on the node that should host the VM:
drbdadm primary <resource>
virsh start <vm>
```

9. **Dashboard mgmt service starts** on whichever node carries the mgmt
   role (mgmt+compute by default = node1 unless the operator promoted
   the role elsewhere; see HA follow-up in the plan).
10. **VictoriaMetrics / VictoriaLogs / exporters** all auto-start from
    systemd. Historical data before the outage is preserved (they
    persist to `/opt/bedrock/data/{vm,vl}`).

## What the dashboard shows during recovery

Assuming the mgmt node is node1 and it's among the last to boot:

| T | Dashboard state |
|---|---|
| 0 (outage) | Browser WS disconnects, page falls back to cached state and shows 3 red dots. |
| +1 min | Operator visits `http://node1:8080` — returns "connection refused" until mgmt comes back. |
| node1 systemd ready | Dashboard reloads; `_last_state` seeded from `cluster.json` — sidebar shows 3 hosts, all Offline. |
| first state push loop tick | SSH succeeds to self (node1); node1 goes green. Others still red (may be booting or unreachable). |
| node2/node3 responsive | Green dots fill in; VM tile shows `running_on=None` (all Secondaries). |
| operator promotes + `virsh start` | Next state push shows VM running on the chosen node. |

## Manual recovery walkthrough

After a full outage, follow this order:

```bash
# 1. Verify all nodes are up and DRBD resources are Secondary/UpToDate everywhere
for n in node1 node2 node3; do
  echo === $n ===
  ssh $n 'drbdadm status'
done

# Expected output per node:
#   vm-foo-disk0 role:Secondary
#     disk:UpToDate open:no
#     peer-node role:Secondary peer-disk:UpToDate
#     ...

# 2. Choose the Primary. Default: the node that held the VM before the outage
#    (inspect `cluster.json` if unclear about which host was running what —
#     mgmt kept that as vm.running_on at the last push before outage).

# 3. Promote one resource + start one VM at a time
ssh <primary-host> 'drbdadm primary vm-foo-disk0'
ssh <primary-host> 'virsh start foo'

# 4. Watch Recent Logs for:
#    "VM foo started on <primary-host>"   (via push_log)

# 5. Repeat for each VM.
```

## Why no auto-recovery for this case

A full-cluster outage cannot be distinguished from a network partition
(from the perspective of any single node) without a witness observing
from a different power domain. Automatic promotion would risk split-brain
in the partition case. The witness solves this for partial outages (see
[`power-loss-primary.md`](power-loss-primary.md)), but a site-wide
outage loses the witness too unless it's off-site. Manual operator
intervention is the correct conservative default.

## Data that survives the outage

- **DRBD resources**: intact on every node (external meta-disk). Writes
  ACKed to the guest VM are on at least 2 disks (pet) or 3 disks
  (ViPet). In-flight writes lost as usual.
- **VM XML definitions**: `/etc/libvirt/qemu/*.xml` intact.
- **Cluster state**: `/etc/bedrock/cluster.json` intact on the mgmt node.
- **Metrics + logs history**: `/opt/bedrock/data/{vm,vl}` intact.
- **DRBD activity log**: used on restart to recover any in-flight block
  ranges deterministically.

Nothing needs to be re-downloaded, re-installed, or re-configured.

## Cattle VMs

A cattle VM's disk survives the outage (local LV is untouched), but
there is no replica. When the node comes back, the VM auto-starts (if
libvirt was configured to) or can be started manually. If the node's
storage was corrupted (failed disk, not just power cut), cattle data is
lost — which is the contract with the operator.

## Related

- [`power-loss-secondary.md`](power-loss-secondary.md) — single-node case.
- [`power-loss-primary.md`](power-loss-primary.md) — primary-only outage.
- [`node-rejoin.md`](node-rejoin.md) — bringing a single node back.
- [`split-brain.md`](split-brain.md) — if generation UUIDs diverged.
