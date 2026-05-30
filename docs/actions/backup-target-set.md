# Configure a backup target

Sets (or rotates the credentials of) the cluster's kopia repository.
The repo lives on operator-provided storage — S3 / S3-compatible
(Wasabi, B2, R2, MinIO, QNAP-S3, …) or a filesystem path. Every node
connects to the same repo, so any node can back up its locally-resident
VMs.

**Triggered by:**

- Dashboard: `/backups` page → fill in the form, click `Save target`
- HTTP: `POST /api/backup/targets` with the body below

**Source:** `mgmt/app.py:api_backup_target_set`,
`mgmt/backup.py:configure_target_locally`,
`installer/lib/bedrock_state.py:backup_target_set`,
`mgmt/orchestrator.py:_react_backup_target_set`.

## Request body

```json
{
  "target_id": "main",
  "kind": "kopia-s3" | "kopia-fs",
  "s3_endpoint": "host:port",
  "s3_bucket": "bucket-name",
  "s3_region": "us-east-1",
  "s3_disable_tls": false,
  "s3_disable_tls_verification": false,
  "filesystem_path": "/mnt/nas/path",
  "override_source_prefix": "",
  "cache_directory": "",
  "s3_access_key": "...",
  "s3_secret_key": "...",
  "encryption_password": "...",
  "force_password_overwrite": false,
  "is_mirror": false,
  "sync_to": ["mirror-b2"],
  "delete_orphans": false,
  "reason": "operator note"
}
```

- `s3_*` fields apply only to `kind=kopia-s3`; `filesystem_path` only to
  `kind=kopia-fs`.
