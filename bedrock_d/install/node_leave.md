# bedrock_d/install/node_leave.py

The `node_leave` saga: the master-orchestrated, crash-resumable flow that cleanly
removes a target node from the cluster. It runs on the current **master** in
response to `bedrock node leave <target>` and walks an ordered, idempotent
sequence — drop the target from rqlite, drop its Raft voter slot, refresh peer
config, stop the target's services, and confirm the removal. It is registered as
`@saga("node_leave")` and driven by the shared `SagaExecutor`; its public entry
point is `run_node_leave`. Re-running against an already-removed node is a string
of no-ops.

Cluster-DRBD re-balancing — re-seeding the `cluster` singleton's 3-peer set when
the leaver was carrying it — is **not** done here. `node_leave` only bumps the
rqlite revision; the calm orchestrator picks up the under-redundant set on its
next reconcile.

## Functions / Classes

### `class NodeLeave` — `@saga("node_leave")`
Master-orchestrated removal of `<target>`, expressed as ordered `@step` methods on
a shared `ctx` dict. The executor runs the steps in declaration order, persisting
progress between each.
- **ctx inputs** (from `run_node_leave` params): `target_name`, `reason` (default
  `"leave"`), `self_name` (the master running the saga).
- **ctx outputs** derived along the way: `target_host` (LAN IP), `target_loopback`
  (loopback `/32`), `target_voter_id` (loopback last octet → Raft voter id), and
  the guard flag `already_gone`.

Steps, in order (each takes `ctx`, returns nothing):

- **`step_validate` (`validate_target`)** — Looks the target up in the snapshot.
  Raises `RuntimeError` if `target_name == self_name` (a master cannot leave
  itself). If the target is absent, sets `ctx["already_gone"]=True` (idempotent
  re-run, no failure). Otherwise records `target_host`, `target_loopback`, and
  `target_voter_id` (only if the last octet is numeric, else empty string).
  - **In:** `ctx` with `target_name`, `self_name`.
  - **Out:** mutates ctx; reads `lib.cluster_state.load_cluster()`; no external
    side effects.

- **`step_unregister` (`rqlite_node_unregister`)** — Writes the `node_unregister`
  row to rqlite (master single-writer; Raft replicates). Skipped if `already_gone`.
  - **In:** `ctx["target_name"]`, `ctx["reason"]`.
  - **Out:** calls `bedrock_d.state.node_unregister(...)`, appends an rqlite row,
    returns a revision (logged). Duplicate unregisters are a harmless append.

- **`step_voter_remove` (`rqlite_voter_remove`)** — Drops the leaver's Raft voter
  slot. Skipped if `already_gone`; warns and returns if no numeric
  `target_voter_id` was derivable.
  - **In:** `ctx["target_voter_id"]`.
  - **Out:** runs `curl -X DELETE https://127.0.0.1:4001/remove` (mTLS:
    `/etc/bedrock/node.crt`, `node.key.pem`, `ca.crt`) with body
    `{"id": <voter_id>}`, 10 s timeout. Non-zero rc logged as a warning, not raised
    (server-side `/remove` is itself idempotent — a gone voter is a 200 OK no-op).

- **`step_propagate` (`propagate_daemon_config`)** — Bumps the cluster revision so
  every node's rqlite subscriber regenerates its own daemon config and drops the
  leaver from its peer set.
  - **In:** none beyond ctx.
  - **Out:** opens `bedrock_d.state.RqliteClient()` and calls
    `bedrock_d.state.bump_revision(client)`. Failures are caught and logged as a
    warning (non-fatal).

- **`step_stop_remote` (`stop_remote_services`)** — Best-effort SSH into the leaver
  to stop its bedrock units. Skipped if `already_gone` or no `target_host`.
  - **In:** `ctx["target_host"]`.
  - **Out:** runs `ssh root@<host>` (StrictHostKeyChecking=no,
    UserKnownHostsFile=/dev/null, BatchMode=yes, ConnectTimeout=5, 20 s timeout)
    executing `systemctl stop bedrock-d bedrock-rqlited bedrock-rqlited-arbiter`
    and removing `/run/bedrock-no-quorum`. Non-zero rc logged as a warning, not
    raised — the witness slot ages out naturally otherwise.

