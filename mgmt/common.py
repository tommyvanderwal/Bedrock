"""Shared cluster-management infrastructure for the mgmt app + its routers.

A LEAF module (it imports no router and no `app`), so every router can eager-import the
cross-cutting helpers from here without an import cycle: the SSH connection pool + `ssh_cmd`,
cluster-state reads (`load_cluster`/`get_nodes`/`build_cluster_state`), per-node/VM data
gathering, and `push_log` (dashboard WebSocket + VictoriaLogs).

`push_log` broadcasts from worker threads onto the asyncio loop, so the loop is injected by
main.py's startup hook via `set_main_loop()`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os as _os
import re
import subprocess
import threading as _threading
import time
from pathlib import Path
from typing import Optional

import paramiko
from fastapi import HTTPException

import sys as _sys
_sys.path.insert(0, "/usr/local/lib/bedrock")
from lib import bedrock_state as _bs            # noqa: E402
from lib import rqlite_client as _rqlite        # noqa: E402
from lib import cluster_state as _cluster_state  # noqa: E402
from lib import event_log as _events            # noqa: E402

from ws import hub
from victoria import push_log as _vl_push_log

log = logging.getLogger("bedrock")

# The asyncio loop, injected by main.py's @app.on_event("startup") so push_log can broadcast
# to dashboard WebSockets from worker threads (run_coroutine_threadsafe).
_main_loop: Optional[asyncio.AbstractEventLoop] = None


def set_main_loop(loop) -> None:
    global _main_loop
    _main_loop = loop


SSH_USER = "root"




def load_cluster():
    """Cluster-wide state. Reads from the local rqlite replica via
    cluster_state.load_cluster() at level='none', so it works without
    quorum (the per-node Raft follower replica is always readable when
    this node's rqlited is up)."""
    try:
        return _cluster_state.load_cluster()
    except Exception as e:
        log.warning("cluster_state.load_cluster failed: %s", e)
        return {"cluster_name": "bedrock", "nodes": {}}




def save_cluster(cluster: dict):
    """Its only side-effect is write_scrape_config(cluster), which
    produces the VictoriaMetrics scrape config from the snapshot.
    Cluster state itself lives in rqlite, not in a local projection."""
    write_scrape_config(cluster)




SCRAPE_FILE = Path("/opt/bedrock/scrape.yml")




def write_scrape_config(cluster: dict):
    """Regenerate VictoriaMetrics scrape.yml from cluster state, then reload VM."""
    if not SCRAPE_FILE.parent.exists():
        return  # Not on a mgmt node
    name = cluster.get("cluster_name", "bedrock")
    hosts = [n["host"] for n in cluster.get("nodes", {}).values() if n.get("host")]
    if not hosts:
        return
    node_t = "\n".join(f"        - '{h}:9100'" for h in hosts)
    libvirt_t = "\n".join(f"        - '{h}:9177'" for h in hosts)
    SCRAPE_FILE.write_text(
        "scrape_configs:\n"
        "  - job_name: node\n"
        "    scrape_interval: 10s\n"
        "    static_configs:\n"
        "      - targets:\n"
        f"{node_t}\n"
        f"        labels: {{cluster: {name}}}\n"
        "  - job_name: libvirt\n"
        "    scrape_interval: 10s\n"
        "    static_configs:\n"
        "      - targets:\n"
        f"{libvirt_t}\n"
        f"        labels: {{cluster: {name}}}\n"
    )
    # Scrape config consumer is now `bedrock-vmagent`. SIGHUP on this
    # vmagent build terminates the process instead of reloading, so use
    # restart — the persistent disk queue means a sub-second restart
    # drops zero scrapes. Best-effort: if the unit isn't here yet (early
    # init), the reconciler will start vmagent on the next log fold with
    # the fresh scrape.yml already on disk.
    # Fire-and-forget so a slow `systemctl restart` doesn't block the
    # FastAPI startup lifespan. Mgmt would otherwise loop-crash if the
    # vmagent unit takes >5s to settle (which it does when other
    # services are racing for the same systemd lock at install time).
    try:
        subprocess.Popen(
            ["systemctl", "restart", "--no-block", "bedrock-vmagent.service"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass




def get_nodes() -> dict:
    return load_cluster().get("nodes", {})




# Per-VM config, now discovered dynamically from virsh/drbd state.
# Kept only as cached metadata (updated by background refresh).
_VM_META_CACHE: dict = {}



_SSH_POOL: dict[str, "paramiko.SSHClient"] = {}


_SSH_POOL_LOCK = _threading.Lock()




def _ssh_pool_drop(host: str) -> None:
    """Drop the cached client for `host`, if any. Called when an
    exec_command raises — the next call will reconnect."""
    with _SSH_POOL_LOCK:
        c = _SSH_POOL.pop(host, None)
    if c is not None:
        try: c.close()
        except Exception: pass




def _ssh_connect(host: str):
    """Get a cached paramiko client for `host`, opening a new connection
    on first call or after a drop. Uses the root@host key mesh set up
    by agent_install — every node has every other node's pubkey."""
    with _SSH_POOL_LOCK:
        c = _SSH_POOL.get(host)
        if c is not None:
            t = c.get_transport()
            if t is not None and t.is_active() and t.is_alive():
                return c
            # Stale entry — drop and reconnect below.
            _SSH_POOL.pop(host, None)
            try: c.close()
            except Exception: pass

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username=SSH_USER, timeout=5, allow_agent=True,
              look_for_keys=True)
    # Keepalive packets so the connection doesn't get torn down by a
    # NAT or firewall between probes. 20s matches the existing probe
    # cadence (3s loop) with plenty of headroom.
    try:
        t = c.get_transport()
        if t is not None:
            t.set_keepalive(20)
    except Exception:
        pass

    with _SSH_POOL_LOCK:
        # Lost a race? Whoever published first wins; close ours.
        existing = _SSH_POOL.get(host)
        if existing is not None and existing.get_transport() and existing.get_transport().is_active():
            try: c.close()
            except Exception: pass
            return existing
        _SSH_POOL[host] = c
    return c




def ssh_cmd(host: str, cmd: str, timeout: int = 10) -> str:
    try:
        c = _ssh_connect(host)
        _, so, _ = c.exec_command(cmd, timeout=timeout)
        return so.read().decode().strip()
    except (paramiko.SSHException, EOFError, OSError):
        _ssh_pool_drop(host)
        raise




def ssh_cmd_rc(host: str, cmd: str, timeout: int = 30) -> tuple[str, int]:
    """Run cmd over SSH, return (combined_output, exit_code). Always combines
    stdout+stderr so callers never lose the failure reason."""
    try:
        c = _ssh_connect(host)
        _, so, se = c.exec_command(cmd, timeout=timeout)
        out = so.read().decode().strip()
        err = se.read().decode().strip()
        rc = so.channel.recv_exit_status()
    except (paramiko.SSHException, EOFError, OSError):
        _ssh_pool_drop(host)
        raise
    combined = (out + ("\n" + err if err else "")).strip()
    return combined, rc



# ── Data gathering ──────────────────────────────────────────────────────────

def get_node_info(name: str, cfg: dict) -> dict:
    host = cfg["host"]
    try:
        raw = ssh_cmd(host, (
            "echo '---VIRSH---'; virsh list --all --name; "
            "echo '---VIRSH_RUNNING---'; virsh list --name --state-running; "
            "echo '---DRBD---'; drbdadm status 2>/dev/null; "
            "echo '---LOAD---'; cat /proc/loadavg; "
            "echo '---MEM---'; free -m | grep Mem; "
            "echo '---UPTIME---'; uptime -s; "
            "echo '---KERNEL---'; uname -r; "
            "echo '---THINPOOL---'; lvs --noheadings --units b --nosuffix "
            "--separator '|' -o vg_name,lv_name,lv_size,data_percent,metadata_percent "
            "--select 'lv_attr=~\"^t\"' 2>/dev/null; "
            # Trailing `echo` after each `cat` guarantees a newline
            # between this section's content and the next ---MARKER---
            # line, even if the JSON file on disk lacks a final newline.
            "echo '---SWITCHES---'; cat /run/bedrock/switch_neighbors.json 2>/dev/null; echo; "
            "echo '---MESH---'; cat /run/bedrock/mesh_neighbors.json 2>/dev/null; echo"
        ))
        sections = {}
        current = None
        for line in raw.split("\n"):
            if line.startswith("---") and line.endswith("---"):
                current = line.strip("-")
                sections[current] = []
            elif current:
                sections[current].append(line)

        all_vms = [v for v in sections.get("VIRSH", []) if v.strip()]
        running_vms = [v for v in sections.get("VIRSH_RUNNING", []) if v.strip()]
        mem_parts = sections.get("MEM", [""])[0].split()
        load_parts = sections.get("LOAD", ["0 0 0"])[0].split()

        # Thin pools: list of {vg, name, size_bytes, data_pct, meta_pct}
        thinpools = []
        for row in sections.get("THINPOOL", []):
            parts = [p.strip() for p in row.split("|") if p.strip()]
            if len(parts) >= 5:
                try:
                    thinpools.append({
                        "vg": parts[0], "name": parts[1],
                        "size_bytes": int(parts[2]),
                        "data_pct": float(parts[3]),
                        "meta_pct": float(parts[4]),
                    })
                except ValueError: pass

        # Switch neighbours seen by bedrock-net (LLDP / CDP / MNDP). The
        # raw payload is the contents of /run/bedrock/switch_neighbors.json
        # on the node — a per-NIC dict keyed by protocol. We parse it
        # eagerly so the rollup logic doesn't have to re-parse on every
        # 3 s push.
        try:
            switches_raw = "\n".join(sections.get("SWITCHES", [])).strip()
            switches = json.loads(switches_raw) if switches_raw else {}
            if not isinstance(switches, dict):
                switches = {}
        except (ValueError, TypeError):
            switches = {}

        # Mesh neighbours (node-to-node, protocol-1 discovery). Contents
        # of /run/bedrock/mesh_neighbors.json — shape:
        # {'me': <node_name>, 'nics': {<my_nic>: {'addr':…,
        #  'speed_mbps':…, 'neighbours': [{peer_node,peer_nic,…}…]}}}
        try:
            mesh_raw = "\n".join(sections.get("MESH", [])).strip()
            mesh = json.loads(mesh_raw) if mesh_raw else {}
            if not isinstance(mesh, dict):
                mesh = {}
        except (ValueError, TypeError):
            mesh = {}

        return {
            "name": name, "host": host, "online": True,
            "kernel": sections.get("KERNEL", [""])[0],
            "uptime_since": sections.get("UPTIME", [""])[0],
            "load": load_parts[0] if load_parts else "0",
            "mem_total_mb": int(mem_parts[1]) if len(mem_parts) > 1 else 0,
            "mem_used_mb": int(mem_parts[2]) if len(mem_parts) > 2 else 0,
            "all_vms": all_vms, "running_vms": running_vms,
            "drbd_raw": "\n".join(sections.get("DRBD", [])),
            "thinpools": thinpools,
            "switches": switches,
            "mesh": mesh,
            "cockpit_url": cfg.get("cockpit", f"https://{host}:9090"),
        }
    except Exception as e:
        return {
            "name": name, "host": host, "online": False, "error": str(e),
            "all_vms": [], "running_vms": [], "drbd_raw": "",
            "thinpools": [],
            "switches": {},
            "mesh": {},
            "cockpit_url": cfg.get("cockpit", f"https://{host}:9090"),
            "kernel": "", "uptime_since": "", "load": "0",
            "mem_total_mb": 0, "mem_used_mb": 0,
        }



def parse_drbd_status(raw: str) -> dict:
    resources = {}
    current_res = None
    for line in raw.split("\n"):
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^(\S+)\s+role:(\S+)", line)
        if m:
            current_res = m.group(1)
            resources[current_res] = {"role": m.group(2), "disk": "", "peer_role": "",
                                      "peer_disk": "", "replication": "", "done": ""}
            continue
        if current_res and current_res in resources:
            r = resources[current_res]
            if "disk:" in line and "peer-disk" not in line:
                m2 = re.search(r"disk:(\S+)", line)
                if m2: r["disk"] = m2.group(1)
            if "peer-disk:" in line:
                m2 = re.search(r"peer-disk:(\S+)", line)
                if m2: r["peer_disk"] = m2.group(1)
            m2 = re.match(r"^\S+\s+role:(\S+)", line)
            if m2 and "connection:" not in line:
                r["peer_role"] = m2.group(1)
            if "replication:" in line:
                m2 = re.search(r"replication:(\S+)", line)
                if m2: r["replication"] = m2.group(1)
            if "done:" in line:
                m2 = re.search(r"done:([\d.]+)", line)
                if m2: r["done"] = m2.group(1)
    return resources



def get_witness_status() -> dict:
    """Witness panel data for the dashboard. Pulls the configured
    witnesses from replicated cluster state (the `witnesses` map);
    live reachability is tracked by netd's election/witness loop and is
    not surfaced through the mgmt API."""
    return {"witnesses": load_cluster().get("witnesses", {})}



def get_vm_drbd_resource(host: str, vm_name: str) -> str:
    """Find the DRBD resource backing this VM's first data disk.
    Thin wrapper over get_vm_disks, returning just the first resource."""
    disks = get_vm_disks(host, vm_name)
    for d in disks:
        if d.get("drbd_resource"):
            return d["drbd_resource"]
    return ""




def get_vm_disks(host: str, vm_name: str) -> list[dict]:
    """Parse virsh dumpxml + drbdsetup status to enumerate every block disk
    attached to the VM (cdroms excluded). One entry per disk:
      {
        target: "vda" | "vdb" | ...,     # guest-visible device name
        bus: "virtio" | "sata" | "scsi",
        source: "/dev/almalinux/vm-X-disk0" | "/dev/drbd1000",
        drbd_resource: "vm-X-disk0" | "",
        drbd_minor: 1000 | None,
        backing_lv: "/dev/almalinux/vm-X-disk0",  # raw LV under the DRBD device
      }
    Ordered by target (vda, vdb, vdc …). Returns [] if the VM doesn't exist.
    """
    try:
        xml = ssh_cmd(host, f"virsh dumpxml {vm_name} 2>/dev/null") or ""
    except Exception:
        return []
    if not xml:
        return []

    import re as _re
    out = []
    # DRBD status lookup (one call per host, reused across disks)
    drbd_by_minor: dict[str, str] = {}
    try:
        import json as _json
        raw = ssh_cmd(host, "drbdsetup status --json 2>/dev/null || echo '[]'")
        for res in _json.loads(raw or "[]"):
            for dev in res.get("devices", []):
                drbd_by_minor[str(dev.get("minor", ""))] = res.get("name", "")
    except Exception:
        pass

    for m in _re.finditer(r"<disk\b([^>]*)>(.*?)</disk>", xml, _re.DOTALL):
        attrs, body = m.group(1), m.group(2)
        dev_m = _re.search(r"device=['\"]([^'\"]+)['\"]", attrs)
        device = dev_m.group(1) if dev_m else "disk"
        if device != "disk":
            continue  # skip cdroms / floppies
        src_m = _re.search(r"<source\s+(?:file|dev)=['\"]([^'\"]+)['\"]", body)
        tgt_m = _re.search(r"<target\s+dev=['\"]([^'\"]+)['\"]\s+bus=['\"]([^'\"]+)['\"]", body)
        if not (src_m and tgt_m):
            continue
        source = src_m.group(1)
        target = tgt_m.group(1)
        bus = tgt_m.group(2)

        drbd_resource = ""
        drbd_minor = None
        backing_lv = source
        minor_m = _re.match(r"/dev/drbd(\d+)$", source)
        if minor_m:
            drbd_minor = int(minor_m.group(1))
            drbd_resource = drbd_by_minor.get(str(drbd_minor), "")
            # Resolve backing LV via drbdadm show. Cheap; cached would be nicer.
            try:
                show = ssh_cmd(host,
                    f"drbdadm show {drbd_resource} 2>/dev/null | head -30") if drbd_resource else ""
                lv_m = _re.search(r"disk\s+(/dev/[^\s;]+);", show)
                if lv_m:
                    backing_lv = lv_m.group(1)
            except Exception: pass
        out.append({
            "target": target, "bus": bus, "source": source,
            "drbd_resource": drbd_resource, "drbd_minor": drbd_minor,
            "backing_lv": backing_lv,
        })
    out.sort(key=lambda d: d["target"])
    return out




def get_vm_vnc_port(host: str, vm_name: str) -> int:
    """Get VNC display port for a running VM. Returns -1 if not available."""
    try:
        out = ssh_cmd(host, f"virsh vncdisplay {vm_name} 2>/dev/null")
        # Output like ":0" or ":1" → VNC port = 5900 + N
        if out.startswith(":"):
            return int(out[1:]) + 5900
    except Exception:
        pass
    return -1




def build_cluster_state() -> dict:
    nodes_cfg = get_nodes()
    # Parallel SSH fan-out: 3 nodes went from ~3s sequential to ~1s.
    from concurrent.futures import ThreadPoolExecutor
    nodes_data = {}
    with ThreadPoolExecutor(max_workers=max(4, len(nodes_cfg))) as ex:
        futs = {ex.submit(get_node_info, name, cfg): name
                for name, cfg in nodes_cfg.items()}
        for fut, name in futs.items():
            nodes_data[name] = fut.result()

    # Parse DRBD across all nodes
    drbd = {}
    for name, info in nodes_data.items():
        if info["online"]:
            parsed = parse_drbd_status(info["drbd_raw"])
            for res, state in parsed.items():
                if res not in drbd or state["role"] == "Primary":
                    drbd[res] = {**state, "from_node": name}

    vms_data = {}
    all_vm_names = set()
    for info in nodes_data.values():
        all_vm_names.update(info["all_vms"])

    def _probe_vm(vm_name):
        running_on = None
        defined_on = []
        for nname, info in nodes_data.items():
            if vm_name in info["running_vms"]: running_on = nname
            if vm_name in info["all_vms"]: defined_on.append(nname)
        disks = (get_vm_disks(nodes_cfg[defined_on[0]]["host"], vm_name)
                 if defined_on else [])
        vnc_port = (get_vm_vnc_port(nodes_cfg[running_on]["host"], vm_name)
                    if running_on and running_on in nodes_cfg else -1)
        return vm_name, running_on, defined_on, disks, vnc_port

    with ThreadPoolExecutor(max_workers=max(4, len(all_vm_names) or 1)) as ex:
        probes = list(ex.map(_probe_vm, sorted(all_vm_names)))

    for vm_name, running_on, defined_on, disks, vnc_port in probes:
        backup_node = next((n for n in defined_on if n != running_on), None)
        # drbd_resource is the first disk's resource; the full per-disk
        # detail is in the disks[] array.
        first_resource = next((d["drbd_resource"] for d in disks
                              if d.get("drbd_resource")), "")
        drbd_state = drbd.get(first_resource, {}) if first_resource else {}
        vnc_ws_url = f"/vnc/{vm_name}" if vnc_port > 0 else ""

        # Enrich each disk with its DRBD state (if any), and with LV size
        # so the settings UI can show per-disk capacity without a second call.
        disks_out = []
        size_host = (nodes_cfg[defined_on[0]]["host"] if defined_on else None)
        for d in disks:
            disk = dict(d)
            r = d.get("drbd_resource", "")
            if r and r in drbd:
                disk["drbd_role"] = drbd[r].get("role", "")
                disk["drbd_disk"] = drbd[r].get("disk", "")
                disk["drbd_peer_disk"] = drbd[r].get("peer_disk", "")
                disk["drbd_sync_pct"] = drbd[r].get("done", "")
            # Resolve size from the backing LV (cheap blockdev call)
            try:
                if size_host and d.get("backing_lv"):
                    b = ssh_cmd(size_host,
                        f"blockdev --getsize64 {d['backing_lv']} 2>/dev/null || echo 0")
                    disk["size_bytes"] = int((b or "0").strip())
                    disk["size_gb"] = max(1, disk["size_bytes"] // (1 << 30))
            except Exception: pass
            disks_out.append(disk)

        vms_data[vm_name] = {
            "name": vm_name, "state": "running" if running_on else "shut off",
            "running_on": running_on, "backup_node": backup_node, "defined_on": defined_on,
            "disks": disks_out,
            # First-disk DRBD summary, alongside the full disks[] array:
            "drbd_resource": first_resource,
            "drbd_role": drbd_state.get("role", ""),
            "drbd_disk": drbd_state.get("disk", ""),
            "drbd_peer_disk": drbd_state.get("peer_disk", ""),
            "drbd_replication": drbd_state.get("replication", ""),
            "drbd_sync_pct": drbd_state.get("done", ""),
            "vnc_ws_url": vnc_ws_url,
        }

    # Merge per-VM inventory (priority, creation metadata)
    inventory = load_inventory()
    for vm_name, data in inventory.items():
        if vm_name in vms_data:
            vms_data[vm_name].update({
                "priority":  data.get("priority", "normal"),
                "vcpus":     data.get("vcpus"),
                "ram_mb":    data.get("ram_mb"),
                "disk_gb":   data.get("disk_gb"),
                "iso":       data.get("iso"),
                "created_at": data.get("created_at"),
            })

    topology = build_physical_topology(nodes_data)
    return {"nodes": nodes_data, "vms": vms_data,
            "witness": get_witness_status(),
            "topology": topology}




def build_physical_topology(nodes_data: dict) -> dict:
    """Group per-node switch observations by the device's MAC (the
    `device_key` field bedrock-net's l2disc parser computes).

    Why MAC, not chassis_id: different protocols report the same
    physical device with different identifiers. CDP says the
    device name ('office-sw-01'); MNDP says the device MAC
    ('d4:01:c3:0e:7b:36'); LLDP says either depending on the
    switch's chassis-ID subtype. The MAC is the only identifier
    every real switch carries and the only one guaranteed unique.
    l2disc.decode_*() does the chassis-id-or-frame-src-MAC pick
    so we always have a usable device_key here.

    Input: nodes_data[node_name]['switches'] is the per-NIC dict
    that bedrock-net writes to /run/bedrock/switch_neighbors.json:
        { '<my_nic>': { '<protocol>': {device_key, chassis_id,
                                        src_mac, system_name,
                                        port_id, mgmt_ip, ...} } }

    Output: derived view-only structure, never written to consensus
    state. Shape:

        {
          'switches': {
            '<device_key>': {                # lowercase MAC
              'device_key': '<lowercase mac>',
              'system_name': '<best known>',
              'mgmt_ip':    '<best known>',
              'platform':   '<best known>',
              'aliases':    ['MikroTik', 'd4:01:c3:0e:7b:36'],
              'protocols':  ['cdp', 'mndp'],
              'connections': [
                {'node': 'bedrock-X', 'my_nic': 'br0',
                 'port_id': 'Gi1/0/3', 'protocol': 'lldp',
                 'first_seen': ..., 'last_seen': ...}, ...
              ]
            }
          },
          'node_count':   3,
          'switch_count': 2,    # distinct devices (by MAC)
          'computed_at':  <epoch>,
        }
    """
    grouped: dict = {}
    node_count = 0
    for node_name, info in (nodes_data or {}).items():
        switches = (info or {}).get("switches") or {}
        if not switches:
            continue
        node_count += 1
        for my_nic, by_proto in switches.items():
            if not isinstance(by_proto, dict):
                continue
            for protocol, entry in by_proto.items():
                if not isinstance(entry, dict):
                    continue
                # Prefer device_key (MAC-canonicalised by l2disc).
                # Fall back to chassis_id when an entry lacks it so the
                # merge still surfaces something instead of dropping it.
                device_key = (entry.get("device_key")
                               or entry.get("chassis_id") or "")
                if not device_key:
                    continue
                device_key = str(device_key).lower()
                bucket = grouped.setdefault(device_key, {
                    "device_key": device_key,
                    "system_name": "",
                    "mgmt_ip":     "",
                    "platform":    "",
                    "aliases":     set(),
                    "protocols":   set(),
                    "connections": [],
                })
                # Pick the most-informative system_name / mgmt_ip /
                # platform across all observations (first non-empty
                # wins; tied values keep the first-seen).
                for src, key in (("system_name", "system_name"),
                                  ("mgmt_ip",     "mgmt_ip"),
                                  ("platform",    "platform")):
                    v = entry.get(src)
                    if v and not bucket[key]:
                        bucket[key] = v
                # Aliases — every distinct chassis_id we've ever seen
                # for this MAC. Useful so the dashboard can show
                # 'MikroTik / d4:01:c3:0e:7b:36' as the device label.
                ci = entry.get("chassis_id")
                if ci:
                    bucket["aliases"].add(str(ci))
                bucket["protocols"].add(protocol)
                bucket["connections"].append({
                    "node":       node_name,
                    "my_nic":     my_nic,
                    "protocol":   protocol,
                    "port_id":    entry.get("port_id", ""),
                    "port_descr": entry.get("port_descr", ""),
                    "first_seen": entry.get("first_seen"),
                    "last_seen":  entry.get("last_seen"),
                })

    # Sets aren't JSON-serialisable; finalize.
    for bucket in grouped.values():
        bucket["aliases"]   = sorted(bucket["aliases"])
        bucket["protocols"] = sorted(bucket["protocols"])
        bucket["connections"].sort(
            key=lambda c: (c["node"], c["my_nic"], c["protocol"]))

    # Node-to-node links from protocol-1 (multicast discovery)
    # observations on each node's mesh_neighbors.json. Each link is
    # canonicalised so (A,B) and (B,A) collapse to one entry; we
    # prefer the entry from the lexicographically-smaller node's
    # view so each link's bandwidth/latency values are stable across
    # rebuilds even if the two sides disagree slightly.
    links: list[dict] = []
    seen_link_keys: set[tuple] = set()
    for node_name, info in sorted((nodes_data or {}).items()):
        mesh = (info or {}).get("mesh") or {}
        nics = (mesh.get("nics") or {})
        for my_nic, nic_view in nics.items():
            speed = int(nic_view.get("speed_mbps") or 0)
            for r in (nic_view.get("neighbours") or []):
                if not r.get("logged_up"):
                    continue
                peer = r.get("peer_node", "")
                if not peer or peer == node_name:
                    continue
                # Canonical order: smaller node name first. Stable.
                if node_name < peer:
                    a, a_nic, b, b_nic = (node_name, my_nic,
                                          peer, r.get("peer_nic", ""))
                    a_addr, b_addr = (nic_view.get("addr", ""),
                                       r.get("peer_link_addr", ""))
                else:
                    a, a_nic, b, b_nic = (peer, r.get("peer_nic", ""),
                                          node_name, my_nic)
                    a_addr, b_addr = (r.get("peer_link_addr", ""),
                                       nic_view.get("addr", ""))
                key = (a, a_nic, b, b_nic)
                if key in seen_link_keys:
                    continue
                seen_link_keys.add(key)
                links.append({
                    "node_a":    a,
                    "nic_a":     a_nic,
                    "addr_a":    a_addr,
                    "node_b":    b,
                    "nic_b":     b_nic,
                    "addr_b":    b_addr,
                    "speed_mbps": speed,
                    "rtt_us":     int(r.get("rtt_us") or 0),
                    "blip_total": int(r.get("blip_total") or 0),
                    "first_seen": r.get("first_seen"),
                    "last_seen":  r.get("last_seen"),
                })

    rollup = {
        "switches":     grouped,
        "links":        links,
        "node_count":   node_count,
        "switch_count": len(grouped),
        "link_count":   len(links),
        "computed_at":  time.time(),
    }

    # Best-effort cache to disk so post-mortem inspection without the
    # mgmt service running is possible. Not authoritative — the live
    # view is always the in-memory _last_state.
    try:
        PHYSICAL_TOPOLOGY_CACHE.parent.mkdir(parents=True, exist_ok=True)
        tmp = PHYSICAL_TOPOLOGY_CACHE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(rollup, indent=2, sort_keys=True))
        tmp.replace(PHYSICAL_TOPOLOGY_CACHE)
    except OSError as e:
        log.debug("physical_topology cache write failed: %s", e)

    return rollup




def _mgmt_node_name() -> str:
    """Return the node name of the mgmt host (where ISOs live)."""
    cfg = get_nodes()
    for name, node in cfg.items():
        if "mgmt" in node.get("role", ""):
            return name
    # Fallback: first node
    return next(iter(cfg)) if cfg else ""




def push_log(msg: str, node: str = "mgmt", app: str = "bedrock-mgmt",
             level: str = "info"):
    """Stream to dashboard WebSockets first, then persist to VictoriaLogs.
    The VL insert is a blocking HTTP call; doing it second keeps the UI
    responsive even if VL is slow or unreachable."""
    entry = {"_msg": msg, "hostname": node, "app": app, "level": level,
             "_time": time.strftime("%Y-%m-%dT%H:%M:%S")}
    if _main_loop is not None:
        try:
            asyncio.run_coroutine_threadsafe(hub.broadcast("event", entry), _main_loop)
        except Exception:
            pass
    _vl_push_log(msg, node=node, app=app, level=level)


# ── Shared WS cluster-state cache ────────────────────────────────────
# The state-push loop (app.py) rebinds this every few seconds; /ws and the cluster router
# serve from it instantly so the dashboard never waits on fresh SSH probes. Lives here (a
# leaf module) so both the loop and the router reach the SAME current value via the accessors.
_last_state: dict = {"nodes": {}, "vms": {}, "witness": {"nodes": {}}}


def get_last_state() -> dict:
    return _last_state


def set_last_state(state: dict) -> None:
    global _last_state
    _last_state = state
# ── ISO / image inventory (shared: imports + vm-create iso list) ─────






def load_inventory() -> dict:
    if VM_INVENTORY_FILE.exists():
        try: return json.loads(VM_INVENTORY_FILE.read_text())
        except Exception: return {}
    return {}






def save_inventory(inv: dict):
    VM_INVENTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    VM_INVENTORY_FILE.write_text(json.dumps(inv, indent=2))
# ── Import job directory (shared: imports router + _vm_create_from_import) ──






def _import_dir(job_id: str) -> Path:
    # Strict job-id form to prevent traversal
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", job_id):
        raise HTTPException(400, "invalid id")
    return IMPORT_ROOT / job_id
# ── Import job meta writer (shared: imports router + _vm_create_from_import) ──






def _write_import_meta(d: Path, meta: dict):
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(json.dumps(meta, indent=2))
# ── shared VM/import helpers + roots (used by common + multiple routers) ──




# routes_iso registration needs the push_log callable, so it runs after
# `def push_log(...)` further down (see "── routes_iso ──").


# ── Import library (VMware/Hyper-V/qcow2 → Bedrock) ──────────────────────

IMPORT_ROOT = Path("/opt/bedrock/imports")




# ── Settings helpers ────────────────────────────────────────────────────────

def _vm_host(vm_name: str) -> tuple:
    """Return (running_on_name, host, resource_name) for a VM that exists."""
    state = build_cluster_state()
    vm = state["vms"].get(vm_name)
    if not vm: raise HTTPException(404, f"Unknown VM: {vm_name}")
    running = vm.get("running_on") or (vm.get("defined_on") or [None])[0]
    if not running: raise HTTPException(503, "VM has no known node")
    nodes_cfg = get_nodes()
    return running, nodes_cfg[running]["host"], vm.get("drbd_resource", "")




def _vm_get_settings(vm_name: str) -> dict:
    running, host, resource = _vm_host(vm_name)
    xml = ssh_cmd(host, f"virsh dumpxml {vm_name}")
    info = _parse_dominfo(xml)
    # disk size from the data disk (first <disk device='disk'>)
    data_disk = next((d for d in info["disks"] if d["device"] == "disk"), None)
    disk_bytes = 0
    if data_disk and data_disk["source"]:
        try:
            disk_bytes = int(ssh_cmd(host, f"blockdev --getsize64 {data_disk['source']}"))
        except Exception: pass
    # Current CDROM inserted (if any). The USER's ISO slot is whichever SATA
    # CDROM is NOT virtio-win.iso.
    cdrom_slot, cdrom_iso = None, None
    for d in info["disks"]:
        if d["device"] == "cdrom":
            fname = d["source"].rsplit("/", 1)[-1] if d["source"] else ""
            if fname != "virtio-win.iso":
                cdrom_slot = d["target"]
                cdrom_iso = fname or None
                break
    # Priority from inventory
    inv = load_inventory()
    priority = (inv.get(vm_name) or {}).get("priority", "normal")
    # Get cpu_shares live
    try:
        out = ssh_cmd(host, f"virsh schedinfo {vm_name} 2>/dev/null | awk '/cpu_shares/{{print $3}}'")
        cpu_shares = int(out.strip()) if out.strip() else None
    except Exception:
        cpu_shares = None
    return {
        "name": vm_name,
        "host": host,
        "vcpus": info["vcpus"],
        "ram_mb": info["ram_kib"] // 1024,
        "disk_gb": disk_bytes // (1024**3),
        "disk_path": data_disk["source"] if data_disk else "",
        "disk_target": data_disk["target"] if data_disk else "",
        "drbd_resource": resource,
        "cdrom_slot": cdrom_slot,
        "cdrom_iso": cdrom_iso,
        "priority": priority,
        "cpu_shares": cpu_shares,
    }
# ── data-gathering sub-helpers + cache paths (used by build_*/_vm_get_settings) ──




# ── Physical topology rollup ────────────────────────────────────────────────

PHYSICAL_TOPOLOGY_CACHE = Path("/run/bedrock/physical_topology.json")


VM_INVENTORY_FILE = Path("/etc/bedrock/vm_inventory.json")




def _parse_dominfo(xml: str) -> dict:
    """Pull vcpus, ram, cdrom target+source, disk target+source from VM XML."""
    import re as _re
    m_vcpu = _re.search(r"<vcpu[^>]*>(\d+)</vcpu>", xml)
    m_mem = _re.search(r"<memory[^>]*unit=['\"]KiB['\"][^>]*>(\d+)</memory>", xml) or \
            _re.search(r"<memory[^>]*>(\d+)</memory>", xml)
    disks = []
    for m in _re.finditer(r"<disk\b([^>]*)>(.*?)</disk>", xml, _re.DOTALL):
        attrs, body = m.group(1), m.group(2)
        device = _re.search(r"device=['\"]([^'\"]+)['\"]", attrs)
        device = device.group(1) if device else "disk"
        src = _re.search(r"<source\s+(?:file|dev)=['\"]([^'\"]+)['\"]", body)
        tgt = _re.search(r"<target\s+dev=['\"]([^'\"]+)['\"]\s+bus=['\"]([^'\"]+)['\"]", body)
        if tgt:
            disks.append({
                "device": device, "target": tgt.group(1), "bus": tgt.group(2),
                "source": src.group(1) if src else "",
            })
    return {
        "vcpus": int(m_vcpu.group(1)) if m_vcpu else 0,
        "ram_kib": int(m_mem.group(1)) if m_mem else 0,
        "disks": disks,
    }
