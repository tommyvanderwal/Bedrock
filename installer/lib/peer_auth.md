# installer/lib/peer_auth.py

Per-node Ed25519 identity and request signing for inter-node API auth. Each node owns a keypair at `/etc/bedrock/node.{key,pub}`; the public key is registered (`/api/nodes/register`) and replicated to every node's snapshot via the log, so any node can verify any peer's signature without a side channel. Outgoing mgmt/installer code calls `request()` to talk to another node's API; the receiving side calls `verify()` to authenticate a peer's `Authorization` header. Signing binds the method, path (with query string), SHA-256 of the body, a timestamp, and the node name, so a tampered body or stale request fails verification.

The header carried on every signed request:

```
Authorization: Bedrock-Ed25519 <node_name>:<ts>:<sig_b64>
```

## Functions / Classes

### `ensure_node_key() -> tuple[bytes, bytes]`
Return this node's `(priv_seed, pub_bytes)`, both 32-byte raw representations; generate the keypair on first call, idempotent afterward.
- **In:** none.
- **Out:** `(priv_seed, pub_bytes)`. Side effects: on first use, writes `/etc/bedrock/node.key` (mode `0600`, via tmp+rename) and `/etc/bedrock/node.pub` (mode `0644`); also rewrites `node.pub` if it is missing or disagrees with the derived public key. Raises `ValueError` if an existing `node.key` is not 32 bytes.

### `pubkey_hex() -> str`
This node's public key as a hex string (calls `ensure_node_key()`).
- **In:** none.
- **Out:** 64-char hex string. Side effect: may generate/write the keypair via `ensure_node_key()`.

### `sign(method, path, body, node_name) -> str`
Build a signed `Authorization` header value for an outgoing request.
- **In:** `method` HTTP verb; `path` request path (caller includes any query string); `body` raw request bytes; `node_name` this node's name, embedded in the canonical string and header.
- **Out:** header string `"Bedrock-Ed25519 <node_name>:<ts>:<sig_b64>"` where `ts` is the current unix time and `sig` is `Ed25519_sign(privkey, canonical)`. Side effect: may generate/write the keypair via `ensure_node_key()`.

### `verify(header, method, path, body, pubkey_lookup) -> str`
Authenticate an incoming `Authorization` header; return the verified node name or raise.
- **In:** `header` the raw header value; `method`/`path`/`body` of the received request (must match what was signed); `pubkey_lookup` callable `node_name -> Optional[bytes]` returning that node's 32-byte raw pubkey (or `None`).
- **Out:** verified `node_name` on success. Raises `ValueError` on: missing/wrong scheme, malformed header, timestamp skew greater than `TOKEN_TTL_S`, no pubkey on file for the node, or invalid signature. No side effects.

### `request(method, url, body, node_name, timeout=10.0) -> dict`
Sign and send a request to another node's mgmt API; return parsed JSON.
- **In:** `method` HTTP verb; `url` full target URL; `body` dict (JSON-encoded) or `None` (no body); `node_name` this node's name for signing; `timeout` seconds.
- **Out:** parsed JSON `dict` (empty dict if the response body is empty). Side effects: signs via `sign()` (may write the keypair); performs an HTTP(S) request via `urllib`. For `https` URLs, TLS certificate/hostname verification is disabled (`CERT_NONE`, `check_hostname=False`).

### Private helpers
- `_canonical(method, path, body, ts, node)` — build the byte string that is signed/verified: `"<METHOD> <PATH>\n<sha256_hex(body)>\n<ts>\n<node>"`.

## Constants

- `NODE_KEY = /etc/bedrock/node.key` — 32-byte raw private seed, mode `0600`.
- `NODE_PUB = /etc/bedrock/node.pub` — 32-byte raw public key, mode `0644`.
- `TOKEN_TTL_S = 300` — accepted timestamp skew window (±5 min); replay bound.
- `SCHEME = "Bedrock-Ed25519"` — `Authorization` scheme prefix.

## How it works

Canonical string (signed by `sign`, recomputed by `verify`):

```
<METHOD> <PATH>\n<sha256_hex(body)>\n<ts>\n<node_name>
```

The body is never signed directly; only its SHA-256 hex digest is, so a tampered body produces a different canonical string and fails verification. `PATH` is the path plus raw query string (so `/a?b=c` is matched as-is), which `request()` reconstructs from the parsed URL and the caller of `verify()` must supply consistently.

Key material is lazy and crash-safe. `ensure_node_key()` reads `node.key` if present (rejecting any file that is not exactly 32 bytes), otherwise generates a fresh Ed25519 private key, writes the 32-byte seed to a `.tmp` sibling at mode `0600`, then renames it over `node.key` so a crash mid-write can never leave a half-written key. The public key (`node.pub`, mode `0644`) is derived from the seed and rewritten whenever it is absent or stale.

Verify path and its guards, in order:

```
header  ──► startswith "Bedrock-Ed25519 " ? ─no─► ValueError (wrong scheme)
        │
        ▼
   split "<node>:<ts>:<sig_b64>" + b64decode ─fail─► ValueError (malformed)
        │
        ▼
   |now - ts| <= TOKEN_TTL_S (300s) ? ─no─► ValueError (outside window)
        │
        ▼
   pubkey_lookup(node) -> 32 bytes ? ─none─► ValueError (no pubkey)
        │
        ▼
   Ed25519.verify(sig, canonical) ─bad─► ValueError (signature invalid)
        │
        ▼
   return node_name
```

The `TOKEN_TTL_S = 300` window (±5 min skew) bounds replay: a captured header stops being accepted once the timestamp ages past the window.

`request()` is the outgoing convenience wrapper: it JSON-encodes the body (empty bytes if `None`), reconstructs the signed path including query, sets `Authorization` via `sign()`, adds `Content-Type: application/json` only when there is a body, and issues the call with `urllib`. For `https` it supplies the insecure SSL context.

## Why

Peer trust comes from the Ed25519 signature, not TLS PKI — the cert on 8443 is for `<dashed-ip>.my.local-ip.co`, never the bare IP a peer dials, so certificate verification is intentionally off and authentication rests on the signature instead. The threat model is a LAN attacker forging requests as a cluster member; it deliberately does not defend against a compromised node, whose pubkey is in the snapshot and which may legitimately sign anything as itself.
