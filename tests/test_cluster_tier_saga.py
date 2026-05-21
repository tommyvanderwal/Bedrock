"""Contract tests for the cluster-tier DRBD sagas.

Both sagas are thin orchestration over ``tier_storage`` helpers; the
heavy lifting (LV creation, drbdadm shell-outs, fstab edits) is
unit-tested separately. The tests here lock in:

  * The sagas are registered under their kind names.
  * The step lists match the documented flow (precondition,
    promote, record state on the master; wait, join on the peer).
  * Step names are unique within each saga (a re-named step would
    silently break resume).
  * ``cluster_tier_promote_master.check_preconditions`` is honest:
    refuses to run on a node that isn't the current mgmt-master,
    and bails out gracefully when the tier is already in ``drbd``
    mode.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bedrock_d.install import cluster_tier  # noqa: E402  (after sys.path)
from bedrock_d.orchestrator.sagas import SAGAS  # noqa: E402
from bedrock_d.orchestrator.sagas.executor import _ordered_steps  # noqa: E402


PROMOTE_STEPS = [
    "check_preconditions",
    "promote_local_to_drbd",
    "record_tier_state_rqlite",
]

JOIN_STEPS = [
    "wait_master_drbd",
    "join_as_secondary",
]


class ClusterTierSagasRegistered(unittest.TestCase):
    def test_promote_saga_registered(self):
        self.assertIn("cluster_tier_promote_master", SAGAS)
        self.assertIs(SAGAS["cluster_tier_promote_master"],
                      cluster_tier.ClusterTierPromoteMaster)

    def test_join_saga_registered(self):
        self.assertIn("cluster_tier_join_peer", SAGAS)
        self.assertIs(SAGAS["cluster_tier_join_peer"],
                      cluster_tier.ClusterTierJoinPeer)


class StepFlowContract(unittest.TestCase):
    def test_promote_step_set_matches_documented_flow(self):
        declared = [name for (name, _fn)
                    in _ordered_steps(cluster_tier.ClusterTierPromoteMaster)]
        self.assertEqual(declared, PROMOTE_STEPS)

    def test_join_step_set_matches_documented_flow(self):
        declared = [name for (name, _fn)
                    in _ordered_steps(cluster_tier.ClusterTierJoinPeer)]
        self.assertEqual(declared, JOIN_STEPS)

    def test_no_duplicate_step_names(self):
        """Resume relies on unique step names per saga — a typo that
        duplicates a name would silently break the resume path."""
        for cls in (cluster_tier.ClusterTierPromoteMaster,
                    cluster_tier.ClusterTierJoinPeer):
            names = [n for (n, _f) in _ordered_steps(cls)]
            self.assertEqual(len(names), len(set(names)),
                             f"duplicate step name in {cls.__name__}: {names}")


class CheckPreconditionsBehaviour(unittest.TestCase):
    """The first step refuses to run when assumptions don't hold —
    it's the safety net protecting the disruptive promote step that
    follows it."""

    def _patch_cluster(self, cluster_dict, self_node="bedrock-SELF"):
        def _load():
            return cluster_dict
        # Patch the module-level loader so the step sees our fake.
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(cluster_tier, "_load_cluster",
                          side_effect=_load).start()
        mock.patch.object(cluster_tier, "_self_node_name",
                          return_value=self_node).start()

    def test_refuses_when_not_master(self):
        self._patch_cluster(
            {"mgmt_master": "bedrock-OTHER",
             "nodes": {"bedrock-OTHER": {"loopback_ip": "100.1.1.3"},
                       "bedrock-SELF":  {"loopback_ip": "100.1.1.1"}},
             "tiers": {"critical": {"mode": "local"}}})
        saga = cluster_tier.ClusterTierPromoteMaster()
        with self.assertRaisesRegex(RuntimeError, "no longer mgmt_master"):
            saga.step_check_preconditions(
                {"peer_node": "bedrock-OTHER",
                 "peer_loopback": "100.1.1.3"})

    def test_already_drbd_short_circuits(self):
        self._patch_cluster(
            {"mgmt_master": "bedrock-SELF",
             "nodes": {"bedrock-SELF": {"loopback_ip": "100.1.1.1"},
                       "bedrock-OTHER": {"loopback_ip": "100.1.1.3"}},
             "tiers": {"critical": {"mode": "drbd"}}})
        saga = cluster_tier.ClusterTierPromoteMaster()
        ctx = {"peer_node": "bedrock-OTHER",
               "peer_loopback": "100.1.1.3"}
        saga.step_check_preconditions(ctx)
        self.assertTrue(ctx.get("_already_drbd"),
                        "already-drbd should set the short-circuit flag")

    def test_refuses_when_peer_missing_from_cluster_json(self):
        self._patch_cluster(
            {"mgmt_master": "bedrock-SELF",
             "nodes": {"bedrock-SELF": {"loopback_ip": "100.1.1.1"}},
             "tiers": {"critical": {"mode": "local"}}})
        saga = cluster_tier.ClusterTierPromoteMaster()
        with self.assertRaisesRegex(RuntimeError, "not in cluster.json"):
            saga.step_check_preconditions(
                {"peer_node": "bedrock-GHOST",
                 "peer_loopback": "100.1.1.99"})

    def test_refuses_when_peer_params_missing(self):
        self._patch_cluster(
            {"mgmt_master": "bedrock-SELF",
             "nodes": {"bedrock-SELF": {"loopback_ip": "100.1.1.1"}},
             "tiers": {"critical": {"mode": "local"}}})
        saga = cluster_tier.ClusterTierPromoteMaster()
        with self.assertRaisesRegex(RuntimeError, "missing peer params"):
            saga.step_check_preconditions({})


if __name__ == "__main__":
    unittest.main()
