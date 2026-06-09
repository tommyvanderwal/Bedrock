"""Cluster witness management: list/add/remove witnesses + discover Echo witnesses on the LAN."""
from __future__ import annotations
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from dependencies import require_operator
from common import (load_cluster, push_log, ssh_cmd, ssh_cmd_rc, get_nodes,
                    _self_host, _propagate_secret, _write_remote_secret)

import sys as _sys
_sys.path.insert(0, "/usr/local/lib/bedrock")
from lib import bedrock_state as _bs             # noqa: E402
router = APIRouter(tags=["witnesses"])




# ── Witness management ──────────────────────────────────────────────
# Add / list / remove cluster witnesses for the weighted-vote quorum
# (each valid witness = 1 vote; nodes = 100). Writes the rqlite
# `witnesses` table — Raft replicates it, and EVERY node's netd 1 Hz
# election tick reloads the list automatically, so no explicit daemon
# propagation is needed from mgmt (unlike the CLI path). The operator
# dashboard drives these.

class WitnessAddRequest(BaseModel):
    witness_id: str
    addr: str = ""             # echo: "host[:port]"; fileshare: mounted dir path
    witness_pubkey: str = ""   # X25519 pubkey hex (64 chars) — required for echo
    backend: str = "echo"      # "echo" | "fileshare" (smb/s3 = future managed)
    reason: str = ""




@router.get("/api/witnesses")
def api_witnesses_list():
    return {"witnesses": load_cluster().get("witnesses", {})}




def _api_witness_add_fileshare(wid: str, req: WitnessAddRequest):
    """Register a PATH-BASED fileshare witness. addr = an absolute directory the
    operator has mounted the shared store (NFS/SMB/object) at on EVERY node;
    netd's off-hot-path worker writes slot-<NN>.bin there and folds the verdict
    into the vote. We probe writability on THIS node (the master) as a fail-fast
    UX guard — full per-node assurance is enforced at vote time by the slot
    protocol (a node that can't write leaves its slot absent → 0 votes, never a
    miscount)."""
    import os as _os
    try:
        from lib import witness_file as _wf  # type: ignore
    except ImportError:
        import sys as _sys
        _sys.path.insert(0, "/usr/local/lib/bedrock")
        from lib import witness_file as _wf  # type: ignore
    path = (req.addr or "").strip()
    if not path:
        raise HTTPException(400, "addr (the mounted share directory) is required "
                                 "for a fileshare witness")
    if not _os.path.isabs(path):
        raise HTTPException(400, f"fileshare witness path must be absolute, "
                                 f"got {path!r}")
    err = _wf.probe_writable(path)
    if err:
        raise HTTPException(
            400, f"fileshare witness path {path!r} is not usable on this node: "
            f"{err}. Mount the share and ensure it is writable on EVERY node "
            f"before adding it.")
    try:
        rev = _bs.witness_register(witness_id=wid, addr=path,
                                   witness_pubkey_hex="",
                                   encrypted_witness_key_hex="",
                                   backend="fileshare")
    except Exception as e:
        raise HTTPException(500, f"rqlite write failed: {e}")
    push_log(f"witness {wid!r} added (fileshare {path})",
             app="bedrock-mgmt", level="info")
    return {"status": "ok", "revision": rev, "witness_id": wid,
            "addr": path, "backend": "fileshare"}




