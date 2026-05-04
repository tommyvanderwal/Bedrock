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

### 6c-bis. Changed-block tracking — and choosing the right method per VM

Short answer up front: **no, PBS doesn't need to re-read the whole
volume every time, and there are three quite different mechanisms
to avoid it. The right pick depends on the VM's write pattern, so
mgmt picks per-VM and explains the choice.** What follows is what
each mechanism actually does, why one beats the other in different
conditions, and how mgmt decides.

#### What thin-pool CoW actually charges for

A common misread (mine, in the earlier draft): "if the volume gets
fully written every 12 h, the snapshot eats the whole volume's
worth of space." That's not how LVM thin works.

LVM thin CoW counts **first-write-since-snapshot per block**, not
total writes:

- A 4 KiB block written 10 times since the snapshot consumes
  one block of divergence space, not ten.
- A circular WAL / database redo log that rewrites the same 100 MB
  region all night: divergence space ≈ 100 MB, not 100 MB × the
  rewrite count.
- A database that touches large parts of its working set with
  *random* updates across many distinct blocks: divergence space
  scales with unique-block count. **This** is the case that can
  blow up the snapshot.

So the user's "DB log file with lots of edits, same blocks" example
is actually fine for thin-snapshot CBT — the divergence space stays
small because the writes hit the same blocks. The problematic case
is the working-set churn one.

#### Three CBT mechanisms

**Mechanism 1 — `thin_delta` between LV snapshots.**

Cost: keep one reference snapshot alive between backups (sized by
unique-block churn, not total writes).
Failure mode: when the working set is large and randomly updated,
the reference snapshot's divergence approaches the source LV size.
At that point CBT loses its space advantage and we should fall back
to a full read.

```
  step 1 (last night): snap-N, full backup; keep snap-N as reference.
  step 2 (tonight):    snap-N+1.
                       thin_delta pool snap-N snap-N+1 → changed blocks.
                       read only those; ship to PBS.
                       lvremove snap-N; keep snap-N+1 as next reference.
```

**Mechanism 2 — QEMU persistent dirty bitmap.**

QEMU has a built-in feature called a *block dirty bitmap*: a small
in-memory map of which blocks of a virtual disk have been written
since some reference moment. `qmp` (the QEMU monitor protocol)
exposes `block-dirty-bitmap-add`, `-clear`, `-remove`, and a query
that returns the dirty block list.

On qcow2 disks the bitmap is *natively persistent* — it lives in the
qcow2 metadata. On raw block devices (our LV-on-DRBD case) the
bitmap is in-memory only by default; we have to handle persistence
ourselves.

Backup loop:
```
  At VM start (idempotent):
     qmp: block-dirty-bitmap-add name=bedrock-cbt size=64KiB granularity
            (if not already present — load from sidecar file if so)

  Backup time:
     qmp: query for current dirty blocks
     read only those offsets from the running disk image
     ship to PBS
     qmp: block-dirty-bitmap-clear   (mark all clean again)

  At VM stop:
     qmp: dump bitmap state to /var/lib/bedrock/bitmaps/<vm>-disk0.bitmap

  On live migration:
     QEMU 5.0+ migrates dirty bitmaps natively along with the VM
     (the destination side ends up with the same bitmap state).

  On cold migration / VM definition transfer:
     copy the sidecar bitmap file along with the disk content.
```

The bitmap itself is tiny: at 64 KiB granularity, a 1 TB disk =
2 MB bitmap. Practically free.

**Why this is interesting compared to thin_delta:**

| | `thin_delta` | QEMU bitmap |
|---|---|---|
| Storage cost | 1 day's worth of writes (reference snapshot) | ~2 MB sidecar per disk; constant |
| Reads back to | "any earlier snapshot" — operator can pick any reference | Always "since last clear" — flat history |
| Works when VM is off | yes (you snapshot the LV regardless) | no — bitmap requires QEMU to be running and tracking |
| Survives ungraceful crash | snapshot survives; whatever's after the snapshot is what changed | bitmap is in-memory; OS crash loses pending writes from the bitmap. **Requires fall-back to thin_delta or full on next backup after a crash.** |
| Migration | snapshot is a local artifact — not portable across nodes by itself | QEMU 5.0+ migrates bitmaps natively |
| Read cost while VM busy | reads from a frozen LV; no live-IO conflict | reads from the live disk while VM runs (potential IO contention) |

