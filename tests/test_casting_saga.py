"""Unit tests for the 2-node casting-vote saga decision function (#7).

The saga is split-brain-critical, so these tests ARE the spec: one transition per
call, bar-lowering steps gated on all-nodes-applied, forward/reverse ordering, and
automatic abort-on-master-change via name-binding.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "installer"))

from lib import casting_saga as cs       # noqa: E402


def _cluster(*, master="sim-1", casting=None, epoch=0,
             n1_epoch=None, n2_epoch=None, n3=False, n3_epoch=0,
             witnesses=None, n2_state="active", n2_maint=False):
    """Build a cluster view. applied_epoch defaults to `epoch` (all caught up)
    unless overridden, so tests opt INTO a lagging node."""
    if n1_epoch is None:
        n1_epoch = epoch
    if n2_epoch is None:
        n2_epoch = epoch
    nodes = {
        "sim-1": {"state": "active", "applied_epoch": n1_epoch},
        "sim-2": {"state": n2_state, "applied_epoch": n2_epoch,
                  "maintenance": n2_maint},
    }
    if n3:
        nodes["sim-3"] = {"state": "active", "applied_epoch": n3_epoch}
    return {
        "mgmt_master": master,
        "casting_vote_node": casting,
        "vote_config_epoch": epoch,
        "nodes": nodes,
        "witnesses": witnesses or {},
    }


class TestHelpers(unittest.TestCase):
    def test_active_nodes_excludes_joining_and_maintenance(self):
        c = _cluster(n2_state="joining")
        self.assertEqual(set(cs.active_nodes(c)), {"sim-1"})
        c = _cluster(n2_maint=True)
        self.assertEqual(set(cs.active_nodes(c)), {"sim-1"})

    def test_watermark_is_min_over_active(self):
        c = _cluster(epoch=5, n1_epoch=5, n2_epoch=3)
        self.assertEqual(cs.applied_watermark(c), 3)
        # a joining node does NOT hold the watermark back (excluded)
        c = _cluster(epoch=5, n1_epoch=5, n2_epoch=0, n2_state="joining")
        self.assertEqual(cs.applied_watermark(c), 5)


class TestNotMaster(unittest.TestCase):
    def test_non_master_never_acts(self):
        c = _cluster(master="sim-2", epoch=1,
                     witnesses={"w-1": {"corrupt": True}})
        self.assertIsNone(cs.decide_casting_action(c, "sim-1"))


class TestForward(unittest.TestCase):
    def test_corrupt_witness_arms_casting_to_self(self):
        c = _cluster(casting=None, epoch=0,
                     witnesses={"w-1": {"corrupt": True}})
        self.assertEqual(cs.decide_casting_action(c, "sim-1"),
                         ("arm_casting", "sim-1"))

    def test_armed_but_not_all_applied_waits(self):
        # THE GUARD: casting armed at epoch 1 but the follower still at epoch 0 →
        # must NOT disable the witness yet (would let the master lower its bar
        # while the follower still trusts the old config).
        c = _cluster(casting="sim-1", epoch=1, n1_epoch=1, n2_epoch=0,
                     witnesses={"w-1": {"corrupt": True}})
        self.assertIsNone(cs.decide_casting_action(c, "sim-1"))

    def test_armed_and_all_applied_disables_witness(self):
        c = _cluster(casting="sim-1", epoch=1, n1_epoch=1, n2_epoch=1,
                     witnesses={"w-1": {"corrupt": True}})
        self.assertEqual(cs.decide_casting_action(c, "sim-1"),
                         ("disable_witness", "w-1"))

    def test_forward_complete_is_idle(self):
        c = _cluster(casting="sim-1", epoch=2,
                     witnesses={"w-1": {"corrupt": True, "disabled": True}})
        self.assertIsNone(cs.decide_casting_action(c, "sim-1"))

    def test_disable_one_witness_per_call(self):
        c = _cluster(casting="sim-1", epoch=1,
                     witnesses={"w-1": {"corrupt": True},
                                "w-2": {"corrupt": True}})
        act = cs.decide_casting_action(c, "sim-1")
        self.assertEqual(act[0], "disable_witness")
        self.assertIn(act[1], ("w-1", "w-2"))


class TestReverse(unittest.TestCase):
    def test_recovered_witness_disarms_casting_first(self):
        # witness corrupt cleared (recovered) but still disabled + casting armed →
        # disarm casting FIRST (never re-add the witness before disarming).
        c = _cluster(casting="sim-1", epoch=2,
                     witnesses={"w-1": {"disabled": True}})
        self.assertEqual(cs.decide_casting_action(c, "sim-1"),
                         ("disarm_casting", "sim-1"))

    def test_after_disarm_waits_for_all_applied_before_reenable(self):
        c = _cluster(casting=None, epoch=3, n1_epoch=3, n2_epoch=2,
                     witnesses={"w-1": {"disabled": True}})
        self.assertIsNone(cs.decide_casting_action(c, "sim-1"))

    def test_after_disarm_all_applied_reenables_witness(self):
        c = _cluster(casting=None, epoch=3,
                     witnesses={"w-1": {"disabled": True}})
        self.assertEqual(cs.decide_casting_action(c, "sim-1"),
                         ("enable_witness", "w-1"))

    def test_still_corrupt_witness_is_not_reenabled(self):
        # corrupt + disabled, casting already disarmed → leave it disabled (lied).
        c = _cluster(casting=None, epoch=3, n3=True,   # N=3 so need_casting False
                     witnesses={"w-1": {"corrupt": True, "disabled": True}})
        self.assertIsNone(cs.decide_casting_action(c, "sim-1"))


class TestMasterChangeAbort(unittest.TestCase):
    def test_stale_casting_name_rearmed_to_new_master(self):
        # casting bound to old master sim-1, but sim-2 is now master and drives.
        c = _cluster(master="sim-2", casting="sim-1", epoch=2,
                     witnesses={"w-1": {"corrupt": True}})
        self.assertEqual(cs.decide_casting_action(c, "sim-2"),
                         ("arm_casting", "sim-2"))

    def test_stale_casting_name_cleaned_when_not_needed(self):
        # no corrupt witness, but a stale casting name lingers → disarm it.
        c = _cluster(master="sim-2", casting="sim-1", epoch=2, witnesses={})
        self.assertEqual(cs.decide_casting_action(c, "sim-2"),
                         ("disarm_casting", "sim-2"))


class TestN3Cleanup(unittest.TestCase):
    def test_n3_unwinds_casting(self):
        # cluster grew to 3 nodes → normal majority survives a witness loss →
        # casting no longer needed → disarm.
        c = _cluster(casting="sim-1", epoch=2, n3=True,
                     witnesses={"w-1": {"corrupt": True}})
        self.assertEqual(cs.decide_casting_action(c, "sim-1"),
                         ("disarm_casting", "sim-1"))


class TestFullForwardSequence(unittest.TestCase):
    """Walk the whole forward saga as the executor would, feeding each step's
    effect back into the view, to prove it terminates in the disabled state."""

    def test_arm_then_disable_then_idle(self):
        W = {"w-1": {"corrupt": True}}
        c = _cluster(casting=None, epoch=0, witnesses=W)
        # 1: arm
        a1 = cs.decide_casting_action(c, "sim-1")
        self.assertEqual(a1, ("arm_casting", "sim-1"))
        # apply arm: casting set, epoch bumped, followers lag one tick
        c = _cluster(casting="sim-1", epoch=1, n1_epoch=1, n2_epoch=0, witnesses=W)
        self.assertIsNone(cs.decide_casting_action(c, "sim-1"))   # wait
        # follower catches up
        c = _cluster(casting="sim-1", epoch=1, witnesses=W)
        a2 = cs.decide_casting_action(c, "sim-1")
        self.assertEqual(a2, ("disable_witness", "w-1"))
        # apply disable: witness disabled, epoch bumped, all caught up
        c = _cluster(casting="sim-1", epoch=2,
                     witnesses={"w-1": {"corrupt": True, "disabled": True}})
        self.assertIsNone(cs.decide_casting_action(c, "sim-1"))   # done


if __name__ == "__main__":
    unittest.main()
