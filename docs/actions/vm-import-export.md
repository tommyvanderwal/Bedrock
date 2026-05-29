# VM import / export

Bedrock ingests existing disk images from other hypervisors (VMware,
Hyper-V, raw, qcow2) and produces portable exports in `qcow2`, `vmdk`,
`vhdx`, or `raw`. Imports always land as **cattle** VMs (one local thin
LV, no DRBD) with `machine=q35`, source-matched firmware, and a UTC
clock; use the PET / ViPet controls on the VM page afterwards to add
replication.

**Source:** `mgmt/app.py` — the `/api/imports*` and `/api/exports*` /
`/api/vms/<name>/export` endpoints, plus `_inspect_os`, `_run_convert`,
`_run_export`, `_vm_create_from_import`. UI: `/imports` for ingest,
`/vm/<name>/settings` for export.

Import does **not** go through the `bedrock_d` vm_create saga (which
only fills a disk from the cached Alpine image or an ISO). Import needs
source-disk firmware sniffing, Windows enlightenments, and import-meta
consumption the saga does not model, and is cattle-only.

## File layout

```
  /opt/bedrock/imports/
    <job-id>/
      original.<ext>         uploaded file
      converted/
        disk.qcow2           single-disk qemu-img output
        disk0.qcow2 ...       Linux/generic OVA: one per source VMDK
        <name>-sda ...        virt-v2v output (Windows)
        <name>.xml            virt-v2v domain sidecar (firmware/OS hints)
      log.txt                 convert command output
      meta.json               id, status, sizes, detected OS + firmware

  /opt/bedrock/exports/
    <job-id>/
      <vm>.<fmt>             the exported image
      log.txt
      meta.json
```

Job id is `<unix-ts>-<slug>` — sortable and URL-safe. Each job is
self-contained; clean-up is `rm -rf <job-dir>` (the DELETE endpoints).

## Supported input formats

Allowlist (`IMPORT_INPUT_FORMATS`): `.ova .ovf .vmdk .vhd .vhdx
.qcow2 .raw .img`.

| Extension | Input format | Default conversion path |
|---|---|---|
| `.qcow2` | QEMU native | `qemu-img convert -f qcow2 -O qcow2` |
| `.raw`, `.img` | raw sectors | `qemu-img convert -f raw -O qcow2` |
| `.vmdk` | VMware | `qemu-img convert -f vmdk -O qcow2` |
| `.vhd` | Hyper-V gen 1 | `qemu-img convert -f vpc -O qcow2` |
| `.vhdx` | Hyper-V gen 2 | `qemu-img convert -f vhdx -O qcow2` |
| `.ova`, `.ovf` | OVA appliance | `tar -xf` → parse OVF → `qemu-img convert` per VMDK |
| any + drivers ticked | Windows guest | `virt-v2v` (inspect, rewrite boot, inject virtio) |

`QEMU_FORMAT_MAP` translates the extension to the qemu-img `-f` value
(`vhd → vpc`).

### Driver injection (`inject_drivers`)

The convert request is `{ inject_drivers: bool | null }`. When `null`
(the UI default), the backend auto-selects: it injects iff upload-time
OS inspection saw a Windows guest (`os_type == "windows"`). Explicit
`true` / `false` overrides detection.

- **Windows 7/8/8.1, Server 2008/2012/2012R2** — no inbox virtio.
  `inject_drivers` runs virt-v2v, which mounts the guest NTFS offline
  via libguestfs, copies `viostor.sys` + `netkvm.sys` (and scsi /
  balloon / serial / rng as present) into
  `C:\Windows\System32\drivers\`, edits the `SYSTEM` hive's `Services`
  + `CriticalDeviceDatabase` keys, and installs a first-boot
  `RHSrvAny` service to enumerate the rest on first login.
- **Windows 10/11, Server 2016/2019/2022** — virtio is inbox
  (Microsoft ships it for Azure). virt-v2v detects this and does a
  minimal conversion (just the domain XML).
- **Linux guests** — leave injection off; the qemu-img path is
  seconds.

Injection cost: 2-10 minutes per VM, longer under nested KVM. virt-v2v
boots a libguestfs appliance to edit the disk; `_run_cmd` sets
`LIBGUESTFS_MEMSIZE=2048` for it, and it needs `/var/tmp` space on the
order of the virtual-disk size.

### Multi-disk OVAs

OVAs with multiple VMDKs (appliances, OS+data SQL splits) convert
end-to-end. The Linux/generic path extracts the tar, parses the OVF
`<References>`/`<DiskSection>` to recover the disk file list **in slot
order**, and qemu-img-converts each to `disk0.qcow2`, `disk1.qcow2`,
... (falling back to a case-insensitive `vmdk → img → raw` glob if OVF
parse fails). The Windows path uses `virt-v2v -i ova`, which does the
same OVF parsing and emits `<name>-sda`, `-sdb`, ....

```
  OVA(disk-boot.vmdk + disk-data.vmdk)
       │  tar -xf → OVF slot order
       ▼
  disk-boot.vmdk → qemu-img → converted/disk0.qcow2   boot=true
  disk-data.vmdk → qemu-img → converted/disk1.qcow2   boot=false
       │  meta['disks'] = [{index, path, virtual_size_gb,
       │                    actual_size_bytes, boot}, ...]
       ▼
  create-vm: per disk → lvcreate thin + qemu-img sparse-convert
             virt-install --disk disk0 --disk disk1 → vda + vdb + ...
