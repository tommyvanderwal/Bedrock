# installer/lib/cluster_ca.py

The cluster CA: one certificate authority per cluster that issues the TLS certs
backing rqlite mTLS. The CA private key lives on the DRBD `cluster` singleton
volume (`/var/lib/bedrock/cluster/ca`), so signing authority follows the master
role — only the current master can sign new joiner certs. The CA public cert is
replicated to every node (`/etc/bedrock/ca.crt`) so every rqlited can verify its
peers. `cluster_init` calls `generate_ca` + `generate_arbiter_keypair_and_cert`
on the master; `node_join` calls `sign_node_cert` on the master and
`install_node_cert` on the joiner. Each node's TLS keypair is its existing
`peer_auth` Ed25519 keypair, wrapped in a CA-signed cert (no second keypair).

## Functions / Classes

Path constants (referenced by `cluster_init`, `node_join`, and the rqlited
systemd units): `CA_DIR`, `CA_KEY` (CA private key, 0600), `CA_CERT_DRBD` (CA
cert on DRBD), `ARBITER_KEY` / `ARBITER_KEY_PEM` / `ARBITER_CERT` (arbiter TLS
material on DRBD), and the per-node files `NODE_KEY_PEM`, `NODE_CERT`,
`CA_CERT_LOCAL`. `VALIDITY_YEARS = 100`.

