# mgmt/vm_exporter.py

A dependency-free Prometheus exporter that runs as its own auto-started unit
(`vm-exporter`) on every hypervisor node and serves libvirt VM, LVM thin-pool,
and DRBD metrics at `0.0.0.0:9177/metrics`. It is scraped by the node's vmagent
into VictoriaMetrics; the dashboard reads those series (e.g. it warns when a
thin-pool crosses 80 %). It shells out to `virsh`, `lvs`, and `drbdsetup` and
uses only the Python 3 standard library.

## Functions / Classes

### `run(cmd) -> str`
Run one shell command and return its trimmed stdout, swallowing all failures.
- **In:** `cmd` — a shell string (run with `shell=True`).
- **Out:** stdout `.strip()`ed; empty string on any exception or 5 s timeout.
  Side effect: spawns a subprocess.

### `collect_vm_metrics() -> list[str]`
Turn `virsh domstats` output into per-VM Prometheus lines.
- **In:** none.
- **Out:** list of metric lines. Empty if `virsh` produced nothing. Side effect:
  runs `virsh domstats --cpu-total --balloon --block --interface --state --raw`.

Emits, labelled `vm="<domain>"` (plus `disk=` / `iface=` where applicable):
`bedrock_vm_cpu_time_ns`, `bedrock_vm_memory_current_kb`,
`bedrock_vm_memory_maximum_kb`, `bedrock_vm_disk_{read,write}_{reqs,bytes}`,
`bedrock_vm_disk_{read,write}_time_ns`, `bedrock_vm_disk_flush_reqs`,
`bedrock_vm_disk_flush_time_ns`, `bedrock_vm_net_{rx,tx}_bytes`,
`bedrock_vm_net_{rx,tx}_packets`, `bedrock_vm_state`.

### `collect_thinpool_metrics() -> list[str]`
Report LVM thin-pool size and data/metadata fullness.
- **In:** none.
- **Out:** list of metric lines (`bedrock_thinpool_size_bytes`,
  `bedrock_thinpool_data_percent`, `bedrock_thinpool_metadata_percent`, labelled
  `vg=` / `pool=`). Side effect: runs `lvs --units b --nosuffix` selecting
  thin-pool LVs (`lv_attr=~"^t"`).

### `collect_drbd_metrics() -> list[str]`
Parse `drbdsetup status --json` into resource/device/connection/peer metrics.
- **In:** none.
- **Out:** list of metric lines. Empty if `drbdsetup` produced nothing or the
  JSON fails to parse. Side effect: runs `drbdsetup status --json`.

Emits: `bedrock_drbd_role` (label `role=`), `bedrock_drbd_disk_state`
(`minor=`,`state=`), `bedrock_drbd_written_kb`, `bedrock_drbd_read_kb`,
`bedrock_drbd_al_writes`, `bedrock_drbd_connection` (`peer=`,`state=`),
and the per-peer-device `bedrock_drbd_out_of_sync_kb`,
`bedrock_drbd_peer_disk_state` (`+state=`), `bedrock_drbd_sent_kb`,
`bedrock_drbd_received_kb` (all four labelled `peer=`). All carry `resource=`.

### `class MetricsHandler(http.server.BaseHTTPRequestHandler)`
The HTTP handler backing the exporter.
- `do_GET`: serves `/metrics` (200, `text/plain; charset=utf-8`) by
  concatenating a comment header plus the three collectors' lines; any other
  path returns 404.
- `log_message`: overridden to a no-op, suppressing access logs.

## How it works

`__main__` builds an `http.server.HTTPServer` bound to `0.0.0.0:9177`, prints a
startup line, and calls `serve_forever()`. There is no background scrape loop:
metrics are gathered lazily, on each `GET /metrics`.

```
GET /metrics
  ├─ "# Bedrock VM and DRBD metrics exporter"   (header line)
  ├─ collect_vm_metrics()        virsh domstats  --raw
  ├─ collect_drbd_metrics()      drbdsetup status --json
  └─ collect_thinpool_metrics()  lvs (thin-pool LVs)
        → "\n".join(lines) + "\n"  →  200 text/plain
```

VM parsing is stateful line-by-line: a `Domain: 'name'` line sets
`current_domain` (taken from inside the single quotes, else the last
whitespace-token); subsequent `key=value` lines are attributed to it. A line is
skipped when no domain is in scope yet, when it has no `=`, or when the value
won't parse as a float — so only numeric stats become metrics. `block.<dev>.…`
and `net.<dev>.…` keys split out the `<dev>` token as the `disk` / `iface`
label.

Failure handling is uniform and silent: every collector funnels its command
through `run`, which returns `""` on timeout or error, and each collector
returns an empty list when its input is empty (DRBD additionally returns empty
on a JSON parse error). A down or absent tool drops just its own metric family;
the endpoint still answers 200 with whatever else was collected. Each subprocess
is capped at a 5 s timeout, bounding scrape latency.

## Why

Standard-library-only and no persistent state keep the exporter trivially
robust: it can run on any node regardless of cluster role, and a missing or
hung backend (`virsh`/`lvs`/`drbdsetup`) degrades to fewer metrics rather than a
failed scrape.
