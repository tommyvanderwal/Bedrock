"""Unit tests for bedrock-net Phase A changes.

Per the mesh-routing design (docs/06-mesh-network.md,
docs/network-walkthrough.md) — Decisions D-13, D-14, D-15:

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

# Make lib importable as `lib`
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

    def test_multipath_metric_stays_before_nexthops(self):
        """`ip route show` prints `metric` on the header line of a
        multipath route (before the nexthops), and `ip route replace`
        requires that form. Normalize keeps `metric` BEFORE the
        nexthop list (it only pulls a kernel tail-form metric forward)."""
        out = netd._normalize_route_line(
            "100.42.42.2 proto static metric 10 "
            "nexthop via 169.254.1.1 dev enp2s0 weight 1 "
            "nexthop via 169.254.2.1 dev enp3s0 weight 1"
        )
        self.assertEqual(
            out,
            "100.42.42.2/32 metric 10 "
            "nexthop via 169.254.1.1 dev enp2s0 weight 1 "
            "nexthop via 169.254.2.1 dev enp3s0 weight 1",
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

    def test_follower_panic_route_uses_lowest_octet_neighbour(self):
        """The /24 panic route uses the next-hop to the lowest-octet
        neighbour whose octet is strictly lower than ours."""
        # cluster.json content is irrelevant now — routing is
        # master-independent — but we still write one to prove it's unused.
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
        # sim-1 (octet 1 < our 2) is the next-hop
        self.assertIn("via 169.254.10.1", panic_routes[0])
        self.assertIn("dev enp2s0", panic_routes[0])

    def test_lowest_octet_node_installs_no_panic_route(self):
        """The global-lowest-octet node has no lower-octet neighbour, so
        it installs NO catch-all and sinks unknown traffic (the loop-free
        base case). No rqlite/master lookup is consulted."""
        self._write_cluster_json(
            mgmt_master="sim-1",
            nodes={"sim-1": "100.42.42.1", "sim-2": "100.42.42.2"},
        )
        d = self._make_daemon("sim-1", "100.42.42.1", "test-cluster")
        # Even with a (higher-octet) neighbour, sim-1 emits NO panic route.
        d.neighbours[("sim-2", "enp2s0", "enp2s0")] = _make_neighbour(
            "sim-2", "enp2s0", "100.42.42.2", "169.254.10.2", "enp2s0")

        routes = netd.compute_routes(d)
        panic_routes = [r for r in routes if "metric 999" in r]
        self.assertEqual(
            len(panic_routes), 0,
            f"lowest-octet node should sink, not emit panic, got: {panic_routes}",
        )

    def test_panic_prefers_lowest_octet_over_freshest(self):
        """Lowest octet wins over recency: a fresher higher-octet
        neighbour does NOT beat an older lower-octet one."""
        d = self._make_daemon("sim-4", "100.42.42.4", "test-cluster")
        now = time.time()
        n1 = _make_neighbour(
            "sim-1", "enp2s0", "100.42.42.1", "169.254.10.1", "enp2s0")
        n1.last_seen = now - 100   # older, but lowest octet
        n3 = _make_neighbour(
            "sim-3", "enp3s0", "100.42.42.3", "169.254.30.3", "enp3s0")
        n3.last_seen = now         # freshest, but higher octet
        d.neighbours[("sim-1", "enp2s0", "enp2s0")] = n1
        d.neighbours[("sim-3", "enp3s0", "enp3s0")] = n3

        routes = netd.compute_routes(d)
        panic_routes = [r for r in routes if "metric 999" in r]
        self.assertEqual(len(panic_routes), 1)
        # Lowest octet (sim-1) wins, NOT freshest (sim-3)
        self.assertIn("via 169.254.10.1", panic_routes[0])
        self.assertNotIn("via 169.254.30.3", panic_routes[0])

    def test_panic_is_master_independent(self):
        """Routing ignores who cluster.json names as master: the panic
        next-hop is the lowest-octet neighbour even when a different
        node is master. This is the decoupling guarantee."""
        # cluster.json says sim-3 is master — routing must NOT care.
        self._write_cluster_json(
            mgmt_master="sim-3",
            nodes={"sim-1": "100.42.42.1", "sim-3": "100.42.42.3",
                   "sim-2": "100.42.42.2"},
        )
        d = self._make_daemon("sim-2", "100.42.42.2", "test-cluster")
        d.neighbours[("sim-1", "enp2s0", "enp2s0")] = _make_neighbour(
            "sim-1", "enp2s0", "100.42.42.1", "169.254.10.1", "enp2s0")

        routes = netd.compute_routes(d)
        panic_routes = [r for r in routes if "metric 999" in r]
        self.assertEqual(len(panic_routes), 1)
        # Via sim-1 (lowest octet), regardless of master == sim-3
        self.assertIn("via 169.254.10.1", panic_routes[0])

    def test_panic_sinks_when_only_higher_octet_neighbours(self):
        """If every reachable neighbour has a HIGHER octet than ours, we
        are a local sink (forwarding up would risk a loop) — install NO
        catch-all. (The old freshest-rule would have routed via the
        higher-octet neighbour and could loop.)"""
        d = self._make_daemon("sim-2", "100.42.42.2", "test-cluster")
        # Only sim-3 (octet 3 > our 2) reachable.
        d.neighbours[("sim-3", "enp3s0", "enp3s0")] = _make_neighbour(
            "sim-3", "enp3s0", "100.42.42.3", "169.254.30.3", "enp3s0")

        routes = netd.compute_routes(d)
        panic_routes = [r for r in routes if "metric 999" in r]
        self.assertEqual(
            len(panic_routes), 0,
            "no lower-octet neighbour → must sink, not forward upward",
        )

    # ── Combined: routes for a realistic 2-node testbed ─────────────

    def test_realistic_2node_panic_plus_ecmp(self):
        """Realistic 2-node setup: sim-2 sees sim-1 (lowest octet)
        across 3 NIC pairs at the same speed. Expect ONE ECMP route to
        sim-1 + ONE single-path panic route via sim-1's best link."""
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

        # Exactly one panic /24 route, single-path via sim-1's best link
        # (panic is a fallback, not a hot path — no multipath).
        panic_routes = [r for r in routes if "metric 999" in r]
        self.assertEqual(len(panic_routes), 1)
        link_addrs_sim1 = ["169.254.10.1", "169.254.20.1", "169.254.30.1"]
        self.assertTrue(
            any(la in panic_routes[0] for la in link_addrs_sim1),
            f"panic should use one of sim-1's link addrs, got: "
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
