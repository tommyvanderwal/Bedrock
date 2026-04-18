# Scenario: network partition

Two halves of the cluster can no longer talk to each other, but each
half is still internally healthy and (importantly) each half is still
talking to the operator's LAN. VMs are running somewhere in the cluster
— which half owns them?

## Common partition shapes

```
  Shape A: one node isolated
      node1 ─┐
      node2 ─┼─ LAN ─── (operator)
      node3  X  DRBD ring broken to node3

  Shape B: DRBD ring split, mgmt LAN intact
      node1 ═══ DRBD ═══ node2                 (DRBD healthy)
                          X
                          X
                         node3                 (DRBD isolated)
      all 3 ─── LAN ─── (operator)             (mgmt/SSH still work)

  Shape C: mgmt LAN split, DRBD ring intact (rare, dedicated ring OK)
      node1 ─┐ mgmt LAN A                      (operator on A talks to node1)
             │
      node2 ─┘ mgmt LAN B                      (operator on B talks to node2,3)
      node3 ─
      node1 ═ DRBD ═ node2 ═ DRBD ═ node3     (replication unaffected)
```

## Bedrock's behaviour per shape

### Shape A — one node isolated on the DRBD ring

From the majority side (node1 + node2):

- DRBD to node3 drops. `drbdadm status` shows node3 as `Connecting` /
  `StandAlone`. The data plane is unaffected — writes continue to
  commit on the connected peers.
- node3's `node-exporter` and `vm-exporter` are still scraped via the
  LAN (we scrape by host IP, not drbd_ip). The dashboard shows node3
  as **Online**, but its DRBD tiles show it missing from the peer list.

From node3's side:

- All its DRBD peers show `Connecting` / `StandAlone`. Its local disks
  are still `UpToDate`, but it's out of the cluster from a data perspective.
- It can still run cattle VMs. Any pet/ViPet it previously hosted as
  Secondary keeps running as Secondary (no writes — waiting for peer).
- If node3 was the Primary of a VM, it stops accepting acks from peers;
  depending on `on-no-data-accessible` policy (default: freeze I/O) the
  VM may stall or (with `io-error`) get I/O errors. Bedrock's default
  does not explicitly set this — DRBD falls back to its own default.

**Automatic action**: none, by design. node3 alone cannot safely decide
it has the "right" state — it might be the one that's wrong.

**Operator action**: fix the DRBD link. On recovery, DRBD partial-resync
catches node3 up and the cluster re-converges.

### Shape B — DRBD ring split

Same as A for node3 specifically. The majority (node1+node2 for pet, or
any two-of-three for ViPet) continues serving. DRBD's `after-sb-0pri
discard-zero-changes` + generation UUIDs + the operator-run witness
ensure no split-brain.

If **both halves** try to promote, that's split-brain — see
[`split-brain.md`](split-brain.md).

### Shape C — mgmt LAN split, DRBD intact

Replication continues across the DRBD ring; the data is safe. But:

- Operator on LAN segment A can reach node1 only.
- Operator on LAN segment B can reach node2, node3.
- The dashboard on whichever node runs mgmt has partial visibility: it
  can't SSH to the other segment's hosts, so those tiles go red.

**Behaviour**: both segments observe what they can. No action is
automatic. If the operator tries to migrate a VM through the dashboard,
it either succeeds within the reachable segment or fails with SSH
timeout to unreachable nodes. The log panel shows:

```
VM foo migration FAILED from nodeA to nodeB: ssh: connect to host nodeB
  port 22: No route to host
  level=error
```

**Operator action**: fix LAN. Or, if the split is expected (e.g.,
maintenance on a switch), drain workloads to one segment first via
`bedrock vm migrate` and plan accordingly.

## The witness + quorum principle

The failover orchestrator (`bedrock-failover.py`, scaffolded but not yet
fully wired on the sim cluster) uses 2-of-3 quorum: promote only if the
witness agrees AND the majority of peers agree. This prevents Shape B
from escalating to split-brain:

```
  node1 loses contact with node2 and node3 (partition):
    witness says:  node2 and node3 alive
    peers say:     cannot reach any peer
    → no quorum; I do NOT promote

  node2 and node3 lose contact with node1:
    witness says:  node1 alive (it's behind a partition, not dead)
    peers say:     each other alive, node1 not
    → witness disagrees with peers; conservative: no promote
```

If the witness can distinguish "partitioned" from "dead" (it's on a
different power domain / different path), its vote is the tiebreaker.
Bedrock's witness runs on the MikroTik switch — in the same room but on
a separate PSU and separate uplink, which is enough for most LAN
partition cases.

## Recovery

For every partition shape:

1. Restore the failed link.
2. DRBD reconnects automatically (if `auto-promote-timeout` policies
   haven't kicked in to force StandAlone). Partial resync runs.
3. The state push loop picks up the re-arrived nodes on the next 3 s
   tick; dashboard tiles flip back to Online.
4. No data loss *for writes that were quorum-acked*. Any writes on a
   minority-isolated node were either never acked (lost on link drop)
   or are now in that node's DRBD activity log and will be reconciled
   by resync.

## Log lines during a partition

Bedrock today doesn't push dedicated "partition detected" events; it
surfaces the symptoms:

- State push loop: affected nodes move to `online: false` (visible as
  red dots in the sidebar and `-` tiles on `/hosts`). No push_log —
  this is **observation**, not an action.
- Any migrate / convert attempt that crosses the partition will fail
  with an SSH error; `_vm_migrate` pushes a push_log at level=error.
- Kernel log (`journalctl -k`) on each side shows DRBD connection drops
  and reconnect attempts.

A future improvement is to push a `push_log` event from the state loop
when a node transitions `online=true` → `online=false` so the Recent
Logs panel captures the moment.

## Related

- [`split-brain.md`](split-brain.md) — what to do if both sides
  promoted during the partition.
- [`power-loss-primary.md`](power-loss-primary.md) — special case of
  partition where one side is just dead, not isolated.
- [`node-rejoin.md`](node-rejoin.md) — the clean rejoin path after the
  link comes back.
