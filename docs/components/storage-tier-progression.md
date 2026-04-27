# Storage Tier Progression — node-count-dependent backends

The storage-tiers doc names three fileshare classes (Scratch / Bulk /
Critical) and three VM disk classes (Cattle / Pet / Pet+). Which **backend
implementation** delivers each class is a function of cluster size — and
the answer is non-uniform across sizes because each backend has its own
minimum-cluster requirements.

This doc reasons through the choices at each cluster size from 1 to 4+
nodes and flags the open empirical question that gates the 3-node design.

## Backend properties

| backend | replication / EC | tolerates | min. nodes | efficiency | notes |
|---|---|---|---|---|---|
| Local LVM thin | none | 0 | 1 | 100 % | host failure = data loss |
| DRBD-2way | 2-way mirror | 1 node | 2 | 50 % | sync over network, mature |
| DRBD-3way | 3-way mirror | 2 nodes | 3 | 33 % | mature |
| Garage rep=1 | sharding only | 0 | 1 | 100 % | RAID-0-like, parallel-NIC fanout |
| Garage rep=2 | 2-way mirror | 1 node | 2 | 50 % | distributed RAID-1-like |
| Garage rep=3 | 3-way mirror | 2 nodes | 3 | 33 % | distributed RAID-1-like (3 copies) |
| RustFS EC:1 set=N | N−1 data + 1 parity | 1 drive | N | (N−1)/N | RAID-5-like; needs **min set size** |
| RustFS EC:2 set=N | N−2 data + 2 parity | 2 drives | N | (N−2)/N | RAID-6-like |

For RustFS with 1 drive per node, "drives down" = "nodes down."

## Open empirical question — RustFS minimum set size

**MinIO** (the upstream RustFS borrows its layout from) accepts erasure
set sizes in `[4, 5, 6, 7, 8, 9, 10, 12, 14, 15, 16]`. **3 is not on the
list.** RustFS at alpha.99 may inherit this — earlier we saw the error
`Acceptable values for 5 number drives are [5]` for a 5-drive volume
list, suggesting the minimum is hardcoded similarly.

**The question:** does RustFS reject `RUSTFS_ERASURE_SET_DRIVE_COUNT=3`?

If **yes (3 rejected)**, the only paths to put RustFS on a 3-node cluster are:
- 2 thin LVs per node (6 drives total, set=6, EC:2 = 4d+2p, 67 % eff, 1-node loss = 2 drives = within parity)
- 3 thin LVs per node (9 drives, set=3 still rejected; only set=9 EC:N options)
- ...skip RustFS at 3 nodes; use Garage rep=2 or DRBD-3way for Bulk/Critical

If **no (3 accepted)**, the simpler config is:
- 1 thin LV per node (set=3, EC:1 = 2d+1p, 67 % eff, 1-drive loss = 1-node loss tolerated)
- This matches the natural cluster size cleanly

**This is the gating empirical test for the 3-node architecture.** Until
verified, treat 3-node RustFS as "needs validation." Below, the table
shows both paths.

## Per-cluster-size tier mapping

### 1 node — single-host baseline

| tier | backend | rationale |
|---|---|---|
| Cattle | local LVM thin | only choice |
| Scratch | Garage rep=1 (or skip) | local S3 endpoint, no redundancy. Useful for local mc/aws-cli workflows + ISO storage |
| Pet, Pet+, Bulk, Critical | **not available** | redundancy needs more nodes |

A 1-node deployment is a development/POC mode. The dashboard advertises
only the Cattle and Scratch tiers.

### 2 nodes — minimum HA

| tier | backend | tolerates | efficiency |
|---|---|---|---|
| Cattle | local LVM thin | 0 | 100 % |
| Pet | DRBD-2way | 1 node | 50 % |
| Scratch | Garage rep=1 | 0 | 100 % |
| Bulk | Garage rep=2 | 1 node | 50 % |
| Pet+, Critical | unavailable | — | — |

2-node RustFS is impossible (set size ≥ 4 likely; even if set=2 worked,
EC:1 with 2 drives = 1d+1p, "any drive loss = full cluster" is just RAID-1
which Garage rep=2 already gives us with simpler operations).

### 3 nodes — first real HA cluster

