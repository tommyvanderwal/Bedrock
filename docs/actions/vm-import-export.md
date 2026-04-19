# VM import / export

Bedrock can ingest existing disk images from other hypervisors (VMware,
Hyper-V, raw qcow2 / img) and produce portable exports in any of
`qcow2`, `vmdk`, `vhdx`, `raw`. Imports always land as **cattle** VMs
with `machine=q35`, UEFI firmware, and `clock=utc`; use the PET / ViPet
checkboxes on the Settings page to add replication.

**Source:** `mgmt/app.py` (endpoints + `_run_convert`, `_run_export`,
`_vm_create_from_import`); `mgmt/ui/src/routes/imports/+page.svelte`;
export UI lives on `/vm/<name>/settings`.

## File layout

```
  /opt/bedrock/imports/
    <job-id>/
      original.<ext>         uploaded file
      converted/
        disk.qcow2           qemu-img path
        <name>-sda          \ virt-v2v path
        <name>.xml          / (driver-injected)
      log.txt                convert output
      meta.json              id, status, sizes, detected OS, …

  /opt/bedrock/exports/
    <job-id>/
      <vm>.<fmt>             the exported image
      log.txt
      meta.json
```

Job id is `<unix-ts>-<slug-of-original-name>` — stable, sortable, safe
for URLs. Each job is self-contained in its own directory; clean-up is
`rm -rf <job-dir>`.

## Supported input formats

| Extension | Input format | Conversion path | OS detection + drivers |
|---|---|---|---|
| `.qcow2` | QEMU native | `qemu-img convert -f qcow2 -O qcow2` (passthrough) | no |
| `.raw`, `.img` | raw sectors | `qemu-img convert -f raw -O qcow2` | no |
| `.vmdk` | VMware | `qemu-img convert -f vmdk -O qcow2` | no (default) |
| `.vhd` | Hyper-V gen 1 | `qemu-img convert -f vpc -O qcow2` | no |
| `.vhdx` | Hyper-V gen 2 | `qemu-img convert -f vhdx -O qcow2` | no |
| `.ova`, `.ovf` | VMware Appliance | `tar -xf` → find disk → `qemu-img convert` | no (default) |
| any + **Inject drivers** ticked | full `virt-v2v` | Inspects guest OS, rewrites boot config, **injects viostor + NetKVM on Windows** | yes |

### When to tick "Windows guest — inject virtio drivers"

- **Windows 7, Windows 8/8.1, Windows Server 2008/2012/2012R2** — no
  inbox virtio. virt-v2v copies `viostor.sys` + `netkvm.sys` into
  `C:\Windows\System32\drivers\`, edits the SYSTEM hive's
  `CriticalDeviceDatabase` + `Services` keys via libguestfs registry
  editing, installs a first-boot `RHSrvAny` service so PnP enumerates
  remaining drivers on first login. Same mechanism Datto DRaaS and
  Veeam use for VMware/Hyper-V → KVM migration.
- **Windows 10, Windows 11, Server 2016 / 2019 / 2022** — virtio is
  *inbox* (Microsoft ships viostor + NetKVM for Azure). virt-v2v
  detects this (`virt-v2v: This guest has virtio drivers installed.`)
  and does a minimal conversion — just the domain XML.
- **Linux guests** — don't need injection; just tick nothing and the
  qemu-img path is ~seconds.

Injection cost: 2–10 minutes per VM on modern hardware, longer in
nested-KVM testbeds. virt-v2v boots a libguestfs appliance to mount
and edit the guest disk — needs a few hundred MB RAM and /var/tmp
space ≥ virtual-disk-size.

### Firmware auto-detection

A Gen-1 Hyper-V VHD (MBR partition table) cannot boot on UEFI
firmware — Windows traps `0x7B INACCESSIBLE_BOOT_DEVICE`. Bedrock
reads the source's partition table during convert:

- **GPT** (EFI PART signature at LBA 1) → domain XML gets `<os firmware='efi'>`
- **MBR** → no firmware attribute (= BIOS, the libvirt default)

virt-v2v's sidecar XML carries its own firmware decision; when we
have it (injection path), we trust it. Otherwise we sniff 34 sectors
of the disk head and check for `EFI PART` at offset 512.

Detected firmware is stored in `meta.json` as `detected_firmware`
and shown under the Detected column on `/imports`. The Create VM
flow passes `--boot uefi` to virt-install iff GPT; otherwise it
omits the flag and Q35 firmware defaults to SeaBIOS.

### Verified with Windows Server 2022 Datacenter Eval

Microsoft's official eval VHD (build 20348.169, ~9.5 GB on disk,
40 GB virtual, MBR/BIOS) was imported end-to-end:

- Upload: 162 s (9.5 GB over LAN)
- virt-v2v convert: 115 s (small — no injection needed; inbox
  virtio detected)
- Create VM → virtio-only domain XML, Q35, BIOS, UTC clock
- First boot reached OOBE "Hi there" screen in ~180 s — proves
  kernel loaded via viostor, NTFS mounted, user-mode started
- No SATA, no e1000, no IDE, no rtl8139 in the domain XML.

## Typical flow

```
  operator                       mgmt backend
  ────────                       ────────────
  POST /api/imports/upload        │
      multipart (.vmdk/.vhdx/...) │  stream to disk in 1 MB chunks
                                   ├─► /opt/bedrock/imports/<id>/original.ext
                                   ├─► meta.json  {status: "uploaded"}
                                   ◀── 200 { id, ... }
  POST /api/imports/<id>/convert  │
      { inject_drivers: bool }    │  asyncio.create_task(_run_convert)
                                   ├─► qemu-img (or virt-v2v)
                                   │   output log to log.txt
                                   ├─► meta.json  {status: "ready",
                                   │                 virtual_size_gb, ...}
  GET /api/imports/<id>            │
      (poll every 2 s from UI)    ◀── status, log_tail
  POST /api/imports/<id>/create-vm│
      { name, vcpus, ram_mb,      │  lvcreate thin
        priority }                │  qemu-img qcow2→raw on LV
                                   │  virt-install --machine q35
                                   │    --boot uefi --clock offset=utc
                                   │    --os-variant detect=on
                                   │  virsh schedinfo --live --config
                                   │    cpu_shares=<N>
                                   │  inventory.json update
                                   ├─► meta.json  {status: "consumed",
                                   │                consumed_as: "<name>"}
                                   ◀── 200 { status, name, node }
