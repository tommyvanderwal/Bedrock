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
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, UploadFile, File, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ws import hub
from tasks import registry as task_registry, Task

# Peer-auth + operator-auth + join-handshake — these modules live in the
# bedrock lib tree (installer-deployed) rather than mgmt's own source
# dir so installers and mgmt share the same code.
import sys as _sys_peerauth
_sys_peerauth.path.insert(0, "/usr/local/lib/bedrock")
from lib import peer_auth as _peer_auth        # noqa: E402
from lib import operator_auth as _op_auth      # noqa: E402
from lib import join_handshake as _join_hs     # noqa: E402
from lib import bedrock_state as _bs           # noqa: E402
from lib import rqlite_client as _rqlite       # noqa: E402
from lib import cluster_state as _cluster_state  # noqa: E402


async def require_peer(request: Request) -> str:
    """FastAPI dep — accepts requests signed by a known cluster node.
    Returns the verified node name. Raises 401 on any failure."""
    body = await request.body()

    def _lookup(node_name: str):
        cluster = load_cluster()
        n = (cluster.get("nodes") or {}).get(node_name) or {}
        pk_hex = (n.get("bedrock_pubkey") or "").strip()
        if not pk_hex:
            return None
        try:
            return bytes.fromhex(pk_hex)
        except ValueError:
            return None

    authz = request.headers.get("authorization", "")
    try:
        return _peer_auth.verify(authz, request.method,
                                 request.url.path
                                 + (("?" + request.url.query) if request.url.query else ""),
                                 body, _lookup)
    except ValueError as e:
        raise HTTPException(401, f"peer auth failed: {e}")


async def require_operator(request: Request) -> str:
    """FastAPI dep — accepts requests with a valid `Authorization: Bearer
    <token>` operator session token. Returns the username on success.
    Raises 401 on any failure. Loopback (the trusted local CLI on :8001)
    is exempt — local root is already privileged; see _auth_middleware."""
    _ch = request.client.host if request.client else ""
    if _ch in ("127.0.0.1", "::1"):
        return "local"
    authz = request.headers.get("authorization", "")
    if not authz.startswith("Bearer "):
        raise HTTPException(401, "missing Bearer token")
    try:
        payload = _op_auth.verify_token(authz[7:].strip())
    except ValueError as e:
        raise HTTPException(401, f"operator auth failed: {e}")
    return payload.get("sub", "")


async def require_operator_or_peer(request: Request) -> str:
    """Accepts EITHER a peer Ed25519 signature OR an operator Bearer
    token. Returns `op:<user>` or `peer:<node>` so handlers know which.
    Use for endpoints that legitimately need both call sites (e.g. an
    operator clicks "transfer mgmt" in the dashboard AND the receiving
    node's mgmt service finishes the handoff by calling back)."""
    authz = request.headers.get("authorization", "")
    if authz.startswith("Bearer "):
        try:
            payload = _op_auth.verify_token(authz[7:].strip())
            return f"op:{payload.get('sub', '')}"
        except ValueError as e:
            raise HTTPException(401, f"operator auth failed: {e}")
    if authz.startswith(_peer_auth.SCHEME + " "):
        body = await request.body()

        def _lookup(node_name: str):
            cluster = load_cluster()
            n = (cluster.get("nodes") or {}).get(node_name) or {}
            pk_hex = (n.get("bedrock_pubkey") or "").strip()
            try:
                return bytes.fromhex(pk_hex) if pk_hex else None
            except ValueError:
                return None

        try:
            who = _peer_auth.verify(
                authz, request.method,
                request.url.path + (("?" + request.url.query) if request.url.query else ""),
                body, _lookup)
            return f"peer:{who}"
        except ValueError as e:
            raise HTTPException(401, f"peer auth failed: {e}")
    raise HTTPException(401, "missing operator or peer credentials")

# (The /api/peer-test smoke endpoint is registered after `app = FastAPI()`,
# search for it below.)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("bedrock")

# ── Config ──────────────────────────────────────────────────────────────────

CLUSTER_FILE = Path("/etc/bedrock/cluster.json")
SSH_USER = "root"
import os as _os


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

# ── SSH helpers ─────────────────────────────────────────────────────────────
#
# Connection pool: paramiko `SSHClient` per host, reused across calls.
# Without this, every `ssh_cmd` opened a fresh TCP+kex+auth — at N=4
# nodes × every-3-second probe loop × 4 mgmt processes (master + 3
# followers) sshd's pre-auth queue filled up and dropped connections
# with "exceeded LoginGraceTime" penalty, manifesting as nodes
# flapping between Online/Offline on the dashboard. Caching reuses
# a single Transport per peer + opens new channels on demand, which
# is what paramiko is designed for.
import threading as _threading

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


# ── Physical topology rollup ────────────────────────────────────────────────

PHYSICAL_TOPOLOGY_CACHE = Path("/run/bedrock/physical_topology.json")


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


# ── FastAPI app ─────────────────────────────────────────────────────────────

app = FastAPI(title="Bedrock Cluster Manager")


# ── Auth middleware ─────────────────────────────────────────────────
# Every /api/* request must carry either:
#   - operator Bearer token (issued by /api/login), OR
#   - peer Ed25519 signature (`Authorization: Bedrock-Ed25519 ...`)
# Public-path allow-list covers discovery, login, the join handshake
# (joiner doesn't yet have credentials), and the static dashboard
# assets (the browser fetches HTML/JS/CSS before login).

from fastapi.responses import JSONResponse as _JSONResponse  # noqa: E402

_PUBLIC_PREFIXES = (
    "/_app/", "/favicon", "/static/", "/assets/",
)
_PUBLIC_EXACT = {
    "/", "/login", "/cluster-info", "/health",
    "/api/login",
    "/api/join/request", "/api/join/status",
}


def _is_public(path: str) -> bool:
    if path in _PUBLIC_EXACT:
        return True
    for pfx in _PUBLIC_PREFIXES:
        if path.startswith(pfx):
            return True
    # SvelteKit routes that the browser may hit before login (the
    # static-adapter prerenders them). Treat all non-/api/ paths as
    # static-page-fetches → the route guard does the redirect to /login.
    if not path.startswith("/api/") and not path.startswith("/ws"):
        return True
    return False


@app.middleware("http")
async def _auth_middleware(request: Request, call_next):
    path = request.url.path
    if _is_public(path):
        return await call_next(request)

    # Loopback is the trusted local CLI on :8001 (bound 127.0.0.1 only).
    # Local root is already fully privileged, so the `bedrock` CLI's POSTs
    # carry no operator token. LAN requests (:8443) still require operator
    # or peer auth below. A spoofed-loopback source from a real NIC is
    # dropped by rp_filter/martian filtering, so this can't be reached
    # remotely.
    _ch = request.client.host if request.client else ""
    if _ch in ("127.0.0.1", "::1"):
        return await call_next(request)

    authz = request.headers.get("authorization", "")
    if authz.startswith("Bearer "):
        try:
            _op_auth.verify_token(authz[7:].strip())
            return await call_next(request)
        except ValueError as e:
            return _JSONResponse({"detail": f"operator auth: {e}"}, status_code=401)

    if authz.startswith(_peer_auth.SCHEME + " "):
        body = await request.body()
        # Restore body for the route handler. Without this, request.body()
        # in the handler hangs because the stream is already drained.

        async def _receive():
            return {"type": "http.request", "body": body, "more_body": False}
        request._receive = _receive

        def _lookup(node_name: str):
            cluster = load_cluster()
            n = (cluster.get("nodes") or {}).get(node_name) or {}
            pk_hex = (n.get("bedrock_pubkey") or "").strip()
            try:
                return bytes.fromhex(pk_hex) if pk_hex else None
            except ValueError:
                return None
        try:
            _peer_auth.verify(
                authz, request.method,
                path + (("?" + request.url.query) if request.url.query else ""),
                body, _lookup)
            return await call_next(request)
        except ValueError as e:
            return _JSONResponse({"detail": f"peer auth: {e}"}, status_code=401)

    return _JSONResponse({"detail": "authentication required"}, status_code=401)


# Smoke endpoint for the Ed25519 framework. Returns the caller's verified
# node name. Used by tests + operators wanting to confirm that inter-node
# signing is wired up correctly.
@app.get("/api/peer-test")
def peer_test(node: str = Depends(require_peer)):
    return {"verified_caller": node}


# CDC fast-path receiver. The leader, on an applied rqlite commit, fans this
# out to every node so the central loop converges near-instantly instead of
# waiting for its poll floor. Idempotent + cheap: it only nudges the loop to
# re-read; the loop still does the authoritative master-first read itself.
@app.post("/api/internal/check-now")
def internal_check_now(node: str = Depends(require_peer)):
    from mgmt import orchestrator as _orch
    return {"woke": _orch.signal_check_now()}


# CDC source. The local rqlited — only when it is the Raft leader — POSTs an
# applied-commit event here (loopback only; rqlited dials 127.0.0.1). We don't
# need the payload: any committed change means "converge now". We wake our own
# loop, then fan the nudge out to every other node so the whole cluster
# converges near-instantly instead of each waiting out its poll floor. The
# fan-out runs in a thread (blocking signed HTTP) and is best-effort — the poll
# floor backstops any peer it misses.
@app.post("/api/internal/cdc")
async def internal_cdc(request: Request):
    ch = request.client.host if request.client else ""
    if ch not in ("127.0.0.1", "::1"):
        raise HTTPException(403, "cdc endpoint is loopback-only")
    await request.body()                      # drain so rqlited gets its 200
    from mgmt import orchestrator as _orch
    _orch.signal_check_now()                  # our own loop
    asyncio.create_task(asyncio.to_thread(_orch.fanout_check_now_blocking))
    return {"ok": True}


# ── Operator login ──────────────────────────────────────────────────

class LoginReq(BaseModel):
    username: str
    password: str


# Per-IP leaky-bucket rate limiter so a brute-forcer can't fill the
# event loop with PBKDF2 work. 5 fails/min/IP; resets on success.
_LOGIN_BUCKET: dict[str, list[float]] = {}
_LOGIN_MAX = 5
_LOGIN_WINDOW_S = 60


def _login_throttle(ip: str) -> bool:
    """Returns True if the request should be rejected."""
    import time as _t
    now = _t.time()
    bucket = [t for t in _LOGIN_BUCKET.get(ip, []) if now - t < _LOGIN_WINDOW_S]
    _LOGIN_BUCKET[ip] = bucket
    return len(bucket) >= _LOGIN_MAX


def _login_record_fail(ip: str) -> None:
    import time as _t
    _LOGIN_BUCKET.setdefault(ip, []).append(_t.time())


@app.post("/api/login")
def login(req: LoginReq, request: Request):
    ip = request.client.host if request.client else "?"
    if _login_throttle(ip):
        raise HTTPException(429, "too many failed logins, try again in a minute")
    ops = (load_cluster().get("operators") or {})
    op = ops.get(req.username) or {}
    if not _op_auth.verify_password(req.password, op.get("salt", ""), op.get("hash", "")):
        _login_record_fail(ip)
        # Constant-ish response time: PBKDF2 already ran for ~150ms regardless
        # of whether the user exists. Don't differentiate "no such user" vs
        # "wrong password" in the response.
        raise HTTPException(401, "invalid credentials")
    token, exp = _op_auth.mint_token(req.username)
    push_log(f"operator {req.username!r} logged in from {ip}",
             node="mgmt", app="bedrock-mgmt", level="info")
    return {"token": token, "exp": exp, "user": req.username}


@app.get("/api/whoami")
def whoami(user: str = Depends(require_operator)):
    return {"user": user}


# ── Operator management (passwd / list / remove) ───────────────────

class OperatorSet(BaseModel):
    username: str
    password: str


@app.get("/api/operators")
def list_operators(user: str = Depends(require_operator)):
    """Return the list of operator usernames. Hashes are NOT exposed —
    they live write-only in the replicated `operators` cluster state."""
    ops = (load_cluster().get("operators") or {})
    return {"operators": sorted(ops.keys())}


@app.post("/api/operators/set")
def set_operator(req: OperatorSet, user: str = Depends(require_operator)):
    """Upsert an operator credential. `bedrock operator passwd <user>`
    uses this — the same endpoint adds a new operator OR changes an
    existing one's password (the rqlite write is upsert-shaped). Operator
    must already be authenticated; we don't require the OLD password
    because the Bearer token already proves authority.
    """
    if not req.username or not req.password:
        raise HTTPException(400, "username and password required")
    if len(req.password) < 4:
        raise HTTPException(400, "password too short (min 4 chars for now)")
    salt, phash = _op_auth.hash_password(req.password)
    try:
        _bs.operator_set(
            username=req.username, salt=salt, password_hash=phash)
    except Exception as e:
        raise HTTPException(503, f"could not set operator: {e}")
    push_log(f"operator {user!r} set password for {req.username!r}",
             node="mgmt", app="bedrock-mgmt", level="info")
    return {"username": req.username, "status": "set"}


class OperatorRemove(BaseModel):
    username: str


