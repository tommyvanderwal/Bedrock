"""VM export jobs: trigger an export (qcow2/raw/vmdk), list/download/delete export artifacts."""
from __future__ import annotations
import asyncio
import json
import re
import shutil
import subprocess
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from common import (push_log, ssh_cmd, ssh_cmd_rc, build_cluster_state, get_nodes,
                    load_cluster, _vm_host, _vm_get_settings)
from tasks import registry as task_registry
router = APIRouter(tags=["exports"])




def _export_dir(job_id: str) -> Path:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", job_id):
        raise HTTPException(400, "invalid id")
    return EXPORT_ROOT / job_id




@router.get("/api/exports")
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




@router.post("/api/vms/{vm_name}/export")
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




@router.get("/api/exports/{job_id}/download")
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




@router.delete("/api/exports/{job_id}")
def api_export_delete(job_id: str):
    d = _export_dir(job_id)
    if not d.exists(): raise HTTPException(404)
    shutil.rmtree(d, ignore_errors=True)
    return {"status": "deleted", "id": job_id}
# ── export roots/formats ──


EXPORT_ROOT = Path("/opt/bedrock/exports")




# ── Export library ─────────────────────────────────────────────────────────

EXPORT_FORMATS = {"qcow2", "vmdk", "vhdx", "raw"}
