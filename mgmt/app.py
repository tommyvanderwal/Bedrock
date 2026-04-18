#!/usr/bin/env python3
"""Bedrock cluster management dashboard — FastAPI backend with WebSocket hub."""

import asyncio
import json
import logging
import re
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
    c = _ssh_connect(host)
    _, so, se = c.exec_command(cmd, timeout=timeout)
    out = so.read().decode().strip()
    err = se.read().decode().strip()
    rc = so.channel.recv_exit_status()
    c.close()
    return out if rc == 0 else err, rc

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
            "echo '---KERNEL---'; uname -r"
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

        return {
            "name": name, "host": host, "online": True,
            "kernel": sections.get("KERNEL", [""])[0],
            "uptime_since": sections.get("UPTIME", [""])[0],
            "load": load_parts[0] if load_parts else "0",
            "mem_total_mb": int(mem_parts[1]) if len(mem_parts) > 1 else 0,
            "mem_used_mb": int(mem_parts[2]) if len(mem_parts) > 2 else 0,
            "all_vms": all_vms, "running_vms": running_vms,
            "drbd_raw": "\n".join(sections.get("DRBD", [])),
            "cockpit_url": cfg["cockpit"],
        }
    except Exception as e:
        return {
            "name": name, "host": host, "online": False, "error": str(e),
            "all_vms": [], "running_vms": [], "drbd_raw": "",
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
    """Parse virsh dumpxml to find the DRBD resource backing this VM's disk."""
    try:
        xml = ssh_cmd(host, f"virsh dumpxml {vm_name} 2>/dev/null")
        import re as _re
        # Look for source dev='/dev/drbdN' — standard block DRBD device
        m = _re.search(r"source dev='/dev/drbd([^']+)'", xml)
        if m:
            drbd_dev = m.group(1)
            # Find the matching resource name from drbdsetup
            res_out = ssh_cmd(host, f"drbdsetup status --json 2>/dev/null || echo '[]'")
            try:
                import json as _json
                resources = _json.loads(res_out)
                for res in resources:
                    for dev in res.get("devices", []):
                        if str(dev.get("minor", "")) == drbd_dev.lstrip("/"):
                            return res.get("name", "")
            except Exception:
                pass
            # Fallback: derive resource name from device path (drbd1 → guess from ls)
            return ""
    except Exception:
        pass
    return ""


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
        resource = (get_vm_drbd_resource(nodes_cfg[defined_on[0]]["host"], vm_name)
                    if defined_on else "")
        vnc_port = (get_vm_vnc_port(nodes_cfg[running_on]["host"], vm_name)
                    if running_on and running_on in nodes_cfg else -1)
        return vm_name, running_on, defined_on, resource, vnc_port

    with ThreadPoolExecutor(max_workers=max(4, len(all_vm_names) or 1)) as ex:
        probes = list(ex.map(_probe_vm, sorted(all_vm_names)))

    for vm_name, running_on, defined_on, resource, vnc_port in probes:
        backup_node = next((n for n in defined_on if n != running_on), None)
        drbd_state = drbd.get(resource, {}) if resource else {}
        vnc_ws_url = f"/vnc/{vm_name}" if vnc_port > 0 else ""

        vms_data[vm_name] = {
            "name": vm_name, "state": "running" if running_on else "shut off",
            "running_on": running_on, "backup_node": backup_node, "defined_on": defined_on,
            "drbd_resource": resource,
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
    asyncio.create_task(state_push_loop())
    write_scrape_config(cfg)

# ── REST API (same as before, for curl/scripting) ──────────────────────────

@app.get("/api/cluster")
def api_cluster():
    # Serve cached state. Fresh data lands every 3s via the push loop.
    return _last_state


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


@app.post("/api/nodes/register")
def register_node(req: NodeRegister):
    """Called by `bedrock join` to register a new node with the cluster."""
    cluster = load_cluster()
    cluster.setdefault("nodes", {})
    cluster["nodes"][req.name] = {
        "host": req.host,
        "drbd_ip": req.drbd_ip or "",
        "tb_ip": req.drbd_ip or "",  # use DRBD for migration URI (no USB4 in testbed)
        "eno_ip": req.drbd_ip or "",
        "role": req.role,
        "cockpit": f"https://{req.host}:9090",
    }
    save_cluster(cluster)
    push_log(f"Node {req.name} ({req.host}) registered with cluster",
             node="mgmt", app="bedrock-mgmt", level="info")
    # Return all peer IPs so the joining node can pre-populate known_hosts
    # for virsh migrate / DRBD SSH over both mgmt and replication networks.
    peer_ips = []
    for n in cluster["nodes"].values():
        if n.get("host"): peer_ips.append(n["host"])
        if n.get("drbd_ip"): peer_ips.append(n["drbd_ip"])
    return {"status": "registered", "cluster": cluster.get("cluster_name"),
            "nodes": list(cluster["nodes"].keys()),
            "peer_ips": sorted(set(peer_ips))}


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


class VMCreateRequest(BaseModel):
    name: str
    vcpus: int = 2
    ram_mb: int = 2048
    disk_gb: int = 20
    priority: str = "normal"  # low | normal | high
    iso: Optional[str] = None  # filename in /opt/bedrock/iso, optional

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
def api_vm_convert(vm_name: str, req: ConvertRequest):
    return _vm_convert(vm_name, req.target_type, req.peer_nodes)


@app.post("/api/vms/create")
def api_vm_create(req: VMCreateRequest):
    return _vm_create(req)


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
    resource = vm.get("drbd_resource", "")
    if not resource:
        raise HTTPException(400, f"VM {vm_name} has no DRBD resource (cattle VM — cannot migrate)")

    nodes_cfg = get_nodes()
    src_name = vm["running_on"]
    dst_name = target_node or vm["backup_node"]
    if not dst_name or dst_name == src_name: raise HTTPException(400, "No valid target")
    src, dst = nodes_cfg[src_name], nodes_cfg[dst_name]

    # For migration URI, prefer USB4 IP, fall back to DRBD IP, fall back to LAN
    dst_migrate_ip = dst.get("tb_ip") or dst.get("drbd_ip") or dst.get("eno_ip") or dst.get("host")

    ssh_cmd(src["host"], f"drbdadm net-options --allow-two-primaries=yes {resource}")
    ssh_cmd(dst["host"], f"drbdadm net-options --allow-two-primaries=yes {resource}")
    ssh_cmd(dst["host"], f"drbdadm primary {resource}")

    t0 = time.time()
    out, rc = ssh_cmd_rc(src["host"],
        f'virsh migrate --live --verbose --unsafe --migrateuri tcp://{dst_migrate_ip} '
        f'{vm_name} qemu+ssh://root@{dst_migrate_ip}/system', timeout=120)
    duration = time.time() - t0

    ssh_cmd(src["host"], f"drbdadm secondary {resource}")
    ssh_cmd(src["host"], f"drbdadm net-options --allow-two-primaries=no {resource}")
    ssh_cmd(dst["host"], f"drbdadm net-options --allow-two-primaries=no {resource}")

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
            "truncate -s 20G /var/lib/bedrock-vg.img && "
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


def _next_drbd_minor(hosts: list) -> int:
    """Pick an unused minor number (1000+) across all hosts."""
    used = set()
    for h in hosts:
        out = ssh_cmd(h, "ls /dev/drbd* 2>/dev/null | grep -oE '[0-9]+$' || true")
        for n in out.split():
            try: used.add(int(n))
            except ValueError: pass
    for i in range(1000, 1900):
        if i not in used: return i
    raise HTTPException(500, "No free DRBD minor")


def _lv_bytes(host: str, lv_path: str) -> int:
    return int(ssh_cmd(host, f"blockdev --getsize64 {lv_path}"))


def _gen_drbd_res(vm_name: str, minor: int, peers: list) -> str:
    """peers: list of (node_name, drbd_ip, lv_path, meta_lv_path). 2 or 3 entries.
    External meta-disk keeps the DRBD device the same size as the data LV,
    so virsh blockcopy can pivot 1:1 without size mismatch.
    """
    port = 7000 + minor
    lines = [f"resource vm-{vm_name}-disk0 {{",
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


def _write_drbd_res(hosts: list, vm_name: str, content: str):
    """Write /etc/drbd.d/vm-<name>-disk0.res on all hosts via SSH."""
    import base64
    b64 = base64.b64encode(content.encode()).decode()
    path = f"/etc/drbd.d/vm-{vm_name}-disk0.res"
    for h in hosts:
        ssh_cmd(h, f"echo {b64} | base64 -d > {path}")


def _vm_convert(vm_name: str, target_type: str, peer_nodes=None) -> dict:
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
        return _vm_convert_upgrade(vm_name, current_type, target_type, src_name, peer_nodes)
    else:
        return _vm_convert_downgrade(vm_name, current_type, target_type, src_name, peer_nodes)


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
                         peer_nodes) -> dict:
    nodes_cfg = get_nodes()
    src = nodes_cfg[src_name]

    need_peers = {"pet": 1, "vipet": 2}[tgt]
    available = [n for n in nodes_cfg if n != src_name]

    if cur == "cattle":
        # Need `need_peers` new peers
        chosen = (peer_nodes or available)[:need_peers]
        if len(chosen) < need_peers:
            raise HTTPException(400, f"{tgt} needs {need_peers} peers, have {len(chosen)}")

        disk = _find_vm_disk(src["host"], vm_name)
        src_lv = disk["source_dev"]
        target_dev = disk["target"]
        lv_name = src_lv.split("/")[-1]
        vg_name = src_lv.split("/")[-2]
        meta_lv_name = f"{lv_name}-meta"
        meta_path = f"/dev/{vg_name}/{meta_lv_name}"

        src_size = _lv_bytes(src["host"], src_lv)
        size_mb = (src_size + 1024*1024 - 1) // (1024*1024)
        # External DRBD metadata: ~32KB + 4KB × peers. 4MB LV covers up to many peers.
        meta_mb = 4

        # 1. Create external metadata LV on source
        push_log(f"Convert {vm_name}: create external DRBD meta LV {meta_path}",
                 node=src_name, app="bedrock-mgmt")
        ssh_cmd(src["host"],
                f"lvcreate -V {meta_mb}M -T {vg_name}/thinpool -n {meta_lv_name} -y "
                f"2>&1 || true", timeout=30)

        # 2. Create matching data + meta LV on each peer
        peers_info = [(src_name, src.get("drbd_ip") or src["host"], src_lv, meta_path)]
        for pname in chosen:
            p = nodes_cfg[pname]
            _ensure_thinpool(p["host"], vg_name)
            push_log(f"Convert {vm_name}: create peer LVs on {pname} ({size_mb}M data + {meta_mb}M meta)",
                     node=pname, app="bedrock-mgmt")
            ssh_cmd(p["host"],
                    f"lvcreate -V {size_mb}M -T {vg_name}/thinpool -n {lv_name} -y",
                    timeout=30)
            ssh_cmd(p["host"],
                    f"lvcreate -V {meta_mb}M -T {vg_name}/thinpool -n {meta_lv_name} -y",
                    timeout=30)
            peers_info.append((pname, p.get("drbd_ip") or p["host"],
                               f"/dev/{vg_name}/{lv_name}",
                               f"/dev/{vg_name}/{meta_lv_name}"))

        # 3. Generate + deploy DRBD resource config
        all_hosts = [nodes_cfg[n]["host"] for n, _, _, _ in peers_info]
        minor = _next_drbd_minor(all_hosts)
        res_text = _gen_drbd_res(vm_name, minor, peers_info)
        _write_drbd_res(all_hosts, vm_name, res_text)

        # 4. Initialise metadata on all nodes, bring resource up
        resource = f"vm-{vm_name}-disk0"
        for h in all_hosts:
            ssh_cmd(h, f"drbdadm create-md --force --max-peers=7 {resource}", timeout=30)
            ssh_cmd(h, f"drbdadm up {resource}", timeout=30)

        # 5. Force primary on source (current LV has the data)
        ssh_cmd(src["host"],
                f"drbdadm primary --force {resource}", timeout=30)

        # 6. Hot-swap VM: blockcopy source LV → /dev/drbdN, --reuse-external --pivot
        push_log(f"Convert {vm_name}: blockcopy {target_dev} → /dev/drbd{minor}",
                 node=src_name, app="bedrock-mgmt")
        t0 = time.time()
        out, rc = ssh_cmd_rc(src["host"],
            f"virsh blockcopy {vm_name} {target_dev} /dev/drbd{minor} "
            f"--reuse-external --wait --pivot --verbose --transient-job --blockdev --format raw", timeout=600)
        dur = round(time.time() - t0, 2)
        if rc != 0:
            raise HTTPException(500, f"blockcopy failed: {out}")

        # 7. Define VM on peer nodes so it's a true HA pet (migration works, failover works)
        xml_text = ssh_cmd(src["host"], f"virsh dumpxml {vm_name}", timeout=15)
        import base64 as _b64
        xml_b64 = _b64.b64encode(xml_text.encode()).decode()
        for pname, _, _, _ in peers_info:
            if pname == src_name: continue
            ph = nodes_cfg[pname]["host"]
            ssh_cmd(ph, f"echo {xml_b64} | base64 -d > /tmp/{vm_name}.xml && "
                        f"virsh define /tmp/{vm_name}.xml >/dev/null", timeout=15)

        push_log(f"Convert {vm_name}: {cur} → {tgt} in {dur}s (DRBD minor {minor})",
                 node=src_name, app="bedrock-mgmt", level="info")

        return {"status": "converted", "from": cur, "to": tgt,
                "resource": resource, "duration_s": dur,
                "peers": [p[0] for p in peers_info]}

    elif cur == "pet" and tgt == "vipet":
        # Add a third peer to an existing 2-way resource
        resource = f"vm-{vm_name}-disk0"
        existing = _parse_drbd_res(src["host"], resource)
        if not existing:
            raise HTTPException(500, f"Cannot parse existing {resource}")

        chosen = (peer_nodes or [n for n in available if n not in existing["peers"]])[:1]
        if not chosen:
            raise HTTPException(400, "vipet needs a third peer")
        new_peer = chosen[0]
        p = nodes_cfg[new_peer]
        vg_name = existing["lv_vg"]
        lv_name = existing["lv_name"]
        meta_lv_name = f"{lv_name}-meta"
        size_mb = (existing["size_bytes"] + 1024*1024 - 1) // (1024*1024)

        _ensure_thinpool(p["host"], vg_name)
        push_log(f"Convert {vm_name}: add 3rd peer {new_peer}",
                 node=new_peer, app="bedrock-mgmt")
        ssh_cmd(p["host"],
                f"lvcreate -V {size_mb}M -T {vg_name}/thinpool -n {lv_name} -y",
                timeout=30)
        ssh_cmd(p["host"],
                f"lvcreate -V 4M -T {vg_name}/thinpool -n {meta_lv_name} -y",
                timeout=30)

        peers_info = [(n, nodes_cfg[n].get("drbd_ip") or nodes_cfg[n]["host"],
                       existing["lv_path"], existing["meta_path"])
                      for n in existing["peers"]]
        peers_info.append((new_peer, p.get("drbd_ip") or p["host"],
                           f"/dev/{vg_name}/{lv_name}",
                           f"/dev/{vg_name}/{meta_lv_name}"))

        minor = existing["minor"]
        res_text = _gen_drbd_res(vm_name, minor, peers_info)
        all_hosts = [nodes_cfg[n]["host"] for n, _, _, _ in peers_info]
        _write_drbd_res(all_hosts, vm_name, res_text)

        # Initialise meta on the new peer only
        ssh_cmd(p["host"], f"drbdadm create-md --force --max-peers=7 {resource}", timeout=30)
        # Reload config on all, bring up on new peer
        for h in all_hosts:
            ssh_cmd(h, f"drbdadm adjust {resource} 2>&1 || true", timeout=30)
        ssh_cmd(p["host"], f"drbdadm up {resource}", timeout=30)

        # Define VM on new peer as well
        xml_text = ssh_cmd(src["host"], f"virsh dumpxml {vm_name}", timeout=15)
        import base64 as _b64
        xml_b64 = _b64.b64encode(xml_text.encode()).decode()
        ssh_cmd(p["host"], f"echo {xml_b64} | base64 -d > /tmp/{vm_name}.xml && "
                            f"virsh define /tmp/{vm_name}.xml >/dev/null", timeout=15)

        push_log(f"Convert {vm_name}: pet → vipet, added {new_peer}",
                 node=src_name, app="bedrock-mgmt", level="info")
        return {"status": "converted", "from": cur, "to": tgt,
                "resource": resource, "added_peer": new_peer}


def _vm_convert_downgrade(vm_name: str, cur: str, tgt: str, src_name: str,
                           peer_nodes) -> dict:
    nodes_cfg = get_nodes()
    src = nodes_cfg[src_name]
    resource = f"vm-{vm_name}-disk0"
    existing = _parse_drbd_res(src["host"], resource)
    if not existing:
        raise HTTPException(500, f"Cannot parse {resource}")

    if cur == "vipet" and tgt == "pet":
        # Drop one peer (not the primary). Prefer user-chosen.
        candidates = [n for n in existing["peers"] if n != src_name]
        drop_name = (peer_nodes[0] if peer_nodes else candidates[0])
        if drop_name == src_name:
            raise HTTPException(400, "Cannot drop primary")
        drop = nodes_cfg[drop_name]

        # 1. Undefine VM on dropped peer, disconnect + down DRBD
        ssh_cmd(drop["host"], f"virsh undefine {vm_name} 2>&1 || true", timeout=15)
        ssh_cmd(drop["host"], f"drbdadm down {resource} 2>&1 || true", timeout=30)
        ssh_cmd(drop["host"], f"drbdadm wipe-md --force {resource} 2>&1 || true", timeout=30)

        # 2. Rewrite config with remaining 2 peers, reload
        remaining = [(n, nodes_cfg[n].get("drbd_ip") or nodes_cfg[n]["host"],
                      existing["lv_path"], existing["meta_path"])
                     for n in existing["peers"] if n != drop_name]
        minor = existing["minor"]
        res_text = _gen_drbd_res(vm_name, minor, remaining)
        kept_hosts = [nodes_cfg[n]["host"] for n, _, _, _ in remaining]
        _write_drbd_res(kept_hosts, vm_name, res_text)
        # Remove res file on dropped
        ssh_cmd(drop["host"],
                f"rm -f /etc/drbd.d/vm-{vm_name}-disk0.res", timeout=10)
        # Tell remaining nodes to forget the dropped peer (node-id of the drop)
        drop_idx = existing["peers"].index(drop_name)
        for h in kept_hosts:
            ssh_cmd(h, f"drbdsetup disconnect {resource} {drop_idx} --force 2>&1 || true", timeout=15)
            ssh_cmd(h, f"drbdsetup del-peer {resource} {drop_idx} --force 2>&1 || true", timeout=15)
            ssh_cmd(h, f"drbdadm adjust {resource} 2>&1 || true", timeout=30)

        # 3. Free the LV + meta LV on dropped peer
        ssh_cmd(drop["host"],
                f"lvremove -f {existing['lv_path']} {existing['meta_path']} 2>&1 || true",
                timeout=30)

        push_log(f"Convert {vm_name}: vipet → pet (dropped {drop_name})",
                 node=src_name, app="bedrock-mgmt", level="info")
        return {"status": "converted", "from": cur, "to": tgt, "dropped": drop_name}

    elif cur in ("pet", "vipet") and tgt == "cattle":
        # Pivot VM back to raw LV, tear DRBD down, drop all peer LVs
        disk = _find_vm_disk(src["host"], vm_name)
        target_dev = disk["target"]
        lv_path = existing["lv_path"]
        minor = existing["minor"]

        # blockcopy from /dev/drbdN → LV  (both same physical bytes; self-copy).
        push_log(f"Convert {vm_name}: pivot {target_dev} back to {lv_path}",
                 node=src_name, app="bedrock-mgmt")
        t0 = time.time()
        out, rc = ssh_cmd_rc(src["host"],
            f"virsh blockcopy {vm_name} {target_dev} {lv_path} "
            f"--reuse-external --wait --pivot --verbose --transient-job --blockdev --format raw", timeout=600)
        dur = round(time.time() - t0, 2)
        if rc != 0:
            raise HTTPException(500, f"blockcopy pivot failed: {out}")

        # Undefine VM on all peers (keep primary), tear DRBD down
        for n in existing["peers"]:
            h = nodes_cfg[n]["host"]
            if n != src_name:
                ssh_cmd(h, f"virsh undefine {vm_name} 2>&1 || true", timeout=15)
            ssh_cmd(h, f"drbdadm down {resource} 2>&1 || true", timeout=30)
            ssh_cmd(h, f"drbdadm wipe-md --force {resource} 2>&1 || true", timeout=30)
            ssh_cmd(h, f"rm -f /etc/drbd.d/vm-{vm_name}-disk0.res", timeout=10)
        all_hosts = [nodes_cfg[n]["host"] for n in existing["peers"]]

        # Remove data+meta LVs on peers; remove only meta on primary (LV is now VM disk)
        for n in existing["peers"]:
            if n == src_name:
                ssh_cmd(nodes_cfg[n]["host"],
                        f"lvremove -f {existing['meta_path']} 2>&1 || true", timeout=30)
            else:
                ssh_cmd(nodes_cfg[n]["host"],
                        f"lvremove -f {lv_path} {existing['meta_path']} 2>&1 || true",
                        timeout=30)

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
        iso_path = f"/opt/bedrock/iso/{iso_name}"
        if not (ISO_DIR / iso_name).exists():
            raise HTTPException(400, f"ISO not found: {iso_name}")

    lv_name = f"vm-{req.name}-disk0"
    lv_path = f"/dev/almalinux/{lv_name}"

    # 1. Ensure thin pool, create data LV (thin)
    _ensure_thinpool(host)
    push_log(f"Create VM {req.name}: lvcreate {req.disk_gb}G thin on {home_name}",
             node=home_name, app="bedrock-mgmt")
    out, rc = ssh_cmd_rc(host,
        f"lvcreate -y -V {req.disk_gb}G --thin -n {lv_name} almalinux/thinpool", timeout=30)
    if rc != 0 and "already exists" not in out:
        raise HTTPException(500, f"lvcreate failed: {out}")

    # 2. virt-install — with or without CDROM
    cdrom_arg = f"--cdrom {iso_path}" if iso_path else "--import"
    boot_arg = "--boot cdrom,hd" if iso_path else "--boot hd"

    # For Windows ISOs virt-install's os-variant auto-detect works well;
    # for others, 'generic' is a safe default.
    vi_cmd = (
        f"virt-install "
        f"--name {req.name} "
        f"--vcpus {req.vcpus} "
        f"--ram {req.ram_mb} "
        f"--disk path={lv_path},format=raw,bus=virtio,cache=none,discard=unmap "
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
             f"ram={req.ram_mb}MB, iso={req.iso or 'none'})",
             node=home_name, app="bedrock-mgmt")
    out, rc = ssh_cmd_rc(host, vi_cmd, timeout=120)
    if rc != 0:
        # Clean up the LV so the name is free for retry
        ssh_cmd_rc(host, f"lvremove -f {lv_path} 2>&1", timeout=15)
        raise HTTPException(500, f"virt-install failed: {out[-400:]}")

    # 3. Save inventory
    inv = load_inventory()
    inv[req.name] = {
        "priority": req.priority,
        "vcpus": req.vcpus,
        "ram_mb": req.ram_mb,
        "disk_gb": req.disk_gb,
        "iso": req.iso,
        "home_node": home_name,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "created_by": "dashboard",
    }
    save_inventory(inv)

    push_log(f"Created VM {req.name} on {home_name} "
             f"(cattle, {req.vcpus}vCPU, {req.ram_mb}MB, {req.disk_gb}GB, priority={req.priority})",
             node=home_name, app="bedrock-mgmt", level="info")
    return {"status": "created", "name": req.name, "node": home_name}


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
