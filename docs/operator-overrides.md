# Operator override actions — safety catalog

Catalog of explicit operator commands that bypass an automatic
safety check. Every override here is necessary precisely because
the safe default refuses — auto-recovery would risk silent data
loss or split-brain. Each entry needs hard thinking about how to
make the override as safe as possible *without* turning it back
into the automatic behavior we just removed.

This file is the index. Detailed per-command design (CLI shape,
required prompts, audit trail, what state changes, what locks)
lives in the per-command section below as it gets specified.

## Status of each override

| Override | Required? | Status |
|---|---|---|
| Decommission stuck-LMS holder (`bedrock node leave …` on dead node) | Yes — primary path for resolving INV-7 stuck-LMS | Drafted (this file) |
| Re-key witness identity (new `cluster_uuid` / `cluster.key`) | Yes — full cleanup of stale witness state including ESP32-reboot worst-case | Drafted (this file) |
| Seize master with stale data | Yes — irrecoverable up-to-date peer | Outline only |
| Force `drbdadm invalidate` on a node | Yes — DRBD divergence resolution | Outline only |
| Force-clear no-quorum marker | Yes — operator override after misfire | Outline only |
| Force cluster to single-node mode | Yes — surviving solo node after permanent peer loss | Outline only |
| Cancel an in-flight saga | Maybe — stuck join, stuck takeover | Not yet specified |
| Demote current master on operator command | Yes — planned handoff outside maintenance flow | Not yet specified |
| Re-key cluster CA (TLS) | Eventually — operator-triggered key rotation | Outline only |

## General principles for every override

1. **Authentication.** Override must require operator credentials,
   not a passive flag. Per the existing `bedrock operator` model,
   operators have salt+hash credentials in rqlite. Override CLI
   verbs require an authenticated session.
2. **Confirmation.** Every override prompts with the specific
   consequence (data loss, split-brain risk, etc.) and requires
   an explicit confirmation string — not just `y/N`. Example:
   `Type the cluster name to confirm: bedrock-prod-01`.
3. **Audit trail.** Every override writes an `operator_override`
   row into rqlite (or a local audit log if rqlite is the thing
   being recovered) with: timestamp, operator name, command
   verb, target (node/witness/saga ID), reason field (operator
   input), pre-state snapshot, post-state outcome. Cannot be
   suppressed.
4. **Reversibility check first.** Where possible, the command
   checks whether a safer alternative exists right now
   (e.g. before "seize with stale data," check if the up-to-date
   peer might be reachable via any alternate path) and surfaces
   that to the operator.
5. **Single-actor lock.** While an override is running, all other
   overrides on the same target are refused. Stops two operators
   stomping on each other during an incident.
6. **No silent retries.** If an override fails (e.g. witness
   unreachable mid-execution), it fails loudly. Does not silently
   queue.

## Override: Decommission stuck-LMS holder

**When needed.** A node had `tag.lms = 1` on a witness slot and
died (or got partitioned away permanently) without ever writing
`tag.lms = 0`. Per INV-7, this slot stays `lms = 1` forever from
the cluster's POV. The takeover protocol (`cluster-quorum-spec.md`
step 2) will refuse for every surviving peer until the cluster
stops treating the dead node as a member.

**Why this is the primary path.** The cluster's slot-read logic
ignores any witness slot for a node not in the local `rqlite `nodes` table`
(and the rqlite `nodes` table). So removing the dead node from
cluster membership — the routine `node leave` saga — *also*
removes the LMS-veto effect without ever writing to the witness.
The witness still stores the encrypted slot until 72 h retention
drops it; the cluster just stops looking at it.

**Why no "clear LMS bit" override exists.** Writing `tag.lms = 0`
on behalf of a dead node would mean impersonating that node at
the witness — either through a privileged-write extension to the
witness protocol (INV-2 currently forbids cross-node writes) or
via key material the dead node owns. Neither is correct: the
LMS bit is *evidence the dead node had set itself as solo
master*, and you can't safely retract that evidence on its
behalf. You can only stop counting the dead node as part of
the cluster.

