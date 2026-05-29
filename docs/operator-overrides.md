# Operator override actions — safety catalog

Catalog of explicit operator commands that bypass an automatic safety
check. Each override exists because the safe default refuses on
purpose — auto-recovery there would risk silent data loss or
split-brain. The design goal for each: make the override as safe as
possible *without* re-introducing the automatic behavior the safe
default deliberately omits.

One override is built and shipped (stuck-LMS decommission, via the
existing `bedrock node leave` saga). The rest are design entries —
the failure they address is real, but no CLI verb exists yet. Each is
marked `Built` or `Design`.

## Catalog

| Override | Why it's needed | Status |
|---|---|---|
| Decommission stuck-LMS holder (`bedrock node leave <node>`) | Resolve INV-7 stuck-LMS so takeover can proceed | **Built** |
| Re-key witness identity (new `cluster_uuid` / `cluster.key`) | Clear stale witness slot state, incl. ESP32-reboot limbo | Design |
| Seize master with stale data | Up-to-date peer is irrecoverable | Design |
| Force `drbdadm invalidate` on a node | Resolve DRBD divergence (operator picks the winner) | Design |
| Force-clear no-quorum marker | Marker stuck after a misfire, blocking healthy recovery | Design |
| Collapse to single-node mode | Surviving solo node after permanent peer loss | Design |
| Cancel an in-flight saga | Stuck join / stuck takeover | Design |
| Demote current master on command | Planned handoff outside maintenance flow | Design |
| Re-key cluster CA (TLS) | CA key compromise or scheduled rotation | Design |

## General principles for every override

These are the rules an override must satisfy. Where the supporting
machinery already exists today it is named; where it does not, the
entry is a design constraint on the unbuilt verb.

1. **Authentication.** An override requires operator credentials, not
   a passive flag. Operators have salt+hash credentials in the rqlite
   `operators` table (`operator_auth.py`); the mgmt API mints a Bearer
   token via `/api/login`. Override verbs that go through the mgmt API
   carry that token. (`bedrock node leave` today runs the saga
   directly on the master and records `requested_by` from
   `$SUDO_USER`/`$USER`; tightening it to require a Bearer token is
   part of its hardening.)
2. **Confirmation.** An override prompts with the specific consequence
   (data loss, split-brain risk) and requires an explicit confirmation
   string — e.g. typing the cluster name — not a bare `y/N`. The
   built `node leave` path does not yet enforce this prompt.
3. **Audit trail.** Every saga-driven action writes an `operations`
   row in rqlite (`kind`, `target_node`, `params`, `state`,
   `requested_by`, timestamps) plus per-step `operation_steps`. That
   is the audit record today; `bedrock node leave` lands there as
   `kind="node_leave"` with the operator's `reason` in `params`. A
   richer override-specific record (pre/post-state snapshot, reason
   field, un-suppressible) is a design target, not yet a separate
   table.
4. **Reversibility check first.** Where a safer alternative might still
   exist right now (e.g. an up-to-date peer reachable via an alternate
   path before a stale-data seize), the command surfaces it before
   proceeding. Design constraint for the seize/invalidate verbs.
5. **Single-actor lock.** While an override runs, other overrides on
   the same target are refused, so two operators can't stomp on each
   other mid-incident. The saga executor's per-op state gives the
   substrate; explicit cross-override locking is a design target.
6. **No silent retries.** An override that fails mid-execution (e.g.
   witness unreachable) fails loudly. It never silently queues.

---

## Override: Decommission stuck-LMS holder — **Built**

**When needed.** A node held `tag.lms = 1` on a witness slot and then
died (or was partitioned away permanently) without ever writing
`tag.lms = 0`. Per INV-7, that slot reads `lms = 1` forever from the
cluster's point of view. The takeover protocol
(`cluster-quorum-spec.md` step 2) refuses for every surviving peer
until the cluster stops treating the dead node as a member.

**Why decommission is the right escape.** The slot-read logic ignores
any witness slot for a node not in the rqlite `nodes` table.
`witness.drain_replies` filters every reply against `ws.member_ids`
(refreshed each netd tick from rqlite via
`cluster_state.load_cluster()`), so a slot whose `node_id` is not a
current member is dropped before it can count. Removing the dead node
from membership — the routine `node leave` saga — therefore also
removes the LMS-veto without ever touching the witness. The witness is
a passive last-write slot store with no expiry, so the dead node's
encrypted slot stays there indefinitely; the cluster simply stops
looking at it once that `node_id` leaves `ws.member_ids`.

