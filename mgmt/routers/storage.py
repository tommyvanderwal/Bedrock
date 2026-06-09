"""Storage-endpoint management (S3/SMB/NFS): list/add/remove/test + enable as witness or backup target."""
from __future__ import annotations
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from dependencies import require_operator
from common import (load_cluster, push_log, ssh_cmd, ssh_cmd_rc, get_nodes,
                    _self_host, _propagate_secret, _write_remote_secret, _render_s3_creds_env)

import sys as _sys
_sys.path.insert(0, "/usr/local/lib/bedrock")
from lib import bedrock_state as _bs             # noqa: E402
router = APIRouter(tags=["storage"])




# ── Consolidated storage endpoints (S3 / SMB / NFS) — the unification (#5) ──
# ONE endpoint row carries the location + storage creds; it is then ACTIVATED for
# backups (a backup_target) and/or as a fileshare/S3 witness, each referencing it
# by endpoint_id. Secrets (s3_secret_key, fs_password) are AEAD-sealed in rqlite by
# the setter and NEVER returned by the list API (only has_* flags).

class StorageEndpointSetRequest(BaseModel):
    endpoint_id: str
    type: str                                  # 's3' | 'smb' | 'nfs'
    label: str = ""
    # S3
    s3_endpoint: str = ""
    s3_bucket: str = ""
    s3_region: str = ""
    s3_prefix: str = ""
    s3_disable_tls: bool = False
    s3_disable_tls_verification: bool = False
    s3_access_key: str = ""
    # SMB/NFS
    fs_server: str = ""
    fs_share: str = ""
    fs_options: str = ""
    fs_username: str = ""
    reason: str = ""
    # Secrets: None = KEEP the stored value (a label edit must not wipe a secret
    # the operator didn't re-type); a string (incl. "") = set it.
    s3_secret_key: Optional[str] = None
    fs_password: Optional[str] = None




def _endpoint_usage(cluster: dict, endpoint_id: str) -> dict:
    """Which backup_targets + witnesses reference this endpoint (for the list UI
    + the in-use delete guard)."""
    bts = [tid for tid, t in (cluster.get("backup_targets") or {}).items()
           if (t or {}).get("endpoint_id") == endpoint_id]
    wits = [wid for wid, w in (cluster.get("witnesses") or {}).items()
            if (w or {}).get("endpoint_id") == endpoint_id]
    return {"backup_targets": bts, "witnesses": wits}




@router.get("/api/storage-endpoints")
def api_storage_endpoints_list():
    cluster = load_cluster()
    eps = cluster.get("storage_endpoints") or {}
    out = []
    for eid, ep in eps.items():
        row = dict(ep)
        row["endpoint_id"] = eid
        row["usage"] = _endpoint_usage(cluster, eid)
        out.append(row)
    return {"endpoints": out}




@router.post("/api/storage-endpoints")
def api_storage_endpoint_set(req: StorageEndpointSetRequest):
    eid = (req.endpoint_id or "").strip()
    if not eid:
        raise HTTPException(400, "endpoint_id is required")
    typ = (req.type or "").strip().lower()
    if typ not in ("s3", "smb", "nfs"):
        raise HTTPException(400, f"type must be s3|smb|nfs, not {typ!r}")
    # Secret-reuse on edit: None → keep the stored (sealed) secret, so editing the
    # label/region can't silently wipe a password the operator didn't re-type.
    s3_secret = req.s3_secret_key
    if s3_secret is None:
        s3_secret = _bs.storage_endpoint_secret(eid, "s3_secret_key")
    fs_password = req.fs_password
    if fs_password is None:
        fs_password = _bs.storage_endpoint_secret(eid, "fs_password")
    try:
        rev = _bs.storage_endpoint_set(
            eid, typ, label=req.label,
            s3_endpoint=req.s3_endpoint, s3_bucket=req.s3_bucket,
            s3_region=req.s3_region, s3_prefix=req.s3_prefix,
            s3_disable_tls=req.s3_disable_tls,
            s3_disable_tls_verification=req.s3_disable_tls_verification,
            s3_access_key=req.s3_access_key, s3_secret_key=s3_secret or "",
            fs_server=req.fs_server, fs_share=req.fs_share,
            fs_options=req.fs_options, fs_username=req.fs_username,
            fs_password=fs_password or "", reason=req.reason)
    except Exception as e:
        raise HTTPException(500, f"rqlite write failed: {e}")
    push_log(f"storage endpoint {eid!r} ({typ}) saved",
             app="bedrock-mgmt", level="info")
    return {"status": "ok", "revision": rev, "endpoint_id": eid}