@app.post("/api/operators/remove")
def remove_operator(req: OperatorRemove, user: str = Depends(require_operator)):
    """Delete an operator. Refuses to remove the last operator — that
    would lock the cluster's dashboard. Also refuses to remove
    yourself (operator must be removed by a different operator, so
    accidental lockout requires two mistakes)."""
    ops = (load_cluster().get("operators") or {})
    if req.username not in ops:
        raise HTTPException(404, f"no such operator: {req.username!r}")
    if req.username == user:
        raise HTTPException(400, "refusing to remove yourself; "
                                  "ask another operator")
    if len(ops) <= 1:
        raise HTTPException(400, "refusing to remove the last operator "
                                  "(cluster would lock out)")
    try:
        _bs.operator_remove(username=req.username)
    except Exception as e:
        raise HTTPException(503, f"could not remove operator: {e}")
    push_log(f"operator {user!r} removed {req.username!r}",
             node="mgmt", app="bedrock-mgmt", level="warn")
    return {"username": req.username, "status": "removed"}


# ── Join handshake ──────────────────────────────────────────────────
# A joiner doesn't yet have an operator token or a recognised peer
# identity, so /api/join/request is UNAUTH. The privacy of the
# handshake comes from:
#   - operator visually verifying the Ed25519 fingerprint on approval,
#   - X25519 ECDH so cluster.key never traverses the wire in plaintext.
# The request id alone doesn't authorise anything: it's a handle the
# joiner polls; the master only acts when an operator approves.

class JoinRequest(BaseModel):
    node_name: str
    host: str
    bedrock_pubkey: str           # joiner's Ed25519 identity (hex)
    x25519_eph_pubkey: str        # joiner's X25519 ephemeral (base64)
    ssh_pubkey: str = ""          # joiner's OpenSSH ed25519 line (`ssh-ed25519 …`)


# In-memory cache of master's X25519 private halves, keyed by request_id.
# When operator approves, we look up the private key here, do ECDH +
# AEAD, then drop the private key. Lost on mgmt restart — joiners that
# polled before approval and saw their request go stale need to retry,
# which is correct UX for a security-critical handshake.
_MASTER_EPH_PRIV: dict[str, "X25519PrivateKey"] = {}   # noqa: F821


@app.post("/api/join/request")
def join_request(req: JoinRequest):
    """Joiner asks to join. We log the request (replicates everywhere
    so any node's dashboard shows the popup); operator decides.

    `ssh_pubkey` is the joiner's OpenSSH `ssh-ed25519 …` line — kept on
    the in-process pending map (NOT in the log; we don't want to leak
    half-baked SSH identities into the replicated state) and installed
    on every node's authorized_keys when the operator approves.
    """
    rid = _join_hs.new_request_id()
    fp = _join_hs.fingerprint(req.bedrock_pubkey)
    try:
        _bs.join_request(
            request_id=rid,
            node_name=req.node_name,
            host=req.host,
            bedrock_pubkey=req.bedrock_pubkey,
            x25519_eph_pubkey=req.x25519_eph_pubkey,
            fingerprint=fp,
        )
    except Exception as e:
        raise HTTPException(503, f"could not record join request: {e}")
    # Cache the SSH pubkey + host so the approve handler can install it
    # without needing the joiner to re-send.
    _PENDING_SSH_PUBKEYS[rid] = {"ssh_pubkey": req.ssh_pubkey, "host": req.host}
    push_log(f"join request: {req.node_name} ({req.host}) fp={fp}",
             node="mgmt", app="bedrock-mgmt", level="info")
    return {"request_id": rid, "fingerprint": fp}


# Side-channel for the joiner's SSH pubkey + host, by request_id.
# Lives in memory only; lost on restart. If the operator approves after
# a mgmt restart, the approve handler still works for the crypto path
# but skips the SSH pubkey installation — peer-SSH from this node to
# the joiner won't work until `bedrock node refresh-keys` (TODO).
_PENDING_SSH_PUBKEYS: dict[str, dict] = {}


@app.get("/api/join/status")
def join_status(id: str):
    """Joiner polls this to learn whether an operator approved or
    rejected. No auth — the request_id is the handle (unguessable
    192-bit secret)."""
    cluster = load_cluster()
    req = (cluster.get("join_requests") or {}).get(id)
    if not req:
        raise HTTPException(404, "unknown request_id")
    out = {"state": req.get("state", "pending")}
    if out["state"] == "approved":
        # ECDH bundle so the joiner can decrypt cluster.key.
        out["master_eph_pubkey"] = req.get("master_eph_pubkey", "")
        out["ciphertext"] = req.get("ciphertext", "")
        out["nonce"] = req.get("nonce", "")
        # Cluster membership the joiner needs to finish install. All
        # this lives in the replicated snapshot anyway, but inlining it
        # here saves the joiner a second authenticated round-trip.
        node_name = req.get("node_name", "")
        node_info = (cluster.get("nodes") or {}).get(node_name) or {}
        peer_pubkeys = []
        peer_ips = []
        for n_name, n in (cluster.get("nodes") or {}).items():
            if n_name == node_name:
                continue
            if n.get("pubkey"):
                peer_pubkeys.append(n["pubkey"])
            if n.get("host"):
                peer_ips.append(n["host"])
        # mgmt-master's loopback /32 — that's where the joiner's
        # bedrock-rust dials. Falls back to first node with "mgmt"
        # in role if mgmt_master isn't set yet.
        master_name = None
        for n_name, n in (cluster.get("nodes") or {}).items():
            if "mgmt" in (n.get("role", "") or ""):
                master_name = n_name; break
        master_addr = ((cluster.get("nodes") or {}).get(master_name, {})
                       .get("loopback_ip")
                       or (cluster.get("nodes") or {}).get(master_name, {})
                       .get("host", "")) if master_name else ""
        # Full per-node map so the joiner can write a bootstrap
        # cluster.json — required for rqlite_setup.render_env_file()
        # to compute peer loopbacks and the sorted-name node-id.
        node_map = {}
        for n_name, n in (cluster.get("nodes") or {}).items():
            node_map[n_name] = {
                "host":          n.get("host", ""),
                "loopback_ip":   n.get("loopback_ip", ""),
                "role":          n.get("role", "compute"),
                "pubkey":        n.get("pubkey", ""),
                "bedrock_pubkey": n.get("bedrock_pubkey", ""),
            }
        out.update({
            "cluster_name": cluster.get("cluster_name", "bedrock"),
            "cluster_uuid": cluster.get("cluster_uuid", ""),
            "loopback_ip":  node_info.get("loopback_ip", ""),
            "peer_pubkeys": peer_pubkeys,
            "peer_ips":     sorted(set(peer_ips)),
            "master_loopback_ip": master_addr,
            "mgmt_master":  master_name or "",
            "nodes":        list((cluster.get("nodes") or {}).keys()),
            "node_map":     node_map,
            # Cluster CA + the joiner's CA-signed TLS cert. PEM-encoded.
            # The joiner uses these to configure rqlited mTLS as part of
            # its install. Filled by /api/join/approve via
            # cluster_ca.sign_node_cert; default '' if approval came
            # from a pre-TLS master that hasn't been re-installed yet.
            "node_cert_pem": req.get("node_cert_pem", ""),
            "ca_cert_pem":   req.get("ca_cert_pem", ""),
        })
    elif out["state"] == "rejected":
        out["reason"] = req.get("reason", "")
    return out


@app.get("/api/join/pending")
def join_pending(user: str = Depends(require_operator)):
    """Dashboard polls this to drive the approval popup."""
    cluster = load_cluster()
    items = []
    for rid, r in (cluster.get("join_requests") or {}).items():
        if r.get("state") == "pending":
            items.append({"request_id": rid, **r})
    return {"pending": items}


class JoinApprove(BaseModel):
    request_id: str


@app.post("/api/join/approve")
def join_approve(req: JoinApprove, user: str = Depends(require_operator)):
    cluster = load_cluster()
    pending = (cluster.get("join_requests") or {}).get(req.request_id) or {}
    if pending.get("state") != "pending":
        raise HTTPException(400, f"request not pending (state={pending.get('state')!r})")

    # Generate master's ephemeral X25519 + seal cluster.key under the ECDH
    # session key (HKDF salted with request_id).
    master_priv, master_pub_b64 = _join_hs.gen_ephemeral()
    cluster_key = Path("/etc/bedrock/cluster.key").read_bytes()
    ciphertext_b64, nonce_b64 = _join_hs.seal(
        master_priv, pending["x25519_eph_pubkey"],
        req.request_id, cluster_key)

    # Allocate the joiner's loopback /32 by scanning rqlite.nodes for
    # taken indices (the authoritative source; the local replica read
    # below may briefly lag, hence the level='strong' query).
    used_loopbacks: set[str] = set()
    try:
        with _rqlite.RqliteClient() as _rc:
            for row in _rc.query(
                "SELECT loopback_ip FROM nodes WHERE loopback_ip <> ''",
                level="strong",
            ):
                used_loopbacks.add(row["loopback_ip"])
    except Exception:
        used_loopbacks = {n.get("loopback_ip")
                          for n in (cluster.get("nodes") or {}).values()
                          if n.get("loopback_ip")}
    _sys_peerauth.path.insert(0, "/usr/local/lib/bedrock")
    from lib import cluster_addr as _ca
    next_loopback = ""
    for i in range(1, 250):
        cand = _ca.node_loopback_ip(cluster.get("cluster_uuid", ""), i)
        if cand not in used_loopbacks:
            next_loopback = cand; break

    # Pull joiner's SSH pubkey from the in-memory side-channel (was
    # cached at /api/join/request time — see _PENDING_SSH_PUBKEYS).
    ssh_info = _PENDING_SSH_PUBKEYS.pop(req.request_id, {}) or {}
    joiner_ssh_pubkey = (ssh_info.get("ssh_pubkey") or "").strip()

    # Install the joiner's SSH pubkey locally (mgmt → joiner SSH works)
    # AND fan it out to every existing peer (peer → joiner SSH works).
    # Without this, the moment any node tries paramiko-probe the
    # joiner, sshd auth-fails accumulate per-source-IP penalties until
    # OpenSSH PerSourcePenalties stops accepting from that source for
    # up to 10 minutes — manifesting as the joiner flapping Offline on
    # every dashboard.
    if joiner_ssh_pubkey:
        _append_authorized_key(joiner_ssh_pubkey)
        for n_name, n in (cluster.get("nodes") or {}).items():
            host = n.get("host", "")
            if host and host != pending["host"]:
                try:
                    _append_authorized_key(joiner_ssh_pubkey, host)
                except Exception as _e:
                    push_log(f"fan-out pubkey to {host} failed: {_e}",
                             node="mgmt", app="bedrock-mgmt", level="warn")

    # Auto-promote on the 1→2 transition: if the cluster currently has
    # only 1 metrics/logs backend, appoint the joiner as the 2nd one.
    # N≥3 joins do NOT change the backend list — they stay agent-only
    # nodes (decommission/promote-spare is a separate operator action,
    # not implemented yet).
    obs_now = (cluster.get("obs_backends") or {})
    metrics_bk = list(obs_now.get("metrics") or [])
    logs_bk    = list(obs_now.get("logs") or [])
    promote_metrics = len(metrics_bk) < 2 and pending["node_name"] not in metrics_bk
    promote_logs    = len(logs_bk)    < 2 and pending["node_name"] not in logs_bk
    if promote_metrics:
        metrics_bk.append(pending["node_name"])
    if promote_logs:
        logs_bk.append(pending["node_name"])

    # If we're promoting this joiner to a backend slot AND there's an
    # existing backend with data, seed the joiner's data dir from the
    # existing backend BEFORE the snapshot says "joiner is a backend".
    # That way the reactor doesn't start an empty backend that agents
    # then dual-write into — we'd accumulate a gap until 90d
    # Stage the auto-promote: same agents-first → seed → start ordering
    # as `observability_promote`. See that handler for the rationale —
    # this block keeps the same shape so behaviour stays consistent.

    # Phase 1: log node_register + node_loopback + (optionally) the
    # OBS_BACKENDS_SET that adds the joiner. Agents on every node then
    # reconfigure to dual-write toward the joiner (queuing because the
    # joiner's bedrock-vm isn't up yet). The joiner's reactor writes
    # the unit file but `_can_start_vm_backend` keeps bedrock-vm
    # stopped until the seed populates the data dir.
    # Sign the joiner's TLS cert with the cluster CA so the joiner
    # can configure rqlited mTLS as part of its install. The joiner's
    # raw Ed25519 pubkey came in pending["bedrock_pubkey"] (hex). CA
    # key+cert live on the DRBD `cluster` singleton mount (master only) per
    # cluster_ca.py — failure here means we lost the master role
    # mid-handshake and should surface to operator.
    try:
        from lib import cluster_ca as _ca
        joiner_pub_raw = bytes.fromhex(pending["bedrock_pubkey"])
        joiner_node_cert_pem = _ca.sign_node_cert(
            joiner_pub_raw, pending["node_name"], next_loopback
        ).decode("ascii")
        ca_cert_pem = _ca.CA_CERT_DRBD.read_bytes().decode("ascii")
    except Exception as e:
        raise HTTPException(503, f"could not sign joiner cert: {e}")

    try:
        with _rqlite.RqliteClient() as _rc:
            _bs.node_register(
                node_name=pending["node_name"],
                host=pending["host"],
                role="compute",
                pubkey=joiner_ssh_pubkey,
                bedrock_pubkey=pending["bedrock_pubkey"],
                # 'joining' until the joiner's saga self-activates at the
                # end of its join (node_set_active). Keeps the joiner out
                # of the election denominator so the master can't be
                # tipped into NoQuorum mid-join (C1).
                state="joining",
                client=_rc,
            )
            if next_loopback:
                _bs.node_loopback(
                    node_name=pending["node_name"],
                    loopback_ip=next_loopback,
                    client=_rc,
                )
            if promote_metrics or promote_logs:
                _bs.obs_backends_set(
                    metrics=metrics_bk, logs=logs_bk, client=_rc)
            _bs.join_resolved(
                request_id=req.request_id,
                decision="approved",
                master_eph_pubkey=master_pub_b64,
                ciphertext=ciphertext_b64,
                nonce=nonce_b64,
                node_cert_pem=joiner_node_cert_pem,
                ca_cert_pem=ca_cert_pem,
                client=_rc,
            )
    except Exception as e:
        raise HTTPException(503, f"could not record approval: {e}")

    # Phase 2: brief wait for agents to fold the new entry + start
    # queueing writes for the joiner.
    if promote_metrics or promote_logs:
        import time as _t
        _t.sleep(2)

    # Phase 3: seed the joiner's data dir from the existing backend.
    # Writes that arrived between the snapshot point and the joiner's
    # backend-start are safe in the agents' disk queues; they drain
    # in phase 4.
    if (promote_metrics and obs_now.get("metrics")) or \
       (promote_logs and obs_now.get("logs")):
        try:
            from lib import observability as _obs
            existing_metrics_bk = (obs_now.get("metrics") or [])
            source_metrics_host = ((cluster.get("nodes") or {})
                                   .get(existing_metrics_bk[0], {}).get("host", "")) \
                                  if existing_metrics_bk else ""
            target_host = pending["host"]

            def _runner(host: str, cmd: str, timeout: int = 60):
                return ssh_cmd_rc(host, cmd, timeout=timeout)

            if promote_metrics and source_metrics_host:
                push_log(f"seeding metrics backend on {pending['node_name']} from {source_metrics_host}",
                         node="mgmt", app="bedrock-mgmt", level="info")
                rep = _obs.seed_backend(source_metrics_host, target_host,
                                        _runner, None)
                push_log(f"metrics seed: {rep.get('metrics','?')}",
                         node="mgmt", app="bedrock-mgmt", level="info")
        except Exception as e:
            push_log(f"seed_backend warning: {e}",
                     node="mgmt", app="bedrock-mgmt", level="warn")

    # Phase 4: start bedrock-vm on the joiner (the seed gate kept it
    # stopped; explicit kick gets it running with the seeded data).
    # bedrock-vl was already started by the reactor (no seed for VL).
    if promote_metrics and pending["host"]:
        try:
            ssh_cmd(pending["host"], "systemctl start bedrock-vm.service", timeout=20)
        except Exception as e:
            push_log(f"could not start bedrock-vm on {pending['node_name']}: {e}",
                     node="mgmt", app="bedrock-mgmt", level="warn")
    push_log(f"operator {user!r} approved join {pending['node_name']} ({pending['host']})",
             node="mgmt", app="bedrock-mgmt", level="info")
    return {"state": "approved", "loopback_ip": next_loopback}


