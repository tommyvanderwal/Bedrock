# Bedrock — snapshots and backup

Backup is one Kopia repository per cluster. A backup is a transient LVM-thin
snapshot of a VM's disks, streamed into Kopia, then dropped. There is no
long-lived snapshot object: the snapshot exists only for the seconds-to-minutes
of a backup run; the Kopia repo is the durable artefact.

`mgmt/backup.py` orchestrates it; `mgmt/orchestrator.py` reacts to target
config and fires scheduled backups; `installer/lib/bedrock_state.py` records
outcomes in rqlite (`backup_targets`, `vm_backups`, and `last_*` columns on
`vms`).

---

## 1. Storage underneath

Both cattle and pet/vipet disks are **LVM-thin** volumes — the universal
storage primitive on every node:

```
  cattle VM disk             pet/vipet VM disk
  ─────────────────          ─────────────────────────────
  /dev/vda in guest          /dev/vda in guest
       │                            │
       ▼                            ▼
  /dev/<vg>/vm-<name>-disk0   /dev/drbdN        ← DRBD primary (one side)
       │                            │
       ▼                            ▼
  thin pool (per node)       /dev/bedrock/vm-<name>-disk0   ← LV-thin
       │                     + /dev/bedrock/vm-<name>-disk0-meta (DRBD
       ▼                                          activity log; separate LV)
  physical                          │
                                    ▼
                            thin pool (per node) → physical
```

Two properties make LVM-thin snapshots the only snapshot primitive needed:

1. **The data LV under DRBD is byte-identical to what `/dev/drbdN` serves.**
   DRBD writes its bookkeeping to the separate `-meta` LV, not the data LV. A
   snapshot of the data LV is exactly what the guest wrote, with nothing
   DRBD-specific to strip.
2. **Both DRBD peers hold the same acked data** (Protocol C, synchronous), so a
   snapshot on either side reflects the same point-in-time content.

---

## 2. Backup mechanism

A backup runs **on the VM's home node** — the node that holds the disk LV, which
for pet/vipet is the DRBD primary. mgmt resolves the home node from cluster state
(`cluster_state.load_cluster()`, the rqlite `vms.host`), SSHes in as root, and
runs Kopia there. Kopia is one-shot:
it spins up, does the work, exits. mgmt never runs a Kopia daemon.

```
   POST /api/vms/<vm>/backup { target_id, label }   (dashboard / API)
        │
        ▼
   mgmt.backup.run_backup(target_id, vm, label):
   on the home node, in one SSH script (freeze window bounded by one
   round-trip, not SSH-latency × N):
        ① virsh domstate <vm> | grep running
           → if running and qemu-guest-agent answers:
                virsh domfsfreeze <vm>   (guest FS quiesces; in-flight IO
                                          drains; FROZEN_USED=1)
              else: crash-consistent (ext4/xfs replay their journals on
                    next mount — safe; unconfigured DBs less so)
        ② for each disk LV:
                lvcreate --snapshot --name <lv>-bk-<label> <lv-path>
           (one fsfreeze window wraps every lvcreate, so all disks of a
            multi-disk VM are at the same logical instant)
        ③ virsh domfsthaw <vm>           (thaw ASAP; a trap also thaws on
                                          any mid-loop failure)
        ④ lvchange -ay -K each snap LV   (thin snaps are skip-activation
                                          by default)
        │
        ▼
   for each disk:
        dd if=<snap-path> bs=4M | kopia snapshot create <pseudo-path>
          --stdin-file=disk0.img
          --override-source=<prefix>:<vm>:<target_dev>
          --description=<label> --json
        │
        ▼  (always, even on kopia failure)
   lvremove -f every snap LV
        │
        ▼
   bedrock_state.backup_done(...) → vm_backups row + vms.backups
   (or backup_failed → vms.last_backup_error on any exception)
```

`--override-source=<prefix>:<vm>:<target_dev>` (where `<prefix>` defaults to
`<cluster-uuid>:vms`) gives each disk a stable Kopia source line keyed on cluster
UUID + VM name + guest target dev (`vda`, `vdb`, …). VM identity is stable across
migrations and host failures, so a VM's snapshot history never forks when its
home node changes.

