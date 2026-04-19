#!/usr/bin/env python3
"""Bedrock cluster management dashboard — FastAPI backend with WebSocket hub."""

import asyncio
import json
import logging
import re
import shutil
import subprocess
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
    return {"status": "registered", "cluster": cluster.get("cluster_name"),
            "nodes": list(cluster["nodes"].keys()),
            "peer_ips": sorted(set(peer_ips)),
            "peer_pubkeys": peer_pubkeys}


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
    return meta


QEMU_FORMAT_MAP = {
    "qcow2": "qcow2", "raw": "raw", "img": "raw",
    "vmdk": "vmdk",  "vhd": "vpc",  "vhdx": "vhdx",
}


def _run_cmd(log_path: Path, cmd: list) -> int:
    """Synchronous subprocess run with log file. Returns exit code."""
    with log_path.open("a") as lf:
        lf.write(f"\n# command: {' '.join(cmd)}\n"); lf.flush()
        return subprocess.run(cmd, stdout=lf, stderr=subprocess.STDOUT).returncode


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
        if ext in ("ova", "ovf"):
            # OVA = tar with OVF + disk(s). Extract, find the disk, convert.
            extract = d / "ova-extract"
            extract.mkdir(exist_ok=True)
            rc = await loop.run_in_executor(None, _run_cmd, log,
                ["tar", "-xf", str(src), "-C", str(extract)])
            if rc == 0:
                # Find the first *.vmdk (or *.raw/*.img) referenced by the OVF
                disks = [p for p in extract.glob("*.vmdk")] + \
                        [p for p in extract.glob("*.img")] + \
                        [p for p in extract.glob("*.raw")]
                if not disks:
                    meta["error"] = "OVA contained no recognisable disk"; rc = 1
                else:
                    d0 = disks[0]
                    fmt_in = QEMU_FORMAT_MAP.get(d0.suffix.lstrip(".").lower(), "raw")
                    if inject_drivers:
                        # Use virt-v2v on the bundled disk
                        rc = await loop.run_in_executor(None, _run_cmd, log,
                            ["virt-v2v", "-v", "-x", "-i", "disk", str(d0),
                             "-o", "local", "-os", str(dst_dir), "-of", "qcow2"])
                    else:
                        rc = await loop.run_in_executor(None, _run_cmd, log,
                            ["qemu-img", "convert", "-p", "-f", fmt_in,
                             "-O", "qcow2", str(d0), str(out_qcow)])
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
            # virt-v2v produces <name>-sda, our qemu-img path produces disk.qcow2
            qcow = out_qcow if out_qcow.exists() else \
                   next((p for p in dst_dir.glob("*.qcow2")), None) or \
                   next((p for p in dst_dir.glob("*-sd?")), None)
            if not qcow:
                meta["status"] = "failed"; meta["error"] = "no output file"
            else:
                iq = json.loads(subprocess.run(
                    ["qemu-img", "info", "--output=json", str(qcow)],
                    capture_output=True, text=True).stdout or "{}")
                meta["status"] = "ready"
                meta["disk_path"] = str(qcow)
                meta["virtual_size_bytes"] = iq.get("virtual-size") or 0
                meta["virtual_size_gb"] = max(1,
                    (meta["virtual_size_bytes"] + (1<<30) - 1) >> 30)
                # OS detection from virt-v2v sidecar XML (if present)
                xml = next((p for p in dst_dir.glob("*.xml")), None)
                if xml:
                    xt = xml.read_text()
                    m = re.search(r"<name>([^<]+)</name>", xt)
                    if m: meta["detected_name"] = m.group(1)
                    m = re.search(r"<os>.*?<type[^>]*>([^<]+)</type>", xt, re.S)
                    if m: meta["detected_os_type"] = m.group(1)
                    # Firmware choice — virt-v2v adds <firmware>efi</firmware>
                    # (or firmware='efi' on <os>) for GPT/UEFI guests.
                    meta["detected_firmware"] = (
                        "uefi" if ("firmware='efi'" in xt or
                                   "<firmware>efi</firmware>" in xt)
                        else "bios"
                    )
                # Fallback partition-table sniff if no sidecar
                if "detected_firmware" not in meta:
                    try:
                        head = subprocess.run(
                            ["qemu-img", "dd", "-O", "raw", "bs=512", "count=34",
                             f"if={qcow}", "of=/dev/stdout"],
                            capture_output=True, timeout=20).stdout
                        meta["detected_firmware"] = (
                            "uefi" if len(head) >= 520 and head[512:520] == b"EFI PART"
                            else "bios"
                        )
                    except Exception: meta["detected_firmware"] = "bios"
                meta["convert_finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                push_log(f"Import convert done: {job_id} → {qcow.name} "
                         f"({meta['virtual_size_gb']}G virtual)",
                         node="mgmt", app="bedrock-mgmt", level="info")
    except Exception as e:
        meta["status"] = "failed"; meta["error"] = str(e)
        push_log(f"Import convert EXCEPTION: {job_id}: {e}",
                 node="mgmt", app="bedrock-mgmt", level="error")
    _write_import_meta(d, meta)