class JoinReject(BaseModel):
    request_id: str
    reason: str = ""


# ── Observability backend management (operator CLI) ─────────────────

class ObsPromote(BaseModel):
    new_node: str
    replace: str = ""
    kind: str = "both"   # "both", "metrics", or "logs"


@app.post("/api/observability/backends")
def observability_promote(req: ObsPromote, user: str = Depends(require_operator)):
    """Add or swap a backend in `obs_backends`. Runs the vmbackup-
    vmrestore seed BEFORE flipping the snapshot so the new backend
    isn't visible until it's caught up. Synchronous — operator CLI
    waits on this call."""
    cluster = load_cluster()
    nodes = (cluster.get("nodes") or {})
    if req.new_node not in nodes:
        raise HTTPException(400, f"unknown node {req.new_node!r}")
    obs = (cluster.get("obs_backends") or {})
    metrics_bk = list(obs.get("metrics") or [])
    logs_bk    = list(obs.get("logs") or [])
    do_metrics = req.kind in ("both", "metrics")
    do_logs    = req.kind in ("both", "logs")

    def _slot(curr: list[str]) -> tuple[list[str], str]:
        """Compute the post-promote list + source for seeding. Returns
        (new_list, source_host_or_'')."""
        if req.new_node in curr:
            return curr, ""   # nothing to do — already a backend
        if len(curr) < 2:
            # Free slot available; just append.
            return curr + [req.new_node], (nodes.get(curr[0], {}).get("host", "") if curr else "")
        # Both slots full → must replace.
        if not req.replace:
            raise HTTPException(400, "both backend slots full; pass --replace")
        if req.replace not in curr:
            raise HTTPException(400, f"--replace {req.replace!r} not in current backend list {curr}")
        # Seed from the OTHER existing backend (the one we're keeping).
        keep = [b for b in curr if b != req.replace][0]
        return [n if n != req.replace else req.new_node for n in curr], \
               nodes.get(keep, {}).get("host", "")

    new_metrics, src_metrics = (_slot(metrics_bk) if do_metrics else (metrics_bk, ""))
    new_logs,    src_logs    = (_slot(logs_bk)    if do_logs    else (logs_bk, ""))

    target_host = nodes[req.new_node].get("host", "")
    if not target_host:
        raise HTTPException(503, f"{req.new_node!r} has no host address")

    # === Phase 1: flip the snapshot FIRST. ===
    # This puts the new node into the agent target list everywhere.
    # Every node's vmagent + vlagent reconfigures and starts dual-
    # writing to the new target — which isn't accepting yet, so writes
    # accumulate in the agent disk queue. The new node's reactor sees
    # itself in `obs_backends` but `_can_start_vm_backend` returns
    # False (data dir empty + not solo backend), so bedrock-vm stays
    # stopped. bedrock-vl starts (VL has no seed path).
    try:
        _bs.obs_backends_set(metrics=new_metrics, logs=new_logs)
    except Exception as e:
        raise HTTPException(503, f"could not set obs backends: {e}")

    # Give agents a moment to fold the entry + reconfigure. Two seconds
    # is enough on the testbed; the orchestrator subscriber polls fast.
    # If we skipped this and went straight to seed, agents would still
    # be configured for the OLD target list and writes between snapshot
    # and start would land only on the source — exactly the gap this
    # reorder eliminates.
    import time as _t
    _t.sleep(2)

    # === Phase 2: seed the new node's data dir. ===
    # During this window: agents are buffering for the new target;
    # source backend is still serving reads. vmbackup snapshots the
    # source at this instant, ships, vmrestores into the target's data
    # dir. The seed is "frozen in time" from this snapshot moment.
    seed_report = {}
    try:
        from lib import observability as _obs

        def _runner(host: str, cmd: str, timeout: int = 60):
            return ssh_cmd_rc(host, cmd, timeout=timeout)

        # `force=True` whenever we're replacing an existing backend.
        # The new node might have stale data from a previous tenancy
        # as a backend; without force, seed_backend's "data dir is
        # not empty, skip" guard would leave that stale data in
        # place. For a free-slot promote (cluster expansion 1→2),
        # the empty-data-dir check is the right safety net.
        _force = bool(req.replace)
        if do_metrics and src_metrics and req.new_node not in metrics_bk:
            rep = _obs.seed_backend(src_metrics, target_host, _runner, None,
                                    force=_force)
            seed_report["metrics"] = rep.get("metrics", "?")
        if do_logs and src_logs and req.new_node not in logs_bk:
            if src_logs != src_metrics or not do_metrics:
                rep = _obs.seed_backend(src_logs, target_host, _runner, None,
                                        force=_force)
            seed_report["logs"] = rep.get("logs", "?")
    except Exception as e:
        push_log(f"obs.seed_backend warning: {e}",
                 node="mgmt", app="bedrock-mgmt", level="warn")

    # === Phase 3: start the backend daemon on the new node. ===
    # Reactor's seed gate keeps bedrock-vm stopped until the data dir
    # is populated. We just populated it via vmrestore, so SSH in and
    # start it explicitly. Once it's up, agents drain their disk-queue
    # buffers (writes that accumulated during phases 1+2) into the new
    # backend — convergence with zero data gap.
    if do_metrics and req.new_node in new_metrics and target_host:
        try:
            ssh_cmd(target_host, "systemctl start bedrock-vm.service", timeout=20)
        except Exception as e:
            push_log(f"could not start bedrock-vm on {req.new_node}: {e}",
                     node="mgmt", app="bedrock-mgmt", level="warn")

    _replace_disp = req.replace or "-"
    push_log(f"operator {user!r} promoted {req.new_node!r} "
             f"(replace={_replace_disp}, kind={req.kind})",
             node="mgmt", app="bedrock-mgmt", level="info")
    return {
        "metrics_backends": new_metrics,
        "logs_backends":    new_logs,
        "seed_report":      seed_report,
    }


@app.post("/api/join/reject")
def join_reject(req: JoinReject, user: str = Depends(require_operator)):
    cluster = load_cluster()
    pending = (cluster.get("join_requests") or {}).get(req.request_id) or {}
    if pending.get("state") != "pending":
        raise HTTPException(400, f"request not pending (state={pending.get('state')!r})")
    try:
        _bs.join_resolved(
            request_id=req.request_id,
            decision="rejected",
            reason=req.reason or "denied by operator",
        )
    except Exception as e:
        raise HTTPException(503, f"could not record rejection: {e}")
    push_log(f"operator {user!r} rejected join {pending.get('node_name','?')}",
             node="mgmt", app="bedrock-mgmt", level="warn")
    return {"state": "rejected"}

# ── WebSocket endpoint ──────────────────────────────────────────────────────

# Last-known cluster state. The state push loop fills it; /ws and /api/cluster
# serve from here instantly so the dashboard never waits on fresh SSH probes.
_last_state: dict = {"nodes": {}, "vms": {}, "witness": {"nodes": {}}}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    # WebSockets bypass the HTTP middleware. Token comes via query param
    # because the browser WebSocket API can't set custom headers.
    token = ws.query_params.get("token", "")
    try:
        _op_auth.verify_token(token)
    except ValueError as e:
        await ws.close(code=1008, reason=f"auth: {e}")
        return
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
        return await loop.run_in_executor(
            None, lambda: api_vm_migrate(
                params["name"], MigrateRequest(target_node=params.get("target_node"))))
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
_STARTUP_DONE: bool = False
_STARTUP_LOCK = threading.Lock()


@app.on_event("startup")
async def startup():
    global _last_state, _main_loop, _STARTUP_DONE
    # Under bedrock-d we run TWO uvicorn instances (8443 HTTPS + 8001
    # loopback) in SEPARATE threads, each with its own event loop —
    # both call this hook on the same `app`. Without a real lock the
    # `if _STARTUP_DONE` check + assign races and both threads proceed.
    # That spawned two no_quorum_responder tasks in v22, which clobbered
    # the 120s wait_for_role (visible as "still no quorum after 120s"
    # appearing within 0 seconds of "no_quorum: cleanup done").
    with _STARTUP_LOCK:
        if _STARTUP_DONE:
            return
        _STARTUP_DONE = True
    _main_loop = asyncio.get_running_loop()
    # Seed from cluster state so the sidebar shows host names instantly.
    cfg = load_cluster()
    _last_state = {
        "nodes": {n: {"name": n, "host": c.get("host", ""), "online": False,
                      "kernel": "", "uptime_since": "", "load": "",
                      "mem_total_mb": 0, "mem_used_mb": 0,
                      "all_vms": [], "running_vms": [], "drbd_raw": "",
                      "switches": {},
                      "cockpit_url": c.get("cockpit", f"https://{c.get('host', '')}:9090")}
                  for n, c in cfg.get("nodes", {}).items()},
        "vms": {},
        "witness": {"nodes": {}},
        "topology": {"switches": {}, "links": [], "node_count": 0,
                     "switch_count": 0, "link_count": 0,
                     "computed_at": 0.0},
    }
    task_registry().wire(_main_loop, hub.broadcast)
    asyncio.create_task(state_push_loop())
    write_scrape_config(cfg)

    # Boot the cluster-protocol orchestrator: log subscriber, boot
    # service-starter, no_quorum responder, reactor.
    #
    # Use sys.modules to share the SAME module instance as bedrock-d.
    # bedrock-d does `from mgmt import orchestrator` (creates
    # `mgmt.orchestrator`) and calls `orchestrator.attach_state(state)`
    # there. A plain `import orchestrator` here would create a SECOND
    # module object (because sys.path has /opt/bedrock/mgmt) with its
    # own _STATE = None — so no_quorum_responder's
    # state.last_election_outcome gate never fires, marker flapping
    # loop bites (observed v29–v31 5c regression: "no_quorum: quorum
    # back as leader; marker cleared" at 0 s after cleanup, repeats
    # every 3 s).
    import sys as _sys
    if "mgmt.orchestrator" in _sys.modules:
        orchestrator = _sys.modules["mgmt.orchestrator"]
    else:
        import orchestrator
    orchestrator.start_all()

# ── REST API (for curl/scripting) ──────────────────────────────────────────

