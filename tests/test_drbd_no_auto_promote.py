"""Permanent guard: EVERY DRBD resource Bedrock renders MUST disable
``auto-promote``.

DRBD's default is ``auto-promote yes`` — it silently promotes a node to
Primary the instant ``/dev/drbdN`` is opened (a mount, qemu opening a VM
disk, even a udev/blkid/lvm probe). That is the systemic cause of
dual-Primary split-brain: one node is bedrock-d-promoted (election /
witness / rqlite gated) while ANOTHER self-promotes on an incidental device
open during a failover or migration race. ``allow-two-primaries no`` only
blocks that *while connected* — across a partition both sides could still
self-promote. So every resource must pin ``auto-promote no`` and let ONLY
bedrock-d's orchestrated ``drbdadm primary`` ever promote.

If any render path regresses to the DRBD default, these tests fail. See
docs/cluster-convergence.md (the R3/R5 ``auto-promote no`` keystone) and
docs/witness-death-oracle.md.
"""
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "installer"))


def _assert_off(text: str, what: str) -> None:
    assert re.search(r"auto-promote\s+no\b", text), (
        f"{what}: rendered .res is missing `auto-promote no` — DRBD will "
        f"self-promote to Primary on any device open (dual-Primary risk).\n"
        f"--- rendered ---\n{text}"
    )
    assert not re.search(r"auto-promote\s+yes\b", text), (
        f"{what}: rendered .res explicitly sets `auto-promote yes`."
    )


def _assert_quorum_fenced(text: str, what: str) -> None:
    """The arbiter (cluster-singleton) resource must quorum-gate writes so a
    minority Primary freezes (no writes, no UUID rotation). See
    docs/cluster-convergence.md."""
    assert re.search(r"quorum\s+all\b", text), (
        f"{what}: rendered .res is missing `quorum all` — a Primary that loses "
        f"any peer must freeze instantly (no writes, no UUID rotation). Without "
        f"it a minority Primary keeps writing → split-brain + false "
        f"takeover-divergence.\n--- rendered ---\n{text}"
    )
    assert re.search(r"on-no-quorum\s+suspend-io\b", text), (
        f"{what}: rendered .res is missing `on-no-quorum suspend-io`."
    )
    assert not re.search(r"quorum\s+off\b", text), (
        f"{what}: rendered .res explicitly sets `quorum off`."
    )


def test_vm_drbd_render_disables_auto_promote():
    """Per-VM disks (cattle / pet / vipet) via bedrock_d.vm.drbd_config."""
    from bedrock_d.vm import drbd_config as cfg
    peers = [
        cfg.Peer(node_name="n1", host="h1", loopback_ip="10.0.0.1", node_id=0),
        cfg.Peer(node_name="n2", host="h2", loopback_ip="10.0.0.2", node_id=1),
    ]
    text = cfg.render("vm-test-disk0", minor=1102, peers=peers)
    _assert_off(text, "VM DRBD (drbd_config.render)")


def test_arbiter_drbd_render_disables_auto_promote(monkeypatch):
    """The cluster-singleton / arbiter tier via tier_storage.render_drbd_res."""
    from lib import tier_storage as ts
    monkeypatch.setattr(ts, "get_drbd_node_id",
                        lambda resource, name: {"n1": 0, "n2": 1, "n3": 2}[name])
    peers = [{"name": "n1", "loopback_ip": "10.0.0.1"},
             {"name": "n2", "loopback_ip": "10.0.0.2"}]
    text = ts.render_drbd_res("cluster", 1101, peers)
    _assert_off(text, "arbiter DRBD (render_drbd_res)")
    _assert_quorum_fenced(text, "arbiter DRBD (render_drbd_res)")


def test_arbiter_mesh_drbd_render_disables_auto_promote(monkeypatch):
    """The mesh/multi-path arbiter render via render_drbd_res_mesh (empty
    snapshot → loopback path fallback)."""
    from lib import tier_storage as ts
    monkeypatch.setattr(ts, "get_drbd_node_id",
                        lambda resource, name: {"n1": 0, "n2": 1, "n3": 2}[name])
    peers = [{"name": "n1", "loopback_ip": "10.0.0.1"},
             {"name": "n2", "loopback_ip": "10.0.0.2"}]
    text = ts.render_drbd_res_mesh("cluster", 1101, peers, {})
    _assert_off(text, "arbiter mesh DRBD (render_drbd_res_mesh)")
    _assert_quorum_fenced(text, "arbiter mesh DRBD (render_drbd_res_mesh)")