**Single-side snapshot.** The snapshot is one LV on one node — the home /
DRBD-primary side. The peer's LV is byte-identical for acked writes, but no
snapshot is taken there. `fsfreeze` quiesces the guest **filesystem**, not the
DRBD pipeline; in-flight writes and wall-clock skew between two `lvcreate`
calls mean two-peer snapshots are not byte-identical without explicit
`drbdsetup suspend-io`/`resume-io` coordination across both peers. Backups
read the one snapshot and ship offsite — the offsite copy is the redundancy.

For cattle the home node is the only node with the disk LV, so "primary side"
and "home node" are the same place.

`cache=none` in the VM's libvirt XML is required: otherwise qemu holds a
writeback cache fsfreeze can't see. The fsfreeze window is microseconds to
tens of milliseconds — no observable guest hang.

---

## 3. Restore

`mgmt.backup.run_restore(target_id, kopia_snapshot_id, vm, …)` writes the
captured bytes straight back to a block device — the VM's LV.

```
   POST /api/vms/<vm>/restore { target_id, kopia_snapshot_id,
                                dest_node?, target_lv_path? }
        │
        ▼
   resolve dest node (default: VM's host from cluster state — where the LV is)
        │
        ▼
   REFUSE if virsh domstate == running        (qemu holds the LV O_RDWR;
                                                a restore dd would race and
                                                corrupt both — the API is the
                                                security boundary, not just the
                                                disabled dashboard button)
        │
        ▼
   build per-disk plan:
     - no target_lv_path: find the vm_backups row containing
       kopia_snapshot_id, restore EVERY disk in that row (one consistent
       rollback). Per disk, resolve the live LV by matching target_dev,
       falling back to the recorded lv_path.
     - target_lv_path given: restore that one snapshot to that one LV.
        │
        ▼  per disk, on the dest node:
   kopia mount <kopia_snapshot_id> <tmp-mnt>   (read-only FUSE)
   wait ≤20 s for <mnt>/disk0.img
   dd if=<mnt>/disk0.img of=<target-lv> bs=4M conv=sparse
   fusermount -u; rmdir
        │
        ▼
   bedrock_state.restore_done(...) → vms.last_restore
   (or restore_failed → vms.last_restore_err)
```

The caller must shut the VM down first; `run_restore` enforces it. The
restored LV is byte-identical to what `run_backup` captured. For pet/vipet
the restore lands on whichever node currently holds the LV; DRBD carries the
written content to peers through normal replication.

**Read-only inspection** without restoring: a backup snapshot LV is just an
LV — `mount -o ro /dev/<vg>/<snap>` reads exactly what DRBD served when it was
taken. DRBD isn't involved; the snapshot is below DRBD in the stack. (Backup
snapshots are dropped at the end of a run, so this applies during a run or to a
manually-created snapshot.)

---

## 4. Backup targets

One Kopia repository per cluster, configured via `POST /api/backup/targets`
(`BackupTargetSetRequest`). Two kinds:

- `kopia-s3` — S3 / S3-compatible (Wasabi, B2, R2, MinIO, QNAP-S3, …).
  Requires `/etc/bedrock/backup-credentials/<target_id>.env` with
  `KOPIA_S3_ACCESS_KEY` / `KOPIA_S3_SECRET_KEY`. `s3_disable_tls` (plain HTTP)
  and `s3_disable_tls_verification` (skip cert check) are explicit opt-ins for
  self-hosted endpoints.
- `kopia-fs` — a filesystem path (e.g. NFS mount). Credentials file optional.

**Secret propagation.** Two secrets live on every node, mode 0600, never in
rqlite:

- `/etc/bedrock/backup.key` — the Kopia repo encryption password.
- `/etc/bedrock/backup-credentials/<target_id>.env` — S3 keys.

When the operator submits secrets inline to `POST /api/backup/targets`, mgmt
fans them out to every node over the root@host SSH mesh, then appends
`BACKUP_TARGET_SET` to the cluster log. A per-node propagation failure is
logged, not fatal — that node fails loudly the first time its reactor runs
`kopia repository connect`; resubmitting the form re-propagates.
`GET /api/backup/credentials/status` reports which nodes have the key + which
`.env` files exist.

