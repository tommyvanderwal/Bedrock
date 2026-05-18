# `rqlite_setup.py`

**Module purpose.** Render the per-node + arbiter `rqlited` env
files that the `bedrock-rqlited.service` +
`bedrock-rqlited-arbiter.service` systemd units source. Owns
the deterministic node-id rule (last octet of loopback /32),
the bootstrap-vs-join logic, and the bind-address conventions
that keep per-node and arbiter rqlite coexisting on the same
host.

The systemd unit's `ExecStartPre` runs `python3 rqlite_setup.py
--render-env` as a safety net so even a manual systemctl restart
re-renders the env if state.json changed in between.

## Constants

- `PER_NODE_ENV = /etc/bedrock/rqlited.env`,
  `ARBITER_ENV = /etc/bedrock/rqlited-arbiter.env`.
- Per-node bind: `0.0.0.0:4001/4002`. The advertised address is
  `<loopback>:4001/4002` so peers route to us via the mesh.
- Arbiter bind: `<.254>:4011/4012`. Uses different ports so it
  can coexist with the per-node unit on the master.
- Per-node data dir: `/var/lib/bedrock/rqlite`.
- Arbiter data dir: `/var/lib/bedrock/cluster/rqlite-arbiter`
  (lives on the DRBD-replicated singleton mount).

## Node-id rule

`BEDROCK_RQLITED_NODE_ID` = the last octet of the node's loopback
IP. So node `.1` is id `1`, node `.42` is id `42`. The id is
**permanent** for the lifetime of the cluster — collisions are
impossible because the CGNAT /24 is per-cluster and the master
allocates loopback indices uniquely.

Earlier (pre-rewrite) sorted-name-index was used here, which
shifted on every join + collided when two nodes happened to have
names that sort-collided. See `lesson_rqlite_node_id_stability`.

## Functions

### Per-node rqlite

- `render_env_file()` — main per-node renderer. Reads
  cluster.json (for peer loopbacks) + state.json (for self's
  loopback + node_name). Writes `PER_NODE_ENV` with:
  - `BEDROCK_RQLITED_NODE_ID = <last-octet>`
  - `BEDROCK_RQLITED_BIND_IP = <loopback_ip>`
  - `BEDROCK_RQLITED_DATA_DIR = /var/lib/bedrock/rqlite`
  - `BEDROCK_RQLITED_JOIN_FLAG = "-join <peer1>:4002,<peer2>:4002,..."`
    (empty on the master at init)
  - `BEDROCK_RQLITED_BOOTSTRAP_FLAG = "-bootstrap-expect 1"`
    (only set on the master at init; cleared after first boot)

  Idempotent: re-rendering with the same inputs produces an
  identical file.

- `clear_bootstrap_flag()` — strip the
  `BEDROCK_RQLITED_BOOTSTRAP_FLAG` line from the env file after
  first successful boot. Called from `mgmt_install` once the
  rqlite Raft has elected. Without this, every restart of the
  master would try to re-bootstrap, which doesn't actually
  work (rqlite ignores the flag if data dir is non-empty) but
  spams the log.

### Arbiter rqlite

- `render_arbiter_env_file()` — renders `ARBITER_ENV` for the
  arbiter rqlite that runs on the master. Different node-id
  space (200..253; arbiter id = `.254 - 1 = 253` minus any
  conflict with operator ids — currently fixed at the loopback
  last octet of `.254` = 254, but bumped to 200+x for clarity).
  Bind to .254:4011/4012.

  `BEDROCK_ARBITER_JOIN_FLAG = "-join …:4012,…:4012,..."` with
  the same logic as per-node — empty at first promote, populated
  on subsequent rejoins.

  Idempotent: only rewritten when the rendered content actually
  changes (avoids triggering a systemd daemon-reload on every
  converge tick).

### CLI

- `__main__`: `python3 rqlite_setup.py --render-env` calls
  `render_env_file()`. Used by the systemd `ExecStartPre`.

## Lifecycle

- `bedrock init`: `mgmt_install.install_full` writes a minimal
  cluster.json, calls `render_env_file()` (per-node, bootstrap
  flag set), `systemctl restart bedrock-rqlited`, waits for
  Raft, seeds schema.
- `bedrock join`: `agent_install.install` writes its own
  state.json + cluster.json (with the master in nodes),
  `render_env_file()` (per-node, join flag pointing at master),
  starts the unit.
- `cluster_arbiter.promote_to_arbiter_host`:
  `render_arbiter_env_file()`, starts
  `bedrock-rqlited-arbiter.service`.
- `cluster_arbiter.demote_arbiter_host`: stops the arbiter unit
  (leaves the env file in place; idempotent re-render on next
  promote).