The big win for QEMU bitmap: **no extra disk space.** The big
catches: (a) it depends on QEMU running continuously between
backups; a guest crash or host fence loses the bitmap state and
forces a full re-read; (b) the read is from the *live* disk, so
it competes with the running guest's IO unless we also take a
quick LV snapshot to read from.

The clean combination is bitmap-decides-what + snapshot-provides-
read-source: take a fast LV snapshot at backup time, query QEMU's
bitmap for the dirty list, read those offsets from the snapshot
(no contention), ship to PBS, drop the snapshot, clear the bitmap.
Best of both worlds; no persistent reference snapshot eating space.

**Mechanism 3 — `dm-era`.**

Continuous dirty-block tracking at the device-mapper layer. Adds
an extra dm device above the thin LV; tracks "eras" of writes;
ask "what changed since era N?" at any time. Works for any block
device (no QEMU dependency). Bitmap size is metadata-only.

Less battle-tested than thin_delta or QEMU bitmaps in production
backup pipelines. Mostly interesting if a non-VM workload ever
needs CBT. Skip for v1.

#### Adaptive selection — which one does mgmt use?

Per-VM, mgmt picks at backup time. Pseudo-logic:

```
  decide_cbt_method(vm):
      if vm is offline:
          # bitmap is gone (or stale across stop/start); fall back
          return "thin_delta if reference exists else FULL"

      if qemu_bitmap_supported(vm) and bitmap_is_clean(vm):
          # QEMU has been tracking continuously since last clear
          return "qemu_bitmap"

      if reference_snapshot_exists(vm):
          ref_div = lv_thin_used(reference_snapshot)
          source_size = lv_size(vm.disk0)
          if ref_div > 0.5 * source_size:
              # snapshot has diverged so much that CBT savings
              # are gone; do full + reset reference.
              return "FULL_RESET"
          return "thin_delta"

      return "FULL"   # creates the next reference
```

The 50% threshold is the tunable switchover. Below it, CBT-by-
snapshot saves IO; above it, we're paying snapshot storage for no
read savings, and a full re-read is cheaper.

#### How does PBS know which method we used?

It doesn't, and it doesn't need to. **PBS receives data; we choose
how to send it.** Specifically:

- For full backup: stream the snapshot block device end-to-end.
  PBS chunks at 4 MiB, hashes, dedups. Most chunks already exist
  on PBS from the previous backup → only new chunks transfer.
- For CBT (any flavor): we have a list of changed-block offsets.
  We don't try to re-shape that into something PBS understands
  natively. Instead:
  - **Easiest path:** stream the *whole* snapshot to PBS as a
    full backup. PBS's chunk-dedup already takes care of the
    storage-and-wire side. CBT only saves us *local* read IO and
    chunk-hashing CPU. So CBT becomes "tell proxmox-backup-client
    to skip these byte ranges (read as zeros), don't bother PBS
    about it" — except that breaks dedup because zeros at the
    wrong offsets produce different chunk hashes than the real
    content did.
  - **Correct path:** maintain our own reference image (the result
    of last backup) and patch it with changed blocks; PBS sees
    a normal full image with mostly-unchanged chunks. The chunks
    we didn't read are filled from the local previous reference
    snapshot, which is the *same content* PBS already has.
    Everything dedups.

  Concretely, when CBT is on:
  ```
    ① take snap-N+1
    ② thin_delta snap-N snap-N+1 → changed-block list
    ③ for each chunk:
          if chunk overlaps any changed block:
              read from snap-N+1 (the new content)
          else:
              read from snap-N      (the unchanged content; what PBS
                                      has already)
       (or with QEMU bitmap, replace ② and the if-overlap test
        with the bitmap query result)
    ④ stream chunks to proxmox-backup-client as one image
    ⑤ PBS chunk-hashes; the unchanged chunks are byte-identical
       to last time → dedup hit; only the changed chunks transfer.
    ⑥ rotate references: lvremove snap-N; snap-N+1 becomes next ref.
  ```

  PBS sees a full image arriving every backup; from PBS's perspective
  there are no "incremental backups" — just full backups that happen
  to dedup well. That's fine; it's exactly how PBS is designed to be
  used. We get the local-IO savings; PBS handles the wire and the
  storage.

