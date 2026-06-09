"""Unit tests for lib/rqlite_setup.py.

Uses a temporary directory for the cluster.json / state.json /
rqlited.env paths so the tests don't touch the host system.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "installer"))

from lib import rqlite_setup as rs  # noqa: E402


class TestRenderEnvFile(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cluster_path = self.tmp / "cluster.json"
        self.state_path = self.tmp / "state.json"
        self.env_path = self.tmp / "rqlited.env"
        self.data_dir = self.tmp / "rqlite-data"

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, cluster, state):
        self.cluster_path.write_text(json.dumps(cluster))
        self.state_path.write_text(json.dumps(state))

    def _render(self):
        return rs.render_env_file(
            cluster_path=self.cluster_path,
            state_path=self.state_path,
            env_path=self.env_path,
            data_dir=self.data_dir,
        )

    def test_n1_solo_mgmt_master_bootstraps(self):
        """Single-node mgmt master at fresh init — expect bootstrap
        flag, no join."""
        self._write(
            cluster={
                "cluster_name": "test",
                "cluster_uuid": "u-1",
                "mgmt_master": "sim-1",
                "nodes": {"sim-1": {"loopback_ip": "100.42.42.1"}},
            },
            state={
                "node_name": "sim-1",
                "loopback_ip": "100.42.42.1",
                "role": "mgmt+compute",
            },
        )
        env = self._render()
        self.assertEqual(env["BEDROCK_RQLITED_NODE_ID"], "1")
        self.assertEqual(env["BEDROCK_RQLITED_BIND_IP"], "100.42.42.1")
        self.assertEqual(env["BEDROCK_RQLITED_BOOTSTRAP_FLAG"], "-bootstrap-expect 1")
        self.assertEqual(env["BEDROCK_RQLITED_JOIN_FLAG"], "")

    def test_n2_follower_joins_master(self):
        """2-node cluster, follower — expect join flag pointing at
        master's loopback, no bootstrap."""
        self._write(
            cluster={
                "mgmt_master": "sim-1",
                "nodes": {
                    "sim-1": {"loopback_ip": "100.42.42.1"},
                    "sim-2": {"loopback_ip": "100.42.42.2"},
                },
            },
            state={
                "node_name": "sim-2",
                "loopback_ip": "100.42.42.2",
                "role": "compute",
            },
        )
        env = self._render()
        self.assertEqual(env["BEDROCK_RQLITED_NODE_ID"], "2")
        self.assertEqual(env["BEDROCK_RQLITED_BIND_IP"], "100.42.42.2")
        self.assertEqual(env["BEDROCK_RQLITED_BOOTSTRAP_FLAG"], "")
        self.assertEqual(
            env["BEDROCK_RQLITED_JOIN_FLAG"],
            "-join 100.42.42.1:4002",
        )

    def test_n3_master_joins_existing_cluster(self):
        """3-node case from the master's perspective — master still
        joins, NOT bootstraps (cluster already formed). Join flag
        lists all peers in stable sorted order."""
        self._write(
            cluster={
                "mgmt_master": "sim-1",
                "nodes": {
                    "sim-1": {"loopback_ip": "100.42.42.1"},
                    "sim-2": {"loopback_ip": "100.42.42.2"},
                    "sim-3": {"loopback_ip": "100.42.42.3"},
                },
            },
            state={
                "node_name": "sim-1",
                "loopback_ip": "100.42.42.1",
                "role": "mgmt+compute",
            },
        )
        env = self._render()
        self.assertEqual(env["BEDROCK_RQLITED_NODE_ID"], "1")
        # Bootstrap is OFF — cluster already has multiple nodes.
        self.assertEqual(env["BEDROCK_RQLITED_BOOTSTRAP_FLAG"], "")
        # Both other peers listed in sorted order.
        self.assertEqual(
            env["BEDROCK_RQLITED_JOIN_FLAG"],
            "-join 100.42.42.2:4002,100.42.42.3:4002",
        )

    def test_node_ids_are_stable_sorted_index(self):
        """The node-id is the 1-based sorted-name index. Verifies the
        invariant: same node_name → same node_id across renders, no
        matter what order entries arrived in cluster.json."""
        self._write(
            cluster={
                "mgmt_master": "alpha",
                "nodes": {
                    "alpha":   {"loopback_ip": "100.42.42.1"},
                    "bravo":   {"loopback_ip": "100.42.42.2"},
                    "charlie": {"loopback_ip": "100.42.42.3"},
                },
            },
            state={"node_name": "bravo", "loopback_ip": "100.42.42.2",
                   "role": "compute"},
        )
        env = self._render()
        self.assertEqual(env["BEDROCK_RQLITED_NODE_ID"], "2")  # alpha=1, bravo=2

    def test_missing_state_raises(self):
        self._write(
            cluster={
                "nodes": {"sim-1": {"loopback_ip": "100.42.42.1"}},
            },
            state={},  # no node_name / loopback yet
        )
        with self.assertRaises(RuntimeError) as cm:
            self._render()
        self.assertIn("cannot render env yet", str(cm.exception))

    def test_node_not_in_cluster_yet_raises(self):
        """Race: state.json has us, but cluster.json hasn't replicated
        our registration yet. Refuse to render (don't fabricate a
        node-id)."""
        self._write(
            cluster={
                "nodes": {"sim-1": {"loopback_ip": "100.42.42.1"}},
            },
            state={"node_name": "sim-2", "loopback_ip": "100.42.42.2",
                   "role": "compute"},
        )
        with self.assertRaises(RuntimeError) as cm:
            self._render()
        self.assertIn("not in cluster.json yet", str(cm.exception))

    def test_no_peers_and_not_master_raises(self):
        """Pathological case: cluster.json has us as the only node
        but we're NOT the master. Don't auto-bootstrap; surface the
        error so the operator can fix the snapshot."""
        self._write(
            cluster={
                "mgmt_master": "someone-else",
                "nodes": {"sim-2": {"loopback_ip": "100.42.42.2"}},
            },
            state={"node_name": "sim-2", "loopback_ip": "100.42.42.2",
                   "role": "compute"},
        )
        with self.assertRaises(RuntimeError) as cm:
            self._render()
        self.assertIn("no peers", str(cm.exception))

    def test_idempotent_render_produces_identical_file(self):
        self._write(
            cluster={
                "mgmt_master": "sim-1",
                "nodes": {
                    "sim-1": {"loopback_ip": "100.42.42.1"},
                    "sim-2": {"loopback_ip": "100.42.42.2"},
                },
            },
            state={"node_name": "sim-2", "loopback_ip": "100.42.42.2",
                   "role": "compute"},
        )
        self._render()
        first = self.env_path.read_text()
        self._render()
        second = self.env_path.read_text()
        self.assertEqual(first, second)

    def test_creates_data_dir(self):
        self.assertFalse(self.data_dir.exists())
        self._write(
            cluster={
                "mgmt_master": "sim-1",
                "nodes": {"sim-1": {"loopback_ip": "100.42.42.1"}},
            },
            state={"node_name": "sim-1", "loopback_ip": "100.42.42.1",
                   "role": "mgmt+compute"},
        )
        self._render()
        self.assertTrue(self.data_dir.exists())
        self.assertTrue(self.data_dir.is_dir())


if __name__ == "__main__":
    unittest.main(verbosity=2)
