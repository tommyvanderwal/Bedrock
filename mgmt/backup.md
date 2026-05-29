# mgmt/backup.py

Bedrock's VM backup/restore orchestration — a thin wrapper around the one-shot
`kopia` CLI. mgmt runs kopia over SSH on the VM's home node (where the disk LVs
live); kopia spins up, does work, and exits — there is no long-lived kopia
daemon. Operators reach these entry points through the mgmt API / `bedrock
backup` CLI; the mgmt master is the maintenance owner (only it schedules
`kopia maintenance run`). One kopia repository serves the whole cluster
(operator-chosen S3, S3-compatible, or filesystem path), with the encryption
password in `/etc/bedrock/backup.key` (mode 0600) and per-target S3 credentials
in `/etc/bedrock/backup-credentials/<target_id>.env`. VM identity is stable
across migrations because every snapshot uses
`--override-source=<prefix>:<vm>:<target_dev>`. Cluster state is read via
`lib.cluster_state.load_cluster()`; mutations are recorded into rqlite through
`lib.bedrock_state` (`bs`) helpers.

## Functions / Classes

### `configure_target_locally(target_id, kind, *, s3_endpoint='', s3_bucket='', s3_region='', s3_disable_tls=False, s3_disable_tls_verification=False, filesystem_path='', override_source_prefix='', cache_directory='') -> None`
Connect (or create-then-connect) the kopia repo on **this** node so later
backup/restore calls work, then verify its block hash meets the 256-bit floor.
- **In:** `target_id` — per-target identity (names config/cache/credential
  files); `kind` — `"kopia-s3"` or `"kopia-fs"` (anything else raises);
  `s3_*` — S3 backend params + TLS opt-outs; `filesystem_path` — path for
  `kopia-fs`; `cache_directory` — cache override (defaults under
  `/var/cache/bedrock-kopia/<target_id>`).
- **Out:** None. Side effects: creates `/etc/bedrock/kopia/` and the cache dir;
  runs `kopia repository connect` (and possibly `create`) via `bash -lc`
  subprocesses; requires `backup.key` (raises if absent) and, for `kopia-s3`,
  the `<target_id>.env` credentials file (raises if absent). Raises
  `RuntimeError` on connect failure, create failure, connect timeout (30 s), or
  a block hash outside `ALLOWED_BLOCK_HASHES`.

### `run_backup(target_id, vm_name, *, label='') -> dict`
Back up every disk of one VM to a target as one consistent point-in-time.
- **In:** `target_id` — configured backup target; `vm_name` — VM key in cluster
  state; `label` — optional human label (defaults to a `%Y%m%dT%H%M%S` stamp).