**Repo connect / create.** Every node's reactor (`_react_backup_target_set` in
`mgmt/orchestrator.py`) runs `configure_target_locally` on `BACKUP_TARGET_SET`,
so any node can back up its own VMs. It tries `kopia repository connect`; if the
repo is uninitialized it runs `kopia repository create` with the strong-hash
policy, handling the create-race between nodes by reconnecting.

**Content-hash floor: ≥256 bits, no override.** Kopia dedup is content-addressed
— a hash collision means a wrong-blob restore. New repos are created with
`--block-hash=BLAKE2B-256 --encryption=AES256-GCM-HMAC-SHA256`. On connect,
`_verify_repo_block_hash` reads `kopia repository status --json` and refuses any
repo whose block hash isn't in `ALLOWED_BLOCK_HASHES`
(`{HMAC-SHA256, HMAC-SHA3-256, BLAKE2B-256, BLAKE2S-256, BLAKE3-256}`).
Truncated 128-bit variants are rejected. Extend the allow-list in
`mgmt/backup.py:ALLOWED_BLOCK_HASHES` if Kopia adds another ≥256-bit hash;
there is no override flag — fail-loud is the right default for content
addressing.

**Per-node cache.** Each node keeps its own Kopia config + cache under
`/etc/bedrock/kopia/<target_id>.config` and `/var/cache/bedrock-kopia/<target_id>`,
passed explicitly via `--config-file` / `--cache-directory` (the bedrock-mgmt
systemd unit has no `$HOME`). Caches don't share state and don't need to:
chunks are content-addressed and immutable, so cached data never goes stale.
After a VM migrates to a node with a cold cache, the next backup pays one
chunk-existence check per chunk against the repo (over the network instead of
local disk) — minutes of metadata round-trips for a large VM, then warm again.
Dedup is repo-level, so a cold cache never re-uploads unchanged content.

### 4a. Multi-target replication (mirrors)

A primary target can **mirror** to one or more secondary targets. After a VM
backup lands in the primary repo, the VM's home node runs
`kopia repository sync-to <secondary>` for each mirror — a blob-level copy, so
the source is read once and dedup is preserved (it does NOT re-snapshot per
target). Each mirror is synced **independently** (never `&&`-chained) so one
unreachable mirror can't abort the others.

- A mirror destination is a normal `backup_targets` row but flagged
  **`is_mirror`**: it is NEVER independently `kopia repository create`d (that
  gives it an incompatible format block — see lessons-log **L49**). It starts
  EMPTY; the first `sync-to` (no `--must-exist`) copies the PRIMARY's repo
  format + blobs into it, making it a true byte-compatible mirror you can later
  connect to and restore from. All targets share the one cluster backup
  password (`/etc/bedrock/backup.key`), which is exactly what `sync-to` requires.
