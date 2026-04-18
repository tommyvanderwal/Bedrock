# Logs — where every line lands and how to query

Bedrock has three overlapping log channels. Understanding which one is
which avoids the "I don't see my event" debugging rabbit hole.

## The three channels

```
   ┌───────────────────────────────────────────────────────────────┐
   │ 1. push_log()  — mgmt application events                      │
   │                                                                │
   │    mgmt/app.py:push_log()  wraps both:                         │
   │      a. WebSocket 'event' broadcast  (instant to all browsers) │
   │      b. VictoriaLogs HTTP insert     (persistent, queryable)   │
   │                                                                │
   │    Examples: migrate success, convert steps, node registered.  │
   └───────────────────────────────────────────────────────────────┘

   ┌───────────────────────────────────────────────────────────────┐
   │ 2. Systemd journal  — per-service stdout/stderr                │
   │                                                                │
   │    `journalctl -u <service>`  on each node.                    │
   │    Captures: uvicorn access log, VictoriaMetrics info, DRBD    │
   │    kernel messages, libvirtd, cloud-init, etc.                 │
   │                                                                │
   │    Not queryable from the dashboard today.                     │
   └───────────────────────────────────────────────────────────────┘

   ┌───────────────────────────────────────────────────────────────┐
   │ 3. Syslog  → VictoriaLogs :5140                                │
   │                                                                │
   │    Any cluster node can forward syslog to mgmt's VL TCP 5140.  │
   │    Currently opt-in per node (rsyslog config not auto-deployed).│
   │                                                                │
   │    Plan: agent_install.py writes /etc/rsyslog.d/bedrock.conf   │
   │    pointing at mgmt:5140 — follow-up.                          │
   └───────────────────────────────────────────────────────────────┘
```

The dashboard Recent Logs panel shows **channel 1** (push_log). Channels 2
and 3 are operator tools on the host.

## Every `push_log` call site

Grep the code: `grep -n "push_log(" mgmt/app.py`. Full list:

| Source | Trigger | Message format | Level |
|---|---|---|---|
| `register_node` | `POST /api/nodes/register` | `Node {name} ({host}) registered with cluster` | info |
| `_vm_start` | `POST /api/vms/{n}/start` | `VM {vm_name} started on {target}` | info |
| `_vm_shutdown` | `POST /api/vms/{n}/shutdown` | `VM {vm_name} shutdown requested on {host}` | info |
| `_vm_poweroff` | `POST /api/vms/{n}/poweroff` | `VM {vm_name} powered off on {host}` | warn |
| `_vm_migrate` success | `POST /api/vms/{n}/migrate` | `VM {vm_name} migrated from {src} to {dst} in {dur}s` | info |
| `_vm_migrate` failure | same | `VM {vm_name} migration FAILED from {src} to {dst}: {stderr}` | error |
| `_vm_convert_upgrade` cattle→pet | `POST /api/vms/{n}/convert` | `Convert {n}: create external DRBD meta LV {path}` | info |
|  |  | `Convert {n}: create peer LVs on {peer} ({n}M data + 4M meta)` | info |
|  |  | `Convert {n}: blockcopy {dev} → /dev/drbd{minor}` | info |
|  |  | `Convert {n}: {cur} → {tgt} in {dur}s (DRBD minor {minor})` | info |
| `_vm_convert_upgrade` pet→vipet | convert API | `Convert {n}: add 3rd peer {new_peer}` | info |
|  |  | `Convert {n}: pet → vipet, added {new_peer}` | info |
| `_vm_convert_downgrade` vipet→pet | convert API | `Convert {n}: vipet → pet (dropped {drop})` | info |
| `_vm_convert_downgrade` →cattle | convert API | `Convert {n}: pivot {dev} back to {lv_path}` | info |
|  |  | `Convert {n}: {cur} → cattle in {dur}s` | info |
| `_vm_create` | `POST /api/vms/create` | `Create VM {n}: lvcreate {n}G thin on {host}` | info |
|  |  | `Create VM {n}: virt-install (vcpus=., ram=.MB, iso=.)` | info |
|  |  | `Created VM {n} on {host} (cattle, ...vCPU, ...MB, ...GB, priority=., cpu_shares=.)` | info |
| `_vm_delete` | `DELETE /api/vms/{n}` | `Deleted VM {n} (was on {nodes})` | warn |
| `api_upload_iso` | `POST /api/isos/upload` | `ISO uploaded: {name} ({N} MB)` | info |
| `api_delete_iso` | `DELETE /api/isos/{n}` | `ISO deleted: {name}` | info |

