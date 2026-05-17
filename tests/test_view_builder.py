"""Unit tests for installer/lib/view_builder.py.

Mocks the rqlite_client and exercises build_snapshot() end-to-end —
asserts the dict shape, key naming, JSON deserialization, and
projection helpers (_cluster_view / _state_view) match what
downstream consumers (mgmt/app.py, orchestrator.py) expect.

The shape compat is the crucial invariant: every downstream reader
that previously consumed the log-fold output must see the same
keys + types from the rqlite-backed builder.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "installer"))

from lib import view_builder as vb        # noqa: E402


class _MockQueryRouter:
    """Map SELECT statements to canned result-sets so build_snapshot
    can be exercised without a live rqlite. Keys are substrings of
    the SQL (build_snapshot's queries are unique enough that prefix-
    matching works)."""

    def __init__(self, routes: dict[str, list[dict]]):
        self._routes = routes

    def __call__(self, sql, params=None, level="weak"):
        for hint, rows in self._routes.items():
            if hint in sql:
                return list(rows)
        return []


def _make_client(routes: dict[str, list[dict]]):
    c = mock.MagicMock()
    router = _MockQueryRouter(routes)
    c.query = mock.MagicMock(side_effect=router)

    def _query_one(sql, params=None, level="weak"):
        rows = router(sql, params, level)
        return rows[0] if rows else None

    c.query_one = mock.MagicMock(side_effect=_query_one)
    return c


class TestEmptySnapshot(unittest.TestCase):
    def test_empty_snapshot_shape(self):
        s = vb.empty_snapshot()
        self.assertEqual(s["cluster_name"], None)
        self.assertEqual(s["cluster_uuid"], None)
        self.assertEqual(s["mgmt_master"], None)
        self.assertEqual(s["log_index"], 0)
        # Every top-level key downstream reads must exist:
        for key in ("nodes", "tiers", "witnesses", "params",
                    "vms", "backup_targets", "paths",
                    "operators", "join_requests"):
            self.assertIn(key, s)
            self.assertIsInstance(s[key], dict)
        # obs_backends is a 2-stack dict, not a regular nested dict
        self.assertEqual(s["obs_backends"], {"metrics": [], "logs": []})


class TestBuildSnapshot(unittest.TestCase):
    def test_full_snapshot_assembly(self):
        c = _make_client({
            "FROM cluster_info": [{
                "cluster_uuid": "u-test",
                "cluster_name": "test-cluster",
                "mgmt_master": "sim-1",
            }],
            "FROM bedrock_meta": [{"revision": 42}],
            "FROM nodes": [
                {"node_name": "sim-1", "host": "192.168.2.201",
                 "drbd_ip": "10.99.0.1", "loopback_ip": "100.42.42.1",
                 "role": "mgmt+compute", "pubkey": "k1",
                 "bedrock_pubkey": "bk1", "maintenance": 0},
                {"node_name": "sim-2", "host": "192.168.2.202",
                 "drbd_ip": "10.99.0.2", "loopback_ip": "100.42.42.2",
                 "role": "compute", "pubkey": "k2",
                 "bedrock_pubkey": "bk2", "maintenance": 1},
            ],
            "FROM tiers": [
                {"tier_name": "scratch", "mode": "drbd-nfs",
                 "master": "sim-1",
                 "peers": json.dumps(["sim-1", "sim-2"]),
                 "backend_path": "/var/lib/bedrock/scratch",
                 "garage_endpoint": None,
                 "version": 3},
            ],
            "FROM tier_drbd_node_ids": [
                {"tier_name": "scratch", "node_name": "sim-1", "node_id": 0},
                {"tier_name": "scratch", "node_name": "sim-2", "node_id": 1},
            ],
            "FROM witnesses": [
                {"witness_id": "mikrotik-1",
                 "addr": "192.168.2.252:12321",
                 "witness_pubkey": "wp",
                 "encrypted_witness_key": "ek",
                 "backend": "echo"},
            ],
            "FROM params": [
                {"key": "ttl_ms", "value": "5000"},
                {"key": "label", "value": '"production"'},
            ],
            "FROM operators": [
                {"username": "root", "salt": "s", "password_hash": "h"},
            ],
            "FROM join_requests": [
                {"request_id": "rq-1", "node_name": "sim-3",
                 "host": "192.168.2.203",
                 "bedrock_pubkey": "bpk",
                 "x25519_eph_pubkey": "eph",
                 "fingerprint": "fp",
                 "state": "approved",
                 "master_eph_pubkey": "mep", "ciphertext": "ct",
                 "nonce": "n", "reason": ""},
            ],
            "FROM vms": [
                {"vm_name": "vm-test",
                 "vm_type": "cattle", "host": "sim-1",
                 "ram_mb": 256, "disk_gb": 1,
                 "state": "running",
                 "intent_index": 7, "fail_reason": None,
                 "backup_schedule": None,
                 "last_backup_error": None,
                 "last_restore": None,
                 "last_restore_err": None},
            ],
            "FROM vm_backups": [
                {"vm_name": "vm-test",
                 "primary_kopia_id": "kid-1",
                 "disks": json.dumps([{"target_dev": "vda",
                                       "lv_path": "/dev/vg/lv",
                                       "kopia_snapshot_id": "kid-1",
                                       "bytes_added": 1024}]),
                 "target_id": "main",
                 "source_node": "sim-1",
                 "bytes_added": 1024,
                 "duration_s": 5.0,
                 "label": "auto-20260518T000000",
                 "fs_freeze_used": 1,
                 "ts_index": 9},
            ],
            "FROM backup_targets": [
                {"target_id": "main", "kind": "kopia-s3",
                 "s3_endpoint": "http://localhost:8333",
                 "s3_bucket": "backups",
                 "s3_region": "us-east-1",
                 "s3_disable_tls": 0,
                 "s3_disable_tls_verification": 0,
                 "filesystem_path": "",
                 "override_source_prefix": "",
                 "cache_directory": ""},
            ],
            "FROM paths": [
                {"path_key": "sim-1|enp2s0|sim-2|enp2s0",
                 "node_a": "sim-1", "nic_a": "enp2s0",
                 "link_addr_a": "169.254.10.1",
                 "node_b": "sim-2", "nic_b": "enp2s0",
                 "link_addr_b": "169.254.10.2",
                 "speed_mbps": 10000, "rtt_us": 100,
                 "observed_at": 1000.0, "up_since": 1000.0},
            ],
            "FROM obs_backends": [
                {"stack": "metrics", "node_name": "sim-1", "position": 0},
                {"stack": "metrics", "node_name": "sim-2", "position": 1},
                {"stack": "logs",    "node_name": "sim-1", "position": 0},
                {"stack": "logs",    "node_name": "sim-2", "position": 1},
            ],
        })
        snap = vb.build_snapshot(client=c)

        # cluster identity
        self.assertEqual(snap["cluster_uuid"], "u-test")
        self.assertEqual(snap["cluster_name"], "test-cluster")
        self.assertEqual(snap["mgmt_master"], "sim-1")
        self.assertEqual(snap["log_index"], 42)

        # nodes — sim-2 maintenance=1 makes it on
        self.assertIn("sim-1", snap["nodes"])
        self.assertIn("sim-2", snap["nodes"])
        self.assertEqual(snap["nodes"]["sim-1"]["loopback_ip"], "100.42.42.1")
        self.assertTrue(snap["nodes"]["sim-2"].get("maintenance"))
        self.assertNotIn("maintenance", snap["nodes"]["sim-1"])  # not set

        # tiers — peers JSON-decoded, drbd_node_ids merged from tier_drbd_node_ids
        scratch = snap["tiers"]["scratch"]
        self.assertEqual(scratch["mode"], "drbd-nfs")
        self.assertEqual(scratch["peers"], ["sim-1", "sim-2"])
        self.assertEqual(scratch["drbd_node_ids"], {"sim-1": 0, "sim-2": 1})

        # witnesses with backend column
        w = snap["witnesses"]["mikrotik-1"]
        self.assertEqual(w["addr"], "192.168.2.252:12321")
        self.assertEqual(w["backend"], "echo")

        # params — JSON-decoded
        self.assertEqual(snap["params"]["ttl_ms"], 5000)
        self.assertEqual(snap["params"]["label"], "production")

        # operators
        self.assertEqual(snap["operators"]["root"],
                         {"salt": "s", "hash": "h"})

        # join_requests — approved gets master_eph_pubkey + ciphertext
        rq = snap["join_requests"]["rq-1"]
        self.assertEqual(rq["state"], "approved")
        self.assertEqual(rq["master_eph_pubkey"], "mep")

        # vm + per-vm backups joined onto the vm
        vm = snap["vms"]["vm-test"]
        self.assertEqual(vm["state"], "running")
        self.assertEqual(vm["intent_index"], 7)
        self.assertEqual(len(vm["backups"]), 1)
        self.assertEqual(vm["backups"][0]["kopia_snapshot_id"], "kid-1")
        self.assertEqual(vm["backups"][0]["bytes_added"], 1024)

        # backup_targets
        self.assertIn("main", snap["backup_targets"])
        self.assertEqual(snap["backup_targets"]["main"]["kind"], "kopia-s3")

        # paths
        self.assertIn("sim-1|enp2s0|sim-2|enp2s0", snap["paths"])

        # obs_backends — ordered by position
        self.assertEqual(snap["obs_backends"]["metrics"], ["sim-1", "sim-2"])
        self.assertEqual(snap["obs_backends"]["logs"],    ["sim-1", "sim-2"])

    def test_empty_cluster(self):
        """No rows anywhere → empty_snapshot shape preserved."""
        c = _make_client({})
        snap = vb.build_snapshot(client=c)
        self.assertEqual(snap["cluster_uuid"], None)
        self.assertEqual(snap["nodes"], {})
        self.assertEqual(snap["log_index"], 0)


class TestProjections(unittest.TestCase):
    """_cluster_view and _state_view shape — what gets written to
    /etc/bedrock/cluster.json and /etc/bedrock/state.json."""

    def _base_snapshot(self):
        return {
            "cluster_name": "test",
            "cluster_uuid": "u-1",
            "mgmt_master": "sim-1",
            "nodes": {
                "sim-1": {"host": "192.168.2.201", "drbd_ip": "10.99.0.1",
                          "loopback_ip": "100.42.42.1",
                          "role": "mgmt+compute"},
                "sim-2": {"host": "192.168.2.202", "drbd_ip": "10.99.0.2",
                          "loopback_ip": "100.42.42.2", "role": "compute"},
            },
            "tiers": {}, "witnesses": {}, "params": {}, "vms": {},
            "backup_targets": {}, "paths": {}, "operators": {},
            "join_requests": {},
            "obs_backends": {"metrics": [], "logs": []},
            "log_index": 5,
        }

    def test_cluster_view_includes_mgmt_master(self):
        snap = self._base_snapshot()
        cv = vb._cluster_view(snap)
        self.assertEqual(cv["mgmt_master"], "sim-1")
        self.assertEqual(cv["log_index"], 5)

    def test_state_view_for_master(self):
        snap = self._base_snapshot()
        sv = vb._state_view(snap, "sim-1")
        self.assertEqual(sv["role"], "mgmt+compute")
        self.assertEqual(sv["loopback_ip"], "100.42.42.1")
        self.assertEqual(sv["mgmt_url"], "http://192.168.2.201:8080")

    def test_state_view_for_follower(self):
        snap = self._base_snapshot()
        sv = vb._state_view(snap, "sim-2")
        self.assertEqual(sv["role"], "compute")
        self.assertEqual(sv["mgmt_ip"], "192.168.2.202")
        # mgmt_url points at the MASTER (sim-1)
        self.assertEqual(sv["mgmt_url"], "http://192.168.2.201:8080")


if __name__ == "__main__":
    unittest.main(verbosity=2)
