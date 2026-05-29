# Scenario: primary node power loss (VM failover)

The node **running** a pet/vipet VM — DRBD Primary, QEMU process, writable
mount — loses power or crashes hard. The VM stops instantly. Bedrock detects
the loss, promotes a surviving peer to Primary, and restarts the VM there.

## State before

Pet example:

```
   node1 (P, mgmt)                node2 (S)
  ┌──────────────┐               ┌──────────────┐
  │  VM foo ←──   │               │              │
  │  DRBD Primary │═══════════════│  DRBD Sec.   │
  │  UpToDate     │               │  UpToDate    │
  └──────────────┘               └──────────────┘
```

vipet: same with a 3rd Secondary on node3.

## What happens

1. **T=0 — node1 dies.** QEMU gone, DRBD connection drops, libvirtd gone,
   mgmt dashboard gone if node1 hosted it.
2. **Immediately**: in-flight writes the guest issued but never got an ACK for
   are lost. Anything already ACKed to the guest was also ACKed by the peer
   (protocol C = synchronous), so the peer's disk is byte-identical up to the
   last completed write.
3. **~6 s**: DRBD on peers notes the connection drop. `drbdadm status` on node2
   shows peer node1 `Connecting` or `StandAlone`. node2's own disk stays
   `UpToDate`. It is still Secondary — DRBD9 never auto-promotes on peer loss.
4. **Mesh**: peers stop hearing node1's heartbeat (`bedrock-d` netd tracks
   each neighbour's `last_seen`).

## What Bedrock does

Failover is automated by the three-task state machine in
[`../../bedrock_d/orchestrator/vm_failover.py`](../../bedrock_d/orchestrator/vm_failover.py),
running inside `bedrock-d` on every node. Two independent clocks drive it:
the **dying side** suspends its own VMs, and the **surviving peer** takes them
over. Both keep off split-brain — neither acts without a clear signal.

```
  on the surviving peer (every 5 s tick):
    if rqlite has quorum
       AND a known peer's last_seen >= 35 s (mesh-neighbour view):
      for each VM with vms.host == dead_peer
          AND peers_after_dead(vms.failover_order, me, dead_peer):   # I'm next in line
        drbdadm disconnect <each disk>     # stop refused inbound replication
        drbdadm primary    <each disk>     # bumps DRBD current-UUID
        record_uuid_after_promote          # write new UUID to rqlite (quorum-confirmed)
        is_safe_to_start_vm                # STRONG-read: local UUID == recorded UUID?
        virsh start                        # VM is already defined on every peer (create/convert)
        UPDATE vms SET host = me
```

Targeting is deterministic: `vms.failover_order` (set at create/convert) picks
exactly one next-in-line peer, so two survivors never both promote the same VM.

The pre-start safety check refuses takeover unless the local DRBD current-UUID
matches the cluster's recorded UUID (strong read, forcing a Raft round-trip).
A mismatch means the local copy is behind or a later takeover already happened
elsewhere — promoting would lose writes, so it refuses and logs for the
operator.

### Quorum gate (the dying side)

When node1's own weighted vote falls below majority, netd's election layer
drops `/run/bedrock-no-quorum`. ~5 s after that marker (≈T+20 wall-clock from
the partition) `bedrock-d` suspends every local pet/vipet VM and records each
in `/var/lib/bedrock/suspended-vms.json`, keyed by the marker mtime
(quorum-loss start). A node that cannot see quorum therefore freezes its VMs
rather than keep writing — so even if the surviving peer were wrong, the dead
side is not racing it with new writes.

A suspended VM that is still down **5 minutes after quorum loss** (clock runs
from the marker mtime, not from suspend) is `virsh destroy`ed — by then the peer
holds it, and the frozen local copy is only consuming RAM. Recovery on quorum
return resumes any VM still suspended inside that window (see *Recovery* below).

