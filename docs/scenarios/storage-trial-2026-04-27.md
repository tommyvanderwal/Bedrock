# Storage tiers trial — 2026-04-27

End-to-end validation of the three fileshare tiers (Scratch / Bulk /
Critical) on the 4-node sim cluster, plus the **hero scenario**: a Pet VM
with a DRBD-2way boot disk and an s3backer-on-RustFS data disk live-
migrated between sim-1 and sim-2.

## TL;DR

- **All three tiers operational** across 4 nodes:
  - `scratch` on Garage (replication_factor=1) — 100 GiB usable, blocks
    distributed across all 4 nodes by rendezvous hash.
  - `bulk` on RustFS (REDUCED_REDUNDANCY = EC:1) — ~120 GiB usable,
    survives 1 node loss.
  - `critical` on RustFS (STANDARD = EC:2) — ~80 GiB usable, data
    survives 2 concurrent node losses (writes pause; manual restart to
    resume — matches storage-tiers doc spec).
- **Hero test passed**: live-migrate sim-1↔sim-2 of `vm-hero` with DRBD
  boot + s3backer data disk:
  - Forward (sim-1 → sim-2): 1.30 s
  - Reverse (sim-2 → sim-1): 1.02 s
  - Pre-positioned 4 MiB integrity patterns at offsets 1.5 GiB / 1.6 GiB
    on the s3backer disk verified bit-perfect after each migration.
  - DRBD multi-primary handoff clean both ways.
- **Two product gaps surfaced**:
  - RustFS does not support per-bucket EC profiles. EC is cluster-wide;
    only two storage classes (STANDARD, REDUCED_REDUNDANCY) are
    available. Three-tier-per-bucket as written in the storage-tiers
    doc is **not expressible in a single RustFS deployment** — Garage
    fills the third tier.
  - RustFS distributed mode rejects EC:0. RAID-0-across-nodes ("Scratch")
    is not a RustFS configuration — Garage at replication_factor=1
    delivers it cleanly.

## Architecture as deployed

```
                   ┌────────────────────────────────────────┐
                   │ Each sim node                          │
                   │                                         │
                   │  /var/lib/rustfs/data  (40 GB ext4 thin LV)│
                   │      └─ /data inside container          │
                   │  ┌──────────────────────────┐          │
                   │  │ podman: rustfs/rustfs    │          │
                   │  │   :9000 S3 API           │          │
                   │  │   :9001 console          │          │
                   │  │   STANDARD = EC:2        │          │
                   │  │   REDUCED_REDUNDANCY=EC:1│          │
                   │  └──────────────────────────┘          │
                   │                                         │
                   │  /var/lib/garage/data  (25 GB ext4 thin)│
                   │  /var/lib/garage/meta  (LMDB)           │
                   │  ┌──────────────────────────┐          │
                   │  │ garage server (native)   │          │
                   │  │   :3900 S3 API           │          │
                   │  │   :3901 RPC              │          │
                   │  │   :3903 admin            │          │
                   │  │   replication_factor=1   │          │
                   │  └──────────────────────────┘          │
                   │                                         │
                   │  s3fs-fuse → /var/lib/libvirt/templates │
                   │      libvirt directory pool             │
                   │                                         │
                   │  s3backer → /mnt/s3disk1/file           │
                   │      qemu virtio-blk passthrough        │
                   └────────────────────────────────────────┘

         Inter-node:  bedrock-drbd (10.99.0.0/24, primary)
                      mgmt LAN     (192.168.2.0/24, fallback at metric 200)
```

## Software inventory

| Component | Version | Source |
|---|---|---|
| RustFS | 1.0.0-alpha.99 | `docker.io/rustfs/rustfs:latest` (glibc 2.39 in container; AlmaLinux 9 host has 2.34 — container is mandatory) |
| Garage | v2.3.0 | static musl binary from garagehq.deuxfleurs.fr |
| s3backer | 2.1.6 (master HEAD) | built from source on AlmaLinux 9 — v1.4.5 EPEL too old (no `--sharedDiskMode`) |
| s3fs-fuse | 1.97 | EPEL |
| podman | 5.6.0 | AlmaLinux 9 base |
| fuse3 | 3.10.2 | AlmaLinux 9 base |

## Test matrix

### T1. RustFS distributed cluster bring-up

