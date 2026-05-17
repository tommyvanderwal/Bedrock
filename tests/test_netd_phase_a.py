"""Unit tests for bedrock-net Phase A changes.

Per docs/post-alpha-rewrite-notes.md (Decisions D-13, D-14, D-15):

  D-13 — panic catch-all /24 routes via the current mgmt-master's
         loopback, not via the freshest neighbour. Master itself
         doesn't install a /24-via-self route. Falls back to freshest
         if master unknown.
  D-14 — local_metric latency cost floors to 0 below 1 ms (sub-ms is
         noise on a healthy LAN; bandwidth should dominate).
  D-15 — paths to the same peer with tied bucketed-bandwidth metric
         get emitted as ONE multipath (ECMP) route with multiple
         nexthops, instead of N single-path routes with separate
         metrics.

Live testbed-sim tests aren't currently runnable (SSH key drift since
last sim spawn). This unit-test exercises compute_routes() against
synthetic Daemon + Neighbour fixtures so the Phase A invariants are
verifiable without VMs.

Run with: python3 -m pytest tests/test_netd_phase_a.py -v
     or: python3 tests/test_netd_phase_a.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

# Make installer/lib importable as `lib`
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "installer"))

from lib import netd  # noqa: E402


# ────────────────────────────────────────────────────────────────────
# local_metric — latency floor
# ────────────────────────────────────────────────────────────────────

class TestLocalMetricLatencyFloor(unittest.TestCase):
    """D-14: latency below 1 ms contributes 0 to the metric."""

    def test_sub_ms_latency_floored_to_zero(self):
        # Two paths same bandwidth, one at 50us, one at 999us — should
        # produce identical metric (both floor to 0 latency contribution).
        bw = 10_000
        bw_only = int(1_000_000 / bw)
        for lat_us in (0, 50, 500, 999, 1000):
            with self.subTest(lat_us=lat_us):
                self.assertEqual(
                    netd.local_metric(bw_mbps=bw, latency_us=lat_us),
                    bw_only,
                    f"latency_us={lat_us} should not contribute "
                    f"(below or at the 1ms floor)",
                )

    def test_above_floor_contributes_1_per_100us(self):
        bw = 10_000
        bw_only = int(1_000_000 / bw)
        # 1100 us = 100us over the floor = 1 metric unit
        self.assertEqual(
            netd.local_metric(bw_mbps=bw, latency_us=1100),
            bw_only + 1,
        )
        # 10ms = 9000us over floor = 90 metric units
        self.assertEqual(
            netd.local_metric(bw_mbps=bw, latency_us=10000),
            bw_only + 90,
        )

    def test_bandwidth_still_dominates_at_local_scale(self):
        """At local sub-ms latency, paths differ ONLY by bandwidth."""
        # 10G vs 1G, both at 100us
        m_10g = netd.local_metric(bw_mbps=10_000, latency_us=100)
        m_1g = netd.local_metric(bw_mbps=1_000, latency_us=100)
        self.assertLess(m_10g, m_1g)
        # Order-of-magnitude ratio matches the bandwidth ratio
        self.assertAlmostEqual(m_1g / m_10g, 10.0, places=0)


# ────────────────────────────────────────────────────────────────────
# Route-line normalize — diff round-trip
# ────────────────────────────────────────────────────────────────────

class TestNormalizeRouteLine(unittest.TestCase):
    """The normalizer makes `ip route show` output match what
    compute_routes emits, so the set-based diff in apply_routes()
    doesn't churn on every tick."""

    def test_adds_slash32_to_bare_ip_host_routes(self):
        self.assertEqual(
            netd._normalize_route_line(
                "100.42.42.2 via 169.254.1.1 dev enp2s0 metric 10"),
            "100.42.42.2/32 via 169.254.1.1 dev enp2s0 metric 10",
        )

    def test_preserves_slash_24_panic_routes(self):
        self.assertEqual(
            netd._normalize_route_line(
                "100.42.42.0/24 via 169.254.1.1 dev enp2s0 metric 999"),
            "100.42.42.0/24 via 169.254.1.1 dev enp2s0 metric 999",
        )

    def test_strips_kernel_added_annotations(self):
        self.assertEqual(
            netd._normalize_route_line(
                "100.42.42.2 via 169.254.1.1 dev enp2s0 "
                "proto static metric 10"),
            "100.42.42.2/32 via 169.254.1.1 dev enp2s0 metric 10",
        )
        self.assertEqual(
            netd._normalize_route_line(
                "100.42.42.2 via 169.254.1.1 dev enp2s0 "
                "metric 10 linkdown"),
            "100.42.42.2/32 via 169.254.1.1 dev enp2s0 metric 10",
        )

    def test_multipath_metric_moves_to_tail(self):
        """`ip route show` puts `metric` on the header line of a
        multipath route; we emit it after all nexthops. Normalize
        must move it to match."""
        out = netd._normalize_route_line(
            "100.42.42.2 proto static metric 10 "
            "nexthop via 169.254.1.1 dev enp2s0 weight 1 "
            "nexthop via 169.254.2.1 dev enp3s0 weight 1"
        )
        self.assertEqual(
            out,
            "100.42.42.2/32 "
            "nexthop via 169.254.1.1 dev enp2s0 weight 1 "
            "nexthop via 169.254.2.1 dev enp3s0 weight 1 "
            "metric 10",
        )

    def test_empty_string(self):
        self.assertEqual(netd._normalize_route_line(""), "")


