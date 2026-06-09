# installer/lib/tier_storage.py

Per-node storage provisioning for the two things this module owns: the **cluster
singleton** (the `cluster` DRBD resource, mounted at `/var/lib/bedrock/cluster`,
holding the arbiter rqlite data + SeaweedFS filer leveldb3 + S3 IAM database) and
the **local SeaweedFS volume LV** (`bedrock-weed-volume`). It manages the VG, the
single thin pool, the LV pairs (data + external-meta) for the singleton, the DRBD
`.res` files under `/etc/drbd.d/`, the fstab/mount lines, and the growth/shrink
transitions of the singleton between a plain local LV and a 2-/3-way DRBD device.
Called from `mgmt_install.install_full()` and `agent_install.install()` (both →
`setup_n1()`), and from the `bedrock storage` subcommands and init/join sagas for
the master/peer transitions. Per-VM disks (cattle / pet / vipet) are NOT this
module's job — the VM-lifecycle sagas own those; this module only provides the
shared name/port/meta-size formulas they reuse.

## Functions / Classes

### `detect_vg() -> str`
Resolve the VG name this node uses for the thin pool, tier LVs, and VM disks.
- **In:** none (reads `/etc/bedrock/storage.json` and `vgs`).
- **Out:** VG name string. Priority: storage.json `vg` key → the sole VG if
  exactly one exists → `bedrock-vg` if present → `bedrock` if present → `bedrock`
  fallback. No side effects. Runs `vgs` (10 s timeout).

### `VG` (module global)
The resolved VG name, set once at import via `detect_vg()`. Stable for the process
lifetime; `ensure_vg()` reassigns it when it adopts or creates a VG.

### `ensure_vg() -> None`
Adopt or create the VG bedrock uses.
- **In:** none.
- **Out:** none. If a VG named `VG` exists, persists that name to storage.json. If
  some other VG exists, adopts the first one and persists it. If no VG exists,
  carves a PV (boot-disk tail, else a separate data disk) and `vgcreate bedrock-vg`
  on it, then persists. Runs `vgs`/`pvcreate`/`vgcreate`/`wipefs`; writes
  `/etc/bedrock/storage.json`; mutates global `VG`.

### `ensure_thinpool() -> None`
Create the single thin pool `<VG>/thinpool` sized to fill the VG.
- **In:** none.
- **Out:** none. Calls `ensure_vg()` first. If the pool exists, calls
  `_ensure_vg_headroom()` and returns. Otherwise removes any OS swap LV (swapoff +
  fstab strip + lvremove), checks free space against `(CLUSTER_SIZE_GB +
  WEED_VOLUME_SIZE_GB)*1024 + 1024` MB and raises if short, then `lvcreate -T` a
  pool leaving 768 MB headroom. Edits `/etc/fstab`; runs `lvs`/`vgs`/`lvcreate`/
  `swapoff`/`lvremove`/`blkid`.

### `ensure_thin_lv(lv, size_gb) -> None`
Idempotently create a thin data LV in the pool.
- **In:** `lv` LV name; `size_gb` virtual size.
- **Out:** none. No-op if the LV exists; else `lvcreate -V <size>G -T <VG>/thinpool
  -n <lv>`.

### `ensure_meta_lv(lv, size_mb=32) -> None`
Idempotently create a thin external-metadata LV in the pool for one DRBD resource.
- **In:** `lv` meta LV name; `size_mb` virtual size (default `DRBD_META_SIZE_MB`).
- **Out:** none. No-op if it exists; else `lvcreate -V <size>M -T`.

### `ensure_weed_volume_lv() -> None`
Create + XFS-format + mount the local SeaweedFS volume LV.
- **In:** none.
- **Out:** none. Provisions `bedrock-weed-volume` (`WEED_VOLUME_SIZE_GB`), mkfs.xfs
  (label `weed-volume`), and mounts at `/var/lib/bedrock/seaweedfs/volumes` with an
  fstab line. Idempotent.

### `setup_n1(*, write_rqlite=False) -> None`
Single-node storage setup — the install entry point.
- **In:** `write_rqlite` — pass `False` when rqlite isn't up yet (default, used
  during `bedrock init`); `True` from `bedrock storage init`.
- **Out:** none. Ensures the thin pool + weed-volume LV, creates the
  `/var/lib/bedrock/cluster` directory on the root FS, and records the singleton's
  tier state as `mode='local'` (via `set_tier_state`). Idempotent.

### `mirror_tier_state_to_rqlite() -> None`
Push the singleton's `mode='local'` state into rqlite after rqlited reaches Leader.
- **In:** none.
- **Out:** none. Calls `bedrock_state.tier_state(tier='cluster', mode='local',
  backend_path=CLUSTER_MOUNT)`. Idempotent (INSERT OR REPLACE). Called by the
  init/join saga.

