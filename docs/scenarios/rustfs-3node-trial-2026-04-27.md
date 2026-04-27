# RustFS 3-node EC:1 trial — 2026-04-27

Empirical investigation of whether RustFS at alpha.99 can serve as the
Bulk-tier backend on a 3-node cluster with `RUSTFS_ERASURE_SET_DRIVE_COUNT=3`
and `STANDARD=EC:1` (2 data + 1 parity per stripe). The unknown coming
in: does RustFS accept set_size=3 (MinIO upstream rejects anything
below 4) and if so, can the cluster tolerate the loss of any 1 of the 3
nodes?

## TL;DR

| dimension | result |
|---|---|
| Pool init with `RUSTFS_ERASURE_SET_DRIVE_COUNT=3` | ✅ **accepted** — RustFS does NOT inherit MinIO's set_size ≥ 4 hard rule |
| Steady-state read/write (all 3 up) | ✅ 100 % — 100 MB PUT/GET round-trip, integrity bit-perfect |
| EC distribution | ✅ exactly 2d+1p — 50 MiB stored per node for a 100 MiB object |
| Storage efficiency | 67 % (2 of 3 drives = data) |
| Write quorum with 1 node down | ✅ writes succeed via remaining nodes |
| Read quorum on **fresh** writes (during 1 down) | ✅ 5/5 ok |
| Read quorum on **pre-existing** objects (during 1 down) | ❌ **2/5 ok (40 %)** — unpredictable failures, depends on which node is down vs. lock-target placement |
| Recovery | ✅ all failed reads succeed once the down node returns; no data lost |

**Verdict: not viable for "Bulk tier tolerates 1 node loss" guarantee.**
Single-node-loss leaves a fraction of pre-existing objects unreadable
because RustFS's distributed lock manager (dsync) needs 2-of-3 peers
reachable, and **specific** objects have lock targets that include the
down node — those objects fail until the node returns.

## Setup

- 3 fresh AlmaLinux 9 sims (192.168.2.183/184/185, drbd 10.99.0.10/11/12)
- Stock `bedrock bootstrap` + manual 76 GiB loop-backed thin pool per
  node
- `installer/lib/storage_install.py setup --ec-set-size 3 --ec-standard 1`
- Per-node 40 GiB thin LV → ext4 → `/var/lib/rustfs/data`
- RustFS env:
  ```
  RUSTFS_VOLUMES=http://10.99.0.10:9000/data http://10.99.0.11:9000/data http://10.99.0.12:9000/data
  RUSTFS_STORAGE_CLASS_STANDARD=EC:1
  RUSTFS_ERASURE_SET_DRIVE_COUNT=3
  ```
- Bucket `bulk` on STANDARD storage class (= EC:1 here)

## Result 1 — Pool init succeeds with set_size=3

Earlier hypothesis: RustFS would reject `set_size=3` because the MinIO
fork's hardcoded list is `[4, 5, 6, 7, 8, 9, 10, 12, 14, 15, 16]`. The
4-node trial saw `Acceptable values for 5 number drives are [5]` which
suggested the same upstream rule.

**This is wrong for RustFS alpha.99.** All 3 nodes started cleanly:

```
$ systemctl is-active rustfs.service
active     # sim-1
active     # sim-2
active     # sim-3
```

The RustFS S3 API responds, listing buckets, accepting `s3api create-bucket`,
and returning the expected `RUSTFS_STORAGE_CLASS_STANDARD=EC:1`.

## Result 2 — EC distribution matches the math

100 MiB PUT to bulk → per-node disk usage:

| node | data dir size |
|------|---------------|
| sim-1 | 51 MB |
| sim-2 | 51 MB |
| sim-3 | 51 MB |

Total 153 MiB stored = 100 MiB user data × 1.5 (2d+1p ratio). Exactly
what `2d + 1p` predicts. EC:1 is genuinely active, not silently
degraded.

GET round-trip: SHA-256 matches the original. No corruption.

## Result 3 — 1-node-loss reveals a dsync lock-quorum failure mode

The hero test: write 30 fresh 10 MiB objects, then stop one node,
attempt to read each one through each surviving endpoint.

| down node | endpoint | reads ok / 30 |
|---|---|---|
| sim-1 (183) | sim-2 (184) | 30/30 |
| sim-1 (183) | sim-3 (185) | 29/30 |
| sim-2 (184) | sim-1 (183) | 28/30 |
| sim-2 (184) | sim-3 (185) | 27/30 |
| sim-3 (185) | sim-1 (183) | 29/30 |
| sim-3 (185) | sim-2 (184) | 27/30 |

