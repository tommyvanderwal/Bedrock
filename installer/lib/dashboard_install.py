"""Dashboard install — runs on every node so the Svelte UI + FastAPI is
reachable at http://<any-node>:8080.

The master node also runs the metrics + logs stack (VictoriaMetrics +
VictoriaLogs); followers only get the dashboard. The mgmt API on a
follower works against the same /etc/bedrock/cluster.json that
view_builder rebuilds from the replicated log, so reads return the
cluster-wide picture; writes go through the same code path and rely
on cluster-wide SSH access (every node has every other node's pubkey
from the join handshake)."""

from __future__ import annotations

import subprocess
from pathlib import Path


BEDROCK_BASE = Path("/opt/bedrock")
MGMT = BEDROCK_BASE / "mgmt"
SYSTEMD_DIR = Path("/etc/systemd/system")


def _run(cmd: str, check: bool = False) -> int:
    return subprocess.run(cmd, shell=True).returncode


def install_dashboard(repo: str, with_metrics: bool = False) -> None:
    """Fetch mgmt.tar.gz, extract into /opt/bedrock/mgmt, write the
    systemd unit, enable + start it.

    `with_metrics=True` adds an `After=` dep on bedrock-vm/bedrock-vl
    (only set on the master, where those services exist).
    """
    MGMT.mkdir(parents=True, exist_ok=True)
    mgmt_tar = f"{repo}/mgmt.tar.gz"
    if _run(f"curl -fsSL '{mgmt_tar}' -o /tmp/mgmt.tar.gz") == 0:
        _run(f"tar xzf /tmp/mgmt.tar.gz -C {MGMT} --strip-components=1")

    after = "network.target"
    if with_metrics:
        after += " bedrock-vm.service bedrock-vl.service"
    unit = SYSTEMD_DIR / "bedrock-mgmt.service"
    unit.write_text(
        "[Unit]\n"
        "Description=Bedrock Management Dashboard\n"
        f"After={after}\n"
        "\n"
        "[Service]\n"
        f"WorkingDirectory={MGMT}\n"
        f"ExecStart=/usr/bin/python3 {MGMT}/app.py\n"
        "Restart=always\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )
    _run("systemctl daemon-reload")
    _run("systemctl enable --now bedrock-mgmt")
