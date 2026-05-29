# bedrock_d/orchestrator/self_heal.py

Replica repair after a host is permanently lost (SG-05). A calm loop that
runs **only on the mgmt-master** watches for nodes that have been mesh-
unreachable continuously for a calm-down period (default 65 min) and rebuilds
the redundancy those nodes were providing — **one resource per pass**, in a
locked priority order, never pushing any target node past 80 % disk usage.
The leader reads cluster state from rqlite, plans exactly one repair, and
submits a `replica_repair` saga. `self_heal_task()` is the asyncio entrypoint
launched by the mgmt/orchestrator side of `bedrock-d`; the pure planning +
disk-gate functions have no I/O and are unit-tested directly.

## Functions / Classes

### `fits_under_gate(used_bytes, total_bytes, add_bytes, gate=DISK_GATE) -> bool`
True iff placing `add_bytes` on a node currently at `used_bytes/total_bytes`
keeps it at or under `gate`.
- **In:** `used_bytes`/`total_bytes`/`add_bytes` ints; `gate` fraction (0.80).
- **Out:** bool. A node with `total_bytes <= 0` (unknown capacity) never fits —
  refuse rather than guess. No side effects.

### `pick_target(*, current_peers, candidates, add_bytes, usage, gate=DISK_GATE) -> Optional[str]`
Choose the node to host a new replica.
- **In:** `current_peers` — nodes already holding the resource (excluded);
  `candidates` — active non-lost nodes in deterministic order; `add_bytes` —
  size charged to the target (actual thin usage); `usage` — `node_name →
  {"used_bytes","total_bytes"}`; `gate`.
- **Out:** first candidate that doesn't already hold the resource AND stays
  under the gate, else `None` (resource stays degraded). No side effects.

### `compute_repair_plan(*, active_nodes, lost_nodes, singleton_peers, vm_resources, usage, resource_sizes, gate=DISK_GATE) -> dict`
Decide the single next repair action in the locked priority order. Pure.
- **In:** `active_nodes` — present + healthy, deterministically ordered;
  `lost_nodes` — permanently lost (gone ≥ calm-down); `singleton_peers` —
  nodes hosting the `cluster` resource; `vm_resources` — one dict per
  replicated VM disk `{"resource","vm_name","vm_type","priority","peers"}`;
  `usage` — per-node disk; `resource_sizes` — `resource_name → actual thin
  bytes` charged to the target.
- **Out:** one of
  `{"action":"repair","resource","kind","target","reason"[,"vm_name"]}`,
  `{"action":"none"}`, or
  `{"action":"degraded","resource","kind","reason"[,"vm_name"]}`. No side
  effects.

### `node_disk_usage(host) -> dict`
A node's bedrock thinpool usage over SSH.
- **In:** `host` — SSH-reachable address.
- **Out:** `{"used_bytes","total_bytes"}` summed over thin-pool LVs
  (`lv_attr` starting `t`); usage = `lv_size * data_percent/100`. Empty dict if
  unreachable or no capacity (treated as "doesn't fit"). Side effect: one
  `ssh root@host lvs ...` subprocess.

### `resource_thin_usage(host, data_lv, vg) -> int`
Actual allocated bytes of a resource's thin data LV on a current holder.
- **In:** `host`; `data_lv` — the resource's data LV name; `vg` — volume group.
- **Out:** `int` (`lv_size * data_percent/100`); `0` if unreadable. Side effect:
  one `ssh root@host lvs vg/data_lv` subprocess.

### `self_heal_task()` *(async)*
Leader-only calm loop: detect permanent host loss and submit one
`replica_repair` saga per pass.
- **In:** none.
- **Out:** never returns (loops on `TICK_S` = 30 s). Each tick checks
  leadership; non-leader ticks clear the in-process down-since timers. On a
  leader tick, runs `_self_heal_pass` in a worker thread. Side effects flow
  through the pass (rqlite reads, SSH, saga submit). Exceptions per tick are
  logged, not raised.

### Private helpers
- `_want_replicas(vm_type)` — `cattle→1`, `pet→2`, `vipet→3`, else 1.
- `_rqlite()` — import `lib.rqlite_client`, adding `/usr/local/lib/bedrock` to
  `sys.path` if needed.
- `_ssh(host, cmd, timeout=20)` — `ssh root@host` with
  `StrictHostKeyChecking=no`, `UserKnownHostsFile=/dev/null`, `ConnectTimeout=8`;
  returns stdout on rc 0 else `""`.
