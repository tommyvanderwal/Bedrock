"""ISO library routes (Stage 9 PR #4 of the rewrite).

Three endpoints — list, upload, delete — over the local
``/opt/bedrock/iso`` tree. The tree itself is the SeaweedFS FUSE
mount at ``/mnt/bedrock/iso`` on every node (single source of
truth, cluster-replicated). These routes operate on the local
view; SeaweedFS handles replication.

Same dependency-injection pattern as ``routes_console`` —
``push_log`` is passed in to avoid a circular import.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, File, HTTPException, UploadFile


# This dir lives on every node — SeaweedFS FUSE mounts the filer's
# /iso/ subtree here. Listings on any node show the same files; a
# delete on any node deletes cluster-wide.
ISO_DIR = Path("/opt/bedrock/iso")


def register_routes(app: FastAPI, *, push_log: Callable) -> None:
    """Attach /api/isos, /api/isos/upload, /api/isos/{name} to ``app``."""

    @app.get("/api/isos")
    def api_list_isos():
        """Every ``.iso`` (case-insensitive) in ISO_DIR. Microsoft's
        official Windows Server downloads arrive with ``.ISO``
        (uppercase) and we want them visible without making the
        operator rename them by hand."""
        if not ISO_DIR.exists():
            return []
        out = []
        for p in sorted(ISO_DIR.iterdir()):
            if p.is_file() and p.suffix.lower() == ".iso":
                try:
                    out.append({"name": p.name, "size_bytes": p.stat().st_size})
                except Exception:
                    continue
        return out

    @app.post("/api/isos")
    async def api_upload_iso(file: UploadFile = File(...)):
        """Stream-upload an ISO into ISO_DIR. Chunked so multi-GB
        Windows ISOs don't balloon memory. Filename extension is
        normalised to lowercase ``.iso`` regardless of source
        casing; basename preserved so the operator still recognises
        their file."""
        if not file.filename.lower().endswith(".iso"):
            raise HTTPException(400, "filename must end in .iso")
        ISO_DIR.mkdir(parents=True, exist_ok=True)
        src_name = Path(file.filename).name  # strip any directory
        base = src_name[:-4] if len(src_name) > 4 else src_name
        dst = ISO_DIR / f"{base}.iso"
        total = 0
        with dst.open("wb") as fh:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
                total += len(chunk)
        push_log(f"ISO uploaded: {dst.name} ({total // 1024 // 1024} MB)",
                 node="mgmt", app="bedrock-mgmt", level="info")
        return {"status": "uploaded", "name": dst.name, "size_bytes": total}

    @app.delete("/api/isos/{name}")
    def api_delete_iso(name: str):
        """Delete an ISO. Path traversal guarded by ``Path(name).name``."""
        safe = Path(name).name
        p = ISO_DIR / safe
        if not p.exists() or not p.is_file():
            raise HTTPException(404, "Not found")
        p.unlink()
        push_log(f"ISO deleted: {safe}",
                 node="mgmt", app="bedrock-mgmt", level="info")
        return {"status": "deleted", "name": safe}