### `get_tier_state(tier) -> dict` / `set_tier_state(tier, *, write_rqlite=True, **kv) -> None`
Read / write one tier's cluster-wide state.
- **In (get):** `tier` name. **Out (get):** that tier's dict from
  `load_cluster()`, or `{"mode": "local", "version": 1}` default.
- **In (set):** `tier`; `write_rqlite` gate; `mode`/`master`/`peers`/`backend_path`
  in `**kv`. **Out (set):** none. No-op if `write_rqlite=False` or this node is not
  the mgmt master. Otherwise writes via `bedrock_state.tier_state(...)`; on failure
  prints an error but never raises.

### `get_drbd_node_id(resource, peer_name) -> int`
Return the permanent DRBD node-id for a peer in a resource, allocating on first
sight.
- **In:** `resource`, `peer_name`.
- **Out:** the integer node-id. Allocates the smallest unused id, bumps the tier
  `version`, and — when this node is the mgmt master — persists via
  `bedrock_state.drbd_node_id_assigned(...)`. Reads `load_cluster()`.

### `free_drbd_node_id(resource, peer_name, reason="") -> int | None`
Release a peer's node-id for reuse (call only after `drbdsetup forget-peer`).
- **In:** `resource`, `peer_name`, optional `reason`.
- **Out:** the freed id or `None` if unassigned. Master persists via
  `bedrock_state.drbd_node_id_freed(...)`.

### `render_drbd_res(resource, minor, peers) -> str`
Render a single-address (one address per `on` block) DRBD 9 resource file.
- **In:** `resource`, `minor`, `peers=[{name, loopback_ip}, ...]`.
- **Out:** the `.res` body as a string. External meta, port from
  `drbd_port_for(minor)`, persistent node-ids, full connection mesh. No side
  effects beyond node-id allocation inside `get_drbd_node_id`.

### `render_drbd_res_mesh(resource, minor, peers, snapshot) -> str`
Render a multi-path DRBD 9 resource file from the mesh path table.
- **In:** `resource`, `minor`, `peers`, `snapshot` (view_builder fold, must carry
  `paths`).
