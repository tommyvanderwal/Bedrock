# installer/lib/join_handshake.py

Crypto primitives for approval-based node join: an ECDH key exchange that
encrypts the cluster's shared `cluster.key` so only the genuine joiner can read
it, plus an SSH-style fingerprint the operator eyeballs to defeat a man-in-the-
middle. A joiner generates an X25519 ephemeral key and posts a request carrying
its Ed25519 identity fingerprint; the master, on operator approval, `seal`s the
`cluster.key` under a session key derived from the joiner's ephemeral pubkey; the
joiner `open_seal`s it. Called by the join flow on both the master/dashboard side
and the joiner side; it holds no state and touches no files, services, or rqlite.

## Functions / Classes

### `gen_ephemeral() -> tuple[X25519PrivateKey, str]`
Generate a fresh X25519 ephemeral keypair for one handshake.
- **In:** none.
- **Out:** `(priv_obj, pub_b64)` — the private key object and its raw public key, base64-encoded. No side effects.

### `fingerprint(pubkey_hex: str) -> str`
Compute the SSH-style fingerprint of an Ed25519 public key.
- **In:** `pubkey_hex` — the Ed25519 pubkey as a hex string.
- **Out:** `"SHA256:<base64>"` (padding stripped), matching `ssh-keygen -l -E sha256` formatting. No side effects.

### `seal(master_priv, joiner_eph_pub_b64, request_id, cluster_key) -> tuple[str, str]`
Master side: encrypt the cluster key for one approved joiner.
- **In:** `master_priv` — master's X25519 ephemeral private key; `joiner_eph_pub_b64` — joiner's ephemeral pubkey (base64); `request_id` — the join attempt's ID (HKDF salt + AEAD associated data); `cluster_key` — the secret bytes to transfer.
- **Out:** `(ciphertext_b64, nonce_b64)` — ChaCha20-Poly1305 ciphertext and its random 12-byte nonce, both base64. No side effects.

### `open_seal(joiner_priv, master_eph_pub_b64, request_id, ciphertext_b64, nonce_b64) -> bytes`
Joiner side: decrypt and authenticate the cluster key.
- **In:** `joiner_priv` — joiner's X25519 ephemeral private key; `master_eph_pub_b64` — master's ephemeral pubkey (base64); `request_id` — the same ID used at request time; `ciphertext_b64`, `nonce_b64` — the sealed payload.
- **Out:** the decrypted `cluster_key` bytes. Raises if the ciphertext, nonce, AAD, or session key don't all match (tamper / wrong request). No side effects.

### `new_request_id() -> str`
Mint a unique ID for a join attempt.
- **In:** none.
- **Out:** a 24-byte URL-safe base64 random string (padding stripped). No side effects. Also used as the ECDH/HKDF salt.

Private helpers: `_b64` / `_b64_d` (base64 encode/decode), `_derive` (HKDF-SHA256 over the ECDH shared secret — see below).

## How it works

The exchange is a one-shot ECDH-then-AEAD seal, with the `request_id` woven into
both halves so a captured approval payload can't be replayed against a different
attempt.

`_derive` is the shared core: it takes a local X25519 private key plus the
peer's ephemeral pubkey, runs `exchange` to get the raw shared secret, then
HKDF-SHA256s it to a 32-byte key using `request_id` as the **salt** and the
fixed string `b"bedrock join handshake v1"` as `info`. Because both sides feed
the same `request_id`, both derive the same session key; a replay under a
different `request_id` derives a different key and fails to open.

`seal` derives that session key, picks a random 12-byte nonce, and
ChaCha20-Poly1305-encrypts the cluster key with `request_id` as the **associated
data** — so the AEAD tag also covers the request binding. `open_seal` re-derives
the key the same way and decrypts; any mismatch (wrong key, altered ciphertext,
altered nonce, or wrong `request_id` AAD) raises.

```
joiner                                     master + dashboard
──────                                     ──────────────────
gen_ephemeral() -> (j_priv, j_pub)
fingerprint(ed25519_pub) -> SHA256:…       (printed on joiner's own console)
POST /api/join/request {…, j_pub, fp} ──▶  log JOIN_REQUEST
                                           popup on every node's UI;
                                           operator compares fp visually
                                           operator approves
                                           gen_ephemeral() -> (m_priv, m_pub)
                                           seal(m_priv, j_pub, req_id, ck)
                                             -> (ct, nonce)
                                           log JOIN_RESOLVED {m_pub, ct, nonce}
poll /api/join/status?id=…            ◀──  returns approved + m_pub + ct + nonce
open_seal(j_priv, m_pub, req_id, ct, nonce)
  -> cluster.key
proceed with install
```

The session key is derived from the joiner's ephemeral X25519 private half,
which never leaves the joiner. A LAN MITM can swap the X25519 pubkey in the
request, but cannot forge the Ed25519 fingerprint the joiner prints on its own
console; the operator's visual comparison catches the substitution and aborts.

## Why

Salt-binding the HKDF to `request_id` (rather than relying on the nonce alone)
ties the entire session key — not just the ciphertext framing — to one specific
join attempt, so an approval payload is meaningless outside its original request.
