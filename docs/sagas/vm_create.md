# Saga: `vm_create`

**Module:** `bedrock_d/vm/create.py` — **Class:** `VmCreate`

## Summary

Provisions a VM (`cattle` / `pet` / `vipet`) and starts it.

- **What:** allocate storage, bring up DRBD (pet/vipet only), fill or
  ISO-boot disk0, `virt-install` the domain on the home node, propagate the
  domain definition to peers, register the VM in rqlite.
- **Trigger:** `POST /api/vms` (dashboard / CLI). The handler resolves
  `(home, peers)` via `_vm_create_peers`, then `_run_vm_saga` submits the saga
  and runs it synchronously **on the mgmt master** (this process).
- **Where it runs:** entirely on the master. Steps that must touch a peer SSH
  into it via `lvm._run_on(host, cmd)` (runs locally when `host` is self).
- **End state:** disk LVs on every peer; for pet/vipet a per-disk DRBD resource
  Up on every peer, Primary on home; a defined libvirt domain on every peer; a
  running domain on home; one `vms` row (`state='running'`).

Storage by type (`is_replicated = vm_type in {pet, vipet}`):

| type   | peers | disk0 storage                              |
|--------|-------|--------------------------------------------|
| cattle | 1     | one local thin LV, no DRBD, no failover    |
| pet    | 2     | 2-way DRBD, external-meta LV pair per disk |
| vipet  | 3     | 3-way DRBD, external-meta LV pair per disk |

Multi-disk: `disk0` (boot, `disk_gb`) plus one resource per entry in
`extra_disks` (vdb, vdc, …). Every disk gets its own resource
`vm-<name>-disk<N>` and, when replicated, its own DRBD minor/port.

### Steps

| # | Step | What it does |
|---|------|--------------|
| 1  | `validate_request`     | Check ctx; build per-disk plan + `is_replicated` |
| 2  | `allocate_minors`      | Pick a free DRBD minor per disk (replicated only) |
| 3  | `register_drbd_resources` | Compute LV names; write `drbd_resources` rows (replicated only) |
| 4  | `lvcreate_on_peers`    | Create disk LVs on every peer |
| 5  | `write_drbd_config`    | Write `.res` on every peer (replicated only) |
| 6  | `drbd_create_md`       | `drbdadm create-md --max-peers=7` on every peer (replicated only) |
| 7  | `drbd_up`              | `drbdadm up` on every peer (replicated only) |
| 8  | `drbd_primary`         | `drbdadm primary --force` on home (replicated only) |
| 9  | `write_boot_image`     | Write cached Alpine onto disk0 (no-op if ISO set or data present) |
| 10 | `virsh_install`        | `virt-install` on home; dump XML into ctx |
| 11 | `virsh_define_on_peers`| `virsh define` the domain on the other peers (replicated only) |
| 12 | `record_disk_uuids`    | Record post-promote DRBD current-UUIDs in rqlite (replicated only) |
| 13 | `register_vm`          | Write the `vms` row, `state='running'` |

### Inputs (`ctx`, from `POST /api/vms`)

| key | type | meaning |
|-----|------|---------|
| `vm_name` | str | Unique VM name |
| `vcpus` | int | vCPU count |
| `ram_mb` | int | RAM in MiB |
| `disk_gb` | int | disk0 size in GiB |
| `extra_disks` | list[int] | Extra data-disk sizes in GiB (optional) |
| `vm_type` | `cattle\|pet\|vipet` | Replica count |
| `priority` | `low\|normal\|high` | HA-importance; drives self-heal restore ordering |
| `iso` | str (optional) | Filename in `/mnt/bedrock/iso/`; boots from CDROM. Empty → Alpine image written to disk0 |
| `peers` | list[str] | 1 / 2 / 3 node names; `peers[0] == home` |
| `home` | str | Node the VM runs on |

### Outputs (`ctx`)

- `disks`: list of per-disk dicts, each `{index, resource, size_gb, data_lv,
  meta_lv}` plus `{minor, port}` when replicated.
- `is_replicated`: bool.
- `libvirt_xml`: domain XML dumped from home after `virt-install`.

## Detail

Idempotency is per step: the executor records each step `done` and skips it on
resume, and the step bodies also self-guard so re-running is safe. Resume after
a crash re-submits the same op and walks from the first not-`done` step.

### 1. `validate_request`
Rejects if any of `vm_name, vcpus, ram_mb, disk_gb, vm_type, peers, home` is
missing/empty, `vm_type` is unknown, `len(peers)` != {cattle:1, pet:2, vipet:3},
or `home not in peers`. Builds `ctx["disks"]` from `disk_gb` + `extra_disks` and
sets `ctx["is_replicated"]`.
- **Revert:** none (no side effects).
- **Idempotent:** pure.

### 2. `allocate_minors` *(replicated only)*
Reads `drbd_resources` minors in `[1102, 1189]`, then assigns each disk the
lowest free minor not in use and not in `{1132, 1133, 1134}`; existing rows
reuse their minor. `port = 7700 + (minor - 1100)`.
- **Why this band:** every DRBD port stays in 7700-7799; minors 1132/1133/1134
  are skipped because their ports collide with the netd mesh probe/advert/
  heartbeat ports 7732/7733/7734. The cluster singleton is minor 1101.
- **Revert:** none (rqlite row written by step 3, removed by `vm_destroy`).
- **Idempotent:** reuses the row's minor on resume.