90–100 % succeed. Failure mode for the misses:

```
An error occurred (InternalError) when calling the GetObject operation
(reached max retries: 4): Io error: Failed to acquire read lock:
ns_loc: read lock acquisition failed on bulk/big.bin:
Quorum not reached: required 2, achieved 1
```

A second, deliberately stress-shaped run after rapid stop/start cycles
gave a worse picture:

| step | result |
|---|---|
| Pre-existing objects (`fresh1.bin`..`fresh5.bin`), sim-1 down, read via sim-2 | **2/5 ok (40 %)** |
| Newly-written objects with sim-1 down (`d1w1.bin`..`d1w5.bin`) | 5/5 ok writes, 5/5 ok reads |
| Same pre-existing 5 objects after sim-1 returns | 5/5 ok |

The pattern is consistent with **per-object lock-target hashing**: each
object has a fixed set of nodes that hold its dsync metadata. When the
down node happens to be in that set, the lock can't reach quorum
(`required 2, achieved 1`) and the read fails — until the node returns.
Newly-written objects have lock targets chosen from the currently-alive
peers, so they're fine.

## Why this is a deal-breaker for 3-node Bulk on RustFS

The Bulk tier definition is "tolerates 1 node failure." That has to mean
**every existing object is still readable** when any 1 of the 3 nodes
goes down — not "most of them." A stochastic 40-90 % availability under
single-node failure isn't a guarantee an operator can plan around.

The math is clean (EC:1 reconstruct from 2 of 3 drives works fine), but
the **dsync lock quorum** is a parallel system that demands majority of
reachable peers, and at N=3 the majority requirement is "2 peers
besides the requesting node = 2 of the other 2 = both." That's fragile
by design.

This matches MinIO's longstanding minimum-cluster-size of 4 even in
their own documentation, and the underlying dsync paper's quorum
analysis. The reason 4 is the practical minimum is exactly so that
1-node-loss leaves 3 alive, 2 of whom can form the lock quorum.

## What works at 3 nodes (alternatives)

| backend | tolerates 1 node down | efficiency | notes |
|---|---|---|---|
| **Garage `replication_factor=2`** | ✅ all objects still readable | 50 % | block-level 2-way replication, dsync-free; right answer for 3-node Bulk |
| **DRBD-3way exposed via NBD/s3backer** | ✅ | 33 % | mature path, but limited to single-volume semantics |
| **RustFS EC:1 set=3** | ❌ ~10–60 % of existing reads fail until recovery | 67 % | tested above |
| **RustFS EC:2 set=3 (1d+2p)** | unknown, expected same dsync issue | 33 % | same lock manager; not separately tested |

## Architectural decision (3-node Bedrock)

- **Bulk tier**: Garage `replication_factor=2`. Loses 17 percentage
  points of efficiency vs. RustFS EC:1, but gains true 1-node-loss
  tolerance with no per-object failure modes.
- **Critical tier**: Garage `replication_factor=3` (50 % efficiency)
  *or* DRBD-3way (33 % via s3backer/NBD shim). RustFS not used at 3 nodes.
- **Scratch tier**: Garage `replication_factor=1` (unchanged from 4-node).
- **VM disk tiers** (Cattle/Pet/Pet+): unchanged — DRBD as before.

RustFS only enters the picture at 4 nodes, which is also where MinIO's
distributed model is properly stable (set_size=4 EC:1 = 1-of-4 down =
3 peers reachable = quorum). That's the original "Critical-EC at 4
nodes" boundary in `storage-tiers.md`. Bulk at 4 nodes also goes to
RustFS EC:1 set=4 for the efficiency win (75 %) — validated in the
prior 2026-04-27 trial.

## Update to storage-tier-progression.md

The progression doc had Path A (RustFS at 3 nodes if set=3 worked) as
the recommended option. With this empirical evidence, **Path B.2 (skip
RustFS at 3, use Garage rep=2/3) is the correct recommendation.**

A small RustFS source patch could in principle relax the dsync lock
quorum to allow N=3 operation, but that's a per-fork maintenance burden
we shouldn't take on lightly. The Garage path is right.

## Open follow-ups

- Is there a RustFS env var / config to tune the dsync quorum threshold?
  Worth checking the RustFS source / issue tracker before assuming "no
  way to make N=3 work."
- Can dsync lock requests be retried with backoff so that **transient**
  reconverge failures (which were 90–97 % readable) recover within
  ~30 s? If so, the alpha.99 stochastic failures might shrink as the
  project matures.
- Test EC:2 set=3 explicitly to confirm lock quorum is the bottleneck
  (not parity). Expectation: same failure mode.
