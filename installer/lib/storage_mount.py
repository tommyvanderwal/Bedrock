"""Bedrock-managed SMB/NFS mounting for storage endpoints.

A `storage_endpoints` row of type 'smb' or 'nfs' is mounted on EVERY node at up
to TWO mountpoints, because kopia and the witness want opposite semantics:

    /mnt/bedrock/kopia/<endpoint_id>   — CACHED (performance; kopia is crash-
                                         resilient and never in a tight race)
    /mnt/bedrock/witness/<endpoint_id> — STRONG (NFS `noac` / SMB `cache=none`;
                                         the slot protocol needs read-after-write
                                         coherence)

Only the mountpoint(s) a consumer actually uses are mounted. S3 endpoints need
NO mount (kopia's native S3 backend and witness_s3 hit the endpoint directly).

Credentials: an SMB username + the (cluster-key-unsealed) password are written
to a 0600 credentials file for mount.cifs; NFS needs none (the export owns auth).
NFS is mounted WITHOUT pinning a version so it negotiates the highest the server
speaks (so it works on an old NAS too); `hard` is always set (never `soft` —
soft can corrupt a write mid-flight).
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

MOUNT_ROOT = Path("/mnt/bedrock")
CRED_DIR = Path("/etc/bedrock/storage-credentials")   # 0700; per-endpoint cifs creds

KOPIA = "kopia"
WITNESS = "witness"
USAGES = (KOPIA, WITNESS)


def mountpoint(endpoint_id: str, usage: str) -> Path:
    """The absolute mountpoint for an endpoint + usage (kopia|witness)."""
    if usage not in USAGES:
        raise ValueError(f"usage must be one of {USAGES}, got {usage!r}")
    return MOUNT_ROOT / usage / endpoint_id


def is_mounted(path: Path) -> bool:
    return subprocess.run(["mountpoint", "-q", str(path)]).returncode == 0


def _mount_source(endpoint: dict) -> str:
    """The mount source: //server/share for SMB, server:/export for NFS."""
    typ = endpoint["type"]
    server = (endpoint.get("fs_server") or "").strip()
    share = (endpoint.get("fs_share") or "").strip()
    if not server or not share:
        raise ValueError("smb/nfs endpoint needs fs_server + fs_share")
    if typ == "nfs":
        # server:/export ; tolerate a share given with or without a leading '/'
        return f"{server}:{share if share.startswith('/') else '/' + share}"
    if typ == "smb":
        return f"//{server}/{share.lstrip('/')}"
    raise ValueError(f"type {typ!r} is not a mountable filesystem (S3 needs no mount)")


def _mount_opts(endpoint: dict, usage: str) -> str:
    """Mount options for an endpoint + usage. Witness gets the strong
    (coherent, write-through) variant; kopia gets the cached/performant one."""
    typ = endpoint["type"]
    extra = (endpoint.get("fs_options") or "").strip()
    if typ == "nfs":
        opts = ["hard", "rw"]
        if usage == WITNESS:
            opts.append("noac")          # read-after-write coherence + write-through
    elif typ == "smb":
        opts = ["rw"]
        opts.append("cache=none" if usage == WITNESS else "cache=strict")
    else:
        raise ValueError(f"type {typ!r} is not mountable (S3 needs no mount)")
    if extra:
        opts.append(extra)
    return ",".join(opts)


def write_cifs_credentials(endpoint_id: str, username: str, password: str) -> Path:
    """Write a 0600 mount.cifs credentials file (tmp+rename). Returns its path."""
    CRED_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(CRED_DIR, 0o700)
    path = CRED_DIR / f"{endpoint_id}.cifs"
    fd, tmp = tempfile.mkstemp(prefix=f".{endpoint_id}.", dir=CRED_DIR)
    try:
        os.write(fd, f"username={username}\npassword={password}\n".encode())
    finally:
        os.close(fd)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path


