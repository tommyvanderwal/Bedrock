# 4-node storage tier scale-up + sim-1 removal — 2026-04-30

End-to-end POC for the 3-tier storage architecture (scratch / bulk / critical)
with seamless 1 → 4 node growth and the start of the inverse N=4 → N=3 path.
Run on the dev box's nested-KVM testbed (4× 12 GB AlmaLinux 9 sim nodes,
mgmt LAN 192.168.100.0/24, DRBD ring 10.99.0.0/24).

## What worked end-to-end

| Phase | Result |
|---|---|
| N=1 setup (local LV thin, symlinks) | ✅ `/bedrock/{scratch,bulk,critical}` on per-node LVs |
| Cattle VM creation | ✅ `bedrock vm create web1 --type cattle` |
| Online cattle → pet conversion | ✅ ~7 s, VM stayed running, DRBD external-meta zero-copy |
| Live migration sim-1 ↔ sim-2 | ✅ ~1.1 s (twice, before + after scale) |
| N=1 → N=2 promote | ✅ Garage scratch (RF=1, FUSE mount via s3fs), DRBD-NFS for bulk + critical |
| N=2 → N=3 (critical → 3-way DRBD) | ✅ after rebuilding metadata with `--max-peers=7` |
| N=3 → N=4 | ✅ Garage layout extends, NFS clients added |
| Tier integrity through every scaling step | ✅ MD5 hashes verified across all nodes, all tiers |
| Pre-removal: pet → vipet, bulk 2→3-way, critical 3→4-way | ✅ Add capacity before removing the departing node |
| Mgmt + NFS server failover sim-1 → sim-2 | ✅ Stop services on sim-1, demote DRBD, promote on sim-2, mount, re-export, re-mount peers |
| Garage layout drain of sim-1 | ✅ `garage layout remove <id>` + `apply --version N`, partitions redistributed across surviving 3 nodes |

## Findings worth keeping

### 1. DRBD `--max-peers=7` must be set at metadata creation time

`drbdadm create-md` defaults to `--max-peers=1`. If you later want to add a
3rd peer, the create-md must be re-run with `--max-peers=7` (or higher),
which involves:

- Take the resource fully down (NFS server stop + umount + drbdadm down)
- Recreate metadata with `--max-peers=7` (data LV is preserved, only meta LV
  is touched — the external-metadata advantage)
- Bring back up + force resync

This is the operational play for a brief scratch-write outage to grow a 2-way
to N-way. Code now creates all metadata with `--max-peers=7` from day one,
so this dance is not needed on a fresh install.

### 2. External DRBD metadata is the right call (revisited)

The `cattle → pet` conversion preserved the VM's data byte-for-byte because
the meta LV was a separate ~32 MB LV; DRBD only initialized that LV, leaving
the data LV untouched. Same pattern for `local LV → DRBD-replicated tier`:
zero-copy promotion. **Internal metadata would have overwritten the last
~32 MB of the running filesystem.** Keeping external.

### 3. Atomic symlink swap is real

`/bedrock/<tier>` is the stable mountpoint; the symlink target swaps via
`rename(2)` (atomic). In-flight readers continue on the old inode; new opens
follow the new symlink. Verified by repointing sim-2's symlinks from
`bulk-nfs` → `bulk-drbd` mid-flight when sim-2 took over master role from
sim-1.

### 4. Garage RF=1 means scratch loses data when you remove a node

By design. We removed sim-1 from the Garage cluster (`layout remove` then
`apply`), and the partitions that lived only on sim-1 are gone. The
test-object's MD5 went from `9f909473…` to `d41d8cd9…` (the empty file MD5
— Garage returns empty bytes for the missing partition slice).

`bulk` (RF=2) and `critical` (RF=3, post-add) survived intact — MD5 hashes
match on every surviving node.

