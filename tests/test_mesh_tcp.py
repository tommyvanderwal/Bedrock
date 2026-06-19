"""Unit tests for TCP mesh routing (lib/mesh_tcp.py)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "installer"))

from lib import mesh_tcp  # noqa: E402


class TestMeshTcpDialer(unittest.TestCase):
    def test_higher_loopback_dials(self):
        self.assertTrue(mesh_tcp.we_dial("100.75.30.3", "100.75.30.2"))
        self.assertFalse(mesh_tcp.we_dial("100.75.30.1", "100.75.30.4"))

    def test_equal_loopback_no_dial(self):
        self.assertFalse(mesh_tcp.we_dial("100.75.30.2", "100.75.30.2"))


class TestMeshTcpKeepalive(unittest.TestCase):
    def test_configure_sets_keepalive(self):
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            mesh_tcp.configure_tcp_keepalive(s)
            self.assertEqual(s.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE), 1)
            self.assertEqual(
                s.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE),
                mesh_tcp._TCP_KEEPIDLE,
            )
        finally:
            s.close()


class TestMeshTcpConnect(unittest.TestCase):
    def test_mesh_maybe_connect_lan_peer(self):
        """TCP adjacency must not be restricted to 169.254 addresses."""
        from unittest.mock import MagicMock, patch

        d = MagicMock()
        d.my_loopback = "100.75.30.3"
        d.my_node = "bedrock-aaa"
        d.nic_addrs = {"br0": "192.168.2.42"}
        d.mesh_sessions = {}
        d.mesh_connecting = set()

        with patch.object(mesh_tcp.threading, "Thread") as mock_thread:
            mesh_tcp.mesh_maybe_connect(
                d,
                peer_node="bedrock-bbb",
                peer_nic="br0",
                my_nic="br0",
                peer_loopback="100.75.30.2",
                peer_link_addr="192.168.2.43",
            )
            mock_thread.assert_called_once()
            self.assertIn(("bedrock-bbb", "br0", "br0"), d.mesh_connecting)


if __name__ == "__main__":
    unittest.main()
