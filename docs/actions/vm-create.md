# Create a VM

Creating a VM is one saga, `vm_create`, run by the saga executor inside
`bedrock-d` on the mgmt master. Both entry points POST the same
`/api/vms` request; the saga is the single live path for every type
(cattle / pet / vipet) and is multi-disk aware.

```
  CLI  ─┐                         ┌─ allocate DRBD minor (rqlite)
        ├─ POST /api/vms ─► saga ─┤  lvcreate data+meta on every peer
  UI   ─┘   (on master)           │  drbdadm create-md / up / primary
                                  │  image-fill disk0 (or ISO boot)
                                  │  virt-install on home, define on peers
                                  └─ register vms row (state=running)
```

Operators never SSH a compute node. The CLI carries intent; the saga
runs on the node that holds the DRBD/arbiter authority.

**Source:**

- API endpoint: `mgmt/app.py:api_vm_create` (`POST /api/vms`), `_run_vm_saga`,
  `_vm_create_peers`.
- Saga: `bedrock_d/vm/create.py` (`VmCreate`), `bedrock_d/vm/drbd_config.py`,
  `bedrock_d/vm/lvm.py`.
- CLI: `installer/bedrock:cmd_vm` (thin client to `127.0.0.1:8001`).
- Dashboard: `mgmt/ui/src/routes/vms/new/+page.svelte`.

## Entry points

**CLI** — `bedrock vm create NAME [--type cattle|pet|vipet] [--vcpus N]
[--ram MB] [--disk GB]`. Any type, with DRBD set up from the start.
POSTs to the local mgmt API; runs on any cluster node.

**Dashboard** (`/vms/new`, or `+ New VM` on `/vms`) — always creates a
**cattle** VM on the mgmt node, with optional ISO boot and extra data
disks. Convert to pet/ViPet later via the checkboxes on the VM Settings
page (see [`vm-convert.md`](vm-convert.md)).

```
  /vms/new
  ┌─────────────────────────────────────────────────────┐
  │ Name             [ webapp2              ]           │
  │ vCPUs            [ 2 ]   RAM (MB) [ 2048 ]          │
  │ Disk 0 (vda,GB)  [ 20 ]    ← boot disk              │
  │                                        + Add disk   │
  │   vdb [ 500 ]  GB, thin-provisioned     ×           │
  │   vdc [  50 ]  GB, thin-provisioned     ×           │
  │ Priority     ( )low  (•)normal  ( )high             │
  │ Install ISO  [ — no ISO —  ▼ ]   + Upload new ISO   │
  │ [ Create VM ]  Cancel                               │
  └─────────────────────────────────────────────────────┘
```

The form validates `name` against `^[a-z][a-z0-9-]{1,32}` client-side;
the API re-validates everything (name, vcpus 1-32, ram 128-131072 MB,
disk 1-2048 GB, extra disks 1-8192 GB, ISO exists, vm_type vs cluster
size) and returns 4xx before accepting. Create is fire-and-forget: the
API returns `{status, task_id, name}` immediately and the task drawer
shows progress.

## Request → peers

`_vm_create_peers(vm_type)` resolves `(home, peers)`:

```
  cattle  home = mgmt master         peers = [home]
  pet     home = master, 1 other     peers = [home, other]       (≥2 nodes)
  vipet   home = master, 2 others    peers = [home, o1, o2]       (≥3 nodes)
```

`workload.validate_type` rejects a type the cluster is too small for —
no silent pet→cattle downgrade. The saga reads each peer's LAN `host`
and `loopback_ip` from the rqlite `nodes` table (read level `none`,
works without quorum).

## Disks

`disk_gb` is disk0 (vda, boot). `extra_disks` add vdb, vdc, … in order.
Each disk gets a DRBD resource `vm-<name>-disk<N>`; its LVs are
`bedrock-data-vm-<name>-disk<N>` / `bedrock-meta-vm-<name>-disk<N>` in
the node's single `thinpool`. Cattle disks are plain local thin LVs
(data LV only, no meta, no DRBD).

`POST /api/vms/{name}/disks` live-attaches one more thin LV to an
existing VM (`virsh attach-disk --live --config`) so "start small, grow
later" works. On pet/ViPet the new disk lands as a local LV; converting
it to DRBD is a separate step.

## Priority

`priority` (low/normal/high) is recorded in the `vms` row at create.
It drives self-heal replica-restore ordering. CPU-weight (libvirt
`cpu_shares`, mapping low=256 / normal=1024 / high=4096) is applied
live by the Settings page (`vm-settings.md`), not at create.

## Preconditions