- **`step_verify` (`verify_membership_drop`)** — Confirms the target is gone.
  Skipped if `already_gone`.
  - **In:** `ctx["target_name"]`.
  - **Out:** polls `lib.cluster_state.load_cluster(level="strong")` up to 10× at
    0.5 s (≈5 s). Logs success when the target leaves `nodes`; logs a warning if
    still present after the window. Never fails the saga.

### `run_node_leave(*, target_name, reason="leave", self_name=None) -> None`
Entry point that submits or resumes the `node_leave` saga and runs it to
completion. Called by the master for `bedrock node leave`.
- **In:** `target_name` (node to remove); `reason` (audit string); `self_name`
  (defaults to `socket.gethostname()`, overridable for tests).
- **Out:** returns `None` on success; raises `RuntimeError` if the saga ends in a
  non-`COMPLETED` state (message names `result.last_step` and `result.error`).
  Side effects: ensures `/var/lib/bedrock/init-progress.json`'s parent dir exists;
  reads/writes that file through `FileSagaBackend`; runs the saga via
  `SagaExecutor`. `requested_by` is taken from `$SUDO_USER` / `$USER`, else
  `"operator"`.

## How it works

`run_node_leave` shares the bootstrap progress file
`/var/lib/bedrock/init-progress.json` with init/join (distinguished by
`kind="node_leave"`), so an operator has one place to read "the last big op". It
scans existing ops for a non-`completed` `node_leave` whose `params.target_name`
matches this target — matching on target avoids hijacking a concurrent leave of a
different node. If one is found it resumes (`retry` for a `failed` op, otherwise
`execute_one`); if not, it `submit`s a fresh op (`target_node=self_name`) and runs
it.

```
run_node_leave(target)
  │
  ├─ scan init-progress.json for in-flight/failed node_leave(target)
  │     found? ── failed ─→ executor.retry(id)
  │              └ other ─→ executor.execute_one(id)
  │     none ──→ executor.submit(...) ─→ execute_one(id)
  │
  ▼ saga steps (resume from last incomplete; each idempotent)
  validate_target ─→ rqlite_node_unregister ─→ rqlite_voter_remove
       │ self?→raise          │ skip if already_gone   │ skip if already_gone
       │ absent?→already_gone │                         │ no voter_id?→warn+skip
       ▼
  propagate_daemon_config ─→ stop_remote_services ─→ verify_membership_drop
       (bump_revision)          (best-effort SSH)       (poll strong-read ≤5s)
```

The load-bearing ordering is **unregister-then-drop-the-voter**. Writing
`node_unregister` removes the node from the cluster's view, but the leaver's
offline `rqlited` would still count as a Raft voter; `rqlite_voter_remove` is what
keeps the quorum math honest, so repeated leaves cannot strand the cluster at
`N/2` live voters. Both writes are idempotent (duplicate unregister = harmless
append; `/remove` of a gone voter = 200 OK no-op), which is what lets a crashed
saga safely re-run from the start.

Guards: `already_gone` (set when the target is missing at validation)
short-circuits every later step, so a re-run after a partial success is a clean
string of no-ops. The later steps are deliberately tolerant — a missing
`target_voter_id`, a non-zero `/remove`, a failed `bump_revision`, an SSH failure,
or a slow verify poll all log warnings rather than aborting, because by that point
the cluster has already removed the node from its authoritative state and the
remaining work is convergence the cluster reaches on its own (witness slot ageing,
subscriber catch-up).

## Why

The unregister and the voter `/remove` are split into two persisted steps because
the failure between them is the dangerous one: a master that records the unregister
and then dies leaves stale Raft membership that can brick quorum on the next leave.
Making each a resumable, idempotent saga step keeps that window recoverable rather
than silent.