```

Both paths converge on one `meta['disks']` list, slot 0 = boot disk,
so `_vm_create_from_import` iterates them identically.

### OS inspection (at upload)

`api_imports_upload` runs `_inspect_os` synchronously after staging the
file (so the convert call that follows sees the result):

```
  virt-inspector --format <fmt> -a <disk>   # mounts FS, reads registry/os-release
      → operatingsystem/name → os_type (windows / linux / ...)
  if it fails AND fmt in (vpc, vhdx):        # Hyper-V-native → almost always Windows
      → os_type = "windows" (virt-v2v re-checks + corrects)
  else: os_detection = "none"
```

Result lands in `meta` (`os_type`, `os_product_name`, `os_version`,
`os_detection`) and drives the auto-select for driver injection.

### Firmware auto-detection

A BIOS/MBR boot disk cannot boot under UEFI firmware (Windows traps
`0x7B`, Linux drops to the EFI shell). Bedrock matches the source:

```
  if a virt-v2v *.xml sidecar exists:
      uefi  if "firmware='efi'" or "<firmware>efi</firmware>" present
      else bios
  else (qemu-img path):
      head = qemu-img dd -O raw bs=512 count=34 if=<boot-disk>
      uefi  if head[512:520] == b"EFI PART"   # LBA 1 of a GPT
      else bios
```

`meta.detected_firmware` (`bios`/`uefi`) shows in the Detected column on
`/imports`. `_vm_create_from_import` re-checks it and passes
`--boot uefi` to virt-install **iff** `uefi`; otherwise it omits the
flag and Q35 boots SeaBIOS.

### virtio-win driver ISO

`bedrock init` (mgmt install) fetches `virtio-win.iso` (~750 MB,
Red Hat-signed) to `/opt/bedrock/iso/`, and `seaweedfs.seed_iso_library`
copies it into the filer so it appears cluster-wide at
`/mnt/bedrock/iso/virtio-win.iso` on every node. It is filtered out of
the install-ISO dropdowns (`name != 'virtio-win.iso'`) so it can never
be chosen as a boot source. Import injection uses the host's
`/usr/share/virtio-win/` payloads via virt-v2v, not this ISO; the ISO
is for attaching as a second CDROM (via the VM's CDROM controls) when a
Windows installer needs storage drivers at install time. See
[`iso-library.md`](iso-library.md#virtio-winiso).

## Typical flow

```
  operator                       mgmt backend (master)
  ────────                       ─────────────────────
  POST /api/imports/upload        │ stream to disk in 1 MB chunks
      multipart (.vmdk/.vhdx/...) ├─► /opt/bedrock/imports/<id>/original.ext
                                   ├─► _inspect_os (virt-inspector, sync)
                                   ├─► meta.json {status:"uploaded", os_type, ...}
                                   ◀── 200 meta
  POST /api/imports/<id>/convert  │ status uploaded|failed → converting
      { inject_drivers?: bool }   │ asyncio.create_task(_run_convert)
                                   │   qemu-img (or virt-v2v) → converted/
                                   │   per-disk qemu-img info → meta['disks']
                                   │   (Windows: virt-win-reg RealTimeIsUniversal)
                                   ├─► meta.json {status:"ready",
                                   │              disks[], detected_firmware}
                                   ◀── 200 {status:"converting", inject_drivers}
  GET /api/imports/<id>           │ (UI polls ~2 s)
                                   ◀── status + log_tail
  POST /api/imports/<id>/create-vm│ status must be "ready"
      { name, vcpus, ram_mb,      │ task-tracked, runs _vm_create_from_import:
        priority }                │   thin-pool free-space preflight (507 if tight)
                                   │   per disk: lvcreate -V thin
                                   │     qemu-img convert -n -S 4k --target-is-zero
                                   │       -O raw <qcow2> /dev/vg/vm-<name>-diskN
                                   │   virt-install --machine q35 [--boot uefi]
                                   │     --disk ...,bus=virtio --network bridge=br0
                                   │     --clock offset=utc [+hyperv if Windows]
                                   │     --os-variant detect=on,name=generic
                                   │     --noautoconsole --wait 0 --import
                                   │   virsh schedinfo cpu_shares=<priority>
                                   │   inventory.json + meta{status:"consumed"}
                                   ◀── 200 {status:"accepted", task_id, name}
