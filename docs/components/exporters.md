# Exporters (node_exporter + vm_exporter)

Every node runs two Prometheus-style exporters. VictoriaMetrics on the
mgmt node scrapes both every 10 seconds.

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
[Service]
ExecStart=/opt/bedrock/bin/node_exporter --web.listen-address=:9100
Restart=always
```

Source binary: `installer/binaries/node_exporter`, installed on every
node by `installer/lib/exporters.py`.

## vm_exporter (port 9177)

Bedrock-specific ~100-line Python exporter (`mgmt/vm_exporter.py`, also
shipped as `installer/binaries/vm_exporter.py`). Uses only Python
stdlib; no extra deps.

Parses the output of:

```bash
virsh domstats --cpu-total --balloon --block --interface --state --raw
drbdsetup status --json
```

Emits text-format metrics like:

```
# HELP libvirt_domain_state VM state (1=running)
# TYPE libvirt_domain_state gauge
libvirt_domain_state{vm="webapp1"} 1

# HELP libvirt_domain_cpu_time_seconds Total CPU time used by the VM
# TYPE libvirt_domain_cpu_time_seconds counter
libvirt_domain_cpu_time_seconds{vm="webapp1"} 31694.058

# HELP libvirt_domain_block_write_iops Write IOPS per block device
libvirt_domain_block_write_iops{vm="webapp1",device="vda"} 12

# HELP drbd_resource_role 1=Primary, 0=Secondary, -1=unknown
drbd_resource_role{resource="vm-webapp1-disk0"} 1

# HELP drbd_disk_state 1=UpToDate, 0=anything else
drbd_disk_state{resource="vm-webapp1-disk0",peer="self"} 1
drbd_disk_state{resource="vm-webapp1-disk0",peer="bedrock-sim-2"} 1

# HELP drbd_sync_percent Ongoing resync progress (0-100)
drbd_sync_percent{resource="vm-webapp1-disk0",peer="bedrock-sim-3"} 22.1
```

Run as `vm-exporter.service`:

```ini
[Service]
ExecStart=/usr/bin/python3 /opt/bedrock/bin/vm_exporter.py
After=libvirtd.service
Wants=libvirtd.service
```

## Deployment

Both services are installed on every node by `installer/lib/exporters.py`
at `bedrock init` (for the mgmt+compute node) and at `bedrock join`
(for every compute node).

```python
# installer/lib/exporters.py
def install(repo: str):
    mkdir /opt/bedrock/bin
    curl <repo>/binaries/node_exporter → /opt/bedrock/bin/; chmod 755
    curl <repo>/binaries/vm_exporter.py → /opt/bedrock/bin/; chmod 755
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
- A ~100-line Python file is readable by anyone on the team; adding a
  new metric is 2 lines.

## Reading metrics directly

```bash
curl http://<node>:9100/metrics | grep node_load
curl http://<node>:9177/metrics | grep drbd_resource_role

# Or via VM (pre-aggregated):
curl 'http://<mgmt>:8428/api/v1/query?query=up{job="libvirt"}'
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
    lines.append("# HELP libvirt_domain_new_metric My new thing")
    lines.append("# TYPE libvirt_domain_new_metric gauge")
    lines.append(f'libvirt_domain_new_metric{{vm="{dom}"}} {value}')
    return lines
```

Redeploy:

```bash
scp mgmt/vm_exporter.py root@<every-node>:/opt/bedrock/bin/
ssh root@<every-node> 'systemctl restart vm-exporter'
```

No VM scrape config change needed — same `/metrics` endpoint.
