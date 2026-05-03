# Bedrock — snapshots and backup, designed once

A long, deliberate think before any code. The goal is to land on
**one** snapshot mechanism that works the same for cattle (local LV-
thin) and pet/vipet (DRBD over LV-thin), captures application-
consistent state when the guest cooperates, replays cleanly through
the cluster log, and gives backup tools a stable read surface they
can stream from.

---

## 1. What the storage actually looks like underneath

Both cattle and pet/vipet land on **LVM thin** volumes — that's our
universal storage primitive on every node:

```
  cattle VM disk        pet/vipet VM disk
  ─────────────────     ─────────────────────────────
  /dev/vda inside       /dev/vda inside guest
  guest                       │
       │                      ▼
       ▼                /dev/drbdN          ← DRBD primary (one side at a time)
  /dev/almalinux/             │
   vm-cattle-disk0            ▼
       │              /dev/bedrock/vm-pet-disk0   ← LV-thin
       │              + /dev/bedrock/vm-pet-disk0-meta (DRBD activity log;
       ▼                                              SEPARATE LV)
  thin pool                   │
       │                      ▼
       ▼              thin pool (per node)
  physical            │
                      ▼
                physical
```

Two important properties:

1. **The user-data LV under DRBD is byte-identical to what `/dev/drbdN`
   serves.** DRBD writes its bookkeeping to the separate meta-disk LV
   (`-meta`), not to the data LV. So a snapshot of the data LV captures
   exactly what the guest wrote, with nothing DRBD-specific to clean
   up before reading it.
2. **Both DRBD peers have the same data on their underlying LVs**
   (Protocol C, synchronous). A snapshot taken on either node reflects
   the same point-in-time content.

This means LVM thin snapshots **are the snapshot primitive** — for
both VM types, on every node. No second mechanism is needed.

---

## 2. The five problems any snapshot system has to solve

Independent of vendor:

1. **Quiescence.** When the snapshot is taken, what state are the
   in-guest filesystem and applications in?
2. **Atomicity.** All disks of a multi-disk VM must be at the same
   point in time.
3. **Multi-replica coordination.** For DRBD-replicated disks, both
   nodes need to capture the same logical point.
4. **Storage cost over time.** Snapshots consume divergence space;
   need rotation, deletion, ceiling alarms.
5. **Restore.** Three flavours (revert, point-in-time clone,
   read-only mount for inspection or backup), each with different
   mechanics.

Bedrock's answer to each, sketched first, then deeply:

| | How |
|---|---|
| 1. Quiescence | `virsh domfsfreeze` via qemu-guest-agent for app-consistent; otherwise crash-consistent (still useful — ext4/xfs always recover from crash-consistent state). |
| 2. Atomicity | Wrap the per-disk `lvcreate --snapshot` calls inside a single fsfreeze window. All disks at the same instant. |
| 3. Multi-replica | The master appends `snapshot_create_intent` to the log. Both DRBD peers' reactors run `lvcreate --snapshot` locally on the underlying LV. Both ends up with a local snapshot of the same content. |
| 4. Storage cost | Snapshots are log-tracked. mgmt enforces a per-VM retention policy (keep last N, delete older than M days). Thin-pool fill alarms. |
| 5. Restore | Stop VM → `lvconvert --merge` → start VM. For pet, the master coordinates the merge on both peers via `snapshot_restore_intent`. Read-only inspection: mount the snapshot directly (bypassing DRBD entirely). |

---

## 3. The core flow — taking one snapshot