class ImportConvertRequest(BaseModel):
    inject_drivers: bool = False  # true → virt-v2v for Windows driver injection


@app.post("/api/imports/{job_id}/convert")
async def api_import_convert(job_id: str, req: ImportConvertRequest = ImportConvertRequest()):
    d = _import_dir(job_id)
    if not d.exists(): raise HTTPException(404)
    meta = _import_meta(d)
    if meta.get("status") not in ("uploaded", "failed"):
        raise HTTPException(400, f"cannot convert from status '{meta.get('status')}'")
    # Fire-and-forget; caller polls GET /api/imports/{id} for progress
    asyncio.create_task(_run_convert(job_id, inject_drivers=req.inject_drivers))
    meta["status"] = "converting"
    _write_import_meta(d, meta)
    return {"status": "converting", "id": job_id,
            "inject_drivers": req.inject_drivers}


class ImportCreateVMRequest(BaseModel):
    name: str
    vcpus: int = 2
    ram_mb: int = 2048
    priority: str = "normal"


@app.post("/api/imports/{job_id}/create-vm")
def api_import_create_vm(job_id: str, req: ImportCreateVMRequest):
    d = _import_dir(job_id)
    meta = _import_meta(d)
    if meta.get("status") != "ready":
        raise HTTPException(400, f"import status {meta.get('status')!r}, need 'ready'")
    return _vm_create_from_import(meta, req)


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


@app.delete("/api/vms/{vm_name}")
def api_vm_delete(vm_name: str):
    return _vm_delete(vm_name)


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

    # 2. virt-install — with or without CDROM. Always attach virtio-win.iso
    #    as a 2nd CDROM when any ISO is used: Windows Setup needs it for
    #    viostor+NetKVM; Linux installs ignore it.
    virtio_extra = ""
    if iso_path and (ISO_DIR / "virtio-win.iso").exists():
        virtio_extra = (f" --disk path={ISO_MOUNT_DIR}/virtio-win.iso,"
                        "device=cdrom,bus=sata,readonly=on")
    cdrom_arg = f"--cdrom {iso_path}{virtio_extra}" if iso_path else "--import"
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
        "disk_gb": req.disk_gb,
        "iso": req.iso,
        "home_node": home_name,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "created_by": "dashboard",
    }
    save_inventory(inv)

    push_log(f"Created VM {req.name} on {home_name} "
             f"(cattle, {req.vcpus}vCPU, {req.ram_mb}MB, {req.disk_gb}GB, "
             f"priority={req.priority}, cpu_shares={shares})",
             node=home_name, app="bedrock-mgmt", level="info")
    return {"status": "created", "name": req.name, "node": home_name}