@router.delete("/api/storage-endpoints/{endpoint_id}")
def api_storage_endpoint_remove(endpoint_id: str, reason: str = ""):
    cluster = load_cluster()
    if endpoint_id not in (cluster.get("storage_endpoints") or {}):
        raise HTTPException(404, f"storage endpoint {endpoint_id!r} not found")
    usage = _endpoint_usage(cluster, endpoint_id)
    if usage["backup_targets"] or usage["witnesses"]:
        raise HTTPException(
            409, f"endpoint {endpoint_id!r} is still in use by "
            f"backup_targets={usage['backup_targets']} "
            f"witnesses={usage['witnesses']} — deactivate those first")
    try:
        rev = _bs.storage_endpoint_removed(endpoint_id)
    except Exception as e:
        raise HTTPException(500, f"rqlite write failed: {e}")
    push_log(f"storage endpoint {endpoint_id!r} removed", app="bedrock-mgmt",
             level="info")
    return {"status": "ok", "revision": rev}




@router.post("/api/storage-endpoints/test")
def api_storage_endpoint_test(req: StorageEndpointTestRequest):
    """Test-on-MASTER-before-commit (this handler runs on the mgmt master). Mounts
    /connects the endpoint with the SUPPLIED creds, proves a real write + read-back
    round-trip (S3 PUT/GET/DELETE; SMB/NFS mount + read-after-write = best-effort
    DFS-R guard), and returns (ok, reason). Never writes to rqlite — pure probe."""
    typ = (req.type or "").strip().lower()
    if typ not in ("s3", "smb", "nfs"):
        raise HTTPException(400, f"type must be s3|smb|nfs, not {typ!r}")
    # Secrets: prefer the freshly-typed value; fall back to the stored one when the
    # operator re-tests an existing endpoint without re-typing.
    s3_secret = req.s3_secret_key
    if s3_secret is None:
        s3_secret = _bs.storage_endpoint_secret(req.endpoint_id, "s3_secret_key")
    fs_password = req.fs_password
    if fs_password is None:
        fs_password = _bs.storage_endpoint_secret(req.endpoint_id, "fs_password")
    endpoint = {
        "type": typ, "s3_endpoint": req.s3_endpoint, "s3_bucket": req.s3_bucket,
        "s3_region": req.s3_region, "s3_prefix": req.s3_prefix,
        "s3_disable_tls": req.s3_disable_tls,
        "s3_disable_tls_verification": req.s3_disable_tls_verification,
        "s3_access_key": req.s3_access_key,
        "fs_server": req.fs_server, "fs_share": req.fs_share,
        "fs_options": req.fs_options, "fs_username": req.fs_username,
    }
    try:
        from lib import storage_mount as _sm
        usage = _sm.WITNESS if (req.usage or "witness") == "witness" else _sm.KOPIA
        ok, reason = _sm.test_endpoint(
            endpoint, usage,
            username=req.fs_username, password=fs_password or "",
            s3_secret_key=s3_secret or "")
    except Exception as e:
        raise HTTPException(500, f"test error: {e}")
    return {"ok": ok, "reason": reason}




class EndpointActivateRequest(BaseModel):
    # Optional explicit id; defaults to deriving one from the endpoint id.
    witness_id: str = ""
    skip_test: bool = False           # operator override of the master pre-test




