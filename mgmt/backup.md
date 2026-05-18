# `mgmt/backup.py`

**Module purpose.** Wrap kopia for VM-snapshot backups. mgmt owns
the policy + scheduler; kopia is the data engine. One kopia
repository per cluster (operator-chosen S3-compatible target);
each VM gets per-snapshot identifier
`<cluster-uuid>:vms:<vm-name>` so a multi-cluster operator can
distinguish snapshots from different sites.

## Functions

### Target configuration

- `configure_target_locally(*, target_id, kind, s3_endpoint,
  s3_bucket, s3_region, s3_disable_tls, s3_disable_tls_verification,
  filesystem_path, override_source_prefix, cache_directory, ...)`
  — idempotent `kopia repository connect` for the given target.
  Reads encryption password from `/etc/bedrock/backup.key`
  (mode 0600). Called from the orchestrator's reactor on every
  `backup_target_set` revision.
- `target_status(target_id) -> dict` — `kopia repository status`
  for the connected target.

### Backup operations

- `backup_vm(state, vm_name, target_id, *, fsfreeze=True) -> dict`
  — does the live-snapshot-then-kopia-snapshot dance:
  1. `virsh domfsfreeze <vm>` (optional, requires qemu-guest-agent)
  2. LVM snapshot of the VM disk LV (5% of VM disk size)
  3. `virsh domfsthaw <vm>`
  4. `kopia snapshot create --tags …` on the snapshot mount
  5. lvremove snapshot
  Returns `{snapshot_id, started_at, finished_at, size_bytes}`.
- `restore_vm(state, vm_name, snapshot_id, target_id) -> dict`
  — `kopia restore --copy` into a fresh LV, virsh redefine the
  VM with the new disk.
- `list_snapshots(target_id, vm_name=None) -> list[dict]` —
  `kopia snapshot list` filtered to this cluster's
  `<cluster-uuid>:vms:<vm-name>` tag.
- `delete_snapshot(target_id, snapshot_id) -> dict` —
  `kopia snapshot delete`.

### Scheduler integration

`mgmt/cron.py` cron-parses per-VM backup schedules from the
rqlite `backup_targets` table; `orchestrator.backup_scheduler`
fires `backup_vm` on schedule from the master only.
