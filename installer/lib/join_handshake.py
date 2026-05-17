"""Approval-based join with ECDH-encrypted cluster.key transfer.

The flow:

  joiner                                     master + dashboard
  ──────                                     ──────────────────
  generate Ed25519 ident (peer_auth)
  generate X25519 ephemeral
  print fingerprint(SHA256:abc…) to console
  POST /api/join/request {…, fingerprint}
                                            log JOIN_REQUEST entry
                                            popup on every node's UI
                                            operator visually compares
                                            fingerprint w/ console
                                            operator clicks Approve
                                            generate master X25519 eph
                                            session = HKDF(ECDH)
                                            cipher  = AEAD(session,cluster.key)
                                            log JOIN_RESOLVED entry
                                              with master_eph + cipher
  poll /api/join/status?id=…
                                            (returns approved + ciphertext)
  session = HKDF(ECDH(my_eph, master_eph))
  cluster.key = AEAD-open(session, cipher)
  proceed with install

Defence-in-depth: an active MITM on the LAN can intercept the join
request and substitute their own X25519 pubkey — but cannot forge the
Ed25519 fingerprint the joiner prints on its OWN console. The operator
visually compares fingerprints; any mismatch aborts. After approval,
cluster.key is encrypted under a session key derived from the joiner's
real X25519 private half (which the MITM doesn't have)."""

from __future__ import annotations

import base64
import hashlib
import secrets

from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey, X25519PublicKey,
)
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def _b64_d(s: str) -> bytes:
    return base64.b64decode(s)


def gen_ephemeral() -> tuple[X25519PrivateKey, str]:
    """Generate an X25519 ephemeral key. Returns (priv_obj, pub_b64)."""
    priv = X25519PrivateKey.generate()
    pub_b = priv.public_key().public_bytes_raw()
    return priv, _b64(pub_b)


def fingerprint(pubkey_hex: str) -> str:
    """SSH-style fingerprint of an Ed25519 pubkey: SHA256:<base64>.
    Matches `ssh-keygen -l -E sha256` output format so it looks familiar."""
    raw = bytes.fromhex(pubkey_hex)
    h = hashlib.sha256(raw).digest()
    return "SHA256:" + base64.b64encode(h).rstrip(b"=").decode()


def _derive(my_priv: X25519PrivateKey, their_pub_b64: str,
            request_id: str) -> bytes:
    """HKDF-SHA256 over the ECDH shared secret. `request_id` as salt
    binds the session key to this specific request — a replayed
    approval payload won't decrypt under a different request_id."""
    their = X25519PublicKey.from_public_bytes(_b64_d(their_pub_b64))
    shared = my_priv.exchange(their)
    return HKDF(algorithm=SHA256(), length=32, salt=request_id.encode(),
                info=b"bedrock join handshake v1").derive(shared)


def seal(master_priv: X25519PrivateKey, joiner_eph_pub_b64: str,
         request_id: str, cluster_key: bytes) -> tuple[str, str]:
    """Master side. Returns (ciphertext_b64, nonce_b64)."""
    sess = _derive(master_priv, joiner_eph_pub_b64, request_id)
    nonce = secrets.token_bytes(12)
    ct = ChaCha20Poly1305(sess).encrypt(
        nonce, cluster_key, request_id.encode())
    return _b64(ct), _b64(nonce)


def open_seal(joiner_priv: X25519PrivateKey, master_eph_pub_b64: str,
              request_id: str, ciphertext_b64: str, nonce_b64: str) -> bytes:
    """Joiner side. Returns the 32-byte cluster.key. Raises on tamper."""
    sess = _derive(joiner_priv, master_eph_pub_b64, request_id)
    return ChaCha20Poly1305(sess).decrypt(
        _b64_d(nonce_b64), _b64_d(ciphertext_b64), request_id.encode())


def new_request_id() -> str:
    """24-byte URL-safe random ID for a join attempt. Used as ECDH salt
    so per-request entropy ends up in the derived session key."""
    return base64.urlsafe_b64encode(secrets.token_bytes(24)).rstrip(b"=").decode()
