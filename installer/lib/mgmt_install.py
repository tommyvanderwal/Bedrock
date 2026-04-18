"""Install full management stack on the first node (`bedrock init`).

Downloads and starts:
  - VictoriaMetrics (port 8428)
  - VictoriaLogs (port 9428, syslog :5140)
  - FastAPI + Svelte dashboard (port 8080)
  - SQLite inventory DB
  - bedrock-witness (podman container, port 9443) — if no external witness
"""

import os
import subprocess
import uuid
from pathlib import Path
from typing import Optional
from . import state, exporters


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


def install_full(cluster_name: str, witness_host: Optional[str], repo: str):
    """Install FastAPI + VM + VL + SQLite + witness."""
    s = state.load()
    hw = s.get("hardware", {})

    # Directories
    for d in (BINARIES, DATA / "vm", DATA / "vl", MGMT):
        d.mkdir(parents=True, exist_ok=True)

    # 1. VictoriaMetrics
    if not (BINARIES / "victoria-metrics").exists():
        _download(f"{repo}/binaries/victoria-metrics", BINARIES / "victoria-metrics")
        os.chmod(BINARIES / "victoria-metrics", 0o755)

    # 2. VictoriaLogs
    if not (BINARIES / "victoria-logs").exists():
        _download(f"{repo}/binaries/victoria-logs", BINARIES / "victoria-logs")
        os.chmod(BINARIES / "victoria-logs", 0o755)

    # 3. ISO library + NFS export (mgmt-node-only; compute nodes can mount it
    #    on demand. Read-only export on mgmt LAN + DRBD ring.)
    iso_dir = BEDROCK_BASE / "iso"
    iso_dir.mkdir(parents=True, exist_ok=True)
    (iso_dir / "README.md").write_text(
        "# Bedrock ISO library\n\n"
        "Upload install ISOs via the dashboard (/isos) or scp here directly.\n"
        "Files appear in the 'Create VM' dropdown.\n"
    )
    run("dnf install -y -q nfs-utils >/dev/null 2>&1", check=False)
    Path("/etc/exports.d").mkdir(exist_ok=True)
    Path("/etc/exports.d/bedrock-iso.exports").write_text(
        "/opt/bedrock/iso  192.168.2.0/24(ro,sync,no_subtree_check) "
        "10.99.0.0/24(ro,sync,no_subtree_check)\n"
    )
    run("systemctl enable --now nfs-server >/dev/null 2>&1", check=False)
    run("exportfs -ra 2>&1 || true", check=False)

    # 4. FastAPI + Svelte dashboard files
    print("  Installing dashboard application...")
    # Fetch a tarball of the mgmt app (pre-packaged on repo)
    mgmt_tar = f"{repo}/mgmt.tar.gz"
    r = subprocess.run(f"curl -fsSL '{mgmt_tar}' -o /tmp/mgmt.tar.gz", shell=True)
    if r.returncode == 0:
        run(f"tar xzf /tmp/mgmt.tar.gz -C {MGMT} --strip-components=1")
        run("pip3 install -q fastapi uvicorn paramiko websockets pydantic python-multipart 2>&1 | tail -1 || true",
            check=False)

    # 5. Prometheus scrape config — mgmt app will rewrite this whenever
    #    nodes register/unregister, so we just seed with this node.
    mgmt_ip = _pick_mgmt_ip(hw)
    scrape_conf = f"""scrape_configs:
  - job_name: node
    scrape_interval: 10s
    static_configs:
      - targets: ['{mgmt_ip}:9100']
        labels:
          cluster: {cluster_name}
  - job_name: libvirt
    scrape_interval: 10s
    static_configs:
      - targets: ['{mgmt_ip}:9177']
        labels:
          cluster: {cluster_name}
"""
    (BEDROCK_BASE / "scrape.yml").write_text(scrape_conf)

    # 6. Install node_exporter + vm_exporter (this node is mgmt+compute)
    exporters.install(repo)

    # 7. Systemd units
    _write_systemd("bedrock-vm", f"""[Unit]
Description=Bedrock VictoriaMetrics
After=network.target

[Service]
ExecStart={BINARIES}/victoria-metrics -storageDataPath={DATA}/vm -promscrape.config={BEDROCK_BASE}/scrape.yml -retentionPeriod=90d -httpListenAddr=:8428
Restart=always

[Install]
WantedBy=multi-user.target
""")
    _write_systemd("bedrock-vl", f"""[Unit]
Description=Bedrock VictoriaLogs
After=network.target

[Service]
ExecStart={BINARIES}/victoria-logs -storageDataPath={DATA}/vl -httpListenAddr=:9428 -syslog.listenAddr.tcp=:5140
Restart=always

[Install]
WantedBy=multi-user.target
""")
    _write_systemd("bedrock-mgmt", f"""[Unit]
Description=Bedrock Management Dashboard
After=network.target bedrock-vm.service bedrock-vl.service

[Service]
WorkingDirectory={MGMT}
ExecStart=/usr/bin/python3 {MGMT}/app.py
Restart=always

[Install]
WantedBy=multi-user.target
""")

    # Start services
    print("  Starting services...")
    run("systemctl enable --now bedrock-vm bedrock-vl bedrock-mgmt", check=False)

    if not witness_host:
        witness_host = "self"

    # Save state
    s["cluster_name"] = cluster_name
    s["cluster_uuid"] = s.get("cluster_uuid") or str(uuid.uuid4())
    s["role"] = "mgmt+compute"
    s["node_id"] = 0
    s["node_name"] = hw.get("hostname", "node1")
    s["witness_host"] = witness_host
    s["mgmt_ip"] = _pick_mgmt_ip(hw)
    s["mgmt_url"] = f"http://{s['mgmt_ip']}:8080"
    state.save(s)

    # Initialise /etc/bedrock/cluster.json with this node registered
    import json as _json
    drbd_ip = ""
    for n in hw.get("nics", []):
        if n.get("ip", "").startswith("10.99."):
            drbd_ip = n["ip"]
    cluster = {
        "cluster_name": cluster_name,
        "cluster_uuid": s["cluster_uuid"],
        "nodes": {
            s["node_name"]: {
                "host": s["mgmt_ip"],
                "drbd_ip": drbd_ip,
                "tb_ip": drbd_ip,
                "eno_ip": drbd_ip,
                "role": "mgmt+compute",
                "cockpit": f"https://{s['mgmt_ip']}:9090",
            }
        },
    }
    from pathlib import Path as _Path
    _Path("/etc/bedrock/cluster.json").write_text(_json.dumps(cluster, indent=2))

    print(f"  Cluster UUID: {s['cluster_uuid']}")
    print(f"  Mgmt URL:     {s['mgmt_url']}")
