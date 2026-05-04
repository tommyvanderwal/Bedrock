# Configure a backup target

Sets (or rotates the credentials of) the cluster's kopia repository.
The repo lives on operator-provided storage — S3 / S3-compatible
(Wasabi, B2, R2, MinIO, QNAP-S3, …) or a filesystem path. Every node
ends up connected to the same repo so any node can take backups of
its locally-resident VMs.

**Triggered by:**

- Dashboard: `/backups` page → fill in the form, click `Save target`
- HTTP: `POST /api/backup/targets` with the body documented below

**Source:** `mgmt/app.py:api_backup_target_set`,
`mgmt/backup.py:configure_target_locally`,
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
  "s3_access_key": "...",
  "s3_secret_key": "...",
  "encryption_password": "...",
  "force_password_overwrite": false,
  "reason": "operator note"
}
```

`s3_*` fields are only used for `kind=kopia-s3`. `filesystem_path` is
only used for `kind=kopia-fs`. The three credential fields
(`s3_access_key`, `s3_secret_key`, `encryption_password`) are
**never** persisted to the cluster log; they get propagated as files
to every node and the log only records connection metadata.

## Preconditions

- Caller is on the mgmt master (other nodes don't append log entries).
- bedrock-rust IPC socket reachable at `/run/bedrock-rust.sock`.
- Passwordless `root@<peer>` SSH from the master (mesh from join).
- For new repos: target storage exists and the access keys can write
  to it. For existing repos: the encryption password matches.

## Sequence

```
  T=0    POST /api/backup/targets  {body}
         │
         │ ── (1) propagate secrets if supplied ──
         │
         │ encryption_password + force_password_overwrite=false
         │   AND /etc/bedrock/backup.key already exists
         │   → 400 Bad Request (refuses silent password rotation)
         │
         │ encryption_password supplied → SFTP push
         │   /etc/bedrock/backup.key, mode 0600, every node
         │   (paramiko sftp.posix_rename for atomic replace)
         │
         │ s3_access_key + s3_secret_key supplied → SFTP push
         │   /etc/bedrock/backup-credentials/<target_id>.env
         │   (KOPIA_S3_ACCESS_KEY, KOPIA_S3_SECRET_KEY,
         │    AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY)
         │
         │ Each node failure → recorded in `warnings[]` list,
         │ does NOT abort the call (operator can retry).
         │
  T+0.5  ── (2) connect this node + verify hash floor ──
         │
         │ configure_target_locally(...) on master:
         │   - ensure /etc/bedrock/kopia/ + /var/cache/bedrock-kopia/
         │   - kopia --config-file=… repository connect <kind> …
         │       (passes --disable-tls / --disable-tls-verification
         │        when set; reads creds via per-target .env)
         │   - if connect reports "repository not initialized":
         │       kopia repository create <kind> with
         │       --block-hash=BLAKE2B-256
         │       --encryption=AES256-GCM-HMAC-SHA256
         │     (race-safe: another node creating concurrently
         │      → fall back to connect)
         │   - kopia repository status --json → parse hash field;
         │     reject if hash NOT in
         │     {HMAC-SHA256, HMAC-SHA3-256, BLAKE2B-256,
         │      BLAKE2S-256, BLAKE3-256}
         │     (≥256-bit content-addressing floor — see
         │      docs/snapshots-and-backup.md §9c-bis)
         │
         │ Failure here → 400 Bad Request with kopia error message.
         │
  T+1.5  ── (3) record in cluster log ──
         │
         │ bedrock-rust IPC: append BACKUP_TARGET_SET entry
         │   {target_id, kind, s3_endpoint, s3_bucket, s3_region,
         │    s3_disable_tls, s3_disable_tls_verification,
         │    filesystem_path, override_source_prefix,
         │    cache_directory, reason}
         │
         │ (no secrets in payload)
         │
  T+1.6  Return 200 {
         │   "status": "ok",
         │   "log_index": <N>,
         │   "target_id": <id>,
         │   "warnings": [...]
         │ }
         │
  (async) Every other node's mgmt subscriber sees the new entry,
          folds it into cluster.json, and the orchestrator's
          reactor (`_react_backup_target_set`) runs
          configure_target_locally on that node. Boot-time
          reconcile in `_start_local_services` covers the same
          targets at mgmt restart.
```

## Why this exact order

1. **Secrets first, then connect, then log.** Connecting needs the
   password file; appending the log entry needs the connect to have
   succeeded (otherwise peers' reactors will fail too — at least the
   master must be known-good before broadcasting).
2. **Password protection.** Overwriting `/etc/bedrock/backup.key`
   makes existing snapshots unreadable. The default refuses overwrite
   unless `force_password_overwrite=true` — a deliberate destructive
   action.
3. **Hash floor verified at every connect.** Even on connecting to a
   pre-existing repo (created out-of-band by an older bedrock or by
   the operator's own kopia CLI), bedrock refuses repos using
   <256-bit content hashes. See
   `mgmt/backup.py:ALLOWED_BLOCK_HASHES`.
4. **`posix_rename` over `rename`.** Plain `sftp.rename()` (per the
   SFTP spec) refuses to overwrite an existing target — every secret
   update past the first would silently fail with "Failure". POSIX
   rename is atomic-replace.

## Failure modes and recovery

| Symptom | Cause | Recovery |
|---|---|---|
| `400 missing /etc/bedrock/backup.key …` | First-time setup; no password file yet | Submit again with `encryption_password` in the body. |
| `400 missing /etc/bedrock/backup-credentials/<id>.env …` | First-time setup for an S3 target | Submit with `s3_access_key` + `s3_secret_key`. |
| `400 encryption_password supplied but … already exists` | Trying to silently rotate the kopia password | Either omit `encryption_password` (keep current) or pass `force_password_overwrite=true` (destroys access to existing backups). |
| `400 backup target uses block hash X which is not in …allow-list` | Repo was created with `--block-hash=HMAC-SHA256-128` or similar | Rebuild the repo: `kopia repository create … --block-hash=BLAKE2B-256` from a fresh empty bucket. Bedrock refuses by design — see `lesson_kopia_e2e_setup.md`. |
| `400 kopia connect failed: …InvalidAccessKeyId…` | Wrong S3 access key | Verify creds in the storage admin UI; resubmit. |
| `200` with `warnings: ["S3 credentials not deployed to: <node>(Failure)"]` | Master can't SFTP to a peer; usually missing root SSH key in peer authorized_keys | Fix the SSH mesh, then resubmit. The cluster-log entry already fired so peers' reactors will retry on next mgmt restart via boot reconcile. |
| Peers stay unconnected after a `200 ok` | Their reactors run only when `_SERVICES_STARTED=True`; if they were catching up the log at submit time the entry was folded but reactor was a no-op | `systemctl restart bedrock-mgmt` on the affected peer triggers `_start_local_services` reconcile. |

## Operator perspective

- **Typical duration**: 1–3 s. SFTP push to N peers ≈ 100 ms each;
  kopia connect/create against S3 ≈ 1 s; log append ≈ 50 ms.
- The same form re-submitted with no credential fields just
  re-confirms the existing target — useful to nudge peers to retry
  their reactor connect.
- `GET /api/backup/credentials/status` shows per-node which secrets
  are present on disk; the dashboard's `/backups` page renders this
  as a green "installed" / yellow "missing" pill per node.
- Removing a target: `DELETE /api/backup/targets/{id}` appends
  `BACKUP_TARGET_REMOVED`; the data in the kopia repo stays — only
  bedrock stops pointing at it.
