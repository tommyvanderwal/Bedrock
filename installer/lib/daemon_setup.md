# `daemon_setup.py`

**Module purpose.** Cluster-key bootstrap. The historical job —
rendering `/etc/bedrock/daemon.toml` and starting the
bedrock-rust daemon — is gone (Rust deleted in P4 of the May-2026
rewrite). Only the surviving helper lives here.

The HMAC key is a 32-byte shared secret used by `lib.witness.py`
to sign Echo probes/heartbeats/claims. Master generates it on
`bedrock init`; joiners receive it in the join-approval response
and call `write_cluster_key(material)` to persist it.

## Constants

- `CLUSTER_KEY = /etc/bedrock/cluster.key`.

## Functions

- `write_cluster_key(material=None) -> bytes` — idempotent. If
  the file already exists, returns its bytes (preserves the
  master's key across re-runs of `bedrock init`). Otherwise
  uses `material` if given, else generates 32 random bytes via
  `secrets.token_bytes(32)`. Writes mode 0o600. Returns the key
  bytes for the master to ship back in the join-approval JSON.

  Raises `ValueError` if `material` is not exactly 32 bytes.

This module file is intentionally tiny (~30 lines). Callers:

- `mgmt_install.install_full` — `daemon_setup.write_cluster_key()`
  with no args at init.
- `agent_install.install` — `daemon_setup.write_cluster_key(
  bytes.fromhex(approval["cluster_key_hex"]))` at join.

The file name + path stay because both callers import
`from . import daemon_setup`. Renaming it would force a sweep
of the install paths for no real benefit.
