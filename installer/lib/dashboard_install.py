"""Dashboard install — runs on every node so the Svelte UI + FastAPI is
reachable at https://<any-node>:8443 (operator browser, TLS via the
local-ip.co wildcard cert) and http://localhost:8080 (per-node CLI).

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

    # The dashboard is served by the unified bedrock-d process — see
    # docs/daemon-unification.md. We no longer write a separate
    # bedrock-mgmt.service. install.sh has already shipped
    # bedrock-d.service; this helper only needs to ensure the mgmt/
    # source tarball is extracted into /opt/bedrock/mgmt/ (done above)
    # so bedrock-d can import it. Enable + start the unified daemon.
    _run("systemctl daemon-reload")
    # Reset-failed first: a previous attempt may have left bedrock-d
    # in a rate-limited state from before cluster.key was written.
    _run("systemctl reset-failed bedrock-d.service 2>/dev/null", check=False)
    _run("systemctl enable --now bedrock-d")
    # Tidy: if a stale bedrock-mgmt.service is still installed from a
    # pre-unification ISO, disable it so it can't shadow bedrock-d on
    # port 8080/8443.
    _run("systemctl disable --now bedrock-mgmt.service 2>/dev/null",
         check=False)
