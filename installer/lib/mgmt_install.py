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
from . import state, exporters, tier_storage, daemon_setup


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
    # Pre-fetch the virtio-win driver ISO. Attached as a 2nd CDROM on every
    # VM install so Windows Setup can load viostor + NetKVM without manual
    # download. Harmless for Linux installs — ignored by the installer.
    virtio_win = iso_dir / "virtio-win.iso"
    if not virtio_win.exists():
        # Prefer the LAN-cached copy (dev box repo); fall back to upstream on
        # first-ever install where the dev box hasn't cached it yet.
        print("  Fetching virtio-win.iso (~750 MB, one-time)...")
        sources = [
            f"{repo}/binaries/virtio-win.iso",
            "https://fedorapeople.org/groups/virt/virtio-win/"
            "direct-downloads/stable-virtio/virtio-win.iso",
        ]
        ok = False
        for url in sources:
            r = subprocess.run(
                f"curl -fsSL --connect-timeout 5 -o {virtio_win}.tmp '{url}'",
                shell=True)
            if r.returncode == 0:
                (iso_dir / "virtio-win.iso.tmp").rename(virtio_win)
                ok = True
                break
        if not ok:
            print("  WARN: virtio-win.iso download failed; Windows installs "
                  "will need the driver ISO attached manually.")
    run("dnf install -y -q nfs-utils >/dev/null 2>&1", check=False)
    Path("/etc/exports.d").mkdir(exist_ok=True)
    Path("/etc/exports.d/bedrock-iso.exports").write_text(
        "/opt/bedrock/iso  192.168.2.0/24(ro,sync,no_subtree_check) "
        "10.99.0.0/24(ro,sync,no_subtree_check)\n"
    )
    run("systemctl enable --now nfs-server >/dev/null 2>&1", check=False)
    run("exportfs -ra 2>&1 || true", check=False)

    # Bind-mount /opt/bedrock/iso → /mnt/isos so the mgmt node references
    # ISOs via the same path as compute nodes (which NFS-mount there).
    Path("/mnt/isos").mkdir(exist_ok=True)
    Path("/etc/systemd/system/mnt-isos.mount").write_text(
        "[Unit]\nDescription=Bedrock ISO library (bind mount)\n\n"
        "[Mount]\nWhat=/opt/bedrock/iso\nWhere=/mnt/isos\n"
        "Type=none\nOptions=bind,ro\n\n"
        "[Install]\nWantedBy=multi-user.target\n"
    )
    run("systemctl daemon-reload", check=False)
    run("systemctl enable --now mnt-isos.mount >/dev/null 2>&1", check=False)

    # 4. FastAPI + Svelte dashboard files. Same helper runs on
    # followers too — the dashboard is reachable from ANY node.
    # NOTE: Python deps (fastapi, uvicorn, paramiko, websockets, pydantic,
    # python-multipart) installed by packages.install_base() on every
    # node, not here. (Lessons-log L17 — every node may become master.)
    print("  Installing dashboard application...")
    from . import dashboard_install as _di

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

    # 7. Systemd units — bedrock-vm + bedrock-vl run on the master only
    # (single VictoriaMetrics + VictoriaLogs instance per cluster).
    # bedrock-mgmt (FastAPI + Svelte UI) is installed by dashboard_install,
    # which also runs on followers so the dashboard is reachable on every
    # node.
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

    print("  Starting metrics + logs services...")
    run("systemctl enable --now bedrock-vm bedrock-vl", check=False)
    print("  Installing + starting dashboard service (with metrics)...")
    _di.install_dashboard(repo, with_metrics=True)

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

    # Storage tiers — N=1 single-node setup. Idempotent; safe on re-run.
    print()
    print("Setting up storage tiers (N=1: local LV thin)...")
    try:
        tier_storage.setup_n1()
    except Exception as e:
        print(f"  WARN: tier setup failed: {e}")
        print(f"  You can re-run with: bedrock storage init")

    # bedrock-rust daemon setup. Generates the cluster's 32-byte AEAD
    # key (saved at /etc/bedrock/cluster.key), initialises the log with
    # the cluster_uuid as bootstrap entry, writes daemon.toml, and
    # starts the systemd service. Standalone mode at init — peer +
    # witness entries are added via cluster transitions later.
    print()
    print("Starting bedrock-rust daemon...")
    try:
        daemon_setup.write_cluster_key()
        daemon_setup.init_log_if_needed(s["cluster_uuid"])
        daemon_setup.render_daemon_toml(
            sender_id=1,
            peer_sender_id=None,    # filled in when first peer joins
            peer_listen=["0.0.0.0:8200"],
            peer=[],
            fence_interfaces=[],
            witnesses=[],           # added later; see `bedrock witness add`
            # Master at init advertises Leader so a future joiner
            # attaches as Follower without a manual reconfigure step.
            # The lease loop's witness-based election still has the
            # final say once the peer + witness are both up.
            role="leader",
        )
        daemon_setup.restart()
        print(f"  bedrock-rust running, IPC at /run/bedrock-rust.sock")

        # L48 fix: cluster_init + master node_register entries so the
        # snapshot's nodes dict has the master from the start.
        # Subsequent maintenance/transfer/witness operations all need
        # the snapshot to know about every node.
        try:
            import time as _t
            _t.sleep(1)   # daemon needs a moment to bind IPC
            from . import rust_ipc as _ipc, log_entries as _le
            with _ipc.Daemon() as d:
                d.append(_le.cluster_init(
                    name=cluster_name, uuid=s["cluster_uuid"]))
                d.append(_le.node_register(
                    node_name=s["node_name"],
                    host=s["mgmt_ip"],
                    drbd_ip=drbd_ip,
                    role="mgmt+compute",
                    pubkey="",   # filled in on rotation
                ))
                d.append(_le.mgmt_master(node_name=s["node_name"]))
        except Exception as e:
            print(f"  WARN: master node_register log-append skipped: {e}")
    except Exception as e:
        print(f"  WARN: bedrock-rust setup failed: {e}")