# ────────────────────────────────────────────────────────────────────
# compute_routes — full integration with mocks
# ────────────────────────────────────────────────────────────────────

def _make_neighbour(peer_node, peer_nic, peer_loopback, peer_link_addr,
                    my_nic, speed_mbps=10_000, rtt_us=100):
    """Build a fully-populated Neighbour fixture in 'logged_up' state."""
    now = time.time()
    return netd.Neighbour(
        peer_node=peer_node,
        peer_nic=peer_nic,
        peer_loopback=peer_loopback,
        peer_link_addr=peer_link_addr,
        my_nic=my_nic,
        first_seen=now - 3600,   # well past the flap window
        last_seen=now,
        speed_mbps=speed_mbps,
        rtt_us=rtt_us,
        logged_up=True,
    )


class TestComputeRoutes(unittest.TestCase):
    """Integration test for compute_routes — exercises all three Phase A
    changes against a realistic synthetic neighbour table."""

    def setUp(self):
        # Each test gets its own temp cluster.json so the master-lookup
        # path is exercisable without globals leaking between tests.
        self._tmpdir = tempfile.TemporaryDirectory()
        self.cluster_json_path = Path(self._tmpdir.name) / "cluster.json"

        # Patch CLUSTER_JSON at module level
        self._cluster_patch = mock.patch.object(
            netd, "CLUSTER_JSON", self.cluster_json_path)
        self._cluster_patch.start()

        # nic_speed_mbps reads /sys/class/net — patch to a known value
        # so ECMP grouping is deterministic. Real testbed has virtio
        # NICs returning 0 (unknown); we use a known value to exercise
        # the bucketing path.
        self._speed_patch = mock.patch.object(
            netd, "nic_speed_mbps",
            side_effect=lambda nic: {
                "enp2s0": 10_000, "enp3s0": 10_000,
                "enp4s0": 10_000, "enp5s0": 2500,
                "br0":    1_000,
            }.get(nic, 0),
        )
        self._speed_patch.start()

    def tearDown(self):
        self._speed_patch.stop()
        self._cluster_patch.stop()
        self._tmpdir.cleanup()

    def _write_cluster_json(self, *, mgmt_master, nodes):
        """nodes: {name: loopback_ip}"""
        data = {
            "mgmt_master": mgmt_master,
            "nodes": {n: {"loopback_ip": lo} for n, lo in nodes.items()},
        }
        self.cluster_json_path.write_text(json.dumps(data))

    def _make_daemon(self, my_node, my_loopback, cluster_uuid):
        return netd.Daemon(
            cluster_key=b"\x00" * 32,
            cluster_uuid=cluster_uuid,
            my_node=my_node,
            my_loopback=my_loopback,
        )

    # ── D-15: ECMP grouping ──────────────────────────────────────────

    def test_tied_paths_emit_single_multipath_route(self):
        """Two paths to the same peer at the same bucketed bandwidth
        + sub-ms RTT should emit ONE multipath route, not two."""
        # 2-node cluster; sim-1 is master, this is sim-2.
        # 'cluster' UUID controls the /24 derivation — pick one whose
        # derived prefix is known.
        self._write_cluster_json(
            mgmt_master="sim-1",
            nodes={"sim-1": "100.42.42.1", "sim-2": "100.42.42.2"},
        )
        d = self._make_daemon("sim-2", "100.42.42.2", "test-cluster")
        # Two paths to sim-1 at the same bucketed speed
        d.neighbours[("sim-1", "enp2s0", "enp2s0")] = _make_neighbour(
            "sim-1", "enp2s0", "100.42.42.1", "169.254.10.1", "enp2s0")
        d.neighbours[("sim-1", "enp3s0", "enp3s0")] = _make_neighbour(
            "sim-1", "enp3s0", "100.42.42.1", "169.254.20.1", "enp3s0")

        # _cluster_node_loopbacks reads cluster.json directly; patched
        # via CLUSTER_JSON above
        routes = netd.compute_routes(d)

        # Find the /32 route to sim-1
        sim1_routes = [r for r in routes if r.startswith("100.42.42.1/32")]
        self.assertEqual(
            len(sim1_routes), 1,
            f"expected ONE multipath route to sim-1, got: {sim1_routes}",
        )
        spec = sim1_routes[0]
        self.assertIn("nexthop via 169.254.10.1 dev enp2s0 weight 1", spec)
        self.assertIn("nexthop via 169.254.20.1 dev enp3s0 weight 1", spec)

    def test_different_bandwidth_paths_emit_separate_routes(self):
        """A 10G path and a 2.5G path are NOT tied — each gets its
        own metric-tier route."""
        self._write_cluster_json(
            mgmt_master="sim-1",
            nodes={"sim-1": "100.42.42.1", "sim-2": "100.42.42.2"},
        )
        d = self._make_daemon("sim-2", "100.42.42.2", "test-cluster")
        d.neighbours[("sim-1", "enp2s0", "enp2s0")] = _make_neighbour(
            "sim-1", "enp2s0", "100.42.42.1", "169.254.10.1", "enp2s0",
            speed_mbps=10_000)
        d.neighbours[("sim-1", "enp5s0", "enp5s0")] = _make_neighbour(
            "sim-1", "enp5s0", "100.42.42.1", "169.254.50.1", "enp5s0",
            speed_mbps=2500)

        routes = netd.compute_routes(d)
        sim1_routes = [r for r in routes if r.startswith("100.42.42.1/32")]
        self.assertEqual(
            len(sim1_routes), 2,
            "different-bandwidth paths should produce 2 metric-tier "
            f"routes, got: {sim1_routes}",
        )
        # First (best) is 10G, second (backup) is 2.5G
        self.assertIn("via 169.254.10.1 dev enp2s0 metric 10", sim1_routes[0])
        self.assertIn("via 169.254.50.1 dev enp5s0 metric 11", sim1_routes[1])
        # Neither should be a multipath route
        for spec in sim1_routes:
            self.assertNotIn("nexthop", spec)

    def test_three_tied_paths_one_multipath_three_nexthops(self):
        """ECMP across 3 tied paths produces one route with 3 nexthops."""
        self._write_cluster_json(
            mgmt_master="sim-1",
            nodes={"sim-1": "100.42.42.1", "sim-2": "100.42.42.2"},
        )
        d = self._make_daemon("sim-2", "100.42.42.2", "test-cluster")
        for nic, link_addr in [("enp2s0", "169.254.10.1"),
                                ("enp3s0", "169.254.20.1"),
                                ("enp4s0", "169.254.30.1")]:
            d.neighbours[("sim-1", nic, nic)] = _make_neighbour(
                "sim-1", nic, "100.42.42.1", link_addr, nic)
        routes = netd.compute_routes(d)
        sim1_routes = [r for r in routes if r.startswith("100.42.42.1/32")]
        self.assertEqual(len(sim1_routes), 1)
        self.assertEqual(sim1_routes[0].count("nexthop via"), 3)

    # ── D-13: Panic-via-master ───────────────────────────────────────

    def test_follower_panic_route_uses_master_path(self):
        """On a follower, the /24 panic route uses the next-hop that
        leads to the master."""
        self._write_cluster_json(
            mgmt_master="sim-1",
            nodes={"sim-1": "100.42.42.1", "sim-2": "100.42.42.2"},
        )
        d = self._make_daemon("sim-2", "100.42.42.2", "test-cluster")
        d.neighbours[("sim-1", "enp2s0", "enp2s0")] = _make_neighbour(
            "sim-1", "enp2s0", "100.42.42.1", "169.254.10.1", "enp2s0")

        routes = netd.compute_routes(d)
        panic_routes = [r for r in routes if "metric 999" in r]
        self.assertEqual(len(panic_routes), 1)
        # Master's link_addr is the next-hop
        self.assertIn("via 169.254.10.1", panic_routes[0])
        self.assertIn("dev enp2s0", panic_routes[0])

    def test_master_does_not_install_panic_via_self(self):
        """The master itself doesn't install a /24-via-self route —
        that would be a routing loop."""
        self._write_cluster_json(
            mgmt_master="sim-1",
            nodes={"sim-1": "100.42.42.1", "sim-2": "100.42.42.2"},
        )
        d = self._make_daemon("sim-1", "100.42.42.1", "test-cluster")
        # Even with a neighbour, the master should emit NO panic route
        d.neighbours[("sim-2", "enp2s0", "enp2s0")] = _make_neighbour(
            "sim-2", "enp2s0", "100.42.42.2", "169.254.10.2", "enp2s0")

        routes = netd.compute_routes(d)
        panic_routes = [r for r in routes if "metric 999" in r]
        self.assertEqual(
            len(panic_routes), 0,
            f"master should not emit panic route, got: {panic_routes}",
        )

    def test_panic_falls_back_to_freshest_when_no_cluster_json(self):
        """Bootstrap case: before cluster.json is written, fall back
        to the historical freshest-neighbour rule so the cluster is
        still routable."""
        # No cluster.json created
        d = self._make_daemon("sim-2", "100.42.42.2", "test-cluster")
        now = time.time()
        n1 = _make_neighbour(
            "sim-1", "enp2s0", "100.42.42.1", "169.254.10.1", "enp2s0")
        n1.last_seen = now - 100   # older
        n3 = _make_neighbour(
            "sim-3", "enp3s0", "100.42.42.3", "169.254.30.3", "enp3s0")
        n3.last_seen = now   # freshest
        d.neighbours[("sim-1", "enp2s0", "enp2s0")] = n1
        d.neighbours[("sim-3", "enp3s0", "enp3s0")] = n3

        routes = netd.compute_routes(d)
        panic_routes = [r for r in routes if "metric 999" in r]
        self.assertEqual(len(panic_routes), 1)
        # Freshest neighbour (sim-3) wins
        self.assertIn("via 169.254.30.3", panic_routes[0])

    def test_panic_uses_master_link_addr_not_freshest_when_both_known(self):
        """When the master is set in cluster.json, the panic route
        goes via the master's link_addr — NOT the freshest neighbour.
        This is the core D-13 change."""
        self._write_cluster_json(
            mgmt_master="sim-1",
            nodes={"sim-1": "100.42.42.1", "sim-3": "100.42.42.3",
                   "sim-2": "100.42.42.2"},
        )
        d = self._make_daemon("sim-2", "100.42.42.2", "test-cluster")
        now = time.time()
        # sim-1 (master) seen 100s ago; sim-3 seen NOW. Pre-D-13 this
        # would route panic via sim-3 (freshest); post-D-13 it must
        # route via sim-1 (master).
        n_master = _make_neighbour(
            "sim-1", "enp2s0", "100.42.42.1", "169.254.10.1", "enp2s0")
        n_master.last_seen = now - 100
        n_other = _make_neighbour(
            "sim-3", "enp3s0", "100.42.42.3", "169.254.30.3", "enp3s0")
        n_other.last_seen = now
        d.neighbours[("sim-1", "enp2s0", "enp2s0")] = n_master
        d.neighbours[("sim-3", "enp3s0", "enp3s0")] = n_other

        routes = netd.compute_routes(d)
        panic_routes = [r for r in routes if "metric 999" in r]
        self.assertEqual(len(panic_routes), 1)
        # Master's link_addr is what we want, NOT sim-3's
        self.assertIn("via 169.254.10.1", panic_routes[0])
        self.assertNotIn("via 169.254.30.3", panic_routes[0])

    def test_panic_falls_back_when_master_unreachable(self):
        """If cluster.json names a master but we have no neighbour
        for it (transient outage, master just went away), fall back
        to the freshest available neighbour rather than no panic
        route at all."""
        self._write_cluster_json(
            mgmt_master="sim-1",
            nodes={"sim-1": "100.42.42.1", "sim-2": "100.42.42.2",
                   "sim-3": "100.42.42.3"},
        )
        d = self._make_daemon("sim-2", "100.42.42.2", "test-cluster")
        # No neighbour for sim-1 (the master); only sim-3 reachable
        d.neighbours[("sim-3", "enp3s0", "enp3s0")] = _make_neighbour(
            "sim-3", "enp3s0", "100.42.42.3", "169.254.30.3", "enp3s0")

        routes = netd.compute_routes(d)
        panic_routes = [r for r in routes if "metric 999" in r]
        self.assertEqual(
            len(panic_routes), 1,
            "expected fallback panic route when master unreachable",
        )
        self.assertIn("via 169.254.30.3", panic_routes[0])

    # ── Combined: routes for a realistic 2-node testbed ─────────────

    def test_realistic_2node_panic_via_master_plus_ecmp(self):
        """Realistic 2-node setup: sim-2 (follower) sees sim-1 (master)
        across 4 NIC pairs all at the same speed. Expect ONE ECMP
        route to sim-1 + ONE panic route via that same ECMP path."""
        self._write_cluster_json(
            mgmt_master="sim-1",
            nodes={"sim-1": "100.42.42.1", "sim-2": "100.42.42.2"},
        )
        d = self._make_daemon("sim-2", "100.42.42.2", "test-cluster")
        for nic, link_addr in [("enp2s0", "169.254.10.1"),
                                ("enp3s0", "169.254.20.1"),
                                ("enp4s0", "169.254.30.1")]:
            d.neighbours[("sim-1", nic, nic)] = _make_neighbour(
                "sim-1", nic, "100.42.42.1", link_addr, nic)

        routes = netd.compute_routes(d)

        # Exactly one /32 route to sim-1 (the ECMP multipath)
        sim1_routes = [r for r in routes if r.startswith("100.42.42.1/32")]
        self.assertEqual(len(sim1_routes), 1)
        self.assertEqual(sim1_routes[0].count("nexthop via"), 3)

        # Exactly one panic /24 route, single-path via master's
        # best link (panic uses by_peer[master][0], not multipath —
        # by design: panic is a fallback, not a hot path)
        panic_routes = [r for r in routes if "metric 999" in r]
        self.assertEqual(len(panic_routes), 1)
        # It points at one of the master's link addresses
        link_addrs_master = ["169.254.10.1", "169.254.20.1", "169.254.30.1"]
        self.assertTrue(
            any(la in panic_routes[0] for la in link_addrs_master),
            f"panic should use one of master's link addrs, got: "
            f"{panic_routes[0]}",
        )


# ────────────────────────────────────────────────────────────────────
# Smoke test for ensure_routing_sysctls (the kernel-sysctl side of D-15)
# ────────────────────────────────────────────────────────────────────

class TestEnsureRoutingSysctls(unittest.TestCase):
    """ensure_routing_sysctls must be silent + idempotent on read-only
    /proc (e.g. inside CI containers)."""

    def test_silent_on_readonly_proc(self):
        # Patch Path so writes raise PermissionError. Function should
        # log to stderr but not raise.
        real_path = netd.Path

        def boom_path(p):
            class _RO:
                def read_text(self_inner):
                    return "0"
                def write_text(self_inner, val):
                    raise OSError("read-only filesystem")
            return _RO()

        with mock.patch.object(netd, "Path", boom_path):
            try:
                netd.ensure_routing_sysctls()
            except Exception as e:
                self.fail(f"raised on read-only /proc: {e!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
