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
      all 3 ─── LAN ─── (operator)             (mgmt still reachable)

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
  `StandAlone`. The data plane is unaffected — writes keep committing on
  the connected peers.
- node3's `node-exporter` (9100) and `vm-exporter` (9177) run on every
  node and are still scraped — they ride the LAN/loopback path, not the
  DRBD link. The dashboard shows node3 as **Online**, but its DRBD tiles
  show it missing from each resource's peer list.

From node3's side:

- All its DRBD peers show `Connecting` / `StandAlone`. Its local disks
  are still `UpToDate`, but it is out of the cluster from a data
  perspective.
- It keeps running cattle VMs (local thin LV, no DRBD). Any pet/vipet it
  hosted as Secondary keeps running as Secondary — no writes, waiting for
  a peer.
- If node3 was the Primary of a pet/vipet, its DRBD writes stop being
  acked by peers. The default config sets `after-sb-0pri
  discard-zero-changes` but does not set `on-no-data-accessible`, so DRBD
  uses its own default for the in-flight-I/O behaviour.

**Automatic action**: none on the isolated minority side, by design.
node3 alone is below quorum and cannot safely decide it holds the right
state. On the majority side, the weighted vote (below) keeps the master
role and storage live without any operator action.

**Operator action**: fix the DRBD link. On recovery, DRBD partial-resync
catches node3 up and the cluster re-converges.

### Shape B — DRBD ring split

For node3 specifically, same as Shape A. The majority (node1+node2 for a
pet, any two-of-three for a vipet) keeps serving. `after-sb-0pri
discard-zero-changes` + generation UUIDs + the witness slot check ensure
no split-brain.

If **both halves** try to promote, that is split-brain — see
[`split-brain.md`](split-brain.md).

### Shape C — mgmt LAN split, DRBD intact

Replication continues across the DRBD ring; the data is safe. But the
mgmt API collects per-node state by SSH to each node's LAN host IP, so:

- Operator on LAN segment A reaches node1 only.
- Operator on LAN segment B reaches node2, node3.
- The dashboard on whichever node runs mgmt has partial visibility: it
  cannot SSH to the other segment's hosts, so those node tiles go red
  (`online: false`).

**Behaviour**: each segment observes what it can. No action is automatic.
A `bedrock vm migrate` succeeds within a reachable segment or fails when
the target is unreachable — the migrate saga's `virsh migrate --live` to
`qemu+ssh://root@<target-loopback>/system` cannot open the SSH channel.
The log panel shows:

```
VM foo migration FAILED from nodeA to nodeB: ssh: connect to host nodeB
  port 22: No route to host
  level=error
```

**Operator action**: fix the LAN. If the split is planned (e.g. switch
maintenance), drain workloads to one segment first via
`bedrock vm migrate`.

## The witness + weighted-vote principle

Failover is decided per node by a pure weighted-vote election
(`installer/lib/election.py`), run once per second inside `bedrock-d`'s
netd thread. It needs no rqlite — it is what *recovers* rqlite.

Vote model (`node = 100`, `witness = 1`):

```
  total    = 100·active_nodes + configured_witnesses     (from rqlite)
  majority = total // 2 + 1
  my_votes = 100·(self + ACKing peers) + valid_witnesses
```

A peer's 100 votes are an **active ack**, not passive reachability: a
peer grants them only once it too has lost the master AND finds the
candidate's advertised arbiter-DRBD UUID eligible. A witness adds its 1
vote only when it is reachable AND reflects our own slot write-back;
otherwise it counts 0 in `my_votes` but still counts in `total` — which
raises the bar and biases toward "do not fail over". A witness can only
ever break an exact node-tie, never overrule a real node.

This is what stops Shape B from escalating to split-brain:

```
  node1 loses contact with node2 and node3:
    my_votes = 100·(self) + witness     ≈ 100
    total    = 300 (+ witnesses), majority = 151
    → 100 < 151 → NoQuorum; node1 does NOT promote

  node2 + node3 lose contact with node1:
    my_votes = 100·(self + acking peer) = 200 (+ witness)
    → 200 ≥ 151 → quorum; lowest-loopback-octet of the two promotes,
      the other defers a tick and acks it (deterministic tiebreak)
```

Timing (election tick 1 s):

- A survivor promotes at `MASTER_LOSS_MISSES = 10` (~10 s after the
  master's heartbeats stop).
- An isolated master self-demotes at `SELF_DEMOTE_MISSES = 9` (~9 s, one
  second earlier) so the `.254` arbiter VIP is never on two nodes at once.
- An isolated master that *restarts* mid-partition reads all active nodes
  from rqlite, sees `n_nodes = N`, and falls to NoQuorum instead of
  faking a single-node cluster.

The witness is **BedRock Echo** on UDP 12321 — a passive per-node K/V
slot store, ChaCha20-Poly1305 over msgpack. Each node owns one slot
(key = loopback last octet), writes it every second and reads the others
to decide arbiter takeover. A slot is stale after `SLOT_STALE_MS = 10000`.
Echo runs on a separate power domain and path from the nodes (an ESP32
appliance in production; `testbed/bedrock_echo_stub.py` on the testbed),
so it can distinguish "partitioned" from "dead" and serve as the
tiebreaker for an exact node split.

## What actually moves the VMs

On a node that loses quorum, the arbiter and VM failover paths run
inside `bedrock-d` (`bedrock_d/orchestrator/vm_failover.py`,
`installer/lib/cluster_arbiter.py`) with no operator action:

- The isolated minority side suspends its local pet/vipet VMs ~20 s after
  the connection drops (RAM-frozen, no disk writes). Cattle are left
  alone.
- The surviving majority side takes over each VM where it is next in
  `vms.failover_order` ~35 s in: `drbdadm disconnect` → `primary` →
  record the new UUID in rqlite → strong-read safety check → `virsh
  start` → `UPDATE vms SET host = me`.
- A VM still down **5 minutes after quorum loss** is killed on the
  isolated side (the clock runs from the connection drop, not from
  suspend); it has been taken over elsewhere by then.

The arbiter takeover (`.254` VIP + arbiter rqlite + SeaweedFS filer)
uses only the witness slot read plus local commands (`drbdadm`, `ip`,
`mount`, `systemctl`) with an exact arbiter-UUID match — no rqlite, since
rqlite is the service being recovered.

## Recovery

For every partition shape:

1. Restore the failed link.
2. DRBD reconnects and runs a partial resync.
3. The mgmt state collection picks up the re-arrived nodes on its next
   ~3 s SSH poll; dashboard tiles flip back to Online.
4. On quorum return, a still-suspended pet/vipet is resumed and dropped
   from the kill record.
5. No data loss for writes that were quorum-acked. Writes made on a
   minority-isolated node were either never acked (lost on link drop) or
   sit in that node's DRBD activity log and are reconciled by resync.

## Observability during a partition

Bedrock surfaces the symptoms rather than a dedicated "partition" event:

- The mgmt state collection moves unreachable nodes to `online: false`
  (red dots in the sidebar, `-` tiles on `/hosts`). This is observation,
  not an action — no `push_log`.
- Any migrate/convert that crosses the partition fails with an SSH error;
  that path emits a `push_log` at `level=error`.
- `journalctl -k` on each side shows DRBD connection drops and reconnect
  attempts.
- The takeover/suspend/kill steps log under `bedrock.vm_failover`.

## Related

- [`split-brain.md`](split-brain.md) — what to do if both sides promoted
  during the partition.
- [`power-loss-primary.md`](power-loss-primary.md) — partition where one
  side is dead, not isolated.
- [`node-rejoin.md`](node-rejoin.md) — the clean rejoin path after the
  link comes back.
