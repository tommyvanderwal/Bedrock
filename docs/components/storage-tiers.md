# Bedrock Storage Tiers

Two parallel hierarchies, three classes each. Class availability scales
with cluster size.

## VM Disk Tier — DRBD-backed, full random-IO performance

| Class      | Min Nodes | Layout              | On node loss |
|------------|-----------|---------------------|--------------|
| **Cattle** | 1 | Local thin LV         | VM shares fate with its host: node reboot = VM reboot |
| **Pet**    | 2 | DRBD 2-way mirror     | Failover to surviving node |
| **Pet+** (VIPet) | 3 | DRBD 3-way     | Survives 2 concurrent failures; 1 node down for maintenance still leaves 2 copies |

## Fileshare Tier — S3-compatible, live-migration friendly

| Class       | Min Nodes | Backend / Layout                | Tolerates |
|-------------|-----------|---------------------------------|-----------|
| **Scratch** | 1 | Garage `replication_factor=1` (RAID-0-like fanout across nodes) | Nothing — any node loss = lost data on this clusterwide volume |
| **Bulk**    | 2 | RustFS `REDUCED_REDUNDANCY` (EC:1, RAID-5-like) | 1 node failure |
| **Critical** | 4 (v1) | RustFS `STANDARD` (EC:2, RAID-6-like) | 2 concurrent failures — data survives even when write quorum is lost (manual restart, data intact) |

## Rules

- Boot disks belong on the VM Disk tier. S3-backend IO patterns are
  wrong for OS workloads. Use a Bulk-tier s3backer disk only for
  **cold/warm secondary data**: rman backups, media files, archive,
  ML model weights, ISO mirrors. Sync-write S3 round-trips are ~10–20 ms
  per block on a fast LAN — usable but not OS-grade.
- A VM with any Scratch-class disk is itself classified Scratch — most
  VMs cannot run with a missing disk.
- Live migration cutover: ~1 s for all classes except Cattle (pinned to
  its host node). Validated on the sim cluster: forward 1.30 s, reverse
  1.02 s for a Pet VM with a DRBD-2way boot disk + s3backer-on-Bulk
  data disk (see [storage-trial-2026-04-27](../scenarios/storage-trial-2026-04-27.md)).
- Cluster size gates the catalog. A 1-node deployment offers only Cattle
  and Scratch. Pet, Bulk, Pet+, and Critical unlock as nodes are added.
- Mature components on the most critical tier. RustFS reaches GA July
  2026; until then, Critical's RustFS path is in trial. The **DRBD-3-way
  fallback** for Critical-tier fileshares is available if RustFS proves
  unreliable on this cluster — disabled by default.

## Why two backends

RustFS only exposes two storage classes per cluster (STANDARD,
REDUCED_REDUNDANCY) — it cannot host three different EC profiles in one
deployment. RustFS also rejects EC:0 in distributed mode (Reed-Solomon
needs parity > 0 by definition; "RAID-0 across nodes" is not an EC
scheme). Garage at `replication_factor=1` is a clean fit for the
Scratch tier: replication=1 places each block on exactly one node by
rendezvous hash, so a 100 GiB upload distributes ~25 GiB to each of 4
nodes with no redundancy.

## Implementation status (v1, validated 2026-04-27)

| Tier      | Backend           | Status |
|-----------|-------------------|--------|
| Cattle    | Local LVM thin    | shipping (existing) |
| Pet       | DRBD 2-way        | shipping (existing) |
| Pet+      | DRBD 3-way        | shipping (existing) |
| Scratch   | Garage v2.3.0     | trial validated — install codified in `installer/lib/storage_install.py` |
| Bulk      | RustFS 1.0.0-α.99 | trial validated — REDUCED_REDUNDANCY=EC:1 |
| Critical  | RustFS 1.0.0-α.99 | trial validated at 4 nodes — STANDARD=EC:2; **3-node config gated off** until RustFS GA |

End-user access: standard S3 clients (aws-cli sigv4 + path-style, mc,
boto3, s3fs, s3backer). All buckets exposed on the management LAN; for
ISOs and templates, mount on each node via s3fs-fuse and register as a
libvirt directory storage pool.

## Cluster-size gating in mgmt

The mgmt API advertises the available tiers based on `len(online_nodes)`:

```python
def available_tiers(node_count):
    vm = ["cattle"]
    fs = ["scratch"]
    if node_count >= 2:
        vm.append("pet"); fs.append("bulk")
    if node_count >= 3:
        vm.append("pet+")
    if node_count >= 4:
        fs.append("critical")  # RustFS EC:2 needs 4 for documented config
    return {"vm_disk": vm, "fileshare": fs}
```

The dashboard's tier dropdown reads this list — selecting an unavailable
tier is impossible, not just "errors at submit."

## TRIM end-to-end

The discard chain is supported at every layer **except** s3backer's FUSE
backend, which doesn't pass virtio-blk DISCARD through. Workaround:
qemu-side `detect-zeroes='unmap' discard='unmap'` converts zero-writes
to discard internally; s3backer then DELETEs the all-zero block on
RustFS (validated: 50-block zero-write test deleted 50 RustFS objects).

A backstop compactor (`installer/lib/s3backer_compactor.py`) walks the
bucket on a 15-minute cycle and DELETEs any all-zero block that slipped
through. Configured via the systemd template
`s3backer-compactor@<bucket>.timer`.

## Multi-network resilience

The cluster runs on two networks: `bedrock-mgmt` (192.168.x) and
`bedrock-drbd` (10.99.x — used for both DRBD replication and RustFS /
Garage inter-node traffic). Per-peer host routes via the mgmt LAN at
metric 200 provide outbound failover when the drbd link drops on a
node — kernel switches transparently and TCP reconnect logic handles
the brief disruption.

For full multi-network resilience (both inbound and outbound, for
in-flight connections), use Linux bonding (LACP or active-backup) on
top of the two interfaces. Per-peer routing alone protects against
single-link admin-down on the local node, but does not help if the
peer's drbd interface fails.
