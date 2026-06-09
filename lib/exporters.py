"""Deploy node_exporter + vm_exporter on this node, plus the
observability binaries (vmagent, vlagent, vmbackup, vmrestore,
victoria-metrics, victoria-logs). Every node carries the full set
because any node can be promoted to a metrics/logs backend slot."""

import os
import subprocess
from pathlib import Path

BIN_DIR = Path("/opt/bedrock/bin")
NODE_EXPORTER = BIN_DIR / "node_exporter"
VM_EXPORTER = BIN_DIR / "vm_exporter.py"

# Observability stack — agents needed on every node, backends needed on
# whichever nodes are currently in obs_backends. Cheaper to deploy
# everywhere than to deal with "missing binary at promotion time."
OBS_BINS = ["vmagent", "vlagent", "vmbackup", "vmrestore",
            "victoria-metrics", "victoria-logs"]


def _run(cmd, check=True):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"{cmd} failed: {r.stderr}")


def install(repo: str):
    """Fetch binaries from the install repo, install systemd units, start them."""
    BIN_DIR.mkdir(parents=True, exist_ok=True)

    if not NODE_EXPORTER.exists():
        print("  Fetching node_exporter...")
        _run(f"curl -fsSL -o {NODE_EXPORTER} '{repo}/binaries/node_exporter'")
    os.chmod(NODE_EXPORTER, 0o755)

    print("  Fetching vm_exporter...")
    _run(f"curl -fsSL -o {VM_EXPORTER} '{repo}/binaries/vm_exporter.py'")
    os.chmod(VM_EXPORTER, 0o755)

    # Fetch the observability binaries. Idempotent: skip if already on
    # disk. The reconciler in lib/observability.py owns the
    # systemd units for these — exporters.py only ensures the bytes
    # are available.
    for b in OBS_BINS:
        target = BIN_DIR / b
        if target.exists() and target.stat().st_size > 1_000_000:
            continue
        print(f"  Fetching {b}...")
        try:
            _run(f"curl -fsSL -o {target} '{repo}/binaries/{b}'")
            os.chmod(target, 0o755)
        except RuntimeError as e:
            print(f"  WARN: {b} not available in repo: {e}")

    Path("/etc/systemd/system/node-exporter.service").write_text(
        "[Unit]\nDescription=Prometheus node_exporter\nAfter=network.target\n\n"
        "[Service]\nType=simple\nExecStart=/opt/bedrock/bin/node_exporter --web.listen-address=:9100\n"
        "Restart=always\nRestartSec=3\n\n[Install]\nWantedBy=multi-user.target\n"
    )
    Path("/etc/systemd/system/vm-exporter.service").write_text(
        "[Unit]\nDescription=Bedrock VM/DRBD exporter\nAfter=libvirtd.service\n"
        "Wants=libvirtd.service\n\n[Service]\nType=simple\n"
        "ExecStart=/usr/bin/python3 /opt/bedrock/bin/vm_exporter.py\n"
        "Restart=always\nRestartSec=3\n\n[Install]\nWantedBy=multi-user.target\n"
    )
    _run("systemctl daemon-reload")
    _run("systemctl enable --now node-exporter vm-exporter")
