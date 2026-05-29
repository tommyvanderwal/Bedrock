"""Management-node install entry point (`bedrock init`).

`install_full` runs the cluster_init saga, which installs the
observability stack (VictoriaMetrics :8428, VictoriaLogs :9428 + syslog
:5140), the dashboard (:8443 HTTPS LAN + 127.0.0.1:8001 loopback), the
per-node rqlite, and SeaweedFS. The path constants and small helpers in
this module (`run`, `_pick_mgmt_ip`, `_download`, `_write_systemd`) are
reused by the saga's steps.
"""

import subprocess
from pathlib import Path


BEDROCK_BASE = Path("/opt/bedrock")
BINARIES = BEDROCK_BASE / "bin"
DATA = BEDROCK_BASE / "data"
MGMT = BEDROCK_BASE / "mgmt"


def run(cmd, check=True):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"{cmd} failed: {r.stderr}")
    return r.stdout.strip()


def _pick_mgmt_ip(hw: dict) -> str:
    """Pick the mgmt NIC IP — prefer br0, else any 192.168.x.x (LAN)."""
    for n in hw.get("nics", []):
        if n["state"] == "UP" and n["name"] == "br0" and n["ip"]:
            return n["ip"]
    for n in hw.get("nics", []):
        if n["state"] == "UP" and n["ip"] and not n["ip"].startswith("10."):
            return n["ip"]
    for n in hw.get("nics", []):
        if n["state"] == "UP" and n["ip"]:
            return n["ip"]
    return ""


def _download(url: str, dest: Path):
    print(f"  Fetching {url.split('/')[-1]}...")
    run(f"curl -fsSL -o {dest} '{url}'")


def _write_systemd(name: str, content: str):
    path = Path(f"/etc/systemd/system/{name}.service")
    path.write_text(content)
    run("systemctl daemon-reload")


def install_full(cluster_name: str, repo: str):
    """Entry point for ``bedrock init``: run the cluster_init saga
    (ordered idempotent steps, progress persisted to
    /var/lib/bedrock/init-progress.json, resumable from crash). The saga
    reuses the path constants + helpers defined above."""
    import sys as _sys
    from pathlib import Path as _Path
    _root = _Path(__file__).resolve().parents[2]
    if str(_root) not in _sys.path:
        _sys.path.insert(0, str(_root))
    for p in ("/usr/local/lib/bedrock",):
        if p not in _sys.path:
            _sys.path.insert(0, p)
    from bedrock_d.install.cluster_init import run_cluster_init
    return run_cluster_init(cluster_name=cluster_name, repo=repo)