- **Out:** the `.res` body. Per peer pair, one `connection` with one `path` block
  per observed direct NIC pair (using each NIC's `link_addr_*`), plus a final
  loopback-fallback `path`.

### `write_drbd_resource(resource, peers) -> None`
Write `/etc/drbd.d/<resource>.res` (single-address render).
- **In:** `resource`, `peers`.
- **Out:** none. For `cluster`, caps `peers` to the 3-way set via
  `cap_singleton_peers`. Resolves the minor via `_minor_for` (only `cluster` is
  valid). Writes the file.

### `cap_singleton_peers(peers) -> list[dict]`
Trim a peer list to the singleton's replica cap.
- **In:** `peers`. **Out:** the lowest-octet `SINGLETON_MAX_REPLICAS` (=3) peers,
  sorted by loopback last octet.

### `regen_drbd_configs_from_snapshot(snapshot) -> bool`
Rewrite the singleton `.res` (mesh render) on path-table changes and apply it.
- **In:** `snapshot` (view_builder fold with `nodes` + `tiers`).
- **Out:** `True` iff the file actually changed. No-op when the file is absent
  (N=1 / not promoted) or the tier mode isn't `drbd`/`drbd-3way` or the body is
  unchanged. On change, writes the file and runs `drbdadm adjust cluster`
  (failures swallowed). Called from the orchestrator subscriber.

### `promote_local_to_drbd_master(resource, peers) -> None`
Convert the singleton's local directory into a DRBD primary holding the same data.
- **In:** `resource` (must be `cluster`), `peers`.
- **Out:** none. Stops singleton services, snapshots `/var/lib/bedrock/cluster`,
  creates the LV pair, writes `.res`, `create-md`/`up`/`primary --force`, mkfs.xfs
  (on first promote), mounts the DRBD device, restores the snapshot, writes the
  DRBD fstab line, restarts the singletons. Idempotent (no-op if already Primary +
  mounted). Heavy subprocess use (`drbdadm`, `mount`, `cp -a`, `systemctl`).

### `join_drbd_peer(resource, peers) -> None`
On a non-source peer: create the LV pair, write config, bring up as Secondary.
- **In:** `resource`, `peers`.
- **Out:** none. `create-md --force`, `drbdadm up` (tolerates already-up). Does NOT
  promote; initial sync runs from the primary.

### `transition_to_n2_master(self_loopback_ip, peer) -> dict`
Master side of the N=1 → N=2 singleton promotion.
- **In:** `self_loopback_ip`; `peer={"name","loopback_ip"}`.
- **Out:** `{"peers": [...]}`. Calls `promote_local_to_drbd_master`, sets tier state
  `mode='drbd'` with `master`=self, and writes `CLUSTER_DRBD_MARKER`.

### `transition_to_n2_peer(self_loopback_ip, master, peers) -> None`
Peer side of N=1 → N=2: join the singleton DRBD as Secondary.
- **In:** `self_loopback_ip`, `master`, `peers`.
- **Out:** none. Calls `join_drbd_peer` and writes `CLUSTER_DRBD_MARKER`.

### `promote_cluster_to_3way(third_peer) -> None`
Add a third peer to the singleton DRBD resource (run on master).
- **In:** `third_peer={"name", ...}`.
- **Out:** none. Rewrites local `.res` + `drbdadm adjust`, SSH-distributes the same
  `.res` (base64) to existing peers and adjusts each, sets tier state `mode='drbd'`
  with the new peer list. Assumes `--max-peers=7`.

### `drbd_remove_peer(resource, leaving_peer_name, surviving_hosts, surviving_peers=None, new_res_text=None, bedrock_resource=True) -> None`
Online removal of one peer from any DRBD resource.
- **In:** `resource` (verbatim DRBD name); `leaving_peer_name`; `surviving_hosts`
  (SSH targets); `surviving_peers` (required when `bedrock_resource=True`);
  `new_res_text` (override render); `bedrock_resource` (auto-render for `cluster`).
- **Out:** none. Distributes the new `.res` first, then per survivor runs
  `drbdsetup disconnect` + `del-peer` + `forget-peer` by the leaving peer's
  node-id, frees the node-id, and persists the surviving peer list. SSH-heavy;
  raises only if the leaving peer's id can't be resolved or `surviving_peers` is
  missing for a bedrock resource.

### `drbd_demote_to_local(remove_meta=False) -> bool`
Turn the stand-alone singleton DRBD resource back into a plain local LV mount.
- **In:** `remove_meta` — also lvremove the meta LV.
- **Out:** `True` on success; `False` if peers are still connected. Pre-checks via
  `drbdsetup status`, umounts, `drbdadm down`, renames `.res` aside, rewrites
  fstab to the local LV, mounts the data LV (XFS preserved), sets tier state
  `mode='local'`, optionally removes the meta LV, deletes the `.res` backup.

### `node_reset_local() -> None`
Return this node to its post-`bedrock bootstrap` state.
- **In:** none.
- **Out:** none. Stops/disables/reset-fails the bedrock services, downs all DRBD
  resources and removes `.res` files, unmounts every bedrock path, strips fstab
  entries, removes SeaweedFS + rqlite + generated-config state, lvremoves the
  singleton/weed-volume + all per-VM LVs in `VG`, removes `/opt/bedrock/*` mgmt
  subdirs and mgmt units, truncates `state.json` to `{hardware, bootstrap_done}`
  via `lib.state.save`, and `systemctl daemon-reload`. Preserves the VG + thin
  pool, OS packages, DRBD module, bridge/NIC config, SSH keys. Idempotent.

### Helpers
`run` / `run_ok` / `ssh` — local + remote (`shlex.quote`'d root SSH) shell.
`find_data_disk` / `_boot_disk` / `carve_pv_from_boot_disk_tail` — locate or create
a PV for a greenfield VG. `_ensure_vg_headroom` — attach a sparse loop-backed PV so
the thin pool has metadata room. `data_lv_for` / `meta_lv_for` / `drbd_port_for` —
delegate the canonical LV names + minor→port mapping to
`bedrock_d.vm.{lvm,drbd_config}`. `atomic_symlink` — tmp-symlink + `os.replace`
swap. `ensure_xfs` / `ensure_fstab` / `ensure_mounted` / `umount_quiet` —
idempotent FS plumbing. `load_cluster` (delegates to `cluster_state`), `load_state`,
`_is_mgmt_master`. `save_cluster` and `_log_append_typed` are no-op shims.
`_cluster_meta_size_mb`, `_minor_for`, `_peer_octet`, `_direct_paths_between`,
`_peer_link_addr` (returns `""`).

## How it works

**VG / pool bring-up (`ensure_vg` → `ensure_thinpool`).** One disk, one VG, one
thin pool. `ensure_vg` adopts whatever VG the OS installer left (it never renames,
since vgrename forces a grub + initramfs regeneration and reboot) and only
greenfield-creates `bedrock-vg` when no VG exists at all. `ensure_thinpool` frees
space by dropping the OS swap LV, hard-fails early if the VG can't fit the
singleton + weed-volume + 1 GB slack, and reserves 768 MB so the pool's own
metadata can grow. `_ensure_vg_headroom` patches the kickstart-grew-to-100% case
by attaching a sparse loop-backed PV.

**The resolved VG is the one source of truth.** Every LV/DRBD path uses the module
global `VG`. `ensure_vg` writes the resolved name to `storage.json` so every later
`bedrock` invocation and the daemon agree on it without re-detecting.

**Singleton growth path** (`/var/lib/bedrock/cluster` follows the mgmt master):

```
N=1  setup_n1            cluster = plain dir on root FS   tier mode=local
       |
N=2  transition_to_n2_master (master) ──┐
     transition_to_n2_peer  (peer)   ───┤  cluster = 2-way DRBD, XFS preserved
       |                                │  tier mode=drbd, CLUSTER_DRBD_MARKER set
N=3  promote_cluster_to_3way ───────────┘  cluster = 3-way DRBD (cap = min(3,N))
```

`promote_local_to_drbd_master` is the load-bearing step. Mounting a DRBD device
over `/var/lib/bedrock/cluster` would hide the N=1 files (arbiter rqlite, filer
leveldb3, S3 IAM), so the order is: stop the singleton services →
`cp -a` the directory to `/var/lib/bedrock-promote-snapshot` + `sync` → umount →
`create-md`/`up`/`primary --force` → mkfs.xfs (first promote only) → mount the DRBD
device → `cp -a` the snapshot back in → fstab line → restart the singletons. It
short-circuits when the resource is already Primary and mounted, so a re-issued
promote or an auto-promote race is a no-op rather than a re-snapshot of live data.
External DRBD metadata is what makes this zero-copy on the LV side: the data LV's
XFS is byte-for-byte preserved and the DRBD device stays the same size as the LV.

`CLUSTER_DRBD_MARKER` (`/etc/bedrock/cluster-drbd-ready`) is the handoff signal:
`cluster_arbiter` gates its election-driven promote on it so it defers to this
module while the transition is mid-flight, then takes over the `.254` arbiter +
arbiter rqlite once the marker is written.

**Mesh-aware config + regen.** `render_drbd_res_mesh` turns the bedrock-net path
table into DRBD 9 multi-path connections: one `path` per observed direct NIC pair
(separate TCP / keepalive / carrier detection), plus an always-last loopback
fallback that rides the kernel route table so the peer is still reachable — even
through a third node — when every direct path is down.

```
connection {
  path { # via eth1<->eth1   host A <link_a>:port; host B <link_b>:port; }
  path { # via eth2<->eth2   ...                                         }
  path { # loopback fallback (kernel routes via best NIC)                }
}
```

`regen_drbd_configs_from_snapshot` is the subscriber hook: it rewrites the `.res`
only when the singleton is DRBD-backed AND the file exists AND the body changed,
then runs `drbdadm adjust cluster` to apply new paths without disrupting in-flight
replication. The cost of a no-op call is one `stat()`.

**Permanent node-ids.** DRBD node-ids must never be renumbered for the life of a
resource — a reused bitmap slot triggers a forced full resync. `get_drbd_node_id`
allocates the smallest free id once per peer and persists it; `free_drbd_node_id`
releases it only after the bitmap slot is cleared via `drbdsetup forget-peer`.

**Single-writer rqlite discipline.** `_is_mgmt_master()` reads `state.json` and
gates every rqlite-mutating write (`set_tier_state`, the node-id persisters).
Followers no-op; the master's write replicates via Raft so every node's
view_builder folds identical state. These writes never raise — a write failure
prints loudly but does not block the storage operation.

**Shrink / role-move + crash safety.** `drbd_remove_peer` mutates kernel state via
`drbdsetup disconnect`/`del-peer`/`forget-peer` keyed on the leaving peer's
persistent node-id (drbdadm adjust is unreliable when shrinking a full mesh), and
distributes the rewritten on-disk `.res` to every survivor **before** touching the
kernel — so a power loss leaves persistent config already at the end state.
`drbd_demote_to_local` likewise mutates `.res` + fstab before `drbdadm down`: a
reboot mid-flight finds the `.res` gone and fstab pointing at the local LV, the
local mount succeeds (XFS preserved by external metadata), and the node arrives at
the desired end state.

**Reset.** `node_reset_local` is the cluster-leave / take-out-of-service path. It
umounts the DRBD `cluster` mount FIRST (so `drbdadm down` won't refuse on a busy
device), retries the down after unmounts (so the next `create-md` doesn't hit
"Device is configured"), then tears down services, configs, LVs, and `/opt/bedrock`
state — leaving the VG + thin pool intact so a later init/join skips the slow
PV/VG creation.

## Why

External DRBD metadata is used by every Bedrock DRBD resource because it keeps the
DRBD device the same size as its data LV and makes a local-LV → DRBD promotion
zero-copy — the data LV's filesystem is preserved byte-for-byte. The singleton's
replica set is capped at 3 (lowest-octet nodes): an uncapped N-way singleton leaves
peers Unconnected and stalls the initial sync, which would block the arbiter
failover the singleton exists to serve.
