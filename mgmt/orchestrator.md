# mgmt/orchestrator.py

The management-plane orchestrator: a set of asyncio tasks running inside the
bedrock-d mgmt process (one per node). It watches rqlite cluster state, projects
this node's role to `state.json`, and converges local services — per-VM DRBD,
libvirtd, the node's VMs, SeaweedFS, backup targets — to whatever rqlite says
they should be. It also runs the no-quorum responder, the snapshot-diff reactor,
the master-only backup scheduler, and the boot-time saga resume sweep.
`start_all()` is the entry point, called from FastAPI's startup hook in
`mgmt/app.py`; `attach_state(state)` binds it to the unified daemon's shared
state object. Nothing here acts before netd's election establishes a quorum role.

## Functions / Classes

### `attach_state(state) -> None`
Bind the unified daemon's shared state object so the subscriber, no-quorum
responder, boot, converge-retry, and backup tasks read/write through it.
- **In:** `state` → the shared `BedrockState` (has `snapshot`, `prev_snapshot`,
  `last_log_idx`, `snapshot_lock`, `netd`, `netd_lock`).
- **Out:** none. Sets module global `_STATE`; when unset, the tasks fall back to
  module-level globals.

### `get_snapshot() -> dict`
Read-only access to the live in-memory cluster snapshot, for FastAPI handlers.
- **Out:** the current snapshot dict (taken under `snapshot_lock` when attached).

### `async rqlite_subscriber()`
Task ①. Watches the rqlite `bedrock_meta.revision` counter; on each advance,
rebuilds the snapshot and reconciles local state.
- **Out:** runs forever. Per advance (via `_apply_revision`): updates the
  in-memory snapshot, writes `state.json`, reconciles observability, regenerates
  DRBD configs, runs `cluster_arbiter.converge()`, ensures the ISO-library FUSE
  mount, and schedules `_reactor_diff`. Retries every 2 s if rqlite is
  unreachable.

### `async boot_orchestrator()`
Task ②. One-shot at startup: wait for a settled quorum role, then start this
node's local services.
- **Out:** waits up to 120 s for a role. If `noquorum`/`""`/`unknown`, logs and
  returns without starting anything. Otherwise calls `_start_local_services()`
  and sets `_SERVICES_STARTED = True`.

### `async no_quorum_responder()`
Task ③. Watches `/run/bedrock-no-quorum`. On appearance, pauses VMs, then waits
for quorum to return before clearing the marker and reconciling.
- **Out:** runs forever (1 s tick). On marker: runs `_run_no_quorum_cleanup`
  (bounded by `NO_QUORUM_CLEANUP_TIMEOUT_S = 30 s`), then `_wait_for_role(..,
  ignore_marker=True)`; on a settled role, unlinks the marker, runs
  `_reconcile_paused_vms()` + `_start_local_services()`. On timeout/error it
  loops and retries; never exits.

### `async backup_scheduler()`
Task ⑤. Master-only loop, 60 s tick: fires due scheduled backups.
- **Out:** runs forever. Skips the tick unless `_is_leader()`. On a leader tick
  (`_scheduler_tick`), evaluates each VM's `backup_schedule` against
  `cron.should_fire_now` and spawns `_run_scheduled_backup` for due VMs.

### `async cluster_tier_watcher()`
Calm-loop (10 s) that promotes the `cluster` singleton from local to DRBD once
the cluster has ≥ 2 nodes.
- **Out:** runs forever. Only when this node is mgmt-master, `len(nodes) >= 2`,
  and tier `cluster` mode is not `drbd`: submits and synchronously executes the
  `cluster_tier_promote_master` saga (peer = lowest-octet non-self node) via
  `SagaExecutor`. Submits at most once per `peer@loopback` key.

