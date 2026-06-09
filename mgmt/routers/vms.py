"""VM lifecycle: create / delete / start / stop / migrate / attach-disk / compute+priority
settings / HA-level changes. The heavy DRBD + libvirt orchestration helpers live here too."""
from __future__ import annotations
import asyncio
import json
import logging
import os as _os
import re
import threading
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from dependencies import require_operator
from common import (load_cluster, push_log, ssh_cmd, ssh_cmd_rc, get_nodes, build_cluster_state,
                    build_physical_topology, get_vm_disks, get_vm_drbd_resource, get_vm_vnc_port,
                    _vm_start, _vm_shutdown, _vm_poweroff, _vm_host, _vm_get_settings,
                    _propagate_secret, _import_dir, _write_import_meta, IMPORT_ROOT,
                    _mgmt_node_name, load_inventory, save_inventory)
from tasks import registry as task_registry, Task

import sys as _sys
_sys.path.insert(0, "/usr/local/lib/bedrock")
from lib import bedrock_state as _bs             # noqa: E402
from lib import workload as _workload            # noqa: E402
log = logging.getLogger("bedrock")
router = APIRouter(tags=["vms"])




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



@router.post("/api/vms/{vm_name}/start")
def api_vm_start(vm_name: str):
    return _vm_start(vm_name)



@router.post("/api/vms/{vm_name}/stop")
def api_vm_stop(vm_name: str):
    return _vm_shutdown(vm_name)



@router.post("/api/vms/{vm_name}/force-stop")
def api_vm_force_stop(vm_name: str):
    return _vm_poweroff(vm_name)



@router.post("/api/vms/{vm_name}/ha-level")
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




@router.post("/api/vms")
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




@router.delete("/api/vms/{vm_name}")
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




@router.get("/api/vms/{vm_name}/settings")
def api_vm_get_settings(vm_name: str):
    return _vm_get_settings(vm_name)




@router.post("/api/vms/{vm_name}/compute")
def api_vm_compute(vm_name: str, req: ComputeRequest):
    return _vm_set_resources(vm_name, req)




@router.post("/api/vms/{vm_name}/priority")
def api_vm_priority(vm_name: str, req: PriorityRequest):
    return _vm_set_priority(vm_name, req.priority)




@router.post("/api/vms/{vm_name}/cdrom")
def api_vm_cdrom(vm_name: str, req: CdromRequest):
    return _vm_set_cdrom(vm_name, req.action, req.iso)




class AttachDiskRequest(BaseModel):
    size_gb: int  # thin LV size




@router.post("/api/vms/{vm_name}/disks")
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




@router.post("/api/vms/{vm_name}/migrate")
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
# ── more VM helpers + constants ──




# ── ISO library ─────────────────────────────────────────────────────────────
# The three endpoints (list / upload / delete) live in mgmt/routes_iso.py.
# The ISO_DIR constant + VM inventory helpers stay here because the VM
# creation paths in app.py import them.

# Cluster-wide SeaweedFS FUSE mount — identical on every node, so
# `--cdrom {ISO_DIR}/<name>.iso` works from anywhere. See routes_iso.py
# for the upload path that writes here.
ISO_DIR = Path("/mnt/bedrock/iso")




# Process-local reservation set for DRBD minors chosen by in-flight
# converts that haven't yet created their /dev/drbdN. Without this, two
# parallel converts both query `ls /dev/drbd*`, both see "nothing here in
# the target range", both pick the same minor, and one fails at
# `drbdadm create-md` / `up`. The lock below serialises the pick+reserve.
_drbd_minor_lock = threading.Lock()


_drbd_minor_reserved: set[int] = set()




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




# ── VM creation (cattle, optionally ISO-booted) ─────────────────────────────

_VM_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}[a-z0-9]$")


_VALID_PRIORITIES = ("low", "normal", "high")


# Maps priority → libvirt cpu_shares (cgroup weight; default is 1024).
# Powers of 2 on either side so the relative weights are clearly visible.
PRIORITY_CPU_SHARES = {"low": 256, "normal": 1024, "high": 4096}




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
# ── ISO mount dir ──


ISO_MOUNT_DIR = "/mnt/bedrock/iso"  # identical on every cluster node (SeaweedFS FUSE)
