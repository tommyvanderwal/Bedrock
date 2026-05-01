#!/usr/bin/env python3
"""Bedrock cluster management dashboard — FastAPI backend with WebSocket hub."""

import asyncio
import json
import logging
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

import paramiko
import urllib.request
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ws import hub
from tasks import registry as task_registry, Task

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("bedrock")

# ── Config ──────────────────────────────────────────────────────────────────

# Fallback NODES for development (physical lab). On a real cluster, this is
# overridden by /etc/bedrock/cluster.json — populated by bedrock init and
# node register API calls from joining nodes.
FALLBACK_NODES = {
    "node1": {"host": "192.168.2.141", "tb_ip": "10.88.0.1", "eno_ip": "10.99.0.1",
              "cockpit": "https://192.168.2.141:9090"},
    "node2": {"host": "192.168.2.142", "tb_ip": "10.88.0.2", "eno_ip": "10.99.0.2",
              "cockpit": "https://192.168.2.142:9090"},
}

CLUSTER_FILE = Path("/etc/bedrock/cluster.json")
SSH_USER = "root"
# Password fallback is only used when key auth fails. In a real cluster the
# SSH mesh is key-based; this env var is for dev boxes that still rely on a
# shared password. Leave unset and only key auth will be attempted.
import os as _os
SSH_PASS = _os.environ.get("BEDROCK_SSH_PASS", "")
WITNESS_URL = _os.environ.get("BEDROCK_WITNESS_URL", "")


def load_cluster():
    """Load cluster config. Use /etc/bedrock/cluster.json if available, else fallback."""
    if CLUSTER_FILE.exists():
        try:
            data = json.loads(CLUSTER_FILE.read_text())
            if data.get("nodes"):
                return data
        except Exception as e:
            log.warning("Failed to load %s: %s", CLUSTER_FILE, e)
    return {"cluster_name": "bedrock-dev", "nodes": FALLBACK_NODES}


def save_cluster(cluster: dict):
    CLUSTER_FILE.parent.mkdir(parents=True, exist_ok=True)
    CLUSTER_FILE.write_text(json.dumps(cluster, indent=2))
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
    try:
        urllib.request.urlopen("http://127.0.0.1:8428/-/reload", timeout=2)
    except Exception:
        pass


def get_nodes() -> dict:
    return load_cluster().get("nodes", FALLBACK_NODES)


# Per-VM config, now discovered dynamically from virsh/drbd state.
# Kept only as cached metadata (updated by background refresh).
_VM_META_CACHE: dict = {}

# ── SSH helpers ─────────────────────────────────────────────────────────────

def _ssh_connect(host: str):
    """Connect via SSH. Key auth first (production); password fallback only if
    BEDROCK_SSH_PASS is set (dev/lab convenience)."""
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        c.connect(host, username=SSH_USER, timeout=5, allow_agent=True,
                  look_for_keys=True)
    except paramiko.AuthenticationException:
        if not SSH_PASS:
            raise
        c.connect(host, username=SSH_USER, password=SSH_PASS, timeout=5)
    return c


def ssh_cmd(host: str, cmd: str, timeout: int = 10) -> str:
    c = _ssh_connect(host)
    _, so, _ = c.exec_command(cmd, timeout=timeout)
    out = so.read().decode().strip()
    c.close()
    return out