**Why there is no "clear LMS bit" verb.** Writing `tag.lms = 0` on the
dead node's behalf means impersonating it at the witness — either via
a privileged cross-node write (INV-2 forbids cross-node slot writes)
or via key material only the dead node holds. Neither is correct: the
LMS bit is *evidence the dead node had set itself solo master*, and
that evidence cannot be safely retracted on its behalf. The only safe
move is to stop counting the dead node as a member.

**What the operator must verify before running this.** All of:
- The LMS-holder is genuinely gone, not merely unreachable from one
  vantage point (hardware confirmed dead, scrapped, or fenced and
  never coming back).
- Accepted risk: any data the dead LMS-holder wrote after the
  cluster's last successful DRBD sync to it is lost. This is likely —
  LMS was set precisely because the surviving peer was already gone
  when the dead node went solo.
- A surviving node holds the DRBD `cluster` singleton resource in a
  usable state (`UpToDate`, even if behind the lost LMS-holder's last
  writes).

**CLI.**

```
bedrock node leave <target-node> [--reason "<free-text>"]
```

The positional `<target-node>` names the dead node; `--reason`
(default `"leave"`) is the audit string carried into the `operations`
row. There is no separate `--accept-data-loss` flag — `node leave` is
the same verb used for any decommission; the data-loss acceptance is
the operator's pre-flight judgement above. The command runs the
`node_leave` saga on the current master.

**What the saga does** (`bedrock_d/install/node_leave.py`,
`@saga("node_leave")`, crash-resumable, each step idempotent):

```
validate_target ─→ rqlite_node_unregister ─→ rqlite_voter_remove
     │ self?→raise        │ skip if already_gone   │ skip if already_gone
     │ absent?→already_gone│                        │ no voter_id?→warn+skip
     ▼
propagate_daemon_config ─→ stop_remote_services ─→ verify_membership_drop
     (bump_revision)         (best-effort SSH)       (poll strong-read ≤5s)
```

