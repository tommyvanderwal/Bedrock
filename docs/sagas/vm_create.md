# Saga: `vm_create`

**Module:** `bedrock_d/vm/create.py`  
**Class:** `VmCreate`

## Purpose

Provision a new VM (`cattle` / `pet` / `vipet` type). Per the
storage architecture, every VM disk is backed by a per-resource
DRBD pair (`bedrock-data-vm-<name>-disk0` + `bedrock-meta-vm-<name>-disk0`)
mirrored to `peers` (1 for cattle, 2 for pet, 3 for vipet). On
each peer the LVs live in the shared thinpool.

## Trigger

`POST /api/vms` from the dashboard / CLI. The route submits the
saga via the executor with `target_node = home`, where the steps
that need to run on every peer SSH into them.

## Inputs (`ctx`)

| key | type | meaning |
|-----|------|---------|
| `vm_name` | str | Globally unique VM name |
| `vcpus` | int | vCPU count |
| `ram_mb` | int | RAM in MiB |
| `disk_gb` | int | Data LV size in GiB |
| `vm_type` | `"cattle"\|"pet"\|"vipet"` | Determines how many peers replicate |
| `priority` | `"low"\|"normal"\|"high"` | QoS hint (currently informational) |
| `iso` | str (optional) | Filename in `/mnt/bedrock/iso/` to boot from. If empty, a base Alpine image is written to the DRBD device. |
| `peers` | list[str] | 1 / 2 / 3 node names depending on vm_type |
| `home` | str | Node where the VM runs (must be in `peers`) |

## Outputs (`ctx`)

| key | filled by | meaning |
|-----|-----------|---------|
| `minor` | `allocate_minor` | DRBD minor in 1200..1899 (cluster-singleton lives at 1101) |
| `port` | `allocate_minor` | DRBD link port = `7700 + minor` |
| `data_lv` | `lvcreate_pair_on_peers` | `bedrock-data-vm-<name>-disk0` |
| `meta_lv` | `lvcreate_pair_on_peers` | `bedrock-meta-vm-<name>-disk0` |
| `libvirt_xml` | `write_libvirt_xml` | Rendered domain XML |

## Step overview