@app.get("/api/cluster")
def api_cluster():
    # Serve cached state. Fresh data lands every 3s via the push loop.
    return _last_state


@app.get("/api/topology")
def api_topology():
    """Physical topology rollup — switches and routers each cluster
    NIC sees, grouped by device_key (MAC). Computed every 3 s by the
    state push loop from each node's /run/bedrock/switch_neighbors.json.
    Not consensus state — purely a derived view for the dashboard."""
    return _last_state.get("topology", {"switches": {}, "links": [],
                                          "node_count": 0, "switch_count": 0,
                                          "link_count": 0,
                                          "computed_at": 0.0})


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
    role: str = "compute"
    pubkey: Optional[str] = None          # SSH ed25519 — paramiko mesh
    bedrock_pubkey: Optional[str] = None  # Ed25519 identity — inter-node API auth


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


# Node registration goes through the join-handshake flow
# (`POST /api/join/request` → operator approval → `POST /api/join/approve`):
# SSH-pubkey fan-out, loopback-IP allocation, and node_register+node_loopback
# logging, with cluster.key shipped AEAD-sealed under an ECDH session key
# (see installer/lib/join_handshake.py) rather than in plaintext.


@app.get("/api/nodes")
def list_nodes():
    return load_cluster().get("nodes", {})


# ── ISO library ─────────────────────────────────────────────────────────────
# The three endpoints (list / upload / delete) live in mgmt/routes_iso.py.
# The ISO_DIR constant + VM inventory helpers stay here because the VM
# creation paths in app.py import them.

# Cluster-wide SeaweedFS FUSE mount — identical on every node, so
# `--cdrom {ISO_DIR}/<name>.iso` works from anywhere. See routes_iso.py
# for the upload path that writes here.
ISO_DIR = Path("/mnt/bedrock/iso")
VM_INVENTORY_FILE = Path("/etc/bedrock/vm_inventory.json")


def load_inventory() -> dict:
    if VM_INVENTORY_FILE.exists():
        try: return json.loads(VM_INVENTORY_FILE.read_text())
        except Exception: return {}
    return {}


def save_inventory(inv: dict):
    VM_INVENTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    VM_INVENTORY_FILE.write_text(json.dumps(inv, indent=2))


# routes_iso registration needs the push_log callable, so it runs after
# `def push_log(...)` further down (see "── routes_iso ──").


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
                # Globs here are case-insensitive on purpose: VMware exports
                # often use .OVF/.VMDK uppercase while Linux tools default to
                # lowercase. Same hygiene as the ISO listing.
                ovf_files = [p for p in extract.iterdir()
                             if p.is_file() and p.suffix.lower() == ".ovf"]
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
                    # Fallback: case-insensitive disk discovery, in the
                    # priority order vmdk → img → raw.
                    by_ext: dict[str, list[Path]] = {".vmdk": [], ".img": [], ".raw": []}
                    for p in extract.iterdir():
                        if p.is_file() and p.suffix.lower() in by_ext:
                            by_ext[p.suffix.lower()].append(p)
                    disk_refs = (sorted(by_ext[".vmdk"])
                                 + sorted(by_ext[".img"])
                                 + sorted(by_ext[".raw"]))
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
                # Single-disk convenience fields, mirroring disks[0]
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


# ── VM lifecycle saga runner ────────────────────────────────────────────────
#
# create / migrate / delete all run through the bedrock_d/vm/* sagas — the
# single live VM-lifecycle path (T-01/T-02). The saga executes ON THE MASTER
# (this mgmt process), which holds DRBD/arbiter authority; the CLI is a thin
# HTTP client that POSTs here. Returns the saga's final state dict.

def _run_vm_saga(kind: str, params: dict) -> dict:
    """Submit + synchronously run a VM-lifecycle saga on this node.
    Raises HTTPException on saga failure so the API returns a real 5xx
    instead of a 200 with a buried error."""
    import sys as _sys
    import socket as _socket
    _sys.path.insert(0, "/usr/local/lib/bedrock")
    _sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from bedrock_d.orchestrator.sagas import SagaExecutor, SagaState
    from bedrock_d.orchestrator.sagas.rqlite_backend import RqliteSagaBackend
    from bedrock_d import state as _st
    # Importing the saga modules registers them in SAGAS.
    from bedrock_d.vm import create as _c, destroy as _d, migrate as _m  # noqa: F401
    ex = SagaExecutor(backend=RqliteSagaBackend(_st.RqliteClient()),
                      this_node=_socket.gethostname())
    op_id = ex.submit(kind=kind, target_node=_socket.gethostname(),
                      params=params, requested_by="mgmt")
    result = ex.execute_one(op_id)
    if result.state != SagaState.COMPLETED:
        raise HTTPException(
            500, f"{kind} saga failed at step "
                 f"{result.last_step!r}: {result.error}")
    return {"op_id": op_id, "state": result.state.value,
            "last_step": result.last_step}


def _vm_create_peers(vm_type: str) -> tuple[str, list[str]]:
    """Resolve (home, peers) for a create. home = the mgmt master; peers
    = home + (replicas-1) other nodes. Raises HTTPException if the
    cluster is too small for the requested type."""
    home = _mgmt_node_name()
    others = [n for n in get_nodes() if n != home]
    if vm_type == "cattle":
        return home, [home]
    if vm_type == "pet":
        if not others:
            raise HTTPException(400, "pet requires ≥1 peer")
        return home, [home, others[0]]
    if vm_type == "vipet":
        if len(others) < 2:
            raise HTTPException(400, "vipet requires ≥2 peers")
        return home, [home, others[0], others[1]]
    raise HTTPException(400, f"unknown vm_type: {vm_type}")


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


class MigrateRequest(BaseModel):
    target_node: Optional[str] = None


class HaLevelRequest(BaseModel):
    vm_type: str  # "cattle", "pet", or "vipet"
    peer_nodes: Optional[list] = None  # auto-pick if not specified


class VMDiskSpec(BaseModel):
    size_gb: int


class VMCreateRequest(BaseModel):
    name: str
    vcpus: int = 2
    ram_mb: int = 2048
    disk_gb: int = 20        # size of the primary (boot) disk
    priority: str = "normal"  # low | normal | high
    iso: Optional[str] = None  # filename in /mnt/bedrock/iso/, optional
    # Workload type. Must satisfy workload.validate_type against current
    # cluster size — pet needs ≥2 nodes, vipet needs ≥3.
    vm_type: str = "cattle"  # cattle | pet | vipet
    # Additional data disks, in order — vdb, vdc, vdd … Each is another thin LV
    # attached to the VM via virtio. Empty list = single-disk VM (unchanged).
    extra_disks: list[VMDiskSpec] = []

@app.post("/api/vms/{vm_name}/start")
def api_vm_start(vm_name: str):
    return _vm_start(vm_name)

@app.post("/api/vms/{vm_name}/stop")
def api_vm_stop(vm_name: str):
    return _vm_shutdown(vm_name)

@app.post("/api/vms/{vm_name}/force-stop")
def api_vm_force_stop(vm_name: str):
    return _vm_poweroff(vm_name)

@app.post("/api/vms/{vm_name}/ha-level")
async def api_vm_set_ha_level(vm_name: str, req: HaLevelRequest):
    """Fire-and-forget. Returns task_id immediately; the dashboard reads
    progress from /api/tasks (WS 'task' channel).

    All validation happens synchronously BEFORE creating the task, so
    clearly-invalid requests fail with a proper 4xx — they don't get a
    200 / task_id + async task-fail, which would mislead the caller."""
    state = build_cluster_state()
    vm = state["vms"].get(vm_name)
    if not vm: raise HTTPException(404, f"VM {vm_name} not found")
    if req.vm_type not in ("cattle", "pet", "vipet"):
        raise HTTPException(400, f"Invalid vm_type: {req.vm_type}")
    nodes_cfg = get_nodes()
    # `running_on` is empty for shut-off VMs — fall back to the first
    # `defined_on` node (where virsh dumpxml resolved) so offline
    # convert works too. Online convert keeps using the live host.
    src_name = (vm.get("running_on")
                or (vm.get("defined_on") or [None])[0])
    if not src_name:
        raise HTTPException(400,
            f"Cannot resolve home node for {vm_name} — VM not defined "
            f"on any cluster node")
    current_type = (
        "vipet" if vm.get("drbd_resource")
            and _count_drbd_peers(nodes_cfg[src_name]["host"], vm["drbd_resource"]) >= 3
        else ("pet" if vm.get("drbd_resource") else "cattle")
    )
    if current_type == req.vm_type:
        return {"status": "no-op", "current": current_type}
    # Upgrade (cattle/pet → pet/vipet): require enough peers up front so
    # an empty peer_nodes list errors before we burn a task on it.
    rank = {"cattle": 0, "pet": 1, "vipet": 2}
    if rank[req.vm_type] > rank[current_type]:
        need_peers = {"pet": 1, "vipet": 2}[req.vm_type]
        chosen = req.peer_nodes or [n for n in nodes_cfg if n != src_name]
        # Filter to only nodes we don't already have on this resource
        if current_type == "pet" and req.vm_type == "vipet":
            existing = _parse_drbd_res(nodes_cfg[src_name]["host"],
                                       vm["drbd_resource"]) or {}
            chosen = [n for n in chosen if n not in existing.get("peers", [])]
            need_peers = 1
        else:
            chosen = [n for n in chosen if n != src_name]
        chosen = chosen[:need_peers]
        if len(chosen) < need_peers:
            raise HTTPException(400,
                f"{req.vm_type} needs {need_peers} peer node(s), "
                f"found {len(chosen)} usable")

    task = task_registry().create(
        "vm.set_ha_level", f"VM {vm_name}: {current_type} → {req.vm_type}",
        vm_name=vm_name, node=src_name)

    async def _runner():
        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(
                None, _vm_set_ha_level, vm_name, req.vm_type, req.peer_nodes, task)
            task.log(f"result: {result}")
            task.succeed()
        except HTTPException as e:
            task.fail(f"{e.status_code}: {e.detail}")
        except Exception as e:
            task.fail(str(e))

    asyncio.create_task(_runner())
    return {"status": "accepted", "task_id": task.id,
            "from": current_type, "to": req.vm_type}


@app.post("/api/vms")
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

    # Validate vm_type against current cluster size. Cattle = local LV
    # (no DRBD, any cluster); pet = 2-way DRBD (≥2 nodes); vipet = 3-way
    # DRBD (≥3 nodes). Reject early — never accept then quietly
    # downgrade pet→cattle, which would silently turn a replicated
    # workload into a single-host one.
    import sys as _sys
    _sys.path.insert(0, "/usr/local/lib/bedrock")
    from lib import workload as _workload
    cluster_state_pre = build_cluster_state()
    node_count = len(cluster_state_pre.get("nodes") or {})
    ok, msg = _workload.validate_type(req.vm_type, node_count)
    if not ok:
        raise HTTPException(400, msg)

    # Existing VM?
    if req.name in cluster_state_pre["vms"]:
        raise HTTPException(409, f"VM {req.name} already exists")

    disk_count = 1 + len(req.extra_disks or [])

    # Resolve home + the full replica peer set BEFORE returning; a bad
    # cluster size for the requested type fails 4xx here, not async.
    # The intent breadcrumb is a secondary durability marker — the saga
    # itself writes a durable operations row that crash-resume keys off.
    home, peers = _vm_create_peers(req.vm_type)
    intent_idx = None
    try:
        intent_idx = _bs.vm_create_intent(
            name=req.name,
            vm_type=req.vm_type,
            host=home,
            ram_mb=int(req.ram_mb),
            disk_gb=int(req.disk_gb),
            requested_by=_os.environ.get("USER", "api"),
        )
    except Exception as e:
        # rqlite unreachable → fall through. The saga still creates the
        # VM (and writes its own durable operations row); we just don't
        # get the vm_create_intent breadcrumb for this run.
        log.warning(f"vm_create_intent write skipped: {e}")

    task = task_registry().create(
        "vm.create",
        f"Create {req.vm_type} VM {req.name} ({req.vcpus} vCPU, "
        f"{req.ram_mb} MB, {disk_count} disk"
        f"{'s' if disk_count != 1 else ''})",
        vm_name=req.name)

    # The bedrock_d vm_create saga is the single live path for every
    # type (cattle / pet / vipet) and is multi-disk aware. It runs ON
    # THE MASTER (this process) and crash-resumes from its own
    # operations row.
    saga_params = {
        "vm_name": req.name, "vcpus": int(req.vcpus),
        "ram_mb": int(req.ram_mb), "disk_gb": int(req.disk_gb),
        "extra_disks": [d.size_gb for d in (req.extra_disks or [])],
        "vm_type": req.vm_type, "priority": req.priority,
        "iso": req.iso, "peers": peers, "home": home,
    }

    async def _runner():
        loop = asyncio.get_event_loop()
        try:
            task.step_start(f"provision {req.vm_type}")
            result = await loop.run_in_executor(
                None, _run_vm_saga, "vm_create", saga_params)
            task.step_done(f"provision {req.vm_type}")
            task.log(f"created: {result}")
            task.succeed()
            # NOTE: the saga's register_vm step is the authoritative vms-row
            # writer (state='running' + failover_order). We deliberately do
            # NOT call _bs.vm_created here — it would reset state back to
            # 'created' on the just-started VM (ON CONFLICT … state='created').
        except HTTPException as e:
            task.fail(f"{e.status_code}: {e.detail}")
            _log_create_failed(req.name, f"{e.status_code}: {e.detail}")
        except Exception as e:
            task.fail(str(e))
            _log_create_failed(req.name, str(e))

    asyncio.create_task(_runner())
    return {"status": "accepted", "task_id": task.id, "name": req.name,
            "intent_revision": intent_idx}