@router.post("/api/witnesses")
def api_witness_add(req: WitnessAddRequest):
    wid = (req.witness_id or "").strip()
    if not wid:
        raise HTTPException(400, "witness_id is required")
    backend = (req.backend or "echo").strip().lower()
    if backend not in ("echo", "fileshare", "smb", "s3"):
        raise HTTPException(400, f"unknown witness backend {backend!r} "
                                 f"(expected echo | fileshare)")
    if backend in ("smb", "s3"):
        # NATIVE (Bedrock-managed-creds) SMB/S3 is a future build — backup uses
        # kopia's own S3 client and there is no mount/cred infra to reuse, so an
        # S3 blob client in the quorum path would be net-new. Today a fileshare
        # witness is PATH-BASED: the operator mounts the SMB/S3/NFS share on
        # every node and adds it with backend='fileshare' + that path; Bedrock
        # writes slot files there. Refuse smb/s3 rather than register a witness
        # with no transport (it would raise the quorum bar without ever voting →
        # can BLOCK failover on a 2-node cluster).
        raise HTTPException(
            400, f"witness backend {backend!r} is not a managed backend yet. "
            f"Mount the {backend.upper()} share on every node and add it as a "
            f"fileshare witness (backend='fileshare', addr=<mounted dir>) — "
            f"Bedrock writes slot files there. Managed-{backend} is a future build.")
    if backend == "fileshare":
        return _api_witness_add_fileshare(wid, req)
    addr = (req.addr or "").strip()
    if not addr:
        raise HTTPException(400, "addr is required (ipv4 or ipv4:port)")
    # An Echo witness must be an IPv4 UNICAST literal: netd directed-probes it
    # from the single-threaded 1Hz election tick over an AF_INET socket, so a
    # hostname (synchronous getaddrinfo would stall failover detection), an
    # IPv6 literal (unreachable on AF_INET), or a multicast/broadcast/0.0.0.0
    # addr (would flood the segment) are all refused HERE — fail loud at add
    # time rather than register an unusable witness that silently raises the
    # quorum bar. host:port, default port 12321.
    import ipaddress as _ipaddr
    host, _, port_s = addr.partition(":") if ":" in addr else (addr, "", "")
    port = 12321
    if port_s:
        try:
            port = int(port_s)
        except ValueError:
            raise HTTPException(400, f"invalid port {port_s!r} in addr {addr!r}")
        if not (1 <= port <= 65535):
            raise HTTPException(400, f"port {port} out of range (1-65535)")
    try:
        ip = _ipaddr.ip_address(host)
    except ValueError:
        raise HTTPException(
            400, f"Echo witness address must be an IPv4 literal, not a "
            f"hostname ({host!r}). A hostname would block the election tick on "
            f"DNS. Add the Echo by its IP.")
    if (ip.version != 4 or ip.is_multicast or ip.is_unspecified
            or ip.is_reserved or ip.is_loopback or ip.is_link_local):
        raise HTTPException(
            400, f"Echo witness address {host!r} is not a usable IPv4 unicast "
            f"address (no multicast/broadcast/loopback/link-local/unspecified).")
    stored_addr = f"{host}:{port}"
    pubkey = (req.witness_pubkey or "").strip().lower()
    if backend == "echo":
        # An Echo's X25519 public key is 32 bytes = 64 hex chars. Validate
        # FAIL-LOUD: a bad paste would silently write a witness netd can never
        # authenticate against (it would just never count toward quorum).
        if len(pubkey) != 64 or any(c not in "0123456789abcdef" for c in pubkey):
            raise HTTPException(
                400, "witness_pubkey must be 64 hex chars (the Echo's X25519 "
                "public key) for an echo witness")
    try:
        rev = _bs.witness_register(witness_id=wid, addr=stored_addr,
                                   witness_pubkey_hex=pubkey,
                                   encrypted_witness_key_hex="",
                                   backend=backend)
    except Exception as e:
        raise HTTPException(500, f"rqlite write failed: {e}")
    push_log(f"witness {wid!r} added ({backend} {stored_addr})",
             app="bedrock-mgmt", level="info")
    return {"status": "ok", "revision": rev, "witness_id": wid,
            "addr": stored_addr, "backend": backend}




@router.delete("/api/witnesses/{witness_id}")
def api_witness_remove(witness_id: str, reason: str = ""):
    # 404 for a non-existent witness — witness_unregister's DELETE matches 0
    # rows but still "succeeds" and bumps the revision, so without this a
    # typo'd delete reports success and churns every node's reactor for nothing.
    if witness_id not in (load_cluster().get("witnesses") or {}):
        raise HTTPException(404, f"witness {witness_id!r} not found")
    try:
        rev = _bs.witness_unregister(witness_id)
    except Exception as e:
        raise HTTPException(500, f"rqlite write failed: {e}")
    push_log(f"witness {witness_id!r} removed", app="bedrock-mgmt", level="info")
    return {"status": "ok", "revision": rev, "witness_id": witness_id}




@router.get("/api/witnesses/discover")
def api_witnesses_discover():
    """Best-effort mDNS discovery of BedRock Echo witnesses (bedrock-echo.local)
    on the LAN, so the dashboard can offer one-click add. Each result carries
    echo_id (used AS the witness_id — netd binds the vote to echo_id==witness_id)
    and the Echo's pubkey, so nothing needs hand-typing. Echoes advertise this
    service (real firmware + the testbed stub); an Echo on a routed segment that
    doesn't answer multicast can still be added by IP."""
    try:
        from lib import discovery as _disc
        echoes = _disc.discover_echo_witnesses(timeout=2.0)
    except Exception as e:
        raise HTTPException(500, f"discovery failed: {e}")
    return {"candidates": [
        {"ip": e.ip, "echo_id": e.echo_id, "pubkey": e.pubkey}
        for e in (echoes or [])]}