| # | Step | What it does |
|---|------|--------------|
| 1 | [`validate_request`](#validate_request) | Sanity-check ctx; refuse malformed requests |
| 2 | [`allocate_minor`](#allocate_minor) | Pick free DRBD minor (re-use existing row on resume) |
| 3 | [`register_drbd_resource`](#register_drbd_resource) | Insert row into rqlite `drbd_resources` |
| 4 | [`lvcreate_pair_on_peers`](#lvcreate_pair_on_peers) | `lvcreate data + meta` on every peer (idempotent) |
| 5 | [`write_drbd_config`](#write_drbd_config) | Render and write `/etc/drbd.d/vm-<name>-disk0.res` on every peer |
| 6 | [`drbd_create_md`](#drbd_create_md) | `drbdadm create-md --max-peers=7` on every peer |
| 7 | [`drbd_up`](#drbd_up) | `drbdadm up` on every peer (idempotent) |
| 8 | [`drbd_primary`](#drbd_primary) | `drbdadm primary --force` on `home` (initial sync source) |
| 9 | [`fetch_base_image`](#fetch_base_image) | Ensure Alpine cloud image on `home` (skipped if `iso` set) |
| 10 | [`write_image_to_drbd`](#write_image_to_drbd) | `qemu-img convert` base image onto `/dev/drbdN` (skipped if `iso` set; skipped if data already present) |
| 11 | [`write_libvirt_xml`](#write_libvirt_xml) | Render domain XML; write `/tmp/<name>.xml` on every peer |
| 12 | [`virsh_define`](#virsh_define) | `virsh define` on every peer |
| 13 | [`register_vm`](#register_vm) | Insert row into rqlite `vms` (state=`created`) |

## Revert

The inverse is [`vm_destroy`](vm_destroy.md). Submit it with the
same `vm_name` and it reverses each effect in the safe order
(virsh destroy → undefine → drbd down → wipe-md → res file remove →
lvremove → delete rqlite rows).

A `vm_create` that partially completed and never reached step 13
leaves a half-state visible in `drbd_resources` but absent from
`vms`. `vm_destroy` handles this case — it reads `drbd_resources`
to know which LVs/DRBD to clean up.

## Idempotency / resume

Every step opens with the appropriate idempotency check:
- `allocate_minor` re-uses the existing row if one exists for this `vm_name`
- `lvcreate_pair_on_peers` uses `lv_exists` per LV before creating
- `drbd_create_md` checks `drbdadm status` and skips if configured
- `drbd_up` is naturally idempotent ("Minor already exists" is success)
- `drbd_primary` is idempotent on the home node (no-op if already Primary)
- `write_image_to_drbd` skips if `blkid` reports an existing filesystem
- `virsh_define` updates the existing definition in place

Resume after crash: re-submitting the same vm_create op (via
`/api/operations` retry) walks the not-`done` steps. The saga is
safe to re-run on the home node any number of times — every step
converges to the desired final state without re-doing finished
work.

## Step details

### `validate_request`

Refuses if:
- Any required ctx field is missing or empty
- `vm_type` not in `{cattle, pet, vipet}`
- `len(peers)` doesn't match the type's expectation (1 / 2 / 3)
- `home` not in `peers`

### `allocate_minor`

Reads `drbd_resources WHERE name = "vm-<name>-disk0"`:
- If a row exists: re-use its `minor` (saga-resume safe).
- Otherwise: pick the lowest integer in `[1200, 1899]` not already
  used by another `drbd_resources` row.

`port = 7700 + minor` (i.e. minor 1200 → port 8900).

VM minors start at 1200 to keep them clearly above the
cluster-singleton minor 1101 in `drbdadm status` output.

### `register_drbd_resource`

`INSERT OR IGNORE` a row into `drbd_resources` so the cluster has a
durable record of `name → minor` mapping before we burn any
on-disk state. Used by `vm_destroy` to find the resource later
even if other rqlite tables drift.

### `lvcreate_pair_on_peers`

For each peer in `ctx["peers"]`: SSH (or run locally if peer is
self) and call `lvm.lvcreate_pair(host, resource, data_gb)`. That
helper computes meta-LV size from
`lvm.meta_size_mb_for(data_gb, max_peers=7)` and creates both LVs
as thin LVs in the `thinpool`. Idempotent per LV.

### `write_drbd_config`

Renders `tier_storage.render_drbd_res_mesh()` for the per-VM
resource — full mesh, every peer pair, single minor. Writes
`/etc/drbd.d/vm-<name>-disk0.res` on every peer.

### `drbd_create_md`

`drbdadm create-md --force --max-peers=7 vm-<name>-disk0` on every
peer. `--max-peers=7` is baked in at create-md once; raising it
later is the only thing that needs downtime.

Skips if `drbdadm status vm-<name>-disk0` already reports the
resource configured (idempotent on retry).

### `drbd_up`

`drbdadm up vm-<name>-disk0` on every peer. Idempotent — DRBD's
"Minor or volume exists already" return is treated as success.

### `drbd_primary`

`drbdadm primary --force vm-<name>-disk0` on the `home` node only.
The other peers stay Secondary so writes flow home → peers.

### `fetch_base_image`

If `ctx["iso"]` is set (ISO-booted VM), this step is a no-op.
Otherwise it ensures `/var/lib/bedrock/alpine.qcow2` exists on the
home node (downloads via the legacy `vm._download_alpine_on_node`
helper). Skipped if file already present.

### `write_image_to_drbd`

If `ctx["iso"]` is set, this step is a no-op (install runs from
CDROM). Otherwise it `qemu-img convert -f qcow2 -O raw` the Alpine
image onto `/dev/drbd<minor>` on the home node.

Lightweight idempotency: skips if `blkid -s TYPE -o value
/dev/drbdN` returns anything (means data is already there from a
previous run).

### `write_libvirt_xml`

Uses the legacy `vm._vm_xml_cattle` / `_vm_xml_pet` helpers to
render a libvirt domain XML appropriate to the VM type. Writes the
XML to `/tmp/<vm_name>.xml` on **every** peer so `virsh define`
in the next step can reference it.

(A future Stage 8.1 PR replaces these helpers with a clean
`bedrock_d/vm/libvirt_xml.py` module.)

### `virsh_define`

`virsh define /tmp/<vm_name>.xml` on every peer. virsh on an
already-defined VM updates the in-place definition — idempotent.

### `register_vm`

`INSERT OR REPLACE INTO vms (vm_name, vm_type, host, ram_mb,
disk_gb, state, updated_at) VALUES (?, ?, ?, ?, ?, 'created', ?)`.
State is `created` (NOT `running` yet) — a separate
`bedrock vm start` (or auto-start hook) flips it to `running`.
