#!/usr/bin/env python3
"""Lightweight Prometheus exporter for libvirt VM and DRBD metrics.

Exposes metrics at :9177/metrics. Runs on each hypervisor node.
No dependencies beyond Python 3 standard library + subprocess.
"""

import http.server
import subprocess
import re
import time

PORT = 9177


def run(cmd):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=5)
        return r.stdout.strip()
    except Exception:
        return ""


def collect_vm_metrics():
    lines = []
    raw = run("virsh domstats --cpu-total --balloon --block --interface --state --raw 2>/dev/null")
    if not raw:
        return lines

    current_domain = None
    for line in raw.split("\n"):
        line = line.strip()
        if line.startswith("Domain:"):
            current_domain = line.split("'")[1] if "'" in line else line.split()[-1]
            continue
        if not current_domain or "=" not in line:
            continue

        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()

        try:
            fval = float(val)
        except ValueError:
            continue

        # Map virsh domstats keys to Prometheus metrics
        if key == "cpu.time":
            lines.append('bedrock_vm_cpu_time_ns{vm="%s"} %s' % (current_domain, val))
        elif key == "balloon.current":
            lines.append('bedrock_vm_memory_current_kb{vm="%s"} %s' % (current_domain, val))
        elif key == "balloon.maximum":
            lines.append('bedrock_vm_memory_maximum_kb{vm="%s"} %s' % (current_domain, val))
        elif key.startswith("block.") and key.endswith(".rd.reqs"):
            disk = key.split(".")[1]
            lines.append('bedrock_vm_disk_read_reqs{vm="%s",disk="%s"} %s' % (current_domain, disk, val))
        elif key.startswith("block.") and key.endswith(".rd.bytes"):
            disk = key.split(".")[1]
            lines.append('bedrock_vm_disk_read_bytes{vm="%s",disk="%s"} %s' % (current_domain, disk, val))
        elif key.startswith("block.") and key.endswith(".rd.times"):
            disk = key.split(".")[1]
            lines.append('bedrock_vm_disk_read_time_ns{vm="%s",disk="%s"} %s' % (current_domain, disk, val))
        elif key.startswith("block.") and key.endswith(".wr.reqs"):
            disk = key.split(".")[1]
            lines.append('bedrock_vm_disk_write_reqs{vm="%s",disk="%s"} %s' % (current_domain, disk, val))
        elif key.startswith("block.") and key.endswith(".wr.bytes"):
            disk = key.split(".")[1]
            lines.append('bedrock_vm_disk_write_bytes{vm="%s",disk="%s"} %s' % (current_domain, disk, val))
        elif key.startswith("block.") and key.endswith(".wr.times"):
            disk = key.split(".")[1]
            lines.append('bedrock_vm_disk_write_time_ns{vm="%s",disk="%s"} %s' % (current_domain, disk, val))
        elif key.startswith("block.") and key.endswith(".fl.reqs"):
            disk = key.split(".")[1]
            lines.append('bedrock_vm_disk_flush_reqs{vm="%s",disk="%s"} %s' % (current_domain, disk, val))
        elif key.startswith("block.") and key.endswith(".fl.times"):
            disk = key.split(".")[1]
            lines.append('bedrock_vm_disk_flush_time_ns{vm="%s",disk="%s"} %s' % (current_domain, disk, val))
        elif key.startswith("net.") and key.endswith(".rx.bytes"):
            iface = key.split(".")[1]
            lines.append('bedrock_vm_net_rx_bytes{vm="%s",iface="%s"} %s' % (current_domain, iface, val))
        elif key.startswith("net.") and key.endswith(".tx.bytes"):
            iface = key.split(".")[1]
            lines.append('bedrock_vm_net_tx_bytes{vm="%s",iface="%s"} %s' % (current_domain, iface, val))
        elif key.startswith("net.") and key.endswith(".rx.pkts"):
            iface = key.split(".")[1]
            lines.append('bedrock_vm_net_rx_packets{vm="%s",iface="%s"} %s' % (current_domain, iface, val))
        elif key.startswith("net.") and key.endswith(".tx.pkts"):
            iface = key.split(".")[1]
            lines.append('bedrock_vm_net_tx_packets{vm="%s",iface="%s"} %s' % (current_domain, iface, val))
        elif key == "state.state":
            lines.append('bedrock_vm_state{vm="%s"} %s' % (current_domain, val))

    return lines


def collect_drbd_metrics():
    lines = []
    raw = run("drbdsetup status --json 2>/dev/null")
    if not raw:
        return lines

    import json
    try:
        resources = json.loads(raw)
    except Exception:
        return lines

    for res in resources:
        name = res.get("name", "?")
        role = res.get("role", "Unknown")
        lines.append('bedrock_drbd_role{resource="%s",role="%s"} 1' % (name, role))

        for dev in res.get("devices", []):
            minor = dev.get("minor", "?")
            disk_state = dev.get("disk-state", "Unknown")
            lines.append('bedrock_drbd_disk_state{resource="%s",minor="%s",state="%s"} 1' % (name, minor, disk_state))

            written = dev.get("written", 0)
            read = dev.get("read", 0)
            al_writes = dev.get("al-writes", 0)
            lines.append('bedrock_drbd_written_kb{resource="%s"} %s' % (name, written))
            lines.append('bedrock_drbd_read_kb{resource="%s"} %s' % (name, read))
            lines.append('bedrock_drbd_al_writes{resource="%s"} %s' % (name, al_writes))

        for conn in res.get("connections", []):
            peer = conn.get("name", "?")
            conn_state = conn.get("connection-state", "Unknown")
            lines.append('bedrock_drbd_connection{resource="%s",peer="%s",state="%s"} 1' % (name, peer, conn_state))

            for pd in conn.get("peer_devices", []):
                out_of_sync = pd.get("out-of-sync", 0)
                peer_disk = pd.get("peer-disk-state", "Unknown")
                sent = pd.get("sent", 0)
                received = pd.get("received", 0)
                lines.append('bedrock_drbd_out_of_sync_kb{resource="%s",peer="%s"} %s' % (name, peer, out_of_sync))
                lines.append('bedrock_drbd_peer_disk_state{resource="%s",peer="%s",state="%s"} 1' % (name, peer, peer_disk))
                lines.append('bedrock_drbd_sent_kb{resource="%s",peer="%s"} %s' % (name, peer, sent))
                lines.append('bedrock_drbd_received_kb{resource="%s",peer="%s"} %s' % (name, peer, received))

    return lines


class MetricsHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/metrics":
            self.send_response(404)
            self.end_headers()
            return

        lines = []
        lines.append("# Bedrock VM and DRBD metrics exporter")
        lines.extend(collect_vm_metrics())
        lines.extend(collect_drbd_metrics())

        body = "\n".join(lines) + "\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, format, *args):
        pass  # suppress access logs


if __name__ == "__main__":
    server = http.server.HTTPServer(("0.0.0.0", PORT), MetricsHandler)
    print("bedrock-exporter listening on :%d" % PORT)
    server.serve_forever()
