# `mgmt/orchestrator.py`

**Module purpose.** The asyncio task set that runs inside the
`bedrock-mgmt` FastAPI process on every node. Bridges the rqlite
canonical state to local-disk effects:

- Polls `bedrock_meta.revision` and rebuilds `cluster.json` +
  `state.json` whenever it advances.
- Calls `cluster_arbiter.converge()` on every revision tick AND
  every 5 s timer (so transient promote failures, e.g. DRBD
  primary refused while the isolated old master is still primary,
  get retried).
- Watches `/run/bedrock-cluster.fence` and runs cleanup
  (pause VMs) when it appears; waits for quorum to return before
  clearing the marker.
- Snapshot-diff reactor: on each revision transition,
  derives "vm host changed", "vm destroyed", "backup_target added"
  events from `prev → cur` snapshot diff and runs the matching
  side-effects.

`start_all()` is the entry point called from `mgmt/app.py`'s
FastAPI startup hook. It spawns four tasks: `rqlite_subscriber`,
`fence_responder`, `boot_orchestrator`, `backup_scheduler`,
`converge_retry`.

## Constants

- `CLUSTER_JSON = /etc/bedrock/cluster.json`,
  `STATE_JSON = /etc/bedrock/state.json`.
- `FENCE_MARKER = /run/bedrock-cluster.fence` — written by netd's
  election layer on NoQuorum; cleared here after cleanup +
  quorum recovery.
- `FENCE_CLEANUP_TIMEOUT_S = 30.0` — cap on
  `_run_fence_cleanup`; the independent
  `bedrock-fence-watchdog` timer reboots after 5 min.

## Globals

- `_SNAPSHOT` — live in-memory snapshot. Initialised empty,
  refreshed by `_apply_revision` on every rqlite revision change.
- `_LAST_LOG_IDX` — last seen rqlite revision (`bedrock_meta.revision`).
- `_PREV_SNAPSHOT` — previous tick's snapshot, used by
  `_reactor_diff` to compute transitions.
- `_SERVICES_STARTED` — gate that suppresses the reactor until
  the boot path has finished bringing up local services.

## Functions

### Helpers

- `_self_node_name() -> str` — short read of
  `state.json["node_name"]`.
- `_running_vm_names() / _paused_vm_names()` — `virsh list
  --state-{running,paused} --name`.
- `_vm_drbd_resource(vm_name) -> str | None` — parses `virsh
  dumpxml` for `/dev/drbdN`, maps to `/etc/drbd.d/*.res` to find
  the resource name. Used by the unfence path to know which DRBD
  resource to drop to secondary when destroying a paused VM.
- `_drbd_role(resource) -> str` — `Primary` / `Secondary` /
  `Unknown`.
- `get_snapshot() -> dict` — read-only access to `_SNAPSHOT` for
  FastAPI handlers.

### ① rqlite_subscriber

- `rqlite_subscriber()` — outer wrapper around `_subscriber_pass`
  with reconnect on RqliteError; runs forever.
- `_subscriber_pass(self_name)` — opens an rqlite client + a
  `watch()` generator on `bedrock_meta.revision`; for each yielded
  revision calls `_apply_revision`.
- `_apply_revision(rc, revision, self_name)` — pure write
  cascade:
  1. `view_builder.build_snapshot(client=rc)` → new snapshot dict.
  2. Atomic-write `cluster.json` (cluster-wide projection).
  3. Atomic-write `state.json` (this-node projection: role,
     loopback_ip, mgmt_url, witness_host).
  4. `observability.reconcile(snapshot, self_name)` — converge
     local vmagent/vlagent and (on the obs_backends nodes)
     vmsingle/vlsingle to the snapshot.
  5. `tier_storage.regen_drbd_configs_from_snapshot(snapshot)` —
     re-render DRBD .res files on mesh path-table changes so
     DRBD uses bedrock-net's chosen paths.
  6. `cluster_arbiter.converge()` — promote/demote singletons
     based on `state.json["role"]`.
  7. `seaweedfs.ensure_iso_library_mount()` — re-render the
     `/mnt/isos` FUSE mount unit when the master moved.
  8. Schedule `_reactor_diff(prev, cur, self_name)` on the event
     loop.

