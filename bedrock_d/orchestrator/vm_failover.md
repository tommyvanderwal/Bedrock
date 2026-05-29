# bedrock_d/orchestrator/vm_failover.py

The pet/vipet VM failover state machine. It runs as three independent async
tasks inside `bedrock-d`'s mgmt/orchestrator loop (spawned by
`mgmt/orchestrator.start_all` via `start_failover_tasks`). It watches the local
quorum signal and the mesh peer-freshness view, suspends locally-running
pet/vipet VMs when this node loses quorum, takes over a dead peer's VMs where
this node is next in line, and kills VMs that stay down past a 5-minute budget.
Cattle VMs are ignored throughout — they have a local-only LV, no DRBD, and no
failover meaning.

## Functions / Classes

### `start_failover_tasks() -> list`
Public entry point. Spawns the three failover tasks on the current asyncio loop.
- **In:** none.
- **Out:** list of three `asyncio.Task` handles
(`suspend_on_no_quorum_task`, `takeover_after_peer_down_task`,
`kill_suspended_after_5min_task`). The loop owns them; the caller need not keep
the handles. Idempotency at the loop-task level is the orchestrator's
`_TASKS_STARTED` guard, not this function.

### `suspend_on_no_quorum_task()` (async)
Task ①. Every `TICK_S`, if the no-quorum marker is older than
`SUSPEND_AFTER_NO_QUORUM_S`, suspend every still-running local pet/vipet VM and
record it; also adopt already-paused pet/vipet VMs into the record.
- **In:** none (reads `NO_QUORUM_MARKER` mtime, queries rqlite for vm_type).
- **Out:** never returns (infinite loop). Side effects: `virsh suspend <vm>` per
running pet/vipet VM not yet in the record; writes
`/var/lib/bedrock/suspended-vms.json` (`{vm_name: quorum_loss_ts}`, tmp+rename).
The recorded timestamp is the marker mtime (quorum-loss episode start), not the
suspend time.

### `takeover_after_peer_down_task()` (async)
Task ②. Every `TICK_S`, if rqlite is quorate and any known peer's freshest mesh
`last_seen` is `>= TAKEOVER_AFTER_PEER_DOWN_S` old, take over that peer's VMs
where this node is next in `failover_order`.
- **In:** none (reads `_STATE.netd` neighbour freshness; queries `vms` table).
- **Out:** never returns. Side effects via `_takeover_one`: `drbdadm
disconnect`/`primary` per disk, UUID write-back to rqlite, `virsh start`, and a
`vms.host` update. An `in_progress` set prevents re-attempting the same VM on the
next tick while its first attempt is still running.

### `kill_suspended_after_5min_task()` (async)
Task ③. Every `TICK_S`, destroy any recorded VM whose quorum-loss timestamp is
older than `KILL_AFTER_QUORUM_LOSS_S`.
- **In:** none (reads `suspended-vms.json`).
- **Out:** never returns. Side effects: re-checks live state with `virsh
domstate`; `virsh destroy <vm>` only if the VM is still `paused`; rewrites
`suspended-vms.json` with the killed/evicted entries removed.

### `drop_suspended(vm_names: list[str]) -> None`
Remove names from the suspended-vms record so a resumed VM is no longer eligible
for the 5-minute kill. Called by the recovery path
(`mgmt/orchestrator._reconcile_paused_vms`) the moment it `virsh resume`s a VM on
quorum return.
- **In:** `vm_names` — VM names to drop.
- **Out:** `None`. Side effect: rewrites (or deletes) `suspended-vms.json` if any
named VM was present. No-op for an empty list or names not in the record.

### Private helpers
- `_now() -> float` — `time.time()`.
- `_self_node_name() -> str` — `node_name` from `/etc/bedrock/state.json`, or
`""`.
- `_load_suspended_record() -> dict[str, float]` — load the
`{vm_name: quorum_loss_ts}` map; `{}` on any error.
- `_save_suspended_record(record) -> None` — tmp+rename atomic write; an empty
record unlinks the file.
- `_virsh(*args, timeout=30.0) -> (rc, stdout, stderr)` — run a `virsh` command;
`(124, "", ...)` on timeout.
- `_virsh_domstate(vm_name) -> str` — domain state string, or `""`.
- `_local_pet_vipet_vms(states=("running",)) -> list[str]` — local VMs in the
given libvirt state(s) whose `vms.vm_type` is `pet` or `vipet` (rqlite,
level `none`).
- `_vms_on_dead_peer(dead_peer, me) -> list[dict]` — rows (`vm_name`, `vm_type`,
decoded `failover_order`) where `vms.host == dead_peer` and `peers_after_dead`
puts `me` next.
- `_vm_disks(vm_name) -> list[str]` — every `drbd_resources.name` matching
`vm-<name>-disk%`; falls back to `[f"vm-{vm_name}-disk0"]` if the query is empty.
- `_peers_observed_down(max_age_s) -> list[str]` — peers in
`netd.ever_seen_peers` whose freshest neighbour `last_seen` (monotonic) is stale
or absent; self never returned.
- `_rqlite_quorate() -> bool` — `True` iff a `level='strong'` `SELECT 1`
succeeds (a quorate leader is reachable).
- `_takeover_one(vm_name, disks, me) -> bool` — runs the full takeover sequence
for one VM (steps a–f below); `True` on success, `False` on any refusal.

## How it works

