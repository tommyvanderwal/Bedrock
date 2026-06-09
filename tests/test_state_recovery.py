"""Unit tests for the state.json self-heal (lib/state.py).

Locks in the fix for the sim-4 2026-05-29 brick: a 0-byte/corrupt
state.json (power-loss in save()'s rename window) must NOT crash callers
and must self-heal this node's identity from the surviving local
cluster.json + hostname. Without this, netd.load_state() and
rqlite_setup.render_env_file() refused to start (node_name='') and
bedrock-rqlited crash-looped to the systemd start-limit.
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "installer"))

from lib import state as st  # noqa: E402


CLUSTER_JSON = {
    "cluster_uuid": "03bee55d-aa66-471c-a857-7228c8c80941",
    "cluster_name": "test-failover",
    "mgmt_master": "bedrock-91db91",
    "nodes": {
        "bedrock-58ff6c": {
            "host": "192.168.2.107",
            "loopback_ip": "100.112.135.4",
            "role": "compute",
        },
        "bedrock-91db91": {
            "host": "192.168.2.105",
            "loopback_ip": "100.112.135.1",
            "role": "mgmt+compute",
        },
    },
}


@pytest.fixture
def staged(tmp_path, monkeypatch):
    """Point state.py's file paths at tmp + pin the hostname to a node
    that exists in CLUSTER_JSON."""
    state_file = tmp_path / "state.json"
    cluster_file = tmp_path / "cluster.json"
    monkeypatch.setattr(st, "STATE_FILE", state_file)
    monkeypatch.setattr(st, "CLUSTER_JSON_FILE", cluster_file)

    class _Uname:
        nodename = "bedrock-58ff6c"

    monkeypatch.setattr(st.os, "uname", lambda: _Uname())
    return state_file, cluster_file


def test_load_tolerates_zero_byte_file(staged):
    state_file, _ = staged
    state_file.write_text("")          # the exact 0-byte truncation
    assert st.load() == {}             # must NOT raise JSONDecodeError


def test_load_tolerates_corrupt_json(staged):
    state_file, _ = staged
    state_file.write_text("{ partial")
    assert st.load() == {}


def test_recover_rebuilds_identity_from_cluster_json(staged):
    state_file, cluster_file = staged
    state_file.write_text("")          # bricked state.json
    cluster_file.write_text(json.dumps(CLUSTER_JSON))

    healed = st.recover_identity_from_cluster_json()

    assert healed["node_name"] == "bedrock-58ff6c"
    assert healed["cluster_uuid"] == CLUSTER_JSON["cluster_uuid"]
    assert healed["loopback_ip"] == "100.112.135.4"   # this node's octet .4
    assert healed["role"] == "compute"
    assert healed["bootstrap_done"] is True
    # Persisted atomically so the next reader sees it.
    on_disk = json.loads(state_file.read_text())
    assert on_disk["loopback_ip"] == "100.112.135.4"


def test_recover_does_not_seed_stale_recovery_fields(staged):
    """believed_master + arbiter_uuid_history must NOT be reconstructed
    from cluster.json — they are election/DRBD-derived and a stale hint
    is worse than none."""
    state_file, cluster_file = staged
    state_file.write_text("")
    cluster_file.write_text(json.dumps(CLUSTER_JSON))

    healed = st.recover_identity_from_cluster_json()

    assert "believed_master" not in healed
    assert not healed.get("arbiter_uuid_history")


def test_recover_is_noop_when_already_healthy(staged):
    state_file, cluster_file = staged
    good = {
        "node_name": "bedrock-58ff6c",
        "cluster_uuid": "u",
        "loopback_ip": "100.112.135.4",
        "bootstrap_done": True,
    }
    state_file.write_text(json.dumps(good))
    cluster_file.write_text(json.dumps(CLUSTER_JSON))

    healed = st.recover_identity_from_cluster_json()
    assert healed == good   # untouched — no spurious cluster_name graft


def test_recover_leaves_state_when_cluster_json_also_missing(staged):
    """If cluster.json can't supply the essentials, do NOT persist a
    half-baked state.json — surface the broken state for the caller."""
    state_file, _ = staged
    state_file.write_text("")          # both files effectively empty
    healed = st.recover_identity_from_cluster_json()
    # node_name falls back to hostname, but no cluster_uuid/loopback →
    # not all identity present → not persisted.
    assert not all(healed.get(k) for k in st._IDENTITY_KEYS)
    assert state_file.read_text() == ""    # untouched


def test_load_or_recover_end_to_end(staged):
    state_file, cluster_file = staged
    state_file.write_text("")
    cluster_file.write_text(json.dumps(CLUSTER_JSON))

    st_dict = st.load_or_recover()
    assert st_dict["cluster_uuid"] == CLUSTER_JSON["cluster_uuid"]
    assert st_dict["loopback_ip"] == "100.112.135.4"