### ② boot_orchestrator

- `boot_orchestrator()` — one-shot at mgmt startup. Calls
  `_wait_for_role(120s)` to wait until cluster.json has a settled
  `mgmt_master`. If timeout → role="unknown", logs and exits
  (the watchdog will reboot). Else calls `_start_local_services()`
  and sets `_SERVICES_STARTED = True`.
- `_wait_for_role(timeout_s) -> str` — poll cluster.json until
  `mgmt_master` is set. Returns "leader" if we are master,
  "follower" otherwise. Returns "fenced" if the marker is
  present, "unknown" on timeout.
- `_start_local_services()` — starts libvirtd, starts running
  VMs that the snapshot says belong here, runs
  `backup.configure_target_locally` for each registered
  backup_target so kopia is connected on boot.

### ③ fence_responder

- `fence_responder()` — watches `FENCE_MARKER` once per second.
  On appearance:
  1. `_run_fence_cleanup()` with FENCE_CLEANUP_TIMEOUT_S cap.
  2. **Wait for quorum to return** via
     `_wait_for_role(120s)` — without this, the election
     re-flagged NoQuorum next tick and we'd flap.
  3. Clear the marker.
  4. `_reconcile_paused_vms()` — destroy stale paused copies
     of VMs the log says moved, resume the rest.
  5. `_start_local_services()` again.
- `_run_fence_cleanup()` — for each running VM: `virsh suspend`.
  Doesn't demote DRBD here (qemu's open FD would EBUSY); that's
  done in `_reconcile_paused_vms` after destroying stale copies.
- `_reconcile_paused_vms()` — for each paused VM: if cluster
  state says it moved to another host, `virsh destroy` + `drbdadm
  secondary` the resource; if it's still ours, `virsh resume`.

### ④ reactor

- `_reactor_diff(prev, cur, self_name)` — derives transitions
  from snapshot pairs and dispatches:
  - VMs in `prev.vms` but not `cur.vms` → `virsh destroy +
    undefine`.
  - VMs whose `host` changed → if now ours, virsh start; if
    moved away, virsh destroy local copy.
  - backup_targets that appeared/changed → `_react_backup_target_set`.
  (Critical-tier DRBD master transitions are owned by
  `cluster_arbiter.converge`, not the reactor.)
- `_react_backup_target_set(target_id, target)` — kopia
  repository connect via `backup.configure_target_locally`.

### Periodic converge

- `converge_retry()` — every 5 s, call `cluster_arbiter.converge()`
  unconditionally. Catches the case where `_apply_revision`'s
  converge failed (e.g. DRBD primary refused because the
  isolated old master is still primary) and no new rqlite
  revision arrives to re-trigger.

### Backup scheduler

- `backup_scheduler()` — runs only on the mgmt master (gated by
  `_is_leader()`); polls per-VM backup schedules and fires
  `_run_scheduled_backup(vm, target_id, sched)` via
  `mgmt/backup.py`.
- `_scheduler_tick()`, `_last_scheduled_fire_time`,
  `_run_scheduled_backup` — implementation details.
- `_is_leader() -> bool` — `mgmt_master == self_node_name()`.

## Lifecycle invariants

- `_SERVICES_STARTED` gates the reactor so the first
  rqlite-revision tick at boot doesn't fire side-effects
  before `boot_orchestrator` has had a chance to bring up the
  local services that the reactor depends on.
- `cluster_arbiter.converge()` is called from BOTH
  `_apply_revision` (revision-driven) AND `converge_retry` (5 s
  timer). Idempotent — a no-op when state is already correct,
  retries the failed step otherwise.
- The fence marker is cleared ONLY by `fence_responder` after
  cleanup + quorum return. netd's election keeps re-writing it
  while NoQuorum persists.