- `is_mirror` / `sync_to` / `delete_orphans` configure multi-target
  replication — see [Multi-target replication](#multi-target-replication-mirrors)
  below.
- `override_source_prefix` defaults to `<cluster_uuid>:vms`;
  `cache_directory` defaults to `/var/cache/bedrock-kopia/<target_id>`.
- The three credential fields (`s3_access_key`, `s3_secret_key`,
  `encryption_password`) are **never** written to rqlite. They are pushed
  as files to every node; rqlite records only connection metadata.

## Preconditions

- rqlite reachable with a leader (the master must commit the row).
- Passwordless `root@<peer>` SSH from the master (the mesh key set up at
  join), so secret files can be SFTP'd to peers.
- New repo: target storage exists and the access keys can write to it.
  Existing repo: the encryption password matches.

## Sequence

```
  T=0    POST /api/backup/targets  {body}
         │
         │ ── (1) propagate secrets if supplied ──
         │
         │ encryption_password + force_password_overwrite=false
         │   AND /etc/bedrock/backup.key already exists locally
         │   → 400 (refuses silent password rotation)
         │
         │ encryption_password supplied → write
         │   /etc/bedrock/backup.key, mode 0600, on every node
         │   (local node direct; peers via SFTP posix_rename)
         │
         │ kind=kopia-s3 + s3_access_key + s3_secret_key → write
         │   /etc/bedrock/backup-credentials/<target_id>.env, 0600
         │   (KOPIA_S3_ACCESS_KEY, KOPIA_S3_SECRET_KEY,
         │    AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY)
         │   (both S3 keys must be supplied together, else 400)
         │
         │ Per-node push failure → appended to warnings[],
         │   does NOT abort the call (operator can retry).
         │
  T+0.5  ── (2) configure_target_locally on master ──
         │
         │ requires /etc/bedrock/backup.key to exist;
         │   for kopia-s3 also requires the <target_id>.env
         │   (kopia-fs needs only the key + a writable dir)
         │ ensure /var/cache/bedrock-kopia/<target_id>/
         │ kopia repository connect <s3|filesystem> …
         │   (adds --disable-tls / --disable-tls-verification
         │    when set; reads creds from the per-target .env)
         │ if connect reports "repository not initialized":
         │   kopia repository create with
         │     --block-hash=BLAKE2B-256
         │     --encryption=AES256-GCM-HMAC-SHA256
         │   (race-safe: a peer creating concurrently
         │    → fall back to a second connect)
         │ kopia repository status --json → parse hash field;
         │   reject if NOT in
         │   {HMAC-SHA256, HMAC-SHA3-256, BLAKE2B-256,
         │    BLAKE2S-256, BLAKE3-256}
         │   (≥256-bit content-addressing floor;
         │    see mgmt/backup.py:ALLOWED_BLOCK_HASHES)
         │
         │ Any failure here → 400
         │   "backup target setup failed locally: <kopia error>"
         │
  T+1.5  ── (3) record in rqlite ──
         │
         │ bedrock_state.backup_target_set(...) → INSERT/UPSERT into
         │   backup_targets, bumps bedrock_meta.revision
         │   {target_id, kind, s3_endpoint, s3_bucket, s3_region,
         │    s3_disable_tls, s3_disable_tls_verification,
         │    filesystem_path, override_source_prefix, cache_directory}
         │   (no secrets)
         │ rqlite write failure here → 500
         │
  T+1.6  Return 200 {
         │   "status": "ok",
         │   "revision": <N>,
         │   "target_id": <id>,
         │   "warnings": [...]
         │ }
         │
  (async) Every other node's rqlite_subscriber sees the revision
          advance and the orchestrator's reactor
          (`_reactor_diff` → `_react_backup_target_set`) runs
          configure_target_locally for the new/changed target.
          Boot-time reconcile in `_start_local_services` re-connects
          every target on bedrock-d restart (idempotent).
```

## Why this order

1. **Secrets first, then connect, then record.** Connecting needs the
   password file and (for S3) the per-target creds. The rqlite row is
   written only after the master's own connect succeeds — so the master
   is known-good before peers' reactors fire on the revision.
2. **Password protection.** Overwriting `/etc/bedrock/backup.key` makes
   existing snapshots unreadable, so the default refuses to overwrite
   unless `force_password_overwrite=true`.
3. **Hash floor at every connect.** Even when connecting to a repo
   created out-of-band, bedrock refuses repos using <256-bit content
   hashes — `_verify_repo_block_hash` checks `kopia repository status`.
4. **`posix_rename` for peer pushes.** Plain `sftp.rename()` refuses to
   overwrite an existing file (per the SFTP spec), so every credential
   update past the first would fail with a bare "Failure". The
   `posix-rename@openssh.com` extension is atomic-replace.

## Failure modes and recovery

| Symptom | Cause | Recovery |
|---|---|---|
| `400 …missing /etc/bedrock/backup.key…` | No encryption key on the master yet | Resubmit with `encryption_password` in the body. |
| `400 …missing …backup-credentials/<id>.env…` | First-time S3 target, no creds file | Resubmit with `s3_access_key` + `s3_secret_key` (kopia-fs targets don't need this). |
| `400 …encryption_password supplied but …backup.key already exists` | Trying to silently rotate the kopia password | Omit `encryption_password` to keep the current key, or pass `force_password_overwrite=true` (destroys access to existing backups). |
| `400 s3_access_key and s3_secret_key must be supplied together` | Only one S3 key given | Supply both, or neither (to reuse the file already on disk). |
| `400 …block hash X …not in …allow-list` | Repo uses a <256-bit hash (e.g. `HMAC-SHA256-128`) | Rebuild from a fresh empty bucket: `kopia repository create … --block-hash=BLAKE2B-256`. Refused by design. |
| `400 …connect timed out after 30s…` | Endpoint unreachable or refusing connections | Check the endpoint URL, network path, and (S3) key permissions; resubmit. |
| `400 …kopia connect failed: …InvalidAccessKeyId…` | Wrong S3 access key | Verify creds in the storage admin UI; resubmit. |
| `200` with `warnings: ["S3 credentials not deployed to: <node>(…)"]` | Master can't SFTP to a peer (usually missing root key in the peer's authorized_keys) | Fix the SSH mesh, then resubmit. The rqlite row already landed, so the peer's reactor / boot reconcile retries. |
| Peers stay unconnected after `200 ok` | A peer's reactor runs only once `_SERVICES_STARTED=True`; if it was still catching up at submit time, the diff was applied but the connect was skipped | `systemctl restart bedrock-d` on that peer to trigger `_start_local_services` reconcile. |

## Multi-target replication (mirrors)

Back up once to a **primary** target, then fan the same snapshots out to one or
more **mirror** targets (a second region, an on-prem copy) via
`kopia repository sync-to`. The backup runs once on the VM's home node and the
mirror copy follows it — you don't pay the VM-read twice.

A **mirror** is a target created with `is_mirror: true`. It is special:

- It is **never independently initialized** — you do *not* run a normal
  `configure` against it. The **first** `sync-to` from its primary copies the
  primary's repo *format block* into the (empty) destination. A mirror you
  `kopia repository create`d yourself would have an incompatible format and
  every sync would fail "incompatible data" forever. So: create the mirror
  destination's *storage* (an empty bucket / dir), then add it with
  `is_mirror: true` and **no** encryption password — the format comes from the
  primary.
- It must belong to **exactly one** primary. The API refuses a `sync_to` that
  points at a mirror already owned by another primary — two primaries would push
  incompatible formats and, with `delete_orphans`, `--delete`-prune each other's
  blobs (data loss).

On a normal target you then set `sync_to: ["<mirror_id>", …]`. After each backup
of a VM on that target, the home node runs `kopia repository sync-to` to each
listed mirror. `delete_orphans: true` adds `--delete` so the mirror prunes
blobs no longer in the primary (a true mirror); leave it `false` to let the
mirror accumulate (append-only safety copy).

Mirror credentials follow the same rule as any target: a `kopia-s3` mirror needs
its own `/etc/bedrock/backup-credentials/<mirror_id>.env` (the sync uses the
destination's keys, not the primary's). Configuring a `kopia-s3` mirror whose
`.env` is missing is refused — otherwise the sync would silently fall back to
the primary's identity.

**Fail-loud, non-masking:** if the primary backup succeeds but a mirror sync
fails, the operation reports the failure (the VM **is** safely backed up to the
primary and restorable; only the secondary copy lagged — retry is safe). A
mirror is a copy, never the only copy.

Dashboard: on `/backups`, an `is_mirror` checkbox marks a target as a mirror
destination, and a primary target's form has a `sync_to` multi-select of the
configured mirrors.

## Operator perspective

- **Typical duration**: 1–3 s. SFTP push to N peers ≈ 100 ms each; kopia
  connect/create against S3 ≈ 1 s; rqlite write ≈ 50 ms.
- Re-submitting the same form with no credential fields just re-confirms
  the existing target — useful to nudge peers to retry their connect.
- `GET /api/backup/credentials/status` reports, per node, whether the
  password file and each target's `.env` are on disk. The `/backups`
  page renders this as a green `installed` / yellow `missing` pill.
- Removing a target: `DELETE /api/backup/targets/{target_id}` deletes the
  `backup_targets` row and bumps the revision. The kopia repo data stays
  — only bedrock stops pointing at it.
