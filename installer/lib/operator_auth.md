# `operator_auth.py`

**Module purpose.** Argon2id password hashing + JWT issue/verify
for the operator-facing dashboard + `/api/*` endpoints. Operator
credentials live in the rqlite `operators` table: `(username,
salt, password_hash)`. JWTs are short-lived (24 h) and signed
with a per-cluster secret stored in
`/etc/bedrock/operator_jwt.key`.

Distinct from `peer_auth` (inter-node, Ed25519, long-lived) —
this layer is for human operators logging into the dashboard
and CLI scripts setting `BEDROCK_OPERATOR_TOKEN`.

## Constants

- `JWT_KEY_PATH = /etc/bedrock/operator_jwt.key` — 32 random
  bytes, mode 0600. Same on every node (master generates on
  init; joiners receive in join-approval response).
- `JWT_TTL_S = 86400` — 24 h.
- `ARGON2_MEMORY_KB = 65536`, `ARGON2_ITERATIONS = 3`,
  `ARGON2_PARALLELISM = 4` — Argon2id tuning.

## Functions

### Password hashing

- `hash_password(plain: str) -> tuple[salt_hex, hash_hex]` —
  generate 16-byte random salt, run Argon2id, return both as
  hex. Used by `mgmt_install.install_full` (seeds default
  `root` / `admin`) and `/api/operators/set` handler.
- `verify_password(plain: str, salt_hex: str, hash_hex: str)
  -> bool` — recompute Argon2id with the same salt, constant-
  time compare.

### JWT

- `ensure_jwt_key() -> bytes` — generate + write
  `/etc/bedrock/operator_jwt.key` if missing. Returns the
  32-byte key.
- `issue_token(username: str, *, ttl_s=JWT_TTL_S) -> str` —
  sign a `{sub, iat, exp}` JWT with HS256.
- `verify_token(token: str) -> dict | None` — decode + verify
  + check `exp`. Returns the claims dict on success, None on
  failure (bad signature, expired, malformed).

### CLI helpers

- `prompt_password(prompt="Password: ") -> str` — getpass
  wrapper used by the `bedrock` CLI's interactive flows.

## Lifecycle

- `bedrock init` (mgmt_install): generate JWT key, seed default
  `root` / `admin` via `bs.operator_set(salt, hash)`.
- `bedrock join`: agent_install pulls the JWT key from the
  master's join-approval response and writes locally so this
  node can issue valid tokens (e.g. when it becomes mgmt master
  on failover and operators log in to it directly).
- Operator logs in to the dashboard → `POST /api/login` →
  `verify_password` → `issue_token` → 24 h JWT in cookie.
- Subsequent `/api/*` calls carry `Authorization: Bearer <jwt>`
  or `--token` flag from CLI scripts.
