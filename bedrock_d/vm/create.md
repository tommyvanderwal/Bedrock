# bedrock_d/vm/create.py

The `vm_create` saga: the single path that turns a `bedrock vm create` request
into a running VM. It runs in the saga executor inside `bedrock-d`, driven by
the mgmt `/api/vms` endpoint. Per disk it allocates a DRBD minor, creates the
data + meta LVs on every peer, writes the `.res` config, brings DRBD up and
promotes the home node to Primary, fills the boot disk, defines the libvirt
domain on the home node, propagates that domain to the other peers, records
disk UUIDs, and registers the VM in rqlite. Each `@step` is crash-resumable and
idempotent so a mid-saga crash re-runs cleanly. Cattle VMs skip every DRBD step
(plain local thin LV, single peer); pet (2-way) and vipet (3-way) VMs get
external-meta DRBD on every peer so any peer can take over on host death.

## Functions / Classes

### `class VmCreate` — `@saga("vm_create")`
The saga. Each method below is a `@step`; the executor runs them in declaration
order, persisting `ctx` between steps. `ctx` carries `vm_name, vcpus, ram_mb,
disk_gb, extra_disks, vm_type, priority, iso, peers, home` in, and the executor
fills `ctx["disks"]` (and `is_replicated`, `libvirt_xml`) as steps run.

- **In (ctx):** `vm_name` str; `vcpus`/`ram_mb`/`disk_gb` ints; `extra_disks`
  list[int] (extra data-disk sizes, vdb, vdc…); `vm_type` one of
  `cattle`/`pet`/`vipet`; `priority` `low`/`normal`/`high`; `iso` optional
  filename in `/mnt/bedrock/iso/`; `peers` list of node names (1/2/3 for
  cattle/pet/vipet); `home` node name where the VM runs (must be `peers[0]`).
- **Out:** a running, registered VM. Side effects across steps: rqlite rows in
  `drbd_resources` and `vms`; LVs (`bedrock-data-<r>` / `bedrock-meta-<r>` or a
  single local thin LV); `/etc/drbd.d/<resource>.res` on each peer; a libvirt
  domain defined on every peer and started on home; many `_run_on` subprocess
  calls (`lvcreate`, `drbdadm`, `blkid`, `curl`, `qemu-img`, `virt-install`,
  `virsh`).

#### Steps (in order)

`step_validate("validate_request")` — checks required ctx fields are present,
that the peer count matches the vm_type (cattle 1 / pet 2 / vipet 3), and that
`home` is in `peers`; builds the per-disk plan `ctx["disks"]` (disk0 from
`disk_gb` plus `extra_disks`, each `{index, resource, size_gb}`) and sets
`ctx["is_replicated"]` for pet/vipet. No side effects beyond ctx.

`step_allocate_minors("allocate_minors")` — for each replicated disk, picks a
free DRBD minor in 1102..1189 by querying `drbd_resources`. Reuses an existing
row's minor on resume; raises `RuntimeError` if the band is exhausted. Sets each
disk's `minor` and `port` (`_cfg.drbd_port_for`). No-op for cattle.

`step_register_resources("register_drbd_resources")` — fills each disk's
`data_lv`/`meta_lv` (`_lvm.lv_names_for`). For replicated disks, `INSERT OR
REPLACE`s the `drbd_resources` row (name, minor, LVs, thinpool, data/meta sizes
in bytes, `max_peers=7`, peers JSON, timestamps) so a resume already has minor +
intended peers. Cattle writes no row.

`step_lvcreate("lvcreate_on_peers")` — on every peer, replicated disks get a
data+meta pair via `_lvm.lvcreate_pair`; cattle gets a single thin data LV
(`lvcreate -V … --thin`) guarded by `lv_exists`. Idempotent (both check `lvs`).

`step_write_config("write_drbd_config")` — replicated only. Renders
`/etc/drbd.d/<resource>.res` (`_cfg.render` over `_peer_metadata`) and writes it
on every peer. Content is deterministic given (peers, minor), so re-runs
overwrite with identical bytes.

`step_create_md("drbd_create_md")` — replicated only. `drbdadm create-md
--force --max-peers=7` per disk on every peer, `check=False` (already-existing
metadata exits non-zero and is tolerated).

`step_drbd_up("drbd_up")` — replicated only. `drbdadm up` per disk on every
peer, `check=False`. Idempotent.

`step_drbd_primary("drbd_primary")` — replicated only. `drbdadm primary
--force` per disk on the home node so the image step can write disk0 and libvirt
can boot, `check=False`. Promoting an already-Primary resource is a no-op.

