# Logs — where every line lands and how to query

Bedrock has three log channels. Knowing which carries what avoids the
"I don't see my event" rabbit hole.

## The three channels

```
   ┌───────────────────────────────────────────────────────────────┐
   │ 1. push_log()  — mgmt application events                       │
   │                                                                │
   │    mgmt/app.py:push_log() does, in order:                      │
   │      a. WebSocket 'event' broadcast  (instant to all browsers) │
   │      b. VictoriaLogs JSON insert     (persistent, queryable)   │
   │                                                                │
   │    Examples: migrate success, convert steps, join approved,    │
   │    VM start/stop, ISO upload, operator login.                  │
   └───────────────────────────────────────────────────────────────┘

   ┌───────────────────────────────────────────────────────────────┐
   │ 2. Systemd journal  — per-service stdout/stderr                │
   │                                                                │
   │    `journalctl -u <service>`  on each node.                    │
   │    Captures: uvicorn access log, rqlite consensus, DRBD        │
   │    kernel messages, libvirtd, cloud-init, etc.                 │
   │    Host-only; not surfaced in the dashboard.                   │
   └───────────────────────────────────────────────────────────────┘

   ┌───────────────────────────────────────────────────────────────┐
   │ 3. Syslog  → vlagent :5140 → VictoriaLogs                      │
   │                                                                │
   │    Every node runs bedrock-vlagent listening on syslog TCP     │
   │    :5140. It dual-writes to both designated VL backends, so    │
   │    forwarded syslog ends up queryable next to push_log events. │
   │    Pointing a host's rsyslog at :5140 is operator config.      │
   └───────────────────────────────────────────────────────────────┘
```

The dashboard Recent Logs panel shows **channel 1** (push_log). Channels 2
and 3 are host-side tools.

## push_log: what it is and where it lands

There are two `push_log` functions, layered:

- `mgmt/victoria.py:push_log(msg, node, app, level)` — POSTs one JSON-line
  entry to each designated VL backend at `:9428/insert/jsonline`
  (best-effort, silent on failure).
- `mgmt/app.py:push_log(...)` — the one routes call. Broadcasts the entry
  on the WebSocket `event` channel first (so the UI updates with WS latency
  only), then calls the victoria.py one to persist. VL is written second so
  a slow/unreachable backend never stalls the UI.

Every entry carries:

```
_msg     : the message string
_time    : strftime("%Y-%m-%dT%H:%M:%S")
hostname : the node the event is *about* (often != where mgmt runs)
app      : "bedrock-mgmt"
level    : info | warn | error
```

Call sites span `mgmt/app.py` and `mgmt/routes_iso.py`; list them with:

```bash
grep -rn "push_log(" mgmt/
```

push_log is for **operator-meaningful events** — join request/approve, VM
start/shutdown/poweroff/delete, migrate, convert (cattle↔pet↔vipet,
step-by-step), create, disk attach/grow, vcpu/ram/priority change, ISO
upload/eject/insert, import/export jobs, backup target/schedule, operator
login and password changes.

## Backends and ports

- Two designated VL backends per cluster, from `obs_backends.logs` in the
  cluster state. Each runs `bedrock-vl` on `:9428` (HTTP query + JSON
  insert). vlagent on every node dual-writes to both, so either backend has
  the full set — reads hit the first that answers, no merge needed.
- VL backend syslog listener is `:5141`; the per-node vlagent owns the
  operator-facing syslog `:5140` and the JSON insert path.

## Querying VictoriaLogs

VictoriaLogs uses LogsQL. The mgmt app exposes pre-shaped read endpoints
(`mgmt/routes_obs.py`); all default `limit=50, hours=1`:

| Endpoint | LogsQL used |
|---|---|
| `GET /api/logs?query=*&limit=&hours=` | passthrough of `query` |
| `GET /api/logs/node/{name}?limit=&hours=` | `hostname:"<name>"` |
| `GET /api/logs/vm/{name}?limit=&hours=` | `"<vm-name>"` (free-text in `_msg`) |

Or directly against any logs backend (`<vl>` = a node in `obs_backends.logs`):

```bash
# last 20 migration events cluster-wide
curl 'http://<vl>:9428/select/logsql/query?query=_msg:migrated&limit=20'

# all error-level mgmt events in the last hour
now=$(date +%s); start=$((now-3600))
curl "http://<vl>:9428/select/logsql/query?query=level:error&start=$start&limit=100"

# everything from one node
curl 'http://<vl>:9428/select/logsql/query?query=hostname:"bedrock-sim-2.bedrock.local"&limit=200'
```

## Streaming (journalctl -f equivalents)

The dashboard is the closest to `tail -f` for push_log events. For the
systemd journal of any service:

```bash
# unified daemon — uvicorn access, orchestrator/netd, tracebacks
ssh <node> 'journalctl -u bedrock-d -f'

# rqlite — consensus, leader changes
ssh <node> 'journalctl -u bedrock-rqlited -f'

# DRBD kernel messages
ssh <any-node> 'journalctl -kf | grep drbd'

# VM (QEMU) logs
ssh <host-of-vm> 'tail -f /var/log/libvirt/qemu/<vm>.log'
```

## How dashboard pages consume push_log

`mgmt/ui/src/lib/ws.ts` dispatches each frame by its `channel`. The root
layout (`+layout.svelte`) handles `channel: "event"` and prepends to the
global `events` store (capped at the last 100).

Each page with a Recent Logs panel:

- **Overview** (`/`): all `events` + seeded history from `/api/logs?limit=30&hours=1`.
- **VM detail** (`/vm/<name>`): `events` whose `_msg` contains `<vm-name>`,
  plus `/api/logs/vm/<name>?limit=50&hours=4`.
- **Node detail** (`/node/<name>`): `events` whose `hostname` or `_msg`
  contains the node's short name, plus `/api/logs/node/<name>?limit=50&hours=4`.

Live entries appear with WS latency only (~ms on LAN). Seeded history is
fetched once on mount; after that the panel is push-driven.

## Retention

- **VictoriaLogs**: VictoriaLogs default retention (no `-retentionPeriod`
  flag set). Storage at `/opt/bedrock/data/vl/`.
- **VictoriaMetrics**: 90 days (`-retentionPeriod=90d`). Storage at
  `/opt/bedrock/data/vm/`.
- **systemd journal**: per-unit defaults (size-capped). Trim with
  `journalctl --vacuum-time=30d`.

## What's deliberately *not* in push_log

- State-push-loop ticks (would flood).
- Periodic VictoriaMetrics scrapes (always-on noise).
- Per-TCP paramiko auth chatter (DEBUG-level only).
- DRBD kernel messages (too high-volume; see `journalctl -k`).
- Guest OS activity (arrives via syslog → vlagent, a separate channel).

push_log is for state transitions that change what the cluster is doing.
Everything else is journal-only.