def build_mount_cmd(endpoint_id: str, endpoint: dict, usage: str,
                    *, cred_file: Optional[Path] = None) -> list[str]:
    """Construct the `mount` argv for one mountpoint. For SMB a cred_file must
    be supplied (written by write_cifs_credentials)."""
    typ = endpoint["type"]
    fstype = "nfs" if typ == "nfs" else "cifs"
    opts = _mount_opts(endpoint, usage)
    if typ == "smb":
        if cred_file is None:
            raise ValueError("smb mount needs a credentials file")
        opts = f"credentials={cred_file},{opts}"
    return ["mount", "-t", fstype, "-o", opts,
            _mount_source(endpoint), str(mountpoint(endpoint_id, usage))]


def mount_endpoint(endpoint_id: str, endpoint: dict, usage: str,
                   *, username: str = "", password: str = "") -> None:
    """Idempotently mount one mountpoint for an endpoint. Raises (fail-loud) on
    a mount failure — the caller decides (a backup/witness that can't mount must
    not silently appear configured)."""
    mp = mountpoint(endpoint_id, usage)
    if is_mounted(mp):
        return
    mp.mkdir(parents=True, exist_ok=True)
    cred = None
    if endpoint["type"] == "smb":
        cred = write_cifs_credentials(endpoint_id, username, password)
    cmd = build_mount_cmd(endpoint_id, endpoint, usage, cred_file=cred)
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(
            f"mount of endpoint {endpoint_id!r} ({usage}) failed: "
            f"{(r.stderr or r.stdout).strip()}")


def unmount_endpoint(endpoint_id: str, usage: str) -> None:
    """Idempotently unmount one mountpoint (best-effort)."""
    mp = mountpoint(endpoint_id, usage)
    if is_mounted(mp):
        subprocess.run(["umount", str(mp)], capture_output=True)


# ─────────────────────────────────────────────────────────────────
#  Lifecycle: reconcile the live mounts to what the cluster wants
# ─────────────────────────────────────────────────────────────────
@dataclass
class MountSpec:
    """One desired mount: an SMB/NFS endpoint at one usage's mountpoint.
    Secrets are NOT carried here — an SMB password is unsealed on-demand at
    mount time (the cluster view never projects the sealed blob), matching the
    'read+unseal directly from rqlite when you must mount' rule."""
    endpoint_id: str
    endpoint: dict
    usage: str

    @property
    def key(self) -> tuple:
        return (self.endpoint_id, self.usage)


def desired_mounts_from_view(view: dict) -> list:
    """Derive the desired set of SMB/NFS mounts from a cluster view: every
    backup_target that references a storage_endpoint needs that endpoint at the
    KOPIA mountpoint; every witness that references one needs it at the WITNESS
    mountpoint. S3 endpoints produce NO mount (hit directly). De-duplicated by
    (endpoint_id, usage) — one endpoint used by two targets mounts once."""
    eps = view.get("storage_endpoints") or {}
    specs: dict = {}

    def _add(eid: str, usage: str) -> None:
        ep = eps.get(eid)
        if not ep or ep.get("type") not in ("smb", "nfs"):
            return                         # missing, or S3 (needs no mount)
        specs[(eid, usage)] = MountSpec(endpoint_id=eid, endpoint=ep, usage=usage)

    for t in (view.get("backup_targets") or {}).values():
        if t.get("endpoint_id"):
            _add(t["endpoint_id"], KOPIA)
    for w in (view.get("witnesses") or {}).values():
        if w.get("endpoint_id"):
            _add(w["endpoint_id"], WITNESS)
    return list(specs.values())


def current_bedrock_mounts() -> set:
    """The set of (endpoint_id, usage) currently mounted under /mnt/bedrock.
    Scans both usage dirs and checks each child is a real mountpoint."""
    present: set = set()
    for usage in USAGES:
        base = MOUNT_ROOT / usage
        try:
            children = list(base.iterdir())
        except OSError:
            continue
        for child in children:
            if child.is_dir() and is_mounted(child):
                present.add((child.name, usage))
    return present


def _reconcile_plan(desired: set, present: set) -> tuple:
    """Pure planner: (to_mount, to_unmount) given the desired and present
    (endpoint_id, usage) sets. to_mount = desired-not-present;
    to_unmount = present-not-desired (only ever within /mnt/bedrock, so we
    never touch an operator's own mount)."""
    to_mount = desired - present
    to_unmount = present - desired
    return to_mount, to_unmount