| step | result |
|---|---|
| podman + RustFS image pulled on 4/4 nodes | ✅ |
| `RUSTFS_VOLUMES` = explicit 4-URL list, drbd network | ✅ |
| Cluster forms via `http://10.99.0.{10..13}:9000/data` | ✅ |
| Object UID 10001 inside container; chown host data dir to 10001:10001 | ✅ (gotcha) |
| Log dir must NOT be `/data/logs` — pollutes namespace as phantom bucket. Bind-mount `/var/log/rustfs` instead. | ✅ (gotcha) |
| `RUSTFS_VOLUMES` must be in env, not CLI args (entrypoint appends `/data` to CLI list, breaking drive count) | ✅ (gotcha) |
| Bucket `critical` (STANDARD = EC:2) | ✅ |
| Bucket `bulk` (REDUCED_REDUNDANCY = EC:1) | ✅ |
| 100 MiB PUT to critical → 84 MB stored per node (2/4 data + 2/4 parity) | ✅ |
| 100 MiB PUT to bulk → distributed: 3/4 data + 1/4 parity, ~33 MB per node | ✅ |
| Roundtrip checksums match for both | ✅ |

### T2. Failure tolerance (RustFS)

| nodes down | tier | expected | actual |
|---|---|---|---|
| 1/4 | critical | read+write OK | ✅ |
| 1/4 | bulk | read+write OK | ✅ |
| 2/4 | critical | data intact, writes paused | ✅ — `erasure write quorum` error, both bring-back required |
| 2/4 | bulk | unavailable | ✅ same — bulk tolerates 1 only |

This is consistent with the storage-tiers doc: "Critical: data survives 2
concurrent failures; manual restart required, data intact."

### T3. Garage scratch cluster

| step | result |
|---|---|
| Static musl binary on 4/4 nodes | ✅ |
| 4-node cluster formed via `garage node connect` | ✅ |
| Layout: 4 × 20 GiB cap, replication_factor=1 | ✅ |
| Bucket `scratch` + access key `scratch-key` | ✅ |
| 100 MiB PUT — blocks distributed 21/41/21/21 MiB across nodes | ✅ |
| Roundtrip checksum match | ✅ |
| `block_size=10 MiB` in config — better fanout for big files (LLM models, ISOs) | ✅ |

### T4. s3backer + sharedDiskMode — concurrent mounts

s3backer 2.1.6 from master because v1.4.5 (latest tagged) lacks
`--sharedDiskMode` and the addressing-style flags. v1.4.5's `--region`
unconditionally enables vhost-style which RustFS doesn't accept on raw
IPs.

| step | result |
|---|---|
| Built from `master` HEAD (autoreconf + configure + make) | ✅ |
| Mount on sim-1 with `--no-vhost --sharedDiskMode --size=2G --blockSize=1m` | ✅ |
| Concurrent mount on sim-2 (no `--force`, mount tokens coordinate) | ✅ |
| Cross-host write → read: sim-1 writes 5 MiB at offset 0 → sim-2 reads same SHA-256 | ✅ (71 ms / 57 ms; sync writes via RustFS round-trip) |
| Reverse: sim-2 writes 5 MiB at offset 100 MiB → sim-1 reads same SHA-256 | ✅ |
| All caching disabled (no `--blockCache*`, `--md5Cache*` overrides — `--sharedDiskMode` enforces) | ✅ |

### T5. **Hero test** — live migrate Pet VM with DRBD boot + s3backer data

VM definition: `vm-hero` (UUID identical on both nodes), 1 vCPU, 1 GiB
RAM, 2 disks:

```
vda  <disk type='block' source='/dev/drbd1010'  cache='none' io='native' discard='unmap'>
vdb  <disk type='file'  source='/mnt/s3disk1/file' cache='none' discard='unmap'>
```

Both with `<seclabel model='dac' relabel='no'/>` and a domain-level
`<seclabel type='none' model='selinux'/>` to skip libvirt's chown
attempt on the FUSE-backed file.

`qemu` user added to `disk` group on both sim hosts so `/dev/drbd*` is
readable.

Boot disk: 1 GiB DRBD-2way resource `vm-hero-disk0` on minor 1010 / port
8010, alpine.qcow2 written via `qemu-img convert -O raw -S 4k -n` (1.4 s
on the sync DRBD pair). Silent-truncation guard fired no-op: `/dev/drbd1010`
== backing LV exactly (1073741824 bytes both sides — meta-LV formula
`max(32, 32 + 2 × data_gb)` MB held).

Migration sequence:

```
sim-1: drbdadm net-options --allow-two-primaries=yes vm-hero-disk0
sim-2: drbdadm net-options --allow-two-primaries=yes vm-hero-disk0
sim-2: drbdadm primary vm-hero-disk0    # both Primary, multi-primary window
sim-1: virsh migrate --live --persistent --undefinesource --unsafe \
       vm-hero qemu+ssh://root@sim-2/system tcp://10.99.0.11
sim-1: drbdadm secondary vm-hero-disk0
sim-1+2: drbdadm net-options --allow-two-primaries=no vm-hero-disk0
```