```

## Export

```
  POST /api/vms/<name>/export { format: "qcow2" }
      → async qemu-img convert -f raw -O <fmt> /dev/lv → /opt/bedrock/exports/…
      → status=ready
  GET  /api/exports/<id>/download  (FileResponse, streaming)
  DELETE /api/exports/<id>         (clean up disk)
```

Export reads the live LV directly on the mgmt node. Cross-node export
(VM runs on sim-2 or sim-3) uses an `ssh | dd | qemu-img convert` pipe
through a FIFO — no intermediate copy on the source node.

## Log lines (in the Recent Logs panel)

```
Import uploaded: NAME (N MB, id=<id>)                            info
Import convert started: <id> (ext, qemu-img | virt-v2v+drivers)  info
Import convert done: <id> → disk.qcow2 (N G virtual)             info
Import convert FAILED: <id> (exit N)                             error
Import <id> → create VM NAME: lvcreate NG thin                   info
Import <id> → qemu-img convert qcow2 → raw LV                    info
Import <id> → virt-install                                       info
Imported VM NAME on <host> (vcpus=N, ram=NMB, NGB, from FILE)    info
Import deleted: <id>                                             info
Export started: NAME → FMT (id=<id>)                             info
Export done: NAME (FMT, N MB)                                    info
Export FAILED: NAME (exit N)                                     error
```

## Failure modes

| Symptom | Cause | Recovery |
|---|---|---|
| `unsupported extension '.xxx'` on upload | Not in the allowlist | Rename to a supported extension, or use `.raw` if it's a flat disk. |
| `Import convert FAILED: ... exit 1` on .vhdx/.vmdk with drivers ticked | virt-v2v's libguestfs appliance ran out of RAM/disk | Untick "inject drivers" — for Linux guests, qemu-img alone works. |
| Create VM fails `lvcreate … Insufficient free extents` | Thin pool full | Grow the pool (`truncate -s NG /var/lib/bedrock-vg.img` + `losetup -c` + `pvresize` + `lvextend`). `/hosts` page warns at ≥ 80 %. |
| Exported .vhd won't boot in Hyper-V | Hyper-V needs fixed (not dynamic) VHDs for gen-1 boot disks | `qemu-img convert -o subformat=fixed …` — or export to `.vhdx` (gen-2). Follow-up: expose subformat in the UI. |
| Download returns 400 `status 'converting'` | Export still running | Wait; the UI polls every 2 s and shows a Download button only when ready. |

## Security

- **Path traversal blocked** on every operation — `Path(name).name` strips
  any slashes before the server writes.
- **Uploads stream in 1 MB chunks** direct to disk; memory usage bounded
  regardless of image size.
- **No shell interpolation in subprocess** — `_run_cmd` passes a list of
  args, not a string. The one `bash -c` in the export path only appears
  for cross-node streaming and uses a validated source host.
- **ISO filename whitelist** on create-VM and cdrom-insert: any path
  component other than the basename is stripped before the backend
  touches disk.

## Why a separate /imports page rather than baking it into Create VM

Upload + conversion is not instant (750 MB virtio-win.iso for drivers,
disk images can be gigabytes, virt-v2v takes minutes). Putting it
behind the "Create VM" button would either block the UI for minutes or
fake progress. Splitting "upload + convert" from "create the VM" means:

1. The operator uploads once and can create many VMs from the same
   converted disk (via Clone on the detail page — future).
2. Long-running conversions don't block the form.
3. The import list is a visible queue — progress + logs + retry — as
   operators expect from any import workflow.