def ssh_cmd_rc(host: str, cmd: str, timeout: int = 30) -> tuple[str, int]:
    """Run cmd over SSH, return (combined_output, exit_code). Always combines
    stdout+stderr so callers never lose the failure reason."""
    c = _ssh_connect(host)
    _, so, se = c.exec_command(cmd, timeout=timeout)
    out = so.read().decode().strip()
    err = se.read().decode().strip()
    rc = so.channel.recv_exit_status()
    c.close()
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
            "--select 'lv_attr=~\"^t\"' 2>/dev/null"
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
            "cockpit_url": cfg["cockpit"],
        }
    except Exception as e:
        return {
            "name": name, "host": host, "online": False, "error": str(e),
            "all_vms": [], "running_vms": [], "drbd_raw": "",
            "thinpools": [],
            "cockpit_url": cfg["cockpit"],
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
    try:
        resp = urllib.request.urlopen(WITNESS_URL + "/status", timeout=3)
        return json.loads(resp.read())
    except Exception:
        return {"nodes": {}, "error": "unreachable"}

def get_vm_drbd_resource(host: str, vm_name: str) -> str:
    """Parse virsh dumpxml to find the DRBD resource backing this VM's
    first data disk. Back-compat shim over get_vm_disks — prefer that."""
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
        # Back-compat: drbd_resource = first disk's resource. New clients
        # should read the disks[] array instead.
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
            # Back-compat fields — kept until all callers switched to disks[]:
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

    return {"nodes": nodes_data, "vms": vms_data, "witness": get_witness_status()}

# ── FastAPI app ─────────────────────────────────────────────────────────────

app = FastAPI(title="Bedrock Cluster Manager")

# ── WebSocket endpoint ──────────────────────────────────────────────────────

# Last-known cluster state. The state push loop fills it; /ws and /api/cluster
# serve from here instantly so the dashboard never waits on fresh SSH probes.
_last_state: dict = {"nodes": {}, "vms": {}, "witness": {"nodes": {}}}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await hub.connect(ws)
    # Push cached state immediately so the UI renders before the next refresh.
    await hub.send_to(ws, "cluster", _last_state)
    try:
        while True:
            data = await ws.receive_text()
            try:
                msg = json.loads(data)
                channel = msg.get("channel", "")

                if channel == "rpc":
                    result = await handle_rpc(msg.get("method", ""), msg.get("params", {}))
                    await hub.send_to(ws, "rpc.response", {"id": msg.get("id"), "result": result})
            except Exception as e:
                await hub.send_to(ws, "rpc.response", {"id": msg.get("id", 0), "error": str(e)})
    except WebSocketDisconnect:
        hub.disconnect(ws)

async def handle_rpc(method: str, params: dict) -> dict:
    loop = asyncio.get_event_loop()
    if method == "vm.start":
        return await loop.run_in_executor(None, _vm_start, params["name"])
    elif method == "vm.shutdown":
        return await loop.run_in_executor(None, _vm_shutdown, params["name"])
    elif method == "vm.poweroff":
        return await loop.run_in_executor(None, _vm_poweroff, params["name"])
    elif method == "vm.migrate":
        return await loop.run_in_executor(None, _vm_migrate, params["name"], params.get("target_node"))
    raise ValueError(f"Unknown method: {method}")

# ── Background task: push cluster state every 3 seconds ────────────────────

async def state_push_loop():
    global _last_state
    while True:
        try:
            loop = asyncio.get_event_loop()
            state = await loop.run_in_executor(None, build_cluster_state)
            _last_state = state
            await hub.broadcast("cluster", state)
        except Exception as e:
            log.error("State push error: %s", e)
        await asyncio.sleep(3)

_main_loop: Optional[asyncio.AbstractEventLoop] = None


@app.on_event("startup")
async def startup():
    global _last_state, _main_loop
    _main_loop = asyncio.get_running_loop()
    # Seed with cluster.json so the sidebar shows host names instantly.
    cfg = load_cluster()
    _last_state = {
        "nodes": {n: {"name": n, "host": c.get("host", ""), "online": False,
                      "kernel": "", "uptime_since": "", "load": "",
                      "mem_total_mb": 0, "mem_used_mb": 0,
                      "all_vms": [], "running_vms": [], "drbd_raw": "",
                      "cockpit_url": c.get("cockpit", "")}
                  for n, c in cfg.get("nodes", {}).items()},
        "vms": {},
        "witness": {"nodes": {}},
    }
    task_registry().wire(_main_loop, hub.broadcast)
    asyncio.create_task(state_push_loop())
    write_scrape_config(cfg)

# ── REST API (same as before, for curl/scripting) ──────────────────────────

@app.get("/api/cluster")
def api_cluster():
    # Serve cached state. Fresh data lands every 3s via the push loop.
    return _last_state


@app.get("/api/tasks")
def api_tasks():
    """Active + recently-finished tasks. Clients use WS 'task' channel for
    live updates; this endpoint is the snapshot on fresh page load."""
    return task_registry().list()


@app.get("/api/tasks/{task_id}")
def api_task_get(task_id: str):
    t = task_registry().get(task_id)
    if not t:
        raise HTTPException(404, "task not found (finished and aged out, or never existed)")
    from tasks import _serialize
    return _serialize(t)


@app.get("/cluster-info")
def cluster_info():
    """Discovery endpoint — lets `bedrock join` find this cluster."""
    state_file = Path("/etc/bedrock/state.json")
    cluster = load_cluster()
    info = {
        "cluster_name": cluster.get("cluster_name", "bedrock"),
        "cluster_uuid": cluster.get("cluster_uuid", "unknown"),
        "nodes": list(cluster.get("nodes", {}).keys()),
    }
    if state_file.exists():
        s = json.loads(state_file.read_text())
        info["cluster_uuid"] = s.get("cluster_uuid", info["cluster_uuid"])
        info["mgmt_url"] = s.get("mgmt_url", "")
        info["witness_host"] = s.get("witness_host", "")
    return info


class NodeRegister(BaseModel):
    name: str
    host: str
    drbd_ip: Optional[str] = None
    role: str = "compute"
    pubkey: Optional[str] = None


def _append_authorized_key(pubkey: str, target_host: Optional[str] = None):
    """Append pubkey to /root/.ssh/authorized_keys on target_host (or local)."""
    line = pubkey.strip()
    if not line:
        return
    if target_host is None:
        authz = Path("/root/.ssh/authorized_keys")
        authz.parent.mkdir(mode=0o700, exist_ok=True)
        existing = authz.read_text() if authz.exists() else ""
        if line not in existing:
            authz.write_text(existing.rstrip() + "\n" + line + "\n")
            authz.chmod(0o600)
        return
    # On a peer over SSH — mgmt already has SSH trust there (peer joined earlier).
    import shlex as _shlex
    quoted = _shlex.quote(line)
    try:
        ssh_cmd(target_host,
            f"mkdir -p -m 700 /root/.ssh && "
            f"grep -qxF {quoted} /root/.ssh/authorized_keys 2>/dev/null || "
            f"echo {quoted} >> /root/.ssh/authorized_keys && "
            f"chmod 600 /root/.ssh/authorized_keys",
            timeout=10)
    except Exception as e:
        push_log(f"Could not push pubkey to {target_host}: {e}",
                 node="mgmt", app="bedrock-mgmt", level="warn")


def _read_local_pubkey() -> str:
    p = Path("/root/.ssh/id_ed25519.pub")
    return p.read_text().strip() if p.exists() else ""


@app.post("/api/nodes/register")
def register_node(req: NodeRegister):
    """Called by `bedrock join` to register a new node with the cluster.

    SSH mesh: the joining node sends its pubkey; we install it locally (so
    mgmt's paramiko can reach the new node) and fan it out over existing SSH
    trust to every previously-registered peer. In the response we return
    every peer's pubkey so the joiner can trust them back.
    """
    cluster = load_cluster()
    cluster.setdefault("nodes", {})

    # Snapshot existing peers before adding this one, so we know who to fan out to.
    prior_peers = [(name, n) for name, n in cluster["nodes"].items()
                   if n.get("host") and n["host"] != req.host]

    cluster["nodes"][req.name] = {
        "host": req.host,
        "drbd_ip": req.drbd_ip or "",
        "tb_ip": req.drbd_ip or "",  # use DRBD for migration URI (no USB4 in testbed)
        "eno_ip": req.drbd_ip or "",
        "role": req.role,
        "cockpit": f"https://{req.host}:9090",
        "pubkey": (req.pubkey or "").strip(),
    }
    save_cluster(cluster)

    # SSH mesh: (a) trust joiner from mgmt, (b) trust joiner from every prior peer,
    # (c) return every peer's pubkey (including mgmt's) for the joiner to trust back.
    peer_pubkeys = []
    mgmt_pubkey = _read_local_pubkey()
    if mgmt_pubkey:
        peer_pubkeys.append(mgmt_pubkey)
    if req.pubkey:
        _append_authorized_key(req.pubkey)                 # mgmt trusts joiner
        for name, n in prior_peers:
            _append_authorized_key(req.pubkey, n["host"])  # peer trusts joiner
    for name, n in prior_peers:
        if n.get("pubkey"):
            peer_pubkeys.append(n["pubkey"])

    push_log(f"Node {req.name} ({req.host}) registered with cluster",
             node="mgmt", app="bedrock-mgmt", level="info")

    # Return all peer IPs so the joining node can pre-populate known_hosts
    # for virsh migrate / DRBD SSH over both mgmt and replication networks.
    peer_ips = []
    for n in cluster["nodes"].values():
        if n.get("host"): peer_ips.append(n["host"])
        if n.get("drbd_ip"): peer_ips.append(n["drbd_ip"])
    # Cluster key + master's DRBD-ring IP, so the joining node's
    # bedrock-rust daemon comes up with a matching AEAD key and knows
    # which peer to dial. Without these, joiners would generate a fresh
    # cluster_key (witness AEAD wouldn't validate cross-node) and have
    # no peer to replicate from. Phase 5 cutover prerequisite.
    cluster_key_hex = ""
    try:
        from pathlib import Path as _P
        ck = _P("/etc/bedrock/cluster.key")
        if ck.exists():
            cluster_key_hex = ck.read_bytes().hex()
    except Exception:
        pass
    master_drbd_ip = ""
    for n_name, n in cluster.get("nodes", {}).items():
        if "mgmt" in n.get("role", "") and n.get("drbd_ip"):
            master_drbd_ip = n["drbd_ip"]
            break

    return {"status": "registered", "cluster": cluster.get("cluster_name"),
            "cluster_uuid": cluster.get("cluster_uuid", ""),
            "nodes": list(cluster["nodes"].keys()),
            "peer_ips": sorted(set(peer_ips)),
            "peer_pubkeys": peer_pubkeys,
            "cluster_key_hex": cluster_key_hex,
            "master_drbd_ip": master_drbd_ip}


@app.get("/api/nodes")
def list_nodes():
    return load_cluster().get("nodes", {})


# ── ISO library ─────────────────────────────────────────────────────────────

ISO_DIR = Path("/opt/bedrock/iso")
VM_INVENTORY_FILE = Path("/etc/bedrock/vm_inventory.json")


def load_inventory() -> dict:
    if VM_INVENTORY_FILE.exists():
        try: return json.loads(VM_INVENTORY_FILE.read_text())
        except Exception: return {}
    return {}


def save_inventory(inv: dict):
    VM_INVENTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    VM_INVENTORY_FILE.write_text(json.dumps(inv, indent=2))


@app.get("/api/isos")
def api_list_isos():
    """Return the list of .iso files in /opt/bedrock/iso with sizes."""
    if not ISO_DIR.exists(): return []
    out = []
    for p in sorted(ISO_DIR.glob("*.iso")):
        try:
            out.append({"name": p.name, "size_bytes": p.stat().st_size})
        except Exception: continue
    return out


@app.post("/api/isos/upload")
async def api_upload_iso(file: UploadFile = File(...)):
    """Stream-upload an ISO to /opt/bedrock/iso. Chunked to stay memory-safe
    for multi-GB Windows ISOs."""
    if not file.filename.lower().endswith(".iso"):
        raise HTTPException(400, "filename must end in .iso")
    ISO_DIR.mkdir(parents=True, exist_ok=True)
    dst = ISO_DIR / Path(file.filename).name  # strip any directory
    total = 0
    with dst.open("wb") as fh:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk: break
            fh.write(chunk)
            total += len(chunk)
    push_log(f"ISO uploaded: {dst.name} ({total // 1024 // 1024} MB)",
             node="mgmt", app="bedrock-mgmt", level="info")
    return {"status": "uploaded", "name": dst.name, "size_bytes": total}


# ── Import library (VMware/Hyper-V/qcow2 → Bedrock) ──────────────────────

IMPORT_ROOT = Path("/opt/bedrock/imports")
EXPORT_ROOT = Path("/opt/bedrock/exports")
IMPORT_INPUT_FORMATS = {".ova", ".ovf", ".vmdk", ".vhd", ".vhdx",
                        ".qcow2", ".raw", ".img"}


def _inspect_os(src: str, fmt: str) -> dict:
    """Detect the guest OS on an uploaded disk image.

    Order of fallbacks:
      1. virt-inspector with explicit format (authoritative — mounts the
         filesystem + reads registry/os-release).
      2. For VHD / VHDX where libguestfs often fails to introspect the
         container: assume Windows (the Hyper-V-native formats are almost
         exclusively Windows). virt-v2v will re-inspect + correct if wrong.
      3. Unknown.

    Returns dict with os_type, os_distro, os_product_name, os_version,
    os_osinfo, os_detection (which path produced the result). Empty keys
    stay absent so UI can show "unknown" cleanly.
    """
    cmd = ["virt-inspector"]
    if fmt: cmd += ["--format", fmt]
    cmd += ["-a", src]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if r.returncode == 0 and r.stdout.strip():
            import xml.etree.ElementTree as ET
            root = ET.fromstring(r.stdout)
            os_el = root.find(".//operatingsystem")
            if os_el is not None:
                name = (os_el.findtext("name") or "").lower()
                out = {
                    "os_type": name,  # windows / linux / freebsd / ...
                    "os_distro": os_el.findtext("distro") or "",
                    "os_product_name": os_el.findtext("product_name") or "",
                    "os_version": os_el.findtext("major_version") or "",
                    "os_osinfo": os_el.findtext("osinfo") or "",
                    "os_detection": "virt-inspector",
                }
                return {k: v for k, v in out.items() if v or k == "os_detection"}
    except Exception as e:
        push_log(f"virt-inspector failed on {src}: {e}",
                 node="mgmt", app="bedrock-mgmt", level="warn")
    # Fallback: Hyper-V formats are almost always Windows
    if (fmt or "").lower() in ("vpc", "vhdx"):
        return {"os_type": "windows",
                "os_detection": "format-hint (vhd/vhdx → Hyper-V)"}
    return {"os_detection": "none"}


def _import_dir(job_id: str) -> Path:
    # Strict job-id form to prevent traversal
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", job_id):
        raise HTTPException(400, "invalid id")
    return IMPORT_ROOT / job_id


def _import_meta(d: Path) -> dict:
    mp = d / "meta.json"
    if not mp.exists(): return {}
    try: return json.loads(mp.read_text())
    except Exception: return {}


def _write_import_meta(d: Path, meta: dict):
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(json.dumps(meta, indent=2))


@app.get("/api/imports")
def api_imports_list():
    """Every import job with its current status."""
    if not IMPORT_ROOT.exists(): return []
    out = []
    for d in sorted(IMPORT_ROOT.iterdir()):
        if not d.is_dir(): continue
        m = _import_meta(d)
        if m: out.append({**m, "id": d.name})
    # newest first
    out.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return out


@app.get("/api/imports/{job_id}")
def api_import_get(job_id: str):
    d = _import_dir(job_id)
    if not d.exists(): raise HTTPException(404, "no such import")
    m = _import_meta(d) or {"id": job_id, "status": "unknown"}
    m["id"] = job_id
    # Tail of log for the UI
    log_file = d / "log.txt"
    if log_file.exists():
        try:
            txt = log_file.read_text()
            m["log_tail"] = txt[-4000:]
            m["log_size"] = len(txt)
        except Exception: pass
    return m


@app.post("/api/imports/upload")
async def api_imports_upload(file: UploadFile = File(...)):
    """Accept a disk image (VMware/Hyper-V/qcow2/raw/OVA) and stage it for
    conversion. The file is written in 1 MB chunks directly to
    /opt/bedrock/imports/<id>/original.<ext>; conversion is a separate
    step (POST /api/imports/{id}/convert) so long uploads don't block."""
    name = Path(file.filename or "").name
    ext = "".join(Path(name).suffixes[-1:]).lower()  # last suffix only
    if ext not in IMPORT_INPUT_FORMATS:
        raise HTTPException(400,
            f"unsupported extension {ext!r}; want {sorted(IMPORT_INPUT_FORMATS)}")

    # Build a job id: timestamp + slug of original stem
    stem = re.sub(r"[^a-z0-9]+", "-", Path(name).stem.lower()).strip("-")[:40] or "disk"
    job_id = f"{int(time.time())}-{stem}"
    d = _import_dir(job_id)
    d.mkdir(parents=True, exist_ok=True)
    dst = d / f"original{ext}"

    total = 0
    with dst.open("wb") as fh:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk: break
            fh.write(chunk)
            total += len(chunk)

    meta = {
        "id": job_id,
        "original_name": name,
        "input_format": ext.lstrip("."),
        "input_path": str(dst),
        "input_size_bytes": total,
        "status": "uploaded",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    _write_import_meta(d, meta)
    push_log(f"Import uploaded: {name} ({total // 1024 // 1024} MB, id={job_id})",
             node="mgmt", app="bedrock-mgmt", level="info")

    # Inspect the image so the UI can show detected OS and auto-select
    # driver injection on convert. Synchronous (5-30 s typical) so the
    # /convert call that the UI fires right after sees the result in meta.
    fmt = QEMU_FORMAT_MAP.get(ext.lstrip("."))
    loop = asyncio.get_event_loop()
    det = await loop.run_in_executor(None, _inspect_os, str(dst), fmt)
    meta.update(det)
    _write_import_meta(d, meta)
    if det.get("os_type"):
        push_log(f"Import {job_id} OS detected: {det['os_type']} "
                 f"{det.get('os_product_name','')} (via {det['os_detection']})",
                 node="mgmt", app="bedrock-mgmt", level="info")
    return meta


QEMU_FORMAT_MAP = {
    "qcow2": "qcow2", "raw": "raw", "img": "raw",
    "vmdk": "vmdk",  "vhd": "vpc",  "vhdx": "vhdx",
}


def _run_cmd(log_path: Path, cmd: list) -> int:
    """Synchronous subprocess run with log file. Returns exit code."""
    # Give virt-v2v's libguestfs appliance enough memory + tmpfs workspace.
    # Default is 768 MB; on multi-disk OVAs virt-v2v's inner-appliance root
    # fills up with staging data and dies with 'not enough free space on /'.
    # 2048 MB is safe and RAM-cheap (only touched during convert).
    env = None
    if cmd and cmd[0] in ("virt-v2v", "virt-inspector", "virt-win-reg",
                          "virt-filesystems", "guestfish"):
        import os as _os
        env = {**_os.environ, "LIBGUESTFS_MEMSIZE": "2048"}
    with log_path.open("a") as lf:
        lf.write(f"\n# command: {' '.join(cmd)}\n"); lf.flush()
        return subprocess.run(cmd, stdout=lf, stderr=subprocess.STDOUT,
                              env=env).returncode


async def _run_convert(job_id: str, inject_drivers: bool = False):
    """Convert uploaded image → qcow2 at /opt/bedrock/imports/<id>/converted/disk.qcow2.
    Default path: qemu-img (fast, format-only). virt-v2v is invoked for OVA
    (bundled disk+metadata) or when the operator explicitly asked for
    driver injection (Windows imports)."""
    d = _import_dir(job_id)
    meta = _import_meta(d)
    if not meta: return
    src = Path(meta["input_path"])
    ext = meta["input_format"]
    meta["status"] = "converting"
    meta["convert_started_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    meta["injected_drivers"] = bool(inject_drivers)
    _write_import_meta(d, meta)
    push_log(f"Import convert started: {job_id} ({ext}, "
             f"{'virt-v2v+drivers' if inject_drivers or ext in ('ova','ovf') else 'qemu-img'})",
             node="mgmt", app="bedrock-mgmt", level="info")

    log = d / "log.txt"
    log.write_text("")  # reset on retry
    dst_dir = d / "converted"
    if dst_dir.exists(): shutil.rmtree(dst_dir)
    dst_dir.mkdir()
    out_qcow = dst_dir / "disk.qcow2"

    loop = asyncio.get_event_loop()
    rc = 0

    try:
        if ext in ("ova", "ovf") and inject_drivers:
            # Windows OVA path: virt-v2v parses the OVF, inspects the guest,
            # converts each disk to qcow2 and injects viostor/NetKVM on the
            # boot disk. Emits <name>-sda, <name>-sdb, ... plus a .xml sidecar.
            rc = await loop.run_in_executor(None, _run_cmd, log,
                ["virt-v2v", "-v", "-x", "-i", "ova", str(src),
                 "-o", "local", "-os", str(dst_dir), "-of", "qcow2"])
        elif ext in ("ova", "ovf"):
            # Linux / generic OVA path: extract the tar, parse the OVF to get
            # the disk file list in slot order (so disks[0] is the boot disk
            # virt-install's vda wants), and qemu-img convert each one to
            # qcow2 individually. Avoids virt-v2v's libguestfs appliance
            # (which would otherwise boot a tiny Linux to do the same work
            # and occasionally run out of ram-fs space on multi-disk OVAs).
            # Result is byte-identical qcow2s, one per source VMDK.
            extract = d / "ova-extract"
            if extract.exists(): shutil.rmtree(extract)
            extract.mkdir()
            rc = await loop.run_in_executor(None, _run_cmd, log,
                ["tar", "-xf", str(src), "-C", str(extract)])
            if rc == 0:
                ovf_files = list(extract.glob("*.ovf"))
                disk_refs: list[Path] = []
                if ovf_files:
                    # Parse OVF: <References><File ovf:id=... ovf:href=...>,
                    # plus <DiskSection><Disk ovf:fileRef=...>. The order of
                    # Disk elements (+ their VirtualHardwareSection Items)
                    # is the slot order. For a simple OVA, the File order
                    # = the Disk order = slot order.
                    try:
                        import xml.etree.ElementTree as _ET
                        ovf = _ET.parse(ovf_files[0]).getroot()
                        ns = {"ovf": "http://schemas.dmtf.org/ovf/envelope/1"}
                        id_to_href = {}
                        for f in ovf.iter():
                            if f.tag.endswith("}File"):
                                fid = f.attrib.get(f"{{{ns['ovf']}}}id") \
                                      or f.attrib.get("ovf:id") or f.attrib.get("id")
                                href = f.attrib.get(f"{{{ns['ovf']}}}href") \
                                      or f.attrib.get("ovf:href") or f.attrib.get("href")
                                if fid and href: id_to_href[fid] = href
                        disk_order = []
                        for d_el in ovf.iter():
                            if d_el.tag.endswith("}Disk"):
                                fr = (d_el.attrib.get(f"{{{ns['ovf']}}}fileRef")
                                      or d_el.attrib.get("ovf:fileRef")
                                      or d_el.attrib.get("fileRef"))
                                if fr and fr in id_to_href:
                                    disk_order.append(id_to_href[fr])
                        for href in disk_order:
                            p = extract / href
                            if p.exists(): disk_refs.append(p)
                    except Exception as e:
                        push_log(f"OVF parse failed, falling back to glob: {e}",
                                 node="mgmt", app="bedrock-mgmt", level="warn")
                if not disk_refs:
                    # Fallback: glob-find disks in whatever order the tar gives
                    disk_refs = (sorted(extract.glob("*.vmdk"))
                                 + sorted(extract.glob("*.img"))
                                 + sorted(extract.glob("*.raw")))
                if not disk_refs:
                    meta["error"] = "OVA contained no recognisable disks"
                    rc = 1
                else:
                    for i, dp in enumerate(disk_refs):
                        fmt_in = QEMU_FORMAT_MAP.get(
                            dp.suffix.lstrip(".").lower(), "raw")
                        out_path = dst_dir / f"disk{i}.qcow2"
                        rc = await loop.run_in_executor(None, _run_cmd, log,
                            ["qemu-img", "convert", "-p", "-f", fmt_in,
                             "-O", "qcow2", str(dp), str(out_path)])
                        if rc != 0: break
        elif inject_drivers:
            # Windows import path — virt-v2v inspects, rewrites bootloader, inject viostor/NetKVM
            rc = await loop.run_in_executor(None, _run_cmd, log,
                ["virt-v2v", "-v", "-x", "-i", "disk", str(src),
                 "-o", "local", "-os", str(dst_dir), "-of", "qcow2"])
        else:
            fmt_in = QEMU_FORMAT_MAP.get(ext, "raw")
            rc = await loop.run_in_executor(None, _run_cmd, log,
                ["qemu-img", "convert", "-p", "-f", fmt_in, "-O", "qcow2",
                 str(src), str(out_qcow)])

        if rc != 0:
            meta["status"] = "failed"
            meta.setdefault("error", f"convert exit {rc}")
            push_log(f"Import convert FAILED: {job_id} (exit {rc})",
                     node="mgmt", app="bedrock-mgmt", level="error")
        else:
            # Collect every qcow2 output in the right order.
            #   Single-disk (VHDX/qcow2/raw + qemu-img):   disk.qcow2
            #   Linux OVA (our tar + qemu-img):            disk0.qcow2, disk1.qcow2, ...
            #   Windows OVA (virt-v2v -i ova):             <name>-sda, -sdb, ...
            #   Windows single-disk (virt-v2v -i disk):    <name>-sda
            # Order must match guest slot order (first = boot disk), so we
            # sort by the ordering suffix.
            found: list[Path] = []
            if out_qcow.exists():
                found.append(out_qcow)
            # diskN.qcow2 from the manual OVA path
            numbered = sorted(dst_dir.glob("disk[0-9]*.qcow2"),
                              key=lambda p: int(re.search(r"disk(\d+)", p.name).group(1)))
            for p in numbered:
                if p not in found: found.append(p)
            # -sdX from virt-v2v (sorted by letter: sda, sdb, sdc...)
            v2v_outs = sorted([p for p in dst_dir.iterdir()
                               if re.search(r"-sd[a-z]$", p.name)],
                              key=lambda p: p.name)
            for p in v2v_outs:
                if p not in found: found.append(p)
            # Any other *.qcow2 (catchall — won't duplicate)
            for p in sorted(dst_dir.glob("*.qcow2")):
                if p not in found: found.append(p)
            if not found:
                meta["status"] = "failed"; meta["error"] = "no output file"
            else:
                # UTC registry key for Windows (only meaningful on the boot
                # disk which is always found[0]). virt-win-reg mounts the
                # SYSTEM hive from the NTFS on that qcow2.
                if inject_drivers:
                    reg_file = dst_dir / "utc.reg"
                    reg_file.write_text(
                        "Windows Registry Editor Version 5.00\r\n\r\n"
                        "[HKLM\\SYSTEM\\CurrentControlSet\\Control\\"
                        "TimeZoneInformation]\r\n"
                        '"RealTimeIsUniversal"=dword:00000001\r\n'
                    )
                    rc_reg = await loop.run_in_executor(None, _run_cmd, log,
                        ["virt-win-reg", "--merge", str(found[0]), str(reg_file)])
                    meta["utc_registry_applied"] = (rc_reg == 0)
                    if rc_reg == 0:
                        push_log(f"Import {job_id}: RealTimeIsUniversal=1 set "
                                 f"(guest will read RTC as UTC)",
                                 node="mgmt", app="bedrock-mgmt", level="info")
                    else:
                        push_log(f"Import {job_id}: virt-win-reg failed (exit "
                                 f"{rc_reg}); guest may show local-time offset "
                                 f"until NTP corrects it",
                                 node="mgmt", app="bedrock-mgmt", level="warn")

                # Describe each output disk (virtual_size, actual_size).
                disk_metas = []
                for i, p in enumerate(found):
                    iq = json.loads(subprocess.run(
                        ["qemu-img", "info", "--output=json", str(p)],
                        capture_output=True, text=True).stdout or "{}")
                    vsz = iq.get("virtual-size") or 0
                    disk_metas.append({
                        "index": i,
                        "path": str(p),
                        "virtual_size_bytes": vsz,
                        "virtual_size_gb": max(1, (vsz + (1 << 30) - 1) >> 30),
                        "actual_size_bytes": iq.get("actual-size") or 0,
                        "boot": (i == 0),   # first disk = boot
                    })
                meta["status"] = "ready"
                meta["disks"] = disk_metas
                # Back-compat single-disk fields (disks[0])
                meta["disk_path"] = disk_metas[0]["path"]
                meta["virtual_size_bytes"] = disk_metas[0]["virtual_size_bytes"]
                meta["virtual_size_gb"]    = disk_metas[0]["virtual_size_gb"]

                # OS detection from virt-v2v sidecar XML
                xml = next((p for p in dst_dir.glob("*.xml")), None)
                if xml:
                    xt = xml.read_text()
                    m = re.search(r"<name>([^<]+)</name>", xt)
                    if m: meta["detected_name"] = m.group(1)
                    m = re.search(r"<os>.*?<type[^>]*>([^<]+)</type>", xt, re.S)
                    if m: meta["detected_os_type"] = m.group(1)
                    meta["detected_firmware"] = (
                        "uefi" if ("firmware='efi'" in xt or
                                   "<firmware>efi</firmware>" in xt)
                        else "bios"
                    )
                if "detected_firmware" not in meta:
                    # Sniff partition table of the BOOT disk (disks[0])
                    try:
                        head = subprocess.run(
                            ["qemu-img", "dd", "-O", "raw", "bs=512", "count=34",
                             f"if={disk_metas[0]['path']}", "of=/dev/stdout"],
                            capture_output=True, timeout=20).stdout
                        meta["detected_firmware"] = (
                            "uefi" if len(head) >= 520 and head[512:520] == b"EFI PART"
                            else "bios"
                        )
                    except Exception: meta["detected_firmware"] = "bios"
                meta["convert_finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                total_virtual_gb = sum(d["virtual_size_gb"] for d in disk_metas)
                push_log(f"Import convert done: {job_id} → {len(disk_metas)} "
                         f"disk{'s' if len(disk_metas)!=1 else ''}, "
                         f"{total_virtual_gb}G virtual total",
                         node="mgmt", app="bedrock-mgmt", level="info")
    except Exception as e:
        meta["status"] = "failed"; meta["error"] = str(e)
        push_log(f"Import convert EXCEPTION: {job_id}: {e}",
                 node="mgmt", app="bedrock-mgmt", level="error")
    _write_import_meta(d, meta)


class ImportConvertRequest(BaseModel):
    # None → auto-select based on detected OS (Windows → True). Explicit
    # True/False overrides detection.
    inject_drivers: Optional[bool] = None


@app.post("/api/imports/{job_id}/convert")
async def api_import_convert(job_id: str, req: ImportConvertRequest = ImportConvertRequest()):
    d = _import_dir(job_id)
    if not d.exists(): raise HTTPException(404)
    meta = _import_meta(d)
    if meta.get("status") not in ("uploaded", "failed"):
        raise HTTPException(400, f"cannot convert from status '{meta.get('status')}'")
    # Auto-select driver injection from detected OS when caller didn't pick.
    inject = req.inject_drivers
    if inject is None:
        inject = (meta.get("os_type", "").lower() == "windows")
    asyncio.create_task(_run_convert(job_id, inject_drivers=inject))
    meta["status"] = "converting"
    _write_import_meta(d, meta)
    return {"status": "converting", "id": job_id, "inject_drivers": inject}


class ImportCreateVMRequest(BaseModel):
    name: str
    vcpus: int = 2
    ram_mb: int = 2048
    priority: str = "normal"


@app.post("/api/imports/{job_id}/create-vm")
async def api_import_create_vm(job_id: str, req: ImportCreateVMRequest):
    """Fire-and-forget: spinning a 40 GB Windows image into a thin LV +
    virt-install can take a minute or two. Task-tracked so the UI shows
    per-step progress (lvcreate, qemu-img convert, virt-install)."""
    d = _import_dir(job_id)
    meta = _import_meta(d)
    if meta.get("status") != "ready":
        raise HTTPException(400, f"import status {meta.get('status')!r}, need 'ready'")

    task = task_registry().create(
        "vm.create_from_import",
        f"Create VM {req.name} from import ({meta.get('original_name','')})",
        vm_name=req.name, import_id=job_id)

    async def _runner():
        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(
                None, _vm_create_from_import, meta, req, task)
            task.log(f"created: {result}")
            task.succeed()
        except HTTPException as e:
            task.fail(f"{e.status_code}: {e.detail}")
        except Exception as e:
            task.fail(str(e))

    asyncio.create_task(_runner())
    return {"status": "accepted", "task_id": task.id, "name": req.name,
            "import_id": job_id}


@app.delete("/api/imports/{job_id}")
def api_import_delete(job_id: str):
    d = _import_dir(job_id)
    if not d.exists(): raise HTTPException(404)
    shutil.rmtree(d, ignore_errors=True)
    push_log(f"Import deleted: {job_id}", node="mgmt", app="bedrock-mgmt", level="info")
    return {"status": "deleted", "id": job_id}


# ── Export library ─────────────────────────────────────────────────────────

EXPORT_FORMATS = {"qcow2", "vmdk", "vhdx", "raw"}


def _export_dir(job_id: str) -> Path:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", job_id):
        raise HTTPException(400, "invalid id")
    return EXPORT_ROOT / job_id


@app.get("/api/exports")
def api_exports_list():
    if not EXPORT_ROOT.exists(): return []
    out = []
    for d in sorted(EXPORT_ROOT.iterdir()):
        if not d.is_dir(): continue
        m = {}
        mp = d / "meta.json"
        if mp.exists():
            try: m = json.loads(mp.read_text())
            except Exception: continue
        m["id"] = d.name
        out.append(m)
    out.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return out


class ExportRequest(BaseModel):
    format: str = "qcow2"


@app.post("/api/vms/{vm_name}/export")
async def api_vm_export(vm_name: str, req: ExportRequest):
    if req.format not in EXPORT_FORMATS:
        raise HTTPException(400, f"format must be one of {sorted(EXPORT_FORMATS)}")
    # Find the VM + its disk path
    running, host, _ = _vm_host(vm_name)
    s = _vm_get_settings(vm_name)
    src_path = s["disk_path"]
    if not src_path:
        raise HTTPException(500, "VM has no disk_path")
    job_id = f"{int(time.time())}-{vm_name}-{req.format}"
    d = _export_dir(job_id)
    d.mkdir(parents=True, exist_ok=True)
    dst = d / f"{vm_name}.{req.format}"
    meta = {
        "id": job_id, "vm": vm_name, "format": req.format,
        "src_host": host, "src_path": src_path,
        "dst_path": str(dst), "status": "converting",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    (d / "meta.json").write_text(json.dumps(meta, indent=2))
    asyncio.create_task(_run_export(job_id, meta))
    push_log(f"Export started: {vm_name} → {req.format} (id={job_id})",
             node="mgmt", app="bedrock-mgmt", level="info")
    return meta


async def _run_export(job_id: str, meta: dict):
    """qemu-img convert the source disk directly (live — works while VM runs
    because DRBD/raw LVs are read-consistent through QEMU's page cache)."""
    d = _export_dir(job_id)
    log = d / "log.txt"
    fmt_flag = meta["format"]  # qcow2/vmdk/vhdx/raw — all pass straight to qemu-img

    # Determine locality: is the source disk on the mgmt node (this process)?
    # Compare the src_host to every local interface address rather than doing
    # a hostname lookup, which is unreliable on multi-NIC machines.
    import socket as _s
    local_ips = {"127.0.0.1", "localhost"}
    try:
        for fam, _, _, _, sockaddr in _s.getaddrinfo(_s.gethostname(), None):
            local_ips.add(sockaddr[0])
    except Exception: pass
    try:
        # Include every bound IP via /proc/net/fib_trie if possible
        for ln in subprocess.run(
                ["hostname", "-I"], capture_output=True, text=True).stdout.split():
            local_ips.add(ln.strip())
    except Exception: pass

    if meta["src_host"] in local_ips:
        cmd = ["qemu-img", "convert", "-p", "-f", "raw", "-O", fmt_flag,
               meta["src_path"], meta["dst_path"]]
    else:
        # Remote source: ssh + dd → qemu-img. qemu-img can't read /dev/stdin,
        # so stream via a named pipe.
        fifo = str(d / "src.fifo")
        cmd = [
            "bash", "-c",
            f"mkfifo {fifo}; "
            f"( ssh -o BatchMode=yes root@{meta['src_host']} "
            f"'dd if={meta['src_path']} bs=1M status=none' > {fifo} & ) && "
            f"qemu-img convert -p -f raw -O {fmt_flag} {fifo} {meta['dst_path']}; "
            f"rm -f {fifo}"
        ]
    try:
        with log.open("w") as lf:
            lf.write(f"# command: {' '.join(cmd)}\n"); lf.flush()
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=lf, stderr=asyncio.subprocess.STDOUT)
            rc = await proc.wait()
        meta["status"] = "ready" if rc == 0 else "failed"
        if rc == 0:
            try: meta["size_bytes"] = Path(meta["dst_path"]).stat().st_size
            except Exception: pass
            push_log(f"Export done: {meta['vm']} ({meta['format']}, "
                     f"{meta.get('size_bytes',0)//1024//1024} MB)",
                     node="mgmt", app="bedrock-mgmt", level="info")
        else:
            meta["error"] = f"exit {rc}"
            push_log(f"Export FAILED: {meta['vm']} (exit {rc})",
                     node="mgmt", app="bedrock-mgmt", level="error")
    except Exception as e:
        meta["status"] = "failed"; meta["error"] = str(e)
    (d / "meta.json").write_text(json.dumps(meta, indent=2))


@app.get("/api/exports/{job_id}/download")
def api_export_download(job_id: str):
    d = _export_dir(job_id)
    if not d.exists(): raise HTTPException(404)
    mp = d / "meta.json"
    if not mp.exists(): raise HTTPException(404)
    m = json.loads(mp.read_text())
    if m.get("status") != "ready":
        raise HTTPException(400, f"status {m.get('status')!r}")
    from fastapi.responses import FileResponse as _FR
    return _FR(path=m["dst_path"], filename=Path(m["dst_path"]).name,
               media_type="application/octet-stream")


@app.delete("/api/exports/{job_id}")
def api_export_delete(job_id: str):
    d = _export_dir(job_id)
    if not d.exists(): raise HTTPException(404)
    shutil.rmtree(d, ignore_errors=True)
    return {"status": "deleted", "id": job_id}


@app.delete("/api/isos/{name}")
def api_delete_iso(name: str):
    # Prevent path traversal
    safe = Path(name).name
    p = ISO_DIR / safe
    if not p.exists() or not p.is_file():
        raise HTTPException(404, "Not found")
    p.unlink()
    push_log(f"ISO deleted: {safe}", node="mgmt", app="bedrock-mgmt", level="info")
    return {"status": "deleted", "name": safe}


class MigrateRequest(BaseModel):
    target_node: Optional[str] = None


class ConvertRequest(BaseModel):
    target_type: str  # "cattle", "pet", or "vipet"
    peer_nodes: Optional[list] = None  # auto-pick if not specified


class VMDiskSpec(BaseModel):
    size_gb: int


class VMCreateRequest(BaseModel):
    name: str
    vcpus: int = 2
    ram_mb: int = 2048
    disk_gb: int = 20        # size of the primary (boot) disk
    priority: str = "normal"  # low | normal | high
    iso: Optional[str] = None  # filename in /opt/bedrock/iso, optional
    # Additional data disks, in order — vdb, vdc, vdd … Each is another thin LV
    # attached to the VM via virtio. Empty list = single-disk VM (unchanged).
    extra_disks: list[VMDiskSpec] = []

@app.post("/api/vms/{vm_name}/start")
def api_vm_start(vm_name: str):
    return _vm_start(vm_name)

@app.post("/api/vms/{vm_name}/shutdown")
def api_vm_shutdown(vm_name: str):
    return _vm_shutdown(vm_name)

@app.post("/api/vms/{vm_name}/poweroff")
def api_vm_poweroff(vm_name: str):
    return _vm_poweroff(vm_name)

@app.post("/api/vms/{vm_name}/convert")
async def api_vm_convert(vm_name: str, req: ConvertRequest):
    """Fire-and-forget. Returns task_id immediately; the dashboard reads
    progress from /api/tasks (WS 'task' channel).

    All validation happens synchronously BEFORE creating the task, so
    clearly-invalid requests fail with a proper 4xx — they don't get a
    200 / task_id + async task-fail, which would mislead the caller."""
    state = build_cluster_state()
    vm = state["vms"].get(vm_name)
    if not vm: raise HTTPException(404, f"VM {vm_name} not found")
    if vm["state"] != "running":
        raise HTTPException(400, "VM must be running to hot-convert")
    if req.target_type not in ("cattle", "pet", "vipet"):
        raise HTTPException(400, f"Invalid target_type: {req.target_type}")
    nodes_cfg = get_nodes()
    src_name = vm["running_on"]
    current_type = (
        "vipet" if vm.get("drbd_resource")
            and _count_drbd_peers(nodes_cfg[src_name]["host"], vm["drbd_resource"]) >= 3
        else ("pet" if vm.get("drbd_resource") else "cattle")
    )
    if current_type == req.target_type:
        return {"status": "no-op", "current": current_type}
    # Upgrade (cattle/pet → pet/vipet): require enough peers up front so
    # an empty peer_nodes list errors before we burn a task on it.
    rank = {"cattle": 0, "pet": 1, "vipet": 2}
    if rank[req.target_type] > rank[current_type]:
        need_peers = {"pet": 1, "vipet": 2}[req.target_type]
        chosen = req.peer_nodes or [n for n in nodes_cfg if n != src_name]
        # Filter to only nodes we don't already have on this resource
        if current_type == "pet" and req.target_type == "vipet":
            existing = _parse_drbd_res(nodes_cfg[src_name]["host"],
                                       vm["drbd_resource"]) or {}
            chosen = [n for n in chosen if n not in existing.get("peers", [])]
            need_peers = 1
        else:
            chosen = [n for n in chosen if n != src_name]
        chosen = chosen[:need_peers]
        if len(chosen) < need_peers:
            raise HTTPException(400,
                f"{req.target_type} needs {need_peers} peer node(s), "
                f"found {len(chosen)} usable")

    task = task_registry().create(
        "vm.convert", f"VM {vm_name}: {current_type} → {req.target_type}",
        vm_name=vm_name, node=src_name)

    async def _runner():
        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(
                None, _vm_convert, vm_name, req.target_type, req.peer_nodes, task)
            task.log(f"result: {result}")
            task.succeed()
        except HTTPException as e:
            task.fail(f"{e.status_code}: {e.detail}")
        except Exception as e:
            task.fail(str(e))

    asyncio.create_task(_runner())
    return {"status": "accepted", "task_id": task.id,
            "from": current_type, "to": req.target_type}


@app.post("/api/vms/create")
async def api_vm_create(req: VMCreateRequest):
    """Fire-and-forget: returns {task_id} immediately. Create can take 1-2
    minutes for VMs with a big ISO or many disks; we don't block the UI.

    All input validation happens sync up-front so a bad name or ISO path
    returns 4xx immediately — not a 200 / task_id followed by an async
    task-fail (which would mislead the caller)."""
    if not _VM_NAME_RE.match(req.name):
        raise HTTPException(400,
            "VM name: 3-32 chars, lowercase letters/digits/dashes, "
            "start with a letter")
    if req.priority not in _VALID_PRIORITIES:
        raise HTTPException(400, f"priority must be one of {_VALID_PRIORITIES}")
    if req.vcpus < 1 or req.vcpus > 32:
        raise HTTPException(400, "vcpus must be 1-32")
    if req.ram_mb < 128 or req.ram_mb > 131072:
        raise HTTPException(400, "ram_mb must be 128-131072")
    if req.disk_gb < 1 or req.disk_gb > 2048:
        raise HTTPException(400, "disk_gb must be 1-2048")
    for i, d in enumerate(req.extra_disks or []):
        if d.size_gb < 1 or d.size_gb > 8192:
            raise HTTPException(400,
                f"extra_disks[{i}].size_gb must be 1-8192")
    if req.iso:
        iso_name = Path(req.iso).name
        if not (ISO_DIR / iso_name).exists():
            raise HTTPException(400, f"ISO not found: {iso_name}")
    # Existing VM?
    if req.name in build_cluster_state()["vms"]:
        raise HTTPException(409, f"VM {req.name} already exists")

    disk_count = 1 + len(req.extra_disks or [])
    task = task_registry().create(
        "vm.create",
        f"Create VM {req.name} ({req.vcpus} vCPU, {req.ram_mb} MB, "
        f"{disk_count} disk{'s' if disk_count != 1 else ''})",
        vm_name=req.name)

    async def _runner():
        loop = asyncio.get_event_loop()
        try:
            task.step_start("provision + virt-install")
            result = await loop.run_in_executor(None, _vm_create, req)
            task.step_done("provision + virt-install")
            task.log(f"created: {result}")
            task.succeed()
        except HTTPException as e:
            task.fail(f"{e.status_code}: {e.detail}")
        except Exception as e:
            task.fail(str(e))

    asyncio.create_task(_runner())
    return {"status": "accepted", "task_id": task.id, "name": req.name}


@app.delete("/api/vms/{vm_name}")
async def api_vm_delete(vm_name: str):
    """Fire-and-forget. Runs teardown in background; task reports per-disk
    per-node progress so the UI can show what's happening."""
    state = build_cluster_state()
    vm = state["vms"].get(vm_name)
    if not vm:
        raise HTTPException(404, f"Unknown VM: {vm_name}")
    disk_count = len(vm.get("disks") or []) or 1
    task = task_registry().create(
        "vm.delete",
        f"Delete VM {vm_name} ({disk_count} disk{'s' if disk_count != 1 else ''})",
        vm_name=vm_name)

    async def _runner():
        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(None, _vm_delete, vm_name, task)
            task.log(f"deleted: {result}")
            task.succeed()
        except HTTPException as e:
            task.fail(f"{e.status_code}: {e.detail}")
        except Exception as e:
            task.fail(str(e))

    asyncio.create_task(_runner())
    return {"status": "accepted", "task_id": task.id, "name": vm_name}


# ── VM settings (vcpus, ram, disk, priority, cdrom) ─────────────────────────

class ResourcesRequest(BaseModel):
    vcpus: Optional[int] = None
    ram_mb: Optional[int] = None
    disk_gb: Optional[int] = None


class PriorityRequest(BaseModel):
    priority: str  # low | normal | high


class CdromRequest(BaseModel):
    action: str  # "eject" | "insert"
    iso: Optional[str] = None  # required when action=insert


@app.get("/api/vms/{vm_name}/settings")
def api_vm_get_settings(vm_name: str):
    return _vm_get_settings(vm_name)


@app.post("/api/vms/{vm_name}/resources")
def api_vm_resources(vm_name: str, req: ResourcesRequest):
    return _vm_set_resources(vm_name, req)


@app.post("/api/vms/{vm_name}/priority")
def api_vm_priority(vm_name: str, req: PriorityRequest):
    return _vm_set_priority(vm_name, req.priority)


@app.post("/api/vms/{vm_name}/cdrom")
def api_vm_cdrom(vm_name: str, req: CdromRequest):
    return _vm_set_cdrom(vm_name, req.action, req.iso)


class AttachDiskRequest(BaseModel):
    size_gb: int  # thin LV size


@app.post("/api/vms/{vm_name}/disks")
def api_vm_attach_disk(vm_name: str, req: AttachDiskRequest):
    """Attach a new thin-provisioned disk to an existing VM. Live-attach via
    `virsh attach-disk --live --config` so the guest sees the new disk
    immediately and it survives reboot. For pet/ViPet VMs, converting the
    newly-attached disk to DRBD is a separate `pet → pet` re-convert step
    (not implemented in this endpoint; the attach only adds a local LV."""
    if req.size_gb < 1 or req.size_gb > 8192:
        raise HTTPException(400, "size_gb must be 1-8192")
    state = build_cluster_state()
    vm = state["vms"].get(vm_name)
    if not vm: raise HTTPException(404, f"VM {vm_name} not found")
    nodes_cfg = get_nodes()
    host_name = vm.get("running_on") or (vm.get("defined_on") or [None])[0]
    if not host_name: raise HTTPException(503, "VM has no known node")
    host = nodes_cfg[host_name]["host"]

    existing_targets = {d["target"] for d in vm.get("disks", [])}
    # Pick next free vd* letter
    for ch in "bcdefghijklmnop":
        tgt = f"vd{ch}"
        if tgt not in existing_targets: break
    else:
        raise HTTPException(400, "No free virtio target (vda..vdp in use)")
    idx = len(vm.get("disks", []))
    lv_name = f"vm-{vm_name}-disk{idx}"
    lv_path = f"/dev/almalinux/{lv_name}"

    _ensure_thinpool(host)
    push_log(f"Attach disk to {vm_name}: lvcreate {req.size_gb}G ({lv_name})",
             node=host_name, app="bedrock-mgmt")
    out, rc = ssh_cmd_rc(host,
        f"lvcreate -y -V {req.size_gb}G --thin -n {lv_name} almalinux/thinpool "
        f"2>&1", timeout=60)
    if rc != 0 and "already exists" not in out:
        raise HTTPException(500, f"lvcreate failed: {out}")

    # virsh attach-disk — live attach when VM is running, --config either way
    live_flag = "--live" if vm["state"] == "running" else ""
    out, rc = ssh_cmd_rc(host,
        f"virsh attach-disk {vm_name} {lv_path} {tgt} --targetbus virtio "
        f"--driver qemu --subdriver raw --sourcetype block "
        f"{live_flag} --config 2>&1", timeout=30)
    if rc != 0:
        ssh_cmd_rc(host, f"lvremove -f {lv_path} 2>&1", timeout=15)
        raise HTTPException(500, f"attach-disk failed: {out}")

    # Update inventory
    inv = load_inventory()
    entry = inv.setdefault(vm_name, {})
    entry.setdefault("disks", [
        {"index": 0, "lv": f"vm-{vm_name}-disk0",
         "size_gb": entry.get("disk_gb", 0)},
    ])
    entry["disks"].append({"index": idx, "lv": lv_name, "size_gb": req.size_gb})
    save_inventory(inv)

    push_log(f"Attached {req.size_gb}G disk {tgt} to VM {vm_name}",
             node=host_name, app="bedrock-mgmt", level="info")
    return {"status": "attached", "target": tgt, "lv": lv_name,
            "size_gb": req.size_gb}


@app.post("/api/vms/{vm_name}/migrate")
def api_vm_migrate(vm_name: str, req: MigrateRequest = MigrateRequest()):
    return _vm_migrate(vm_name, req.target_node)

# ── VM action implementations ──────────────────────────────────────────────

def _vm_start(vm_name: str) -> dict:
    state = build_cluster_state()
    vm = state["vms"].get(vm_name)
    if not vm: raise HTTPException(404, f"Unknown VM: {vm_name}")
    if vm["state"] == "running": raise HTTPException(400, "Already running")
    resource = vm.get("drbd_resource", "")
    nodes_cfg = get_nodes()

    target = None
    # Prefer node where DRBD is already Primary
    if resource:
        for nname, cfg in nodes_cfg.items():
            if state["nodes"][nname]["online"]:
                drbd = parse_drbd_status(state["nodes"][nname]["drbd_raw"])
                if resource in drbd and drbd[resource]["role"] == "Primary":
                    target = nname; break
    # Fallback: any defined node that's online
    if not target:
        for nname in vm.get("defined_on", []):
            if nname in state["nodes"] and state["nodes"][nname]["online"]:
                target = nname; break
    if not target:
        raise HTTPException(503, "No online node with this VM defined")

    # Promote DRBD if needed (cattle VMs have no DRBD)
    if resource:
        ssh_cmd_rc(nodes_cfg[target]["host"], f"drbdadm primary {resource}")

    out, rc = ssh_cmd_rc(nodes_cfg[target]["host"], f"virsh start {vm_name}")
    if rc != 0: raise HTTPException(500, f"Failed: {out}")
    push_log(f"VM {vm_name} started on {target}", node=target, app="bedrock-mgmt", level="info")
    return {"status": "started", "node": target}


def _vm_shutdown(vm_name: str) -> dict:
    state = build_cluster_state()
    vm = state["vms"].get(vm_name)
    if not vm or vm["state"] != "running": raise HTTPException(400, "Not running")
    nodes_cfg = get_nodes()
    ssh_cmd_rc(nodes_cfg[vm["running_on"]]["host"], f"virsh shutdown {vm_name}")
    push_log(f"VM {vm_name} shutdown requested on {vm['running_on']}",
             node=vm["running_on"], app="bedrock-mgmt")
    return {"status": "shutdown sent"}


def _vm_poweroff(vm_name: str) -> dict:
    state = build_cluster_state()
    vm = state["vms"].get(vm_name)
    if not vm or vm["state"] != "running": raise HTTPException(400, "Not running")
    nodes_cfg = get_nodes()
    ssh_cmd_rc(nodes_cfg[vm["running_on"]]["host"], f"virsh destroy {vm_name}")
    return {"status": "powered off"}


def _vm_migrate(vm_name: str, target_node: str = None) -> dict:
    state = build_cluster_state()
    vm = state["vms"].get(vm_name)
    if not vm or vm["state"] != "running": raise HTTPException(400, "Not running")

    # Multi-disk: virsh migrate handles all disks in one call, but we must
    # cycle allow-two-primaries + primary across EVERY DRBD resource. Cattle
    # disks (no resource) are no-ops.
    resources = [d["drbd_resource"] for d in vm.get("disks", [])
                 if d.get("drbd_resource")]
    if not resources:
        raise HTTPException(400, f"VM {vm_name} has no DRBD resources (cattle VM — cannot migrate)")

    nodes_cfg = get_nodes()
    src_name = vm["running_on"]
    dst_name = target_node or vm["backup_node"]
    if not dst_name or dst_name == src_name: raise HTTPException(400, "No valid target")
    src, dst = nodes_cfg[src_name], nodes_cfg[dst_name]

    # For migration URI, prefer USB4 IP, fall back to DRBD IP, fall back to LAN
    dst_migrate_ip = dst.get("tb_ip") or dst.get("drbd_ip") or dst.get("eno_ip") or dst.get("host")

    for r in resources:
        ssh_cmd(src["host"], f"drbdadm net-options --allow-two-primaries=yes {r}")
        ssh_cmd(dst["host"], f"drbdadm net-options --allow-two-primaries=yes {r}")
        ssh_cmd(dst["host"], f"drbdadm primary {r}")

    t0 = time.time()
    out, rc = ssh_cmd_rc(src["host"],
        f'virsh migrate --live --verbose --unsafe --migrateuri tcp://{dst_migrate_ip} '
        f'{vm_name} qemu+ssh://root@{dst_migrate_ip}/system', timeout=120)
    duration = time.time() - t0

    for r in resources:
        ssh_cmd(src["host"], f"drbdadm secondary {r}")
        ssh_cmd(src["host"], f"drbdadm net-options --allow-two-primaries=no {r}")
        ssh_cmd(dst["host"], f"drbdadm net-options --allow-two-primaries=no {r}")

    if rc != 0:
        push_log(f"VM {vm_name} migration FAILED from {src_name} to {dst_name}: {out}",
                 node=src_name, app="bedrock-mgmt", level="error")
        raise HTTPException(500, f"Migration failed: {out}")
    push_log(f"VM {vm_name} migrated from {src_name} to {dst_name} in {round(duration, 2)}s",
             node=dst_name, app="bedrock-mgmt", level="info")
    return {"status": "migrated", "from": src_name, "to": dst_name, "duration_s": round(duration, 2)}


# ── Workload conversion (cattle ↔ pet ↔ vipet) ──────────────────────────────

def _ensure_thinpool(host: str, vg_name: str = "almalinux", pool: str = "thinpool"):
    """Make sure {vg_name}/{pool} exists on host. Creates a loop-backed VG if needed
    (matches the testbed/bedrock-vm-create pattern so peers are ready on first use)."""
    out = ssh_cmd(host, f"lvs --noheadings -o lv_name {vg_name} 2>/dev/null || true")
    if pool in out.split():
        return
    vg_out = ssh_cmd(host, f"vgs --noheadings -o vg_name 2>/dev/null || true")
    if vg_name not in vg_out.split():
        ssh_cmd(host,
            "truncate -s 80G /var/lib/bedrock-vg.img && "
            "LOOP=$(losetup --find --show /var/lib/bedrock-vg.img) && "
            f"pvcreate -f -y $LOOP >/dev/null && vgcreate {vg_name} $LOOP >/dev/null",
            timeout=30)
    ssh_cmd(host, f"lvcreate -y -l 95%FREE --thinpool {pool} {vg_name} >/dev/null",
            timeout=30)


def _find_vm_disk(host: str, vm_name: str) -> dict:
    """Return {target, source_dev} for the VM's primary block disk."""
    xml = ssh_cmd(host, f"virsh dumpxml {vm_name}")
    import re as _re
    for m in _re.finditer(r"<disk\b[^>]*type=['\"]block['\"][^>]*>(.*?)</disk>",
                          xml, _re.DOTALL):
        chunk = m.group(1)
        src = _re.search(r"<source\s+dev=['\"]([^'\"]+)['\"]", chunk)
        tgt = _re.search(r"<target\s+dev=['\"]([^'\"]+)['\"]", chunk)
        if src and tgt:
            return {"target": tgt.group(1), "source_dev": src.group(1)}
    raise HTTPException(500, f"Cannot find block disk for {vm_name}")


# Process-local reservation set for DRBD minors chosen by in-flight
# converts that haven't yet created their /dev/drbdN. Without this, two
# parallel converts both query `ls /dev/drbd*`, both see "nothing here in
# the target range", both pick the same minor, and one fails at
# `drbdadm create-md` / `up`. The lock below serialises the pick+reserve.
_drbd_minor_lock = threading.Lock()
_drbd_minor_reserved: set[int] = set()


def _next_drbd_minor(hosts: list) -> int:
    """Pick + atomically reserve an unused minor number (1000+) across all
    hosts. The reservation lives until `_release_drbd_minor` is called
    (after the resource is fully up, or on rollback)."""
    with _drbd_minor_lock:
        used = set(_drbd_minor_reserved)
        for h in hosts:
            out = ssh_cmd(h, "ls /dev/drbd* 2>/dev/null | grep -oE '[0-9]+$' || true")
            for n in out.split():
                try: used.add(int(n))
                except ValueError: pass
        for i in range(1000, 1900):
            if i not in used:
                _drbd_minor_reserved.add(i)
                return i
    raise HTTPException(500, "No free DRBD minor")


def _release_drbd_minor(minor: int):
    """Drop the in-process reservation. Called after the DRBD device is up
    (the ssh-ls check will now see /dev/drbdN directly) OR on rollback."""
    with _drbd_minor_lock:
        _drbd_minor_reserved.discard(minor)


def _lv_bytes(host: str, lv_path: str) -> int:
    """Block device size in bytes. Returns 0 if the device doesn't exist
    or blockdev returned nothing — callers (e.g. the silent-truncation
    guard) treat a zero result as "something is wrong, fail loud"."""
    out = ssh_cmd(host, f"blockdev --getsize64 {lv_path} 2>/dev/null || echo 0")
    try:
        return int(out.strip() or "0")
    except (ValueError, AttributeError):
        return 0


def _gen_drbd_res(resource: str, minor: int, peers: list) -> str:
    """peers: list of (node_name, drbd_ip, lv_path, meta_lv_path). 2 or 3 entries.
    External meta-disk keeps the DRBD device the same size as the data LV,
    so virsh blockcopy can pivot 1:1 without size mismatch.
    """
    port = 7000 + minor
    lines = [f"resource {resource} {{",
             "    protocol C;",
             "    net { allow-two-primaries no; after-sb-0pri discard-zero-changes;",
             "          after-sb-1pri discard-secondary; after-sb-2pri disconnect; }"]
    for i, (name, ip, lv, meta) in enumerate(peers):
        lines.append(f"    on {name} {{ node-id {i}; device /dev/drbd{minor}; "
                     f"disk {lv}; address {ip}:{port}; meta-disk {meta}; }}")
    if len(peers) == 2:
        lines.append(f"    connection {{ host {peers[0][0]}; host {peers[1][0]}; }}")
    else:
        lines.append("    connection-mesh { hosts " +
                     " ".join(p[0] for p in peers) + "; }")
    lines.append("}")
    return "\n".join(lines) + "\n"


def _write_drbd_res(hosts: list, resource: str, content: str):
    """Write /etc/drbd.d/<resource>.res on all hosts via SSH. The file name
    matches the resource name so one VM can have multiple .res files
    (vm-foo-disk0.res, vm-foo-disk1.res)."""
    import base64
    b64 = base64.b64encode(content.encode()).decode()
    path = f"/etc/drbd.d/{resource}.res"
    for h in hosts:
        ssh_cmd(h, f"echo {b64} | base64 -d > {path}")


def _vm_convert(vm_name: str, target_type: str, peer_nodes=None,
                task: Optional[Task] = None) -> dict:
    if target_type not in ("cattle", "pet", "vipet"):
        raise HTTPException(400, f"Invalid target_type: {target_type}")

    state = build_cluster_state()
    vm = state["vms"].get(vm_name)
    if not vm: raise HTTPException(404, f"VM {vm_name} not found")
    if vm["state"] != "running":
        raise HTTPException(400, "VM must be running to hot-convert")

    nodes_cfg = get_nodes()
    src_name = vm["running_on"]
    src = nodes_cfg[src_name]
    current_type = "vipet" if vm.get("drbd_resource") and _count_drbd_peers(src["host"], vm["drbd_resource"]) >= 3 \
                   else ("pet" if vm.get("drbd_resource") else "cattle")

    if current_type == target_type:
        return {"status": "no-op", "current": current_type}

    rank = {"cattle": 0, "pet": 1, "vipet": 2}
    if rank[target_type] > rank[current_type]:
        return _vm_convert_upgrade(vm_name, current_type, target_type, src_name, peer_nodes, task)
    else:
        return _vm_convert_downgrade(vm_name, current_type, target_type, src_name, peer_nodes, task)


def _count_drbd_peers(host: str, resource: str) -> int:
    try:
        out = ssh_cmd(host, f"drbdsetup status {resource} --json 2>/dev/null || echo '[]'")
        import json as _json
        data = _json.loads(out)
        if isinstance(data, list) and data:
            return 1 + len(data[0].get("connections", []))
    except Exception: pass
    return 0


def _vm_convert_upgrade(vm_name: str, cur: str, tgt: str, src_name: str,
                         peer_nodes, task: Optional[Task] = None) -> dict:
    """Cattle → pet / cattle → ViPet / pet → ViPet.

    Iterates over every disk the VM has, so multi-disk guests become
    pet/ViPet across ALL their disks. Atomic: if any disk fails mid-way,
    rollback unwinds the changes already made to earlier disks."""
    nodes_cfg = get_nodes()
    src = nodes_cfg[src_name]

    need_peers = {"pet": 1, "vipet": 2}[tgt]
    available = [n for n in nodes_cfg if n != src_name]

    # Enumerate disks the VM actually has. Works for cattle (plain LVs) and
    # for pet→ViPet (DRBD devices). cdroms are excluded by get_vm_disks.
    disks = get_vm_disks(src["host"], vm_name)
    if not disks:
        raise HTTPException(500, f"No disks found on VM {vm_name}")

    if cur == "cattle":
        chosen = (peer_nodes or available)[:need_peers]
        if len(chosen) < need_peers:
            raise HTTPException(400, f"{tgt} needs {need_peers} peers, have {len(chosen)}")

        # Track what we created so we can unwind on failure
        created: list[dict] = []  # [{resource, hosts: [host, lv, meta], target_dev}]
        # Targets we started a blockcopy on; need `virsh blockjob --abort` if it
        # was interrupted, otherwise libvirt keeps disk->blockjob set and all
        # future blockcopies on this disk fail with "already in active block
        # job" until the daemon restarts.
        copy_started: list[str] = []

        def _unwind():
            # First: abort any blockcopy that failed mid-flight, so libvirt
            # clears disk->blockjob. The pivot was never reached (blockcopy
            # raised) so the VM is still on its original LV.
            for tgt in copy_started:
                ssh_cmd_rc(src["host"],
                    f"virsh blockjob {vm_name} {tgt} --abort 2>&1 || true",
                    timeout=15)
                ssh_cmd_rc(src["host"],
                    f"virsh blockjob {vm_name} {tgt} --abort --async 2>&1 || true",
                    timeout=15)
            for c in reversed(created):
                for h, lv, meta in c["hosts"]:
                    ssh_cmd_rc(h, f"drbdadm down {c['resource']} 2>&1 || true", timeout=15)
                    ssh_cmd_rc(h, f"drbdadm wipe-md --force {c['resource']} 2>&1 || true", timeout=15)
                    ssh_cmd_rc(h, f"rm -f /etc/drbd.d/{c['resource']}.res", timeout=5)
                    rm_paths = " ".join(p for p in (lv, meta) if p and "-meta" in (meta or ""))
                    if rm_paths:
                        ssh_cmd_rc(h, f"lvremove -f {rm_paths} 2>&1 || true", timeout=30)
                # Release the minor reservation so another concurrent convert
                # can use it (or its number — this one). Safe regardless of
                # whether the create-md / up calls even ran.
                if "minor" in c:
                    _release_drbd_minor(c["minor"])

        t_start = time.time()
        try:
            converted_disks = []
            for i, disk in enumerate(disks):
                src_lv = disk["backing_lv"]
                target_dev = disk["target"]
                lv_name = src_lv.split("/")[-1]
                vg_name = src_lv.split("/")[-2]
                resource = f"vm-{vm_name}-disk{i}"
                # Meta LV name must be unique per resource; keep the
                # historical `<lv>-meta` suffix so existing resources parse.
                meta_lv_name = f"{lv_name}-meta"
                meta_path = f"/dev/{vg_name}/{meta_lv_name}"

                src_size = _lv_bytes(src["host"], src_lv)
                size_mb = (src_size + 1024*1024 - 1) // (1024*1024)
                # DRBD 9 external metadata size (max-peers=7):
                #   superblock   = 4 KB
                #   bitmap       = 1 bit per 4 KB of data × max_peers
                #                ≈ 1.5 MB per GB of data at max_peers=7
                #   activity log = 32 MB (default)
                #   safety       = 2× headroom
                # Formula: 32 MB base + 2 MB per GB of data. Thin-provisioned
                # so only actually-used meta blocks allocate.
                # Note: DRBD doesn't error on an undersized meta LV — it
                # silently truncates /dev/drbdN to whatever fits. The
                # silent-truncation guard after `drbdadm up` asserts
                # /dev/drbdN size == backing LV size before blockcopy runs,
                # so any future regression here fails loud, pre-pivot.
                size_gb = (src_size + (1 << 30) - 1) >> 30
                meta_mb = max(32, 32 + size_gb * 2)

                step_prefix = f"disk{i} ({target_dev})"
                if task: task.step_start(f"{step_prefix}: create meta LV on source")

                # 1. Create external metadata LV on source for this disk
                ssh_cmd(src["host"],
                        f"lvcreate -V {meta_mb}M -T {vg_name}/thinpool "
                        f"-n {meta_lv_name} -y 2>&1 || true", timeout=30)

                # 2. Create matching data + meta LV on each peer
                peers_info = [(src_name, src.get("drbd_ip") or src["host"],
                               src_lv, meta_path)]
                for pname in chosen:
                    p = nodes_cfg[pname]
                    _ensure_thinpool(p["host"], vg_name)
                    ssh_cmd(p["host"],
                            f"lvcreate -V {size_mb}M -T {vg_name}/thinpool "
                            f"-n {lv_name} -y", timeout=30)
                    ssh_cmd(p["host"],
                            f"lvcreate -V {meta_mb}M -T {vg_name}/thinpool "
                            f"-n {meta_lv_name} -y", timeout=30)
                    peers_info.append((pname, p.get("drbd_ip") or p["host"],
                                       f"/dev/{vg_name}/{lv_name}",
                                       f"/dev/{vg_name}/{meta_lv_name}"))
                all_hosts = [nodes_cfg[n]["host"] for n, _, _, _ in peers_info]
                # record for unwind: hosts + the peer LV paths (we don't
                # remove the source-side original LV — blockcopy will
                # repoint the VM away from it but the original LV stays)
                created.append({
                    "resource": resource,
                    "hosts": [(nodes_cfg[n]["host"],
                               f"/dev/{vg_name}/{lv_name}" if n != src_name else "",
                               f"/dev/{vg_name}/{meta_lv_name}")
                              for n, _, _, _ in peers_info],
                })
                if task: task.step_done(f"{step_prefix}: create meta LV on source")

                if task: task.step_start(f"{step_prefix}: generate DRBD res")
                minor = _next_drbd_minor(all_hosts)
                # Record the minor on the `created` entry so _unwind can
                # release the reservation on failure.
                created[-1]["minor"] = minor
                res_text = _gen_drbd_res(resource, minor, peers_info)
                _write_drbd_res(all_hosts, resource, res_text)
                if task: task.step_done(f"{step_prefix}: generate DRBD res")

                if task: task.step_start(f"{step_prefix}: create-md + up")
                for h in all_hosts:
                    ssh_cmd(h, f"drbdadm create-md --force --max-peers=7 "
                               f"{resource}", timeout=30)
                    ssh_cmd(h, f"drbdadm up {resource}", timeout=30)
                ssh_cmd(src["host"], f"drbdadm primary --force {resource}",
                        timeout=30)
                if task: task.step_done(f"{step_prefix}: create-md + up")

                # SILENT-TRUNCATION GUARD.
                # DRBD silently shrinks the effective /dev/drbdN if the meta
                # LV is too small, if internal meta is used by mistake, or on
                # any other failure path we haven't anticipated. No error,
                # just a shorter device — the blockcopy pivot would then fail
                # with "Copy failed" at 0 % (destination < source). Assert
                # equality HERE so a mismatch is caught before blockcopy
                # touches anything, and with the real byte counts in the log
                # so operators see exactly what went wrong.
                if task: task.step_start(f"{step_prefix}: assert /dev/drbd{minor} == backing LV")
                drbd_bytes = _lv_bytes(src["host"], f"/dev/drbd{minor}")
                if drbd_bytes != src_size:
                    msg = (f"DRBD silent-truncation guard tripped on {resource}: "
                           f"/dev/drbd{minor} = {drbd_bytes} bytes, "
                           f"backing LV = {src_size} bytes (delta "
                           f"{src_size - drbd_bytes} bytes). Meta LV almost "
                           f"certainly too small — check meta_mb formula.")
                    if task: task.step_fail(
                        f"{step_prefix}: assert /dev/drbd{minor} == backing LV", msg)
                    raise HTTPException(500, msg)
                if task: task.step_done(
                    f"{step_prefix}: assert /dev/drbd{minor} == backing LV")

                if task: task.step_start(f"{step_prefix}: blockcopy → /dev/drbd{minor}")
                # Belt-and-braces: clear any stale libvirt blockjob state on
                # this disk before we start. No-op if nothing is pending.
                ssh_cmd_rc(src["host"],
                    f"virsh blockjob {vm_name} {target_dev} --abort 2>&1 || true",
                    timeout=10)
                copy_started.append(target_dev)
                out, rc = ssh_cmd_rc(src["host"],
                    f"virsh blockcopy {vm_name} {target_dev} /dev/drbd{minor} "
                    f"--reuse-external --wait --pivot --verbose "
                    f"--transient-job --blockdev --format raw", timeout=1800)
                if rc != 0:
                    if task: task.step_fail(f"{step_prefix}: blockcopy → /dev/drbd{minor}",
                                            f"rc={rc}: {out[-400:]}")
                    raise HTTPException(500, f"blockcopy failed on disk{i}: {out}")
                # Blockcopy succeeded + pivoted → target_dev is no longer in
                # the `needs-abort` set (pivot drops the mirror).
                if target_dev in copy_started:
                    copy_started.remove(target_dev)
                if task: task.step_done(f"{step_prefix}: blockcopy → /dev/drbd{minor}")
                converted_disks.append({"index": i, "target": target_dev,
                                        "resource": resource, "minor": minor})
                # DRBD device is now live cluster-wide; future ssh-ls checks
                # will see /dev/drbd{minor} directly — drop the reservation.
                _release_drbd_minor(minor)

            # After all disks succeed: define VM on peers so migration works.
            if task: task.step_start("define VM on peers")
            xml_text = ssh_cmd(src["host"], f"virsh dumpxml {vm_name}", timeout=15)
            import base64 as _b64
            xml_b64 = _b64.b64encode(xml_text.encode()).decode()
            for pname in chosen:
                ph = nodes_cfg[pname]["host"]
                ssh_cmd(ph, f"echo {xml_b64} | base64 -d > /tmp/{vm_name}.xml && "
                            f"virsh define /tmp/{vm_name}.xml >/dev/null", timeout=15)
            if task: task.step_done("define VM on peers")

            dur = round(time.time() - t_start, 2)
            push_log(f"Convert {vm_name}: {cur} → {tgt} in {dur}s "
                     f"({len(converted_disks)} disk(s))",
                     node=src_name, app="bedrock-mgmt", level="info")
            return {"status": "converted", "from": cur, "to": tgt,
                    "disks": converted_disks, "duration_s": dur,
                    "peers": [src_name] + chosen}
        except Exception as e:
            push_log(f"Convert {vm_name}: FAILED ({e}) — unwinding",
                     node=src_name, app="bedrock-mgmt", level="error")
            _unwind()
            raise

    elif cur == "pet" and tgt == "vipet":
        # Add a third peer to every existing DRBD resource the VM has.
        resources = [d["drbd_resource"] for d in disks if d.get("drbd_resource")]
        if not resources:
            raise HTTPException(500, f"No DRBD resources found on {vm_name}")

        chosen = peer_nodes or []
        if not chosen:
            # Pick a node not already in the first resource's peer list
            first_existing = _parse_drbd_res(src["host"], resources[0]) or {}
            chosen = [n for n in available if n not in first_existing.get("peers", [])][:1]
        if not chosen:
            raise HTTPException(400, "vipet needs a third peer")
        new_peer = chosen[0]
        p = nodes_cfg[new_peer]

        added = []
        t_start = time.time()
        for i, resource in enumerate(resources):
            existing = _parse_drbd_res(src["host"], resource)
            if not existing:
                raise HTTPException(500, f"Cannot parse existing {resource}")
            vg_name = existing["lv_vg"]
            lv_name = existing["lv_name"]
            meta_lv_name = f"{lv_name}-meta"
            size_mb = (existing["size_bytes"] + 1024*1024 - 1) // (1024*1024)
            # Meta LV sized to match the other peers — see _vm_convert_upgrade
            # cattle→pet path for the formula derivation.
            size_gb = (existing["size_bytes"] + (1 << 30) - 1) >> 30
            meta_mb = max(32, 32 + size_gb * 2)

            step_prefix = f"disk{i} ({resource})"
            if task: task.step_start(f"{step_prefix}: LVs on new peer {new_peer}")
            _ensure_thinpool(p["host"], vg_name)
            ssh_cmd(p["host"], f"lvcreate -V {size_mb}M -T {vg_name}/thinpool "
                               f"-n {lv_name} -y", timeout=30)
            ssh_cmd(p["host"], f"lvcreate -V {meta_mb}M -T {vg_name}/thinpool "
                               f"-n {meta_lv_name} -y", timeout=30)
            if task: task.step_done(f"{step_prefix}: LVs on new peer {new_peer}")

            peers_info = [(n, nodes_cfg[n].get("drbd_ip") or nodes_cfg[n]["host"],
                           existing["lv_path"], existing["meta_path"])
                          for n in existing["peers"]]
            peers_info.append((new_peer, p.get("drbd_ip") or p["host"],
                               f"/dev/{vg_name}/{lv_name}",
                               f"/dev/{vg_name}/{meta_lv_name}"))
            minor = existing["minor"]
            res_text = _gen_drbd_res(resource, minor, peers_info)
            all_hosts = [nodes_cfg[n]["host"] for n, _, _, _ in peers_info]
            _write_drbd_res(all_hosts, resource, res_text)

            if task: task.step_start(f"{step_prefix}: create-md + adjust")
            ssh_cmd(p["host"], f"drbdadm create-md --force --max-peers=7 "
                               f"{resource}", timeout=30)
            for h in all_hosts:
                ssh_cmd(h, f"drbdadm adjust {resource} 2>&1 || true", timeout=30)
            ssh_cmd(p["host"], f"drbdadm up {resource}", timeout=30)
            if task: task.step_done(f"{step_prefix}: create-md + adjust")
            added.append(resource)

        # Define VM on new peer (once; shared XML for all disks)
        if task: task.step_start(f"define VM on new peer {new_peer}")
        xml_text = ssh_cmd(src["host"], f"virsh dumpxml {vm_name}", timeout=15)
        import base64 as _b64
        xml_b64 = _b64.b64encode(xml_text.encode()).decode()
        ssh_cmd(p["host"], f"echo {xml_b64} | base64 -d > /tmp/{vm_name}.xml && "
                            f"virsh define /tmp/{vm_name}.xml >/dev/null", timeout=15)
        if task: task.step_done(f"define VM on new peer {new_peer}")

        dur = round(time.time() - t_start, 2)
        push_log(f"Convert {vm_name}: pet → vipet in {dur}s "
                 f"({len(added)} resource(s) added peer {new_peer})",
                 node=src_name, app="bedrock-mgmt", level="info")
        return {"status": "converted", "from": cur, "to": tgt,
                "resources": added, "added_peer": new_peer,
                "duration_s": dur}


def _vm_convert_downgrade(vm_name: str, cur: str, tgt: str, src_name: str,
                           peer_nodes, task: Optional[Task] = None) -> dict:
    """ViPet → pet / pet → cattle / ViPet → cattle. Iterates over every
    DRBD resource the VM has (one per disk)."""
    nodes_cfg = get_nodes()
    src = nodes_cfg[src_name]
    disks = get_vm_disks(src["host"], vm_name)
    resources = [d["drbd_resource"] for d in disks if d.get("drbd_resource")]
    if not resources:
        raise HTTPException(500, f"No DRBD resources found on {vm_name}")

    if cur == "vipet" and tgt == "pet":
        # Pick one peer to drop (not src). Use first resource's peer list
        # to make the choice; we'll drop the same peer from every resource.
        first_existing = _parse_drbd_res(src["host"], resources[0]) or {}
        candidates = [n for n in first_existing.get("peers", []) if n != src_name]
        drop_name = (peer_nodes[0] if peer_nodes else (candidates[0] if candidates else None))
        if not drop_name or drop_name == src_name:
            raise HTTPException(400, "Cannot drop primary / no drop candidate")
        drop = nodes_cfg[drop_name]

        # 1. Undefine VM on dropped peer (once for all disks)
        if task: task.step_start(f"undefine VM on {drop_name}")
        ssh_cmd(drop["host"], f"virsh undefine {vm_name} 2>&1 || true", timeout=15)
        if task: task.step_done(f"undefine VM on {drop_name}")

        # 2. Per-resource: tear down DRBD on drop, rewrite config on kept, remove LVs
        for i, resource in enumerate(resources):
            existing = _parse_drbd_res(src["host"], resource)
            if not existing: continue
            step_prefix = f"disk{i} ({resource})"

            if task: task.step_start(f"{step_prefix}: drop DRBD on {drop_name}")
            ssh_cmd(drop["host"], f"drbdadm down {resource} 2>&1 || true", timeout=30)
            ssh_cmd(drop["host"], f"drbdadm wipe-md --force {resource} 2>&1 || true", timeout=30)

            remaining = [(n, nodes_cfg[n].get("drbd_ip") or nodes_cfg[n]["host"],
                          existing["lv_path"], existing["meta_path"])
                         for n in existing["peers"] if n != drop_name]
            minor = existing["minor"]
            res_text = _gen_drbd_res(resource, minor, remaining)
            kept_hosts = [nodes_cfg[n]["host"] for n, _, _, _ in remaining]
            _write_drbd_res(kept_hosts, resource, res_text)
            ssh_cmd(drop["host"], f"rm -f /etc/drbd.d/{resource}.res", timeout=10)

            drop_idx = existing["peers"].index(drop_name)
            for h in kept_hosts:
                ssh_cmd(h, f"drbdsetup disconnect {resource} {drop_idx} --force 2>&1 || true", timeout=15)
                ssh_cmd(h, f"drbdsetup del-peer {resource} {drop_idx} --force 2>&1 || true", timeout=15)
                ssh_cmd(h, f"drbdadm adjust {resource} 2>&1 || true", timeout=30)

            ssh_cmd(drop["host"],
                    f"lvremove -f {existing['lv_path']} {existing['meta_path']} 2>&1 || true",
                    timeout=30)
            if task: task.step_done(f"{step_prefix}: drop DRBD on {drop_name}")

        push_log(f"Convert {vm_name}: vipet → pet (dropped {drop_name}, "
                 f"{len(resources)} resource(s))",
                 node=src_name, app="bedrock-mgmt", level="info")
        return {"status": "converted", "from": cur, "to": tgt,
                "dropped": drop_name, "resources": resources}

    elif cur in ("pet", "vipet") and tgt == "cattle":
        # Pivot every DRBD device back to its raw LV, tear down DRBD, drop peer LVs.
        t_start = time.time()
        # Collect all peers affected across all resources (they should overlap).
        all_peer_names: set[str] = set()
        per_resource: list[dict] = []
        for r in resources:
            existing = _parse_drbd_res(src["host"], r)
            if not existing:
                raise HTTPException(500, f"Cannot parse {r}")
            per_resource.append({"resource": r, "existing": existing})
            all_peer_names.update(existing["peers"])

        # Pivot each disk from /dev/drbdN → raw LV (same backing bytes)
        for i, pr in enumerate(per_resource):
            existing = pr["existing"]
            # Find the disk in the VM XML that matches this resource's minor
            target_dev = None
            for d in disks:
                if d.get("drbd_minor") == existing["minor"]:
                    target_dev = d["target"]; break
            if target_dev is None:
                raise HTTPException(500, f"Cannot match disk for resource {pr['resource']}")
            step_prefix = f"disk{i} ({pr['resource']})"
            if task: task.step_start(f"{step_prefix}: pivot {target_dev} → {existing['lv_path']}")
            out, rc = ssh_cmd_rc(src["host"],
                f"virsh blockcopy {vm_name} {target_dev} {existing['lv_path']} "
                f"--reuse-external --wait --pivot --verbose --transient-job "
                f"--blockdev --format raw", timeout=1800)
            if rc != 0:
                if task: task.step_fail(f"{step_prefix}: pivot {target_dev} → {existing['lv_path']}",
                                        f"rc={rc}: {out[-400:]}")
                raise HTTPException(500, f"blockcopy pivot failed on {pr['resource']}: {out}")
            if task: task.step_done(f"{step_prefix}: pivot {target_dev} → {existing['lv_path']}")

        # Undefine VM on non-primary peers (once)
        for n in all_peer_names:
            if n == src_name: continue
            if n not in nodes_cfg: continue
            ssh_cmd(nodes_cfg[n]["host"], f"virsh undefine {vm_name} 2>&1 || true", timeout=15)

        # For every resource, tear DRBD down on every peer, remove peer data LVs,
        # remove only meta on primary (data LV IS the VM disk now).
        for i, pr in enumerate(per_resource):
            existing = pr["existing"]
            resource = pr["resource"]
            step_prefix = f"disk{i} ({resource})"
            if task: task.step_start(f"{step_prefix}: tear DRBD down + remove LVs")
            for n in existing["peers"]:
                if n not in nodes_cfg: continue
                h = nodes_cfg[n]["host"]
                ssh_cmd(h, f"drbdadm down {resource} 2>&1 || true", timeout=30)
                ssh_cmd(h, f"drbdadm wipe-md --force {resource} 2>&1 || true", timeout=30)
                ssh_cmd(h, f"rm -f /etc/drbd.d/{resource}.res", timeout=10)
                if n == src_name:
                    ssh_cmd(h, f"lvremove -f {existing['meta_path']} 2>&1 || true", timeout=30)
                else:
                    ssh_cmd(h, f"lvremove -f {existing['lv_path']} "
                               f"{existing['meta_path']} 2>&1 || true", timeout=30)
            if task: task.step_done(f"{step_prefix}: tear DRBD down + remove LVs")

        dur = round(time.time() - t_start, 2)

        push_log(f"Convert {vm_name}: {cur} → cattle in {dur}s",
                 node=src_name, app="bedrock-mgmt", level="info")
        return {"status": "converted", "from": cur, "to": tgt, "duration_s": dur}


def _parse_drbd_res(host: str, resource: str) -> dict:
    """Parse /etc/drbd.d/<resource>.res for peers, LV path, meta path, minor, size."""
    try:
        txt = ssh_cmd(host, f"cat /etc/drbd.d/{resource}.res 2>/dev/null")
    except Exception:
        return {}
    import re as _re
    peers, lv_path, meta_path, minor = [], "", "", 0
    for m in _re.finditer(
        r"on\s+(\S+)\s*\{[^}]*device\s+/dev/drbd(\d+)[^}]*disk\s+(\S+);[^}]*"
        r"meta-disk\s+(\S+);", txt, _re.DOTALL):
        peers.append(m.group(1))
        minor = int(m.group(2))
        lv_path = m.group(3)
        meta_path = m.group(4)
    if not lv_path:
        return {}
    parts = lv_path.split("/")
    lv_name, vg_name = parts[-1], parts[-2]
    try:
        size = _lv_bytes(host, lv_path)
    except Exception:
        size = 0
    return {"peers": peers, "lv_path": lv_path, "lv_name": lv_name,
            "lv_vg": vg_name, "meta_path": meta_path,
            "minor": minor, "size_bytes": size}


# ── VM creation (cattle, optionally ISO-booted) ─────────────────────────────

_VM_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}[a-z0-9]$")
_VALID_PRIORITIES = ("low", "normal", "high")
# Maps priority → libvirt cpu_shares (cgroup weight; default is 1024).
# Powers of 2 on either side so the relative weights are clearly visible.
PRIORITY_CPU_SHARES = {"low": 256, "normal": 1024, "high": 4096}
ISO_MOUNT_DIR = "/mnt/isos"  # identical on every cluster node (bind/NFS)


def _mgmt_node_name() -> str:
    """Return the cluster.json node name of the mgmt host (where ISOs live)."""
    cfg = get_nodes()
    for name, node in cfg.items():
        if "mgmt" in node.get("role", ""):
            return name
    # Fallback: first node
    return next(iter(cfg)) if cfg else ""


def _vm_create(req) -> dict:
    """Provision a cattle VM on the mgmt node. ISO optional.
    Stores priority + creation metadata in /etc/bedrock/vm_inventory.json."""
    if not _VM_NAME_RE.match(req.name):
        raise HTTPException(400, "VM name: 3-32 chars, lowercase letters/digits/dashes, start with a letter")
    if req.priority not in _VALID_PRIORITIES:
        raise HTTPException(400, f"priority must be one of {_VALID_PRIORITIES}")
    if req.vcpus < 1 or req.vcpus > 32:
        raise HTTPException(400, "vcpus must be 1-32")
    if req.ram_mb < 128 or req.ram_mb > 131072:
        raise HTTPException(400, "ram_mb must be 128-131072")
    if req.disk_gb < 1 or req.disk_gb > 2048:
        raise HTTPException(400, "disk_gb must be 1-2048")

    # Existing VM?
    state = build_cluster_state()
    if req.name in state["vms"]:
        raise HTTPException(409, f"VM {req.name} already exists")

    home_name = _mgmt_node_name()
    nodes_cfg = get_nodes()
    home = nodes_cfg.get(home_name)
    if not home:
        raise HTTPException(500, "No mgmt node found in cluster.json")
    host = home["host"]

    # Validate ISO if given
    iso_path = ""
    if req.iso:
        iso_name = Path(req.iso).name  # prevent traversal
        # Reference via the cluster-wide auto-mount path — identical on every
        # node so future cross-node creates work without changing this arg.
        iso_path = f"{ISO_MOUNT_DIR}/{iso_name}"
        if not (ISO_DIR / iso_name).exists():
            raise HTTPException(400, f"ISO not found: {iso_name}")

    # All disks: disk0 is the primary/boot disk (req.disk_gb), any additional
    # entries from req.extra_disks become vdb, vdc, ... at their given sizes.
    extra = req.extra_disks or []
    disks_plan: list[dict] = []
    for i, spec in enumerate([VMDiskSpec(size_gb=req.disk_gb)] + extra):
        if spec.size_gb < 1 or spec.size_gb > 8192:
            raise HTTPException(400, f"disk{i} size_gb must be 1-8192")
        lv_name = f"vm-{req.name}-disk{i}"
        disks_plan.append({
            "index": i, "lv_name": lv_name,
            "lv_path": f"/dev/almalinux/{lv_name}",
            "size_gb": spec.size_gb,
        })

    # 1. Ensure thin pool, create every thin LV
    _ensure_thinpool(host)
    for d in disks_plan:
        push_log(f"Create VM {req.name}: lvcreate {d['size_gb']}G thin "
                 f"({d['lv_name']}) on {home_name}",
                 node=home_name, app="bedrock-mgmt")
        out, rc = ssh_cmd_rc(host,
            f"lvcreate -y -V {d['size_gb']}G --thin -n {d['lv_name']} "
            f"almalinux/thinpool", timeout=30)
        if rc != 0 and "already exists" not in out:
            # Unwind any LVs we already made
            for prev in disks_plan[:d["index"]]:
                ssh_cmd_rc(host, f"lvremove -f {prev['lv_path']} 2>&1", timeout=15)
            raise HTTPException(500, f"lvcreate {d['lv_name']} failed: {out}")

    # 2. virt-install — with or without CDROM. Always attach virtio-win.iso
    #    as a 2nd CDROM when any ISO is used: Windows Setup needs it for
    #    viostor+NetKVM; Linux installs ignore it.
    virtio_extra = ""
    if iso_path and (ISO_DIR / "virtio-win.iso").exists():
        virtio_extra = (f" --disk path={ISO_MOUNT_DIR}/virtio-win.iso,"
                        "device=cdrom,bus=sata,readonly=on")
    cdrom_arg = f"--cdrom {iso_path}{virtio_extra}" if iso_path else "--import"
    boot_arg = "--boot cdrom,hd" if iso_path else "--boot hd"

    # Build the --disk argument list: one per data disk.
    disk_args = " ".join(
        f"--disk path={d['lv_path']},format=raw,bus=virtio,cache=none,discard=unmap"
        for d in disks_plan)

    vi_cmd = (
        f"virt-install "
        f"--name {req.name} "
        f"--vcpus {req.vcpus} "
        f"--ram {req.ram_mb} "
        f"{disk_args} "
        f"--network bridge=br0,model=virtio "
        f"--graphics vnc,listen=0.0.0.0 "
        f"--channel unix,target_type=virtio,name=org.qemu.guest_agent.0 "
        f"--os-variant detect=on,name=generic "
        f"--noautoconsole "
        f"{cdrom_arg} "
        f"{boot_arg} "
        f"2>&1"
    )
    push_log(f"Create VM {req.name}: virt-install (vcpus={req.vcpus}, "
             f"ram={req.ram_mb}MB, disks={len(disks_plan)}, "
             f"iso={req.iso or 'none'})",
             node=home_name, app="bedrock-mgmt")
    out, rc = ssh_cmd_rc(host, vi_cmd, timeout=120)
    if rc != 0:
        # Clean up all LVs so the name is free for retry
        for d in disks_plan:
            ssh_cmd_rc(host, f"lvremove -f {d['lv_path']} 2>&1", timeout=15)
        raise HTTPException(500, f"virt-install failed: {out[-400:]}")

    # 3. Apply priority → cpu_shares (cgroup weight, default 1024)
    shares = PRIORITY_CPU_SHARES[req.priority]
    ssh_cmd_rc(host, f"virsh schedinfo {req.name} --live --config "
                     f"cpu_shares={shares} 2>&1", timeout=15)

    # 4. Save inventory
    inv = load_inventory()
    inv[req.name] = {
        "priority": req.priority,
        "vcpus": req.vcpus,
        "ram_mb": req.ram_mb,
        "disk_gb": req.disk_gb,        # primary disk size (back-compat)
        "disks": [                      # full per-disk record (multi-disk aware)
            {"index": d["index"], "lv": d["lv_name"], "size_gb": d["size_gb"]}
            for d in disks_plan
        ],
        "iso": req.iso,
        "home_node": home_name,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "created_by": "dashboard",
    }
    save_inventory(inv)

    disk_summary = ", ".join(f"disk{d['index']}={d['size_gb']}G" for d in disks_plan)
    push_log(f"Created VM {req.name} on {home_name} "
             f"(cattle, {req.vcpus}vCPU, {req.ram_mb}MB, {disk_summary}, "
             f"priority={req.priority}, cpu_shares={shares})",
             node=home_name, app="bedrock-mgmt", level="info")
    return {"status": "created", "name": req.name, "node": home_name,
            "disks": [d["lv_name"] for d in disks_plan]}


def _vm_create_from_import(meta: dict, req, task: Optional[Task] = None) -> dict:
    """Turn a converted import (qcow2 on mgmt node) into a cattle VM.
    Creates a thin LV sized to the qcow2 virtual size, qemu-img converts the
    qcow2 into the LV (raw), then virt-installs with machine=q35, UEFI
    firmware, clock=UTC. Marks the import meta as consumed."""
    if not _VM_NAME_RE.match(req.name):
        raise HTTPException(400, "invalid VM name (3-32 chars, lowercase)")
    if req.priority not in _VALID_PRIORITIES:
        raise HTTPException(400, f"priority must be one of {_VALID_PRIORITIES}")

    state = build_cluster_state()
    if req.name in state["vms"]:
        raise HTTPException(409, f"VM {req.name} already exists")

    home_name = _mgmt_node_name()
    nodes_cfg = get_nodes()
    host = nodes_cfg[home_name]["host"]

    # Multi-disk imports: OVA with multiple VMDKs produces meta['disks'] with
    # one entry per disk. Single-disk imports (VHDX/qcow2/etc) still fill in
    # disk_path/virtual_size_gb so we synthesise a one-element disks list
    # for uniform iteration below.
    src_disks = meta.get("disks") or [{
        "index": 0,
        "path": meta.get("disk_path", ""),
        "virtual_size_bytes": meta.get("virtual_size_bytes", 0),
        "virtual_size_gb": meta.get("virtual_size_gb", 20),
        "actual_size_bytes": 0,
        "boot": True,
    }]
    for sd in src_disks:
        if not sd.get("path") or not Path(sd["path"]).exists():
            raise HTTPException(500,
                f"converted disk {sd.get('path','?')} is gone — re-run convert?")

    # Firmware: trust the inspection result from _run_convert if available.
    # Otherwise sniff the BOOT disk's partition table here. Rationale: a BIOS
    # -boot disk can't boot on UEFI firmware — Windows traps 0x7B, Linux drops
    # to EFI shell. Match the source to avoid the footgun.
    boot_src = next((d for d in src_disks if d.get("boot")), src_disks[0])
    firmware = meta.get("detected_firmware")
    if firmware not in ("bios", "uefi"):
        firmware = "bios"
        try:
            head = subprocess.run(
                ["qemu-img", "dd", "-O", "raw", "bs=512", "count=34",
                 f"if={boot_src['path']}", "of=/dev/stdout"],
                capture_output=True, timeout=20).stdout
            if len(head) >= 520 and head[512:520] == b"EFI PART":
                firmware = "uefi"
        except Exception: pass

    _ensure_thinpool(host)

    # Pre-flight: thin-pool must fit the SUM of actual sizes of all disks.
    total_actual_b = sum(int(d.get("actual_size_bytes") or 0) for d in src_disks)
    if not total_actual_b:
        # Fallback when we couldn't read actual-size from the qcow2
        for sd in src_disks:
            try:
                iq = json.loads(subprocess.run(
                    ["qemu-img", "info", "--output=json", sd["path"]],
                    capture_output=True, text=True).stdout or "{}")
                sd["actual_size_bytes"] = int(iq.get("actual-size") or 0)
            except Exception: pass
        total_actual_b = sum(int(d.get("actual_size_bytes") or 0) for d in src_disks)
    pool_info, _ = ssh_cmd_rc(host,
        "lvs --noheadings --units b --nosuffix --separator '|' "
        "-o lv_size,data_percent almalinux/thinpool 2>/dev/null | head -1",
        timeout=10)
    try:
        parts = [p.strip() for p in pool_info.split("|") if p.strip()]
        pool_size_b = int(parts[0]); pool_used_pct = float(parts[1])
        pool_free_b = int(pool_size_b * (100.0 - pool_used_pct) / 100.0)
        need_b = total_actual_b or \
                 sum(d["virtual_size_gb"] for d in src_disks) * (1 << 30)
        if pool_free_b < need_b + (1 << 30):  # +1 GB slack
            raise HTTPException(507,
                f"Thin pool on {home_name} has "
                f"{pool_free_b // (1<<30)} GB free; this import needs "
                f"{need_b // (1<<30)} GB + 1 GB slack. Free space or grow "
                f"the pool before retrying.")
    except HTTPException:
        raise
    except Exception:
        pass

    # Per-disk plan: one LV per source disk, named vm-<vm>-disk0/1/2...
    disks_plan = []
    for sd in src_disks:
        vgb = sd["virtual_size_gb"] or 1
        ln = f"vm-{req.name}-disk{sd['index']}"
        disks_plan.append({
            "index": sd["index"],
            "lv_name": ln,
            "lv_path": f"/dev/almalinux/{ln}",
            "size_gb": vgb,
            "size_mb": max(vgb * 1024, 1024),
            "src_qcow": sd["path"],
        })

    # 1. lvcreate + qemu-img convert for every disk. Iterative, unwind on fail.
    created_lvs: list[str] = []
    for d in disks_plan:
        step_name = f"disk{d['index']}: lvcreate + qemu-img convert ({d['size_gb']} GB)"
        if task: task.step_start(step_name)
        push_log(f"Import {meta['id']} → create VM {req.name}: "
                 f"lvcreate {d['size_gb']}G thin ({d['lv_name']})",
                 node=home_name, app="bedrock-mgmt", level="info")
        out, rc = ssh_cmd_rc(host,
            f"lvcreate -y -V {d['size_mb']}M --thin -n {d['lv_name']} "
            f"almalinux/thinpool 2>&1", timeout=60)
        if rc != 0 and "already exists" not in out:
            for lv in created_lvs:
                ssh_cmd_rc(host, f"lvremove -f {lv} 2>&1", timeout=15)
            if task: task.step_fail(step_name, out[-300:])
            raise HTTPException(500, f"lvcreate {d['lv_name']} failed: {out}")
        created_lvs.append(d["lv_path"])
        # Sparse-preserving convert into the LV
        out, rc = ssh_cmd_rc(host,
            f"qemu-img convert -p -n -S 4k --target-is-zero -O raw "
            f"{d['src_qcow']} {d['lv_path']} 2>&1", timeout=3600)
        if rc != 0:
            for lv in created_lvs:
                ssh_cmd_rc(host, f"lvremove -f {lv} 2>&1", timeout=30)
            if task: task.step_fail(step_name, (out or "")[-300:])
            raise HTTPException(500,
                f"qemu-img convert {d['lv_name']} failed:\n" + (out or "(no output)"))
        if task: task.step_done(step_name)

    # virt-install with Q35 + matched firmware + UTC. --import + --wait 0
    # means "define and start the VM, then return immediately" (don't block
    # waiting for the guest to shut down — it has an OS, not an installer).
    boot_arg = "--boot uefi" if firmware == "uefi" else ""

    # Hyper-V enlightenments for Windows guests — Windows detects these at
    # boot and uses faster code paths for APICs, spinlocks, synthetic timer,
    # etc. Red Hat's recommended safe set; measurable CPU-load drop on idle
    # Windows VMs, a few % win on busy ones. No-op for non-Windows guests,
    # so we only set it when we're confident the guest is Windows.
    is_windows = meta.get("os_type", "").lower() == "windows"
    if is_windows:
        features_arg = (
            "--features acpi=on,apic=on,"
            "hyperv.relaxed.state=on,hyperv.vapic.state=on,"
            "hyperv.spinlocks.state=on,hyperv.spinlocks.retries=8191,"
            "hyperv.vpindex.state=on,hyperv.runtime.state=on,"
            "hyperv.synic.state=on,hyperv.stimer.state=on,"
            "hyperv.reset.state=on,hyperv.frequencies.state=on "
        )
        clock_arg = "--clock offset=utc,hypervclock_present=yes "
    else:
        features_arg = ""
        clock_arg = "--clock offset=utc "

    # One --disk arg per data disk, in index order → vda, vdb, vdc, ...
    disk_args = " ".join(
        f"--disk path={d['lv_path']},format=raw,bus=virtio,cache=none,discard=unmap"
        for d in disks_plan)

    vi_cmd = (
        f"virt-install --name {req.name} --vcpus {req.vcpus} --ram {req.ram_mb} "
        f"{disk_args} "
        f"--network bridge=br0,model=virtio "
        f"--graphics vnc,listen=0.0.0.0 "
        f"--channel unix,target_type=virtio,name=org.qemu.guest_agent.0 "
        f"--machine q35 "
        f"{boot_arg} "
        f"{features_arg}"
        f"{clock_arg}"
        f"--os-variant detect=on,name=generic "
        f"--noautoconsole --wait 0 --import 2>&1"
    )
    if task: task.step_start("virt-install")
    push_log(f"Import {meta['id']} → virt-install ({len(disks_plan)} disk(s))",
             node=home_name, app="bedrock-mgmt", level="info")
    out, rc = ssh_cmd_rc(host, vi_cmd, timeout=120)
    if rc != 0:
        ssh_cmd_rc(host, f"virsh undefine {req.name} --nvram 2>&1", timeout=10)
        for lv in created_lvs:
            ssh_cmd_rc(host, f"lvremove -f {lv}", timeout=30)
        if task: task.step_fail("virt-install", (out or "")[-300:])
        raise HTTPException(500, "virt-install failed:\n" + (out or "(no output)"))
    if task: task.step_done("virt-install")

    # Priority
    shares = PRIORITY_CPU_SHARES[req.priority]
    ssh_cmd_rc(host, f"virsh schedinfo {req.name} --live --config cpu_shares={shares}",
               timeout=10)

    # Inventory
    inv = load_inventory()
    inv[req.name] = {
        "priority": req.priority, "vcpus": req.vcpus, "ram_mb": req.ram_mb,
        "disk_gb": disks_plan[0]["size_gb"],   # back-compat primary disk
        "disks": [
            {"index": d["index"], "lv": d["lv_name"], "size_gb": d["size_gb"]}
            for d in disks_plan
        ],
        "iso": None,
        "home_node": home_name,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "created_by": "import",
        "imported_from": meta.get("original_name", meta["id"]),
    }
    save_inventory(inv)

    # Mark import as consumed
    d = _import_dir(meta["id"])
    meta["status"] = "consumed"
    meta["consumed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    meta["consumed_as"] = req.name
    _write_import_meta(d, meta)

    disk_summary = ", ".join(f"disk{d['index']}={d['size_gb']}G" for d in disks_plan)
    push_log(f"Imported VM {req.name} on {home_name} (vcpus={req.vcpus}, "
             f"ram={req.ram_mb}MB, {disk_summary}, "
             f"from {meta.get('original_name')})",
             node=home_name, app="bedrock-mgmt", level="info")
    return {"status": "created", "name": req.name, "node": home_name,
            "disks": [d["lv_name"] for d in disks_plan]}


def _vm_delete(vm_name: str, task: Optional[Task] = None) -> dict:
    """Stop (if running), tear down DRBD (if any), undefine VM + remove LVs
    on every node where it was defined, drop inventory entry. Iterates every
    disk the VM had, so multi-disk VMs clean up fully."""
    state = build_cluster_state()
    vm = state["vms"].get(vm_name)
    if not vm:
        raise HTTPException(404, f"Unknown VM: {vm_name}")
    nodes_cfg = get_nodes()
    defined_on = vm.get("defined_on") or ([vm["running_on"]] if vm.get("running_on") else [])
    if not defined_on:
        raise HTTPException(500, "VM has no defined_on nodes")

    # Per-disk teardown plan. For each disk figure out its DRBD resource (if any)
    # and the LV paths (data + meta) on every peer. We parse the .res on one node
    # for peer LV paths — DRBD resource config has on <node> { disk / meta-disk }.
    disks = vm.get("disks") or []
    if not disks:
        # Fallback — VM has no disks in state (pure cattle, or state stale).
        # Guess the legacy single-disk path.
        disks = [{"drbd_resource": vm.get("drbd_resource", ""),
                  "backing_lv": f"/dev/almalinux/vm-{vm_name}-disk0"}]

    # For each disk, collect a mini-plan: {resource, lv_by_node, meta_by_node}.
    host0 = nodes_cfg[defined_on[0]]["host"]
    disk_plans = []
    for d in disks:
        r = d.get("drbd_resource") or ""
        plan = {"resource": r, "lv_by_node": {}, "meta_by_node": {}}
        if r:
            existing = _parse_drbd_res(host0, r) or {}
            # _parse_drbd_res today returns single peer view; extend to parse all
            # 'on <node>' blocks.
            try:
                cfg_text = ssh_cmd_rc(host0, f"cat /etc/drbd.d/{r}.res 2>/dev/null", timeout=10)[0]
                import re as _re
                for m in _re.finditer(
                    r"on\s+(\S+)\s*\{([^}]*)\}", cfg_text or "", _re.DOTALL):
                    node_fqdn, body = m.group(1), m.group(2)
                    dm = _re.search(r"disk\s+(/dev/[^\s;]+)\s*;", body)
                    mm = _re.search(r"meta-disk\s+(/dev/[^\s;]+)\s*;", body)
                    if dm: plan["lv_by_node"][node_fqdn] = dm.group(1)
                    if mm: plan["meta_by_node"][node_fqdn] = mm.group(1)
            except Exception: pass
        if not plan["lv_by_node"]:
            # No DRBD, or parse failed — use backing_lv we saw on the live node.
            default_lv = d.get("backing_lv") or \
                f"/dev/almalinux/vm-{vm_name}-disk0"
            for n in defined_on:
                plan["lv_by_node"][n] = default_lv
        disk_plans.append(plan)

    # 1. Stop the VM (force-kill; this is delete, not shutdown)
    if task: task.step_start("destroy VM")
    if vm["state"] == "running" and vm.get("running_on"):
        host = nodes_cfg[vm["running_on"]]["host"]
        ssh_cmd_rc(host, f"virsh destroy {vm_name} 2>&1", timeout=15)
    if task: task.step_done("destroy VM")

    # 2. For each node that has the VM, undefine it + tear down DRBD + remove LVs
    for nname in defined_on:
        if nname not in nodes_cfg: continue
        host = nodes_cfg[nname]["host"]
        if task: task.step_start(f"undefine on {nname}")
        ssh_cmd_rc(host, f"virsh undefine {vm_name} --nvram 2>&1 || virsh undefine {vm_name} 2>&1", timeout=15)
        if task: task.step_done(f"undefine on {nname}")

        # Per-disk teardown on this node
        for i, plan in enumerate(disk_plans):
            r = plan["resource"]
            sn = f"disk{i}"
            if task: task.step_start(f"{sn} teardown on {nname}")
            if r:
                ssh_cmd_rc(host, f"drbdadm down {r} 2>&1 || true", timeout=15)
                ssh_cmd_rc(host, f"drbdadm wipe-md --force {r} 2>&1 || true", timeout=15)
                ssh_cmd_rc(host, f"rm -f /etc/drbd.d/{r}.res", timeout=10)
            lv = plan["lv_by_node"].get(nname) or next(iter(plan["lv_by_node"].values()), "")
            mv = plan["meta_by_node"].get(nname, "")
            rm_paths = " ".join(p for p in (lv, mv) if p)
            if rm_paths:
                ssh_cmd_rc(host, f"lvremove -f {rm_paths} 2>&1 || true", timeout=30)
            if task: task.step_done(f"{sn} teardown on {nname}")

    # 3. Drop inventory entry
    inv = load_inventory()
    if vm_name in inv:
        inv.pop(vm_name)
        save_inventory(inv)

    # 4. If this VM was created from an import, reset the import to "ready"
    #    so the operator can recreate the same VM (or a different one) from
    #    the already-converted disk without re-uploading.
    if IMPORT_ROOT.exists():
        for d in IMPORT_ROOT.iterdir():
            if not d.is_dir(): continue
            m = _import_meta(d)
            if m.get("consumed_as") == vm_name:
                m["status"] = "ready"
                m.pop("consumed_as", None)
                m.pop("consumed_at", None)
                _write_import_meta(d, m)
                push_log(f"Import {d.name} reset to ready (was VM {vm_name})",
                         node="mgmt", app="bedrock-mgmt", level="info")
                break

    push_log(f"Deleted VM {vm_name} (was on {','.join(defined_on)})",
             node="mgmt", app="bedrock-mgmt", level="warn")
    return {"status": "deleted", "name": vm_name}


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


def _vm_set_resources(vm_name: str, req) -> dict:
    running, host, resource = _vm_host(vm_name)
    result = {}

    if req.vcpus is not None:
        if req.vcpus < 1 or req.vcpus > 32:
            raise HTTPException(400, "vcpus must be 1-32")
        # --config applies on next boot; also setvcpus-max to the new count so
        # both the current and max declarations stay coherent.
        ssh_cmd(host, f"virsh setvcpus {vm_name} {req.vcpus} --config --maximum", timeout=10)
        ssh_cmd(host, f"virsh setvcpus {vm_name} {req.vcpus} --config", timeout=10)
        result["vcpus"] = {"applied": True, "requires_reboot": True,
                          "note": f"queued for next boot ({req.vcpus} vCPUs)"}
        push_log(f"VM {vm_name}: vcpus → {req.vcpus} (reboot required)",
                 node=running, app="bedrock-mgmt", level="info")

    if req.ram_mb is not None:
        if req.ram_mb < 128 or req.ram_mb > 131072:
            raise HTTPException(400, "ram_mb must be 128-131072")
        kib = req.ram_mb * 1024
        ssh_cmd(host, f"virsh setmaxmem {vm_name} {kib} --config", timeout=10)
        ssh_cmd(host, f"virsh setmem   {vm_name} {kib} --config", timeout=10)
        result["ram_mb"] = {"applied": True, "requires_reboot": True,
                           "note": f"queued for next boot ({req.ram_mb} MB)"}
        push_log(f"VM {vm_name}: ram → {req.ram_mb} MB (reboot required)",
                 node=running, app="bedrock-mgmt", level="info")

    if req.disk_gb is not None:
        # Grow the data LV (and DRBD if this VM is pet/ViPet), then tell QEMU.
        cur = _vm_get_settings(vm_name)
        cur_gb = cur["disk_gb"]
        if req.disk_gb < cur_gb:
            raise HTTPException(400, f"disk shrink not supported ({cur_gb}G → {req.disk_gb}G)")
        if req.disk_gb == cur_gb:
            result["disk_gb"] = {"applied": False, "requires_reboot": False, "note": "unchanged"}
        else:
            delta = req.disk_gb - cur_gb
            nodes_cfg = get_nodes()
            # If DRBD: grow data + meta LVs on every peer first
            if resource:
                existing = _parse_drbd_res(host, resource)
                for n in existing["peers"]:
                    ssh_cmd(nodes_cfg[n]["host"],
                        f"lvextend -L +{delta}G {existing['lv_path']} 2>&1", timeout=30)
                # drbdadm resize on primary propagates to peers
                ssh_cmd(host, f"drbdadm resize {resource}", timeout=30)
            else:
                ssh_cmd(host, f"lvextend -L +{delta}G {cur['disk_path']} 2>&1", timeout=30)
            # Tell QEMU the new size (live)
            new_bytes = req.disk_gb * 1024 * 1024  # KiB units for blockresize
            ssh_cmd(host,
                f"virsh blockresize {vm_name} {cur['disk_target']} {new_bytes}K",
                timeout=15)
            # Inventory
            inv = load_inventory()
            if vm_name in inv:
                inv[vm_name]["disk_gb"] = req.disk_gb
                save_inventory(inv)
            result["disk_gb"] = {"applied": True, "requires_reboot": False,
                                 "note": f"live-grown {cur_gb}G → {req.disk_gb}G "
                                         "(guest may need rescan)"}
            push_log(f"VM {vm_name}: disk grown {cur_gb}G → {req.disk_gb}G (live)",
                     node=running, app="bedrock-mgmt", level="info")

    return result


def _vm_set_priority(vm_name: str, priority: str) -> dict:
    if priority not in _VALID_PRIORITIES:
        raise HTTPException(400, f"priority must be one of {_VALID_PRIORITIES}")
    running, host, _ = _vm_host(vm_name)
    shares = PRIORITY_CPU_SHARES[priority]
    ssh_cmd(host, f"virsh schedinfo {vm_name} --live --config cpu_shares={shares}",
            timeout=10)
    inv = load_inventory()
    inv.setdefault(vm_name, {})["priority"] = priority
    save_inventory(inv)
    push_log(f"VM {vm_name}: priority → {priority} (cpu_shares={shares}, live)",
             node=running, app="bedrock-mgmt", level="info")
    return {"applied": True, "requires_reboot": False,
            "priority": priority, "cpu_shares": shares}


def _vm_set_cdrom(vm_name: str, action: str, iso: Optional[str]) -> dict:
    if action not in ("eject", "insert"):
        raise HTTPException(400, "action must be 'eject' or 'insert'")
    running, host, _ = _vm_host(vm_name)
    settings = _vm_get_settings(vm_name)
    slot = settings.get("cdrom_slot")
    if not slot:
        raise HTTPException(400, "This VM has no CDROM device (was it created "
                            "without an ISO?). Recreate with an ISO to get a "
                            "CDROM slot.")
    if action == "eject":
        ssh_cmd(host, f"virsh change-media {vm_name} {slot} --eject --live --force",
                timeout=10)
        push_log(f"VM {vm_name}: ejected CDROM",
                 node=running, app="bedrock-mgmt", level="info")
        return {"applied": True, "requires_reboot": False, "note": "ejected"}
    # insert
    if not iso:
        raise HTTPException(400, "iso filename required for insert")
    iso_name = Path(iso).name
    if not (ISO_DIR / iso_name).exists():
        raise HTTPException(400, f"ISO not found: {iso_name}")
    target = f"{ISO_MOUNT_DIR}/{iso_name}"
    ssh_cmd(host,
        f"virsh change-media {vm_name} {slot} {target} --insert --live --force",
        timeout=10)
    push_log(f"VM {vm_name}: inserted {iso_name}",
             node=running, app="bedrock-mgmt", level="info")
    return {"applied": True, "requires_reboot": False, "note": f"inserted {iso_name}"}


# ── Metrics API (queries VictoriaMetrics) ───────────────────────────────────

from victoria import query_range, query_instant, query_logs
from victoria import push_log as _vl_push_log


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

@app.get("/api/metrics/nodes")
def api_metrics_nodes(hours: int = 1, step: str = "30s"):
    """CPU and memory for all nodes over time."""
    end = int(time.time())
    start = end - hours * 3600
    return {
        "cpu": query_range(
            '100 - avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[1m])) * 100',
            start, end, step),
        "mem": query_range(
            '(1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100',
            start, end, step),
        "net_rx": query_range(
            'rate(node_network_receive_bytes_total{device="br0"}[1m])',
            start, end, step),
        "net_tx": query_range(
            'rate(node_network_transmit_bytes_total{device="br0"}[1m])',
            start, end, step),
    }

@app.get("/api/metrics/vms")
def api_metrics_vms(hours: int = 1, step: str = "30s"):
    """Per-VM CPU and disk IOPS over time."""
    end = int(time.time())
    start = end - hours * 3600
    return {
        "cpu": query_range(
            'rate(bedrock_vm_cpu_time_ns[1m]) / 1e9 * 100',
            start, end, step),
        "disk_rd_iops": query_range(
            'rate(bedrock_vm_disk_read_reqs{disk="0"}[1m])',
            start, end, step),
        "disk_wr_iops": query_range(
            'rate(bedrock_vm_disk_write_reqs{disk="0"}[1m])',
            start, end, step),
        "disk_wr_lat": query_range(
            'rate(bedrock_vm_disk_write_time_ns{disk="0"}[1m]) / rate(bedrock_vm_disk_write_reqs{disk="0"}[1m]) / 1e6',
            start, end, step),
    }

@app.get("/api/metrics/drbd")
def api_metrics_drbd(hours: int = 1, step: str = "30s"):
    """DRBD replication metrics."""
    end = int(time.time())
    start = end - hours * 3600
    return {
        "sent": query_range('rate(bedrock_drbd_sent_kb[1m])', start, end, step),
        "received": query_range('rate(bedrock_drbd_received_kb[1m])', start, end, step),
        "out_of_sync": query_range('bedrock_drbd_out_of_sync_kb', start, end, step),
    }

# ── Logs API (queries VictoriaLogs) ─────────────────────────────────────────

@app.get("/api/logs")
def api_logs(query: str = "*", limit: int = 50, hours: int = 1):
    end = int(time.time())
    start = end - hours * 3600
    return query_logs(query, limit=limit, start=start, end=end)

@app.get("/api/logs/node/{node_name}")
def api_logs_node(node_name: str, limit: int = 50, hours: int = 1):
    end = int(time.time())
    start = end - hours * 3600
    return query_logs(f'hostname:"{node_name}"', limit=limit, start=start, end=end)

@app.get("/api/logs/vm/{vm_name}")
def api_logs_vm(vm_name: str, limit: int = 50, hours: int = 1):
    end = int(time.time())
    start = end - hours * 3600
    return query_logs(f'"{vm_name}"', limit=limit, start=start, end=end)

# ── Console redirect ────────────────────────────────────────────────────────

@app.get("/console/{vm_name}")
def console_page(vm_name: str):
    state = build_cluster_state()
    vm = state["vms"].get(vm_name)
    if not vm: raise HTTPException(404)
    if not vm.get("vnc_ws_url"):
        return HTMLResponse("<h2>VM not running or no VNC</h2>")
    # Direct noVNC at the mgmt-hosted proxy. An empty host+port tells noVNC
    # to use window.location, and path routes to /vnc/<vm>.
    return RedirectResponse(
        f"/novnc/vnc.html?path=vnc/{vm_name}"
        f"&autoconnect=true&resize=scale&reconnect=true"
    )

# ── WebSocket → raw-TCP VNC proxy ──────────────────────────────────────────
# Lets the browser speak WebSocket to the mgmt node; this mgmt node in turn
# holds a TCP connection to the VM's host:VNC-port. No websockify needed on
# cluster nodes. noVNC connects to ws://<mgmt>:8080/vnc/<vm_name>.

@app.websocket("/vnc/{vm_name}")
async def vnc_proxy(ws: WebSocket, vm_name: str):
    # Only echo back "binary" if the client actually offered it. Modern noVNC
    # often sends no subprotocol; Starlette will reject the handshake if we
    # reply with one the client didn't list.
    offered = (ws.headers.get("sec-websocket-protocol") or "").split(",")
    offered = [o.strip() for o in offered if o.strip()]
    if "binary" in offered:
        await ws.accept(subprotocol="binary")
    else:
        await ws.accept()
    nodes_cfg = get_nodes()
    state = build_cluster_state()
    vm = state["vms"].get(vm_name)
    if not vm or vm.get("state") != "running" or not vm.get("running_on"):
        await ws.close(code=1011, reason="VM not running")
        return
    host = nodes_cfg[vm["running_on"]]["host"]
    port = get_vm_vnc_port(host, vm_name)
    if port <= 0:
        await ws.close(code=1011, reason="no VNC port")
        return
    try:
        reader, writer = await asyncio.open_connection(host, port)
    except Exception as e:
        await ws.close(code=1011, reason=f"connect: {e}")
        return

    total_ws_to_tcp = 0
    total_tcp_to_ws = 0
    async def ws_to_tcp():
        nonlocal total_ws_to_tcp
        try:
            while True:
                data = await ws.receive_bytes()
                total_ws_to_tcp += len(data)
                writer.write(data)
                await writer.drain()
        except Exception as e:
            log.info("vnc_proxy ws->tcp ended: %s (sent=%d)", e, total_ws_to_tcp)
        finally:
            try: writer.close()
            except Exception: pass

    async def tcp_to_ws():
        nonlocal total_tcp_to_ws
        try:
            while True:
                data = await reader.read(16384)
                if not data: break
                total_tcp_to_ws += len(data)
                await ws.send_bytes(data)
        except Exception as e:
            log.info("vnc_proxy tcp->ws ended: %s (sent=%d)", e, total_tcp_to_ws)
        finally:
            try: await ws.close()
            except Exception: pass

    await asyncio.gather(ws_to_tcp(), tcp_to_ws())


# ── Static files (Svelte build + noVNC) ────────────────────────────────────
from fastapi.responses import FileResponse

novnc_dir = Path(__file__).parent / "novnc"
if novnc_dir.exists():
    app.mount("/novnc", StaticFiles(directory=str(novnc_dir)), name="novnc")

ui_build = Path(__file__).parent / "ui" / "build"

# Serve static assets from Svelte build
if ui_build.exists():
    # Mount _app directory for JS/CSS bundles
    app_dir = ui_build / "_app"
    if app_dir.exists():
        app.mount("/_app", StaticFiles(directory=str(app_dir)), name="svelte_app")

    # SPA fallback: any unmatched route serves index.html
    @app.get("/{path:path}")
    async def spa_fallback(path: str):
        # Try serving the exact file first
        file_path = ui_build / path
        if file_path.is_file():
            return FileResponse(str(file_path))
        # Otherwise serve index.html (SPA routing)
        return FileResponse(str(ui_build / "index.html"))

# ── Main ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
