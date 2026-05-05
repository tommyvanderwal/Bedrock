# Bedrock Storage Stack

## One disk per node, one VG, one thin pool

Every Bedrock node — testbed sim or real mini-PC — owns **one** physical
disk and uses it maximally. The OS installer carves off the minimum
required for boot (EFI + /boot), everything else is a single LVM VG
with a single thin pool. Every dynamic LV — host root, optional swap,
storage tiers, every VM disk — lives as a thin LV in that one pool, so
free space is always reallocatable between any of them.

```
┌────────────────────────────────────────────────────────────────────┐
│             /dev/nvme0n1   (≈1 TB on real hw, 130 GB testbed)      │
├────────────────────────────────────────────────────────────────────┤
│  p1   EFI       ~500 MB    vfat        → /boot/efi   (UEFI req)    │
├────────────────────────────────────────────────────────────────────┤
│  p2   /boot     ~1 GB      xfs         → /boot       (kernel/grub) │
├────────────────────────────────────────────────────────────────────┤
│  p3   LVM PV    rest of disk           → VG `bedrock` (the only)   │
│                                                                    │
│   bedrock/thinpool   (≈99% of VG free space)                       │
│     ├─ root           thin xfs   /                                 │
│     ├─ swap           thin (opt-in, default disabled)              │
│     ├─ tier-scratch   thin xfs   /var/lib/bedrock/local/scratch    │
│     ├─ tier-bulk      thin xfs   /var/lib/bedrock/local/bulk       │
│     ├─ tier-critical  thin xfs   /var/lib/bedrock/local/critical   │
│     ├─ vm-X-disk0     thin raw   qemu (cattle) / DRBD backing      │
│     ├─ vm-X-disk0-meta (32 MB, plain LV — DRBD external metadata,  │
│     │                  outside the pool to avoid a meta-allocation │
│     │                  loop on pool-full)                          │
│     ├─ vm-X-disk1     thin                                         │
│     ├─ vm-Y-disk0     thin                                         │
│     └─ …    (one thin LV per VM per disk; allocated lazily)        │
└────────────────────────────────────────────────────────────────────┘
```

**Why exactly this**:
- Single VG = no "which VG do I pick?" decisions in code, no risk of
  data landing on the wrong disk.
- Single thin pool = capacity moves freely between consumers. A new
  VM doesn't fight a pre-allocated tier for its space.
- Storage tier LVs are **thin**, not thick — `tier-bulk` is sized at
  e.g. 30 GB virtually but only consumes blocks as files are
  written. The tier-vs-VM-disk competition is fair.
- DRBD external metadata LVs are **thick** (small, ~32 MB each)
  because DRBD must be able to write metadata even when the data
  thin pool is full; otherwise replication stalls.

## How a VM gets its disk

```
┌───────────────────────────────────────────────┐
│              Linux / Windows VM                │
│   /  (ext4 / xfs / NTFS)                       │
│   │  fstrim or `discard` mount option          │
│   ▼                                            │
│   virtio-blk (driver discard='unmap')          │
└──────┬─────────────────────────────────────────┘
       │ paravirt I/O
┌──────┼─────────────────────────────────────────┐
│      ▼            QEMU / KVM                   │
│  -blockdev type=host_device                    │
│    path=/dev/drbd2  (pet/vipet)  OR            │
│    path=/dev/bedrock/vm-X-disk0  (cattle)      │
│  cache=none discard=unmap detect-zeroes=unmap  │
└──────┬─────────────────────────────────────────┘
       │ raw block
┌──────┼─────────────────────────────────────────┐
│      ▼   DRBD 9.3.x   (only for pet/vipet)     │
│  /dev/drbd2  ↔  peer node's /dev/drbd2         │
│  external metadata on vm-X-disk0-meta          │
│  discard-zeroes-if-aligned yes                 │
└──────┬─────────────────────────────────────────┘
       │ replicated block I/O
┌──────┼─────────────────────────────────────────┐
│      ▼   LVM thin LV  /dev/bedrock/vm-X-disk0  │
│  --discards passdown  (thin_pool_discards in   │
│                        lvm.conf, default)      │
└──────┬─────────────────────────────────────────┘
       │ thin pool block I/O
┌──────┼─────────────────────────────────────────┐
│      ▼   NVMe SSD                              │
│  FITRIM / SCSI UNMAP / NVMe Dataset Mgmt       │
│  fstrim.timer weekly + on each TRIM call       │
└────────────────────────────────────────────────┘
```

Every layer must pass discard through for thin-pool reclaim to actually
free SSD blocks. The `/support` page in the dashboard verifies this
end-to-end live.

## Where bedrock-bootstrap puts the config

| Setting | Where | Why |
|---|---|---|
| `thin_pool_discards = passdown` | `/etc/lvm/lvm.conf.d/bedrock.conf` | LVM passes discard from thin LV → pool → PV → SSD. Default in modern LVM, but bedrock pins it explicitly. |
| `fstrim.timer` enabled | systemd | Weekly fallback fstrim across all mounts; covers any FS mounted without `discard` option. |
| `discard='unmap'` on every libvirt disk | `mgmt/app.py` VM-create XML | Guest TRIM hits qemu's discard handler instead of being silently dropped. |
| `discard-zeroes-if-aligned yes` | every `/etc/drbd.d/*.res` | DRBD passes discard to its backing LV instead of treating it as a write-zeros mirror. |
| `mkfs.xfs -m crc=1,reflink=0` | bedrock-bootstrap on tier LVs | xfs metadata + discard work better with reflink off on thin-pool-backed devices. |

## Sizing rules

| Component | Size | Notes |
|---|---|---|
| EFI partition | 500 MB | Plenty for grub2 + a few kernels |
| `/boot` | 1 GB | xfs; holds 6+ kernels comfortably |
| LVM PV | rest of disk | Whatever the disk has minus the two boot partitions |
| Host `root` thin LV | start ~16 GB virtual, grow as used | xfs supports online grow but not shrink |
| `tier-scratch` | 20 GB virtual default | tunable per-node via `/etc/bedrock/tier-sizes.json` |
| `tier-bulk` | 30 GB virtual default | |
| `tier-critical` | 5 GB virtual default | |
| VM disks | operator-specified at create time | thin LVs grow lazily |
| swap | 0 GB by default | opt in via `bedrock storage swap-set <gb>` |

**No artificial pool-fill cap.** Bedrock never refuses operations
because the pool is X% full. Operator may need to allocate a small LV
to migrate a much larger workload off the node — blocking that is
worse than letting the pool reach 99%. The supportability dashboard
warns at 70% and alarms at 80%; writes only fail when the pool
genuinely runs out of physical extents.

## Sources

- LVM thin discard: `lvmthin(7)`, section "DISCARD"
- DRBD discard: <https://docs.linbit.com/docs/users-guide-9.0/#s-discard-zeroes>
- libvirt qemu discard: <https://libvirt.org/formatdomain.html#hard-drives-floppy-disks-cdroms>
- AlmaLinux 10 LVM kernel module: shipped in stock kernel; no extra package needed
- `kmod-drbd9x` for el10: ELRepo, version 9.3.x against kernel 6.13 (el10_1)
