"""Node-local VM backup saga.

A backup runs as a saga on the VM's HOME node — the node holding its
DRBD-primary disks. The mgmt master submits a `vm_backup` operation with
`target_node=<home>`; that node's `operations_drain` (mgmt/orchestrator.py)
picks it up and runs this saga locally. The home node freezes the guest,
LVM-snapshots its own backing LVs, and streams them to the kopia target
locally — rqlite is the only cross-node channel, no SSH from the master.

ctx (from the operation params):
  - target_id: backup target id (a row in `backup_targets`)
  - vm_name:   VM to back up
  - label:     optional snapshot label (defaults to a timestamp)
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "installer"))

from bedrock_d.orchestrator.sagas import saga, step  # noqa: E402

log = logging.getLogger(__name__)


@saga("vm_backup")
class VmBackup:
    """Back up every disk of a VM to its kopia target plus a portable
    metadata snapshot. One step: the work is a single freeze→snapshot→
    stream→cleanup→record cycle, and kopia is itself incremental and
    resumable, so re-running the saga simply re-snapshots (cheap, dedup)."""

    @step("backup")
    def step_backup(self, ctx):
        target_id = ctx["target_id"]
        vm_name = ctx["vm_name"]
        label = ctx.get("label", "")
        # mgmt.backup is importable in the bedrock-d process (it runs the
        # mgmt app too). run_backup resolves the home node and, because
        # this saga runs ON the home node, executes lvcreate/kopia LOCALLY
        # (mgmt.backup._ssh runs local when the host is this node).
        from mgmt import backup as _bk
        res = _bk.run_backup(target_id, vm_name, label=label)
        ctx["result"] = {
            k: res.get(k) for k in
            ("kopia_snapshot_id", "metadata_kopia_id", "bytes_added",
             "duration_s", "fs_freeze_used")
        }
        log.info("vm_backup[%s]: %d disk(s) backed up, metadata=%s, "
                 "%d bytes added",
                 vm_name, len(res.get("disks", [])),
                 res.get("metadata_kopia_id") or "—",
                 res.get("bytes_added", 0))

    @step("migrate_to_secondaries")
    def step_migrate(self, ctx):
        # Per-VM replication: copy the just-written VM backup to any configured
        # SECONDARY targets via `kopia snapshot migrate` — the single
        # replication path (sync-to is gone). Only THIS VM's sources are
        # migrated; each secondary is independent + idempotent (a retry tops up
        # only what's new). The list is resolved at submit time and carried in
        # params (durable across resume); empty means single-target. Declared
        # AFTER step_backup so it runs second (steps run in source order).
        secondaries = ctx.get("secondary_target_ids") or []
        if not secondaries:
            return
        primary = ctx["target_id"]
        vm_name = ctx["vm_name"]
        from mgmt import backup as _bk
        res = _bk.run_migrate_to_secondaries(primary, secondaries, vm_name=vm_name)
        ok = res.get("ok", [])
        failed = res.get("failed", [])
        log.info("vm_backup[%s]: replicated to %d/%d secondary target(s) via "
                 "migrate (%d source(s))%s",
                 vm_name, len(ok), len(secondaries), len(res.get("sources", [])),
                 "" if not failed
                 else f"; FAILED: {[f['target'] for f in failed]}")
        if failed:
            # Fail LOUD, but make it unmistakable that the PRIMARY backup
            # SUCCEEDED and is restorable — only the secondary copy(ies) failed.
            # The op is marked failed so the operator sees it; migrate is
            # idempotent, so retrying the op safely re-attempts only the
            # replication (the 'backup' step is done + skipped on retry).
            names = ", ".join(
                f"{f['target']} ({(f['reason'] or '')[:120]})" for f in failed)
            raise RuntimeError(
                f"primary backup of {vm_name!r} to {primary!r} SUCCEEDED and "
                f"is restorable; migrate to {len(failed)} secondary "
                f"target(s) FAILED: {names}. Retry this operation to "
                f"re-replicate (safe — the primary backup is not re-run; "
                f"migrate is idempotent).")


@saga("vm_restore")
class VmRestore:
    """Restore a VM from a kopia backup and bring it back up — HA on DRBD
    for pet/vipet. Runs on the VM's home node: power off, restore each
    disk through the DRBD primary (so the bytes replicate to peers), and
    start the VM again."""

    @step("restore")
    def step_restore(self, ctx):
        target_id = ctx["target_id"]
        vm_name = ctx["vm_name"]
        kopia_id = ctx.get("kopia_snapshot_id", "")
        from mgmt import backup as _bk
        res = _bk.run_restore_to_ha(target_id, vm_name,
                                    kopia_snapshot_id=kopia_id)
        ctx["result"] = {
            "started": res.get("started"),
            "disks": len(res.get("disks", [])),
            "home_node": res.get("home_node"),
        }
        log.info("vm_restore[%s]: restored %d disk(s), started=%s",
                 vm_name, len(res.get("disks", [])), res.get("started"))