```
   operator: bedrock vm snapshot create my-pet --label nightly --quiesce
        │
        ▼
   master's mgmt API:
        ① validate VM exists, not already snapshotting, etc.
        ② append snapshot_create_intent to the log:
           { vm: my-pet, label: nightly,
             ts: 2026-05-04T10:00:00Z,
             quiesce: true,
             requested_by: <user> }
        ③ return 202 + intent_log_index to caller
        │
        ▼ replication
        │
   ┌────┴────────────────────────────────────────────┐
   │                                                 │
   ▼ on every node where this VM has a disk:         ▼
   reactor on master                            reactor on peer
   sees snapshot_create_intent                  sees snapshot_create_intent
        │                                                 │
        │  (master also drives the quiesce)               │
        ▼                                                 │
   if quiesce: virsh domfsfreeze my-pet                   │
        │  (qemu-ga → fsfreeze syscall in guest)          │
        │  → guest FS quiesces; in-flight IO drains       │
        ▼                                                 ▼
   for each disk LV of my-pet:           for each disk LV of my-pet:
     lvcreate --snapshot --name             lvcreate --snapshot --name
       vm-my-pet-disk0-nightly-<ts>           vm-my-pet-disk0-nightly-<ts>
       bedrock/vm-my-pet-disk0                bedrock/vm-my-pet-disk0
        │                                                 │
        ▼                                                 ▼
   if quiesce: virsh domfsthaw my-pet     (peer is done; appends ack
                                            via mgmt API on master)
        │                                                 │
        ▼                                                 │
   master collects acks; once all nodes acked:           │
        ④ append snapshot_created log entry:              │
           { vm: my-pet, label: nightly, ts: ...,         │
             nodes: [node1, node2],                       │
             disks: [{lv: vm-my-pet-disk0,                │
                      snap: vm-my-pet-disk0-nightly-<ts>,
                      bytes: <thin pool delta>}] }
```

A few subtleties:

- **fsfreeze runs only on one side** — the master / DRBD primary —
  because that's where qemu is running. The peer just snapshots its
  underlying LV; the *content* is the same because DRBD already
  synchronously wrote it to both LVs before fsfreeze let the next
  guest IO proceed.
- **fsfreeze window is microseconds-to-tens-of-milliseconds.** No
  observable hang in the guest, no TCP timeouts upstream.
- **`cache=none`** in the VM's libvirt XML is required (we set it
  already) — without it, qemu has its own writeback cache that could
  contain dirty data not yet on the DRBD device when fsfreeze returns.
- **Cattle is the same flow with one peer** — snapshot lives only on
  the home node. The reactor is a no-op on other nodes for cattle
  intents because the disk LV doesn't exist there.

---

## 4. Restore — three modes

### 4a. Read-only mount (for backup, inspection, ad-hoc clone)

The simplest case. The snapshot is just an LV; mount it directly:

```
  mkdir -p /mnt/snap
  mount -o ro /dev/bedrock/vm-my-pet-disk0-nightly-<ts> /mnt/snap
  ... read whatever ...
  umount /mnt/snap
```

DRBD doesn't need to be involved. The snapshot is below DRBD in the
stack; the bytes are the same as what DRBD was serving when the
snapshot was taken. This is the pattern backup tools will use.

### 4b. Revert (roll back the VM to the snapshot)

Destructive; loses all writes since the snapshot:

```
  ① bedrock vm shutdown my-pet           (or virsh destroy if forced)
  ② bedrock vm snapshot restore my-pet --label nightly
  ③ master appends snapshot_restore_intent
  ④ on every node with the disk:
       reactor: drbdadm down tier-pet-disk    (if DRBD)
                lvconvert --merge bedrock/vm-my-pet-disk0-nightly-<ts>
                       (this merges snapshot back into origin;
                        snapshot LV is consumed)
                drbdadm up tier-pet-disk      (DRBD re-syncs;
                                               on both sides
                                               at once → instant)
  ⑤ master appends snapshot_restored
  ⑥ bedrock vm start my-pet
```

For pet/vipet, the merge happens on both peers simultaneously (via
log replication); both end up with identical content; DRBD doesn't
need to resync anything because the content is already identical.

### 4c. Clone (create a new VM from a snapshot)

```
  bedrock vm clone-from-snapshot src-vm --label nightly --as new-vm
```

- Master appends `vm_create_intent` for new-vm
- For each disk of src-vm: `lvcreate --thin --name vm-new-vm-diskN`
  pre-populated from the snapshot (`dd` from snapshot to new LV, or
  `lvconvert --type thin` tricks)
- Define the new VM with disks pointed at the new LVs
- For pet → DRBD setup as a fresh resource

This one's chunkier; deferred to v1.1.

---

## 5. Storage-cost discipline

Thin-pool snapshots are cheap to take but not free over time. Each
snapshot's space cost grows as the origin diverges. A VM doing
heavy writes can consume the whole thin pool in days.

**Bedrock's controls:**