Election weights (`installer/lib/election.py`): node = 100, valid+confirmed
witness = 1; `majority = (100·active_nodes + configured_witnesses)//2 + 1`. A
configured-but-invalid witness raises the bar and biases toward "don't fail
over". Survivor promotes at `MASTER_LOSS_MISSES = 10` (~10 s); an isolated
master self-demotes at 9 (~9 s, one tick earlier, so `.254` is never on two
nodes at once).

The **witness** is BedRock Echo — a passive per-node K/V slot store on UDP
12321 (ChaCha20-Poly1305 over msgpack), one slot per node. It is consulted for
the *arbiter* (`.254` rqlite/SeaweedFS singleton) takeover, not for per-VM
failover, which keys off the mesh-neighbour view + rqlite quorum above.

## What the operator sees

| Where | What |
|---|---|
| Dashboard — if mgmt was on node1 | Page stops loading / WS disconnect (browser auto-reconnects). Reach the dashboard on any surviving node — every node serves it on `:8443`. |
| Dashboard — if mgmt on another node | node1 dot red; VM tile flips `running_on` to the takeover peer within ~35–45 s once `vms.host` is updated. |
| `journalctl -u bedrock-d` on the takeover peer | `vm_failover: TAKEOVER COMPLETE — VM 'foo' now running on <me>`, or a refusal with the UUID-mismatch reason. |
| rqlite `vms` table | `host` rewritten to the takeover peer; `drbd_resources.current_uuid` bumped to the post-promote value. |

## Recovery — node1 returns

1. DRBD comes up (`drbdadm up`), connects, and finds itself `Outdated` — its
   last UpToDate generation is older than the now-primary peer.
2. It enters `SyncTarget` and resyncs the delta. The VM keeps running on the
   new primary throughout (the returning node is a read-only shadow during
   resync).
3. When both are `UpToDate`, the operator may live-migrate back to node1 — see
   [`../actions/vm-migrate.md`](../actions/vm-migrate.md).

If node1 was suspended (not killed) before quorum returned, `bedrock-d`'s
recovery path strong-reads the `vms` table: if `vms.host` still names node1 and
the VM is `running`, it `virsh resume`s and drops it from the suspended record;
if a peer took over, it `virsh destroy`s the stale local copy and `drbdadm
secondary`s the resource so DRBD resyncs from the new primary.

### Split-brain variant

If the old primary kept accepting writes after the failover (promoted while the
old primary was briefly disconnected but still running), both disks diverge —
the UUID-match safety check refuses an unsafe start, but a genuine split-brain
still needs resolution. See [`split-brain.md`](split-brain.md).

## Impact per workload type

| Type | Data integrity | Recovery |
|---|---|---|
| cattle on dead node | VM gone; local LV intact on disk but inaccessible until reboot. No DRBD, no failover. | Power on node; VM auto-restarts (XML defined locally, libvirtd auto-starts it). |
| pet on dead node | No loss beyond the last un-ACKed write. | Peer takes over automatically (~T+35); `virsh start` from the last ACKed block state. |
| vipet on dead node | Two surviving Secondaries; one becomes Primary. | Same as pet, one more Secondary can die before data is at risk. |

## The 2-node trap

A 2-node pet cluster whose **primary** dies risks split-brain if the survivor
promotes blindly and the old primary returns first with its own unreplicated
writes. The witness is the third voice: with a valid witness the survivor's
vote clears majority and it promotes safely; without one, a lone survivor's
100 votes fall short of `majority` for a 2-node + 1-configured-witness cluster
(total 201, majority 101) when the witness is unreachable, so it stays
NoQuorum and refuses — safety over availability.

If a witness is genuinely unavailable and the operator must promote by hand,
confirm the old primary is **not** coming back with newer data first; if in
doubt, power it off permanently before promoting.

## Related

- [`power-loss-secondary.md`](power-loss-secondary.md) — easy case.
- [`power-loss-all.md`](power-loss-all.md) — total outage.
- [`network-partition.md`](network-partition.md) — split with no node loss.
- [`split-brain.md`](split-brain.md) — diverged writes.
- [`node-rejoin.md`](node-rejoin.md) — clean rejoin after outage.