def _log_create_failed(vm_name: str, reason: str) -> None:
    """Settle a vm_create_intent with vm_create_failed when the async
    creator throws. Best-effort — logging shouldn't mask the original
    failure path."""
    try:
        _bs.vm_create_failed(name=vm_name, reason=reason)
    except Exception as e:
        log.warning(f"vm_create_failed write skipped: {e}")


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
            result = await loop.run_in_executor(
                None, _run_vm_saga, "vm_destroy", {"vm_name": vm_name})
            # Drop the dashboard inventory breadcrumb the saga doesn't know about.
            try:
                inv = load_inventory()
                if inv.pop(vm_name, None) is not None:
                    save_inventory(inv)
            except Exception as e:
                log.warning(f"vm_delete: inventory cleanup skipped: {e}")
            task.log(f"deleted: {result}")
            task.succeed()
        except HTTPException as e:
            task.fail(f"{e.status_code}: {e.detail}")
        except Exception as e:
            task.fail(str(e))

    asyncio.create_task(_runner())
    return {"status": "accepted", "task_id": task.id, "name": vm_name}


# ── VM settings (vcpus, ram, disk, priority, cdrom) ─────────────────────────

class ComputeRequest(BaseModel):
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


@app.post("/api/vms/{vm_name}/compute")
def api_vm_compute(vm_name: str, req: ComputeRequest):
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
    vg = _vm_disk_vg(host)
    lv_path = f"/dev/{vg}/{lv_name}"

    _ensure_thinpool(host, vg_name=vg)
    push_log(f"Attach disk to {vm_name}: lvcreate {req.size_gb}G ({lv_name}) "
             f"in VG {vg}",
             node=host_name, app="bedrock-mgmt")
    out, rc = ssh_cmd_rc(host,
        f"lvcreate -y -V {req.size_gb}G --thin -n {lv_name} {vg}/thinpool "
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
    """Live-migrate via the vm_migrate saga (the single migrate path).
    The saga resolves source/target/resources from rqlite, cycles
    dual-primary across every disk, records the post-promote UUID on the
    new primary (so HA survives the move — VM-02), and keeps the domain
    defined on the source for failback."""
    target = req.target_node
    if not target:
        # No explicit target → pick the VM's backup peer.
        vm = build_cluster_state()["vms"].get(vm_name)
        if not vm:
            raise HTTPException(404, f"Unknown VM: {vm_name}")
        target = vm.get("backup_node")
        if not target:
            raise HTTPException(400, "no target node and no backup peer to pick")
    return _run_vm_saga("vm_migrate",
                        {"vm_name": vm_name, "target": target})


# ── Backup endpoints ────────────────────────────────────────────────────────
# Kopia orchestration. The mgmt master writes the backup target to rqlite;
# every node's reactor reacts by running `kopia repository connect` locally
# so any node can do backups/restores of its locally-resident VMs.
# See snapshots-and-backup.md §9c-bis.

class BackupTargetSetRequest(BaseModel):
    target_id: str = "main"
    kind: str = "kopia-s3"           # "kopia-s3" | "kopia-fs"
    s3_endpoint: str = ""
    s3_bucket: str = ""
    s3_region: str = ""
    # Self-hosted S3 (QNAP, MinIO with self-signed certs) often needs
    # one of these. Default off — operator opts in per target.
    s3_disable_tls: bool = False              # plain HTTP
    s3_disable_tls_verification: bool = False  # HTTPS, skip cert check
    filesystem_path: str = ""
    override_source_prefix: str = ""  # default: "<cluster_uuid>:vms"
    cache_directory: str = ""         # default: /var/cache/bedrock-kopia
    reason: str = ""
    # ── Multi-target replication ───────────────────────────────────
    # Ordered list of OTHER backup_targets ids this target mirrors to via
    # `kopia repository sync-to` after each backup. Secondaries are normal
    # targets (own endpoint/bucket/creds) sharing the one cluster password.
    # Empty = single-target (the default; no behaviour change).
    sync_to: list[str] = []
    delete_orphans: bool = False      # kopia sync-to --delete (prune mirrors)
    # A mirror target is a sync-to DESTINATION only — registered for its
    # storage config + creds but NEVER independently created (an independent
    # `kopia repository create` gives it an incompatible format block). It
    # starts empty; the first sync-to from its primary copies the source
    # format. Set this when adding a replication destination.
    is_mirror: bool = False
    # ── Credentials (NEVER logged) ─────────────────────────────────
    # Optional inline secrets. When present, mgmt writes the
    # corresponding files on every cluster node before recording the
    # backup target in rqlite. When absent, the operator is expected
    # to have dropped the files manually.
    s3_access_key: Optional[str] = None    # → KOPIA_S3_ACCESS_KEY in env file
    s3_secret_key: Optional[str] = None    # → KOPIA_S3_SECRET_KEY in env file
    encryption_password: Optional[str] = None  # → /etc/bedrock/backup.key
    # If True, overwrite /etc/bedrock/backup.key even if it already
    # exists. Defaults to False — changing the password makes existing
    # backups unreadable, so this is a deliberate destructive action.
    force_password_overwrite: bool = False


class BackupRunRequest(BaseModel):
    target_id: str = "main"
    label: str = ""                   # operator-visible tag


class RestoreRequest(BaseModel):
    target_id: str = "main"
    # Empty → restore the VM's NEWEST recorded backup. run_restore_to_ha
    # resolves vms[<name>].backups[0] when this is blank, so the common
    # "restore latest" case needs no snapshot id from the operator.
    kopia_snapshot_id: str = ""
    dest_node: Optional[str] = None
    target_lv_path: Optional[str] = None


class BackupDeleteRequest(BaseModel):
    target_id: str = "main"
    reason: str = ""


def _import_backup_module():
    """Lazy-import mgmt/backup.py — keeps app.py importable when the
    module is missing (e.g. during partial install) and matches the
    lazy-import pattern used elsewhere for lib modules."""
    import backup as _b
    return _b


# ── Backup secret propagation ──────────────────────────────────────────────
#
# Two secrets need to live on every node, mode 0600, never in cluster state:
#   - /etc/bedrock/backup.key                    (kopia repo password)
#   - /etc/bedrock/backup-credentials/<id>.env   (S3 access/secret keys)
#
# The dashboard collects them once on the master, then mgmt fans them
# out via the existing root@host SSH mesh that agent_install set up.
# Failure to propagate to one node is logged (push_log) but doesn't
# abort the target-set; the affected node will fail loudly the first
# time its reactor tries to `kopia repository connect`. The operator
# can re-trigger propagation by submitting the same form again.

BACKUP_KEY_FILE = "/etc/bedrock/backup.key"
BACKUP_CRED_DIR = "/etc/bedrock/backup-credentials"


def _write_local_secret(path: str, content: str, mode: int = 0o600):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.parent.chmod(0o700)
    p.write_text(content)
    p.chmod(mode)


def _write_remote_secret(host: str, path: str, content: str,
                         mode: int = 0o600, timeout: int = 15):
    """Push a secret file via paramiko SFTP. Atomic-replace via tmp +
    POSIX rename. Caller passes already-rendered content (env file or
    raw key).

    Why posix_rename: plain `sftp.rename()` maps to SSH_FXP_RENAME
    which (per the SFTP spec) refuses to overwrite an existing target.
    `posix_rename` maps to OpenSSH's `posix-rename@openssh.com`
    extension and behaves like POSIX `rename(2)` — atomic replace.
    Without this, every secret update past the first one fails with a
    nondescript "Failure" from the server."""
    c = _ssh_connect(host)
    try:
        parent = str(Path(path).parent)
        # exec_command is async-fire-and-forget; wait for the channel
        # to close so the directory definitely exists before SFTP open.
        _, so, _ = c.exec_command(f"mkdir -p -m 700 {parent}", timeout=timeout)
        so.channel.recv_exit_status()

        sftp = c.open_sftp()
        tmp = f"{path}.tmp.bedrock"
        with sftp.open(tmp, "wb") as f:
            f.write(content.encode())
        sftp.chmod(tmp, mode)
        sftp.posix_rename(tmp, path)
        sftp.close()
    finally:
        c.close()


def _propagate_secret(rel_path: str, content: str, mode: int = 0o600):
    """Write a secret to `rel_path` on every node (including this one).
    Returns (ok_nodes, failed_nodes) so the caller can surface partial
    failure to the UI."""
    ok: list[str] = []
    failed: list[tuple[str, str]] = []  # (node_name, reason)
    self_host = _self_host()
    for name, node in get_nodes().items():
        host = node.get("host")
        if not host:
            continue
        try:
            if host == self_host:
                _write_local_secret(rel_path, content, mode)
            else:
                _write_remote_secret(host, rel_path, content, mode)
            ok.append(name)
        except Exception as e:
            failed.append((name, str(e)))
            push_log(f"backup: propagate {rel_path} → {name} ({host}) "
                     f"failed: {e}",
                     node="mgmt", app="bedrock-mgmt", level="warn")
    return ok, failed


def _self_host() -> str:
    """Best-effort detection of this node's IP/hostname so we don't
    SSH-loop to ourselves (some sshd configs reject that)."""
    try:
        from lib import state as _state
        s = _state.load() if hasattr(_state, "load") else {}
        nodes = get_nodes()
        n = nodes.get(s.get("node_name", ""))
        if n and n.get("host"):
            return n["host"]
    except Exception:
        pass
    return ""


def _render_s3_creds_env(access_key: str, secret_key: str) -> str:
    """Bash-sourceable env file. Variable names match what kopia's S3
    backend reads from the environment (KOPIA_S3_ACCESS_KEY, etc.)."""
    import shlex as _sh
    return (
        "# bedrock-managed; do not edit by hand. mode 0600.\n"
        f"export KOPIA_S3_ACCESS_KEY={_sh.quote(access_key)}\n"
        f"export KOPIA_S3_SECRET_KEY={_sh.quote(secret_key)}\n"
        # AWS_* mirrors so other tools that read the file work too
        f"export AWS_ACCESS_KEY_ID={_sh.quote(access_key)}\n"
        f"export AWS_SECRET_ACCESS_KEY={_sh.quote(secret_key)}\n"
    )


@app.get("/api/backup/credentials/status")
def api_backup_credentials_status():
    """What secrets exist on each node? UI uses this to decide whether
    to show empty fields (operator must enter creds) vs. "already
    configured" placeholders.

    Returns per-node booleans:
      - has_password: /etc/bedrock/backup.key exists, mode 0600
      - has_creds.<target_id>: corresponding env file exists
    """
    out: dict = {"nodes": {}}
    for name, node in get_nodes().items():
        host = node.get("host", "")
        if not host:
            continue
        info: dict = {"has_password": False, "creds": {}}
        try:
            r = ssh_cmd(host, f"[ -f {BACKUP_KEY_FILE} ] && echo yes || echo no")
            info["has_password"] = (r.strip() == "yes")
            r2 = ssh_cmd(host, f"ls {BACKUP_CRED_DIR}/*.env 2>/dev/null | xargs -n1 basename 2>/dev/null")
            for ln in (r2 or "").splitlines():
                ln = ln.strip()
                if ln.endswith(".env"):
                    info["creds"][ln[:-4]] = True
        except Exception as e:
            info["error"] = str(e)
        out["nodes"][name] = info
    return out


@app.post("/api/backup/targets")
def api_backup_target_set(req: BackupTargetSetRequest):
    """Configure (or update) the cluster's backup target. Idempotent —
    emitting the same target twice produces a single fold result.

    Action sequence:
      1. (Optional) Propagate inline credentials to every node:
           - encryption_password → /etc/bedrock/backup.key
           - s3_access_key/secret → /etc/bedrock/backup-credentials/<id>.env
         Files are written mode 0600. Failure on individual nodes is
         logged but doesn't abort — the affected node will fail loudly
         on its reactor's `kopia repository connect`.
      2. Run `kopia repository connect` (or create) locally on master.
         Verifies the repo's block hash is ≥256 bits.
      3. Write the backup target to rqlite. Every node's reactor reacts
         by running `kopia repository connect` against the new target.
      4. Return the revision so callers know the change is committed.

    Credentials are NEVER persisted to cluster state — only file paths
    and metadata (endpoint, bucket, region) are stored."""
    backup = _import_backup_module()

    propagation_warnings: list[str] = []

    # ── (0) Validate the mirror set UP FRONT, before any writes ──────
    # A bad sync_to must 400 with NO partial commit (no kopia repo created, no
    # target row written). STRONG read so a sibling target created moments ago
    # is visible (a level='none' local replica can lag and falsely reject a
    # valid secondary). Each secondary must EXIST and be is_mirror=true — a
    # non-mirror (independently-created) repo has an incompatible format block
    # and every sync-to into it would fail "incompatible data" forever.
    sync_to = list(req.sync_to or [])
    if req.target_id in sync_to:
        raise HTTPException(
            400, f"a backup target cannot mirror to itself ({req.target_id!r})")
    try:
        strong_targets = _cluster_state.load_cluster(
            level="strong").get("backup_targets", {}) or {}
    except Exception as e:
        raise HTTPException(
            503, f"could not validate sync_to — rqlite strong read failed "
            f"(no leader?): {e}")
    for sid in sync_to:
        t = strong_targets.get(sid)
        if t is None:
            raise HTTPException(
                400, f"sync_to references unknown backup target {sid!r} — "
                f"create it as a mirror (is_mirror=true) first")
        if not t.get("is_mirror"):
            raise HTTPException(
                400, f"sync_to secondary {sid!r} is not a mirror target. Create "
                f"the mirror destination with is_mirror=true — a mirror is never "
                f"independently initialized; the first sync-to copies the "
                f"primary's repo format into it.")
        # A mirror must belong to exactly ONE primary. Two primaries syncing to
        # the same mirror push incompatible repo formats (every sync after the
        # first fails "incompatible data") and, with delete_orphans, their
        # --delete passes would prune each other's blobs (data loss). Reject a
        # secondary already owned by a different primary.
        other = next((pid for pid, pt in strong_targets.items()
                      if pid != req.target_id
                      and sid in (pt.get("sync_to") or [])), None)
        if other is not None:
            raise HTTPException(
                400, f"mirror {sid!r} is already a replication target of "
                f"{other!r}. A mirror can belong to only one primary "
                f"(two primaries would push incompatible formats and "
                f"--delete-prune each other). Use a separate mirror target.")
    # Existing mirror set for this primary (strong, so a clear isn't skipped
    # against a stale replica).
    current_mirrors = (strong_targets.get(req.target_id) or {}).get("sync_to") or []

    # ── (1a) Encryption password ──────────────────────────────────
    if req.encryption_password is not None:
        already_have_local_key = Path(BACKUP_KEY_FILE).exists()
        if already_have_local_key and not req.force_password_overwrite:
            raise HTTPException(
                400,
                "encryption_password supplied but /etc/bedrock/backup.key "
                "already exists. Changing the password makes existing "
                "backups unreadable. Pass force_password_overwrite=true "
                "to confirm — or omit encryption_password to keep the "
                "current key."
            )
        ok, failed = _propagate_secret(
            BACKUP_KEY_FILE, req.encryption_password, mode=0o600
        )
        if failed:
            propagation_warnings.append(
                f"backup.key not deployed to: "
                + ", ".join(f"{n}({e})" for n, e in failed)
            )

    # ── (1b) S3 credentials ────────────────────────────────────────
    if req.kind == "kopia-s3" and (req.s3_access_key or req.s3_secret_key):
        if not (req.s3_access_key and req.s3_secret_key):
            raise HTTPException(
                400, "s3_access_key and s3_secret_key must be supplied together"
            )
        env_path = f"{BACKUP_CRED_DIR}/{req.target_id}.env"
        ok, failed = _propagate_secret(
            env_path,
            _render_s3_creds_env(req.s3_access_key, req.s3_secret_key),
            mode=0o600,
        )
        if failed:
            propagation_warnings.append(
                f"S3 credentials not deployed to: "
                + ", ".join(f"{n}({e})" for n, e in failed)
            )

    # ── (2) Connect this node + verify hash floor ──────────────────
    # SKIP for a mirror target: it must stay empty so the first
    # `kopia repository sync-to` can copy the source's format block into it.
    # Independently creating it here would give it an incompatible format
    # and every sync-to would fail "destination contains incompatible data".
    if not req.is_mirror:
        try:
            backup.configure_target_locally(
                target_id=req.target_id, kind=req.kind,
                s3_endpoint=req.s3_endpoint, s3_bucket=req.s3_bucket,
                s3_region=req.s3_region,
                s3_disable_tls=req.s3_disable_tls,
                s3_disable_tls_verification=req.s3_disable_tls_verification,
                filesystem_path=req.filesystem_path,
                override_source_prefix=req.override_source_prefix,
                cache_directory=req.cache_directory,
            )
        except Exception as e:
            raise HTTPException(400, f"backup target setup failed locally: {e}")

    # ── (3) Persist to rqlite so peers get it via their reactors ──
    try:
        rev = _bs.backup_target_set(
            target_id=req.target_id, kind=req.kind,
            s3_endpoint=req.s3_endpoint, s3_bucket=req.s3_bucket,
            s3_region=req.s3_region,
            s3_disable_tls=req.s3_disable_tls,
            s3_disable_tls_verification=req.s3_disable_tls_verification,
            filesystem_path=req.filesystem_path,
            override_source_prefix=req.override_source_prefix,
            cache_directory=req.cache_directory,
            is_mirror=req.is_mirror,
            reason=req.reason,
        )
    except Exception as e:
        raise HTTPException(500, f"rqlite write failed: {e}")

    # ── (3b) Persist the mirror set (already validated in step 0). Write only
    # when setting OR clearing, so single-target sets don't churn the table.
    if sync_to or current_mirrors:
        try:
            rev = _bs.backup_target_sync_set(
                req.target_id, sync_to,
                delete_orphans=req.delete_orphans, reason=req.reason,
            )
        except Exception as e:
            raise HTTPException(500, f"rqlite write (mirror set) failed: {e}")

    push_log(f"backup target {req.target_id!r} set ({req.kind})"
             + (f" → mirrors {sync_to}" if sync_to else ""),
             app="bedrock-mgmt", level="info")
    return {
        "status": "ok",
        "revision": rev,
        "target_id": req.target_id,
        "sync_to": list(req.sync_to or []),
        "warnings": propagation_warnings,
    }


@app.get("/api/backup/targets")
def api_backup_targets_list():
    """List configured backup targets, drawn from cluster state.
    Always returns immediately — no kopia roundtrip."""
    cluster = load_cluster()
    return {"targets": cluster.get("backup_targets", {})}


@app.get("/api/backups")
def api_backups_list_all():
    """Cluster-wide backup history. Walks every VM in cluster state
    and flattens its `backups` list, decorating each row with the
    owning vm name + whether the source VM still exists. Used by
    the dashboard's Backups page to render a single restore-able
    list across the whole cluster.

    Sorted newest-first by ts_index (monotonic timestamp).
    vm_present=False rows are kept so operators can still restore a
    deleted VM's snapshots into a fresh LV."""
    cluster = load_cluster()
    vms = cluster.get("vms", {}) or {}
    out = []
    for vm_name, vm in vms.items():
        for b in (vm.get("backups") or []):
            row = dict(b)
            row["vm"] = vm_name
            row["vm_present"] = True
            out.append(row)
    # Snapshots whose source VM was deleted: cluster state only retains
    # backup entries on live VM records, so there are no orphan rows to
    # surface here. (Listing orphans from the repo via
    # `kopia snapshot list` is a possible future addition.)
    out.sort(key=lambda r: r.get("ts_index", 0), reverse=True)
    return {"backups": out}


@app.delete("/api/backup/targets/{target_id}")
def api_backup_target_remove(target_id: str, reason: str = ""):
    try:
        rev = _bs.backup_target_removed(
            target_id=target_id, reason=reason or "operator-remove",
        )
    except Exception as e:
        raise HTTPException(500, f"rqlite write failed: {e}")
    return {"status": "ok", "revision": rev, "target_id": target_id}


# ── Witness management ──────────────────────────────────────────────
# Add / list / remove cluster witnesses for the weighted-vote quorum
# (each valid witness = 1 vote; nodes = 100). Writes the rqlite
# `witnesses` table — Raft replicates it, and EVERY node's netd 1 Hz
# election tick reloads the list automatically, so no explicit daemon
# propagation is needed from mgmt (unlike the CLI path). The operator
# dashboard drives these.

class WitnessAddRequest(BaseModel):
    witness_id: str
    addr: str = ""             # "host" or "host:port" (echo default 12321)
    witness_pubkey: str = ""   # X25519 pubkey hex (64 chars) — required for echo
    backend: str = "echo"      # "echo" | "smb" | "s3"
    reason: str = ""


@app.get("/api/witnesses")
def api_witnesses_list():
    return {"witnesses": load_cluster().get("witnesses", {})}


@app.post("/api/witnesses")
def api_witness_add(req: WitnessAddRequest):
    wid = (req.witness_id or "").strip()
    if not wid:
        raise HTTPException(400, "witness_id is required")
    backend = (req.backend or "echo").strip().lower()
    if backend not in ("echo", "smb", "s3"):
        raise HTTPException(400, f"unknown witness backend {backend!r} "
                                 f"(expected echo | smb | s3)")
    if backend != "echo":
        # smb/s3 fileshare witness backends are NOT implemented yet (no
        # transport exists — netd only speaks the Echo UDP protocol). A
        # configured-but-non-functional witness is strictly WORSE than no
        # witness: it raises the quorum bar by one vote (it's counted in
        # len(witnesses)) while it can never become valid+confirmed (0 votes),
        # which can brick failover on a 2-node cluster. Refuse until the
        # backend ships, rather than let an operator wedge their quorum.
        raise HTTPException(
            400, f"witness backend {backend!r} is not implemented yet — only "
            f"'echo' witnesses are active. A non-functional witness would "
            f"raise the quorum bar without ever voting, which can BLOCK "
            f"failover. (The fileshare/S3 witness backend is a future build.)")
    addr = (req.addr or "").strip()
    if not addr:
        raise HTTPException(400, "addr is required (ipv4 or ipv4:port)")
    # An Echo witness must be an IPv4 UNICAST literal: netd directed-probes it
    # from the single-threaded 1Hz election tick over an AF_INET socket, so a
    # hostname (synchronous getaddrinfo would stall failover detection), an
    # IPv6 literal (unreachable on AF_INET), or a multicast/broadcast/0.0.0.0
    # addr (would flood the segment) are all refused HERE — fail loud at add
    # time rather than register an unusable witness that silently raises the
    # quorum bar. host:port, default port 12321.
    import ipaddress as _ipaddr
    host, _, port_s = addr.partition(":") if ":" in addr else (addr, "", "")
    port = 12321
    if port_s:
        try:
            port = int(port_s)
        except ValueError:
            raise HTTPException(400, f"invalid port {port_s!r} in addr {addr!r}")
        if not (1 <= port <= 65535):
            raise HTTPException(400, f"port {port} out of range (1-65535)")
    try:
        ip = _ipaddr.ip_address(host)
    except ValueError:
        raise HTTPException(
            400, f"Echo witness address must be an IPv4 literal, not a "
            f"hostname ({host!r}). A hostname would block the election tick on "
            f"DNS. Add the Echo by its IP.")
    if (ip.version != 4 or ip.is_multicast or ip.is_unspecified
            or ip.is_reserved or ip.is_loopback or ip.is_link_local):
        raise HTTPException(
            400, f"Echo witness address {host!r} is not a usable IPv4 unicast "
            f"address (no multicast/broadcast/loopback/link-local/unspecified).")
    stored_addr = f"{host}:{port}"
    pubkey = (req.witness_pubkey or "").strip().lower()
    if backend == "echo":
        # An Echo's X25519 public key is 32 bytes = 64 hex chars. Validate
        # FAIL-LOUD: a bad paste would silently write a witness netd can never
        # authenticate against (it would just never count toward quorum).
        if len(pubkey) != 64 or any(c not in "0123456789abcdef" for c in pubkey):
            raise HTTPException(
                400, "witness_pubkey must be 64 hex chars (the Echo's X25519 "
                "public key) for an echo witness")
    try:
        rev = _bs.witness_register(witness_id=wid, addr=stored_addr,
                                   witness_pubkey_hex=pubkey,
                                   encrypted_witness_key_hex="",
                                   backend=backend)
    except Exception as e:
        raise HTTPException(500, f"rqlite write failed: {e}")
    push_log(f"witness {wid!r} added ({backend} {stored_addr})",
             app="bedrock-mgmt", level="info")
    return {"status": "ok", "revision": rev, "witness_id": wid,
            "addr": stored_addr, "backend": backend}


@app.delete("/api/witnesses/{witness_id}")
def api_witness_remove(witness_id: str, reason: str = ""):
    # 404 for a non-existent witness — witness_unregister's DELETE matches 0
    # rows but still "succeeds" and bumps the revision, so without this a
    # typo'd delete reports success and churns every node's reactor for nothing.
    if witness_id not in (load_cluster().get("witnesses") or {}):
        raise HTTPException(404, f"witness {witness_id!r} not found")
    try:
        rev = _bs.witness_unregister(witness_id)
    except Exception as e:
        raise HTTPException(500, f"rqlite write failed: {e}")
    push_log(f"witness {witness_id!r} removed", app="bedrock-mgmt", level="info")
    return {"status": "ok", "revision": rev, "witness_id": witness_id}


@app.get("/api/witnesses/discover")
def api_witnesses_discover():
    """Best-effort mDNS discovery of Bedrock services on the LAN — surfaced so
    the dashboard can offer reachable hosts for one-click witness add. The
    operator still supplies the Echo's pubkey. (Echo-specific service
    advertisement is a follow-up; today this finds Bedrock nodes/clusters.)"""
    try:
        from lib import discovery as _disc
        cands = _disc.discover_clusters(timeout=2.0)
    except Exception as e:
        raise HTTPException(500, f"discovery failed: {e}")
    return {"candidates": [
        {"ip": getattr(c, "ip", ""),
         "name": getattr(c, "cluster_name", "") or getattr(c, "name", ""),
         "node": getattr(c, "node_name", "")}
        for c in (cands or [])]}


@app.post("/api/vms/{vm_name}/backup")
async def api_vm_backup(vm_name: str, req: BackupRunRequest = BackupRunRequest()):
    """Take a backup of `vm_name` to `target_id`. Returns 202 + task_id;
    the UI watches /api/tasks (or the WS task channel) for completion.

    The backup runs on the VM's home node: take an LV snapshot, kopia
    snapshot create, drop the LV snapshot. Idempotent only at the
    log-entry level; multiple in-flight backups of the same VM are not
    serialised here — the operator is expected not to double-click."""
    cluster = load_cluster()
    vm = (cluster.get("vms") or {}).get(vm_name)
    if vm is None:
        raise HTTPException(404, f"VM {vm_name!r} not found")
    target = (cluster.get("backup_targets") or {}).get(req.target_id)
    if target is None:
        raise HTTPException(400, f"backup target {req.target_id!r} not configured")

    home = vm.get("host") or ""
    if not home:
        raise HTTPException(400, f"VM {vm_name!r} has no home node recorded")

    # Multi-target: resolve the primary's mirror set NOW (from cluster state)
    # and carry it in the saga params so the home node's sync_to_secondaries
    # step mirrors the backup. Putting it in params (vs re-reading on the home
    # node) makes it durable across resume + master failover.
    secondary_target_ids = list(target.get("sync_to") or [])

    # Submit a vm_backup saga targeted at the VM's HOME node. That node's
    # operations_drain (mgmt/orchestrator.py) runs it locally — kopia on
    # the node that owns the disks — recording the result to rqlite. No
    # SSH from here; rqlite's `operations` table is the channel.
    import socket as _socket
    from bedrock_d.orchestrator.sagas import SagaExecutor
    from bedrock_d.orchestrator.sagas.rqlite_backend import RqliteSagaBackend
    from bedrock_d import state as _bst
    from bedrock_d.vm import backup as _vmbk  # noqa: F401  registers vm_backup
    try:
        backend = RqliteSagaBackend(_bst.RqliteClient())
        ex = SagaExecutor(backend=backend, this_node=_socket.gethostname())
        op_id = ex.submit(
            kind="vm_backup", target_node=home,
            params={"target_id": req.target_id, "vm_name": vm_name,
                    "label": req.label or "",
                    "secondary_target_ids": secondary_target_ids},
            requested_by="api_vm_backup",
        )
    except Exception as e:
        raise HTTPException(500, f"could not queue backup: {e}")
    push_log(f"VM {vm_name}: backup queued → {req.target_id} "
             f"(op {op_id}, runs on {home})", level="info")
    return {"status": "accepted", "operation_id": op_id, "home_node": home}


@app.get("/api/vms/{vm_name}/backups")
def api_vm_backups_list(vm_name: str):
    """Backup history for a VM, drawn from cluster state. Newest first."""
    cluster = load_cluster()
    vm = (cluster.get("vms") or {}).get(vm_name)
    if vm is None:
        raise HTTPException(404, f"VM {vm_name!r} not found")
    return {
        "vm": vm_name,
        "backups": vm.get("backups") or [],
        "last_backup_error": vm.get("last_backup_error"),
        "last_restore": vm.get("last_restore"),
        "last_restore_error": vm.get("last_restore_error"),
    }


@app.post("/api/vms/{vm_name}/restore")
async def api_vm_restore(vm_name: str, req: RestoreRequest):
    """Restore a VM from a kopia backup and bring it back up HA. Submits
    a vm_restore saga targeted at the VM's home node; that node powers the
    VM off, restores each disk through its DRBD primary (so the bytes
    replicate to peers), and starts it again. Returns 202 + operation_id."""
    cluster = load_cluster()
    if (cluster.get("backup_targets") or {}).get(req.target_id) is None:
        raise HTTPException(400, f"backup target {req.target_id!r} not configured")
    vm = (cluster.get("vms") or {}).get(vm_name)
    if vm is None:
        raise HTTPException(404, f"VM {vm_name!r} not present in this cluster")
    home = vm.get("host") or ""
    if not home:
        raise HTTPException(400, f"VM {vm_name!r} has no home node recorded")

    import socket as _socket
    from bedrock_d.orchestrator.sagas import SagaExecutor
    from bedrock_d.orchestrator.sagas.rqlite_backend import RqliteSagaBackend
    from bedrock_d import state as _bst
    from bedrock_d.vm import backup as _vmbk  # noqa: F401  registers vm_restore
    try:
        backend = RqliteSagaBackend(_bst.RqliteClient())
        ex = SagaExecutor(backend=backend, this_node=_socket.gethostname())
        op_id = ex.submit(
            kind="vm_restore", target_node=home,
            params={"target_id": req.target_id, "vm_name": vm_name,
                    "kopia_snapshot_id": req.kopia_snapshot_id or ""},
            requested_by="api_vm_restore",
        )
    except Exception as e:
        raise HTTPException(500, f"could not queue restore: {e}")
    push_log(f"VM {vm_name}: restore queued from {req.target_id} "
             f"(op {op_id}, runs on {home})", level="info")
    return {"status": "accepted", "operation_id": op_id, "home_node": home}


class BackupScheduleSetRequest(BaseModel):
    target_id: str = "main"
    cron_expr: str               # 5-field UTC cron, e.g. "0 2 * * *" or "@daily"
    label_prefix: str = "auto"   # auto-generated labels start with "<prefix>-"
    retention_count: int = 0     # 0 = keep all (v1.0 default)
    reason: str = ""


@app.post("/api/vms/{vm_name}/backup-schedule")
def api_vm_backup_schedule_set(vm_name: str, req: BackupScheduleSetRequest):
    """Set or replace the periodic-backup schedule for a VM. The
    schedule is stored in the cluster log so it survives master
    failover; the master's `backup_scheduler` loop is the only firer.

    Returns the next 5 fire times (UTC) so the caller can sanity-check
    their cron expression before relying on it."""
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).parent))
    import cron as _cron

    cluster = load_cluster()
    if (cluster.get("vms") or {}).get(vm_name) is None:
        raise HTTPException(404, f"VM {vm_name!r} not found")
    if (cluster.get("backup_targets") or {}).get(req.target_id) is None:
        raise HTTPException(400, f"backup target {req.target_id!r} not configured")

    # Validate the cron expression server-side. Better to fail at submit
    # time than have the scheduler silently skip the VM forever.
    try:
        next_fires = _cron.next_n(req.cron_expr, n=5)
    except _cron.CronError as e:
        raise HTTPException(400, f"invalid cron expression: {e}")

    try:
        rev = _bs.backup_schedule_set(
            vm=vm_name, target_id=req.target_id,
            cron_expr=req.cron_expr,
            label_prefix=req.label_prefix,
            retention_count=req.retention_count,
            reason=req.reason or "set via dashboard",
        )
    except Exception as e:
        raise HTTPException(500, f"rqlite write failed: {e}")

    push_log(f"backup schedule set for VM {vm_name}: cron={req.cron_expr!r} "
             f"target={req.target_id}",
             app="bedrock-mgmt", level="info")
    return {
        "status": "ok",
        "revision": rev,
        "vm": vm_name,
        "cron_expr": req.cron_expr,
        "next_fires_utc": next_fires,
    }


