# Metrics + Logs (VictoriaMetrics + VictoriaLogs)

Bedrock ships its own metric + log pipeline; no Prometheus, Loki, or
Grafana. The stack is HA: every node runs lightweight agents
(`bedrock-vmagent` + `bedrock-vlagent`) that scrape/ingest locally and
dual-write, while two designated backend nodes run the full single-binary
storage engines (`bedrock-vm` = VictoriaMetrics, `bedrock-vl` =
VictoriaLogs). Backends persist to `/opt/bedrock/data/` and survive
restarts.

The reconciler `installer/lib/observability.py:reconcile(snapshot,
self_name)` converges each node's systemd state to the `obs_backends`
slots in the rqlite snapshot:

    obs_backends = {"metrics": [node_a, node_b], "logs": [node_a, node_b]}

It runs on every log fold, is idempotent (a second run writes nothing),
and:

- always keeps `bedrock-vmagent` + `bedrock-vlagent` running, with unit
  files carrying the current backend URLs (resolved via each backend's
  `loopback_ip`, falling back to `host`);
- starts `bedrock-vm` on a node in `metrics` and `bedrock-vl` on a node in
  `logs`; stops them on nodes that are not.

`bedrock-vm` is held back until its data dir is seeded (see Backend seed),
so agents buffer writes for it rather than dual-writing into an empty
store. `bedrock-vl` has no seed gate and starts immediately.

## VictoriaMetrics (VM) — port 8428

`bedrock-vm.service` runs on the metrics backend node(s).
Unit (`observability.py:_vm_unit`):

```
ExecStart=/opt/bedrock/bin/victoria-metrics
  -storageDataPath=/opt/bedrock/data/vm
  -retentionPeriod=90d
  -httpListenAddr=:8428
```

VM does not scrape. `bedrock-vmagent` (every node) owns the scrape loop and
remote-writes into VM. The agent reads `/opt/bedrock/scrape.yml`, which
targets two exporter jobs across every node (topology from rqlite):

```yaml
scrape_configs:
  - job_name: node
    scrape_interval: 10s
    static_configs:
      - targets:
          - '<host>:9100'   # node-exporter
        labels: {cluster: <cluster_name>}
  - job_name: libvirt
    scrape_interval: 10s
    static_configs:
      - targets:
          - '<host>:9177'   # vm-exporter
        labels: {cluster: <cluster_name>}
```

The mgmt app regenerates the scrape config via
`mgmt/app.py:write_scrape_config(cluster)` whenever:

1. cluster state changes — `save_cluster()` calls it with the snapshot;
2. mgmt starts — `startup()` calls it with `load_cluster()`.

It writes `scrape.yml` (one `<host>:9100` + one `<host>:9177` line per
node with a `host`), then fires `systemctl restart --no-block
bedrock-vmagent.service` so the agent re-reads targets.

### Why restart vmagent (not SIGHUP, not VM `/-/reload`)

The scrape config consumer is `bedrock-vmagent`, not VictoriaMetrics. The
shipped vmagent build terminates on SIGHUP instead of reloading, so the
config change is applied by restarting the unit; vmagent's persistent
on-disk queue (`-remoteWrite.tmpDataPath`) means a sub-second restart drops
zero scrapes. The restart is non-blocking (`--no-block`, via
`subprocess.Popen`) so a slow `systemctl` cannot stall FastAPI startup. If
the unit is not present yet at early init, the reconciler starts vmagent on
the next log fold with the fresh `scrape.yml` already on disk.

### Dashboard read endpoints

The dashboard read wrappers live in `mgmt/routes_obs.py`; each calls
`mgmt/victoria.py:query_range` (or `query_logs`) against the backends.

| Endpoint | PromQL |
|---|---|
| `/api/metrics/nodes` | `100 - avg by(instance)(rate(node_cpu_seconds_total{mode="idle"}[1m]))*100`; `(1 - node_memory_MemAvailable_bytes/node_memory_MemTotal_bytes)*100`; `rate(node_network_{receive,transmit}_bytes_total{device="br0"}[1m])` |
| `/api/metrics/vms` | `rate(bedrock_vm_cpu_time_ns[1m])`; `rate(bedrock_vm_disk_{read,write}_reqs{disk="0"}[1m])`; write latency = `rate(bedrock_vm_disk_write_time_ns{disk="0"}[1m]) / rate(bedrock_vm_disk_write_reqs{disk="0"}[1m])` |
| `/api/metrics/drbd` | `rate(bedrock_drbd_{sent,received}_kb[1m])`; `bedrock_drbd_out_of_sync_kb` |

All take `hours` (default 1) and `step` (default `30s`) query params.

## VictoriaLogs (VL) — port 9428, syslog 5141

`bedrock-vl.service` runs on the logs backend node(s).
Unit (`observability.py:_vl_unit`):

```
ExecStart=/opt/bedrock/bin/victoria-logs
  -storageDataPath=/opt/bedrock/data/vl
  -httpListenAddr=:9428
  -syslog.listenAddr.tcp=:5141
```

The backend's syslog listener is `:5141`. The per-node `bedrock-vlagent`
owns `:5140` syslog ingest and dual-writes to both VL backends, so an
agent and a backend coexist on one node without a port clash. VL runs with
no `-retentionPeriod` flag (no retention cap configured).