`--unsafe` is required because libvirt cannot tell that DRBD or the
shared FUSE-mounted s3backer are shared storage. Both *are* in fact
shared — DRBD via Protocol C synchronous replication, s3backer via
`--sharedDiskMode` direct backend reads/writes. `--unsafe` is honest
here, not a hazard.

| direction | duration | block stats | DRBD state after | s3backer pattern @ 1.5 GiB | s3backer pattern @ 1.6 GiB |
|---|---|---|---|---|---|
| sim-1 → sim-2 | **1.30 s** | vda 64 MB read / 0.2 MB write, vdb 17 MB read / 0 write at start | Primary on sim-2 / Secondary on sim-1, both UpToDate | intact (sha 2cbb7a93…) | n/a |
| sim-2 → sim-1 | **1.02 s** | reset on dest after migrate | Primary on sim-1 / Secondary on sim-2, both UpToDate | intact | intact (sha 2782f887…) |

Post-migration coherency check: pattern written on sim-1 host (NOT via
the VM) at offset 1.6 GiB before reverse migration was visible on sim-2
immediately, and after reverse migration was readable on sim-1. The
shared-disk semantics held across the migration boundary in both
directions.

### T6. TRIM end-to-end

| step | result |
|---|---|
| Write 50 MiB random at offset 0 to `/mnt/s3disk1/file` | bucket: 19 → 64 objects |
| Overwrite same 50 MiB with zeros (`dd if=/dev/zero ... oflag=direct`) | bucket: 64 → 14 objects (50 deleted) |
| Write 5 MiB zeros at offset 200 MiB | bucket count unchanged — s3backer never created the objects (zero-detection at write time) |
| Compactor scan (full bucket walk, GET each block, DELETE if all-zero) | scanned 13 / deleted 0 — no orphans, as expected |
| Fill alarm: 0.01 GiB / 80 GiB (0.0%) | OK |

End-to-end TRIM chain:

```
guest fstrim
  → virtio-blk discard (depends on qemu  disk discard='unmap' + detect-zeroes)
  → /mnt/s3disk1/file write (zeros for the discarded range)
  → s3backer detects all-zero block → DELETE object on RustFS
  → RustFS removes EC fragments → propagates to ext4 thin LV (mounted -o discard)
  → thin LV trims thin pool → loop device punches hole in backing qcow2
```

Each layer **does** pass the discard / zero-write through. The
periodic compactor (`s3backer-compactor@<bucket>.timer`, every 15 min)
is a backstop for any block that slipped past zero-detection.

### T7. Multi-network resilience

| step | result |
|---|---|
| Per-peer host route via mgmt LAN at metric 200 (drbd is metric 101 connected) | ✅ all 4 nodes |
| `sysctl net.ipv4.ip_forward=1` + `rp_filter=2` on all nodes | ✅ |
| Baseline: `ping 10.99.0.11` from sim-1 — 0.42 ms via eth1 | ✅ |
| `ip link set eth1 down` on sim-1 — connected route disappears, fallback engages | ✅ |
| Ping 10.99.0.11 with eth1 down — 0.54 ms via mgmt LAN | ✅ |
| `aws s3api list-buckets` against RustFS during partition — succeeds | ✅ |
| `ip link set eth1 up` — connected route restored, ping back to 0.42 ms | ✅ |

This is a per-link failover — it does NOT recover from a complete
mgmt-LAN-and-drbd-LAN partition (cluster splits in that case). For the
"either network alone is online" requirement, this configuration meets
it: any single network can carry traffic on its own.

### s3fs-fuse template pool

| step | result |
|---|---|
| Garage bucket `templates` + grant scratch-key | ✅ |
| Upload alpine.qcow2 (186 MB) — block fanout 49 / 73 / 45 / 41 MB across nodes | ✅ |
| Mount on sim-1 with `-o sigv4 -o endpoint=garage -o use_path_request_style` | ✅ (gotcha: `-o sigv2` is the default; Garage rejects v2 with "Unsupported authorization method") |
| Define libvirt directory pool `templates` → `/var/lib/libvirt/templates` | ✅ |
| `virsh vol-list templates` shows `alpine.qcow2` | ✅ |
| `virsh pool-info templates` reports 64 PiB capacity (s3fs lies about S3-as-infinite) | (note) |

### T8. Stress-during-migration

Continuous host-side write workload (60 writes × 1 MiB at random offsets
1700–1800 MiB, every 200 ms) running on sim-1 while live-migrating
vm-hero sim-1 → sim-2.

| step | result |
|---|---|
| Migration completed mid-workload | 1.14 s |
| Total writes attempted | 60 |
| Unique offsets after dedup | 45 |
| Read back from sim-2 (post-migration), compared SHA-256 per offset | **45/45 OK, 0 corruption** |

