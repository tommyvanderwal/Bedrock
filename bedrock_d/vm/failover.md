# bedrock_d/vm/failover.py

Pure, side-effect-light helpers for the pet/vipet VM failover path. The
state machine and side-effecting orchestration live in
`bedrock_d/orchestrator/vm_failover.py`; this module is the building blocks
those tasks call: read a DRBD resource's current-UUID off local disk, look up
the cluster's last-recorded UUID in rqlite, decide whether this node is next
in line after a dead primary, write the post-promote UUID back through quorum,
and run the pre-start safety check that guards against starting a VM on stale
data.

## Functions / Classes

### `peers_after_dead(failover_order, me, dead_host) -> bool`
True if `me` is the entry immediately after `dead_host` in the predetermined
failover sequence. Pure list arithmetic, no I/O.
- **In:** `failover_order` — ordered list of hostnames (from `vms.failover_order`); `me` — this node's name; `dead_host` — the host that died.
- **Out:** `bool`. False if either name is absent from the list, if the list is empty (cattle, no failover), or if `me` is not the next index after `dead_host`.

### `read_local_drbd_uuid(resource_name) -> str`
Return this node's live current-UUID for a DRBD resource as lowercase hex, **role-bit masked**.
- **In:** `resource_name` — DRBD resource name (e.g. `vm-<name>-disk0`).
- **Out:** `str`; `""` if unreadable (caller defers). **Delegates to `cluster_arbiter._read_local_drbd_uuid(resource_name)`** so the pet-VM DRBD gate reads + masks IDENTICALLY to the arbiter takeover: token-0 of debugfs `data_gen_id` line 1 (NOT the per-peer bitmap / history tokens), DRBD's primary-role flag (bit 0) masked `& ~((u64)1)` (the way DRBD's own `drbd_uuid_compare` masks it, so an ex-Primary's recorded marker and an in-sync Secondary's read compare equal on the data GENERATION), and `drbdadm dump-md` ONLY when the resource is genuinely detached. The pre-start safety check re-masks the recorded rqlite marker before comparing. No writes.

### `get_recorded_uuid(resource_name, *, level="strong") -> Optional[str]`
Read the cluster's last-known authoritative DRBD UUID for a resource.
- **In:** `resource_name` — DRBD resource name; `level` keyword — rqlite read level, default `"strong"` (Raft leader round-trip for linearizability); pass `"none"` for forensic lookups.
- **Out:** `Optional[str]`, lowercased; `None` if no row or empty value. Queries `SELECT current_uuid FROM drbd_resources WHERE name = ?` via `lib.rqlite_client.RqliteClient`. Read-only.

### `record_uuid_after_promote(resource_name) -> str`
Read the local current-UUID and write it to rqlite as the cluster's record.
- **In:** `resource_name` — DRBD resource name. Caller must have already run `drbdadm primary` on this node, or the local UUID is the pre-promote value.
- **Out:** `str` — the UUID that was written. Side effect: `lib.bedrock_state.drbd_resource_uuid_set(...)` UPDATE through Raft (quorum confirms before return). Raises `RuntimeError` if no local UUID is readable.

### `is_safe_to_start_vm(vm_name, disks) -> _Verdict`
Pre-start safety check for a failover-takeover: every backing DRBD resource's
local UUID must match the cluster's recorded UUID before the VM may start.
- **In:** `vm_name` — VM name (for logging); `disks` — list of DRBD resource names backing the VM (empty for cattle).
- **Out:** `_Verdict` (truthy when safe, `.reason` set on refusal). For each disk it does one strong rqlite read (`get_recorded_uuid`) plus one local read (`read_local_drbd_uuid`). No writes.

### `class _Verdict`
Tiny result object: `.safe` (bool), `.reason` (str). `__bool__` returns
`.safe`, so it can be tested directly. Slotted; constructed only inside this
module.

## How it works

The takeover orchestrator (`orchestrator/vm_failover.py`) drives the sequence;
this module supplies the decisions and the UUID bookkeeping. Two invariants
hold the line: **whoever runs `drbdadm primary` records the new UUID through
quorum**, and **nobody starts a VM whose local DRBD UUID disagrees with that
quorum-confirmed record**.

Who-takes-over is pure arithmetic. `peers_after_dead` returns True only when
`me` sits exactly one index past `dead_host` in `failover_order`. So for
`[A, B, C]` with A dead, B (index 1) takes over, not C — and a cascade where
B was already taken over from earlier is handled by passing `dead_host="B"`,
which makes C next.

```
failover_order = [A, B, C]
                  ↑  ↑  ↑
A dies  ─────────────┘        peers_after_dead(.., me=B, dead=A) → True
B already taken over ─┘ ───┐  peers_after_dead(.., me=C, dead=B) → True
                           └→ cascade tertiary
```

Reading the local UUID is delegated to the single shared reader
(`cluster_arbiter._read_local_drbd_uuid`): token-0 of debugfs `data_gen_id`
(valid while DRBD is UP), `drbdadm dump-md` only when detached, and the DRBD
primary-role bit 0 masked so the compare is on the data GENERATION, not role.

```
read_local_drbd_uuid(r)
   ├─ open /sys/kernel/debug/drbd/resources/<r>/volumes/0/data_gen_id
   │      → "0x...."  → strip "0x", lower()        (DRBD UP)
   └─ OSError → drbdadm dump-md <r>  (3s)
          → parse "current-uuid 0x...;" → strip ; and 0x, lower()
   (neither yields a value → "")
```

The pre-start gate is the load-bearing guard. For each disk:

```
local    = read_local_drbd_uuid(resource)        # this node's UUID
recorded = get_recorded_uuid(resource, strong)   # Raft round-trip

local == ""        → REFUSE  (no local UUID readable)
recorded is None   → REFUSE  (no quorum-confirmed baseline in rqlite)
recorded != local  → REFUSE  (local DRBD behind, or a later takeover
                              happened we don't yet know of)
all disks match    → SAFE
```

An empty `disks` list (cattle VM) short-circuits to safe — there is no DRBD
to verify.

In the normal takeover ordering the surviving node calls
`record_uuid_after_promote` (post-`drbdadm primary`) *before*
`is_safe_to_start_vm`, so the strong-read it then performs sees the value this
node just wrote. A mismatch at that point therefore signals a genuine
divergence (local replica behind, or another node took over) rather than an
unwritten record, and the verdict tells the operator to reconcile.

## Why

The strong read forces a Raft leader round-trip so the check can't pass on a
stale local replica that has not yet seen another node's takeover write — a
quiet write-loss is worse than a refused start, so the gate biases toward
refusing. UUID matching is exact: a partial/approximate match would defeat the
point of detecting which node holds the authoritative data.
