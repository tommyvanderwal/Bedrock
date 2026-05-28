"""Contract tests for the ClusterInit saga.

These tests do NOT run the actual steps (they shell out to systemd,
lvcreate, curl, etc.). They verify the *shape* of the saga:

- the saga is registered under "cluster_init"
- the step list matches the documented flow chart in
  docs/codebase-rewrite-plan.md
- step order is preserved (each declared step has a strictly
  increasing source-line number, so the executor's order matches
  the reading order in the file)
- no step body raises at import time (the import-smoke test covers
  that for the module, but here we also instantiate the class)

The expected step list below IS the saga's load-bearing contract.
Any time the saga's step set changes intentionally, update this
list. Changes here = changes to the bedrock-init flow, which is
exactly the kind of change that wants a deliberate test diff.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

# Import side-effect registers the saga.
from bedrock_d.install import cluster_init as _ci  # noqa: F401
from bedrock_d.orchestrator.sagas import SAGAS
from bedrock_d.orchestrator.sagas.executor import _ordered_steps


EXPECTED_STEPS = [
    # Identity + filesystem prep
    "prepare_dirs",
    "allocate_identity",
    "write_cluster_key",
    "write_bootstrap_cluster_json",
    # Observability binaries + units
    "install_obs_binaries",
    "install_exporters",
    "write_obs_services",
    "start_obs_services",
    # Storage
    "provision_storage_n1",
    # Cluster CA (mTLS) — must exist before rqlite starts
    "bootstrap_cluster_ca",
    # rqlite
    "render_rqlited_env",
    "start_rqlited",
    "apply_schema",
    "seed_cluster_state",
    "mirror_tier_state",
    # bedrock-d
    "start_bedrock_d",
    # SeaweedFS
    "seaweedfs_install",
    "seaweedfs_configs",
    "seaweedfs_start_local",
    "seaweedfs_start_filer",
    "seaweedfs_init_collections",
    "seed_iso_library",
]


def test_cluster_init_saga_is_registered():
    assert "cluster_init" in SAGAS
    assert SAGAS["cluster_init"] is _ci.ClusterInit
    assert _ci.ClusterInit._saga_kind == "cluster_init"


def test_cluster_init_saga_can_be_instantiated():
    # No __init__ args; the executor needs to instantiate this fresh.
    inst = _ci.ClusterInit()
    assert inst is not None


def test_cluster_init_step_set_matches_documented_flow():
    """If you change the saga's step list, update EXPECTED_STEPS too.
    The list IS the bedrock-init contract."""
    declared = [name for (name, _fn) in _ordered_steps(_ci.ClusterInit)]
    assert declared == EXPECTED_STEPS, (
        "ClusterInit step list drifted from the documented flow.\n"
        f"declared:  {declared}\n"
        f"expected:  {EXPECTED_STEPS}"
    )


def test_rqlite_steps_come_after_provision_storage():
    """Architectural invariant from docs/codebase-rewrite-plan §9:
    rqlite is started AFTER local storage is provisioned. This was
    the e2e regression root cause — tier_storage was writing to
    rqlite before rqlite was up."""
    declared = [name for (name, _fn) in _ordered_steps(_ci.ClusterInit)]
    assert "provision_storage_n1" in declared
    assert "start_rqlited" in declared
    assert declared.index("provision_storage_n1") < declared.index("start_rqlited"), (
        "provision_storage_n1 must run before start_rqlited"
    )


def test_seed_steps_come_after_start_rqlited():
    """Anything that writes to rqlite must come after start_rqlited."""
    declared = [name for (name, _fn) in _ordered_steps(_ci.ClusterInit)]
    rqlite_idx = declared.index("start_rqlited")
    for after_step in ("apply_schema", "seed_cluster_state",
                       "mirror_tier_state"):
        assert declared.index(after_step) > rqlite_idx, (
            f"{after_step} must run after start_rqlited"
        )


def test_seaweedfs_filer_after_bedrock_d():
    """The filer is a singleton; cluster_arbiter (running inside
    bedrock-d) owns its lifecycle on N≥2. At N=1 we can start it
    directly. Either way, bedrock-d must be running before we
    declare the filer up."""
    declared = [name for (name, _fn) in _ordered_steps(_ci.ClusterInit)]
    assert declared.index("start_bedrock_d") < declared.index(
        "seaweedfs_start_filer")


def test_no_duplicate_step_names():
    declared = [name for (name, _fn) in _ordered_steps(_ci.ClusterInit)]
    assert len(declared) == len(set(declared)), (
        f"duplicate step names: "
        f"{[n for n in declared if declared.count(n) > 1]}"
    )