1. `validate_target` — looks the target up; raises if it equals the
   master (a master can't leave itself); sets `already_gone` if the
   target is already absent (clean re-run); else records
   `target_host`, `target_loopback`, `target_voter_id` (loopback last
   octet).
2. `rqlite_node_unregister` — master single-writer removes the row
   from the `nodes` table; Raft replicates.
3. `rqlite_voter_remove` — `curl -X DELETE https://127.0.0.1:4001/remove`
   (mTLS) with `{"id": voter_id}` drops the dead node's Raft voter
   slot, so consecutive leaves can't strand quorum at `N/2` live
   voters. A gone voter returns 200 OK (idempotent).
4. `propagate_daemon_config` — bumps the cluster revision so every
   node's rqlite subscriber regenerates `daemon.toml` and drops the
   leaver from its peer set.
5. `stop_remote_services` — best-effort SSH to stop the leaver's
   bedrock units and `rm -f /run/bedrock-no-quorum`. Non-fatal: if the
   host is unreachable, the witness slot ages out naturally.
6. `verify_membership_drop` — polls a strong rqlite read up to ~5 s to
   confirm the target has left `nodes`.

**The takeover unblock.** Once `rqlite_node_unregister` lands, every
surviving node's next netd tick refreshes `ws.member_ids` from rqlite;
the dead node's `node_id` is absent from the member set, so its stale
`lms = 1` slot is dropped in `drain_replies` regardless of the tag.
Takeover can then proceed. This dependency is implemented in
`witness.py:drain_replies` and plumbed via `member_ids` each tick.

---

## Override: Re-key witness identity — **Design**

**When needed.** Any of:
- The witness lost state (ESP32 reboot, fileshare corruption) and the
  cluster needs a deterministic clean start rather than worst-case
  limbo.
- After a stuck-LMS decommission, the operator wants the witness to
  actually drop the old slot instead of relying on the membership
  filter to ignore it.
- `cluster.key` is suspected compromised.

**Mechanism.** Generate a new `cluster_uuid` and/or `cluster.key`,
distribute to every surviving member, restart witness heartbeats on
all nodes with the new identity. Old slots — encrypted under the old
`cluster_uuid`/key — become un-decryptable by any current member, so
they are effectively cleared from the cluster's view. The witness
re-populates the new-identity slot map within a few seconds of the
first fresh heartbeats.

**Proposed CLI.**

```
bedrock cluster rekey-witness --reason "<free-text>"
```

Intended order: authenticate operator → confirm a quorum is reachable
(so the new key distributes atomically) → generate new
`cluster_uuid` (+ optional new `cluster.key`) → record audit → push to
all reachable nodes over the existing secure peer-to-peer channel →
each node atomically swaps its in-memory identity and restarts witness
heartbeats.

**Fileshare-witness variant.** For an SMB/NFS/S3-backed witness, the
equivalent is `rm` on every `slot-*.bin`; slot files re-populate from
heartbeats within seconds. Lower-overhead than a full re-key when the
only goal is clearing stale slot state.

**Failure modes to handle.**
- A surviving node unreachable during distribution keeps the old key,
  sees no slots under its query, and goes out of sync — the operator
  must reach it manually and apply the new key.
- `cluster_uuid` may be referenced elsewhere (backups, audit logs); a
  re-key creates a before/after boundary in those records.

---

## Override: Seize master with stale data — **Design**

**When needed.** The up-to-date master died and is not coming back
(hardware lost, datacenter event). A surviving node has stale data —
its UUID is in the dead master's history; some peer writes are
unrecoverable. The eligibility check (`cluster-quorum-spec.md` INV-5 +
history rule) refuses promotion of a stale node forever, by design.

**Conceptual parallel.** Active Directory FSMO role *seize* onto a
lagging domain controller.

**Proposed CLI.**

```
bedrock cluster seize --reason "<free-text>" --accept-data-loss-from <peer-name>
```

Needs the reversibility check (principle 4) wired first: before
seizing, probe whether the up-to-date peer is reachable by any
alternate path, and surface that to the operator.

---

## Override: Force `drbdadm invalidate` — **Design**

**When needed.** Two nodes have DRBD generations that diverged —
neither's current UUID is in the other's history — so DRBD refuses to
auto-sync and the operator must pick the winner.

**Proposed CLI.**

```
bedrock storage invalidate --tier <name> --on <node> --in-favor-of <peer>
```

---

## Override: Force-clear no-quorum marker — **Design**

**When needed.** `/run/bedrock-no-quorum` was created in error (test
misfire, transient bug) and now blocks recovery even though the
cluster is healthy.

**Mechanism that exists today.** The marker is the sticky no-quorum
file. `election.set_no_quorum_marker(reason)` drops it (once per
episode — its mtime is the quorum-loss timestamp the vm_failover
suspend timer reads), and `election.clear_no_quorum_marker()` removes
it. On quorum return netd's orchestrator clears it automatically. An
operator-facing verb to force-clear it on a single node does not yet
exist.

**Proposed CLI.**

```
bedrock node clear-no-quorum --node <name> --reason "<free-text>"
```

---

## Override: Collapse to single-node mode — **Design**

**When needed.** A 2-node cluster permanently lost its peer (hardware
gone, never returning) and the operator wants to collapse to N=1
operation rather than run degraded forever.

**Building blocks that exist today.** `bedrock storage demote` already
takes the DRBD-replicated `cluster` singleton back to a local LV
(safe to run on the last surviving node), and `bedrock node leave`
removes the dead peer from rqlite and drops its Raft voter. The
missing piece is a single guided saga that sequences voter-set
shrink + DRBD single-replica reconfigure + membership removal with the
right confirmations; the blast radius is wide enough to warrant one.

---

## Override: Re-key cluster CA (TLS) — **Design**

**When needed.** The cluster CA private key is suspected compromised,
or the operator rotates it deliberately. (Per-node certs are
100-year; rotation is explicit, never time-triggered.) Same mechanism
issues fresh per-node certs under a new CA without a full re-install.

**Why it's load-bearing.** rqlite has no hot-reload for TLS certs (no
`-reload` flag, no SIGHUP). Any CA rotation therefore restarts
`bedrock-rqlited` on every node plus `bedrock-rqlited-arbiter` on the
master. That restart must be **rolling and quorum-aware** — restarting
two voters at once in a 3-voter set (2 per-node + 1 arbiter) drops to
1 voter, i.e. no quorum, i.e. control plane offline.

**Intended saga shape.**
1. Generate new CA (key+cert) into a staging path on the master.
2. Re-sign every node cert under the new CA; distribute over the
   *existing* CA's transport while the rotation runs.
3. Each node writes the new cert + new CA cert to staging.
4. Rolling restart — one voter at a time, wait for Raft to re-form
   quorum, then the next.
5. Promote staging files to live paths atomically.
6. Verify every node + arbiter healthy under the new CA.
7. Delete the old CA + old certs.

This is the only operational reason to need the rolling-restart
machinery; routine joins/leaves don't (CA-signed certs are trusted
without bundle changes).

---

(Add new override sections above this line as they get specified.)
