"""Backup target management: S3/SMB credentials status, add/list/remove backup targets,
list all backups across the cluster."""
from __future__ import annotations
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from dependencies import require_operator
from common import (load_cluster, push_log, ssh_cmd, ssh_cmd_rc, get_nodes,
                    _self_host, _propagate_secret, _render_s3_creds_env, _import_backup_module)

import sys as _sys
_sys.path.insert(0, "/usr/local/lib/bedrock")
from lib import bedrock_state as _bs             # noqa: E402
from lib import cluster_state as _cluster_state  # noqa: E402
router = APIRouter(tags=["backup"])




# ── Backup endpoints ────────────────────────────────────────────────────────
# Kopia orchestration. The mgmt master writes the backup target to rqlite;
# every node's reactor reacts by running `kopia repository connect` locally
# so any node can do backups/restores of its locally-resident VMs.
# See snapshots-and-backup.md §9c-bis.

class BackupTargetSetRequest(BaseModel):
    target_id: str = "main"
    kind: str = "kopia-s3"           # "kopia-s3" | "kopia-fs"
    s3_endpoint: str = ""
    s3_bucket: str = ""
    s3_region: str = ""
    # Self-hosted S3 (QNAP, MinIO with self-signed certs) often needs
    # one of these. Default off — operator opts in per target.
    s3_disable_tls: bool = False              # plain HTTP
    s3_disable_tls_verification: bool = False  # HTTPS, skip cert check
    filesystem_path: str = ""
    override_source_prefix: str = ""  # default: "<cluster_uuid>:vms"
    cache_directory: str = ""         # default: /var/cache/bedrock-kopia
    reason: str = ""
    # ── Multi-target replication ───────────────────────────────────
    # Ordered list of OTHER backup_targets ids this target mirrors to via
    # `kopia repository sync-to` after each backup. Secondaries are normal
    # targets (own endpoint/bucket/creds) sharing the one cluster password.
    # Empty = single-target (the default; no behaviour change).
    sync_to: list[str] = []
    delete_orphans: bool = False      # kopia sync-to --delete (prune mirrors)
    # A mirror target is a sync-to DESTINATION only — registered for its
    # storage config + creds but NEVER independently created (an independent
    # `kopia repository create` gives it an incompatible format block). It
    # starts empty; the first sync-to from its primary copies the source
    # format. Set this when adding a replication destination.
    is_mirror: bool = False
    # ── Credentials (NEVER logged) ─────────────────────────────────
    # Optional inline secrets. When present, mgmt writes the
    # corresponding files on every cluster node before recording the
    # backup target in rqlite. When absent, the operator is expected
    # to have dropped the files manually.
    s3_access_key: Optional[str] = None    # → KOPIA_S3_ACCESS_KEY in env file
    s3_secret_key: Optional[str] = None    # → KOPIA_S3_SECRET_KEY in env file
    # The per-repo kopia password → sealed in rqlite (backup_targets.
    # repo_password_enc), the cluster-internal source of truth. None = unchanged;
    # nodes materialize their own 0600 override from rqlite via the reactor.
    encryption_password: Optional[str] = None
    # If True, overwrite this repo's existing REAL password. Defaults to False —
    # changing it makes existing encrypted backups unreadable, a deliberate
    # destructive action. (Switching off the public default needs no force.)
    force_password_overwrite: bool = False




@router.get("/api/backup/credentials/status")
def api_backup_credentials_status():
    """What secrets exist on each node? UI uses this to decide whether
    to show empty fields (operator must enter creds) vs. "already
    configured" placeholders.

    Returns per-node booleans:
      - has_password: /etc/bedrock/backup.key exists, mode 0600
      - has_creds.<target_id>: corresponding env file exists
    """
    out: dict = {"nodes": {}}
    for name, node in get_nodes().items():
        host = node.get("host", "")
        if not host:
            continue
        info: dict = {"has_password": False, "creds": {}}
        try:
            r = ssh_cmd(host, f"[ -f {BACKUP_KEY_FILE} ] && echo yes || echo no")
            info["has_password"] = (r.strip() == "yes")
            r2 = ssh_cmd(host, f"ls {BACKUP_CRED_DIR}/*.env 2>/dev/null | xargs -n1 basename 2>/dev/null")
            for ln in (r2 or "").splitlines():
                ln = ln.strip()
                if ln.endswith(".env"):
                    info["creds"][ln[:-4]] = True
        except Exception as e:
            info["error"] = str(e)
        out["nodes"][name] = info
    return out




