# installer/lib/daemon_setup.py

Cluster-key bootstrap. Owns `/etc/bedrock/cluster.key`, the 32-byte shared secret
every node uses to HMAC-sign and verify mesh traffic (HMAC-SHA256 over the UDP
probe/advert/heartbeat). Called at install time by `mgmt_install` on the master and
`agent_install` on a joiner: the master mints the key and ships the bytes back over
the join handshake, and the joiner persists those same bytes so both ends share one
secret.

## Functions / Classes

### `write_cluster_key(material: bytes | None = None) -> bytes`
Write (or preserve) the node's cluster HMAC key.
- **In:** `material` — an optional 32-byte key to install (the master's key, handed
  to a joiner). When omitted, a fresh random 32-byte key is generated via
  `secrets.token_bytes(32)`.
- **Out:** returns the 32-byte key. Side effects: creates `/etc/bedrock` (parents)
  if needed and writes `/etc/bedrock/cluster.key` mode `0o600`. If the key file
  already exists, it is left untouched and its bytes are returned (idempotent).
  Raises `ValueError` if a supplied `material` is not exactly 32 bytes. No
  subprocess, no rqlite, no services.

## How it works

```
write_cluster_key(material)
        |
        v
  cluster.key exists? --yes--> read_bytes() -> return  (no write, material ignored)
        |
        no
        v
  mkdir -p /etc/bedrock
  key = material or secrets.token_bytes(32)
  len(key) == 32 ? --no--> raise ValueError
        |
        yes
        v
  write_bytes(key); chmod 0o600 -> return key
```

The existence check runs first, so a re-run never rotates or clobbers an established
key — the call is safe on every install/join, and the length validation only runs on
a genuine first write. The master calls it with no `material` to generate the secret
and returns the bytes; the joiner calls it with those exact bytes so the whole
cluster holds one key.

## Why

A single stable shared secret must exist on every node so HMAC-signed mesh traffic
verifies across the cluster; the idempotent write keeps that key constant across
re-runs and reboots rather than regenerating it.