### 3. `register_drbd_resources` *(replicated only)*
Sets each disk's `data_lv` / `meta_lv` via `lvm.lv_names_for` (always, for both
types). For replicated disks, `INSERT OR REPLACE` into `drbd_resources`
(`name, minor, data_lv, meta_lv, thinpool, data_size_bytes, meta_size_bytes,
max_peers=7, peers, timestamps`). Recording the resource before burning on-disk
state lets `vm_destroy` clean up a half-finished create.
- **Revert:** `vm_destroy` deletes the row.
- **Idempotent:** `INSERT OR REPLACE`.

### 4. `lvcreate_on_peers`
For every peer × disk: replicated disks call `lvm.lvcreate_pair` (data + meta
thin LVs; meta size from `lvm.meta_size_mb_for(size_gb, max_peers=7)`); cattle
creates a single thin data LV. Both check `lvs` first.
- **Revert:** `vm_destroy` → `lvm.lvremove_pair` (and the cattle data LV).
- **Idempotent:** per-LV existence check.

### 5. `write_drbd_config` *(replicated only)*
Renders `drbd_config.render(resource, minor, peers)` — protocol C, external
meta, full-mesh connection blocks (every peer pair, one port), per-node
`node-id`. Writes `/etc/drbd.d/<resource>.res` on every peer. Peer metadata
(host, loopback `/32`, node-id by position) comes from rqlite `nodes`.
- **Revert:** `vm_destroy` removes the `.res` file.
- **Idempotent:** content-deterministic; rewrites identical bytes.

### 6. `drbd_create_md` *(replicated only)*
`drbdadm create-md --force --max-peers=7 <resource>` on every peer.
`--max-peers=7` is fixed at create-md; the meta LV pre-reserves bitmap space so
adding peers later needs no downtime.
- **Revert:** `vm_destroy` wipes metadata.
- **Idempotent:** `check=False`; existing metadata exits non-zero and is
  tolerated.

### 7. `drbd_up` *(replicated only)*
`drbdadm up <resource>` on every peer.
- **Revert:** `vm_destroy` → `drbdadm down`.
- **Idempotent:** `check=False`; already-up is success.

### 8. `drbd_primary` *(replicated only)*
`drbdadm primary --force <resource>` on home only, so the image step can write
disk0 and libvirt can boot. Peers stay Secondary.
- **Revert:** none needed (`drbdadm down` in destroy covers it).
- **Idempotent:** promoting an already-Primary resource is a no-op.

### 9. `write_boot_image`
No-op when `iso` is set (install runs from CDROM). Otherwise targets disk0's
device (`/dev/drbd<minor>` if replicated, else the local LV path): skips if
`blkid` reports a filesystem signature; else curls the cached Alpine qcow2 to
`/var/lib/bedrock/alpine.qcow2` if absent and `qemu-img convert -O raw` writes
it onto the device. Runs on home.
- **Revert:** none (LV/DRBD teardown in destroy reclaims it).
- **Idempotent:** `blkid` signature check.

### 10. `virsh_install`
On home: if `virsh dominfo <name>` fails, `virt-install` defines + starts the
domain — `--disk path=…,format=raw,bus=virtio,cache=none,discard=unmap` per
disk (DRBD device or local LV), `--network bridge=br0,model=virtio`,
`--graphics vnc,listen=0.0.0.0`, qemu-guest-agent virtio channel,
`--noautoconsole`. With an ISO: `--cdrom <iso> --boot cdrom,hd`; otherwise
`--import --boot hd`. Then `virsh dumpxml` → `ctx["libvirt_xml"]`.
- **Revert:** `vm_destroy` → `virsh destroy` + `undefine`.
- **Idempotent:** already-defined domain skips straight to the XML dump.

### 11. `virsh_define_on_peers` *(replicated only)*
Writes `ctx["libvirt_xml"]` to `/tmp/<name>.xml` on each non-home peer and
`virsh define`s it so any peer can run the VM on failover. A failed define is
logged (that peer can't take over) but does not fail the saga.
- **Revert:** `vm_destroy` → `virsh undefine` on each peer.
- **Idempotent:** `virsh define` updates in place.

### 12. `record_disk_uuids` *(replicated only)*
Calls `failover.record_uuid_after_promote(resource)` per disk: reads the local
post-promote DRBD current-UUID and writes it to rqlite (through Raft) so the
first failover's exact-equality safety check has a quorum-confirmed baseline.
Failures are logged, not fatal.
- **Revert:** none (`vm_destroy` deletes the resource row).
- **Idempotent:** UPDATE to the current value.

### 13. `register_vm`
`INSERT OR REPLACE INTO vms (vm_name, vm_type, host, ram_mb, disk_gb, state,
failover_order, priority, updated_at)` with `state='running'`.
`failover_order` is `[]` for cattle, else `ctx["peers"]` verbatim
(`peers[0]=home=primary`); the failover orchestrator consults it to decide
who is next in line after a dead primary. `priority` defaults to `normal`.
- **Revert:** `vm_destroy` deletes the row.
- **Idempotent:** `INSERT OR REPLACE`.

## Revert

The inverse is [`vm_destroy`](vm_destroy.md): re-run with the same `vm_name` and
it reverses each effect in safe order (virsh destroy → undefine → drbd down →
wipe-md → `.res` remove → lvremove → delete rqlite rows). A create that crashed
before step 13 leaves a `drbd_resources` row but no `vms` row; `vm_destroy`
reads `drbd_resources` to find the LVs/DRBD to clean up.