- **Out:** `{disks: [{target_dev, lv_path, kopia_snapshot_id, bytes_added}, …],
  duration_s, label, fs_freeze_used, kopia_snapshot_id, bytes_added}` (the last
  two are the first disk's id and the summed bytes). Side effects: SSH to the
  home node to `virsh dumpxml`, optionally `virsh domfsfreeze`/`domfsthaw`,
  `lvcreate`/`lvchange`/`lvremove` snapshot LVs, and `dd | kopia snapshot
  create` per disk; records `bs.backup_done(...)` on success or
  `bs.backup_failed(...)` on failure (rqlite via `bedrock_state`). Raises on any
  step failure (snapshot LVs are always removed first).

### `list_backups_for_vm(vm_name) -> list[dict]`
Backup history for one VM, read from cluster state.
- **In:** `vm_name` — VM key.
- **Out:** the VM record's `backups` list (`[]` if the VM is unknown). No side
  effects.

### `run_restore(target_id, kopia_snapshot_id, vm_name, *, target_lv_path=None, dest_node_name=None) -> dict`
Restore a backup byte-for-byte onto a block device (normally the VM's LV);
caller must have the VM shut down.
- **In:** `target_id` — configured target; `kopia_snapshot_id` — any disk's
  kopia id from the backup row (restores the whole row by default);
  `vm_name` — VM key; `target_lv_path` — optional explicit single-LV override
  (skips row matching); `dest_node_name` — optional override (defaults to the
  VM's `host`, else this node).
- **Out:** `{kopia_snapshot_id, disks: [...], dest_node, duration_s,
  target_lv_path}`. Side effects: SSH to the destination node; **refuses** if
  `virsh domstate` reports `running`; per disk does `kopia mount` →
  `dd if=<mnt>/disk0.img of=<lv> conv=sparse` → `fusermount -u`; records
  `bs.restore_done(...)` / `bs.restore_failed(...)`. Raises if the target/node
  is unknown, the VM is running, no row matches the id, or a disk's LV can't be
  resolved.

### `delete_backup(target_id, kopia_snapshot_id, vm_name, *, reason='') -> None`
Delete one snapshot from the kopia repo and log it.
- **In:** `target_id`, `kopia_snapshot_id`, `vm_name`; `reason` — optional log
  note.
- **Out:** None. Side effects: runs `kopia snapshot delete` over SSH on this
  node (resolves the self node's host, falling back to `127.0.0.1`); records
  `bs.backup_deleted(...)`. Underlying chunks are freed later by the master's
  `kopia maintenance run`. Raises if the target is unknown.

### Private helpers
- `_read_cluster()` / `_vm_record()` / `_target_record()` — read cluster state
  via `cluster_state.load_cluster()` and pick the `vms` / `backup_targets` slot.
- `_override_source_for_vm(vm_name)` — build the `<prefix>:<vm>` override source.
- `_vm_disk_lvs()` / `_vm_disk_target_devs()` / `_parse_disks_from_xml()` —
  `virsh dumpxml` on the home node, return the disk source-LV paths and
  guest-visible target devs (`vda`, `vdb`, …) as two positionally-paired lists,
  block-device disks only.
- `_ssh(host, cmd, check=True, timeout=600)` — run `cmd` as `root@host` over SSH
  (`StrictHostKeyChecking=no`, `BatchMode=yes`, 8 s connect), return stdout,
  raise on non-zero when `check`.
- `_kopia_password_export()` / `_credentials_env()` — bash snippets that export
  `KOPIA_PASSWORD` (from `backup.key`) and source the per-target `.env` (S3
  keys); for `kopia-fs` the `.env` is optional.
- `_kopia_config_file()` / `_kopia_cache_dir()` / `_kopia_global_flags()` /
  `_kopia_cache_flag()` / `_kopia_cache_arg()` / `_kopia_password_arg()` —
  per-target `--config-file` and `--cache-directory` flag plumbing; the config
  flag goes before the subcommand, the cache flag after.
- `_kopia_connect_cmd()` / `_kopia_create_cmd()` / `_kopia_tls_flags()` — build
  the kopia `repository connect` / `create` command strings (create bakes in
  `--block-hash=BLAKE2B-256` + `--encryption=AES256-GCM-HMAC-SHA256`).
- `_verify_repo_block_hash()` — parse `kopia repository status --json`, raise if
  the hash isn't in `ALLOWED_BLOCK_HASHES`; warn-and-accept if no hash field is
  surfaced or status fails.
- `_looks_like_uninitialized()` / `_looks_like_already_initialized()` — classify
  kopia stderr/stdout for the connect→create→connect race path.
- `_parse_kopia_create()` — read the last JSON line of `kopia snapshot create
  --json` → `(snapshot_id, bytes_added)`.
- `_self_node_name()` / `_hostname_to_node_name()` / `_node_for_host()` — small
  node lookups against local `state` and cluster `nodes`.

## How it works

**Module constants.** `ALLOWED_BLOCK_HASHES` is the frozenset of accepted
≥256-bit content hashes (`HMAC-SHA256`, `HMAC-SHA3-256`, `BLAKE2B-256`,
`BLAKE2S-256`, `BLAKE3-256`); `DEFAULT_BLOCK_HASH = BLAKE2B-256` and
`DEFAULT_ENCRYPTION = AES256-GCM-HMAC-SHA256` are used when bedrock creates a
repo. Every kopia invocation passes `--config-file` explicitly (the
`bedrock-mgmt` systemd unit has no `$HOME`, so the default config path would be
ambiguous), and a per-target cache directory.

**Target setup (`configure_target_locally`).** Three steps, with the hash check
as the load-bearing one:

```
connect ──ok──────────────────────────────► verify hash ─► healthy
   │                                              ▲
   └─ "uninitialized"? ─► create ──ok─────────────┘
                            │
                            └─ "already initialized"? (another node raced)
                                     └─► connect again ──► verify hash
```

A successful connect (or "already connected") falls straight through to
`_verify_repo_block_hash`. An uninitialized-repo error triggers `create`; if
create loses a race to a concurrent creator, the code re-connects. Any other
error raises. The hash verifier reads `repository status --json`, tries several
version-dependent field names (`hash`, `blockHash`, `contentHashAlgorithm`, …),
and raises if the hash is outside the allow-list — there is deliberately no
override flag. mgmt's reactor on every node runs this same function on
`BACKUP_TARGET_SET`, so configuring a target on the master propagates to peers.

**Backup (`run_backup`).** Resolve the VM's home node and its disk LVs, then
run the freeze/snapshot phase as a single bash script on the home node so the
quiesce window is bounded by one round-trip, not SSH-latency × N:

```
domstate running? ── yes ─► domfsfreeze ── ok ─► FROZEN_USED=1
                                                 trap EXIT: domfsthaw (safety net)
        │                                            │
        └────────── no / GA absent ──────────────────┤  (crash-consistent)
                                                      ▼
                              lvcreate --snapshot  (× N disks)
                                                      ▼
                              domfsthaw NOW  (before activate; minimise pause)
                                                      ▼
                              lvchange -ay -K  (× N; thin snaps skip-activate)
                                                      ▼
                              echo FS_FREEZE_USED=$FROZEN_USED
```

Then, per disk, `dd if=<snap_path> bs=4M | kopia snapshot create <pseudo_path>
--stdin-file=disk0.img --override-source=<prefix>:<vm>:<target_dev>
--description=<label> --json`, collecting `(snapshot_id, bytes_added)`. The
snapshot LVs are removed in a `finally` so they go away even when kopia fails.
The override-source ties each disk's content-addressed stream to a stable
identity, so dedup works per disk across migrations and renames. On success
`bs.backup_done` records the multi-disk row; any exception records
`bs.backup_failed` and re-raises.

**Restore (`run_restore`).** Resolve the destination node, then refuse if the
VM is `running` (qemu holds the LV `O_RDWR`; a `dd` write would corrupt both).
With no explicit `target_lv_path`, find the backup row containing the given
`kopia_snapshot_id` and restore **every** disk in that row as one unit; each
disk's target LV is taken from the current VM's `target_dev`→LV map (preferred,
survives recreate) or the recorded `lv_path`. Per disk it mounts the snapshot
read-only with `kopia mount` (waits up to 20 s for `disk0.img` to appear),
`dd … conv=sparse` into the LV, then `fusermount -u`. `bs.restore_done` /
`bs.restore_failed` log the outcome.

## Why

The block-hash floor exists because a content-hash collision would make kopia
store the wrong blob under a chunk id and silently corrupt a restore; 256 bits
puts collisions in the "literally never" range, so bedrock refuses any repo
below it rather than trade integrity for a few microseconds per chunk. Secrets
go through `KOPIA_PASSWORD` / sourced env files rather than `--password`/
`--password-file` so they never appear on `/proc/<pid>/cmdline`.