All entries carry:

```
_time      : strftime("%Y-%m-%dT%H:%M:%S")
_msg       : the message string from above
hostname   : <node_name> (the node the event is *about*, not where mgmt runs)
app        : "bedrock-mgmt"
level      : info | warn | error
```

## Querying VictoriaLogs

VictoriaLogs uses LogsQL (similar to PromQL). The mgmt app exposes
pre-shaped endpoints:

| Endpoint | Query used |
|---|---|
| `GET /api/logs?query=*&limit=50&hours=1` | plain LogsQL passthrough |
| `GET /api/logs/node/{name}?limit=50&hours=4` | `hostname:"<name>"` |
| `GET /api/logs/vm/{name}?limit=50&hours=4` | `"<vm-name>"` (free text match in _msg) |

Or directly against VL:

```bash
# last 20 migration events cluster-wide
curl 'http://<mgmt>:9428/select/logsql/query?query=_msg:migrated&limit=20'

# all error-level mgmt events in the last hour
now=$(date +%s); start=$((now-3600))
curl "http://<mgmt>:9428/select/logsql/query?query=level:error&start=$start&limit=100"

# everything from one node
curl 'http://<mgmt>:9428/select/logsql/query?query=hostname:"bedrock-sim-2.bedrock.local"&limit=200'
```

## Streaming with `journalctl -f` equivalents

The dashboard is the closest to `tail -f` for push_log events. For the
systemd journal of any service:

```bash
# mgmt app — uvicorn access + tracebacks + paramiko chatter
ssh <mgmt-node> 'journalctl -u bedrock-mgmt -f'

# VictoriaMetrics — scrape errors, reload confirmations
ssh <mgmt-node> 'journalctl -u bedrock-vm -f'

# DRBD kernel messages
ssh <any-node> 'journalctl -kf | grep drbd'

# VM (QEMU) logs
ssh <host-of-vm> 'tail -f /var/log/libvirt/qemu/<vm>.log'
```

## How dashboard pages consume push_log

The WebSocket client (`mgmt/ui/src/lib/ws.ts`) dispatches frames by
`channel`. The root layout listens to `channel: "event"` and prepends
to the global `events` store.

Each page with a Recent Logs panel:

- **Overview** (`/`): shows all `events` + seeded history from `/api/logs`.
- **VM detail** (`/vm/<name>`): filters `events` whose `_msg` contains
  `<vm-name>`, plus seeded history from `/api/logs/vm/<name>`.
- **Node detail** (`/node/<name>`): filters `events` whose hostname or
  `_msg` contains the node's short name, plus `/api/logs/node/<name>`.

Live entries appear **instantly** (WS latency only; ~ms on LAN). The
seeded history is fetched once on mount — after that the panel is
100 % push-driven.

## Log retention

- **VictoriaLogs**: 90 days by default (`-retention=90d` in
  `bedrock-vl.service`). Storage at `/opt/bedrock/data/vl/`.
- **VictoriaMetrics**: 90 days (`-retentionPeriod=90d`). Storage at
  `/opt/bedrock/data/vm/`.
- **systemd journal**: per-unit defaults (usually size-capped, ~1 GB).
  `journalctl --vacuum-time=30d` to trim.

## What's deliberately *not* logged via push_log

- State-push-loop ticks (they'd flood).
- Periodic VictoriaMetrics scrapes (always-on noise).
- Per-TCP paramiko auth chatter (DEBUG-level only).
- DRBD kernel messages (volume too high; view in `journalctl -k`).
- Guest OS activity (syslog forwarding is a separate opt-in).

push_log is for **operator-meaningful events**: state transitions that
change what the cluster is doing. Everything else is journal-only.
