"""Shared FastAPI dependencies for the mgmt routers.

Per the FastAPI bigger-applications layout, cross-cutting dependencies live here so every
router imports them from one place instead of re-deriving them. Two families:

  * AUTH — `require_peer` / `require_operator` / `require_operator_or_peer`. These mirror the
    app's `_auth_middleware`: loopback (the trusted local CLI on :8001) is exempt, otherwise a
    request needs an operator Bearer token or a peer Ed25519 signature. They are *route-level*
    deps (the middleware is the blanket gate; these are for handlers that name a specific caller
    identity or relax/tighten the default).
  * REQUEST CONTEXT — `get_state` (the shared Daemon object on `app.state.bedrock`) and
    `loopback_only` (the 127.0.0.1/::1 guard for internal endpoints).

The auth code lives in the bedrock `lib` tree (deployed to /usr/local/lib/bedrock/lib) so
installers and mgmt share one implementation.
"""
from __future__ import annotations

import sys as _sys

from fastapi import HTTPException, Request

_sys.path.insert(0, "/usr/local/lib/bedrock")
from lib import peer_auth as _peer_auth          # noqa: E402
from lib import operator_auth as _op_auth        # noqa: E402
from lib import cluster_state as _cluster_state  # noqa: E402


def load_cluster() -> dict:
    """Cluster-wide state from the local rqlite replica (level='none', works without quorum).
    Falls back to an empty cluster on any read error so auth lookups degrade safely."""
    try:
        return _cluster_state.load_cluster()
    except Exception:
        return {"cluster_name": "bedrock", "nodes": {}}


# ── Request context ──────────────────────────────────────────────────

def get_state(request: Request):
    """The shared Daemon object (netd + orchestrator state) attached at startup by bedrock-d
    as `app.state.bedrock`. Raises 503 if it isn't attached yet (early boot)."""
    state = getattr(request.app.state, "bedrock", None)
    if state is None:
        raise HTTPException(503, "bedrock state not attached yet")
    return state


def loopback_only(request: Request) -> None:
    """Guard for internal endpoints: reject anything not from 127.0.0.1/::1. A spoofed-loopback
    source from a real NIC is dropped by rp_filter/martian filtering, so this can't be reached
    remotely."""
    ch = request.client.host if request.client else ""
    if ch not in ("127.0.0.1", "::1"):
        raise HTTPException(403, "endpoint is loopback-only")


# ── Auth dependencies (verbatim from app.py) ─────────────────────────

async def require_peer(request: Request) -> str:
    """FastAPI dep — accepts requests signed by a known cluster node.
    Returns the verified node name. Raises 401 on any failure."""
    body = await request.body()

    def _lookup(node_name: str):
        cluster = load_cluster()
        n = (cluster.get("nodes") or {}).get(node_name) or {}
        pk_hex = (n.get("bedrock_pubkey") or "").strip()
        if not pk_hex:
            return None
        try:
            return bytes.fromhex(pk_hex)
        except ValueError:
            return None

    authz = request.headers.get("authorization", "")
    try:
        return _peer_auth.verify(authz, request.method,
                                 request.url.path
                                 + (("?" + request.url.query) if request.url.query else ""),
                                 body, _lookup)
    except ValueError as e:
        raise HTTPException(401, f"peer auth failed: {e}")


async def require_operator(request: Request) -> str:
    """FastAPI dep — accepts requests with a valid `Authorization: Bearer
    <token>` operator session token. Returns the username on success.
    Raises 401 on any failure. Loopback (the trusted local CLI on :8001)
    is exempt — local root is already privileged; see _auth_middleware."""
    _ch = request.client.host if request.client else ""
    if _ch in ("127.0.0.1", "::1"):
        return "local"
    authz = request.headers.get("authorization", "")
    if not authz.startswith("Bearer "):
        raise HTTPException(401, "missing Bearer token")
    try:
        payload = _op_auth.verify_token(authz[7:].strip())
    except ValueError as e:
        raise HTTPException(401, f"operator auth failed: {e}")
    return payload.get("sub", "")


async def require_operator_or_peer(request: Request) -> str:
    """Accepts EITHER a peer Ed25519 signature OR an operator Bearer
    token. Returns `op:<user>` or `peer:<node>` so handlers know which.
    Use for endpoints that legitimately need both call sites (e.g. an
    operator clicks "transfer mgmt" in the dashboard AND the receiving
    node's mgmt service finishes the handoff by calling back)."""
    authz = request.headers.get("authorization", "")
    if authz.startswith("Bearer "):
        try:
            payload = _op_auth.verify_token(authz[7:].strip())
            return f"op:{payload.get('sub', '')}"
        except ValueError as e:
            raise HTTPException(401, f"operator auth failed: {e}")
    if authz.startswith(_peer_auth.SCHEME + " "):
        body = await request.body()

        def _lookup(node_name: str):
            cluster = load_cluster()
            n = (cluster.get("nodes") or {}).get(node_name) or {}
            pk_hex = (n.get("bedrock_pubkey") or "").strip()
            try:
                return bytes.fromhex(pk_hex) if pk_hex else None
            except ValueError:
                return None

        try:
            who = _peer_auth.verify(
                authz, request.method,
                request.url.path + (("?" + request.url.query) if request.url.query else ""),
                body, _lookup)
            return f"peer:{who}"
        except ValueError as e:
            raise HTTPException(401, f"peer auth failed: {e}")
    raise HTTPException(401, "missing operator or peer credentials")
