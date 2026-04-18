# VM settings (Resources, Priority, HA, CDROM)

A settings page at `/vm/<name>/settings` collects every per-VM knob an
operator needs. Each section shows **clearly** whether the change is
applied live or queued for the next reboot.

**Source:** `mgmt/ui/src/routes/vm/[name]/settings/+page.svelte` (UI) and
the `_vm_get_settings`, `_vm_set_resources`, `_vm_set_priority`,
`_vm_set_cdrom` helpers in `mgmt/app.py`.

## Layout

```
  ┌── Resources ──────────────────────────────┐
  │ vCPUs     [ 2 ]    ⟳ applies on next reboot
  │ RAM (MB)  [ 2048 ] ⟳ applies on next reboot
  │ Disk (GB) [ 20 ]   ✓ grow applies live    │
  │ [ Save resources ]  Current: 2 · 2048 · 20│
  └───────────────────────────────────────────┘

  ┌── Priority ── ✓ live (cpu_shares) ────────┐
  │ ( )low   (•)normal   ( )high               │
  │ cpu_shares on host: 1024                   │
  └───────────────────────────────────────────┘

  ┌── HA Replication ── ✓ conversion is online ┐
  │ [x] PET (2-way DRBD)                       │
  │    [ ] ViPet (3-way DRBD)                  │
  │ Current: pet    Resource: vm-foo-disk0     │
  └───────────────────────────────────────────┘

  ┌── CD-ROM ── ✓ live eject / insert ─────────┐
  │ Currently: foo.iso       [ Eject ]         │
  │ Insert:  [ bar.iso  ▾ ]  [ Insert ]         │
  └───────────────────────────────────────────┘
```

## What happens per knob

| Knob | Mechanism | Live? | Notes |
|---|---|---|---|
| vCPUs | `virsh setvcpus NAME N --config --maximum` + `--config` | reboot | Hot-add is possible on Linux with CPU hot-plug slots pre-declared; we keep it simple and reboot-queued. |
| RAM  | `virsh setmaxmem NAME KIB --config` + `virsh setmem NAME KIB --config` | reboot | Same reasoning as vCPU — avoids balloon/maxmem mismatch. |
| Disk | `lvextend -L +<Δ>G` then `virsh blockresize NAME <target> <size>K` | **live** (grow only) | For pet/ViPet: extend on every peer first, then `drbdadm resize` on the Primary — propagates. Guest may need a rescan inside the OS (Linux auto-detects on virtio-blk; Windows: Disk Management → Rescan Disks). |
| Priority | `virsh schedinfo NAME --live --config cpu_shares=N` | **live** | Mapping: low=256, normal=1024, high=4096 (cgroup cpu.weight on v2, cpu.shares on v1). Written to both running VM and XML for next boot. |
| HA (cattle ↔ pet ↔ ViPet) | Convert pipeline — [`vm-convert.md`](vm-convert.md) | **live** | Moved from the detail page to settings; logic is unchanged. |
| CDROM eject | `virsh change-media NAME sda --eject --live --force` | **live** | Requires a CDROM slot to exist (created when the VM had an install ISO). |
| CDROM insert | `virsh change-media NAME sda /mnt/isos/X --insert --live --force` | **live** | From any ISO in `/opt/bedrock/iso` (virtio-win hidden from dropdown). |

## API endpoints

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/api/vms/{name}/settings` | — | full config blob (see below) |
| POST | `/api/vms/{name}/resources` | `{vcpus?, ram_mb?, disk_gb?}` | per-field `{applied, requires_reboot, note}` |
| POST | `/api/vms/{name}/priority` | `{priority: "low"|"normal"|"high"}` | `{applied, priority, cpu_shares}` |
| POST | `/api/vms/{name}/cdrom` | `{action: "eject"|"insert", iso?: string}` | `{applied, note}` |

Settings blob:

```json
{
  "name": "win",
  "host": "192.168.2.152",
  "vcpus": 2,
  "ram_mb": 2048,
  "disk_gb": 25,
  "disk_path": "/dev/almalinux/vm-win-disk0",
  "disk_target": "vda",
  "drbd_resource": "",
  "cdrom_slot": "sda",
  "cdrom_iso": "SERVER_EVAL_x64FRE_en-us.iso",
  "priority": "high",
  "cpu_shares": 4096
}
```

## Log lines

```
VM NAME: vcpus → N (reboot required)                   level=info
VM NAME: ram → N MB (reboot required)                  level=info
VM NAME: disk grown <from>G → <to>G (live)             level=info
VM NAME: priority → <p> (cpu_shares=<N>, live)         level=info
VM NAME: ejected CDROM                                 level=info
VM NAME: inserted <name>.iso                           level=info
```

All stream immediately into the Recent Logs panel via the WS `event`
channel, same as every other push_log.

## Why disk grow is live but RAM/vCPU aren't

Disk grow: QEMU's `blockresize` is a well-supported monitor command;
the kernel sees a size change on the block device and any rescan inside
the guest is a read-only refresh. Failure mode is bounded (the guest
sees the old size until it rescans).

vCPU / RAM: live-adjusting these requires either declaring memory/CPU
hot-plug slots in the domain XML ahead of time, or using balloon drivers
(for RAM down). Supporting it cleanly would require either (a) always
declaring large slot maximums at create time (wastes XML churn) or (b)
different code paths per guest OS. Bedrock's rule: **configure at
create, reboot to apply.** Good enough, one less edge case.

## Failure modes

| Symptom | Cause | Recovery |
|---|---|---|
| Disk shrink request returns 400 | Shrinking is not supported | Destroy + recreate at smaller size, or shrink the FS inside the guest and leave the LV alone. |
| Disk grow returns 500 on pet/ViPet | Peer LV couldn't be extended (thin pool full) | Free thin-pool space on the affected peer. |
| Disk didn't appear larger in guest | OS hasn't rescanned | Linux virtio-blk usually auto-detects; on Windows: Disk Management → Action → Rescan Disks. |
| Eject fails with "disk bus 'sata' cannot be hotplugged" | The CDROM device is on a bus that doesn't support eject | If the VM was created without an ISO, it has no SATA CDROM. The UI disables the Eject/Insert controls in that case. |
| Priority change returns 400 | Invalid priority string | Use `low`, `normal`, or `high`. |

## Related

- [`vm-convert.md`](vm-convert.md) — HA section of settings pages delegates
  to the convert pipeline.
- [`iso-library.md`](iso-library.md) — where CDROM ISO choices come from
  and how they're mounted cluster-wide.
- [`../reference/api.md`](../reference/api.md) — canonical API index.
