"""Per-node Ed25519 identity for inter-node API auth.

Each node has its own keypair at /etc/bedrock/node.{key,pub} — generated
lazily on first use, registered via /api/nodes/register, and replicated
to every node's snapshot via the log (so any node can verify any peer's
signature without a side channel).

Outgoing requests carry:

    Authorization: Bedrock-Ed25519 <node_name>:<ts>:<sig_b64>

Where sig = Ed25519_sign(privkey, canonical) and canonical is:

    "<METHOD> <PATH>\\n<sha256_hex(body)>\\n<ts>\\n<node_name>"

The receiver looks up <node_name>'s pubkey in the snapshot, recomputes
canonical, verifies the signature, and accepts requests with |now-ts|
under TOKEN_TTL_S. Body is bound by SHA-256 so a tampered body fails
verify. Path includes the query string (raw .path matches /a?b=c).

Threat model: protects against a LAN attacker forging requests as a
cluster member. Does NOT protect against a compromised node — that
node's pubkey is in the snapshot and it can sign anything (which is
exactly the right semantics for "I am this node")."""

from __future__ import annotations

import base64
import hashlib
import os
import time
from pathlib import Path
from typing import Callable, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey,
)

NODE_KEY = Path("/etc/bedrock/node.key")
NODE_PUB = Path("/etc/bedrock/node.pub")
TOKEN_TTL_S = 300   # ±5 min skew window; replay protection
SCHEME = "Bedrock-Ed25519"


def ensure_node_key() -> tuple[bytes, bytes]:
    """Return (priv_seed, pub_bytes). Generate on first call, idempotent
    afterwards. Both are 32-byte raw representations."""
    if NODE_KEY.exists():
        priv_seed = NODE_KEY.read_bytes()
        if len(priv_seed) != 32:
            raise ValueError(f"{NODE_KEY} corrupt (length {len(priv_seed)} != 32)")
        priv = Ed25519PrivateKey.from_private_bytes(priv_seed)
    else:
        priv = Ed25519PrivateKey.generate()
        priv_seed = priv.private_bytes_raw()
        NODE_KEY.parent.mkdir(parents=True, exist_ok=True)
        # tmp+rename so a crash mid-write can't leave a half-written key
        tmp = NODE_KEY.with_suffix(".tmp")
        tmp.write_bytes(priv_seed)
        os.chmod(tmp, 0o600)
        tmp.rename(NODE_KEY)
    pub_bytes = priv.public_key().public_bytes_raw()
    if not NODE_PUB.exists() or NODE_PUB.read_bytes() != pub_bytes:
        NODE_PUB.write_bytes(pub_bytes)
        os.chmod(NODE_PUB, 0o644)
    return priv_seed, pub_bytes


def pubkey_hex() -> str:
    return ensure_node_key()[1].hex()


def _canonical(method: str, path: str, body: bytes, ts: int, node: str) -> bytes:
    body_h = hashlib.sha256(body).hexdigest()
    return f"{method.upper()} {path}\n{body_h}\n{ts}\n{node}".encode()


def sign(method: str, path: str, body: bytes, node_name: str) -> str:
    """Return an Authorization header value signed by this node."""
    priv_seed, _ = ensure_node_key()
    priv = Ed25519PrivateKey.from_private_bytes(priv_seed)
    ts = int(time.time())
    sig = priv.sign(_canonical(method, path, body, ts, node_name))
    return f"{SCHEME} {node_name}:{ts}:{base64.b64encode(sig).decode()}"


def verify(header: str, method: str, path: str, body: bytes,
           pubkey_lookup: Callable[[str], Optional[bytes]]) -> str:
    """Verify the header against pubkey_lookup(node_name) -> 32-byte pubkey.
    Return verified node name on success; raise ValueError otherwise."""
    if not header or not header.startswith(SCHEME + " "):
        raise ValueError("missing or wrong auth scheme")
    rest = header[len(SCHEME) + 1:].strip()
    try:
        node_name, ts_str, sig_b64 = rest.split(":", 2)
        ts = int(ts_str)
        sig = base64.b64decode(sig_b64)
    except (ValueError, base64.binascii.Error):
        raise ValueError("malformed auth header")
    skew = abs(int(time.time()) - ts)
    if skew > TOKEN_TTL_S:
        raise ValueError(f"timestamp outside ±{TOKEN_TTL_S}s window (skew={skew}s)")
    pub_b = pubkey_lookup(node_name)
    if not pub_b:
        raise ValueError(f"no pubkey on file for node {node_name!r}")
    try:
        Ed25519PublicKey.from_public_bytes(pub_b).verify(
            sig, _canonical(method, path, body, ts, node_name))
    except InvalidSignature:
        raise ValueError("signature invalid")
    return node_name


# ── Outgoing helper ──────────────────────────────────────────────────
# Used by mgmt + installer code that needs to call another node's API.
# Wraps urllib so callers don't have to think about signing.

import json as _json
import ssl as _ssl
import urllib.request as _ur

_INSECURE_CTX = _ssl.create_default_context()
_INSECURE_CTX.check_hostname = False
_INSECURE_CTX.verify_mode = _ssl.CERT_NONE


def request(method: str, url: str, body: Optional[dict], node_name: str,
            timeout: float = 10.0) -> dict:
    """Sign + send a request to another node's mgmt API. Returns parsed JSON.

    The cert on 8443 is for `<dashed-ip>.my.local-ip.co`, never the bare
    IP we dial. Verification is intentionally off — peer trust comes from
    the Ed25519 signature, not TLS PKI."""
    from urllib.parse import urlparse
    body_bytes = _json.dumps(body).encode() if body is not None else b""
    parsed = urlparse(url)
    path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    headers = {"Authorization": sign(method, path, body_bytes, node_name)}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = _ur.Request(url, data=body_bytes if body is not None else None,
                      method=method.upper(), headers=headers)
    opener_kwargs = {"timeout": timeout}
    if parsed.scheme == "https":
        opener_kwargs["context"] = _INSECURE_CTX
    with _ur.urlopen(req, **opener_kwargs) as r:
        raw = r.read()
    return _json.loads(raw) if raw else {}