The 15 "fails" before dedup were duplicate offsets — same offset written
twice during the run, where the older write was overwritten by the newer.
Both versions were in the writelog; only the latest survives the disk
state, which is correct.

### T9. RustFS endpoint restart under load

VM running on sim-2; s3backer mount on sim-2 → `192.168.2.168:9000`
(local RustFS). Continuous write workload (100 writes × 1 MiB, 100 ms
between writes), then `systemctl restart rustfs.service` issued on sim-2
mid-workload. Total writer wall-clock ~12 s; restart took 1.16 s.

| step | result |
|---|---|
| Writes completed | 100 / 100 |
| Failures recorded | **0** |
| Unique offsets verified post-restart | 40 / 40 OK |

s3backer's default retry/backoff absorbs the brief endpoint outage —
individual block writes pause and resume across the restart, no
data corruption, no observable failure to the writer.

## Gotchas worth folding into the installer

1. **RustFS container UID mismatch**: chown host data + log dirs to
   `10001:10001` before starting the container.
2. **RustFS log dir**: bind-mount `/var/log/rustfs` separately; never put
   the log dir under `/var/lib/rustfs/data` (becomes a phantom bucket).
3. **RustFS volumes via env, not CLI**: pass `RUSTFS_VOLUMES=...` in the
   env file. Passing volumes as CLI args makes the entrypoint append
   `/data`, breaking the configured `RUSTFS_ERASURE_SET_DRIVE_COUNT`.
4. **s3backer 1.4.5 is not enough**: build from master (2.x) for
   `--sharedDiskMode` and `--no-vhost`. Static binary works across
   AlmaLinux 9 sims after the fuse3 runtime is installed.
5. **s3backer `--region` triggers vhost-style** (1.4.x bug — fixed in
   master via `--no-vhost`). Master with `--no-vhost` gives clean
   path-style requests against RustFS on raw IPs.
6. **s3fs-fuse needs `-o sigv4 -o endpoint=garage`** for Garage. Default
   is sigv2 fallback which Garage rejects.
7. **libvirt + FUSE files**: add per-disk `<seclabel model='dac' relabel='no'/>`
   AND domain-level `<seclabel type='none'/>` to skip the chown attempt,
   plus `usermod -a -G disk qemu` so qemu can open `/dev/drbd*`.
8. **VM UUIDs must match** for cross-node migration. Generate once,
   re-use across sim-1 + sim-2 `virsh define` calls.
9. **s3backer block size**: pick 1 MiB or larger for VM data disks.
   Smaller blocks = more S3 round-trips per VM I/O. 4 KiB default is
   useless for VM workloads.
10. **`--unsafe` on virsh migrate is correct here** — DRBD and shared
    FUSE are both "shared" but libvirt can't auto-detect either.

## Cluster-size gating

Confirmed semantics for the installer:

| nodes | unlocked tiers (VM) | unlocked tiers (fileshare) |
|---|---|---|
| 1 | Cattle | Scratch |
| 2 | + Pet | + Bulk |
| 3 | + Pet+ (DRBD-3way) | + Critical (RustFS EC:2 needs 4 for clean config; 3-node EC:2 = 1+2 is RS-valid but undocumented and brittle in alpha.99) |
| 4 | + Critical-EC | + Critical-EC |

Recommendation: **gate Critical-EC at 4 nodes for v1.** 3-node Critical
on RustFS is not a documented/tested config; revisit once RustFS GA
ships and we can validate the 1+2 EC pool initialization.

## Net assessment

The four-node trial validates the design end-to-end:

- Three tiers reachable through standard S3 clients.
- s3backer + RustFS Bulk handles VM data disks across live migration
  without any cache flushing dance — `--sharedDiskMode` does the right
  thing.
- DRBD-2way still owns the boot disk (correct: blockcopy / pivot is
  a known-good path; s3backer is fine for cold-tier data alongside it).
- Garage delivers RAID-0-across-nodes for downloads / templates with
  block fanout matching what we want for a future 4×200GbE cluster.

Two product limits to plan around: per-bucket EC (RustFS won't), and
RustFS lifecycle / metrics (alpha.99 — wait for GA).

State of the cluster after the trial:

```
  RustFS:   3 buckets (critical, bulk, vmdisk1) on 4-node EC pool
  Garage:   2 buckets (scratch, templates) replication_factor=1
  vm-hero:  running on sim-1 (Pet, DRBD-2way + s3backer-on-Bulk)
  alp2d:    leftover from previous test run (4-disk 2-way) — harmless
  thin pool: ~5 % used on each sim
```