**What the operator must verify before running this.** All of:
- The LMS-holder node is genuinely gone, not just unreachable
  from this node. (Hardware confirmed dead, returned, scrapped,
  or chained to a fence we're never opening.)
- The accepted risk: any data the dead LMS-holder wrote after
  the cluster's last successful DRBD sync to it is lost. This
  is likely — LMS was set precisely because the surviving peer
  was already gone when this node went solo.
- A surviving node has DRBD `tier-critical` in a usable state
  (UpToDate, even if behind the lost LMS-holder's last writes).

**CLI shape.** Uses the existing `bedrock node leave` saga but
with the operator-accept-data-loss flag (currently used only for
seize). Reuses the existing rqlite-backed remove + rqlite `nodes` table
update path; no new witness operation needed.

```
bedrock node leave --target <node-name> \
    --reason "<free-text>" \
    --accept-data-loss
```

**What it does, in order:**

1. Verify caller is authenticated operator.
2. Verify target node is not reachable on mesh (last seen >
   5 × patience window). If reachable, suggest maintenance-mode
   shutdown instead.
3. Show the operator the stuck LMS slot details (last refresh
   timestamp, marker UUID, DRBD history chain context).
4. Confirmation prompt: type the cluster name + the target node
   name.
5. Write `operator_override` audit row to rqlite.
6. Run the existing `node_leave` saga: `DELETE /remove` on rqlite
   to drop the voter, remove the row from the `nodes` table,
   propagate rqlite `nodes` table snapshot to surviving members.
7. Surviving nodes' next election tick: target node-id is no
   longer in rqlite `nodes` table → witness slot for that node-id is
   ignored regardless of `tag.lms`. Takeover can proceed.

**Cluster.json membership filter dependency.** This override
relies on the rqlite `nodes` table filter being implemented in
`installer/lib/witness.py drain_replies`. **Filter is currently
not implemented** — flagged as a follow-up code change. Until
it's in place, even after `node leave` succeeds, surviving
nodes' read_slot() will still return the dead node's entry and
the takeover will refuse. The filter needs to be added before
this override is effective.

## Override: Re-key witness identity

**When needed.** Either:
- The witness itself has lost state (ESP32 reboot, fileshare
  data corruption) and the cluster needs a deterministic clean
  start rather than a worst-case-assumed limbo.
- After a stuck-LMS decommission, the operator wants the witness
  to actually forget the old slot rather than wait 72 h.
- The `cluster.key` is suspected compromised.

**What it does.** Generates a new `cluster_uuid` and/or
`cluster.key`, distributes via rqlite + secure peer-to-peer to
every surviving cluster member, restarts witness heartbeats on
all nodes with the new identity. Old encrypted slots (held by
the witness with the old cluster_uuid) become un-decryptable
by any current cluster member — they're effectively cleared
from the cluster's perspective.

**CLI shape (proposed):**

```
bedrock cluster rekey-witness --reason "<free-text>"
```

**What it does, in order:**

1. Verify caller is authenticated operator.
2. Verify a quorum of nodes is reachable (so the new key can be
   distributed atomically).
3. Generate new `cluster_uuid` (and optionally new `cluster.key`).
4. Write `operator_override` audit row to rqlite.
5. Distribute new key + uuid to all reachable nodes via the
   existing secure peer-to-peer channel.
6. Each node atomically swaps its in-memory cluster identity for
   the new one + restarts witness heartbeats.
7. Within a few seconds the witness's slot map for the new
   cluster_uuid populates from fresh heartbeats. Old cluster_uuid
   slot map is no longer addressable from cluster members.

**Fileshare-witness variant.** For a fileshare-backend witness
(SMB / NFS / S3) the equivalent is `rm` on every `slot-*.bin`
file. Slot files repopulate from heartbeats within a few seconds.
Lower-overhead than a full re-key when only goal is to clear
stale slot state.

**Failure modes:**
- A surviving node is unreachable during distribution → that
  node will continue with the old key, see no slots when
  it queries with the old cluster_uuid, become out-of-sync.
  Operator must reach it manually and apply the new key.
- The cluster_uuid is referenced elsewhere in the system
  (e.g., backups, audit logs) — re-keying creates a "before / after"
  boundary in those records. Plan accordingly.

## Override: Seize master with stale data

**When needed.** The up-to-date master died and is not coming
back (hardware lost, datacenter event, etc.). A surviving node
has stale data (its UUID is in the dead master's history; some
peer writes are unrecoverable). Without override, eligibility
check (cluster-quorum-spec.md INV-5 + history rule) refuses
promotion forever.

**Conceptual parallel.** Active Directory's FSMO role "seize"
on a lagging domain controller.

**Status.** Outline only. Needs full spec before shipping.
The CLI verb is approximately:
`bedrock cluster seize --reason "<free-text>" --accept-data-loss-from <peer-name>`.

## Override: Force `drbdadm invalidate`

**When needed.** Two nodes have DRBD generations that have
diverged (neither's current UUID is in the other's history).
DRBD itself refuses to auto-sync; operator must pick which
side wins.

**Status.** Outline only. CLI is approximately:
`bedrock storage invalidate --tier <name> --on <node> --in-favor-of <peer>`.

## Override: Force-clear no-quorum marker

**When needed.** `/run/bedrock-no-quorum` was created in
error (test misfire, transient bug) and is now blocking
recovery even though the cluster is healthy.

**Status.** Outline only. CLI is approximately:
`bedrock node clear-no-quorum --node <name> --reason "<free-text>"`.

## Override: Force cluster to single-node mode

**When needed.** A 2-node cluster has permanently lost one node
(hardware gone, never coming back) and the operator wants to
collapse to N=1 operation rather than running degraded forever.

**Status.** Outline only. Requires removing the dead node from
rqlite `nodes` table + reconfiguring DRBD to single-replica + adjusting
rqlite voter set. Wide blast radius; needs multi-step saga.

## Override: Re-key cluster CA (TLS)

**When needed.** The cluster CA private key is suspected
compromised, or the operator wants to rotate it on a schedule
(unusual — certs are 100-year and rotation is explicit, not
time-triggered). Same mechanism for issuing fresh per-node certs
under a new CA without a full re-install.

**Why it's load-bearing.** rqlite has no hot-reload for TLS certs
(verified against `./installer/binaries/rqlited -h` — no `-reload`
flag, no SIGHUP support). Any CA rotation therefore requires
restarting `bedrock-rqlited` on every node + `bedrock-rqlited-arbiter`
on the master. The restart must be **rolling and quorum-aware** —
restarting two voters simultaneously in a 3-voter cluster
(2 per-node + 1 arbiter) drops to 1 voter = no quorum =
control plane offline.

**Outline of the saga, not specified:**
1. Generate new CA (key+cert) → write to staging path on master.
2. Re-sign every existing node cert under the new CA →
   distribute via rqlite to each node (using the *existing* CA
   for transport while the rotation runs).
3. On each node, write new cert + new CA cert to staging paths.
4. Rolling restart: one voter at a time, wait for Raft to
   re-form quorum, move on.
5. Promote staging files to live paths atomically.
6. Verify every node + arbiter healthy with the new CA.
7. Delete the old CA + old certs.

**Status.** Outline only — no CLI verb yet. Tracked here because
it's the only operational reason to need the rolling-restart
machinery; routine joins/leaves don't require it (CA-signed
certs are trusted without bundle changes).

## Override: Remove a permanently-dead node from rqlite `nodes` table

**When needed.** Companion to single-node mode: a 3+-node cluster
where one node is permanently dead. Removing it from rqlite `nodes` table
shrinks the voting pool so quorum can be reached with fewer
survivors.

**Status.** Outline only. Less risky than single-node-mode forcing,
but still needs operator confirmation + audit.

---

(Add new override sections above this line as they get specified.)