@router.post("/api/storage-endpoints/{endpoint_id}/enable-witness")
def api_storage_endpoint_enable_witness(endpoint_id: str,
                                        req: EndpointActivateRequest =
                                        EndpointActivateRequest()):
    """The "Enable as fileshare-Witness" box: register a witness backed by this
    endpoint. The witness backend follows the endpoint type (s3 → native S3
    witness; smb/nfs → fileshare witness at the Bedrock-managed strict mount). netd
    (increment 4) resolves the endpoint to its S3 client / mountpoint and writes
    slots there; the slot protocol seals with the CLUSTER key, so no per-witness
    key is needed. Tests on the MASTER first (unless skip_test)."""
    cluster = load_cluster()
    ep = (cluster.get("storage_endpoints") or {}).get(endpoint_id)
    if ep is None:
        raise HTTPException(404, f"storage endpoint {endpoint_id!r} not found")
    typ = (ep.get("type") or "").lower()
    backend = "s3" if typ == "s3" else "fileshare"
    wid = (req.witness_id or f"wit-{endpoint_id}").strip()
    # Test-on-master-before-commit (the share must be usable here before we raise
    # the quorum bar cluster-wide). Reads the sealed creds from rqlite.
    if not req.skip_test:
        s3_secret = _bs.storage_endpoint_secret(endpoint_id, "s3_secret_key")
        fs_password = _bs.storage_endpoint_secret(endpoint_id, "fs_password")
        endpoint = dict(ep); endpoint["type"] = typ
        try:
            from lib import storage_mount as _sm
            ok, reason = _sm.test_endpoint(
                endpoint, _sm.WITNESS, username=ep.get("fs_username", ""),
                password=fs_password or "", s3_secret_key=s3_secret or "")
        except Exception as e:
            raise HTTPException(500, f"witness pre-test error: {e}")
        if not ok:
            raise HTTPException(
                400, f"endpoint {endpoint_id!r} failed the master witness test: "
                f"{reason}. Fix it before enabling as a witness (a witness that "
                f"can't be written would raise the quorum bar without ever voting).")
    try:
        rev = _bs.witness_register(witness_id=wid, addr="",
                                   witness_pubkey_hex="",
                                   encrypted_witness_key_hex="",
                                   backend=backend, endpoint_id=endpoint_id)
    except Exception as e:
        raise HTTPException(500, f"rqlite write failed: {e}")
    push_log(f"witness {wid!r} enabled on endpoint {endpoint_id!r} ({backend})",
             app="bedrock-mgmt", level="info")
    return {"status": "ok", "revision": rev, "witness_id": wid,
            "backend": backend, "endpoint_id": endpoint_id}




class EnableBackupRequest(BaseModel):
    target_id: str = ""
    # Optional per-repo encryption password. "" / None = the published PUBLIC
    # default (effectively-unencrypted); a real value opts this repo into encryption.
    encryption_password: Optional[str] = None
    skip_test: bool = False




@router.post("/api/storage-endpoints/{endpoint_id}/enable-backup")
def api_storage_endpoint_enable_backup(endpoint_id: str,
                                       req: EnableBackupRequest =
                                       EnableBackupRequest()):
    """The "Enable for backups" box: create a backup_target that REFERENCES this
    endpoint (endpoint_id) — the storage location + creds resolve from the endpoint
    at read time (view_builder fills s3_*/filesystem_path; backup_target_s3_creds
    reads the endpoint secret), so there is no duplicated/stale inline config. kind
    follows the endpoint type (s3 → kopia-s3; smb/nfs → kopia-fs at the managed
    cached mount /mnt/bedrock/kopia/<id>). Tests on the MASTER first unless skip_test."""
    cluster = load_cluster()
    ep = (cluster.get("storage_endpoints") or {}).get(endpoint_id)
    if ep is None:
        raise HTTPException(404, f"storage endpoint {endpoint_id!r} not found")
    typ = (ep.get("type") or "").lower()
    kind = "kopia-s3" if typ == "s3" else "kopia-fs"
    tid = (req.target_id or f"bk-{endpoint_id}").strip()
    if not req.skip_test:
        s3_secret = _bs.storage_endpoint_secret(endpoint_id, "s3_secret_key")
        fs_password = _bs.storage_endpoint_secret(endpoint_id, "fs_password")
        endpoint = dict(ep); endpoint["type"] = typ
        try:
            from lib import storage_mount as _sm
            ok, reason = _sm.test_endpoint(
                endpoint, _sm.KOPIA, username=ep.get("fs_username", ""),
                password=fs_password or "", s3_secret_key=s3_secret or "")
        except Exception as e:
            raise HTTPException(500, f"backup pre-test error: {e}")
        if not ok:
            raise HTTPException(
                400, f"endpoint {endpoint_id!r} failed the master backup test: "
                f"{reason}. Fix it before enabling for backups.")
    try:
        rev = _bs.backup_target_set(
            tid, kind, endpoint_id=endpoint_id,
            repo_password=(req.encryption_password or ""), reason="enable-backup")
    except Exception as e:
        raise HTTPException(500, f"rqlite write failed: {e}")
    push_log(f"backup target {tid!r} enabled on endpoint {endpoint_id!r} ({kind})",
             app="bedrock-mgmt", level="info")
    return {"status": "ok", "revision": rev, "target_id": tid, "kind": kind,
            "endpoint_id": endpoint_id}
# ── endpoint test request model ──




class StorageEndpointTestRequest(StorageEndpointSetRequest):
    # Test BEFORE committing: the operator may pass freshly-typed secrets that are
    # not yet in rqlite, so the test request carries them inline (never logged).
    usage: str = "witness"                     # 'witness' (strict) | 'kopia' (cached)