- The relationship lives in the **`backup_target_sync`** table (composite PK
  `primary_id, secondary_id`, ordered, `delete_orphans` flag). A mirror belongs
  to exactly ONE primary (the API rejects fan-in — two primaries pushing
  incompatible formats and `--delete`-pruning each other's blobs).
- **Fail-loud but non-masking:** a mirror failure marks the backup operation
  FAILED with a message that the PRIMARY backup SUCCEEDED and is restorable;
  retry is safe (`sync-to` is idempotent and the primary step is skipped).
- Set it via the dashboard (Backups → target form: "mirror destination"
  checkbox + "Replicate to mirrors" multi-select) or `POST /api/backup/targets`
  (`is_mirror` / `sync_to` / `delete_orphans`). Mirror S3 creds must be present
  on every node (the sync refuses a kopia-s3 mirror whose `<id>.env` is missing,
  rather than silently using the primary's identity).

---

## 5. Scheduling

`POST /api/vms/<vm>/backup-schedule` stores a per-VM schedule in
`vms.backup_schedule` (JSON: `target_id`, `cron_expr`, `label_prefix`,
`retention_count`, `set_at_index`). The cron expression is a 5-field UTC
expression (`mgmt/cron.py`, a self-contained parser — no `croniter`
dependency); the endpoint validates it and returns the next 5 fire times.

`backup_scheduler()` in `mgmt/orchestrator.py` is a **master-only** 60 s loop
(the master is the single log writer, so scheduling against its view is
naturally serialised — a follower would double-fire or fail its append). Each
tick reads cluster state, and for every VM with a schedule whose cron is due
(`cron.should_fire_now`, 60-minute grace window), fires `run_backup` with an
auto label `<label_prefix>-<UTC-timestamp>`. `_SCHEDULED_INFLIGHT` skips a VM
whose previous run hasn't finished. "Last fired" is reconstructed from the most
recent `BACKUP_DONE` whose label matches the prefix, so master restart doesn't
re-fire everything.

---

## 6. State in rqlite

Cluster state lives in rqlite (Raft-replicated SQLite), written through
`installer/lib/bedrock_state.py`, read through
`installer/lib/cluster_state.load_cluster()`.

- `backup_targets` — one row per configured target (kind, S3/FS fields,
  `override_source_prefix`, `cache_directory`).
- `vm_backups` — one row per completed backup: `vm_name`, `target_id`,
  `source_node`, `disks` (JSON `[{target_dev, lv_path, kopia_snapshot_id,
  bytes_added}, …]`), `primary_kopia_id` (disks[0], denormalised for lookup),
  rolled-up `bytes_added`, `duration_s`, `label`, `fs_freeze_used`, `ts_index`.
- `vms` columns carry the most recent outcomes for the dashboard:
  `backup_schedule`, `last_backup_error`, `last_restore`, `last_restore_err`.

Writer functions: `backup_target_set`, `backup_target_removed`, `backup_done`,
`backup_failed`, `backup_deleted`, `restore_done`, `restore_failed`,
`backup_schedule_set`, `backup_schedule_removed`.

API surface (`mgmt/app.py`): `POST/GET/DELETE /api/backup/targets`,
`GET /api/backup/credentials/status`, `GET /api/backups`,
`POST /api/vms/<vm>/backup`, `GET /api/vms/<vm>/backups`,
`POST /api/vms/<vm>/restore`,
`POST/DELETE /api/vms/<vm>/backup-schedule`,
`DELETE /api/vms/<vm>/backups/<kopia_snapshot_id>`. Backup/restore endpoints
return `202` + a `task_id`; the UI watches `/api/tasks` (or the WS task channel)
for completion.

---

## 7. Storage-cost discipline

Backup snapshots are transient — they exist only during a run — so the steady
state cost is the Kopia repo, not local thin-pool divergence.

- **Thin-pool fill** is exported by `mgmt/vm_exporter.py`
  (`bedrock_thinpool_data_percent` / `_metadata_percent`) and surfaced as a
  dashboard advisory (`mgmt/routes_support.py`) at the 70% / 80% marks. It is an
  advisory signal, not a hard gate on backups.
- **Deletion**: `DELETE /api/vms/<vm>/backups/<kopia_snapshot_id>` runs
  `kopia snapshot delete` and records `BACKUP_DELETED`. The underlying chunks
  are freed by Kopia's own GC.
- `retention_count` is stored on the schedule but is not auto-enforced; prune
  and `kopia maintenance` scheduling are not yet wired.

---

## 8. Why Kopia, why single-binary

- **Light.** One ~50 MB binary on every node, no daemon, no backup VM. It runs
  on the node that owns the disk, reads the snapshot locally, and exits.
- **Store-driven recovery.** The repo is the durable artefact. A fresh Bedrock
  cluster with the repo URL + S3 credentials + the `/etc/bedrock/backup.key`
  encryption password can restore any backup — the encryption key is the one
  out-of-band secret the operator carries across clusters; backups carry no
  cluster identity.
- **Content-addressed dedup + zstd**, native S3 with parallel uploads,
  BLAKE2B-256 content hashing. Repo-level S3 Object Lock for ransomware
  immutability is available to the operator (a Kopia/bucket setting), not
  something Bedrock configures.

The snapshot is the read surface; Kopia is the durable store. LVM-thin under
both cattle and pet means one primitive, one code path, one operator model;
DRBD doesn't fight it because it's a thin layer over the same LV.