`step_write_image("write_boot_image")` — when no ISO is given, fills disk0's
block device on the home node with the cached Alpine image. Skips if `blkid`
finds a filesystem signature. Device is `/dev/drbd<minor>` (replicated) or the
LV path (cattle). Downloads `alpine.qcow2` to `/var/lib/bedrock` if absent
(`curl`, 300s), then `qemu-img convert … -O raw` onto the device. ISO VMs skip
this entirely (they install from CDROM).

`step_install("virsh_install")` — on the home node, `virt-install` defines and
starts the domain (skipped if `virsh dominfo` shows it already exists), then
`virsh dumpxml` into `ctx["libvirt_xml"]`. Disks attach raw/virtio/cache=none/
discard=unmap; ISO VMs use `--cdrom` + `--boot cdrom,hd`, else `--import` +
`--boot hd`. Network `bridge=br0,model=virtio`; VNC on 0.0.0.0; QEMU
guest-agent virtio channel.

`step_define_peers("virsh_define_on_peers")` — replicated only. Ships the dumped
XML to each non-home peer and `virsh define`s it so the VM can fail over there.
A failed define logs a warning (that peer cannot host the VM) but does not abort
the saga. Cattle no-ops.

`step_record_uuids("record_disk_uuids")` — replicated only. Calls
`bedrock_d.vm.failover.record_uuid_after_promote(resource)` per disk to store
the post-promote DRBD current-UUID in rqlite, giving the first failover's
exact-equality safety check (INV-5) a quorum-confirmed baseline. Exceptions are
logged and swallowed.

`step_register_vm("register_vm")` — `INSERT OR REPLACE`s the `vms` row with
`state='running'`, `host=home`, ram/disk, `priority` (defaulted to `normal` if
invalid), and `failover_order`: `[]` for cattle, `peers` verbatim for pet/vipet
(so `peers[0]=home=primary`). The failover orchestrator reads `failover_order`
to decide which surviving peer is next in line.

## Helpers

- `_peer_hosts(peer_names) -> list[str]` — maps node names to their LAN `host`
  IPs from the rqlite `nodes` table (read level `none`, works without quorum);
  preserves input order; raises if any name has no host.
- `_peer_metadata(peer_names) -> list[Peer]` — builds `drbd_config.Peer` objects
  (`node_id` = list position, `loopback_ip` from `nodes`) for `_cfg.render`;
  raises if a node lacks host or loopback_ip.

## How it works

Steps run strictly in declaration order, top to bottom. The split between
"pre-flight + register" and "actually provision" is deliberate: validation and
the rqlite `drbd_resources` rows are written *before* any LV or DRBD command, so
a crash partway through provisioning resumes with the minor and peer list
already pinned in rqlite rather than re-deriving them.

```
validate ─ allocate_minors ─ register_drbd_resources    (plan + rqlite rows)
                │ reuse minor on resume; INSERT OR REPLACE
                ▼
lvcreate ─ write_config ─ create_md ─ up ─ primary       (DRBD on all peers,
                │  every cmd idempotent / check=False        promote on home)
                ▼
write_boot_image (home, skip if signature / skip if ISO)
                ▼
virsh_install (home: define+start, dump XML) ─ define_on_peers (other peers)
                ▼
record_disk_uuids (UUID baseline) ─ register_vm (vms row, state=running)
```

The `is_replicated` flag (set in validate from `vm_type`) is the master switch:
every DRBD step early-returns for cattle, so a cattle VM is just a local thin LV
+ a libvirt domain on its single peer with `failover_order=[]`.

Idempotency comes from each step checking real on-disk/rqlite state first
(`lvs`, `blkid`, `virsh dominfo`) or tolerating already-done commands
(`check=False` on `create-md`/`up`/`primary`, `INSERT OR REPLACE` on rows). The
one non-fatal step is `virsh_define_on_peers`: a peer that fails to accept the
definition is logged as unable to host failover, but the saga still completes.

DRBD minor → port layout (the band the allocator works within):

```
minor:  1101        1102 .. 1131   1132 1133 1134   1135 .. 1189
        │           │               │    │    │      │
        cluster     VM disks        reserved        VM disks
        singleton                  (skip: map to
                                    netd probe 7732 /
                                    advert 7733 /
                                    heartbeat 7734)

port = drbd_port_for(minor) = 7700 + minor - 1100   → lands in 7700-7799
```

Minors 1132/1133/1134 are excluded from allocation because their derived DRBD
ports collide with netd's mesh-probe, advert, and election-heartbeat UDP ports.

## Why

Resources are recorded in rqlite before the LV/DRBD work so the minor and peer
set survive a crash and the resumed run is deterministic rather than re-racing
for a free minor. DRBD metadata is created with `--max-peers=7` up front so a
2-way pet can later be grown without re-creating metadata. The home node is
promoted Primary before image-fill and `virt-install` because only a Primary can
back a writable block device for the boot disk and the running domain.