### `generate_ca(cluster_name: str = "bedrock") -> None`
Create the cluster CA — an Ed25519 private key plus a 100-year self-signed CA
cert.
- **In:** `cluster_name` → used in the cert's COMMON_NAME (`<name>-ca`).
- **Out:** returns nothing. Writes `CA_KEY` (PEM PKCS#8, 0600) and
  `CA_CERT_DRBD` (PEM, 0644) atomically. Idempotent: if both files already exist
  and parse cleanly (verified via `load_ca_key` + `load_ca_cert`), it no-ops.
  Caller must have the DRBD `cluster` mount present at
  `/var/lib/bedrock/cluster`.

### `load_ca_key() -> Ed25519PrivateKey`
Load the CA private key from `CA_KEY`.
- **In:** none.
- **Out:** the `Ed25519PrivateKey`. Raises `FileNotFoundError` if `CA_KEY` is
  absent, `ValueError` if it is not Ed25519.

### `load_ca_cert() -> x509.Certificate`
Load the CA cert, preferring the DRBD copy.
- **In:** none.
- **Out:** the parsed `x509.Certificate`. Reads `CA_CERT_DRBD` if it exists,
  otherwise `CA_CERT_LOCAL`.

### `publish_ca_cert_to_local() -> None`
Copy the CA cert from the DRBD volume to this node's `/etc/bedrock/ca.crt`.
- **In:** none.
- **Out:** returns nothing. Writes `CA_CERT_LOCAL` (0644) atomically. Raises
  `FileNotFoundError` if `CA_CERT_DRBD` is missing (DRBD not mounted on master).
  Idempotent: no-ops when the local copy already matches byte-for-byte.

### `sign_node_cert(node_pubkey_raw: bytes, node_name: str, loopback_ip: str) -> bytes`
Sign a per-node leaf TLS cert with the cluster CA. Called by the master.
- **In:** `node_pubkey_raw` → the joiner's raw 32-byte Ed25519 pubkey;
  `node_name` → the cert's CN and a DNS SAN; `loopback_ip` → the node's `/32`,
  added as an IP SAN.
- **Out:** PEM-encoded leaf cert (bytes). SAN = `{node_name (DNS), loopback_ip,
  127.0.0.1}` — the 127.0.0.1 entry lets local clients dial
  `https://127.0.0.1:4001` without a SAN mismatch. Raises `ValueError` if the
  pubkey is not 32 bytes. Loads the CA key/cert; signs in memory; writes no
  files.

### `generate_arbiter_keypair_and_cert(arbiter_loopback_ip: str) -> None`
Generate the arbiter's own Ed25519 keypair (the arbiter is a role, not a node)
and sign its TLS cert.
- **In:** `arbiter_loopback_ip` → the arbiter VIP, added as an IP SAN.
- **Out:** returns nothing. Writes `ARBITER_KEY` (raw 32-byte seed, 0600),
  `ARBITER_KEY_PEM` (PEM PKCS#8, 0600), and `ARBITER_CERT` (PEM, 0644) — all on
  the DRBD volume, so they follow the master on failover. SAN =
  `{arbiter_loopback_ip, "bedrock-arbiter" (DNS)}`, CN `bedrock-arbiter`.
  Idempotent: no-ops when all three files exist.

### `install_node_cert(node_cert_pem: bytes, ca_cert_pem: bytes, node_seed: bytes) -> None`
Joiner-side: drop the master-signed cert, the CA cert, and a PEM copy of this
node's key into `/etc/bedrock/`.
- **In:** `node_cert_pem` → master-signed leaf cert; `ca_cert_pem` → CA cert;
  `node_seed` → this node's raw 32-byte `peer_auth` seed (passed in by caller).
- **Out:** returns nothing. Atomically writes `NODE_CERT` (0644),
  `CA_CERT_LOCAL` (0644), and `NODE_KEY_PEM` (PEM PKCS#8 of the seed, 0600).

### `write_local_node_key_pem(seed: bytes) -> None`
Master at init: write the PEM copy of its own `peer_auth` seed for its rqlited.
- **In:** `seed` → this node's raw 32-byte Ed25519 seed.
- **Out:** returns nothing. Atomically writes `NODE_KEY_PEM` (0600). (The CA
  cert and this node's leaf cert are written via other init paths.)

Private helpers: `_atomic_write(path, data, mode)` does a tmp+rename write (same
pattern as `peer_auth.ensure_node_key`); `_seed_to_pem(seed)` converts a 32-byte
Ed25519 seed to PEM PKCS#8 that rqlited (Go `crypto/tls`) can read, raising
`ValueError` on a non-32-byte seed; `_validity_window()` returns
`(not_before, not_after)` where `not_before` is 5 minutes in the past (clock-skew
absorption) and `not_after` is 100 years out; `_sign_cert(...)` builds and signs
a leaf cert (BasicConstraints CA=false, KeyUsage digital_signature, EKU
server/client auth, plus the SAN).

## How it works

The CA private key and the arbiter material live on the DRBD `cluster`
singleton; the CA cert is also replicated to every node. This split is what makes
the trust model survive failover and avoid join-time churn:

```
  master node                            joiner node
  ───────────                            ───────────
  CA_KEY        (DRBD, signs)
  CA_CERT_DRBD  (DRBD) ──publish──▶ CA_CERT_LOCAL (/etc/bedrock/ca.crt)
  ARBITER_*     (DRBD, follows master)

  node join:
    joiner --(raw 32-byte Ed25519 pubkey + name + loopback)--> master
    master: sign_node_cert() -> leaf PEM
    master --(leaf PEM + ca.crt)--> joiner
    joiner: install_node_cert() writes NODE_CERT, CA_CERT_LOCAL,
            NODE_KEY_PEM  (no rqlited restart on existing nodes)
```

Every leaf cert (node and arbiter) is signed by the one CA, so an existing node
already trusts a freshly-signed joiner cert — no trust-bundle edit and no rolling
rqlited restart on join. The only event needing coordinated restarts is an
explicit operator CA rotation.

All writes go through `_atomic_write` (tmp + chmod + rename), so a crash mid-write
never leaves a half-written key or cert. The generation entry points
(`generate_ca`, `generate_arbiter_keypair_and_cert`) are idempotent: they detect
existing files and return early, so re-running init or a resumed saga does not
clobber live material. `generate_ca` goes further and parses the existing
key/cert to confirm they are usable before short-circuiting.

Each node reuses its existing `peer_auth` Ed25519 keypair
(`/etc/bedrock/node.{key,pub}`) for TLS. rqlited reads PEM, not the raw 32-byte
seed, so the seed is converted once via `_seed_to_pem` and stored alongside as
`NODE_KEY_PEM`; the matching CA-signed `NODE_CERT` carries the public half. The
arbiter is the exception — it has its own keypair because it is a role that moves
between nodes, not tied to any single node's identity.

`load_ca_cert` reads the DRBD copy when mounted (master) and falls back to the
replicated `/etc/bedrock/ca.crt` otherwise, so verification works on any node
regardless of which one currently holds the DRBD volume.

## Why

One cluster CA instead of a per-node trust list keeps `node join` restart-free:
existing nodes trust anything CA-signed. Certs are valid for 100 years and rotate
only on explicit operator action, because a silent time-based expiry that breaks
a healthy cluster is the exact failure mode to avoid.