@app.delete("/api/vms/{vm_name}/backup-schedule")
def api_vm_backup_schedule_remove(vm_name: str, reason: str = ""):
    try:
        rev = _bs.backup_schedule_removed(
            vm=vm_name, reason=reason or "removed via dashboard",
        )
    except Exception as e:
        raise HTTPException(500, f"rqlite write failed: {e}")
    return {"status": "ok", "revision": rev, "vm": vm_name}


@app.get("/api/cron/preview")
def api_cron_preview(expr: str, n: int = 5):
    """Return the next N fire times for a cron expression (UTC ISO).
    Used by the dashboard's schedule-input field for live preview as
    the operator types. Pure parser — no I/O."""
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).parent))
    import cron as _cron
    try:
        return {"cron_expr": expr, "next_fires_utc": _cron.next_n(expr, n=max(1, min(n, 20)))}
    except _cron.CronError as e:
        raise HTTPException(400, f"invalid cron expression: {e}")


@app.delete("/api/vms/{vm_name}/backups/{kopia_snapshot_id}")
def api_vm_backup_delete(vm_name: str, kopia_snapshot_id: str,
                         req: BackupDeleteRequest = BackupDeleteRequest()):
    """Delete one snapshot from the kopia repo. Returns synchronously —
    delete is fast (just drops a manifest). GC of the underlying chunks
    happens during the next `kopia maintenance run` on the master."""
    cluster = load_cluster()
    if (cluster.get("backup_targets") or {}).get(req.target_id) is None:
        raise HTTPException(400, f"backup target {req.target_id!r} not configured")
    backup = _import_backup_module()
    try:
        backup.delete_backup(req.target_id, kopia_snapshot_id, vm_name,
                             reason=req.reason or "operator-delete")
    except Exception as e:
        raise HTTPException(500, f"delete failed: {e}")
    return {"status": "ok", "kopia_snapshot_id": kopia_snapshot_id}


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


