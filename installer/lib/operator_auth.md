# installer/lib/operator_auth.py

Operator login and cluster-wide session tokens for the LAN management API on
8443. It hashes operator passwords (PBKDF2-HMAC-SHA256) and mints/verifies
JWT-HS256 bearer tokens signed with `/etc/bedrock/cluster.key`. Because every
node holds the same `cluster.key`, a token minted on one node verifies on any
node — no per-node session store. `mgmt/app.py` is the sole caller: it runs
`verify_password` + `mint_token` for `POST /api/login`, calls `verify_token` to
authenticate Bearer-token requests against the mgmt API, and uses
`hash_password` when an operator's credentials are set. The operator records
holding `salt`/`hash` live in the replicated cluster state; this module only does
the credential math and is handed the stored salt/hash by the caller.

## Functions / Classes

### `hash_password(password, salt=None) -> tuple[str, str]`
Derive a storable password hash.
- **In:** `password` cleartext; `salt` optional 16-byte salt — generated with
  `secrets.token_bytes(16)` when omitted.
- **Out:** `(salt_hex, hash_hex)` — both hex strings; the hash is 32-byte
  PBKDF2-HMAC-SHA256 over the UTF-8 password with `PBKDF2_ITER = 200_000`
  iterations. No side effects.

### `verify_password(password, salt_hex, hash_hex) -> bool`
Constant-time check of a cleartext password against a stored salt+hash.
- **In:** `password` cleartext; `salt_hex`, `hash_hex` as produced by
  `hash_password` (hex strings; treated as `""` if falsy).
- **Out:** `True` only if the recomputed hash matches via `hmac.compare_digest`.
  Returns `False` on non-hex input or empty salt/hash. No side effects.

### `mint_token(user, ttl_s=TOKEN_TTL_S) -> tuple[str, int]`
Issue a signed session token for an authenticated operator.
- **In:** `user` subject string; `ttl_s` lifetime in seconds (default
  `TOKEN_TTL_S` = 8h, covering an operator shift).
- **Out:** `(token, exp_unix)` where `token` is `header.payload.signature`
  (base64url, no padding) and `exp_unix` is the integer expiry. Reads
  `/etc/bedrock/cluster.key` to HMAC-sign; raises `RuntimeError` if that file is
  absent. No other side effects.

### `verify_token(token) -> dict`
Validate a bearer token's signature and expiry.
- **In:** `token` string (the full `header.payload.signature`).
- **Out:** the decoded payload dict (`sub`, `iat`, `exp`) on success. Reads
  `/etc/bedrock/cluster.key`. Raises `ValueError` for a malformed token, bad
  signature, unparseable payload, or reached expiry; raises `RuntimeError` if
  `cluster.key` is absent.

Module constants: `TOKEN_TTL_S` (8h token lifetime), `REFRESH_WINDOW_S` (3600;
the last hour, in which the frontend may refresh), `PBKDF2_ITER` (200k),
`CLUSTER_KEY_PATH` (`/etc/bedrock/cluster.key`).

Private helpers: `_b64u` / `_b64u_d` (base64url encode/decode with padding
fix-up); `_cluster_key` (reads `CLUSTER_KEY_PATH`, raises `RuntimeError` if
missing).

## How it works

Two independent pieces — password storage and token sessions — that meet only at
login time.

Password side: `hash_password` salts and stretches the password with 200k
PBKDF2-HMAC-SHA256 rounds and returns hex strings the caller persists in the
operator record. `verify_password` re-derives the hash from the supplied salt and
compares with `hmac.compare_digest` so timing does not leak how many bytes
matched. Malformed hex or empty material returns `False` rather than raising.

Token side: tokens are standard JWT-HS256.

```
header   = {"alg":"HS256","typ":"JWT"}          ─┐ base64url, no '='
payload  = {"sub":user,"iat":now,"exp":now+ttl}  │ compact JSON (",",":")
                                                  │
signing_input = b64u(header) "." b64u(payload)   ─┘
signature     = HMAC-SHA256(cluster.key, signing_input)

token = b64u(header) "." b64u(payload) "." b64u(signature)
```

`verify_token` mirrors that construction in order: it rejects anything without
exactly two dots, recomputes the HMAC over the `header.payload` prefix, compares
it constant-time against the decoded signature, then decodes the payload and
rejects it when `now >= exp`. Each failure mode raises a distinct `ValueError`
message; the signature is checked before the payload is parsed, so a tampered
token never reaches `json.loads` and an attacker cannot extend a token's life by
editing `exp`. `REFRESH_WINDOW_S` is a published constant the frontend uses to
decide when to refresh inside the token's last hour; this module does not act on
it.

## Why

HS256 with the shared `cluster.key` means any node verifies any node's tokens
with the secret it already trusts for the witness AEAD — no asymmetric session
keys and no replicated session table. The JWT shape is chosen so any
browser/CLI/library can carry the token, and an external IdP could later replace
the issuer without a wire-format change.