Two ingress paths:

1. **JSON lines from mgmt** — `mgmt/victoria.py:push_log()` POSTs to
   `/insert/jsonline` on each backend. Every Bedrock application event
   goes here (operator logins, join requests, seed progress); see
   [reference/logs.md](../reference/logs.md).
2. **Syslog from cluster nodes** — TCP, RFC 5424, ingested by
   `bedrock-vlagent` on `:5140` and forwarded. Captures kernel, systemd,
   libvirtd, qemu, drbd kernel events.

Dashboard log endpoints in `routes_obs.py` (all call
`victoria.py:query_logs` against `/select/logsql/query`):

| Endpoint | LogsQL |
|---|---|
| `/api/logs?query=<LogsQL>` | as given (default `*`) |
| `/api/logs/node/{node_name}` | `hostname:"<node_name>"` |
| `/api/logs/vm/{vm_name}` | `"<vm_name>"` |

`limit` (default 50) and `hours` (default 1) are query params.

## Agents

`bedrock-vmagent` (`_vmagent_unit`): scrapes via `-promscrape.config`
(scrape.yml on the mgmt master; followers have a stub) and dual-writes to
each metrics backend's `:8428/api/v1/write`. Disk queue at
`/var/lib/bedrock/vmagent-queue`, capped `-remoteWrite.maxDiskUsagePerURL=8GB`.

`bedrock-vlagent` (`_vlagent_unit`): ingests syslog on `:5140` and
dual-writes to each logs backend's `:9428/internal/insert`. Disk queue at
`/var/lib/bedrock/vlagent-queue`, same 8 GB cap.

The disk queues are the only convergence mechanism between the two
backends: a backend that was down replays the queued writes on recovery,
which is why both backends carry identical data and a single-backend read
is the correct answer.

## Reads pick a backend

`mgmt/victoria.py` resolves backend hosts from `obs_backends` and tries
each in order until one returns 2xx. When this node is itself a backend,
`127.0.0.1` is tried first (saves a LAN hop and works during a LAN blip).
It is not a merging proxy: dual-write makes the backends identical, so the
first responder is authoritative.

## Backend seed (1 -> 2 promotion)

When a new node joins and a metrics/logs backend has only one member,
`mgmt/app.py:join_approve()` appends `OBS_BACKENDS_SET` adding the joiner
as slot #2, then runs `observability.py:seed_backend()` synchronously
before the snapshot advertises the new backend.

- **Metrics**: `seed_backend` runs `vmbackup` on the source (snapshotting
  via `http://127.0.0.1:8428/snapshot/create`) to a `fs://` stage,
  tars the stage over SSH to the target, and `vmrestore`s it into a clean
  `/opt/bedrock/data/vm`. vmbackup writes real file copies, so the
  symlinks in VM's `snapshots/` tree never enter the archive.
- **Logs**: VictoriaLogs has no online snapshot endpoint, so logs start
  fresh on the second backend; vlagent dual-write keeps both VL backends
  identical from promotion onward.

`reconcile` will not start `bedrock-vm` on the new backend until its data
dir contains real parts (`_data_dir_seeded` finds a `values.bin`/`items.bin`);
a solo backend at cluster init starts fresh because there is nothing to
seed from.

## Why Victoria* not Prometheus/Loki

- Single binary each, no dependencies; Prometheus+Loki+Grafana is four
  processes minimum.
- ~10x smaller disk footprint than Prometheus for the same retention.
- Prometheus-compatible API for metrics, LogsQL for logs.
- A ~26 MB binary is reasonable to run on a cluster node; a full Grafana
  stack is not.

## Restart / upgrade

Both backends are stateful via `-storageDataPath`. Restart is safe
(flushes in-memory buffers). Upgrade: stop unit -> swap the binary in
`/opt/bedrock/bin/` -> start unit. Storage format is forward-compatible
across recent Victoria* versions.

## Failure modes

| Symptom | Cause | Recovery |
|---|---|---|
| Dashboard metrics tiles all "--" | `bedrock-vm` down on the metrics backend, or first scrape not in yet | `systemctl status bedrock-vm`; wait ~10 s after restart |
| Only one node's metrics visible | `scrape.yml` does not reflect current topology | restart `bedrock-d` (startup calls `write_scrape_config`) or restart `bedrock-vmagent` |
| `up` = 0 for an exporter | exporter process down | `systemctl status node-exporter` (or `vm-exporter`) on that node |
| Log panel stops updating | bedrock-d down or WebSocket dropped | browser auto-reconnects every 2 s; check `bedrock-d.service` |
| Metrics older than 90 d gone | VM retention | raise `-retentionPeriod` in the `bedrock-vm` unit, restart |

## On disk

```
/opt/bedrock/data/vm/   VictoriaMetrics state (parts per retention interval, compacted)
/opt/bedrock/data/vl/   VictoriaLogs state (similar layout)
/var/lib/bedrock/vmagent-queue/   vmagent remote-write disk buffer
/var/lib/bedrock/vlagent-queue/   vlagent remote-write disk buffer
```

A 3-node cluster with 3 VMs runs roughly ~50 MB/day metrics, ~5 MB/day
logs; 90-day metrics retention is ~5 GB. Scales with cluster size and VM
count.