# ── Workload conversion (cattle ↔ pet ↔ vipet) ──────────────────────────────

def _vm_disk_vg(host: str) -> str:
    """Return the LVM VG bedrock uses on `host` for thin LVs (tiers,
    VM disks, all the dynamic ones). Reads /etc/bedrock/storage.json
    which `tier_storage.ensure_vg()` writes at bootstrap time. Falls
    back to detecting the only VG present, then to the literal name
    `bedrock`. No loop-file fallback: bedrock-bootstrap is expected to
    have set up the layout. If it hasn't, downstream lvcreate calls fail
    loudly — the right reaction is to fix the install, not to silently
    put VM data on a sparse file on `/`."""
    try:
        out = ssh_cmd(host,
            "cat /etc/bedrock/storage.json 2>/dev/null", timeout=8)
        if out.strip():
            import json as _json
            cfg = _json.loads(out)
            if cfg.get("vg"):
                return cfg["vg"]
    except Exception:
        pass
    try:
        vgs = ssh_cmd(host,
            "vgs --noheadings -o vg_name 2>/dev/null", timeout=10).split()
        vgs = [v.strip() for v in vgs if v.strip()]
        if len(vgs) == 1:
            return vgs[0]
        # Prefer `bedrock-vg`, then `bedrock` (mirrors
        # tier_storage.detect_vg's multi-VG heuristic).
        if "bedrock-vg" in vgs:
            return "bedrock-vg"
        if "bedrock" in vgs:
            return "bedrock"
    except Exception:
        pass
    return "bedrock"


def _ensure_thinpool(host: str, vg_name: Optional[str] = None, pool: str = "thinpool"):
    """Verify the thin pool exists on `host`. Creating it is the
    responsibility of `bedrock bootstrap` (tier_storage.ensure_thinpool),
    NOT this runtime helper — runtime is the wrong moment to make
    architectural-level storage decisions. If the pool isn't there,
    raise so the operator gets a clear failure pointing at the missing
    install step."""
    if vg_name is None:
        vg_name = _vm_disk_vg(host)
    out = ssh_cmd(host, f"lvs --noheadings -o lv_name {vg_name} 2>/dev/null || true")
    if pool in out.split():
        return
    raise HTTPException(
        500,
        f"thin pool {vg_name}/{pool} does not exist on {host}. "
        f"Run `bedrock storage init` (or re-run `bedrock bootstrap`) on that "
        f"node before creating VMs. Bedrock no longer auto-creates loop-backed "
        f"thin pools at runtime — that path put VM I/O on `/` and filled the "
        f"root filesystem during multi-GB installs.")


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
    """Pick + atomically reserve an unused minor in the VM band
    (1102..1189) across all hosts. The band keeps every VM-disk DRBD
    port inside 7700-7799 (drbd_port_for) and clear of the singleton
    minor (1101) + the netd mesh minors (1132/1133/1134 → UDP probe
    7732, advert 7733, election heartbeat 7734=netd.HB_PORT). The
    reservation lives until `_release_drbd_minor` is called (after the
    resource is fully up, or on rollback)."""
    reserved_minors = {1132, 1133, 1134}
    with _drbd_minor_lock:
        used = set(_drbd_minor_reserved)
        for h in hosts:
            out = ssh_cmd(h, "ls /dev/drbd* 2>/dev/null | grep -oE '[0-9]+$' || true")
            for n in out.split():
                try: used.add(int(n))
                except ValueError: pass
        for i in range(1102, 1190):
            if i not in used and i not in reserved_minors:
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
    """peers: list of (node_name, loopback_ip, lv_path, meta_lv_path). 2 or 3 entries.
    External meta-disk keeps the DRBD device the same size as the data LV,
    so virsh blockcopy can pivot 1:1 without size mismatch.
    """
    # Shared DRBD port formula (7700-7799 band) — same mapping the
    # cluster singleton + the VM sagas use. See drbd_config.drbd_port_for.
    import sys as _sys
    _sys.path.insert(0, "/usr/local/lib/bedrock")
    from bedrock_d.vm import drbd_config as _cfg
    port = _cfg.drbd_port_for(minor)
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


