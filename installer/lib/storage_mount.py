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
from pathlib import Path
from typing import Optional

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
