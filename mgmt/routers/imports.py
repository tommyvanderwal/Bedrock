"""VM image import jobs: upload an image, inspect/convert it (virt-v2v / qemu-img), and
create a VM from it. Long ops run as background tasks (the dashboard polls /api/tasks)."""
from __future__ import annotations

from typing import Any, Optional
import asyncio
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel

from common import (push_log, ssh_cmd, ssh_cmd_rc, get_nodes, load_cluster,
                    load_inventory, save_inventory, _import_dir, _write_import_meta, IMPORT_ROOT)
from tasks import registry as task_registry
router = APIRouter(tags=["imports"])


def _vm_create_from_import(*a, **k):
    from app import _vm_create_from_import as _impl
    return _impl(*a, **k)




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




def _import_meta(d: Path) -> dict:
    mp = d / "meta.json"
    if not mp.exists(): return {}
    try: return json.loads(mp.read_text())
    except Exception: return {}




@router.get("/api/imports")
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




@router.get("/api/imports/{job_id}")
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




@router.post("/api/imports/upload")
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




@router.post("/api/imports/{job_id}/convert")
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




@router.post("/api/imports/{job_id}/create-vm")
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




@router.delete("/api/imports/{job_id}")
def api_import_delete(job_id: str):
    d = _import_dir(job_id)
    if not d.exists(): raise HTTPException(404)
    shutil.rmtree(d, ignore_errors=True)
    push_log(f"Import deleted: {job_id}", node="mgmt", app="bedrock-mgmt", level="info")
    return {"status": "deleted", "id": job_id}
# ── import formats ──


IMPORT_INPUT_FORMATS = {".ova", ".ovf", ".vmdk", ".vhd", ".vhdx",
                        ".qcow2", ".raw", ".img"}




QEMU_FORMAT_MAP = {
    "qcow2": "qcow2", "raw": "raw", "img": "raw",
    "vmdk": "vmdk",  "vhd": "vpc",  "vhdx": "vhdx",
}