def _vm_set_ha_level(vm_name: str, vm_type: str, peer_nodes=None,
                task: Optional[Task] = None) -> dict:
    if vm_type not in ("cattle", "pet", "vipet"):
        raise HTTPException(400, f"Invalid vm_type: {vm_type}")

    state = build_cluster_state()
    vm = state["vms"].get(vm_name)
    if not vm: raise HTTPException(404, f"VM {vm_name} not found")

    nodes_cfg = get_nodes()
    # Running → use running_on; offline → first defined_on node.
    src_name = (vm.get("running_on")
                or (vm.get("defined_on") or [None])[0])
    if not src_name:
        raise HTTPException(400,
            f"Cannot resolve home node for {vm_name}")
    is_running = (vm.get("state") == "running")
    src = nodes_cfg[src_name]
    current_type = "vipet" if vm.get("drbd_resource") and _count_drbd_peers(src["host"], vm["drbd_resource"]) >= 3 \
                   else ("pet" if vm.get("drbd_resource") else "cattle")

    if current_type == vm_type:
        return {"status": "no-op", "current": current_type}

    rank = {"cattle": 0, "pet": 1, "vipet": 2}
    if rank[vm_type] > rank[current_type]:
        return _vm_set_ha_level_up(vm_name, current_type, vm_type, src_name,
                                   peer_nodes, task, is_running=is_running)
    else:
        return _vm_set_ha_level_down(vm_name, current_type, vm_type, src_name,
                                     peer_nodes, task)


def _count_drbd_peers(host: str, resource: str) -> int:
    try:
        out = ssh_cmd(host, f"drbdsetup status {resource} --json 2>/dev/null || echo '[]'")
        import json as _json
        data = _json.loads(out)
        if isinstance(data, list) and data:
            return 1 + len(data[0].get("connections", []))
    except Exception: pass
    return 0


def _vm_set_ha_level_up(vm_name: str, cur: str, tgt: str, src_name: str,
                         peer_nodes, task: Optional[Task] = None,
                         is_running: bool = True) -> dict:
    """Cattle → pet / cattle → ViPet / pet → ViPet.

    Iterates over every disk the VM has, so multi-disk guests become
    pet/ViPet across ALL their disks. Atomic: if any disk fails mid-way,
    rollback unwinds the changes already made to earlier disks.

    Two execution paths:

      - **online** (`is_running=True`): use `virsh blockcopy ...
        --pivot` to swap qemu's disk reference from the local LV to
        /dev/drbdN with no guest pause. Required for "convert this
        VM to HA without downtime" — the operator never reboots.

      - **offline** (`is_running=False`): the VM is shut off, so no
        qemu is holding the LV. Skip blockcopy; directly rewrite the
        persistent libvirt XML to point at /dev/drbdN, redefine on
        all peers. DRBD does its own initial sync from primary
        (this side, marked `--force`) to the peer's empty LV in the
        background. Faster, no live-migration risk, and matches the
        operator expectation that "convert" should also work for VMs
        that haven't been booted yet."""
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

        # No artificial pool-fill guard here. Convert is sometimes
        # exactly the operation an operator runs to free up space
        # (move a VM off a tight node), so refusing it on a soft
        # threshold would block a legitimate use case. The thin pool
        # itself is the safety net — lvcreate fails loudly if there
        # really isn't room. The supportability dashboard surfaces
        # the 80% warning where it belongs (advisory monitoring),
        # not as a write-block.

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
                # Meta LV name must be unique per resource; the `<lv>-meta`
                # suffix is the convention _parse_drbd_res expects.
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
                peers_info = [(src_name, src.get("loopback_ip") or src["host"],
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
                    peers_info.append((pname, p.get("loopback_ip") or p["host"],
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

                if is_running:
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
                else:
                    # Offline path: rewrite this disk's <source dev='…'> in
                    # the persistent XML on the source. DRBD's local side is
                    # already primary --force on the live data LV, so no
                    # data copy is needed locally — DRBD's initial-sync from
                    # primary streams to peers in the background. The VM,
                    # when restarted, opens /dev/drbdN and reads the same
                    # bytes through the replication layer.
                    if task: task.step_start(f"{step_prefix}: rewrite XML offline")
                    xml_text = ssh_cmd(src["host"],
                        f"virsh dumpxml --inactive {vm_name}", timeout=15)
                    needle = f"source dev='{src_lv}'"
                    if needle not in xml_text:
                        # Try double-quoted variant (libvirt may emit either)
                        needle_dq = f'source dev="{src_lv}"'
                        if needle_dq not in xml_text:
                            raise HTTPException(500,
                                f"could not find {src_lv!r} in {vm_name}'s "
                                f"persistent XML — XML schema unexpected")
                        new_xml = xml_text.replace(
                            needle_dq, f'source dev="/dev/drbd{minor}"')
                    else:
                        new_xml = xml_text.replace(
                            needle, f"source dev='/dev/drbd{minor}'")
                    import base64 as _b64
                    xml_b64 = _b64.b64encode(new_xml.encode()).decode()
                    ssh_cmd(src["host"],
                        f"echo {xml_b64} | base64 -d > /tmp/{vm_name}.xml && "
                        f"virsh define /tmp/{vm_name}.xml >/dev/null", timeout=15)
                    if task: task.step_done(f"{step_prefix}: rewrite XML offline")

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
            # Meta LV sized to match the other peers — see _vm_set_ha_level_up
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

            peers_info = [(n, nodes_cfg[n].get("loopback_ip") or nodes_cfg[n]["host"],
                           existing["lv_path"], existing["meta_path"])
                          for n in existing["peers"]]
            peers_info.append((new_peer, p.get("loopback_ip") or p["host"],
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


def _vm_set_ha_level_down(vm_name: str, cur: str, tgt: str, src_name: str,
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

            remaining = [(n, nodes_cfg[n].get("loopback_ip") or nodes_cfg[n]["host"],
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
ISO_MOUNT_DIR = "/mnt/bedrock/iso"  # identical on every cluster node (SeaweedFS FUSE)


def _mgmt_node_name() -> str:
    """Return the node name of the mgmt host (where ISOs live)."""
    cfg = get_nodes()
    for name, node in cfg.items():
        if "mgmt" in node.get("role", ""):
            return name
    # Fallback: first node
    return next(iter(cfg)) if cfg else ""


def _vm_create_from_import(meta: dict, req, task: Optional[Task] = None) -> dict:
    """Turn a converted import (qcow2 on mgmt node) into a cattle VM.
    Creates a thin LV sized to the qcow2 virtual size, qemu-img converts the
    qcow2 into the LV (raw), then virt-installs with machine=q35, UEFI
    firmware, clock=UTC. Marks the import meta as consumed.

    Not routed through the bedrock_d vm_create saga (unlike POST /api/vms,
    which uses _run_vm_saga). The saga's image-fill step only knows how to
    write the cached Alpine image or boot an ISO — it has no "import a
    pre-existing disk image" mode, and import additionally needs source-disk
    firmware sniffing (BIOS vs UEFI), Windows Hyper-V enlightenments, and
    import-meta consumption that the saga doesn't model. Import is also
    cattle-only (single local LV, no DRBD), so there is no post-promote DRBD
    UUID to record (INV-5 only applies to replicated pet/vipet disks).

    Per-resource naming: this path uses vm-<name>-disk<N> LV names, matching
    the rest of the mgmt VM layer (attach-disk at _next_drbd_minor /
    api_vm_attach_disk, _vm_get_settings, mgmt-side destroy) so disk ops on an
    imported VM stay consistent."""
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

    vg = _vm_disk_vg(host)
    _ensure_thinpool(host, vg_name=vg)

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
        f"lvs --noheadings --units b --nosuffix --separator '|' "
        f"-o lv_size,data_percent {vg}/thinpool 2>/dev/null | head -1",
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
    # Resolved VG (never hardcode 'almalinux').
    vg = _vm_disk_vg(host)
    disks_plan = []
    for sd in src_disks:
        vgb = sd["virtual_size_gb"] or 1
        ln = f"vm-{req.name}-disk{sd['index']}"
        disks_plan.append({
            "index": sd["index"],
            "lv_name": ln,
            "lv_path": f"/dev/{vg}/{ln}",
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
            f"{vg}/thinpool 2>&1", timeout=60)
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
        "disk_gb": disks_plan[0]["size_gb"],   # primary disk size
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
    # Mirror to rqlite so the cluster-wide self-heal repair loop orders
    # replica restoration by the operator's current choice (SG-05).
    try:
        _bs.vm_set_priority(name=vm_name, priority=priority)
    except Exception as e:
        log.warning(f"vm priority rqlite-mirror skipped: {e}")
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

# Metrics + logs read endpoints live in mgmt/routes_obs.py.
from routes_obs import register_routes as _register_obs_routes
_register_obs_routes(app)

# Generic saga submission API — POST /api/operations + the read-side.
# This is the surface the CLI (and any external automation) uses to submit
# vm_create / destroy / grow / migrate / cluster_init / node_join /
# node_leave sagas.
from routes_operations import register_routes as _register_operations_routes
_register_operations_routes(app, require_operator=require_operator)


# ── Supportability checks ─────────────────────────────────────────────────
# Endpoint lives in mgmt/routes_support.py. Pure read-only diagnostic;
# see that file for the per-check details.
from routes_support import register_routes as _register_support_routes
_register_support_routes(
    app,
    load_cluster=load_cluster,
    get_nodes=get_nodes,
    ssh_cmd_rc=ssh_cmd_rc,
)


# ── Console redirect + VNC WebSocket → raw-TCP proxy ───────────────────────
# Implementation lives in mgmt/routes_console.py.
from routes_console import register_routes as _register_console_routes
_register_console_routes(
    app,
    build_cluster_state=build_cluster_state,
    get_nodes=get_nodes,
    get_vm_vnc_port=get_vm_vnc_port,
)

# ── routes_iso (deferred from earlier — needs push_log) ──────────────────
from routes_iso import register_routes as _register_iso_routes
_register_iso_routes(app, push_log=push_log)


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

def serve_main():
    """Bind uvicorn to the operator/CLI ports and block until SIGTERM.
    The bedrock-d entrypoint calls this after wiring shared state +
    starting the netd thread.

    Listeners:
      * 8443 HTTPS — operator dashboard + LAN-reachable mgmt API.
        Browser-trusted via the local-ip.co wildcard cert (refresh
        timer keeps it ≤30 days from expiry). Bound only when a cert
        is present; until then we fall back to the open-LAN bootstrap
        port below.
      * 127.0.0.1:8001 HTTP — local CLI / intra-process endpoint. The
        ``bedrock`` CLI dials this; rqlite_client, view_builder, etc.
        also point here. **Loopback-only, no LAN exposure.**
      * 8444 LAN HTTP — bootstrap-only, used when no TLS cert exists
        yet so a joiner can fetch ``/api/cluster``. As soon as the
        cert-refresh timer drops the first cert (~2 min after install)
        the next restart switches to the safe layout above.

    Port 8080 is reserved for ``weed-volume`` (see
    docs/storage-architecture.md); the local mgmt API is on
    ``http://127.0.0.1:8001``.

    The bootstrap listener must NOT reuse 8080: weed-volume binds
    ``0.0.0.0:8080`` (every node), and 0.0.0.0 already covers loopback,
    so a 127.0.0.1:8080 bootstrap bind would EADDRINUSE. With bedrock-d
    owning boot (quorum-aware), weed-volume comes up after the
    orchestrator establishes role/quorum — but the bootstrap branch runs
    on a fresh cert-less node where ordering can't be relied on, so we
    bind a dedicated bootstrap port (8444) clear of the whole map.
    (finding T-05.)
    """
    import threading
    import uvicorn
    cert = Path("/etc/bedrock/tls/cert.pem")
    key  = Path("/etc/bedrock/tls/key.pem")
    # Always bind 127.0.0.1:8001 — the local CLI dials this regardless
    # of cert state. Running in a daemon thread so the main thread can
    # bring up the LAN listener (8443 with cert, 8080 without).
    threading.Thread(
        target=lambda: uvicorn.run(app, host="127.0.0.1", port=8001,
                                   log_level="warning"),
        daemon=True,
    ).start()
    if cert.exists() and key.exists():
        uvicorn.run(app, host="0.0.0.0", port=8443,
                    ssl_keyfile=str(key), ssl_certfile=str(cert))
    else:
        # No cert yet — bind a LAN-reachable bootstrap HTTP port so a
        # joiner can fetch /api/cluster before the first cert exists.
        # NOT 8080: weed-volume binds 0.0.0.0:8080 on every node and
        # 0.0.0.0 already covers loopback, so any 8080 bind here would
        # EADDRINUSE. 8444 is dedicated to this bootstrap window and
        # clear of the whole port map (docs/storage-architecture.md).
        # When the cert-refresh timer drops the first cert, the next
        # bedrock-d restart flips to 8443. (finding T-05.)
        uvicorn.run(app, host="0.0.0.0", port=8444)


if __name__ == "__main__":
    serve_main()
