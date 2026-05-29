# Metrics + Logs (VictoriaMetrics + VictoriaLogs)

Bedrock ships its own metric + log pipeline — no Prometheus, no Loki, no
Grafana needed. The stack is HA: **every node runs lightweight agents**
(`bedrock-vmagent` + `bedrock-vlagent`) that scrape locally and forward,
while **two designated backend nodes** run the full storage engines
(`bedrock-vm` / VictoriaMetrics and `bedrock-vl` / VictoriaLogs). The
observability reconciler (`installer/lib/observability.py`) decides which
nodes hold the backend slots from the `obs_backends` rqlite state and
starts/stops the units accordingly. Backends persist to
`/opt/bedrock/data/` and survive restarts.

## VictoriaMetrics (VM) — port 8428

Runs as `bedrock-vm.service` on the metrics backend node(s), unit written
by the reconciler:

```
ExecStart=/opt/bedrock/bin/victoria-metrics
  -storageDataPath=/opt/bedrock/data/vm
  -retentionPeriod=90d
  -httpListenAddr=:8428
```

VM no longer scrapes directly — `bedrock-vmagent` (on every node) owns the
scrape loop and remote-writes into VM. The agent reads
`/opt/bedrock/scrape.yml`, which targets two exporter jobs across every
node in the cluster (topology from rqlite, not a flat file):

```yaml
scrape_configs:
  - job_name: node
    scrape_interval: 10s
    static_configs:
      - targets:
          - '192.168.2.152:9100'   # node_exporter
          - '192.168.2.153:9100'
          - '192.168.2.154:9100'
        labels: {cluster: bedrock-e2e}
  - job_name: libvirt
    scrape_interval: 10s
    static_configs:
      - targets:
          - '192.168.2.152:9177'   # vm_exporter
          - '192.168.2.153:9177'
          - '192.168.2.154:9177'
        labels: {cluster: bedrock-e2e}
```

The scrape config is **regenerated automatically** by
`mgmt/app.py:write_scrape_config()` whenever:

1. Cluster state changes (`save_cluster()` calls it with the rqlite
   snapshot).
2. The mgmt app starts up (`startup()` calls it with `load_cluster()`).

Regeneration writes `scrape.yml`, then `systemctl restart --no-block
bedrock-vmagent.service` so the agent re-reads the targets.

### Why restart vmagent (not SIGHUP, not VM `/-/reload`)

The scrape config consumer is `bedrock-vmagent`, not VictoriaMetrics. On
the shipped vmagent build SIGHUP terminates the process instead of
reloading, so `write_scrape_config` restarts the unit. vmagent's
persistent on-disk queue means a sub-second restart drops zero scrapes.
The restart is fired non-blocking (`--no-block`) so a slow `systemctl`
can't stall FastAPI startup; if the unit isn't present yet at early init,
the reconciler starts vmagent later with the fresh `scrape.yml` already
on disk.

### Queries the dashboard makes

Dashboard wrappers live in `mgmt/routes_obs.py`:

| Endpoint | PromQL pattern |
|---|---|
| `/api/v1/query?query=up` | `up` |
| `/api/metrics/nodes` | `node_cpu_seconds_total{mode="idle"}` (inverted), `node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes`, `node_network_{receive,transmit}_bytes_total{device="br0"}` |
| `/api/metrics/vms` | `bedrock_vm_cpu_time_ns`, `bedrock_vm_disk_{read,write}_reqs`, `bedrock_vm_disk_write_time_ns` |
| `/api/metrics/drbd` | `bedrock_drbd_sent_kb`, `bedrock_drbd_received_kb`, `bedrock_drbd_out_of_sync_kb` |

## VictoriaLogs (VL) — port 9428, syslog 5140

Runs as `bedrock-vl.service` on the logs backend node(s), unit written by
the reconciler:

```
ExecStart=/opt/bedrock/bin/victoria-logs
  -storageDataPath=/opt/bedrock/data/vl
  -httpListenAddr=:9428
  -syslog.listenAddr.tcp=:5141
```

(The backend listens on syslog `:5141`; the per-node `bedrock-vlagent`
owns the `:5140` syslog ingest and dual-writes to both VL backends, so
the two can coexist on a node that is both an agent and a backend.)

Two ingress paths:

1. **JSON lines from mgmt** (`push_log()` → HTTP POST
   `/insert/jsonline`). This is where every Bedrock application event
   goes — see [reference/logs.md](../reference/logs.md).
2. **Syslog from cluster nodes** (TCP :5140, RFC 5424), forwarded by
   `bedrock-vlagent` on each node. Captures kernel, systemd, libvirtd,
   qemu, drbd kernel events.

Dashboard reads via `/select/logsql/query`:

```
  ?query=<LogsQL>
  &limit=<N>
  &start=<unix-ts>
  &end=<unix-ts>
```

See [`reference/api.md`](../reference/api.md) for the specific wrapper
endpoints exposed by the mgmt app.

## Why Victoria* instead of Prometheus/Loki

- **Single binary each, no deps.** One `victoria-metrics`, one
  `victoria-logs`. Contrast Prometheus+Loki+Grafana: four processes
  minimum.
- **Disk footprint** ~10× smaller than Prometheus for the same
  retention (per VictoriaMetrics' published benchmarks).
- **API is Prometheus-compatible** for metrics, LogsQL for logs (a
  superset of familiar selectors).
- **Fits the "runs on your cluster node" constraint**: a 26 MB binary
  is reasonable; a full Grafana stack is not.

## Restart / upgrade

Both processes are stateful via their `-storageDataPath`. A restart is
safe (flushes in-memory buffers). An upgrade is `stop service → swap
binary in /opt/bedrock/bin/ → start service`. Storage format is
forward-compatible across recent Victoria* versions.

## Failure modes

| Symptom | Cause | Recovery |
|---|---|---|
| Dashboard metrics tiles all "--" | VM not running, or first scrape hasn't hit yet | `systemctl status bedrock-vm` on the metrics backend; wait 10 s after restart. |
| Only one node's metrics visible | scrape.yml out of sync with cluster topology | Re-trigger: restart `bedrock-d` (startup calls `write_scrape_config`), or restart `bedrock-vmagent`. |
| `up` metric = 0 for an exporter | exporter process down, or firewall | Check `systemctl status node-exporter` on that node. |
| Log panel stops updating | bedrock-d or WS dropped | Browser auto-reconnects every 2 s; check `bedrock-d.service`. |
| Old push_log entries gone | beyond 90 d retention | Increase `-retentionPeriod` in bedrock-vl.service, restart. |

## What's on disk

```
/opt/bedrock/data/vm/     VictoriaMetrics state (Parquet-like files per
                          retention interval, compacted over time)
/opt/bedrock/data/vl/     VictoriaLogs state (similar layout)
```

Approximate footprint for a 3-node Bedrock cluster with 3 VMs:
~50 MB/day metrics, ~5 MB/day logs. 90-day retention ≈ 5 GB total.
Grows proportional to cluster size and VM count.
