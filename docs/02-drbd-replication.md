# Bedrock DRBD Replication

> **As of the mesh-network rewrite**: DRBD's `path` blocks are
> generated from the bedrock-net path table — one `path` per direct
> NIC pair the cluster observed, ordered fastest-first, plus a
> loopback fallback as the last resort. The old "dual-path = direct
> cable + switch" model is now N-path where N matches the actual
> cabling. See `docs/06-mesh-network.md` for the layer that owns
> path discovery and route emission.

## Network topology (general case)

```
                  cluster identity layer
                  100.<X>.<Y>.0/24  (RFC 6598)
                   │
                   │  per-node /32 on lo
                   ▼
           ┌──────────────┐         ┌──────────────┐
           │   node 1     │         │   node 2     │
           │ 100.<X>.<Y>.1│         │ 100.<X>.<Y>.2│
           ├──────────────┤         ├──────────────┤
           │ br0 (LAN)    │ ─────── │ br0 (LAN)    │
           │              │ switch  │              │
           │ enp2s0 (mesh)│ ─────── │ enp2s0       │
           │ enp3s0 (mesh)│ ─────── │ enp3s0       │  direct cable
           │ usb4 (mesh)  │ ─────── │ usb4         │  direct cable
           └──────────────┘         └──────────────┘

  Mgmt traffic + VM bridge: br0 (operator LAN)
  Cluster identity:         /32 on lo, 100.X.Y.<node>
  DRBD replication:         every direct NIC pair, plus loopback fallback
  Routes:                   bedrock-net installs metric-ordered host routes;
                            DRBD multi-paths over per-NIC link-local
```

## DRBD multi-path replication

DRBD is configured with **one `path` block per directly-connected NIC
pair** the bedrock-net mesh layer has observed. Failover between
paths is automatic — DRBD detects path-level TCP failure
independent of kernel routing.

```
  Resource    Path 1 (LAN)       Path 2 (cable A)     Path 3 (cable B)   Path 4 (loopback fallback)
  ─────────────────────────────────────────────────────────────────────────────────────────────────
  tier-bulk   192.168.2.1 ↔ .2   169.254.A.B ↔ .C.D   169.254.E.F ↔ .G.H 100.X.Y.1 ↔ 100.X.Y.2

  ↑ Real per-NIC addresses populated automatically from the bedrock-net
    path table; each path block carries the actual addresses for that
    specific NIC pair. DRBD picks fastest-first by metric, falls over
    to the next on path-level TCP failure.

  ┌──────────┐         Path 2: direct cable          ┌──────────┐
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
