"""Tests for journal → vlagent forwarding config (lib/observability.py)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "installer"))

from lib import observability  # noqa: E402


class TestJournalForwardConfig(unittest.TestCase):
    def test_journal_dropin_enables_forward_to_syslog(self):
        body = observability._journal_forward_dropin()
        self.assertIn("ForwardToSyslog=yes", body)

    def test_rsyslog_dropin_targets_local_vlagent(self):
        body = observability._rsyslog_vlagent_dropin()
        self.assertIn("127.0.0.1", body)
        self.assertIn("5140", body)
        self.assertIn("vlagent", body)  # feedback-loop guard

    def test_vlagent_unit_has_no_rsyslog_ordering(self):
        """vlagent is the TCP listener; rsyslog is the client — no After=rsyslog."""
        unit = observability._vlagent_unit(["node-a"], {"nodes": {"node-a": {"host": "10.0.0.1"}}})
        self.assertIn("After=network-online.target", unit)
        self.assertNotIn("rsyslog", unit)


if __name__ == "__main__":
    unittest.main()
