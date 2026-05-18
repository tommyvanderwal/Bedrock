# `vm.py`

**Module purpose.** VM lifecycle for Bedrock's three workload types:

- **cattle** — single-replica, local-LV-backed, stateless. Lives on
  one host; if the host dies, recreate from the snapshot/backup.
- **pet** — 2-way DRBD replicated, primary on home host. Survives
  one node failure.
- **vipet** — 3-way DRBD replicated (when N≥3). Survives two node
  failures.

The mgmt CLI `bedrock vm {create,list,migrate,delete}` and the
dashboard's `/api/vms/*` both hand off here.

## Functions

### Cluster/API helpers

- `run(cmd, check=True)` / `run_on(host, cmd, check=True)` — local
  + SSH wrappers (paramiko under the hood).
- `_cluster() -> dict` — short read of cluster.json.
- `_api_get(state, path) / _api_post(state, path, body)` — mgmt
  API helpers used by CLI to dispatch operations through the
  master (so the master's bs.* helpers do the actual rqlite
  writes).

### LVM thin-pool helper

- `_ensure_thin_pool(host, size_gb=80)` — create + grow a thin
  pool on a target host for VM disk LVs. Called from the cattle
  create path on the destination node.

### Create paths

- `create_vm(state, name, vm_type, ram, disk)` — top-level
  dispatcher. Calls `workload.validate_type`, picks the home
  host(s), then `_create_cattle / _create_pet / _create_vipet`.
- `_download_alpine_on_node(host)` — fetch the default Alpine
  qcow2 boot image if not already present.
- `_create_cattle(host, name, ram, disk)` — virsh define + start.
  Local LV under the bedrock thinpool.
- `_create_pet(host_a, host_b, name_a, name_b, ...)` — DRBD 2-way:
  set up the resource on both hosts, virsh define on home host,
  drbdadm primary, virsh start.
- `_create_vipet(nodes, home_name, peer_names, vm_name, ...)` —
  DRBD 3-way variant.
- `_next_drbd_minor(host) -> int` — picks the next free
  `/dev/drbd<N>` slot from `drbdadm dump` parsing.
- `_drbd_2way_conf / _drbd_3way_conf(...)` — render
  `/etc/drbd.d/vm-<name>.res`.
- `_vm_xml_pet(vm_name, ram, minor)` — virsh XML template for a
  pet VM (DRBD-backed disk0).

### Migrate / delete

- `migrate_vm(state, name, target)` — live migration via
  `virsh migrate --live`. For pet VMs, ensures DRBD primary
  follows.
- `delete_vm(state, name)` — `virsh destroy + undefine` + remove
  the LV / DRBD resource.

### List

- `list_vms(state)` — query the rqlite `vms` table; return the
  list with current `virsh dominfo` status overlaid for the
  master's nodes.

## Stability note

This module is the part most likely to change in v1.x — the
workload model is a "good-enough Phase E stub" per the lessons
log. The cluster-protocol layer (orchestrator/election/storage)
is more mature.
