# Scenario: primary node power loss (VM failover)

The node that is **running** a pet/ViPet VM — DRBD Primary, QEMU process,
writable mount — loses power or crashes hard. The VM stops instantly.
Bedrock must detect the loss, promote a surviving peer to Primary, and
restart the VM there.

## State before

Pet example:

```
   node1 (P, mgmt)                node2 (S)
  ┌──────────────┐               ┌──────────────┐
  │  VM foo ←──  │               │              │
  │  DRBD Primary│═══════════════│  DRBD Sec.   │
  │  UpToDate    │               │  UpToDate    │
  └──────────────┘               └──────────────┘
```

ViPet: same but with a 3rd Secondary on node3.

## What happens

1. **T=0 — node1 dies.** QEMU process gone. DRBD connection drops.
   libvirtd gone. mgmt dashboard gone (if node1 was the mgmt host).
2. **Immediately**: any in-flight writes the VM had issued but not yet
   ACKed to the guest are lost. Writes that had already been ACKed to
   the guest were also ACKed by the peer (protocol C = synchronous),
   so the peer's disk is byte-identical up to the last completed write.
3. **~6 s**: DRBD on peers notes the connection drop. `drbdadm status`
   on node2 shows peer node1 as `Connecting` or `StandAlone` depending
   on `after-sb-0pri` policy. Its own disk is still `UpToDate`. It is
   still Secondary — DRBD9 does **not** auto-promote on peer loss.
4. **Meanwhile the witness** (if configured) observes node1's heartbeat
   stop. After `witness.miss_count` consecutive misses (default 3) it
   marks node1 dead and publishes that to its `/status` endpoint.

## What Bedrock currently does

In v0.1 the failover orchestration is described in the plan but not yet
fully automated on the sim cluster. The physical-lab validation (25-run
migration test) proved the forward path; the backward path (autonomous
VM promotion) is a **manual step** for now:

```bash
# on a surviving peer, typically the one with the most recent data
# (any UpToDate peer is fine; DRBD9 resolves consistency automatically)
drbdadm primary --force vm-foo-disk0
virsh start foo
```

Once the surviving node holds the DRBD Primary and has the VM defined
in its libvirt (pet/ViPet VMs are defined on all peers via the convert
path — see [`../actions/vm-convert.md`](../actions/vm-convert.md)), `virsh
start` brings the VM up from the last ACK'd block state.

### What the orchestrator *will* do (follow-up)

`bedrock-failover.py` (already in the repo root as a scaffold) is the
daemon that each node runs. Its 2-of-3 quorum logic:

```
  every 2s:
    ask witness  : is node1 alive? (result A)
    ping node1   : TCP 22 and 9100 reachable? (result B)
    ask peers    : what do you see? (result C)
    if A==dead AND B==dead AND C confirms:
       I am the failover target (lowest node_id among live peers with UpToDate)
       promote DRBD, virsh start, push_log to mgmt
```

The gate is quorum: a node alone (disconnected from witness and peers)
does **not** promote — prevents split-brain in network-partition cases.

## What the operator sees

| Where | What |
|---|---|
| Dashboard — if mgmt was on node1 | Page stops loading / WS disconnect (browser auto-reconnects forever). Operator moves to cockpit on a surviving node and manually promotes. |
| Dashboard — if mgmt on another node | node1 dot red, VM tile shows `running_on=(unreachable)`, DRBD role last-known `Primary`. |
| Recent Logs | `push_log` was never called from node1 at failure time (it was SIGKILL'd). Surviving mgmt can log a witness-driven "Node X dead" event (future) or the operator's manual `virsh start`. |
| witness `/status` | `nodes.node1.alive=false` within ~6–10 s. |

## Recovery — node1 returns

Once node1 comes back up and rejoins:

1. DRBD resources come up with `drbdadm up`. They connect to peers and
   discover they are `Outdated` (their last UpToDate generation is
   older than the now-primary peer).
2. They enter `SyncTarget` state and resync the delta from the new
   primary. Meanwhile the VM keeps running on the new primary with full
   I/O (the old primary is a read-only shadow during resync).
3. When resync completes, both are `UpToDate`. At this point the
   operator (or orchestrator) may choose to live-migrate back to node1
   — see [`../actions/vm-migrate.md`](../actions/vm-migrate.md).

### Split-brain variant

If the old primary stayed up long enough after the failover to accept
any writes (e.g., the new primary was promoted while the old was
temporarily disconnected but still running), both disks diverge.
See [`split-brain.md`](split-brain.md) for resolution.

## Impact per workload type

| Type | Data integrity | Recovery |
|---|---|---|
| cattle on dead node | VM is gone; local LV is intact on disk but inaccessible until reboot. | Power on node; VM auto-restarts on libvirtd startup (XML still defined locally). |
| pet on dead node | No data loss beyond the last un-ACKed write. | Manual (today) or auto (orchestrator) promote on peer + `virsh start`. |
| ViPet on dead node | Two surviving Secondaries. Either can become Primary — DRBD9 elects via generation UUIDs (the one with the latest is preferred). | Same as pet but with higher resilience (one more Secondary can die). |

## The 2-node trap

A 2-node pet cluster where the **primary** dies has a 50 / 50 split-brain
risk if the surviving Secondary promotes blindly and the old primary
returns first with its own unreplicated writes. The witness is the third
voice that lets the survivor decide safely:

```
   survivor asks:
      - witness: node1 is dead (confirmed)
      - node3:   (n/a, only 2 nodes)
   → witness says dead, I'm the only UpToDate left
   → promote, start VM
```

Without a witness, the safe manual procedure is: **do not promote until
the operator confirms the old primary is not coming back with newer
data**. If in doubt, power it off permanently before promoting.

## Related

- [`power-loss-secondary.md`](power-loss-secondary.md) — easy case.
- [`power-loss-all.md`](power-loss-all.md) — total outage.
- [`split-brain.md`](split-brain.md) — diverged writes.
- [`node-rejoin.md`](node-rejoin.md) — clean rejoin after outage.
