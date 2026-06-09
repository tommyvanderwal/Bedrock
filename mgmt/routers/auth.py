"""Operator login + session + operator-credential management, plus the peer-auth smoke test."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from dependencies import require_operator, require_peer
from common import load_cluster, push_log

import sys as _sys
_sys.path.insert(0, "/usr/local/lib/bedrock")
from lib import operator_auth as _op_auth      # noqa: E402
from lib import bedrock_state as _bs           # noqa: E402

router = APIRouter(tags=["auth"])




# Smoke endpoint for the Ed25519 framework. Returns the caller's verified
# node name. Used by tests + operators wanting to confirm that inter-node
# signing is wired up correctly.
@router.get("/api/peer-test")
def peer_test(node: str = Depends(require_peer)):
    return {"verified_caller": node}




# Internal loopback endpoints (CDC + DRBD fence-decision) live in routers/internal.py


# ── Operator login ──────────────────────────────────────────────────

class LoginReq(BaseModel):
    username: str
    password: str




# Per-IP leaky-bucket rate limiter so a brute-forcer can't fill the
# event loop with PBKDF2 work. 5 fails/min/IP; resets on success.
_LOGIN_BUCKET: dict[str, list[float]] = {}


_LOGIN_MAX = 5


_LOGIN_WINDOW_S = 60




def _login_throttle(ip: str) -> bool:
    """Returns True if the request should be rejected."""
    import time as _t
    now = _t.time()
    bucket = [t for t in _LOGIN_BUCKET.get(ip, []) if now - t < _LOGIN_WINDOW_S]
    _LOGIN_BUCKET[ip] = bucket
    return len(bucket) >= _LOGIN_MAX




def _login_record_fail(ip: str) -> None:
    import time as _t
    _LOGIN_BUCKET.setdefault(ip, []).append(_t.time())




@router.post("/api/login")
def login(req: LoginReq, request: Request):
    ip = request.client.host if request.client else "?"
    if _login_throttle(ip):
        raise HTTPException(429, "too many failed logins, try again in a minute")
    ops = (load_cluster().get("operators") or {})
    op = ops.get(req.username) or {}
    if not _op_auth.verify_password(req.password, op.get("salt", ""), op.get("hash", "")):
        _login_record_fail(ip)
        # Constant-ish response time: PBKDF2 already ran for ~150ms regardless
        # of whether the user exists. Don't differentiate "no such user" vs
        # "wrong password" in the response.
        raise HTTPException(401, "invalid credentials")
    token, exp = _op_auth.mint_token(req.username)
    push_log(f"operator {req.username!r} logged in from {ip}",
             node="mgmt", app="bedrock-mgmt", level="info")
    return {"token": token, "exp": exp, "user": req.username}




@router.get("/api/whoami")
def whoami(user: str = Depends(require_operator)):
    return {"user": user}




# ── Operator management (passwd / list / remove) ───────────────────

class OperatorSet(BaseModel):
    username: str
    password: str




@router.get("/api/operators")
def list_operators(user: str = Depends(require_operator)):
    """Return the list of operator usernames. Hashes are NOT exposed —
    they live write-only in the replicated `operators` cluster state."""
    ops = (load_cluster().get("operators") or {})
    return {"operators": sorted(ops.keys())}




@router.post("/api/operators/set")
def set_operator(req: OperatorSet, user: str = Depends(require_operator)):
    """Upsert an operator credential. `bedrock operator passwd <user>`
    uses this — the same endpoint adds a new operator OR changes an
    existing one's password (the rqlite write is upsert-shaped). Operator
    must already be authenticated; we don't require the OLD password
    because the Bearer token already proves authority.
    """
    if not req.username or not req.password:
        raise HTTPException(400, "username and password required")
    if len(req.password) < 4:
        raise HTTPException(400, "password too short (min 4 chars for now)")
    salt, phash = _op_auth.hash_password(req.password)
    try:
        _bs.operator_set(
            username=req.username, salt=salt, password_hash=phash)
    except Exception as e:
        raise HTTPException(503, f"could not set operator: {e}")
    push_log(f"operator {user!r} set password for {req.username!r}",
             node="mgmt", app="bedrock-mgmt", level="info")
    return {"username": req.username, "status": "set"}




class OperatorRemove(BaseModel):
    username: str




@router.post("/api/operators/remove")
def remove_operator(req: OperatorRemove, user: str = Depends(require_operator)):
    """Delete an operator. Refuses to remove the last operator — that
    would lock the cluster's dashboard. Also refuses to remove
    yourself (operator must be removed by a different operator, so
    accidental lockout requires two mistakes)."""
    ops = (load_cluster().get("operators") or {})
    if req.username not in ops:
        raise HTTPException(404, f"no such operator: {req.username!r}")
    if req.username == user:
        raise HTTPException(400, "refusing to remove yourself; "
                                  "ask another operator")
    if len(ops) <= 1:
        raise HTTPException(400, "refusing to remove the last operator "
                                  "(cluster would lock out)")
    try:
        _bs.operator_remove(username=req.username)
    except Exception as e:
        raise HTTPException(503, f"could not remove operator: {e}")
    push_log(f"operator {user!r} removed {req.username!r}",
             node="mgmt", app="bedrock-mgmt", level="warn")
    return {"username": req.username, "status": "removed"}
