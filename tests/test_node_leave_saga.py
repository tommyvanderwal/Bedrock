"""Contract tests for the NodeLeave saga."""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from bedrock_d.install import node_leave as _nl  # noqa: F401
from bedrock_d.orchestrator.sagas import SAGAS
from bedrock_d.orchestrator.sagas.executor import _ordered_steps


EXPECTED_STEPS = [
    "validate_target",
    "rqlite_node_unregister",
    "rqlite_voter_remove",
    "propagate_daemon_config",
    "stop_remote_services",
    "verify_membership_drop",
]


def test_node_leave_saga_is_registered():
    assert "node_leave" in SAGAS
    assert SAGAS["node_leave"] is _nl.NodeLeave
    assert _nl.NodeLeave._saga_kind == "node_leave"


def test_node_leave_step_set_matches_documented_flow():
    declared = [name for (name, _fn) in _ordered_steps(_nl.NodeLeave)]
    assert declared == EXPECTED_STEPS, (
        f"NodeLeave step list drifted from documented flow.\n"
        f"declared:  {declared}\n"
        f"expected:  {EXPECTED_STEPS}"
    )


def test_unregister_runs_before_voter_remove():
    """Architectural rule: unregister rqlite ROW first, then
    voter slot. Reversing would leave a node 'in cluster.json' but
    no Raft vote — confusing for operators querying state during
    the brief window."""
    declared = [name for (name, _fn) in _ordered_steps(_nl.NodeLeave)]
    assert declared.index("rqlite_node_unregister") < declared.index(
        "rqlite_voter_remove")


def test_stop_remote_runs_after_unregister():
    """Stopping the leaver's bedrock-d before the cluster has
    written node_unregister would let the cluster's election still
    count the leaver in its membership for the brief window —
    surviving nodes would see them as a "stale" peer."""
    declared = [name for (name, _fn) in _ordered_steps(_nl.NodeLeave)]
    assert declared.index("rqlite_node_unregister") < declared.index(
        "stop_remote_services")


def test_verify_is_last_step():
    """The verify step polls for the subscriber to fold; it makes
    sense only after every mutation step has run."""
    declared = [name for (name, _fn) in _ordered_steps(_nl.NodeLeave)]
    assert declared[-1] == "verify_membership_drop"


def test_no_duplicate_step_names():
    declared = [name for (name, _fn) in _ordered_steps(_nl.NodeLeave)]
    assert len(declared) == len(set(declared))
