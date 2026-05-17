"""Operator login + cluster-wide JWT-HS256 session tokens.

Operators live in the replicated snapshot (via `OPERATOR_SET` /
`OPERATOR_REMOVE` log entries), so adding/removing an operator on any
node propagates to all peers within ~1s.

Login: POST /api/login {username, password} → {token, exp}.
Token: standard JWT-HS256 (`header.payload.signature`, all base64url no
padding). Signed with /etc/bedrock/cluster.key so every node verifies
tokens issued by every other — no per-node session DB needed.

Why JWT-shaped: every browser / CLI / library understands it; future
operators can swap in an external IdP without a wire-format break.
Why HS256 (symmetric): every cluster node already trusts cluster.key
for the witness AEAD; adding asymmetric session keys would buy nothing
in our threat model. (RS256 only matters if you want *external* parties
to verify tokens without holding the secret — we don't.)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path

CLUSTER_KEY_PATH = Path("/etc/bedrock/cluster.key")
TOKEN_TTL_S = 8 * 3600        # 8h — covers a full operator shift
REFRESH_WINDOW_S = 3600       # frontend may refresh in last hour
PBKDF2_ITER = 200_000         # ~150ms on the testbed AMD; ↑ later if needed


# ── base64url helpers ──────────────────────────────────────────────

def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64u_d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# ── Password hashing ────────────────────────────────────────────────

def hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    """Returns (salt_hex, hash_hex). 16-byte salt + 32-byte PBKDF2-HMAC-SHA256."""
    if salt is None:
        salt = secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITER)
    return salt.hex(), h.hex()


def verify_password(password: str, salt_hex: str, hash_hex: str) -> bool:
    try:
        salt = bytes.fromhex(salt_hex or "")
        expected = bytes.fromhex(hash_hex or "")
    except ValueError:
        return False
    if not salt or not expected:
        return False
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITER)
    return hmac.compare_digest(h, expected)


# ── Token mint / verify ─────────────────────────────────────────────

def _cluster_key() -> bytes:
    if not CLUSTER_KEY_PATH.exists():
        raise RuntimeError(f"{CLUSTER_KEY_PATH} not found — node not initialised")
    return CLUSTER_KEY_PATH.read_bytes()


def mint_token(user: str, ttl_s: int = TOKEN_TTL_S) -> tuple[str, int]:
    """Mint a JWT-HS256 token. Returns (token, exp_unix)."""
    now = int(time.time())
    exp = now + ttl_s
    header_b = _b64u(b'{"alg":"HS256","typ":"JWT"}')
    payload_b = _b64u(json.dumps(
        {"sub": user, "iat": now, "exp": exp},
        separators=(",", ":")).encode())
    signing_input = f"{header_b}.{payload_b}".encode()
    sig = hmac.new(_cluster_key(), signing_input, hashlib.sha256).digest()
    return f"{header_b}.{payload_b}.{_b64u(sig)}", exp


def verify_token(token: str) -> dict:
    """Returns the decoded payload dict on success. Raises ValueError otherwise."""
    if not token or token.count(".") != 2:
        raise ValueError("malformed token")
    header_b, payload_b, sig_b = token.split(".")
    signing_input = f"{header_b}.{payload_b}".encode()
    expected = hmac.new(_cluster_key(), signing_input, hashlib.sha256).digest()
    try:
        actual = _b64u_d(sig_b)
    except Exception:
        raise ValueError("malformed signature")
    if not hmac.compare_digest(expected, actual):
        raise ValueError("signature invalid")
    try:
        payload = json.loads(_b64u_d(payload_b))
    except Exception:
        raise ValueError("malformed payload")
    if int(time.time()) >= int(payload.get("exp", 0)):
        raise ValueError("token expired")
    return payload