This matches the documented design ("scratch is RAID0 — lose-it-and-redownload
semantics, store source URL in `bulk`"). Operator implication: before
removing a Garage node, snapshot scratch contents you actually want
preserved.

### 5. **The big finding: vipet with a permanently-dead peer blocks live migration**

After sim-1 removal, web1's DRBD config still has sim-1 as the 3rd peer
(node-id 0). `drbdadm forget-peer` requires the resource to be DOWN, which
means stopping the VM (defeats live-migration). Without forget-peer, the
running DRBD treats sim-1 as "Connecting" forever — and **secondary peers
can't be promoted to dual-primary while a configured peer is in
`Connecting` state**, so live migration of web1 from sim-2 → sim-3 fails
with:

```
qemu-kvm: -blockdev .../dev/drbd1000: Could not open '/dev/drbd1000':
Read-only file system
```

The `vipet → pet` conversion API also can't complete cleanly because part
of its cleanup path SSH's to the now-dead sim-1 to drop its resource side.
Task fails with no rollback for the kernel state.

**Operational pattern for clean removal needs to be:**

- *Before* taking sim-1 down, also add a *4th* peer to any existing vipet
  resource that uses sim-1 (so post-removal there are 3 alive peers, no
  ghost). For pet (2-way) we already do `pet → vipet` to add the 3rd
  peer; the analogous play for vipet would be add a 4th, then drop sim-1.
- Or: accept brief VM downtime to `drbdadm down` + remove the dead peer
  from the on-disk config + `drbdadm up`. This requires a code path
  that exists in `_vm_convert_downgrade` but currently fails when the
  peer host is unreachable.

The cleanest fix is probably to **make the convert-downgrade path tolerant
of an unreachable peer**: if SSH to the departing node fails *and* the
operator has explicitly invoked the downgrade after a removal, skip the
remote cleanup and finish the local-side resource update. Today it
short-circuits to `failed` instead.

### 6. mgmt + NFS coupling — confirmed sensible default

Per Tommy: keep mgmt and bulk on the same node. The role moves together
when sim-1 left:

- Stop `bedrock-mgmt`, `bedrock-vm`, `bedrock-vl`, `nfs-server` on sim-1
- DRBD secondary on sim-1, primary on sim-2
- Mount `/var/lib/bedrock/mounts/{bulk,critical}-drbd` on sim-2
- rsync `/opt/bedrock/{mgmt,iso,data,bin}` from sim-1 → sim-2
- Copy systemd unit files
- Start services on sim-2
- `cat /etc/exports.d/bedrock-tiers.exports` regenerated, `exportfs -ra`
- NFS clients (sim-3, sim-4) update `/etc/fstab`: replace
  `10.99.0.10:/var/lib/bedrock/mounts/...` with `10.99.0.11:/...`, mount

`/bedrock/<tier>` symlinks on sim-2 swap from `bulk-nfs` → `bulk-drbd`
(now points at the local DRBD mount, not NFS to itself).

### 7. cluster.json on peers becomes a stale partial copy

`/etc/bedrock/cluster.json` is only fully populated on the mgmt node.
When mgmt fails over, the new mgmt needs to be primed with the existing
`cluster.json` (rsync from old mgmt before it goes down) and then
the master fields updated to point at the new mgmt. Compute-only nodes
have only a partial `cluster.json` (just the tier state set by
`tier_storage.set_tier_state`).

## Concrete code fixes from this run (in branch `storage-tiers-1to4`)

- `tier_storage.py` creates DRBD metadata with `--max-peers=7`
- `nfs_mount_drbd_tiers` uses plain fstab + mount, not systemd `.mount`
  units (the `\x2d` escape for `-` made `systemctl enable` flaky)
- `s3fs_mount_scratch` adds the `endpoint=garage` SigV4 region option
- `garage_form_cluster` uses local execution for the local node
  (no SSH-to-self)
- `s3fs_mount_scratch` installs `epel-release` before `s3fs-fuse`
  (s3fs-fuse is not in stock AlmaLinux 9 repos)
- `vm.py` uses VG `bedrock` (shares the thin pool with tier storage)
  instead of creating a separate loop-backed `almalinux` VG

## What's left

- vipet → pet downconvert API path needs to tolerate an unreachable
  departing peer (POC blocker for full live-migration-during-removal)
- Define + automate the "add an extra peer for any vipet resource that
  uses the departing node" step as the proper precursor to node removal
- Sim-2 removal (next) — will use this finding to plan ahead
- Sim-3 removal → final state on sim-4 alone (Garage decommissioned,
  tiers revert to local LV)
