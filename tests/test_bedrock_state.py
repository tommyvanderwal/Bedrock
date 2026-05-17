"""Unit tests for installer/lib/bedrock_state.py.

Mocks the rqlite_client so the tests run without a live rqlite —
asserts on the SQL each helper emits and the parameters it binds.
This catches schema/helper drift before integration testing.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "installer"))

from lib import bedrock_state as bs       # noqa: E402
from lib import rqlite_client as rc       # noqa: E402


def _fake_client():
    """Build a MagicMock that pretends to be an RqliteClient.
    `execute()` records calls; `query_one()` returns a canned
    revision so bump_revision()'s read-back has something."""
    c = mock.MagicMock()
    c.execute = mock.MagicMock(return_value=[{"rows_affected": 1}])
    c.query_one = mock.MagicMock(
        return_value={"revision": 42},
    )
    return c


def _executed_sql(client) -> list[str]:
    """Pull the SQL strings out of every execute() call on `client`."""
    sql: list[str] = []
    for call in client.execute.call_args_list:
        arg = call.args[0]
        if isinstance(arg, str):
            sql.append(arg)
        elif isinstance(arg, list):
            for stmt in arg:
                if isinstance(stmt, str):
                    sql.append(stmt)
                elif isinstance(stmt, list) and stmt:
                    sql.append(stmt[0])
    return sql


class TestClusterIdentity(unittest.TestCase):
    def test_cluster_init_upserts_singleton(self):
        c = _fake_client()
        bs.cluster_init("uuid-1", "test-cluster", client=c)
        sqls = _executed_sql(c)
        self.assertTrue(any("INSERT INTO cluster_info" in s for s in sqls))
        self.assertTrue(any("ON CONFLICT(id) DO UPDATE" in s for s in sqls))

    def test_set_mgmt_master_atomic_role_flip(self):
        """set_mgmt_master must (a) update cluster_info, (b) demote any
        other mgmt+compute to compute, (c) promote the new master.
        All in one transactional batch."""
        c = _fake_client()
        bs.set_mgmt_master("sim-2", client=c)
        sqls = _executed_sql(c)
        self.assertTrue(any("UPDATE cluster_info SET mgmt_master" in s for s in sqls))
        self.assertTrue(any("role = 'compute'" in s for s in sqls))
        self.assertTrue(any("role = 'mgmt+compute'" in s for s in sqls))


class TestMembership(unittest.TestCase):
    def test_node_register_upserts(self):
        c = _fake_client()
        bs.node_register("sim-1", "192.168.2.201", "10.99.0.1",
                         role="mgmt+compute",
                         pubkey="ssh-key", bedrock_pubkey="bk",
                         client=c)
        sqls = _executed_sql(c)
        self.assertTrue(any("INSERT INTO nodes" in s for s in sqls))
        self.assertTrue(any("ON CONFLICT(node_name)" in s for s in sqls))
        # Caller passed kwargs — check they're in the params of the
        # FIRST call (the INSERT). Subsequent calls are bump_revision's
        # UPDATE + the readback SELECT, neither of which carries params.
        params = c.execute.call_args_list[0].kwargs.get("params") or []
        self.assertIn("sim-1", params)
        self.assertIn("192.168.2.201", params)
        self.assertIn("10.99.0.1", params)
        self.assertIn("mgmt+compute", params)

    def test_node_unregister_drops_node_and_drbd_ids(self):
        c = _fake_client()
        bs.node_unregister("sim-3", reason="decommission", client=c)
        sqls = _executed_sql(c)
        # Must touch nodes + tier_drbd_node_ids + tiers (peers list)
        self.assertTrue(any("DELETE FROM nodes" in s for s in sqls))
        self.assertTrue(any("DELETE FROM tier_drbd_node_ids" in s for s in sqls))
        self.assertTrue(any("UPDATE tiers SET peers" in s for s in sqls))

    def test_node_loopback_only_updates_existing_node(self):
        c = _fake_client()
        bs.node_loopback("sim-2", "100.42.42.2", client=c)
        sqls = _executed_sql(c)
        # UPDATE only — node_loopback is set after node_register
        self.assertTrue(any(s.startswith("UPDATE nodes") for s in sqls))
        self.assertFalse(any("INSERT INTO nodes" in s for s in sqls))

    def test_node_maintenance_flag_set(self):
        c = _fake_client()
        bs.node_maintenance("sim-2", on=True, client=c)
        params = c.execute.call_args_list[0].kwargs.get("params") or []
        self.assertEqual(params[0], 1)