def _vm_create_from_import(meta: dict, req) -> dict:
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

    src_qcow = meta.get("disk_path")
    if not src_qcow or not Path(src_qcow).exists():
        raise HTTPException(500, "converted disk is gone — re-run convert?")

    virtual_gb = meta.get("virtual_size_gb") or 20
    size_mb = max(virtual_gb * 1024, 1024)
    lv_name = f"vm-{req.name}-disk0"
    lv_path = f"/dev/almalinux/{lv_name}"

    # Firmware: trust the inspection result from _run_convert if available.
    # Otherwise sniff the disk's partition table here. Rationale: a BIOS-boot
    # disk can't boot on UEFI firmware — Windows traps 0x7B, Linux drops to
    # EFI shell. Match the source to avoid the footgun.
    firmware = meta.get("detected_firmware")
    if firmware not in ("bios", "uefi"):
        firmware = "bios"
        try:
            head = subprocess.run(
                ["qemu-img", "dd", "-O", "raw", "bs=512", "count=34",
                 f"if={src_qcow}", "of=/dev/stdout"],
                capture_output=True, timeout=20).stdout
            if len(head) >= 520 and head[512:520] == b"EFI PART":
                firmware = "uefi"
        except Exception: pass

    _ensure_thinpool(host)

    # Pre-flight: the converted disk is virtual_gb — but qemu-img convert to
    # raw writes the full size (zero-skip notwithstanding for sparse LVs on
    # thin pools, any non-zero block allocates). Reject up front if the thin
    # pool can't fit the worst case, so we never partially fill and brick the
    # pool. We fail the request cleanly instead of leaving a zombie LV behind.
    pool_info, _ = ssh_cmd_rc(host,
        "lvs --noheadings --units b --nosuffix --separator '|' "
        "-o lv_size,data_percent almalinux/thinpool 2>/dev/null | head -1",
        timeout=10)
    try:
        parts = [p.strip() for p in pool_info.split("|") if p.strip()]
        pool_size_b = int(parts[0]); pool_used_pct = float(parts[1])
        pool_free_b = int(pool_size_b * (100.0 - pool_used_pct) / 100.0)
        need_b = int(size_mb) * 1024 * 1024
        if pool_free_b < need_b + (1 << 30):  # +1 GB slack
            raise HTTPException(507,
                f"Thin pool on {home_name} has "
                f"{pool_free_b // (1<<30)} GB free; this import needs "
                f"{need_b // (1<<30)} GB + 1 GB slack. Free space or grow the "
                f"pool before retrying.")
    except HTTPException:
        raise
    except Exception:
        pass  # non-fatal — proceed and let lvcreate fail loudly if needed

    push_log(f"Import {meta['id']} → create VM {req.name}: lvcreate {virtual_gb}G thin",
             node=home_name, app="bedrock-mgmt", level="info")
    out, rc = ssh_cmd_rc(host,
        f"lvcreate -y -V {size_mb}M --thin -n {lv_name} almalinux/thinpool 2>&1",
        timeout=60)
    if rc != 0 and "already exists" not in out:
        raise HTTPException(500, f"lvcreate failed: {out}")

    # Stream the converted image into the LV. Auto-detect the input format
    # (virt-v2v sometimes outputs raw, sometimes qcow2 depending on flags).
    push_log(f"Import {meta['id']} → qemu-img convert → raw LV",
             node=home_name, app="bedrock-mgmt", level="info")
    out, rc = ssh_cmd_rc(host,
        f"qemu-img convert -p -O raw {src_qcow} {lv_path} 2>&1",
        timeout=3600)
    if rc != 0:
        ssh_cmd_rc(host, f"lvremove -f {lv_path}", timeout=30)
        raise HTTPException(500, "qemu-img convert failed:\n" + (out or "(no output)"))

    # virt-install with Q35 + matched firmware + UTC. --import + --wait 0
    # means "define and start the VM, then return immediately" (don't block
    # waiting for the guest to shut down — it has an OS, not an installer).
    boot_arg = "--boot uefi" if firmware == "uefi" else ""
    vi_cmd = (
        f"virt-install --name {req.name} --vcpus {req.vcpus} --ram {req.ram_mb} "
        f"--disk path={lv_path},format=raw,bus=virtio,cache=none,discard=unmap "
        f"--network bridge=br0,model=virtio "
        f"--graphics vnc,listen=0.0.0.0 "
        f"--channel unix,target_type=virtio,name=org.qemu.guest_agent.0 "
        f"--machine q35 "
        f"{boot_arg} "
        f"--clock offset=utc "
        f"--os-variant detect=on,name=generic "
        f"--noautoconsole --wait 0 --import 2>&1"
    )
    push_log(f"Import {meta['id']} → virt-install",
             node=home_name, app="bedrock-mgmt", level="info")
    out, rc = ssh_cmd_rc(host, vi_cmd, timeout=120)
    if rc != 0:
        ssh_cmd_rc(host, f"virsh undefine {req.name} --nvram 2>&1", timeout=10)
        ssh_cmd_rc(host, f"lvremove -f {lv_path}", timeout=30)
        raise HTTPException(500, "virt-install failed:\n" + (out or "(no output)"))

    # Priority
    shares = PRIORITY_CPU_SHARES[req.priority]
    ssh_cmd_rc(host, f"virsh schedinfo {req.name} --live --config cpu_shares={shares}",
               timeout=10)

    # Inventory
    inv = load_inventory()
    inv[req.name] = {
        "priority": req.priority, "vcpus": req.vcpus, "ram_mb": req.ram_mb,
        "disk_gb": virtual_gb, "iso": None,
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

    push_log(f"Imported VM {req.name} on {home_name} (vcpus={req.vcpus}, "
             f"ram={req.ram_mb}MB, {virtual_gb}GB, from {meta.get('original_name')})",
             node=home_name, app="bedrock-mgmt", level="info")
    return {"status": "created", "name": req.name, "node": home_name}


def _vm_delete(vm_name: str) -> dict:
    """Stop (if running), tear down DRBD (if any), undefine VM + remove LVs
    on every node where it was defined, drop inventory entry."""
    state = build_cluster_state()
    vm = state["vms"].get(vm_name)
    if not vm:
        raise HTTPException(404, f"Unknown VM: {vm_name}")
    nodes_cfg = get_nodes()
    defined_on = vm.get("defined_on") or ([vm["running_on"]] if vm.get("running_on") else [])
    if not defined_on:
        raise HTTPException(500, "VM has no defined_on nodes")

    resource = vm.get("drbd_resource", "")
    existing = _parse_drbd_res(nodes_cfg[defined_on[0]]["host"], resource) if resource else {}
    lv_path = existing.get("lv_path", f"/dev/almalinux/vm-{vm_name}-disk0")
    meta_path = existing.get("meta_path", "")

    # 1. Stop the VM (graceful shutdown → force-kill fallback)
    if vm["state"] == "running" and vm.get("running_on"):
        host = nodes_cfg[vm["running_on"]]["host"]
        ssh_cmd_rc(host, f"virsh destroy {vm_name} 2>&1", timeout=15)

    # 2. For each node that has the VM, tear it down
    for nname in defined_on:
        if nname not in nodes_cfg: continue
        host = nodes_cfg[nname]["host"]
        ssh_cmd_rc(host, f"virsh undefine {vm_name} --nvram 2>&1 || virsh undefine {vm_name} 2>&1", timeout=15)
        if resource:
            ssh_cmd_rc(host, f"drbdadm down {resource} 2>&1 || true", timeout=15)
            ssh_cmd_rc(host, f"drbdadm wipe-md --force {resource} 2>&1 || true", timeout=15)
            ssh_cmd_rc(host, f"rm -f /etc/drbd.d/{resource}.res", timeout=10)
        # Remove LVs (data + meta if present)
        rm_paths = lv_path + (f" {meta_path}" if meta_path else "")
        ssh_cmd_rc(host, f"lvremove -f {rm_paths} 2>&1 || true", timeout=30)

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
