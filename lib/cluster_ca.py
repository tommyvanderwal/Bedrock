"""Cluster CA — issues TLS certs for rqlite mTLS.

One CA per cluster. CA private key lives on the DRBD ``cluster``
singleton volume so it follows the master role (only the current master
can sign new joiner certs). CA public cert is distributed to every node
so all rqlited instances can verify each other's certs.

Why a single cluster CA (and not a per-node trust list):

- Every routine ``node join`` would otherwise require a rolling
  ``rqlited`` restart on every existing node to add the joiner's cert
  to their trust bundle. With one CA, existing nodes already trust
  anything CA-signed; no restarts on join. CA rotation (rare,
  operator-initiated) is the only event that needs coordinated
  restarts; tracked in docs/operator-overrides.md.

Why we reuse the existing per-node Ed25519 keypair from peer_auth:

- Each node already has /etc/bedrock/node.{key,pub} (32-byte raw
  Ed25519 seed + 32-byte raw pubkey). TLS 1.3 (RFC 8446 + RFC 8410)
  supports Ed25519 in X.509 directly, so we just wrap the existing
  key in a CA-signed cert — no second keypair to manage. The raw
  seed cannot be passed to rqlited directly though (rqlited reads
  PEM), so we also write a PEM-formatted copy alongside.

Validity: 100 years. Rotation is an explicit operator action, never
time-based — see docs/operator-overrides.md "rekey-ca". Expiry that
silently breaks a working cluster is the failure mode this avoids.
"""

from __future__ import annotations

import datetime
import ipaddress
import os
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey,
)
from cryptography.x509.oid import NameOID


# ── Path layout (load-bearing — referenced from cluster_init,
#     node_join, and the rqlited systemd units) ────────────────────
CA_DIR        = Path("/var/lib/bedrock/cluster/ca")
CA_KEY        = CA_DIR / "ca.key"          # CA private key, mode 0600
CA_CERT_DRBD  = CA_DIR / "ca.crt"          # CA cert on DRBD (master)
ARBITER_KEY   = CA_DIR / "arbiter.key"     # arbiter TLS private key, 0600
ARBITER_KEY_PEM = CA_DIR / "arbiter.key.pem"  # PEM copy for rqlited
ARBITER_CERT  = CA_DIR / "arbiter.crt"     # arbiter cert (SAN = .254)

# Per-node files (every node has these)
NODE_KEY_PEM  = Path("/etc/bedrock/node.key.pem")   # PEM copy of peer_auth's seed
NODE_CERT     = Path("/etc/bedrock/node.crt")       # CA-signed per-node cert
CA_CERT_LOCAL = Path("/etc/bedrock/ca.crt")         # CA cert, replicated copy

# ── Validity ──────────────────────────────────────────────────────
# 100 years. Rotation is operator-driven, not time-driven.
VALIDITY_YEARS = 100


def _atomic_write(path: Path, data: bytes, mode: int = 0o644) -> None:
    """tmp+rename write. Same pattern as peer_auth.ensure_node_key."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.chmod(tmp, mode)
    tmp.rename(path)


def _seed_to_pem(seed: bytes) -> bytes:
    """Convert peer_auth's 32-byte Ed25519 seed to PEM-encoded
    PKCS#8 form that rqlited (Go crypto/tls) can read."""
    if len(seed) != 32:
        raise ValueError(f"seed length {len(seed)} != 32")
    priv = Ed25519PrivateKey.from_private_bytes(seed)
    return priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _validity_window() -> tuple[datetime.datetime, datetime.datetime]:
    now = datetime.datetime.now(datetime.timezone.utc)
    not_before = now - datetime.timedelta(minutes=5)  # absorb clock skew
    not_after = now + datetime.timedelta(days=365 * VALIDITY_YEARS)
    return not_before, not_after


# ── CA generation ─────────────────────────────────────────────────

def generate_ca(cluster_name: str = "bedrock") -> None:
    """Create the cluster CA: Ed25519 private key + 100-year self-signed
    cert. Writes to CA_KEY and CA_CERT_DRBD. Idempotent: if both files
    already exist and parse cleanly, no-op.

    Caller responsibility: the DRBD `cluster` singleton mount must
    already exist at /var/lib/bedrock/cluster (cluster_arbiter
    promote_to_arbiter sequence does this before cluster_init reaches
    this step).
    """
    if CA_KEY.exists() and CA_CERT_DRBD.exists():
        # Cheap sanity check that the existing CA parses.
        load_ca_key()
        load_ca_cert()
        return

    ca_priv = Ed25519PrivateKey.generate()
    not_before, not_after = _validity_window()
    subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, f"{cluster_name}-ca"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Bedrock"),
    ])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)  # self-signed
        .public_key(ca_priv.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=0),
            critical=True,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, content_commitment=False,
                key_encipherment=False, data_encipherment=False,
                key_agreement=False, key_cert_sign=True, crl_sign=True,
                encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .sign(private_key=ca_priv, algorithm=None)  # Ed25519: algo=None
    )

    _atomic_write(
        CA_KEY,
        ca_priv.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ),
        mode=0o600,
    )
    _atomic_write(
        CA_CERT_DRBD,
        cert.public_bytes(serialization.Encoding.PEM),
        mode=0o644,
    )


