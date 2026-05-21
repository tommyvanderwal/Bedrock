"""Contract tests for the NodeJoin saga.

Same contract pattern as test_cluster_init_saga.py: assert the step
list shape + architectural invariants. Step bodies aren't executed
(they shell out, do SSH, talk to a master); that's e2e territory.

EXPECTED_STEPS is the load-bearing contract — change here means
change to the `bedrock join` flow.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from bedrock_d.install import node_join as _nj  # noqa: F401
from bedrock_d.orchestrator.sagas import SAGAS
from bedrock_d.orchestrator.sagas.executor import _ordered_steps


EXPECTED_STEPS = [
    # Local prep
    "prepare_dirs",
    "detect_mgmt_ip",
    "derive_identity",
    "install_exporters",
    # Network handshake
    "request_join_approval",
    "write_state_json",
    "write_bootstrap_cluster_json",
    "install_peer_pubkeys",
    "prescan_peer_hostkeys",
    # Storage + daemon
    "provision_storage_n1",
    "pre_extract_mgmt",
    "start_bedrock_d",
    # rqlite join
    "wait_master_reachable",
    "render_rqlited_env",
    "start_rqlited_joiner",
    # SeaweedFS local
    "install_dashboard",
    "seaweedfs_install",
    "seaweedfs_configs",
    "seaweedfs_start_local",
    "fuse_mount",
    # Cluster-tier DRBD: peer side. Waits for the master's
    # cluster_tier_promote_master saga to flip mode=drbd, then joins
    # as DRBD Secondary. N=1 path is a no-op.
    "cluster_tier_join_peer",
]


def test_node_join_saga_is_registered():
    assert "node_join" in SAGAS
    assert SAGAS["node_join"] is _nj.NodeJoin
    assert _nj.NodeJoin._saga_kind == "node_join"


def test_node_join_can_be_instantiated():
    inst = _nj.NodeJoin()
    assert inst is not None


def test_node_join_step_set_matches_documented_flow():
    declared = [name for (name, _fn) in _ordered_steps(_nj.NodeJoin)]
    assert declared == EXPECTED_STEPS, (
        "NodeJoin step list drifted from the documented flow.\n"
        f"declared:  {declared}\n"
        f"expected:  {EXPECTED_STEPS}"
    )


def test_approval_handshake_runs_before_state_write():
    """We can't write cluster identity to state.json until we've
    successfully received cluster.key from the master via the
    ECDH-sealed approval response. If state_json was written first
    and the handshake later failed, the node would look "joined"
    in state.json but have no cluster.key."""
    declared = [name for (name, _fn) in _ordered_steps(_nj.NodeJoin)]
    assert declared.index("request_join_approval") < declared.index(
        "write_state_json")


def test_master_reachable_check_before_rqlited_join():
    """rqlited -join times out after 30s per peer. If we start rqlited
    before the mesh has installed the /32 to the master, the join
    blocks for 30s waiting on an unreachable peer."""
    declared = [name for (name, _fn) in _ordered_steps(_nj.NodeJoin)]
    assert declared.index("wait_master_reachable") < declared.index(
        "start_rqlited_joiner")


def test_bedrock_d_starts_before_rqlited():
    """bedrock-d's mesh thread installs the /32 routes rqlited needs
    to reach the master. So bedrock-d must be up first."""
    declared = [name for (name, _fn) in _ordered_steps(_nj.NodeJoin)]
    assert declared.index("start_bedrock_d") < declared.index(
        "start_rqlited_joiner")


def test_pre_extract_mgmt_before_bedrock_d():
    """bedrock-d imports `mgmt.app` at startup; if mgmt.tar.gz hasn't
    been extracted to /opt/bedrock yet, bedrock-d crash-loops."""
    declared = [name for (name, _fn) in _ordered_steps(_nj.NodeJoin)]
    assert declared.index("pre_extract_mgmt") < declared.index(
        "start_bedrock_d")


def test_no_duplicate_step_names():
    declared = [name for (name, _fn) in _ordered_steps(_nj.NodeJoin)]
    assert len(declared) == len(set(declared)), (
        f"duplicate step names: "
        f"{[n for n in declared if declared.count(n) > 1]}"
    )
