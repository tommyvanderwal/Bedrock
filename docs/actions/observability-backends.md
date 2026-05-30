# Manage observability backends

Bedrock's metrics + logs stack is HA by running **two** backend nodes for each:
two nodes run a VictoriaMetrics backend (metrics) and two run a VictoriaLogs
backend (logs), while **every** node runs the lightweight agents
(`vmagent`/`vlagent`) that ship to both backends. Lose one backend and the other
keeps serving every query; the agents' on-disk queues cover the gap. You manage
which nodes hold the backend slots here.

This is normally automatic: `bedrock init` makes the first node a backend, and
the **second** node to join is auto-appointed the 2nd backend (seeded from the
first). You only reach for these commands to **move** a backend — e.g. before
decommissioning a backend node, or to rebalance.

**Triggered by:**

- CLI: `bedrock observability status`, `bedrock observability promote <node> [--replace <existing>]`
- HTTP: `POST /api/observability/backends`

**Source:** `installer/bedrock:cmd_observability`;
`mgmt/app.py:/api/observability/backends`;
`installer/lib/observability.py:seed_backend` (the vmbackup→vmrestore seed);
`obs_backends` field in rqlite cluster state. Background:
[`project_victorialogs_ha`] in the design notes.

## See the current layout

```
bedrock observability status
```

Prints the metrics backends, the logs backends, and the agent-only nodes:

```
Cluster: prod
  Metrics backends: ['node-1', 'node-2']
  Logs backends:    ['node-1', 'node-2']
  Agent-only nodes:
    - node-3
    - node-4
```

(Verify from the outside with `systemctl is-active bedrock-vm` / `bedrock-vl` —
active on exactly the 2 backend nodes; `bedrock-vmagent` / `bedrock-vlagent`
active on **all** nodes.)

## Move / add a backend

```
bedrock observability promote node-3                  # if a slot is free
bedrock observability promote node-3 --replace node-2 # both slots full → swap
```

- Adds `node-3` to the backend set. If both slots are already full, `--replace
  <existing>` is **required** to name which node to drop (you can't run 3
  backends — the model is exactly 2).
- **Seed-before-flip (no data gap):** the mgmt API runs the
  `vmbackup`→`vmrestore` seed from an existing backend into the new node's data
  dir **before** writing the new assignment to `obs_backends`. So the moment the
  new backend appears in cluster state and starts serving, it already holds the
  history — queries don't see a hole. (VictoriaLogs has no snapshot API, so its
  new backend starts fresh and backfills from the agents' queues.)
- Needs an operator token (prompts for the operator login, or
  `BEDROCK_OPERATOR_TOKEN`).

## Recover a lost backend

If a backend node dies or you `bedrock node leave` it, the cluster drops to one
backend for that signal — still serving, but no longer HA. Restore HA by
promoting a healthy agent-only node into the freed slot:

```
bedrock observability status                 # confirm it shows only 1 backend
bedrock observability promote node-4         # fill the slot (seeds from the survivor)
```

If you're decommissioning a backend node, promote the replacement **first**
(while the old one is still up to seed from), then `bedrock node leave` the old
one.

## Preconditions

- rqlite reachable with a leader (the assignment is a committed cluster-state
  change).
- For the seed: the source backend is up and reachable from the target so
  `vmbackup`/`vmrestore` (metrics) can copy the data dir.

## Failure modes and recovery

| Symptom | Cause | Recovery |
|---|---|---|
| `both backend slots full; pass --replace` | Tried to add a 3rd backend | Re-run with `--replace <node-to-drop>`. |
| `--replace <X> not in current backend list` | Named a node that isn't currently a backend | `bedrock observability status` for the exact names. |
| New backend serves empty metrics for a while | Seed skipped/failed (source unreachable) | Re-run promote with the source backend up; or let agents backfill. |
| Only 1 backend after a node loss | Backend node down/left | Promote an agent-only node to refill the slot (above). |

## Operator perspective

- **Typical duration**: a few seconds plus the seed (metrics data-dir copy —
  seconds to a minute depending on retention).
- Day-to-day you won't touch this: joins auto-maintain 2 backends. It's a
  recovery / rebalance tool.
- The dashboard surfaces backend health per node; this CLI is the authoritative
  way to reassign slots.

See also: [`node-decommission.md`](node-decommission.md) (promote a replacement
backend *before* leaving a backend node) and
[`join-cluster.md`](join-cluster.md) (the 2nd joiner is auto-appointed).