- `_load_cluster_state()` — read `nodes`, `vms` (pet/vipet only),
  `drbd_resources`, and the `cluster` row of `tiers` from rqlite (level
  `none`); returns `active_nodes`, `node_hosts`, `singleton_peers`,
  `vm_resources`, `data_lvs`. Raises on rqlite error so the caller retries.
- `_vm_name_from_resource(resource)` — `vm-<name>-disk<N> → <name>`; `""` for
  non-VM resources.
- `_self_node_name()` — `node_name` from `/etc/bedrock/state.json`, else `""`.
- `_is_leader(self_name)` — true iff `cluster_info.mgmt_master == self_name`
  (rqlite level `none`); false on any error.
- `_peers_down_now(max_age_s=PEER_DOWN_S)` — set of peers the mesh layer
  currently sees as down, via `vm_failover._peers_observed_down`.
- `_self_heal_pass(self_name, down_since)` — one leader pass (see below).
- `_measure_resource_sizes(state, lost, node_hosts)` — charge each repairable
  resource by `resource_thin_usage` on a surviving holder (VG from
  `bedrock_d.vm.lvm.VG_NAME`, default `bedrock`).
- `_submit_repair(self_name, plan)` — submit one `replica_repair` saga and run
  it inline.

## How it works

**Loop cadence + ownership.** `self_heal_task` wakes every 30 s. It only acts
when this node is the mgmt-master (single writer with quorum); on any non-leader
tick it clears `down_since`, re-arming the calm-down from zero. A leader change
therefore only ever DELAYS a repair, never triggers one early — the safe
direction. The pass runs in a worker thread (`asyncio.to_thread`), keeping the
SSH/rqlite I/O off the event loop.

**Permanent-loss detection.** `down_since` maps `node_name → monotonic time
first seen gone this leadership.`

```
peer gone now (mesh last_seen > PEER_DOWN_S=35s)
        │  seed down_since[n] = now (active, non-self only)
        ▼
   ... node stays gone ...
        │
  (now - down_since[n]) >= CALM_DOWN_S (65 min)  ──► n is LOST
        ▲
        └─ node returns OR leaves active set ──► down_since.pop(n)  (timer cleared)
```
"Gone right now" uses the same `PEER_DOWN_S=35 s` mesh threshold as the takeover
path, so "down" means the same thing in both places; the long `CALM_DOWN_S`
(override `BEDROCK_SELF_HEAL_CALM_S`, tests set ~60) is what actually gates a
repair. If no node has crossed the calm-down, the pass returns immediately.

**Sizing + the disk gate.** With at least one lost node, the pass gathers each
candidate's disk usage (`node_disk_usage` over SSH) and charges each repairable
resource by its **actual thin usage** on a surviving holder
(`_measure_resource_sizes`), not its provisioned maximum. The gate is a hard
invariant: a repair never pushes a target past 80 % usage. `pick_target` always
gates against the target node's own total, so a resource whose size couldn't be
read (charged 0) can only under-count that one resource — it can't bypass the
ceiling for a full node.

**Planning order.** `compute_repair_plan` picks exactly one action:

```
(1) cluster singleton  → restore toward min(SINGLETON_CAP=3, #candidates)-way
(2) pets   (want 2-way) → high → normal → low → resource name
(3) vipets (want 3-way) → high → normal → low → resource name
```

It checks the singleton first; if degraded and a target fits, return that
repair, else `degraded`. Otherwise it walks VM resources in `order_key` (pets
before vipets, then HA priority, then resource name), skips whole ones (non-lost
peers ≥ wanted count), and returns the first that fits a target. If something
needed repair but nothing fit, the first such case is remembered and returned as
`degraded` only after the full sweep — so a fitting lower-priority repair is
never starved by an unplaceable higher one. If nothing needed repair, `none`.

**Acting on the plan.** `none` → return. `degraded` → log a warning (operator
must add capacity/a node) and return; the resource stays degraded and the
dashboard surfaces it. `repair` → `_submit_repair` submits one `replica_repair`
saga (`RqliteSagaBackend` + `SagaExecutor`, `requested_by="self_heal"`) and runs
it inline with `execute_one` so the calm loop never has two repairs racing. The
saga is rqlite-backed, so a crash mid-repair is resumed by the boot sweep. A
submit failure is logged and retried next pass.

## Why
Charging actual thin usage (not provisioned size) lets thin-overcommitted
clusters self-heal that would otherwise look full. Failing closed on unknown
capacity, biasing the gate toward "stays degraded", and re-arming the calm-down
on every leader change all push the same direction: on a "only the paranoid
survive" platform, the safe error is to wait, never to act unsafely.
