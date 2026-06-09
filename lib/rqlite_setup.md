# installer/lib/rqlite_setup.py

Per-node rqlite configuration materialiser. It reads the local bootstrap files
(`/etc/bedrock/cluster.json` for the peer list, `/etc/bedrock/state.json` for
this node's identity) and writes the env file that the `bedrock-rqlited` systemd
unit sources at every start. Because rqlite can't report its own peer set before
it starts, the node-id, bind IP, and `-join`/`-bootstrap-expect` flags are
derived here and handed to rqlited as `${BEDROCK_RQLITED_*}` variables. It also
materialises a separate env (`rqlited-arbiter.env`) for the `.254` arbiter
rqlite. Callers: the `bedrock-rqlited.service` unit (via the `--render-env`
CLI), `bedrock init` / `agent_install` during cluster formation, and
`cluster_arbiter.promote_to_arbiter_host()` for the arbiter env.

## Constants

- `CLUSTER_JSON` = `/etc/bedrock/cluster.json`, `STATE_JSON` = `/etc/bedrock/state.json`.
- `RQLITED_ENV` = `/etc/bedrock/rqlited.env`, `DATA_DIR` = `/var/lib/bedrock/rqlite`.
- `ARBITER_ENV` = `/etc/bedrock/rqlited-arbiter.env`,
  `ARBITER_DATA_DIR` = `/var/lib/bedrock/cluster/rqlite`.
- `RAFT_PORT` = 4002, `HTTP_PORT` = 4001.
- `ARBITER_NODE_ID` = 254.

## Functions / Classes

### `render_env_file(*, cluster_path, state_path, env_path, data_dir) -> dict`
Write the per-node rqlited env from current `cluster.json` + `state.json`.
- **In:** keyword-only path overrides, all defaulting to the module constants
  (`CLUSTER_JSON`, `STATE_JSON`, `RQLITED_ENV`, `DATA_DIR`).
- **Out:** returns the rendered env dict and atomically writes it to `env_path`
  (temp file + `os.replace`). Creates `data_dir` (mode `0700`). Idempotent —
  identical inputs produce an identical file. May call
  `state.recover_identity_from_cluster_json()` as a self-heal side effect.
  Raises `RuntimeError` if identity is still unresolvable after self-heal, if the
  node-id can't be parsed from the loopback, if the node isn't yet in
  `cluster.json`, or if there are no peers and this node is not the solo
  mgmt-master.

### `render_arbiter_env_file(*, cluster_path, state_path, env_path, data_dir) -> dict`
Write the arbiter rqlited env (`/etc/bedrock/rqlited-arbiter.env`) for the
`.254` arbiter rqlite daemon.
- **In:** keyword-only path overrides defaulting to module constants
  (`ARBITER_ENV`, `ARBITER_DATA_DIR`).
- **Out:** returns the rendered env dict and atomically writes it to `env_path`;
  creates `data_dir` (mode `0700`). Imports `cluster_arbiter` to resolve the
  arbiter IP. Raises `RuntimeError` if the arbiter IP is unknown (no
  `cluster_uuid` in `cluster.json`) or if no peers are known.

### `cli() -> int`
Argparse entry point (`python3 rqlite_setup.py …`); the module's `__main__`
returns its exit code.
- **In:** one required, mutually-exclusive flag — `--render-env`, `--init`, or
  `--join LEADER_LOOPBACK`.
- **Out:** process exit code. `--render-env` renders and prints the env (0).
  `--init` renders and checks a `-bootstrap-expect` flag is present (0, else 2
  with a stderr message). `--join` renders (falling back to a sentinel env if
  rendering raises), overrides `BEDROCK_RQLITED_JOIN_FLAG` to point at the given
  leader loopback, clears the bootstrap flag, and writes the env directly (0).
  Any `RuntimeError` during rendering prints to stderr and returns 1.

### Private helpers
- `_read_json(path)` — best-effort JSON load; returns `{}` on `OSError` / decode error.
- `_sorted_node_index(cluster, node_name)` — 1-based index of the node in the
  name-sorted node list, or `None` if absent. (The rqlite node-id itself comes
  from the loopback's last octet, not this index.)
- `_peer_loopbacks(cluster, my_node)` — sorted list of every *other* node's
  `loopback_ip` (no port suffix); empty at N=1.

## How it works

`render_env_file` resolves identity, then chooses one of three outcomes:

```
read cluster.json + state.json
        │
        ▼
 node_name / loopback_ip present in state.json?
   │ no                              │ yes
   ▼                                 │
 state.recover_identity_            │
   from_cluster_json()  (best-effort)│
 re-read state.json                  │
   │ still missing?                  │
   ▼ yes                             │
 RuntimeError                        │
                                     ▼
        node_idx = int(last octet of loopback_ip)   (RuntimeError if unparseable)
                                     │
                                     ▼
                   node in cluster.json "nodes"? ── no ──▶ RuntimeError
                                     │ yes
                                     ▼
        ┌─────────────────── flag selection ───────────────────┐
        │ len(nodes)==1 & me & "mgmt" in role  (is_solo_master) │
        │   → BOOTSTRAP_FLAG = "-bootstrap-expect 1"            │
        │     JOIN_FLAG      = ""                                │
        │ else if peers exist                                    │
        │   → JOIN_FLAG = "-join ip:4002,ip:4002,…"              │
        │     BOOTSTRAP_FLAG = ""                                 │
        │ else (no peers, not solo master)                       │
        │   → RuntimeError (wait for cluster.json to settle)     │
        └─────────────────────────────────────────────────────────┘
                                     │
                                     ▼
        mkdir data_dir 0700 → write env.tmp → os.replace → env file
```

Emitted per-node env keys: `BEDROCK_RQLITED_NODE_ID`, `BEDROCK_RQLITED_BIND_IP`
(the loopback `/32`), `BEDROCK_RQLITED_DATA_DIR`, and exactly one populated flag
of `BEDROCK_RQLITED_BOOTSTRAP_FLAG` / `BEDROCK_RQLITED_JOIN_FLAG` (the other is
an empty string, so the unit's ExecStart can include it unconditionally and an
empty value means "no flag").

The node-id is the loopback's last octet, which is permanent and unique per node
(the cluster's `/24` is per-cluster and indices are allocated sequentially on
join), so it stays stable across the cluster's lifetime — rqlite cannot change a
node-id once it is in the Raft store. The "no peers and not solo master" branch
deliberately raises rather than emitting flags, so the service waits instead of
spuriously forming a second single-node cluster.

`render_arbiter_env_file` builds its peer list local-first: this node's own
loopback, then every other peer (deduped). It always emits `-join` against those
peers at `RAFT_PORT` (4002), never a bootstrap flag, because the per-node rqlite
cluster is already formed by the time the arbiter joins. Its env keys are
`BEDROCK_ARBITER_NODE_ID` (254), `BEDROCK_ARBITER_BIND_IP` (the `.254`),
`BEDROCK_ARBITER_DATA_DIR`, `BEDROCK_ARBITER_BOOTSTRAP_FLAG` (empty), and
`BEDROCK_ARBITER_JOIN_FLAG`. Listing the local peer first keeps the worst-case
join time short instead of stalling on a 30s TCP connect to an unreachable
remote peer.

The `--join` CLI path renders normally when possible; if rendering raises (the
joiner's `cluster.json` hasn't replicated yet), it falls back to a sentinel env
(node-id `0`, bind `0.0.0.0`) and then forces `BEDROCK_RQLITED_JOIN_FLAG` at the
operator-supplied leader loopback — the caller re-renders once `cluster.json`
catches up.

Both env writes are atomic (temp file in the same directory + `os.replace`) so a
concurrent read by systemd never sees a half-written file.

## Why

The flags depend on this node's place in the cluster, which only the local
bootstrap files know before rqlited starts; deriving them here keeps the systemd
unit a static template that just substitutes `${BEDROCK_RQLITED_*}`. The arbiter
gets its own env path and node-id (254) so its rqlited reads a separate config
and never collides with the per-node rqlited on the same host.
