# `mgmt/vm_exporter.py`

**Module purpose.** Tiny prometheus exporter for per-VM stats.
Runs as `vm-exporter.service` on every node, listens on `:9177`,
scraped by VictoriaMetrics on the obs backend nodes.

## Functions

- `main()` — opens `:9177`, polls libvirt every 15 s, serves
  `/metrics`. Reuses libvirt connection for the duration.
- `vm_metrics() -> str` — query libvirt for each running VM:
  cpu time (cumulative ns), memory current/max, block i/o
  (read/write bytes + iops), net i/o (rx/tx bytes). Formats as
  prometheus text exposition with
  `bedrock_vm_{cpu_seconds_total, memory_bytes, ...}` series.

Scrape interval matches node_exporter's 15 s default.
