# Drain or remove a node

Two distinct operations take a node out of service:

- **Maintenance mode** — *temporary*. Drains the node from the quorum math so
  you can reboot it / service hardware without the cluster treating its absence
  as a fault. The node stays a cluster member; you turn maintenance back off
  when it returns.
- **Leave** — *permanent*. Cleanly removes the node from the cluster: drops its
  rqlite Raft voter, forgets it in cluster state, and stops its services. Use
  when retiring or replacing hardware.

**Triggered by:**

- CLI: `bedrock node maintenance <node> on|off`, `bedrock node leave <node>`,
  `bedrock node list`
- (Maintenance also surfaces on the dashboard nodes view.)

**Source:** `installer/bedrock:_cmd_node_maint` / `_cmd_node_leave`;
`installer/lib/bedrock_state.py:node_maintenance` / `node_unregister`;
`bedrock_d/install/node_leave.py:run_node_leave` (the crash-safe saga) — see
[`sagas/node_leave.md`](../sagas/node_leave.md).

## Maintenance mode (temporary drain)

```
bedrock node maintenance node-3 on      # before planned downtime
#   … reboot / service node-3 …
bedrock node maintenance node-3 off      # when it's back
```

- Sets the node's `maintenance` flag in rqlite. Each node's orchestrator watches
  the rqlite revision and re-renders its `daemon.toml` on change — **rqlite is
  the propagation channel, no SSH fan-out**.
- A node in maintenance is **excluded from the election denominator**: the
  surviving nodes won't drop the no-quorum marker or count its absence as a lost
  vote when you take it offline. Without this, powering a node off looks like a
  fault and can disturb the cluster.
- Turn it **off** when the node returns so it rejoins the vote.
- Maintenance does **not** move running VMs off the node. Live-migrate pets you
  don't want interrupted first ([`vm-migrate.md`](vm-migrate.md)); cattle/ViPet
  VMs come back when the node does, or fail over per their HA level.

## Leave (permanent removal)

```
bedrock node leave node-3                 # run from ANOTHER node
bedrock node leave node-3 --reason "RMA disk backplane"
```

**Run it from a surviving node, not the node being removed** — leave-from-self
is refused (the leaver can't both orchestrate its own removal and stop its
services cleanly). The default path runs the crash-safe `node_leave` saga, which:

1. `node_unregister` in rqlite (single-writer master discipline) — cluster state
   forgets the node.
2. **Drops the node's Raft voter** via rqlite `DELETE /remove` (voter id = last
   octet of its loopback). This is the critical step: a removed node's offline
   rqlited would otherwise still count as a voter, so **consecutive leaves
   without it brick quorum at N/2**.
3. Regenerates the master's `daemon.toml` so its peer set drops the leaver
   immediately.
4. Best-effort stops the leaver's `bedrock-d` (so it stops mesh/election/witness
   gossip; otherwise it lingers in survivors' peer tables and inflates the node
   count — which has blocked failover in the past).

If the node also held storage replicas, drain them first/after with
`bedrock storage remove-peer <node>` so no DRBD resource is left pointing at the
departed node.

## Preconditions

- rqlite reachable with a leader (the master commits the membership change).
- **Quorum is preserved by the change.** Removing a node drops the node-vote
  total; never leave so many nodes that the survivors fall below majority. On a
  small cluster, put a node in **maintenance** (still a member, just drained)
  rather than **leave** it, unless you truly intend permanent removal.
- For `leave`: run from a node that will remain; the target reachable over SSH
  for the best-effort stop (non-fatal if not).

## Failure modes and recovery

| Symptom | Cause | Recovery |
|---|---|---|
| `ERROR: cannot leave-from-self` | Ran `node leave <self>` on the node being removed | Re-run from a surviving node. |
| `<node>: not in cluster snapshot — nothing to do` | Already removed | None — idempotent. |
| `warn: rqlite /remove failed` | Couldn't drop the Raft voter (leader unreachable mid-leave) | Re-run the leave once a leader is back, or `DELETE /remove {"id": <octet>}` against rqlite directly — else the stale voter counts toward quorum. |
| Survivors still see the left node / `n_nodes` too high | The leaver's `bedrock-d` kept gossiping (SSH stop failed) | Stop `bedrock-d` on the departed node (`systemctl stop bedrock-d`), or power it off. |
| Cluster goes no-quorum after a leave | Left below majority, or the voter wasn't removed | Bring a node back / re-add; ensure the Raft voter count matches the live node count. Use maintenance, not leave, for transient downtime. |

## Operator perspective

- **Maintenance**: instant (one rqlite write); propagates within ~1–2 s. The
  natural choice for reboots, kernel updates, brief hardware work.
- **Leave**: a few seconds for the saga. Irreversible without a re-`join`.
- Rule of thumb: **maintenance for "back soon", leave for "gone for good".**
  On a 2- or 3-node cluster, prefer maintenance — a permanent leave that drops
  you below majority halts the cluster by design (split-brain safety).

See also: [`join-cluster.md`](join-cluster.md) (the inverse — adding a node),
[`vm-migrate.md`](vm-migrate.md) (move pets off first), and
[`sagas/node_leave.md`](../sagas/node_leave.md) (saga internals).