Three tasks, all on the same `TICK_S = 5.0` s cadence, each wrapped in a
try/except so one bad tick never kills the loop. The whole timeline is anchored
on the quorum-loss moment: the mtime of `/run/bedrock-no-quorum`, which netd's
election layer creates once per no-quorum episode and clears on quorum return.

```
partition / quorum loss
        │
        │  netd's election layer writes /run/bedrock-no-quorum (marker)
        │  ~9 s after partition (SELF_DEMOTE_MISSES)
        ▼
   marker_age >= SUSPEND_AFTER_NO_QUORUM_S (5 s)   ── task ① ──▶ virsh suspend
        │   → ~partition+14 s; recorded at marker mtime                pet/vipet
        │
   peer last_seen >= TAKEOVER_AFTER_PEER_DOWN_S (35 s)  ── task ② ──▶ surviving
        │   on a DIFFERENT, quorate node                              node takes
        │                                                             over
        ▼
   record_ts + KILL_AFTER_QUORUM_LOSS_S (5 min)    ── task ③ ──▶ virsh destroy
            clock runs from quorum loss, not from suspend
```

**Task ① — suspend.** Reads the `NO_QUORUM_MARKER` mtime as the quorum-loss
episode start. Once `marker_age >= SUSPEND_AFTER_NO_QUORUM_S`, it lists local
pet/vipet VMs in two states and reconciles against the record:
1. For each still-`running` pet/vipet VM not already recorded, it issues `virsh
suspend`; on success the VM is recorded at `marker_mtime`.
2. For each already-`paused` pet/vipet VM not yet recorded, it adopts it into
the record at `marker_mtime`. (`mgmt/orchestrator`'s `no_quorum_responder`
suspends VMs on its own 1 s poll but does not write this file; adoption ensures
task ③ can still see those VMs.)

Both paths anchor to `marker_mtime`, so the kill clock measures from the
connection drop regardless of which path suspended the VM or when. The marker is
created once per no-quorum episode and cleared on quorum return, so its mtime is
the partition start even across a `bedrock-d` restart mid-partition.

**Task ② — takeover.** Guarded first by `_rqlite_quorate()` (only a node that
still has quorum may take over) and by `me` being resolvable. For each peer in
`_peers_observed_down(TAKEOVER_AFTER_PEER_DOWN_S)`, it scans
`_vms_on_dead_peer(dead, me)` and runs `_takeover_one` for each VM where this
node is the immediate next entry after the dead host in that VM's
`failover_order` (`peers_after_dead`), so exactly one survivor claims each VM and
cascades respect order. An `in_progress` set keyed by VM name skips a VM whose
takeover is still mid-flight on a later tick; it's discarded in a `finally` so a
completed (or failed) attempt is retryable next tick.

`_takeover_one` per VM, over every backing disk from `_vm_disks`:
```
a. drbdadm disconnect <resource>          (per disk; failures logged, not fatal)
b. drbdadm primary <resource>             (per disk)
       └─ on failure: retry drbdadm -- --force primary <resource>
       └─ still failing → log + return False (refuse takeover)
c. record_uuid_after_promote(<resource>)  (per disk; quorum-confirmed rqlite write)
       └─ exception → return False
d. is_safe_to_start_vm(vm_name, disks)    (strong-read safety verdict)
       └─ falsy verdict → log verdict.reason + return False
e. virsh dominfo  (warn-only if undefined here), then virsh start
       └─ start failure → return False
f. bedrock_state.vm_state_change(name=vm, host=me, state="running")
       └─ write failure → logged but takeover still counts as success
```
The disconnect in step (a) terminates the inbound replication that would
otherwise refuse local writes; the `--force` primary in (b) is acceptable
because the UUID-record and `is_safe_to_start_vm` checks in (c)/(d) gate the
actual start. The sequence aborts on the first hard failure, before `virsh
start`, so a VM is never started on a disk that failed to promote or verify.

**Task ③ — kill.** Loads the record; for any entry whose `quorum_loss_ts` is
older than `KILL_AFTER_QUORUM_LOSS_S`, it first re-checks `virsh domstate`. A VM
that is no longer `paused` (e.g. resumed out-of-band) is dropped from the record
without being destroyed; only a still-`paused` VM gets `virsh destroy`. Killed
and evicted entries are removed and the record is rewritten. Normal recovery
removes a VM via `drop_suspended` long before this fires; the live re-check is
the belt-and-suspenders for the case the resume path missed it.

**Constants:** `SUSPEND_AFTER_NO_QUORUM_S = 5.0`,
`TAKEOVER_AFTER_PEER_DOWN_S = 35.0`, `KILL_AFTER_QUORUM_LOSS_S = 300`,
`TICK_S = 5.0`. Files: `NO_QUORUM_MARKER = /run/bedrock-no-quorum`,
`SUSPENDED_VMS_FILE = /var/lib/bedrock/suspended-vms.json`,
`_STATE_JSON = /etc/bedrock/state.json`.

## Why

The 15 s gap between suspend (≈T+20) and takeover (T+35) gives the dying node up
to ~5 s after the no-quorum signal to issue its own suspend, then ~10 s of
settling margin for in-flight DRBD writes to drain before the surviving node
disconnects and promotes. The kill clock runs from quorum loss (not from
suspend) so a frozen copy is freed exactly 5 minutes after the connection
dropped, by which point the VM is running elsewhere; 5 minutes aligns with the
Kerberos authentication window.