The interesting size. **Two architectural paths depending on the RustFS
empirical answer.**

#### Path A — RustFS accepts `set_size=3`

| tier | backend | tolerates | efficiency |
|---|---|---|---|
| Cattle | local LVM thin | 0 | 100 % |
| Pet | DRBD-2way | 1 node | 50 % |
| Pet+ | DRBD-3way | 2 nodes | 33 % |
| Scratch | Garage rep=1 | 0 | 100 % |
| Bulk | RustFS EC:1 set=3 | 1 node | 67 % |
| Critical | RustFS EC:2 set=3 (1d+2p) | 2 nodes | 33 % |

#### Path B — RustFS requires `set_size ≥ 4`

Two sub-options:

**B.1: 2 thin LVs per node, set=6**

| tier | backend | tolerates | efficiency |
|---|---|---|---|
| Bulk | RustFS EC:2 set=6 (4d+2p across 6 drives, 2 per node) | 1 node = 2 drives = within parity | 67 % |
| Critical | RustFS EC:3 set=6 (3d+3p) | 2 nodes = 4 drives → ✗ exceeds parity. Doesn't fit |
| Critical | RustFS EC:4 set=6 (2d+4p) | 2 nodes | 33 % |

This works for Bulk but Critical at 3-nodes-2-LVs is awkward: EC:4 is
*possible* but "4 parity to survive 4 drives" is a heavy ratio for a
known 3-node topology. EC:3 doesn't survive 2-node loss because each
node hosts 2 drives, so a 2-node failure = 4 drives = exceeds 3 parity.

**B.2: skip RustFS at 3 nodes; use Garage + DRBD**

| tier | backend | tolerates | efficiency |
|---|---|---|---|
| Bulk | Garage rep=2 | 1 node | 50 % |
| Critical | Garage rep=3 (or DRBD-3way exposed via NBD/s3backer) | 2 nodes | 33 % |

This sidesteps the RustFS minimum-set-size issue entirely. Trade-off: 67 %
→ 50 % efficiency for Bulk, but a much simpler operational story (no
need to manage 2 thin LVs per node, no need to choose between EC:3/EC:4).

**Recommendation pre-experiment:** plan for Path A (cleanest), with B.2
as the documented fallback if RustFS rejects set=3. B.1 is the
operationally awkward middle ground; only pursue if RustFS specifically
forbids set=3 *and* the user demands maximum efficiency at 3 nodes.

### 4 nodes — validated baseline

| tier | backend | tolerates | efficiency |
|---|---|---|---|
| Cattle | local LVM thin | 0 | 100 % |
| Pet | DRBD-2way | 1 node | 50 % |
| Pet+ | DRBD-3way | 2 nodes | 33 % |
| Scratch | Garage rep=1 | 0 | 100 % |
| Bulk | RustFS EC:1 set=4 (3d+1p) | 1 node | 75 % |
| Critical | RustFS EC:2 set=4 (2d+2p) | 2 nodes | 50 % |

This is the configuration validated in the 2026-04-27 trial. RustFS at
set=4 is well-trodden upstream territory.

### 5+ nodes — scale via wider sets

RustFS supports set sizes up to 16. At 5 nodes:

| tier | backend | tolerates | efficiency |
|---|---|---|---|
| Bulk | RustFS EC:1 set=5 | 1 | 80 % |
| Critical | RustFS EC:2 set=5 | 2 | 60 % |

At 8+ nodes, EC:3 / EC:4 become viable Critical-class options if the
operator wants more headroom. VM disk tiers stay capped at Pet+ (DRBD
3-way) until our code grows 4-way + paths.

## Why Garage instead of more RustFS

- **Scratch** must allow EC:0 / replication=1 for "RAID-0 across nodes,
  any node loss = data loss accepted." RustFS rejects EC:0 in distributed
  mode by design. Garage with `replication_factor=1` is the natural fit.
- **Bulk at small node counts** (1–3 nodes, depending on the empirical
  RustFS answer): Garage rep=2 is a clean fallback.
- For LLM/ISO/template workloads (large files, parallel-NIC throughput),
  Garage's 10 MiB-block fanout matches what we want regardless.

## Why DRBD for VM boot disks (always)

