# Exporters (node_exporter + vm_exporter)

Every node runs two Prometheus-style exporters. `bedrock-vmagent` on each
node scrapes both every 10 seconds and remote-writes into the cluster's
VictoriaMetrics backend.

## node_exporter (port 9100)

Stock Prometheus `node_exporter` v1.8.2. Emits standard host metrics:

- `node_cpu_seconds_total` — per-CPU busy time (cpu %, used for load)
- `node_memory_*` — MemTotal/MemAvailable (memory %)
- `node_network_*_bytes_total` — per-NIC RX/TX counters
- `node_disk_*` — per-device IOPS, latency, throughput
- `node_load1` / `node_load5` / `node_load15`
- `node_uname_info`, `node_boot_time_seconds` — kernel + uptime

Run as `node-exporter.service`:

```ini
[Unit]
After=network.target

[Service]
ExecStart=/opt/bedrock/bin/node_exporter --web.listen-address=:9100
Restart=always
RestartSec=3
```

Source binary: `installer/binaries/node_exporter`, installed on every
node by `installer/lib/exporters.py`.

## vm_exporter (port 9177)

Bedrock-specific ~200-line Python exporter (`mgmt/vm_exporter.py`, also
shipped as `installer/binaries/vm_exporter.py`). Uses only Python
stdlib; no extra deps.

Parses the output of:

```bash
virsh domstats --cpu-total --balloon --block --interface --state --raw
drbdsetup status --json
```

Emits text-format metrics like (all VM series are `bedrock_vm_*`, all DRBD
series `bedrock_drbd_*`):

```
# HELP bedrock_vm_state VM state (1=running)
bedrock_vm_state{vm="webapp1"} 1

# HELP bedrock_vm_cpu_time_ns Total CPU time used by the VM (ns)
bedrock_vm_cpu_time_ns{vm="webapp1"} 31694058000000

# HELP bedrock_vm_disk_write_reqs Write requests per block device
bedrock_vm_disk_write_reqs{vm="webapp1",disk="0"} 12

# HELP bedrock_drbd_role role as a label, value always 1
bedrock_drbd_role{resource="vm-webapp1-disk0",role="Primary"} 1

# HELP bedrock_drbd_disk_state local disk state (state label, value 1)
bedrock_drbd_disk_state{resource="vm-webapp1-disk0",minor="1000",state="UpToDate"} 1

# HELP bedrock_drbd_out_of_sync_kb KiB still to resync to a peer
bedrock_drbd_out_of_sync_kb{resource="vm-webapp1-disk0",peer="bedrock-sim-2"} 4096
```

Run as `vm-exporter.service`:

```ini
[Unit]
After=libvirtd.service
Wants=libvirtd.service

[Service]
ExecStart=/usr/bin/python3 /opt/bedrock/bin/vm_exporter.py
Restart=always
RestartSec=3
```

## Deployment

Both services keep their own systemd units and are installed on every node
by `installer/lib/exporters.py:install()`. It runs from the cluster-init
saga's `install_exporters` step on the first node and the node-join saga's
`install_exporters` step on every subsequent node (idempotent — it
checks-and-skips files already on disk).

```python
# installer/lib/exporters.py
def install(repo: str):
    mkdir /opt/bedrock/bin
    curl <repo>/binaries/node_exporter → /opt/bedrock/bin/; chmod 755
    curl <repo>/binaries/vm_exporter.py → /opt/bedrock/bin/; chmod 755
    # also fetch the obs binaries (vmagent, vlagent, vmbackup,
    # vmrestore, victoria-metrics, victoria-logs) so any node can be
    # promoted to a backend slot later; observability.py owns their units
    write /etc/systemd/system/node-exporter.service
    write /etc/systemd/system/vm-exporter.service
    systemctl daemon-reload
    systemctl enable --now node-exporter vm-exporter
```

## Why vm_exporter exists (vs. a libvirt-exporter project)

- Existing libvirt exporters require Go, more dependencies, or a
  specific libvirt connection mode.
- We needed DRBD metrics in the same series; most libvirt exporters
  don't do DRBD.
- A ~200-line Python file is readable by anyone on the team; adding a
  new metric is 2 lines.

## Reading metrics directly

```bash
curl http://<node>:9100/metrics | grep node_load
curl http://<node>:9177/metrics | grep bedrock_drbd_role

# Or via VictoriaMetrics on the metrics backend node:
curl 'http://<vm-backend>:8428/api/v1/query?query=up{job="libvirt"}'
```

## Failure modes

| Symptom | Cause | Recovery |
|---|---|---|
| `up{job="node"}=0` for a host | node_exporter process down | `systemctl status node-exporter`; `journalctl -u node-exporter`. |
| `up{job="libvirt"}=0` for a host | vm_exporter down, or libvirtd down | Check both: `systemctl status vm-exporter libvirtd`. |
| vm_exporter returns but DRBD metrics missing | `drbdsetup status --json` requires kernel module loaded | `modprobe drbd`; `systemctl restart vm-exporter`. |
| Port collision on 9100 / 9177 | another agent (node_exporter baseline install) | `ss -tlnp \| grep 9100`; stop the stray. |

## Extending vm_exporter

Add a new metric:

```python
# mgmt/vm_exporter.py
def collect_vm_metrics():
    lines = []
    ...
    # existing: CPU, balloon, block, interface
    lines.append("# HELP bedrock_vm_new_metric My new thing")
    lines.append("# TYPE bedrock_vm_new_metric gauge")
    lines.append(f'bedrock_vm_new_metric{{vm="{dom}"}} {value}')
    return lines
```

Redeploy:

```bash
scp mgmt/vm_exporter.py root@<every-node>:/opt/bedrock/bin/
ssh root@<every-node> 'systemctl restart vm-exporter'
```

No VM scrape config change needed — same `/metrics` endpoint.