@router.post("/api/backup/targets")
def api_backup_target_set(req: BackupTargetSetRequest):
    """Configure (or update) the cluster's backup target. Idempotent —
    emitting the same target twice produces a single fold result.

    Action sequence:
      1. (Optional) Propagate inline credentials to every node:
           - encryption_password → /etc/bedrock/backup.key
           - s3_access_key/secret → /etc/bedrock/backup-credentials/<id>.env
         Files are written mode 0600. Failure on individual nodes is
         logged but doesn't abort — the affected node will fail loudly
         on its reactor's `kopia repository connect`.
      2. Run `kopia repository connect` (or create) locally on master.
         Verifies the repo's block hash is ≥256 bits.
      3. Write the backup target to rqlite. Every node's reactor reacts
         by running `kopia repository connect` against the new target.
      4. Return the revision so callers know the change is committed.

    Credentials are NEVER persisted to cluster state — only file paths
    and metadata (endpoint, bucket, region) are stored."""
    backup = _import_backup_module()

    propagation_warnings: list[str] = []

    # ── (0) Validate the mirror set UP FRONT, before any writes ──────
    # A bad sync_to must 400 with NO partial commit (no kopia repo created, no
    # target row written). STRONG read so a sibling target created moments ago
    # is visible (a level='none' local replica can lag and falsely reject a
    # valid secondary). Each secondary must EXIST and be is_mirror=true — a
    # non-mirror (independently-created) repo has an incompatible format block
    # and every sync-to into it would fail "incompatible data" forever.
    sync_to = list(req.sync_to or [])
    if req.target_id in sync_to:
        raise HTTPException(
            400, f"a backup target cannot mirror to itself ({req.target_id!r})")
    try:
        strong_targets = _cluster_state.load_cluster(
            level="strong").get("backup_targets", {}) or {}
    except Exception as e:
        raise HTTPException(
            503, f"could not validate sync_to — rqlite strong read failed "
            f"(no leader?): {e}")
    for sid in sync_to:
        t = strong_targets.get(sid)
        if t is None:
            raise HTTPException(
                400, f"sync_to references unknown backup target {sid!r} — "
                f"create it as a mirror (is_mirror=true) first")
        if not t.get("is_mirror"):
            raise HTTPException(
                400, f"sync_to secondary {sid!r} is not a mirror target. Create "
                f"the mirror destination with is_mirror=true — a mirror is never "
                f"independently initialized; the first sync-to copies the "
                f"primary's repo format into it.")
        # A mirror must belong to exactly ONE primary. Two primaries syncing to
        # the same mirror push incompatible repo formats (every sync after the
        # first fails "incompatible data") and, with delete_orphans, their
        # --delete passes would prune each other's blobs (data loss). Reject a
        # secondary already owned by a different primary.
        other = next((pid for pid, pt in strong_targets.items()
                      if pid != req.target_id
                      and sid in (pt.get("sync_to") or [])), None)
        if other is not None:
            raise HTTPException(
                400, f"mirror {sid!r} is already a replication target of "
                f"{other!r}. A mirror can belong to only one primary "
                f"(two primaries would push incompatible formats and "
                f"--delete-prune each other). Use a separate mirror target.")
    # Existing mirror set for this primary (strong, so a clear isn't skipped
    # against a stale replica).
    current_mirrors = (strong_targets.get(req.target_id) or {}).get("sync_to") or []

    # ── (1a) Encryption password → RQLITE (the central, cluster-internal store) ─
    # The repo password is NOT pushed to per-node files here; it is persisted
    # sealed in rqlite (backup_targets.repo_password_enc) in step (3), and each
    # node's reactor materializes its own 0600 override from rqlite. rqlite (mTLS,
    # cluster-internal) is the single source of truth — so a repo's key survives
    # any node loss and is never out of sync across the cluster.
    if req.encryption_password is not None:
        # Only a REAL (non-public-default) password already on THIS repo is
        # protected from overwrite — changing it makes existing encrypted backups
        # unreadable. has_repo_password (from the strong rqlite read) is True only
        # when a real per-repo password is set; the public default is not.
        existing_has_real = bool(
            (strong_targets.get(req.target_id) or {}).get("has_repo_password"))
        if existing_has_real and not req.force_password_overwrite:
            raise HTTPException(
                400,
                "encryption_password supplied but this repo already has a real "
                "password. Changing it makes existing encrypted backups "
                "unreadable. Pass force_password_overwrite=true to confirm — or "
                "omit encryption_password to keep the current key."
            )

    # ── (1b) S3 credentials → RQLITE (the central store), not per-node files ──
    # The secret is sealed in rqlite (backup_targets.s3_secret_key_enc) in step
    # (3); each node materializes its own 0600 .env cache from rqlite via the
    # reactor (and the master directly, below). No _propagate_secret.
    if req.kind == "kopia-s3" and (req.s3_access_key or req.s3_secret_key):
        if not (req.s3_access_key and req.s3_secret_key):
            raise HTTPException(
                400, "s3_access_key and s3_secret_key must be supplied together"
            )

    # ── (2) Connect this node + verify hash floor ──────────────────
    # SKIP for a mirror target: it must stay empty so the first
    # `kopia repository sync-to` can copy the source's format block into it.
    # Independently creating it here would give it an incompatible format
    # and every sync-to would fail "destination contains incompatible data".
    if not req.is_mirror:
        try:
            backup.configure_target_locally(
                target_id=req.target_id, kind=req.kind,
                s3_endpoint=req.s3_endpoint, s3_bucket=req.s3_bucket,
                s3_region=req.s3_region,
                s3_disable_tls=req.s3_disable_tls,
                s3_disable_tls_verification=req.s3_disable_tls_verification,
                filesystem_path=req.filesystem_path,
                override_source_prefix=req.override_source_prefix,
                cache_directory=req.cache_directory,
                # Master's own setup runs BEFORE the rqlite write below, so pass
                # the new password + S3 creds explicitly; None/'' → read rqlite.
                repo_password=req.encryption_password,
                s3_access_key=req.s3_access_key or "",
                s3_secret_key=req.s3_secret_key or "",
            )
        except Exception as e:
            raise HTTPException(400, f"backup target setup failed locally: {e}")

    # ── (3) Persist to rqlite so peers get it via their reactors ──
    try:
        rev = _bs.backup_target_set(
            target_id=req.target_id, kind=req.kind,
            s3_endpoint=req.s3_endpoint, s3_bucket=req.s3_bucket,
            s3_region=req.s3_region,
            s3_disable_tls=req.s3_disable_tls,
            s3_disable_tls_verification=req.s3_disable_tls_verification,
            filesystem_path=req.filesystem_path,
            override_source_prefix=req.override_source_prefix,
            cache_directory=req.cache_directory,
            is_mirror=req.is_mirror,
            # Every secret lands sealed in rqlite here (the source of truth).
            # '' when unchanged → CASE-preserve keeps the stored value.
            repo_password=req.encryption_password or "",
            s3_access_key=req.s3_access_key or "",
            s3_secret_key=req.s3_secret_key or "",
            reason=req.reason,
        )
    except Exception as e:
        raise HTTPException(500, f"rqlite write failed: {e}")

    # ── (3b) Persist the mirror set (already validated in step 0). Write only
    # when setting OR clearing, so single-target sets don't churn the table.
    if sync_to or current_mirrors:
        try:
            rev = _bs.backup_target_sync_set(
                req.target_id, sync_to,
                delete_orphans=req.delete_orphans, reason=req.reason,
            )
        except Exception as e:
            raise HTTPException(500, f"rqlite write (mirror set) failed: {e}")

    push_log(f"backup target {req.target_id!r} set ({req.kind})"
             + (f" → mirrors {sync_to}" if sync_to else ""),
             app="bedrock-mgmt", level="info")
    return {
        "status": "ok",
        "revision": rev,
        "target_id": req.target_id,
        "sync_to": list(req.sync_to or []),
        "warnings": propagation_warnings,
    }