class TestTiers(unittest.TestCase):
    def test_tier_state_serialises_peers_as_json(self):
        c = _fake_client()
        bs.tier_state("scratch", "drbd-nfs",
                      master="sim-1",
                      peers=["sim-1", "sim-2"], client=c)
        params = c.execute.call_args_list[0].kwargs.get("params") or []
        # peers position in INSERT is 4th positional (tier, mode, master, peers, ...)
        self.assertIn(json.dumps(["sim-1", "sim-2"]), params)

    def test_drbd_node_id_assigned_idempotent(self):
        c = _fake_client()
        bs.drbd_node_id_assigned("tier-bulk", "sim-2", 2, client=c)
        sqls = _executed_sql(c)
        self.assertTrue(any("INSERT INTO tier_drbd_node_ids" in s for s in sqls))
        self.assertTrue(any("ON CONFLICT(tier_name, node_name)" in s for s in sqls))


class TestWitnesses(unittest.TestCase):
    def test_witness_register_with_backend(self):
        c = _fake_client()
        bs.witness_register("mikrotik-1", "192.168.2.252:12321",
                            "deadbeef" * 8, "cafef00d" * 8,
                            backend="echo", client=c)
        params = c.execute.call_args_list[0].kwargs.get("params") or []
        self.assertIn("mikrotik-1", params)
        self.assertIn("echo", params)


class TestParams(unittest.TestCase):
    def test_param_value_round_trips_via_json(self):
        c = _fake_client()
        bs.param_change("witness_ttl_ms", 5000, client=c)
        params = c.execute.call_args_list[0].kwargs.get("params") or []
        self.assertEqual(params[1], "5000")  # int → JSON string

    def test_param_dict_value(self):
        c = _fake_client()
        bs.param_change("backup_grace", {"window_min": 15, "fuzz_pct": 5},
                        client=c)
        params = c.execute.call_args_list[0].kwargs.get("params") or []
        self.assertEqual(
            json.loads(params[1]),
            {"window_min": 15, "fuzz_pct": 5},
        )


class TestVmIntents(unittest.TestCase):
    def test_vm_create_intent_bumps_revision_first(self):
        """vm_create_intent grabs the new revision BEFORE writing the
        vms row, so intent_index reflects the same Raft commit."""
        c = _fake_client()
        # Mock bump_revision via the same client. bump_revision()
        # internally does an UPDATE + a query_one; the helper returns
        # whatever query_one returns.
        with mock.patch.object(rc, "bump_revision",
                                return_value=7) as bump:
            bs.vm_create_intent("vm-test", "cattle", "sim-1",
                                ram_mb=256, disk_gb=1, client=c)
        # Bump called once before the INSERT
        bump.assert_called_once_with(c)
        # The vms row uses revision=7 as intent_index
        params = c.execute.call_args_list[0].kwargs.get("params") or []
        self.assertIn(7, params)


class TestMeshPaths(unittest.TestCase):
    def test_path_key_canonical_order(self):
        """The (a, b) tuple gets sorted before the path_key is built
        so observer-order doesn't matter."""
        c = _fake_client()
        # Reverse-order args — should produce the same key as the
        # canonical (a < b) order would
        bs.link_up(node_a="sim-2", nic_a="enp3s0",
                   node_b="sim-1", nic_b="enp2s0",
                   link_addr_a="169.254.30.2",
                   link_addr_b="169.254.20.1",
                   speed_mbps=10000, rtt_us=100,
                   client=c)
        params = c.execute.call_args_list[0].kwargs.get("params") or []
        # path_key should start with sim-1|enp2s0|... (canonical order)
        self.assertTrue(
            params[0].startswith("sim-1|enp2s0|sim-2|enp3s0"),
            f"path_key={params[0]!r} not canonicalised",
        )
        # link_addr_a / link_addr_b also swapped to match
        self.assertEqual(params[3], "169.254.20.1")  # addr for sim-1
        self.assertEqual(params[6], "169.254.30.2")  # addr for sim-2

    def test_link_down_uses_canonical_key(self):
        c = _fake_client()
        bs.link_down(node_a="sim-2", nic_a="enp3s0",
                     node_b="sim-1", nic_b="enp2s0",
                     client=c)
        params = c.execute.call_args_list[0].kwargs.get("params") or []
        self.assertTrue(params[0].startswith("sim-1|enp2s0|sim-2|enp3s0"))


class TestObsBackends(unittest.TestCase):
    def test_obs_backends_set_replaces_all_rows(self):
        """obs_backends_set is replace-all-rows, not partial update."""
        c = _fake_client()
        bs.obs_backends_set(metrics=["sim-1", "sim-2"],
                            logs=["sim-1", "sim-2"],
                            client=c)
        sqls = _executed_sql(c)
        # First statement deletes everything
        self.assertEqual(sqls[0], "DELETE FROM obs_backends")
        # Then 4 INSERTs (2 metrics + 2 logs)
        inserts = [s for s in sqls if s.startswith("INSERT INTO obs_backends")]
        self.assertEqual(len(inserts), 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