### `async converge_retry()`
Timer-based (5 s) re-run of `cluster_arbiter.converge()`, so a promote that
failed during failover (peer not yet self-demoted) is retried even when no rqlite
revision advances.
- **Out:** runs forever; calls `cluster_arbiter.converge()` in a thread each tick.

### `async saga_resume()`
One-shot at boot: resume in-flight runtime saga operations owned by this node.
- **Out:** waits up to 120 s for a settled role; skips if no quorum. Lists
  inflight ops for this node via `RqliteSagaBackend.list_inflight_for`, and runs
  `SagaExecutor.execute_one` for each — skipping `_LEADER_ONLY_SAGA_KINDS`
  (`cluster_tier_promote_master`, `cluster_tier_join_peer`, `cluster_rename`,
  `replica_repair`) unless this node is the leader.

### `start_all() -> None`
Spawn every orchestrator task on the running event loop. Called from FastAPI's
startup hook.
- **Out:** under a `threading.Lock`, idempotent — the second startup hook (the
  two uvicorns on 8443 + 8001 run in separate threads) is a no-op. Creates tasks:
  `rqlite_subscriber`, `no_quorum_responder`, `boot_orchestrator`,
  `backup_scheduler`, `converge_retry`, `cluster_tier_watcher`, `saga_resume`,
  `self_heal.self_heal_task`, and `vm_failover.start_failover_tasks()`.

### Private helpers
- `_self_node_name()` → `node_name` from `state.json`, or `""`.
- `_running_vm_names()` / `_paused_vm_names()` → `virsh list --state-running /
  --state-paused --name` output as a list.
- `_vm_drbd_resource(vm)` → DRBD resource backing a VM's disk, by matching the
  `/dev/drbdN` minor in `virsh dumpxml` against `/etc/drbd.d/*.res`; `None` for
  cattle (local LV).
- `_vm_drbd_resources(vm)` → all `vm-<name>-disk*.res` resource names for a VM
  (handles multi-disk); empty for cattle.
- `_drbd_role(res)` → `'Primary'` / `'Secondary'` / `'Unknown'` via `drbdadm
  role`.
- `_bring_up_vm_drbd(vm)` → `drbdadm up` each of a VM's resources (idempotent; no
  promote).
- `_subscriber_pass` / `_apply_revision` → one subscriber lifecycle and one
  revision-apply (snapshot rebuild + projections + reactor scheduling).
- `_wait_for_role(timeout_s, ignore_marker=False)` → polls `cluster_state.
  load_cluster()` until `mgmt_master` is set; returns `'leader'`/`'follower'`/
  `'noquorum'`/`'unknown'`.
- `_start_local_services()` → brings this node's services up to the rqlite-stated
  set (read at level `strong`).
- `_run_no_quorum_cleanup()` → `virsh suspend` every running VM.
- `_reconcile_paused_vms()` → per paused VM, destroy stale copies or resume ours,
  against a strong rqlite read.
- `_reactor_diff(prev, cur, self_name)` → side-effects from prev→cur snapshot
  transitions.
- `_react_backup_target_set(tid, target)` → `kopia repository connect` for a
  target locally.
- `_is_leader()` → `True` iff rqlite (`cluster_info.mgmt_master`, level `none`)
  names this node.
- `_scheduler_tick`, `_last_scheduled_fire_time`, `_run_scheduled_backup` →
  scheduler internals.

## How it works

Each node runs all of these as concurrent asyncio tasks; the in-memory snapshot
is the shared spine. The rqlite subscriber is the only writer of the snapshot;
everyone else reads it (or reads rqlite directly for authoritative decisions).

**Subscriber → snapshot → reconcile (Task ①).** `RqliteClient.watch` polls
`bedrock_meta.revision` at ~500 ms. On each advance `_apply_revision` deep-copies
the old snapshot to `prev`, rebuilds `cur` via `view_builder.build_snapshot`, and
swaps them under `snapshot_lock`. It then projects this node's role + mgmt URL to
`state.json` (atomic write), and runs a fixed reconcile chain — each step wrapped
so one failure does not block the rest:

```
revision advance
   ├─ state.json projection      (_state_view → _atomic_write_json)
   ├─ observability.reconcile     (vmagent/vlagent, conditional VM/VL)
   ├─ tier_storage.regen_drbd_configs_from_snapshot   (mesh path change)
   ├─ cluster_arbiter.converge()  (.254 VIP, arbiter rqlite, filer/s3; idempotent)
   ├─ seaweedfs.ensure_iso_library_mount()  (mount follows the master /32)
   └─ schedule _reactor_diff(prev, cur)   (call_soon_threadsafe → create_task)
```

**Boot ordering (Task ②).** `boot_orchestrator` waits for `_wait_for_role`, then
`_start_local_services` does, in order: `drbdadm up` every per-VM resource this
node hosts (so `/dev/drbdN` exists before libvirtd opens it — promote is left to
the failover/create paths to avoid dual-primary), start per-node SeaweedFS via
`promote_to_master_volume_host`, start libvirtd, `virsh start` each VM that rqlite
says is `host == self && state == running`, and connect each backup target. All
reads here use rqlite level `strong` — a stale local replica could otherwise say
a peer's taken-over VM still lives here and trigger a split-brain `virsh start`.

**No-quorum cycle (Task ③).** The marker file is dropped by netd's election when
this node loses quorum.

```
/run/bedrock-no-quorum appears
   │
   ├─ _run_no_quorum_cleanup      virsh suspend all running VMs   (≤30 s)
   │      (DRBD is NOT demoted here — qemu's open FDs would EBUSY)
   │
   ├─ _wait_for_role(ignore_marker=True)
   │      gate on mesh peer-liveness (any neighbour logged_up), then a
   │      STRONG rqlite read of mgmt_master — not on the election outcome,
   │      which stays NO_QUORUM while we still hold the marker (circular)
   │
   ├─ role settled (leader/follower) → unlink marker
   │      else: sleep 10 s and loop (never return — that would strand
   │      the marker + paused VMs)
   │
   ├─ _reconcile_paused_vms       per paused VM, STRONG read of vms:
   │      not in log / host != self → virsh destroy + drbdadm secondary
   │      host == self & running    → virsh resume (+ vm_failover.drop_suspended)
   │
   └─ _start_local_services       re-promote this node's services
```

**Reactor (Task ④).** `_reactor_diff` runs only after `_SERVICES_STARTED` — the
boot path covers anything seen during catch-up. It derives transitions from
`prev`→`cur`: VMs gone from `cur.vms` get `virsh destroy` + `undefine`; VMs whose
`host` changed get `virsh start` (now ours) or `virsh destroy` (moved away); new
or reconfigured `backup_targets` get a `kopia repository connect`. Critical-tier
DRBD master transitions are not handled here — they belong to
`cluster_arbiter.converge()`, run from the subscriber.

**Scheduler (Task ⑤).** `backup_scheduler` short-circuits on every non-leader
tick because the leader is the single log writer; scheduling against any other
node would double-fire or fail to append. `_scheduler_tick` evaluates each VM's
`backup_schedule` with `cron.should_fire_now` (60-min grace), using
`_last_scheduled_fire_time` (parsed from the newest matching `<prefix>-…` backup
label) as last-fired, and guards re-entry with the `_SCHEDULED_INFLIGHT` set.

## Why
Authoritative recovery decisions ("did failover happen — resume here or destroy
the stale copy?") read rqlite at level `strong` to force a Raft round-trip,
because a just-partitioned node's local replica lags and a stale `vms.host` would
split-brain. The no-quorum responder gates on mesh peer-liveness rather than the
election outcome, which is circular while this node still holds the marker.
`start_all` uses a real lock because two uvicorns (8443 + 8001) fire the FastAPI
startup hook from separate threads, and a naive flag would let every task start
twice.
