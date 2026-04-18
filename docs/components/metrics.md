# Metrics + Logs (VictoriaMetrics + VictoriaLogs)

The mgmt node runs two Victoria* processes. Together they handle the
entire metric + log pipeline — no Prometheus, no Loki, no Grafana
needed. Both persist to `/opt/bedrock/data/` and survive restarts.

## VictoriaMetrics (VM) — port 8428

Runs as `bedrock-vm.service`:

```
ExecStart=/opt/bedrock/bin/victoria-metrics
  -storageDataPath=/opt/bedrock/data/vm
  -promscrape.config=/opt/bedrock/scrape.yml
  -retentionPeriod=90d
  -httpListenAddr=:8428
```

Scrapes two exporter jobs across every node registered in
`cluster.json`:

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

1. A node registers (`/api/nodes/register`).
2. The mgmt app starts up.

Regeneration writes the file, then POSTs `http://127.0.0.1:8428/-/reload`
so VM picks up the new targets without restart.

### Why the HTTP reload path (not SIGHUP)

Empirically, `pkill -HUP victoria-metrics` did not cause VM to re-read
its scrape config in the installed version. The `/-/reload` endpoint
does, and is documented as the supported mechanism. `write_scrape_config`
uses that.

### Queries the dashboard makes

| Endpoint | PromQL pattern |
|---|---|
| `/api/v1/query?query=up` | `up` |
| `/api/metrics/nodes` (dashboard wrapper) | rate of `node_cpu_seconds_total{mode!="idle"}`, `node_memory_*`, `node_network_*_bytes_total` |
| `/api/metrics/vms` | `libvirt_domain_cpu_time`, `libvirt_domain_block_*`, `libvirt_domain_interface_*` |
| `/api/metrics/drbd` | `drbd_resource_role`, `drbd_disk_state`, `drbd_sync_percent` |

## VictoriaLogs (VL) — port 9428, syslog 5140

Runs as `bedrock-vl.service`:

```
ExecStart=/opt/bedrock/bin/victoria-logs
  -storageDataPath=/opt/bedrock/data/vl
  -httpListenAddr=:9428
  -syslog.listenAddr.tcp=:5140
```

Two ingress paths:

1. **JSON lines from mgmt** (`push_log()` → HTTP POST
   `/insert/jsonline`). This is where every Bedrock application event
   goes — see [reference/logs.md](../reference/logs.md).
2. **Syslog from cluster nodes** (TCP :5140, RFC 5424). Opt-in today;
   future auto-configured via rsyslog on join. Would capture kernel,
   systemd, libvirtd, qemu, drbd kernel events.

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
| Dashboard metrics tiles all "--" | VM not running, or first scrape hasn't hit yet | `systemctl status bedrock-vm`; wait 10 s after restart. |
| Only one node's metrics visible | scrape.yml out of sync with cluster.json | Re-trigger: restart `bedrock-mgmt` (startup calls `write_scrape_config`). |
| `up` metric = 0 for an exporter | exporter process down, or firewall | Check `systemctl status node-exporter` on that node. |
| Log panel stops updating | bedrock-mgmt or WS dropped | Browser auto-reconnects every 2 s; check `bedrock-mgmt.service`. |
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
