# Bedrock DRBD Replication

## Network Topology

```
                          MikroTik CRS310
                        192.168.2.253 (mgmt)
                     ┌──────────┴──────────┐
                     │   8x 2.5Gbit ports   │
                     │   br0 / VLAN 1       │
                     └───┬──────────────┬───┘
                         │              │
                    enp3s0 (br0)   enp3s0 (br0)
                   192.168.2.141  192.168.2.142
                 ┌───────┴───┐  ┌───┴───────┐
                 │   NODE 1  │  │   NODE 2   │
                 │           │  │            │
                 │  eno1 ────┼──┼──── eno1   │  ◄── Direct 2.5Gbit cable
                 │ 10.99.0.1 │  │ 10.99.0.2  │      (no switch in path)
                 └───────────┘  └────────────┘

     Management / VM traffic: via MikroTik switch (enp3s0 → br0)
     DRBD replication:        DUAL PATH (see below)
     SSH between nodes:       works on BOTH paths
```

## DRBD Dual-Path Replication

DRBD is configured with **two independent replication paths** per resource.
If one path fails, replication continues over the other. No VM freeze.

```
  Resource        Path 1 (direct cable)    Path 2 (via switch)
  ──────────────────────────────────────────────────────────────
  vm-test-disk0   10.99.0.1 ↔ 10.99.0.2   192.168.2.141 ↔ .142
                  port 7789                port 7789
  vm-win-disk0    10.99.0.1 ↔ 10.99.0.2   192.168.2.141 ↔ .142
                  port 7790                port 7790

  ┌──────────┐         Path 1: direct cable          ┌──────────┐
  │  NODE 1  │═══════ 10.99.0.1 ←──────→ 10.99.0.2 ═══│  NODE 2  │
  │          │                                        │          │
  │          │         Path 2: via MikroTik switch    │          │
  │          │─────── 192.168.2.141 ←──→ .142 ────────│          │
  └──────────┘              │                │        └──────────┘
                       ┌────┴────────────────┴────┐
                       │    MikroTik CRS310       │
                       └──────────────────────────┘

  DRBD automatically fails over between paths.
  Normal operation: uses direct cable (faster, dedicated).
  Cable failure: switches to switch path within seconds.
  Both paths active: DRBD picks the best available path.
```

### Why dual-path matters

```
  Single-path (old):    Direct cable fails → DRBD can't replicate
                        → Protocol C stalls writes
                        → VMs FREEZE until timeout

  Dual-path (current):  Direct cable fails → DRBD switches to path 2
                        → Replication continues via switch
                        → VMs never notice
```

## Replication Protocol

```
  DRBD Protocol C — Synchronous Replication

  VM Write on Primary Node:
  ─────────────────────────

  Guest VM
     │  write
     ▼
  QEMU ──► DRBD Primary
               │
               ├──► Write to local thin LV
               │
               ├──► Send over 10.99.0.x ──────► DRBD Secondary
               │         (direct cable)              │
               │                                     ├──► Write to local thin LV
               │                                     │
               │    ◄── ACK ─────────────────────────┘
               │
               └──► ACK to QEMU ──► ACK to Guest VM

  Write is only acknowledged to the VM AFTER both nodes
  have written to disk. Zero data loss on failover.
```

## DRBD States

```
  Normal operation (1 VM per node):
  ┌──────────────────────┐     ┌──────────────────────┐
  │       NODE 1         │     │       NODE 2         │
  │                      │     │                      │
  │  vm-test-disk0:      │     │  vm-test-disk0:      │
  │    Role: Primary     │◄───►│    Role: Secondary   │
  │    Disk: UpToDate    │     │    Disk: UpToDate    │
  │    VM:   RUNNING     │     │    VM:   shut off    │
  │                      │     │                      │
  │  vm-win-disk0:       │     │  vm-win-disk0:       │
  │    Role: Secondary   │◄───►│    Role: Primary     │
  │    Disk: UpToDate    │     │    Disk: UpToDate    │
  │    VM:   shut off    │     │    VM:   RUNNING     │
  └──────────────────────┘     └──────────────────────┘

  After failover (node2 died):
  ┌──────────────────────┐     ┌──────────────────────┐
  │       NODE 1         │     │       NODE 2         │
  │                      │     │                      │
  │  vm-test-disk0:      │     │                      │
  │    Role: Primary     │     │     ╔═══════════╗    │
  │    Disk: UpToDate    │     │     ║  OFFLINE   ║    │
  │    VM:   RUNNING     │     │     ╚═══════════╝    │
  │                      │     │                      │
  │  vm-win-disk0:       │     │                      │
  │    Role: Primary ◄───┼─ promoted by orchestrator  │
  │    Disk: UpToDate    │     │                      │
  │    VM:   RUNNING ◄───┼─ started by orchestrator   │
  └──────────────────────┘     └──────────────────────┘

  After node2 returns:
  ┌──────────────────────┐     ┌──────────────────────┐
  │       NODE 1         │     │       NODE 2         │
  │                      │     │                      │
  │  vm-test-disk0:      │     │  vm-test-disk0:      │
  │    Role: Primary     │────►│    Role: Secondary   │
  │    Disk: UpToDate    │sync │    Disk: Resyncing   │
  │                      │     │                      │
  │  vm-win-disk0:       │     │  vm-win-disk0:       │
  │    Role: Primary     │────►│    Role: Secondary   │
  │    Disk: UpToDate    │sync │    Disk: Resyncing   │
  └──────────────────────┘     └──────────────────────┘
  VMs stay on node1 until admin migrates them back.
```

## Live Migration — DRBD Dual-Primary Sequence

```
  Before:   Primary ◄──────────────► Secondary
               VM                      (standby)

  Step 1:   enable dual-primary on both nodes
  Step 2:   promote Secondary → Primary

            Primary ◄──────────────► Primary
               VM                    (ready)

  Step 3:   virsh migrate --live (RAM only, no disk copy!)

            Primary                  Primary
            (source)────RAM─────────►(destination)
                                        VM

  Step 4:   demote source → Secondary
  Step 5:   disable dual-primary

            Secondary ◄────────────► Primary
            (standby)                   VM

  Migration time: ~3.5s per GB of RAM
  Storage transferred: ZERO (both nodes already have every byte)
```

## Configuration Files

```
  /etc/drbd.d/vm-test-disk0.res    DRBD resource for Linux VM
  /etc/drbd.d/vm-win-disk0.res     DRBD resource for Windows VM
  /etc/drbd.d/global_common.conf   Global DRBD settings (default)

  Each resource config defines:
  - device minor number (1, 2, ...)
  - backing disk (thin LV path)
  - replication address (10.99.0.x:port)
  - split-brain recovery policy
```