def load_ca_key() -> Ed25519PrivateKey:
    """Load CA private key. Raises FileNotFoundError if not present
    (caller must have failed-over to this node and not yet promoted,
    or init not yet run)."""
    pem = CA_KEY.read_bytes()
    key = serialization.load_pem_private_key(pem, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError(f"{CA_KEY} is not Ed25519")
    return key


def load_ca_cert() -> x509.Certificate:
    """Load CA cert. Looks at CA_CERT_DRBD first (if mounted), else
    CA_CERT_LOCAL (every-node replica)."""
    if CA_CERT_DRBD.exists():
        pem = CA_CERT_DRBD.read_bytes()
    else:
        pem = CA_CERT_LOCAL.read_bytes()
    return x509.load_pem_x509_certificate(pem)


def publish_ca_cert_to_local() -> None:
    """Copy the CA cert from DRBD (master-only) to the per-node
    /etc/bedrock/ca.crt. Called by master at init + on every saga
    that distributes CA changes. Idempotent."""
    if not CA_CERT_DRBD.exists():
        raise FileNotFoundError(
            f"{CA_CERT_DRBD} missing — DRBD not mounted on master?")
    data = CA_CERT_DRBD.read_bytes()
    if CA_CERT_LOCAL.exists() and CA_CERT_LOCAL.read_bytes() == data:
        return
    _atomic_write(CA_CERT_LOCAL, data, mode=0o644)


# ── Cert signing ──────────────────────────────────────────────────

def _sign_cert(
    subject_cn: str,
    pubkey: Ed25519PublicKey,
    san: list[x509.GeneralName],
    *,
    is_server: bool = True,
    is_client: bool = True,
) -> x509.Certificate:
    """Internal: build + sign a leaf cert with the cluster CA."""
    ca_priv = load_ca_key()
    ca_cert = load_ca_cert()
    not_before, not_after = _validity_window()
    eku = []
    if is_server:
        eku.append(x509.oid.ExtendedKeyUsageOID.SERVER_AUTH)
    if is_client:
        eku.append(x509.oid.ExtendedKeyUsageOID.CLIENT_AUTH)

    builder = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, subject_cn),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Bedrock"),
        ]))
        .issuer_name(ca_cert.subject)
        .public_key(pubkey)
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None),
            critical=True,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, content_commitment=False,
                key_encipherment=False, data_encipherment=False,
                key_agreement=False, key_cert_sign=False, crl_sign=False,
                encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage(eku),
            critical=False,
        )
        .add_extension(
            x509.SubjectAlternativeName(san),
            critical=False,
        )
    )
    return builder.sign(private_key=ca_priv, algorithm=None)


def sign_node_cert(
    node_pubkey_raw: bytes,
    node_name: str,
    loopback_ip: str,
) -> bytes:
    """Sign a per-node TLS cert for ``node_name``. SAN contains the
    node name (DNS) and the loopback IP. Returns PEM-encoded cert.

    Caller is the master (the only node holding the CA key). Joiner
    sends its raw 32-byte Ed25519 pubkey + node identity over the
    mgmt API; master calls this; cert flows back in the join
    response."""
    if len(node_pubkey_raw) != 32:
        raise ValueError(f"node_pubkey_raw length {len(node_pubkey_raw)} != 32")
    pub = Ed25519PublicKey.from_public_bytes(node_pubkey_raw)
    san: list[x509.GeneralName] = [
        x509.DNSName(node_name),
        x509.IPAddress(ipaddress.ip_address(loopback_ip)),
        # 127.0.0.1 so local clients (CLI, mgmt) on each node can dial
        # the local rqlited via https://127.0.0.1:4001 without SAN mismatch.
        x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
    ]
    cert = _sign_cert(node_name, pub, san)
    return cert.public_bytes(serialization.Encoding.PEM)


def generate_arbiter_keypair_and_cert(arbiter_loopback_ip: str) -> None:
    """Generate the arbiter's TLS keypair (separate from any node's
    keypair — the arbiter is a role, not a node) and sign a cert for
    it. Writes to ARBITER_KEY, ARBITER_KEY_PEM, and ARBITER_CERT — all
    on the DRBD volume, so they follow the master role on failover.

    Idempotent: if all three files exist and parse, no-op.
    """
    if (ARBITER_KEY.exists() and ARBITER_KEY_PEM.exists()
            and ARBITER_CERT.exists()):
        # Trust the existing files; rotation goes through operator-overrides.
        return

    arb_priv = Ed25519PrivateKey.generate()
    seed = arb_priv.private_bytes_raw()
    pem = arb_priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    san: list[x509.GeneralName] = [
        x509.IPAddress(ipaddress.ip_address(arbiter_loopback_ip)),
        x509.DNSName("bedrock-arbiter"),
    ]
    cert = _sign_cert("bedrock-arbiter", arb_priv.public_key(), san)

    _atomic_write(ARBITER_KEY, seed, mode=0o600)
    _atomic_write(ARBITER_KEY_PEM, pem, mode=0o600)
    _atomic_write(
        ARBITER_CERT,
        cert.public_bytes(serialization.Encoding.PEM),
        mode=0o644,
    )


# ── Per-node helpers (joiner side) ────────────────────────────────

def install_node_cert(node_cert_pem: bytes, ca_cert_pem: bytes,
                      node_seed: bytes) -> None:
    """Joiner-side: write the master-signed cert + CA cert + a
    PEM-formatted copy of the node's existing peer_auth seed into the
    right places under /etc/bedrock/.

    ``node_seed`` is the same 32-byte raw seed that
    peer_auth.ensure_node_key() returns — caller passes it in so we
    can emit the PEM form rqlited needs without re-reading the file."""
    _atomic_write(NODE_CERT, node_cert_pem, mode=0o644)
    _atomic_write(CA_CERT_LOCAL, ca_cert_pem, mode=0o644)
    _atomic_write(NODE_KEY_PEM, _seed_to_pem(node_seed), mode=0o600)


def write_local_node_key_pem(seed: bytes) -> None:
    """Master at init time: write the PEM copy of its own peer_auth
    seed so its rqlited can read it. (CA cert + node cert are written
    via other paths during init.)"""
    _atomic_write(NODE_KEY_PEM, _seed_to_pem(seed), mode=0o600)
