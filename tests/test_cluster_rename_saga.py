"""Contract tests for the cluster_rename saga.

Saga is one validation step + one rqlite UPDATE; the meat of the
test is that validate_request enforces the allowed-name policy
(short rejection of empties, over-length, shell-unsafe chars) and
that the saga registers under the right kind name with the right
step list.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bedrock_d.cluster import rename as _rn  # noqa: E402
from bedrock_d.orchestrator.sagas import SAGAS  # noqa: E402
from bedrock_d.orchestrator.sagas.executor import _ordered_steps  # noqa: E402


EXPECTED_STEPS = [
    "validate_request",
    "write_rqlite_cluster_info",
]


class SagaRegistered(unittest.TestCase):
    def test_registered_under_kind(self):
        self.assertIn("cluster_rename", SAGAS)
        self.assertIs(SAGAS["cluster_rename"], _rn.ClusterRename)

    def test_step_set_matches_documented_flow(self):
        declared = [n for (n, _f) in _ordered_steps(_rn.ClusterRename)]
        self.assertEqual(declared, EXPECTED_STEPS)


class ValidateRequest(unittest.TestCase):
    """The first step is the safety net — every name that makes it
    past here ends up in cluster.json + mDNS TXT + state.json. The
    allowed-char policy keeps the value safe across all those
    consumers without per-consumer escaping."""

    def setUp(self):
        self.saga = _rn.ClusterRename()

    def test_accepts_simple_name(self):
        ctx = {"new_name": "bedrock-prod"}
        self.saga.step_validate_request(ctx)
        self.assertEqual(ctx["new_name"], "bedrock-prod")

    def test_strips_surrounding_whitespace(self):
        ctx = {"new_name": "  bedrock-prod  "}
        self.saga.step_validate_request(ctx)
        self.assertEqual(ctx["new_name"], "bedrock-prod")

    def test_accepts_underscore_dot_dash(self):
        for name in ("a_b", "a.b", "a-b", "Cluster_1.0-beta"):
            with self.subTest(name=name):
                ctx = {"new_name": name}
                self.saga.step_validate_request(ctx)

    def test_rejects_empty(self):
        for ctx in ({"new_name": ""}, {"new_name": "   "}, {}):
            with self.subTest(ctx=ctx):
                with self.assertRaises(ValueError):
                    self.saga.step_validate_request(dict(ctx))

    def test_rejects_too_long(self):
        ctx = {"new_name": "a" * 65}
        with self.assertRaises(ValueError):
            self.saga.step_validate_request(ctx)

    def test_rejects_shell_metas(self):
        for bad in ("a$b", "a;b", "a/b", "a b", "a\\b", "a\"b", "a$(rm)"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.saga.step_validate_request({"new_name": bad})

    def test_accepts_exactly_64_chars(self):
        ctx = {"new_name": "a" * 64}
        self.saga.step_validate_request(ctx)


class WriteStepCallsSetClusterName(unittest.TestCase):
    """write_rqlite_cluster_info delegates the actual write to
    bedrock_state.set_cluster_name. The test patches the state
    re-export so we don't need a running rqlite."""

    def test_calls_set_cluster_name_with_canonicalised_name(self):
        from bedrock_d import state as _st
        # Patch RqliteClient + set_cluster_name
        with mock.patch.object(_st, "set_cluster_name",
                               return_value=42) as set_name, \
             mock.patch.object(_st, "RqliteClient") as rc_cls:
            rc_cls.return_value.__enter__.return_value = "fake-client"
            saga = _rn.ClusterRename()
            saga.step_write_rqlite_cluster_info({"new_name": "bedrock-prod"})
            set_name.assert_called_once_with("bedrock-prod",
                                             client="fake-client")


if __name__ == "__main__":
    unittest.main()