- Cluster has enough nodes for the type (cattle 1, pet 2, vipet 3).
- A volume group with a `thinpool` exists on each peer (the OS installer
  makes the VG, usually named `almalinux`; `lvm._resolved_vg()` adopts it).
- For a blank (no-ISO) VM: the Alpine cloud image is reachable at
  `ALPINE_URL` or cached at `/var/lib/bedrock/alpine.qcow2` on the home node.
- For an ISO VM: the selected ISO exists at `/mnt/bedrock/iso/<name>`
  (SeaweedFS filer, visible cluster-wide).

## Saga steps (`VmCreate`)

Steps run in declaration order; `ctx` persists between them so a
mid-saga crash resumes. The `is_replicated` flag (pet/vipet) is the
master switch — every DRBD step early-returns for cattle.

```
  validate_request          check fields, peer count vs type, home∈peers;
                            build ctx["disks"] plan
  allocate_minors           pick free DRBD minor 1102..1189 from rqlite
                            drbd_resources; reuse on resume   (replicated)
  register_drbd_resources   fill data_lv/meta_lv; INSERT OR REPLACE the
                            drbd_resources row (minor, peers, sizes)
  lvcreate_on_peers         data+meta pair on every peer (replicated) /
                            single local thin LV (cattle); checks lvs first
  write_drbd_config         render /etc/drbd.d/<r>.res on every peer  (replicated)
  drbd_create_md            drbdadm create-md --force --max-peers=7   (replicated)
  drbd_up                   drbdadm up on every peer                  (replicated)
  drbd_primary              drbdadm primary --force on home           (replicated)
  write_boot_image          no-ISO only: qemu-img convert Alpine onto disk0;
                            skips if blkid finds a signature
  virsh_install             virt-install on home (--import or --cdrom), then
                            dumpxml → ctx; skips if domain already defined
  virsh_define_on_peers     ship XML + virsh define on each non-home peer
                            (replicated; a failed define warns, doesn't abort)
  record_disk_uuids         store post-promote DRBD current-UUID in rqlite
                            for the failover safety check  (replicated)
  register_vm               INSERT OR REPLACE vms row, state='running',
                            host=home, priority, failover_order
```

`failover_order` is `[]` for cattle (no failover) and `peers` verbatim
for pet/vipet (`peers[0]`=home=primary). The failover orchestrator reads
it to decide which surviving peer is next in line after a dead primary.

The VM is started by `virt-install` (no `--noautoboot`). The dashboard
state push surfaces it in the sidebar on its next tick (≤ 3 s).

## DRBD minor → port

```
  minor:  1101        1102 .. 1131   1132 1133 1134   1135 .. 1189
          cluster     VM disks       reserved         VM disks
          singleton                  (skip)

  port = 7700 + (minor - 1100)        → lands in 7700-7799
```

Minors 1132/1133/1134 are excluded: their derived ports (7732/7733/7734)
collide with netd's mesh-probe, advert, and election-heartbeat UDP ports.

## Why this order

- **rqlite rows before LV/DRBD work**: a crash mid-provision resumes with
  the minor and peer set already pinned, instead of re-racing for a free minor.
- **`--max-peers=7` at create-md**: reserves bitmap slots so a 2-way pet can
  grow to vipet later without re-creating metadata.
- **DRBD up before primary**: a disconnected resource cannot be promoted.
- **home Primary before image-fill / virt-install**: only a Primary backs a
  writable block device for disk0 and the running domain.
- **image write while Primary**: DRBD synchronously replicates the write, so
  the boot image lands on the peers for free.

## Failure modes

| Symptom | Cause | Recovery |
|---|---|---|
| `4xx … requires ≥N nodes` | Type too big for the cluster | `--type cattle`, convert later. |
| `lvcreate … Insufficient free extents` | Thin pool full | Extend the thinpool / VG, retry. |
| `no free DRBD minor in 1102..1189` | ~87 VM disks already allocated | Remove unused resources. |
| `drbdadm up` → "Exclusive open failed" | LV left busy by a prior run | `lsof \| grep /dev/drbd`; `drbdadm down <r>`; retry. |
| `virsh define on peer FAILED` (warning) | Peer can't accept the domain | Saga still completes; that peer can't host failover until fixed. |
| Saga 500 at a step | Underlying command failed | Task drawer / `operations` row shows `last_step`; fix the cause and re-POST (resume is idempotent). |

## Related

- Add HA after the fact: [`vm-convert.md`](vm-convert.md) (cattle → pet → ViPet).
- Move a VM: [`vm-migrate.md`](vm-migrate.md).
- Change priority / resources live: [`vm-settings.md`](vm-settings.md).
- Remove: [`vm-lifecycle.md`](vm-lifecycle.md).