@router.get("/api/backup/targets")
def api_backup_targets_list():
    """List configured backup targets, drawn from cluster state.
    Always returns immediately — no kopia roundtrip."""
    cluster = load_cluster()
    return {"targets": cluster.get("backup_targets", {})}




@router.get("/api/backups")
def api_backups_list_all():
    """Cluster-wide backup history. Walks every VM in cluster state
    and flattens its `backups` list, decorating each row with the
    owning vm name + whether the source VM still exists. Used by
    the dashboard's Backups page to render a single restore-able
    list across the whole cluster.

    Sorted newest-first by ts_index (monotonic timestamp).
    vm_present=False rows are kept so operators can still restore a
    deleted VM's snapshots into a fresh LV."""
    cluster = load_cluster()
    vms = cluster.get("vms", {}) or {}
    out = []
    for vm_name, vm in vms.items():
        for b in (vm.get("backups") or []):
            row = dict(b)
            row["vm"] = vm_name
            row["vm_present"] = True
            out.append(row)
    # Snapshots whose source VM was deleted: cluster state only retains
    # backup entries on live VM records, so there are no orphan rows to
    # surface here. (Listing orphans from the repo via
    # `kopia snapshot list` is a possible future addition.)
    out.sort(key=lambda r: r.get("ts_index", 0), reverse=True)
    return {"backups": out}




@router.delete("/api/backup/targets/{target_id}")
def api_backup_target_remove(target_id: str, reason: str = ""):
    try:
        rev = _bs.backup_target_removed(
            target_id=target_id, reason=reason or "operator-remove",
        )
    except Exception as e:
        raise HTTPException(500, f"rqlite write failed: {e}")
    return {"status": "ok", "revision": rev, "target_id": target_id}
# ── backup credential paths ──




# ── Backup secret propagation ──────────────────────────────────────────────
#
# Two secrets need to live on every node, mode 0600, never in cluster state:
#   - /etc/bedrock/backup.key                    (kopia repo password)
#   - /etc/bedrock/backup-credentials/<id>.env   (S3 access/secret keys)
#
# The dashboard collects them once on the master, then mgmt fans them
# out via the existing root@host SSH mesh that agent_install set up.
# Failure to propagate to one node is logged (push_log) but doesn't
# abort the target-set; the affected node will fail loudly the first
# time its reactor tries to `kopia repository connect`. The operator
# can re-trigger propagation by submitting the same form again.

BACKUP_KEY_FILE = "/etc/bedrock/backup.key"


BACKUP_CRED_DIR = "/etc/bedrock/backup-credentials"