The VM disk tiers (Cattle, Pet, Pet+) always live on DRBD or local LV,
never on s3backer/RustFS/Garage. Reasons:

- **Sync write latency.** S3 PUT round-trips are 10–20 ms on a fast LAN.
  OS workloads have many small synchronous metadata writes; the FUSE-over-
  HTTP path is the wrong shape.
- **Live migration semantics.** DRBD's allow-two-primaries window and
  Protocol C give us atomic primary handoff. s3backer can support this
  via `--sharedDiskMode`, but the benefit is mainly that it's *the same
  block-device model on both sides*, not that it's faster.
- **Boot-time disk discovery.** qemu opening a FUSE file as the boot
  disk has more failure modes (FUSE not mounted yet, RustFS quorum lost,
  etc.) than opening a DRBD device.

Secondary VM data disks ARE a good fit for s3backer + RustFS Bulk —
that's the validated hero scenario. Big, mostly-cold data (rman backups,
media, archives) gets cheap distributed storage; the OS itself stays on
DRBD.

## Recommended cluster-size gating in mgmt

Updated, RustFS-empirical-pending:

```python
def available_tiers(node_count, rustfs_3node_works=False):
    vm_disk = ["cattle"]
    fileshare = ["scratch"]   # always: Garage rep=1 (or skip if 1 node)

    if node_count >= 2:
        vm_disk.append("pet")           # DRBD-2way
        fileshare.append("bulk")        # Garage rep=2
    if node_count >= 3:
        vm_disk.append("pet+")          # DRBD-3way
        if rustfs_3node_works:
            # upgrade Bulk to RustFS EC:1 (67 % efficiency)
            pass
        # Critical at 3 nodes:
        #   path A:  RustFS EC:2 set=3 (33 %)
        #   path B:  Garage rep=3 (33 %)
        #   path C:  DRBD-3way exposed via s3backer (33 %)
        fileshare.append("critical")
    if node_count >= 4:
        # Bulk on RustFS EC:1 set=4 (75 %)
        # Critical on RustFS EC:2 set=4 (50 %)
        pass
    return {"vm_disk": vm_disk, "fileshare": fileshare}
```

## Decision after the 3-node empirical test

**Tested 2026-04-27** — see [rustfs-3node-trial-2026-04-27.md](../scenarios/rustfs-3node-trial-2026-04-27.md).

RustFS at alpha.99 **does** accept `RUSTFS_ERASURE_SET_DRIVE_COUNT=3`
(contrary to my pre-test hypothesis based on MinIO's set-size rule).
EC:1 with 2d+1p actually distributes correctly across 3 nodes at 67 %
efficiency, and steady-state read/write works perfectly with all 3
nodes alive.

**However**, single-node failure exposes a separate dsync lock-quorum
bottleneck: each object has a fixed set of nodes that hold its
distributed-lock metadata, and when one of those happens to be the down
node, the read fails with `ns_loc: Quorum not reached: required 2,
achieved 1` until that node returns. In testing, **40–90 %** of
pre-existing objects remained readable under 1-node-loss — *most* but
not *all*. New writes after the failure work fine because they hash to
the still-alive nodes.

That's incompatible with the Bulk-tier promise of "tolerates 1 node
failure" for **all** existing objects.

**Conclusion: at 3 nodes, do NOT use RustFS for tiers that need
1-node-loss durability.** Adopt Path B.2:

- **Bulk** → Garage `replication_factor=2` (50 % efficiency, full 1-node-loss tolerance, dsync-free)
- **Critical** → Garage `replication_factor=3` (33 % efficiency, 2-node-loss tolerance) or DRBD-3way exposed via s3backer/NBD shim
- **Scratch** → Garage `replication_factor=1`

RustFS enters the picture only at 4+ nodes, where set_size=4 means
1-down still leaves 3 peers reachable for dsync quorum (validated in
the prior 4-node trial).

The 17 percentage-point efficiency gap (Garage rep=2's 50 % vs. RustFS
EC:1 set=3's 67 %) is a real cost. If a future RustFS / dsync release
fixes the quorum behavior for N=3, revisit. Patching dsync ourselves
is possible but a per-fork maintenance burden we should not take on
prematurely.