```

`--wait 0` defines and starts the domain, then returns (the disk holds
an OS, not an installer). `cpu_shares` map: low 256 / normal 1024 /
high 4096. For a detected Windows guest, `_vm_create_from_import` also
adds the Red Hat Hyper-V enlightenment `--features` set and
`hypervclock_present=yes`; for everything else it is a plain
`--clock offset=utc`. The UTC `RealTimeIsUniversal=1` registry merge in
the convert step keeps the guest from showing a local-time offset.

## Export

```
  POST /api/vms/<name>/export { format: "qcow2"|"vmdk"|"vhdx"|"raw" }
      → meta {status:"converting"}; asyncio.create_task(_run_export)
        local source:  qemu-img convert -p -f raw -O <fmt> /dev/<lv> → exports/...
        remote source: ssh root@<host> 'dd if=<lv> bs=1M' > fifo
                       && qemu-img convert -p -f raw -O <fmt> fifo → exports/...
      → status=ready
  GET    /api/exports/<id>/download   FileResponse (streaming), ready-only
  DELETE /api/exports/<id>            rm -rf the job dir
```

`_run_export` compares `src_host` against this node's bound IPs. If the
VM's disk is local it reads the LV in place; otherwise it streams from
the source node through a named pipe — no intermediate copy. The read
is live (DRBD/raw LVs stay read-consistent through QEMU's page cache).

## Log lines (Recent Logs panel)

```
Import uploaded: NAME (N MB, id=<id>)                                   info
Import <id> OS detected: TYPE PRODUCT (via DETECTION)                   info
Import convert started: <id> (ext, qemu-img | virt-v2v+drivers)         info
Import <id>: RealTimeIsUniversal=1 set                                  info
Import convert done: <id> → N disk(s), N G virtual total                info
Import convert FAILED: <id> (exit N)                                    error
Import <id> → create VM NAME: lvcreate NG thin (vm-NAME-diskN)          info
Import <id> → virt-install (N disk(s))                                  info
Imported VM NAME on HOST (vcpus=N, ram=NMB, disk0=NG, from FILE)        info
Import deleted: <id>                                                    info
Export started: NAME → FMT (id=<id>)                                    info
Export done: NAME (FMT, N MB)                                           info
Export FAILED: NAME (exit N)                                            error
```

## Failure modes

| Symptom | Cause | Recovery |
|---|---|---|
| `unsupported extension '.xxx'` on upload | Not in the allowlist | Rename to a supported extension, or `.raw` if it is a flat disk. |
| `Import convert FAILED ... exit 1` on vhdx/vmdk + drivers | virt-v2v's libguestfs appliance ran out of RAM/disk | Untick inject drivers (Linux guests need only qemu-img); free `/var/tmp`. |
| Create VM returns `507 Thin pool ... GB free` | Pool can't fit the disks + 1 GB slack | Free space or grow the pool; `/hosts` warns at ≥ 80 %. |
| Create VM `lvcreate ... Insufficient free extents` | Pool full mid-create | Grow the pool, retry (failed create unwinds its LVs). |
| Exported `.vhd` won't boot in Hyper-V | Gen-1 needs a fixed (not dynamic) VHD | Export to `.vhdx` (gen-2), or convert with `-o subformat=fixed`. |
| Download returns `400 status 'converting'` | Export still running | Wait; the UI shows Download only when ready. |

## Security

- **Path traversal blocked.** Job ids are matched against
  `[a-z0-9][a-z0-9_-]{0,63}`; upload filenames are reduced to
  `Path(name).name`; ISO inserts strip to the basename and require the
  file to exist under `/mnt/bedrock/iso`.
- **Bounded memory.** Uploads stream in 1 MB chunks straight to disk.
- **No shell interpolation.** `_run_cmd` passes an argv list. The sole
  `bash -c` is the cross-node export pipe, built from a `src_host` that
  came from the cluster's own node table.

## Why a separate /imports page

Upload + conversion is not instant (multi-GB images; virt-v2v takes
minutes). Splitting "upload + convert" from "create the VM" keeps long
conversions off the Create-VM form, gives the operator a visible queue
with progress / logs / retry, and lets one converted disk seed several
VMs.