The tag we attach to each backup_completed log entry includes the
method used (`full` / `thin_delta` / `qemu_bitmap`) so the operator
can see what happened at any point in history.

**Storage-cost summary for daily backups (1 TB VM, 50 GB daily change):**

| Method | Local read/night | Wire/night | Extra disk used | Notes |
|---|---|---|---|---|
| FULL (v1.0 default) | 1 TB | ~50 GB | 0 | snapshot lives only during backup |
| `thin_delta` (v1.1) | ~50 GB | ~50 GB | 50 GB (reference snap) | breaks down when working set is large + random |
| QEMU bitmap (v1.2) | ~50 GB | ~50 GB | ~2 MB sidecar | needs continuous QEMU; falls back to FULL after a crash |

For the user's "DB redo log getting hammered" example: thin_delta
works great (writes hit the same blocks; divergence stays tiny).
For "DB working set with random updates across many blocks":
adaptive logic catches the case and falls back to FULL.

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

## 9b. Deploying PBS — licensing and where it lives

Proxmox Backup Server is **AGPLv3** (server, client, web UI). No
commercial restrictions on use; the optional enterprise subscription
buys access to the stable repo and support, nothing else gates use.
You can run PBS anywhere, including as a VM on the very Bedrock
cluster it's backing up.

Three deployment shapes, in increasing safety:

**Shape A — PBS off-cluster (recommended for prod).**
A small dedicated host: a NUC, a Raspberry Pi 4 with a USB SSD,
or a 1U appliance. PBS is light: ~1 GB RAM idle, single-digit CPU
%, storage = however much backup history you keep. The backup
data sits *outside* Bedrock, so a Bedrock-cluster disaster doesn't
also destroy the backups. This is what you want.

**Shape B — PBS as a pet VM on Bedrock with datastore on external
storage.**
The PBS VM itself runs on Bedrock (HA via DRBD); but its datastore
disk is mounted from a separate NAS / S3 / NFS share / external
disk. VM gets HA; backups stay independent of Bedrock storage.
Acceptable production pattern when "deploy a separate host" isn't
practical.

**Shape C — PBS as a pet VM with datastore on Bedrock.**
Everything lives on Bedrock. Cheap and easy; backups recover from
*guest-level* mistakes (someone deleted the wrong file, malware,
config rollback) but not from *cluster-level* failure (storage
corruption, node loss before backup ships elsewhere). OK for
dev/test/lab; **not** OK as your only backup story for production.

Mixed-mode is fine: shape B locally for fast restores + shape A
offsite for DR. PBS supports `proxmox-backup-client sync` between
two PBS instances, so two-tier topologies are first-class.

In all three cases the install on a Bedrock node is just:

```
  apt install proxmox-backup-client
```

