# bedrock_d/vm/grow.py

The `vm_grow` saga: the online disk-extend handler behind `bedrock vm grow`.
It enlarges one VM disk in place by extending the LVM data LV (and the meta LV
first, when the larger DRBD bitmap needs the room) on every peer, then running
`drbdadm resize` so DRBD picks up the bigger device without detaching or
remounting, and finally records the new size in rqlite. Like every saga it is
submitted by writing an `operations` row and run step-by-step by the
orchestrator's saga executor (`bedrock_d/orchestrator/sagas/`); the executor owns
rqlite access and crash-resume, and each step body is idempotent. The actual
LVM/DRBD sizing and shell-out lives in the sibling `lvm` module; this file just
orders the steps. Growing the guest's own partition/filesystem (`growpart` +
`resize2fs`) stays a separate operator action the saga does not perform.

## Functions / Classes

### `class VmGrow` — `@saga("vm_grow")`
Online disk-extend saga for a single VM disk. Steps run in declaration order;
each takes the shared `ctx` dict, returns nothing, and mutates `ctx` or shells
out to peers.

**ctx inputs:** `vm_name` (str); `new_gb` (int, new *total* disk size, must be
strictly > current); `disk_index` (int, which disk to grow, default 0 = boot
disk).
**ctx filled:** `resource` (`vm-<name>-disk<idx>`); `old_gb` (current
`data_size_bytes` // GiB); `peers` (list of node names backing the resource).

Steps:

- **`step_load` — `@step("load_current_size")`** — Reads the resource's current
  size and peer set from rqlite. **In:** `ctx["vm_name"]`, optional
  `ctx["disk_index"]` (default 0). **Out:** sets `ctx["resource"]`,
  `ctx["old_gb"]`, `ctx["peers"]`; one rqlite `SELECT data_size_bytes, peers FROM
  drbd_resources WHERE name=?`; raises `RuntimeError` if no matching row.
- **`step_validate` — `@step("validate_new_size")`** — Guards that the grow grows.
  **In:** `ctx["new_gb"]`, `ctx["old_gb"]`. **Out:** nothing; raises `ValueError`
  if `new_gb <= old_gb` (equal is rejected, not a silent no-op; shrink is out of
  scope). No side effects.
- **`step_extend_meta` — `@step("lvextend_meta_on_peers")`** — Extends the meta LV
  on every peer to fit the larger bitmap. **In:** `ctx["new_gb"]`,
  `ctx["peers"]`, `ctx["resource"]`. **Out:** `lvm.lvextend_meta(host, resource,
  new_gb)` per peer → an `lvextend -L <meta>M` on each host (LVM no-ops if already
  big enough).
- **`step_extend_data` — `@step("lvextend_data_on_peers")`** — Extends the data LV
  on every peer. **In:** same as above. **Out:** `lvm.lvextend_data(host,
  resource, new_gb)` per peer → an `lvextend -L <new_gb>G --no-resize-fs` on each
  host.
- **`step_drbd_resize` — `@step("drbd_resize")`** — Tells DRBD to read the new
  data-device size on every peer. **In:** `ctx["resource"]`, `ctx["peers"]`.
  **Out:** `lvm._run_on(host, "drbdadm resize <resource>", check=False)` per peer;
  online, no detach, no remount.
- **`step_update_row` — `@step("update_drbd_resources_row")`** — Records the new
  size in rqlite as the new baseline. **In:** `ctx["new_gb"]`, `ctx["resource"]`.
  **Out:** one rqlite `UPDATE drbd_resources SET data_size_bytes, meta_size_bytes,
  updated_at WHERE name=?`; `meta_size_bytes` is recomputed from
  `lvm.meta_size_mb_for(new_gb)`, `updated_at` is epoch seconds.

### `_peer_hosts(peer_names) -> list[str]`
Private. Resolves peer node names to SSH-reachable `host` addresses. **In:** list
of `node_name` from `ctx["peers"]`. **Out:** list of `host` strings ordered to
match `peer_names`, dropping any name with no row or empty host; one rqlite
`SELECT node_name, host FROM nodes WHERE node_name IN (...)` at read level
`none`. Empty input → empty list.

## How it works

The saga assumes the resource and its LV pair already exist; it only extends.
The executor runs the six steps in declaration order, and because each body is
idempotent a crash resumes from the first step lacking a `done` row. Ordering is
the load-bearing detail:

```
load_current_size      read drbd_resources → resource, old_gb, peers
        │
validate_new_size      require new_gb > old_gb  (equal/smaller → ValueError)
        │
lvextend_meta_on_peers   meta LV first  ──┐  per peer
lvextend_data_on_peers   data LV next   ──┘  (meta before data)
        │
drbd_resize            drbdadm resize on every peer (online)
        │
update_drbd_resources_row   rqlite UPDATE  ← new baseline for next grow
```

Meta is extended before data because a larger data device needs a larger DRBD
bitmap; growing meta first guarantees the bitmap can describe the new data size
when `drbdadm resize` reads the now-larger data device and recalculates in place.

Each step loops over `_peer_hosts(ctx["peers"])` and shells out per host via
`lvm._run_on`, which runs locally when `host` is empty / `localhost` / the local
hostname and SSHes as `root@<host>` otherwise. The LVM grows go through
`lvextend ... || true` and `drbd_resize` uses `check=False`, so LVM
"already at size" and benign DRBD re-resize results don't abort the saga — which
is what keeps a resumed run safe to re-execute.

The size baseline lives in `drbd_resources.data_size_bytes`, so the rqlite
UPDATE is deliberately last: until it commits, a re-run still reads the old
`old_gb` and a repeat grow stays consistent; once it commits, a later grow sees
the new size as its floor.

## Why
On-disk extend is meta-then-data-then-`drbdadm resize` because DRBD recalculates
its per-peer bitmap in place from the data device; reserving the bitmap room
(meta) before the data grows keeps the whole operation online, with no detach and
no guest downtime.