1. **Per-VM retention policy** in cluster.json (a VM's metadata):
   - `keep_last_n`: e.g. 7 nightly + 4 weekly + 12 monthly
   - `max_age_days`: drop snapshots older than this regardless
   - mgmt enforces by appending `snapshot_deleted` log entries on a
     timer.

2. **Thin-pool fill alarm** — when data%/meta% on the thin pool
   crosses 80%, mgmt logs an alert + refuses new snapshot creation
   until space is freed.

3. **No undisclosed snapshots** — every snapshot exists because of
   a `snapshot_created` log entry. Operators can see them in the
   dashboard; orphaned LVs from manual `lvcreate` will be flagged.

---

## 6. Backup integration

Snapshots are the **API surface**. Backup tools read snapshots; they
don't talk to running VMs. This is how every mature stack works
(VMware CBT-via-snapshot, Nutanix snap-as-replication-unit, Ceph
RBD snap diff). Bedrock's design intentionally aligns.

### 6a. Proxmox Backup Server (PBS)

PBS reads backup data via `proxmox-backup-client`. Bedrock's hook:

```
  bedrock vm backup my-pet --target pbs://backup.example/datastore-1
        │
        ▼
   ① snapshot create (--quiesce) → snapshot_created log entry
   ② mount snapshot LV read-only at /var/lib/bedrock/backup-staging/<vm>
   ③ proxmox-backup-client backup vm-my-pet.img:/var/lib/bedrock/backup-staging/<vm> \
       --repository pbs://...
   ④ unmount + lvremove the snapshot
   ⑤ append backup_completed log entry { target, size, ts, vm }
```

PBS handles its own retention and deduplication. We just provide a
read-mounted snapshot for the duration of a backup run.

### 6b. Borg / Restic

Same shape, different command:

```
  borg create backup-repo::vm-my-pet-{now} /var/lib/bedrock/backup-staging/<vm>/
```

For Borg, the snapshot needs to be mounted as a filesystem (not raw
block), so we either:
- Mount the snapshot's filesystem directly (`mount /dev/bedrock/snap /mnt`)
  — works if the VM has a single FS;
- Or for raw-block backup: `borg create ... --read-special /dev/bedrock/snap`

### 6c. Object storage (S3 / Garage)

Stream the snapshot block-by-block to object storage:

```
  dd if=/dev/bedrock/vm-my-pet-disk0-nightly-<ts> bs=4M | \
    zstd -3 | \
    aws s3 cp - s3://backup/vm-my-pet/disk0-<ts>.img.zst
```

Differential backup: maintain a "last full" snapshot and compute
the diff against it (`lvm thin` exposes block-allocation maps —
`thin_dump`/`thin_delta` give the changed-block list, equivalent to
VMware CBT or Ceph RBD diff). This is a v1.1 feature.

### 6d. Bedrock-native backup tier (v1.x)

The scratch tier is already Garage S3 in N≥2 clusters. A logical
roadmap step:
- `bedrock backup target add s3://garage-internal/backups`
- `bedrock vm backup-policy my-pet --schedule '0 3 * * *' --target s3://...`
- mgmt runs cron-like; takes snapshots; ships diffs; rotates.

This gives a no-extra-license backup story out of the box.

---

## 7. Comparison with VMware / Nutanix / Proxmox / Scale

How others land each problem, and where Bedrock's design borrows /
diverges:

| Problem | VMware | Nutanix | Proxmox+Ceph | Scale HC3 | **Bedrock** |
|---|---|---|---|---|---|
| Snapshot primitive | VMFS redo logs (chained delta files) | Cassandra-tracked CoW at storage layer | Ceph RBD snap (CoW) | SCRIBE block-level CoW | **LVM thin snap** |
| Multi-disk atomicity | "Create snapshot of all disks" inside VM scope | Same | Same | Same | One fsfreeze window wraps all `lvcreate` calls |
| Quiescence | VSS / VMware Tools | NGT (Nutanix Guest Tools) | qemu-guest-agent | Vendor agent | **qemu-guest-agent** (the same tool everyone else uses) |
| Multi-replica | vSAN replicates blocks + snap blocks | Cassandra distributes snap metadata + blocks | Ceph RBD replication carries snaps | SCRIBE handles it | Reactor takes the snapshot independently on each DRBD peer; **no extra replication needed** because DRBD already mirrored the data |
| Storage cost | Operator monitors datastore | Cluster automatically rebalances | Ceph balanced; pool quotas | Vendor manages | Per-VM retention in cluster.json + thin-pool fill alarm |
| CBT / changed-block | CBT (proprietary, internal) | Internal track-changed-extents | Ceph RBD `--diff` between snaps | Internal | **`thin_dump` / `thin_delta`** — Linux's own CBT-equivalent on thin pools |
| Backup integration | Veeam/Commvault hook into CBT | Native + 3rd-party | Native PBS | Vendor backup | Snapshot is the read surface; any tool that can read a block device or a mounted FS works |

**What we get right because LVM thin is the foundation:**
- A primitive that's been in mainline Linux for a decade. No
  vendor-specific internals to learn or rely on.
- `thin_delta` gives us CBT for free — the kernel already knows
  which blocks changed between two snapshots.
- Snapshots are visible in `lvs` just like any other LV; operators
  can see them with standard tools. No black-box.
- Restore = `lvconvert --merge`. It's been in lvm2 forever.

**What we don't get vs. competitors:**
- **No deduplication at the storage layer.** Ceph and vSAN can dedup;
  thin LVM doesn't. PBS/Borg/Restic dedup at the backup layer
  instead, which covers most of the value.
- **Single-pool space accounting.** All snapshots share the thin
  pool with running VMs; a runaway snapshot can starve VMs of space.
  Pool fill alarms + retention policy is the answer; it's one alarm,
  not a separate storage system.
- **No multi-cluster snapshot replication.** v2.0 territory. Today
  you ship snapshots out via the backup target; that's good enough.

---

## 8. Log entries we need

Three new entry types to add to `installer/lib/log_entries.py`:

```
  SNAPSHOT_CREATE_INTENT   { vm, label, ts, quiesce, requested_by, disks }
  SNAPSHOT_CREATED         { vm, label, ts, nodes, disks: [{lv, snap, bytes}] }
  SNAPSHOT_DELETED         { vm, label, ts, reason }
```

`view_builder.fold_into` adds a `snapshots` field per VM:
`vms[name].snapshots = [{label, ts, disks}, ...]`. The reactor
on every node materialises (creates/merges/deletes) the snapshot
LVs locally based on these entries.

For backup orchestration:
```
  BACKUP_STARTED           { vm, target, ts, snapshot_label }
  BACKUP_COMPLETED         { vm, target, ts, size_bytes, snapshot_label }
  BACKUP_FAILED            { vm, target, ts, reason }
```

These can ship in a later phase; not load-bearing for the snapshot
mechanics.

---

## 9. Implementation order

Phase A (small, fast — v1.0.x):
- `lib/log_entries.py`: SNAPSHOT_CREATE_INTENT / SNAPSHOT_CREATED /
  SNAPSHOT_DELETED constructors.
- `view_builder.fold_into`: track per-VM snapshots list.
- `mgmt/snapshots.py`: `create_snapshot(vm, label, quiesce)`,
  `delete_snapshot(vm, label)`, `list_snapshots(vm)`. Each runs the
  corresponding lvm/virsh commands and appends the right log entry.
- `mgmt/orchestrator.py::_reactor`: handle SNAPSHOT_CREATE_INTENT
  (run lvcreate --snapshot locally if this node has the disk LV).
- `mgmt/app.py`: `/api/vms/{vm}/snapshots` endpoints.
- CLI: `bedrock vm snapshot {create,list,delete,restore}`.

Phase B (v1.1):
- `bedrock vm backup` to PBS / Borg / S3 — the snapshot becomes a
  mounted read surface; the backup tool runs against it.
- Retention policy enforcement (timer in mgmt).
- Thin-pool fill alarms.

Phase C (v1.2 or later):
- Differential / changed-block backup using `thin_delta`.
- Bedrock-native backup tier on Garage S3.
- Clone-from-snapshot.

---

## 10. The honest summary

LVM thin under both cattle and pet is the lucky accident that makes
the snapshot story tidy. Same primitive, same code path, same
operator mental model. DRBD doesn't fight us because it's a thin
layer over the LV — snapshot the LV, get the same bytes the DRBD
device serves. Both peers snapshot in parallel because the log
replicates the intent.

The interesting new design call is the **fsfreeze + per-node lvcreate
+ log-coordinated** triple: it gives application consistency,
multi-replica coordination, and audit-clean intent tracking in one
move. That's better than VMware's redo-log chains (which fragment
storage and slow VMs over time) and as good as Nutanix's snap-at-
storage-layer (which requires their whole storage stack).

The piece that's NOT in the design: a clever native backup engine.
We don't need one. PBS and Borg and rsync-to-S3 already work; we
just give them a clean read surface.
