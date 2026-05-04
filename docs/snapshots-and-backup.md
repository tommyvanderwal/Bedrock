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
| 2. Atomicity | Wrap the per-disk `lvcreate --snapshot` calls inside a single fsfreeze window. All disks of the VM at the same logical instant. |
| 3. Multi-replica | **Snapshot on the DRBD primary side only.** This is the part where I had a fragile design earlier; see §3 below for why "both sides at the same time" doesn't hold up and why one-side-only is the right answer. |
| 4. Storage cost | Snapshots are log-tracked. mgmt enforces a per-VM retention policy (keep last N, delete older than M days). Thin-pool fill alarms. |
| 5. Restore | Stop VM → `lvconvert --merge` → restart VM (cattle: trivial; pet: triggers a DRBD resync to the peer, which is fine — it's the price of one-side-only snapshots). Read-only inspection: mount the snapshot directly. |

---

## 3. Why "both sides at the same time" is the wrong primitive

The earlier draft of this doc said: master appends an intent, both
DRBD peers run `lvcreate --snapshot` locally, both end up with a
snapshot of the same content. That's wrong, or at least fragile.
Spelling out why before I fix it:

- **fsfreeze pauses guest-issued writes**, but only the writes from
  inside the guest. DRBD has its own pipeline between the device the
  guest sees and the underlying LV. Writes that the guest issued
  before fsfreeze returned may still be in flight on either node
  when we run `lvcreate`. fsfreeze guarantees the guest **filesystem**
  is consistent; it does not guarantee both **LVs** are byte-identical
  at the lvcreate moment.
- **DRBD Protocol C guarantees an *acked* write hit both LVs**, but
  it doesn't give you a "freeze the LVs at this exact byte" primitive.
  If a write is in flight at lvcreate time, one side may have
  written it and the other hasn't yet.
- **Wall-clock skew between the two `lvcreate` calls** is real. Even
  with NTP, processes on two nodes scheduling at the "same time" can
  differ by tens of ms. A busy guest writes a lot of bytes in tens
  of ms.
- **Conclusion:** there is no off-the-shelf primitive in the
  DRBD/LVM stack that guarantees byte-identical snapshots on both
  peers without explicit IO-suspension coordination. Pretending
  otherwise gives you a sync that *seems* to work in low-load
  testing and breaks under production load. Exactly the user's
  intuition.

What you'd need to make it actually-byte-identical: `drbdsetup
suspend-io` on both peers (DRBD blocks new writes and drains in-
flight ones), waited for both peers to confirm "drained", then
`lvcreate` on both, then `drbdsetup resume-io`. Doable, but it's
a coordination protocol with timeouts, peer-failure handling, etc.
Lots of moving parts, lots of failure modes that mostly hurt the
operator who just wanted a backup.

**The right answer for v1: snapshot on the DRBD primary side only.**

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
             requested_by: <user>,
             primary_node: <where DRBD primary lives> }
        ③ return 202 + intent_log_index to caller
        │
        ▼ replication
        │
   ▼ ONLY on the DRBD-primary node (master in single-master setups):
   reactor sees snapshot_create_intent + primary_node == self
        │
        ▼
   if quiesce: virsh domfsfreeze my-pet
        │  (qemu-ga → fsfreeze syscall in guest)
        │  → guest FS quiesces; in-flight IO drains
        ▼
   for each disk LV of my-pet:
     lvcreate --snapshot --name vm-my-pet-disk0-nightly-<ts>
       bedrock/vm-my-pet-disk0
        │
        ▼
   if quiesce: virsh domfsthaw my-pet
        │
        ▼
   append snapshot_created log entry:
     { vm: my-pet, label: nightly, ts: ...,
       primary_node: <self>,
       disks: [{lv: vm-my-pet-disk0,
                snap: vm-my-pet-disk0-nightly-<ts>,
                bytes: <thin pool delta>}] }

   ── On the DRBD secondary node:
   reactor sees snapshot_create_intent → primary_node != self → NO-OP
   The peer's underlying LV is byte-identical to the primary's
   (Protocol C, modulo acked writes), but we DON'T take a local
   snapshot there. The snapshot is one LV on one node.
```

Why this is fine:
- **Backup**: PBS / Borg / S3 reads the one snapshot from the
  primary; ships data offsite. We don't need redundant local
  snapshots — the offsite copy is the redundancy.
- **Audit / inspection**: `mount -o ro /dev/.../snap-...` on the
  primary, look around. Read-only, no DRBD interaction.
- **Disaster**: if the primary node dies before backup ships, the
  snapshot is gone — but that's exactly when you wanted a snapshot
  anyway. Take the next one on the new primary; ship it offsite
  promptly.

The trade-off is **revert speed** for pet/vipet (see §4b below):
since only one side has the snapshot, reverting forces a DRBD
resync to the peer afterwards. That's the right trade — reverts
are rare and admin-driven; backup operations are frequent and
unattended. We optimise for the common case.

Cattle is even simpler: only one node has the disk LV at all, so
"primary side only" and "the home node" are the same place.

**`cache=none`** in the VM's libvirt XML stays required — without
it, qemu has its own writeback cache that holds dirty pages
fsfreeze can't see. We already set it.

**fsfreeze window** is microseconds to tens of milliseconds. No
observable hang in the guest.

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

Destructive; loses all writes since the snapshot. Cattle case is
trivial; pet case involves a DRBD resync (the cost of one-side-only
snapshots).

**Cattle:**
```
  ① bedrock vm shutdown my-cattle
  ② bedrock vm snapshot restore my-cattle --label nightly
  ③ master appends snapshot_restore_intent
  ④ home node's reactor:
       lvconvert --merge almalinux/vm-my-cattle-disk0-nightly-<ts>
       (origin LV ends up at the snapshot's content; snap consumed)
  ⑤ append snapshot_restored
  ⑥ bedrock vm start my-cattle
```

**Pet/vipet:**
```
  ① bedrock vm shutdown my-pet              (no IO on the resource)
  ② bedrock vm snapshot restore my-pet --label nightly
  ③ master appends snapshot_restore_intent
  ④ DRBD-primary node's reactor:
       drbdadm secondary tier-pet-disk      (no FDs left after step 1)
       drbdadm down tier-pet-disk           (release the LV)
       lvconvert --merge bedrock/vm-my-pet-disk0-nightly-<ts>
       drbdadm up tier-pet-disk             (DRBD reads the LV again)
       drbdadm primary --force tier-pet-disk
       drbdadm new-current-uuid --clear-bitmap tier-pet-disk
              (mark our content authoritative; peer becomes "outdated"
               and DRBD will resync to it)
       drbdadm primary tier-pet-disk
  ⑤ DRBD-secondary node:
       (no log-driven action needed — DRBD's resync brings its LV
        in line with our merged content automatically)
  ⑥ append snapshot_restored
  ⑦ bedrock vm start my-pet                 (resync continues in
                                              background; no impact
                                              on the primary's IO)
```

The `drbdadm new-current-uuid --clear-bitmap` is the key incantation
that tells DRBD "my data is authoritative; treat the peer as
outdated". After that, the resync pushes the new content to the
peer in the background. Resync time = data divergence × bandwidth;
for a pet VM with a few hundred GB of disk this is minutes-to-an-
hour over a fast cable. Acceptable for a rare, admin-initiated
operation.

A faster revert is possible if both sides had a synchronized
snapshot — that's the §3 trade-off we deliberately didn't take.

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

PBS reads backup data via `proxmox-backup-client`. The snapshot
exists only for the duration of the backup run — make it, read it,
delete it:

```
  bedrock vm backup my-pet --target pbs://backup.example/datastore-1
        │
        ▼
   ① snapshot create (--quiesce) → snapshot_created log entry
   ② mount snapshot LV read-only at /var/lib/bedrock/backup-staging/<vm>
        OR: pass /dev/bedrock/vm-my-pet-disk0-bk-<ts> as a block device
   ③ proxmox-backup-client backup \
        my-pet-disk0.img:/dev/bedrock/vm-my-pet-disk0-bk-<ts> \
        --repository pbs://...
   ④ lvremove the snapshot                              (← "discard")
   ⑤ append backup_completed log entry { target, size, ts, vm }
```

That's the basic loop the user asked about: **make → PBS reads →
discard**. The snapshot only consumes thin-pool space while it
exists. For an unattended nightly run it's typically a few minutes.

**Safeguards in mgmt:**
- Snapshot has a max lifetime (default 6 h). A periodic task in mgmt
  scans for snapshots older than max-age + not in an active backup
  job and force-removes them.
- Each backup task has a timeout. If `proxmox-backup-client` doesn't
  finish in N hours, the task aborts and the snapshot is removed.
- backup-failed and backup-aborted are log entries too — an
  abandoned snapshot is observable, not silent.

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

### 6c-bis. Changed-block tracking — does PBS need to read the whole volume each time?

Short answer: **no it doesn't, but the cheapest way to avoid that
*does* require keeping the previous snapshot around.** Three options,
in increasing operational complexity:

**Option 1 — read whole snapshot, rely on PBS chunk dedup. (v1.0)**

This is the default. proxmox-backup-client reads the whole snapshot
locally, chunks it (typically 4 MiB), hashes each chunk, and sends
to PBS only the chunks PBS doesn't already have. The wire and the
PBS storage are efficient — only changed chunks travel — but the
**local read** is the full volume each backup.

For a 1 TB pet VM with 50 GB nightly change, that's 1 TB of disk
read every night just to discover what changed. The thin pool helps
slightly (only allocated blocks are read), but on a busy VM that's
still hundreds of GB of IO.

This is fine for small VMs and overnight backup windows. It's the
right starting point.

**Option 2 — `thin_delta` between two LV snapshots. (v1.1)**

LVM thin's metadata knows exactly which blocks belong to which
snapshot. `thin_dump` exports the metadata; `thin_delta` between
two thin device IDs lists the blocks that changed.

```
  step 1 (last night): take snap-N, back up via PBS, KEEP snap-N
                       around as the next reference
  step 2 (tonight):    take snap-N+1
                       thin_delta /dev/mapper/bedrock-thinpool snap-N snap-N+1
                          → list of changed block ranges
                       read only those ranges from snap-N+1
                       send the changed chunks to PBS (PBS dedup
                          still applies on top)
                       lvremove snap-N
                       keep snap-N+1 as the new reference
```

This **does require the previous reference snapshot to exist** —
that's how thin_delta knows what "changed since last backup" means.
The cost is the divergence space the reference snapshot accumulates
over its lifetime: roughly one day's worth of writes for a daily
backup.

For a 1 TB VM with 50 GB nightly change:
- Without CBT: 1 TB read/night, 50 GB on the wire (after dedup).
- With CBT: 50 GB read/night, 50 GB on the wire. Identical wire
  traffic; **20× less local IO**. Cost: 50 GB of extra thin-pool
  space for the reference snapshot.

That's a great trade for big-disk VMs. Implementation is small —
thin_dump + thin_delta + a custom uploader to PBS that does the
sparse read. PBS's dedup-by-content storage doesn't care that we
sent partial data; it just ends up reusing the chunks that didn't
change.

**Option 3 — `dm-era` or QEMU persistent dirty bitmap. (later, if ever)**

If the "always have one extra snapshot taking space" cost ever
becomes objectionable, two alternatives don't need it:

- **`dm-era`** is a device-mapper layer above the thin LV that
  records a "dirty block era" continuously. You ask "what blocks
  changed since era N?" without keeping a snapshot at era N.
  Adds an extra dm layer; works for any block device.
- **QEMU persistent dirty bitmap** (`qemu-img bitmap --add ... --persistent`)
  records dirty blocks in qcow2 metadata or a sidecar file. After a
  backup, clear the bitmap; QEMU starts tracking again from zero.
  Backup-time, read only blocks the bitmap says are dirty. This is
  what Proxmox VE itself uses for QEMU-managed VMs.

Both need integration work that thin_delta doesn't. v1 picks
thin_delta because it's the path of least resistance from where
we already are (LVM thin is the storage primitive on every node;
the kernel and userspace tooling are already installed).

**Storage-cost summary for daily backups:**

| Option | Local IO/night | Wire/night | Extra disk used |
|---|---|---|---|
| Whole-volume + PBS dedup (v1.0) | full volume | changed bytes (≈daily delta) | none — snapshot lives only during backup |
| `thin_delta` CBT (v1.1) | changed bytes | changed bytes | ~1 day's worth of writes (the reference snapshot) |
| `dm-era` or QEMU bitmap | changed bytes | changed bytes | metadata only (~MB) |

For most of our target market, option 1 is enough. Option 2 lands
in v1.1 when bigger VMs become a concern. Option 3 stays in the
"only if" pile.

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
| Multi-replica | vSAN replicates blocks + snap blocks | Cassandra distributes snap metadata + blocks | Ceph RBD replication carries snaps | SCRIBE handles it | **One snapshot on the DRBD-primary side** — no fragile two-peer coordination. Backup tools ship offsite for redundancy; revert pays a DRBD resync. See §3. |
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
layer over the LV — snapshot the LV (on the primary side), get the
same bytes the DRBD device serves.

The design call I had to walk back: **don't try to snapshot both
DRBD peers simultaneously without explicit IO coordination.** It
*looks* simple ("the LVs are byte-identical, just lvcreate on both")
but in production it's not — fsfreeze handles guest-level writes,
not in-flight DRBD pipeline writes; wall-clock skew is real; the
DRBD/LVM stack has no off-the-shelf "freeze both LVs at the same
logical byte" primitive. Pretending otherwise gives a sync that
works under low load and breaks when machines are busy.

The chosen primitive is single-side snapshotting (always on the
DRBD primary), with `drbdadm new-current-uuid --clear-bitmap`
+ resync as the price of revert. Backup operations are unaffected
by this choice; they just read the one snapshot and ship it
offsite. PBS / Borg / S3 are the v1 backup story; the snapshot is
the API surface.

A coordinated two-peer snapshot is doable later if fast revert
becomes a real customer ask — `drbdsetup suspend-io` on both,
`lvcreate` on both, `drbdsetup resume-io`. But it's deliberately
out of v1 because it adds a coordination protocol with timeout/
failure modes for a rarely-exercised feature.

What's NOT in the design: a clever native backup engine. We don't
need one. PBS and Borg and rsync-to-S3 already work; we just give
them a clean read surface.

**Lifecycle of one backup run, end-to-end:**

```
  v1.0 (whole-volume + PBS dedup):
     lvcreate --snapshot   → PBS reads     → lvremove
     [snapshot exists for ~minutes]

  v1.1 (thin_delta CBT):
     lvcreate --snapshot snap-N+1
     thin_delta snap-N snap-N+1            → list of changed ranges
     PBS reads only those ranges from snap-N+1
     lvremove snap-N        (the previous reference; not snap-N+1)
     [the JUST-USED snapshot becomes the next reference;
      one snapshot is always alive between backups]
```

The "make → read → discard" pattern is the cheap default. CBT
keeps one snapshot alive between backups as the reference for
"what changed since last time" — that's the price of the 20×
local-IO reduction. Thin pool fill alarms keep that price visible.