def reconcile_mounts(specs: list, *,
                     unseal_password: Optional[Callable[[str], str]] = None,
                     log: Optional[Callable[[str], None]] = None) -> dict:
    """Make the live mounts match ``specs``: mount each desired-but-absent
    endpoint, unmount each present-but-no-longer-desired one (only under
    /mnt/bedrock). Per-endpoint fail-loud-but-continue — one share that won't
    mount is logged and recorded in 'failed', never aborting the others (a
    backup target that can't mount must not block a witness, or the boot path).
    ``unseal_password(endpoint_id)->str`` supplies the SMB password on demand.
    Returns {'mounted','unmounted','failed'} lists of (endpoint_id, usage)."""
    by_key = {s.key: s for s in specs}
    desired = set(by_key)
    present = current_bedrock_mounts()
    to_mount, to_unmount = _reconcile_plan(desired, present)
    out = {"mounted": [], "unmounted": [], "failed": []}

    for key in sorted(to_mount):
        eid, usage = key
        spec = by_key[key]
        try:
            username, password = "", ""
            if spec.endpoint.get("type") == "smb":
                username = spec.endpoint.get("fs_username", "") or ""
                if unseal_password is not None:
                    password = unseal_password(eid) or ""
            mount_endpoint(eid, spec.endpoint, usage,
                           username=username, password=password)
            out["mounted"].append(key)
        except Exception as e:           # fail-loud per endpoint, keep going
            if log is not None:
                log(f"mount reconcile: endpoint {eid!r} ({usage}) failed: {e}")
            out["failed"].append(key)

    for key in sorted(to_unmount):
        eid, usage = key
        try:
            unmount_endpoint(eid, usage)
            out["unmounted"].append(key)
        except Exception as e:
            if log is not None:
                log(f"mount reconcile: unmount {eid!r} ({usage}) failed: {e}")
            out["failed"].append(key)
    return out


def reconcile_from_cluster(view: dict, *,
                           unseal_password: Optional[Callable[[str], str]] = None,
                           log: Optional[Callable[[str], None]] = None) -> dict:
    """Convenience: derive the desired mounts from a cluster view and reconcile.
    Best-effort + idempotent — safe to call at boot, on endpoint activate/
    deactivate, and as a periodic safety net."""
    return reconcile_mounts(desired_mounts_from_view(view),
                            unseal_password=unseal_password, log=log)


def test_endpoint(endpoint: dict, usage: str = WITNESS,
                  *, username: str = "", password: str = "") -> tuple[bool, str]:
    """Add-time probe for the Test button / test-on-master-before-commit: mount
    the endpoint at a throwaway mountpoint, prove a real write (create+unlink),
    and unmount. Returns (ok, reason). Never raises. The witness usage is the
    strict mount; pass usage=KOPIA to test the cached one."""
    typ = endpoint.get("type")
    if typ == "s3":
        return True, "s3 endpoint needs no mount (validated by the s3 client)"
    if typ not in ("smb", "nfs"):
        return False, f"unknown endpoint type {typ!r}"
    cred = None
    tmp_mp = Path(tempfile.mkdtemp(prefix="bedrock-mnt-test-"))
    try:
        if typ == "smb":
            cred = write_cifs_credentials("_test", username, password)
        opts = _mount_opts(endpoint, usage)
        if typ == "smb":
            opts = f"credentials={cred},{opts}"
        cmd = ["mount", "-t", "nfs" if typ == "nfs" else "cifs", "-o", opts,
               _mount_source(endpoint), str(tmp_mp)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            return False, f"mount failed: {(r.stderr or r.stdout).strip()}"
        try:
            probe = tmp_mp / ".bedrock-write-probe"
            probe.write_text("ok")
            probe.unlink()
        except OSError as e:
            return False, f"mounted but not writable: {e}"
        return True, "mounted + writable"
    except Exception as e:
        return False, f"test error: {e!r}"
    finally:
        subprocess.run(["umount", str(tmp_mp)], capture_output=True)
        try:
            tmp_mp.rmdir()
        except OSError:
            pass
        if cred is not None:
            try:
                cred.unlink()
            except OSError:
                pass