(or grab the static binary from PBS's repo if not on Debian-derived).

---

## 9c. Recovery flow

The reverse of backup. PBS streams chunks; we provide an LV-thin
target; bytes land on the LV. Then we define the libvirt VM and
log it.

**Cattle (single LV, single node):**

```
  bedrock vm restore my-cattle --from pbs://backup.example/store-1/<backup-id>
        │
        ▼
   ① mgmt fetches backup metadata (disk count, sizes, original config).
   ② lvcreate -V <size>G --thin -n vm-my-cattle-disk0 almalinux/thinpool
   ③ proxmox-backup-client restore <backup-id> my-cattle-disk0.img \
        --target /dev/almalinux/vm-my-cattle-disk0
        (PBS streams chunks; client writes them to the block device)
   ④ define libvirt VM XML — disk path = /dev/almalinux/vm-my-cattle-disk0
   ⑤ append vm_create_intent + vm_created log entries
   ⑥ (optional) virsh start
```

**Pet / vipet (DRBD-replicated):**

```
   ① mgmt fetches backup metadata.
   ② on the chosen home node (typically the master):
        lvcreate -V <size>G --thin -n vm-my-pet-disk0 bedrock/thinpool
        proxmox-backup-client restore <id> my-pet-disk0.img \
          --target /dev/bedrock/vm-my-pet-disk0
   ③ append a tier-side log entry assigning a fresh DRBD minor + node-ids.
   ④ on peer(s): lvcreate the matching empty LV (same size, --thin).
   ⑤ on home: drbdadm create-md tier-vm-my-pet (using the new resource def);
              drbdadm up; drbdadm primary --force.
              DRBD's initial sync starts, copying our restored content
              to the peer's empty LV.
   ⑥ define libvirt VM XML — disk path = /dev/drbdN.
   ⑦ append vm_create_intent + vm_created.
   ⑧ (optional) virsh start.
```

Initial sync is data-rate × disk size; over a 10 GbE direct cable
~= disk size in seconds-to-minutes for SSDs, longer for spinning
rust. The VM can start as soon as the home side is primary; the
sync continues in the background and DRBD serves reads/writes from
local + degraded-peer the whole time.

**Cross-cluster restore (DR):**

Identical flow on a fresh cluster. The new cluster has its own
LV pool, its own DRBD config, its own log. PBS doesn't care which
cluster it's serving — backups are data-only, no cluster identity
embedded. As long as the new cluster has network reach + credentials
to PBS, `bedrock vm restore` works. This is also how you'd lift-and-
shift a workload from one Bedrock site to another.

**File-level restore** (when backup was file-level rather than
block-level):

`proxmox-backup-client` supports `mount`, `ls`, and partial-tree
restore. For "I deleted one config file" scenarios this is the
right tool — no need to spin up a whole VM. mgmt exposes a
`bedrock vm restore-files` verb for this.

**Restore time vs read-cost:**
- Restore is always a full read on PBS's side (it has to send all
  the chunks). The dedup that made backup fast doesn't apply to
  restore — you still need every chunk to materialise the disk.
- Local network is the bottleneck; gigabit ≈ 100 MB/s wire ≈ 360
  GB/h. Plan capacity accordingly.

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
  v1.0 (FULL): always works, no extra storage cost.
     lvcreate --snapshot   → PBS reads → lvremove
     [snapshot exists for ~minutes]

  v1.1 (thin_delta CBT): adaptive — falls back to FULL when the
                         reference snapshot has diverged > ~50%.
     lvcreate --snapshot snap-N+1
     thin_delta snap-N snap-N+1                 → changed ranges
     compose a chunk stream:
       changed regions read from snap-N+1
       unchanged regions read from snap-N        (= what PBS already has)
     stream as a full image to proxmox-backup-client
     PBS dedups the unchanged chunks; only changed chunks transfer.
     lvremove snap-N; keep snap-N+1 as next reference.

  v1.2 (QEMU bitmap): no extra storage; needs continuous QEMU.
     lvcreate --snapshot snap-N+1                (frozen read source)
     qmp query bitmap → changed offsets
     compose chunk stream as above (using a small "previous content"
       cache OR the prior backup's PBS-side image as the unchanged
       source)
     stream as full image; PBS dedups.
     qmp clear bitmap; lvremove snap-N+1.
```

The right pattern depends on the VM's write profile, and **mgmt
picks per-VM** with the adaptive logic in §6c-bis. PBS doesn't
care — it always sees full images; the dedup engine takes care
of the rest. Thin-pool fill alarms keep the storage cost of any
kept reference snapshots visible.
