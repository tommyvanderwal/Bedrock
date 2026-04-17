# Bedrock Storage Stack

## Physical to Virtual — How a VM Gets Its Disk

```
┌─────────────────────────────────────────────────────┐
│                   WINDOWS VM                         │
│                                                     │
│   C:\  (NTFS)                                       │
│     │                                               │
│     ▼                                               │
│   VirtIO Block Driver (viostor)                     │
│     │  sees: 40GB disk                              │
│     │  TRIM: sends SCSI UNMAP / ATA TRIM            │
└─────┼───────────────────────────────────────────────┘
      │  virtio-blk (paravirtualized I/O)
      │
┌─────┼───────────────────────────────────────────────┐
│     ▼            QEMU / KVM                          │
│                                                     │
│   -blockdev driver=host_device                      │
│     path=/dev/drbd2                                 │
│     cache=none                                      │
│     discard=unmap  ◄── TRIM passed through          │
│     detect-zeroes=unmap                             │
└─────┼───────────────────────────────────────────────┘
      │  raw block I/O
      │
┌─────┼───────────────────────────────────────────────┐
│     ▼          DRBD 9.3.1                            │
│                                                     │
│   /dev/drbd2  (minor 2)                             │
│     │                                               │
│     ├── Writes: replicated synchronously (proto C)  │
│     │   to peer via TCP before ACK to QEMU          │
│     │                                               │
│     ├── Reads: served from local disk only           │
│     │                                               │
│     └── TRIM/Discard: passed down to backing dev    │
│         AND replicated to peer                      │
│                                                     │
│   Backing device: /dev/almalinux/vm-win-disk0       │
└─────┼───────────────────────────────────────────────┘
      │  block I/O
      │
┌─────┼───────────────────────────────────────────────┐
│     ▼       LVM Thin Provisioning                    │
│                                                     │
│   Thin LV: vm-win-disk0  (40GB virtual)             │
│     │  Actual allocation: only used blocks           │
│     │  TRIM/Discard: releases thin blocks back       │
│     │  to the pool                                  │
│     │                                               │
│   Thin Pool: almalinux/thinpool  (600GB)            │
│     │  Shared pool for all VM disks                  │
│     │  Each VM is an independent thin LV             │
└─────┼───────────────────────────────────────────────┘
      │  block I/O
      │
┌─────┼───────────────────────────────────────────────┐
│     ▼       LVM Volume Group                         │
│                                                     │
│   VG: almalinux  (952GB)                            │
│     ├── thinpool   600GB  (VM storage pool)          │
│     ├── root        70GB  (host OS)                  │
│     ├── home       100GB  (host /home)               │
│     ├── swap        14GB                             │
│     └── free      ~168GB  (expansion room)           │
└─────┼───────────────────────────────────────────────┘
      │  block I/O
      │
┌─────┼───────────────────────────────────────────────┐
│     ▼       Physical NVMe                            │
│                                                     │
│   /dev/nvme0n1  (953.9GB)                           │
│     Samsung/Micron NVMe SSD                          │
│     └── nvme0n1p3  (952.3GB) ── PV for VG           │
│                                                     │
│   GMKtec Zen4 Mini PC                               │
│   AMD Ryzen 5 7640HS                                │
└─────────────────────────────────────────────────────┘
```

## TRIM / Discard Flow — End to End

When Windows runs `Optimize-Volume -ReTrim` (or Linux runs `fstrim`):

```
  Guest OS                    "These blocks are free"
     │
     │ SCSI UNMAP / VirtIO discard
     ▼
  QEMU (discard=unmap)        Translates to block discard
     │
     │ blkdev_issue_discard()
     ▼
  DRBD                        1. Passes discard to local backing device
     │                        2. Replicates discard to peer node
     │                           (peer also releases thin blocks)
     │ blkdev_issue_discard()
     ▼
  LVM Thin LV                 Releases thin pool extents
     │
     │ Thin pool block free
     ▼
  Thin Pool                   Blocks returned to shared pool
     │                        Available for other VMs
     ▼
  NVMe SSD                    SSD TRIM — flash cells freed
                              for wear leveling
```

**Key property:** Both nodes reclaim space. When the primary node
processes a TRIM, DRBD replicates the discard to the secondary node,
which also frees the corresponding thin pool blocks. Both nodes stay
in sync on actual disk usage, not just data.

## Per-VM Disk Summary

```
  VM           DRBD      Minor  Port  Thin LV           Size
  ─────────────────────────────────────────────────────────────
  vm-test      drbd1     1      7789  vm-test-disk0     10GB
  vm-win       drbd2     2      7790  vm-win-disk0      40GB
```

Each VM disk is an independent DRBD resource with its own:
- Thin LV on each node
- DRBD minor number and TCP port
- Replication state (Primary/Secondary)
- Failure domain (one disk failing doesn't affect others)
