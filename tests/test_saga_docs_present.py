"""Contract test: every registered saga has a markdown doc.

The per-saga doc lives at ``docs/sagas/<kind>.md`` and follows the
template described in ``docs/sagas/README.md`` (Purpose, Trigger,
Inputs, Outputs, Step overview, Revert, Idempotency, Step details).

A drift between code and docs is the kind of bug nobody notices
until they pick up an unfamiliar saga six months later. This test
makes drift fail CI.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "installer"))

# Import every saga-defining module so SAGAS gets populated.
from bedrock_d.install import (  # noqa: E402
    cluster_init,
    cluster_tier,
    node_join,
    node_leave,
)
from bedrock_d.vm import create, destroy, grow, migrate  # noqa: E402,F401
from bedrock_d.cluster import rename as _cluster_rename  # noqa: E402,F401
from bedrock_d.orchestrator import replica_repair  # noqa: E402,F401
from bedrock_d.orchestrator.sagas import SAGAS  # noqa: E402

SAGAS_DIR = ROOT / "docs" / "sagas"


class SagaDocsPresent(unittest.TestCase):
    """Every saga kind has a doc; every doc lines up with a saga
    that's currently registered."""

    def test_sagas_dir_exists(self):
        self.assertTrue(SAGAS_DIR.is_dir(),
                        f"missing {SAGAS_DIR.relative_to(ROOT)}")

    def test_every_saga_has_a_doc(self):
        missing = sorted(k for k in SAGAS
                         if not (SAGAS_DIR / f"{k}.md").exists())
        self.assertFalse(
            missing,
            f"missing per-saga doc(s): {missing}. "
            f"Create docs/sagas/<kind>.md following the template "
            f"in docs/sagas/README.md.",
        )

    def test_no_orphan_docs(self):
        """Doc files in docs/sagas/ that don't match a registered
        saga are usually a rename that left the old doc behind."""
        existing = {p.stem for p in SAGAS_DIR.glob("*.md")
                    if p.stem != "README"}
        orphans = sorted(existing - set(SAGAS))
        self.assertFalse(
            orphans,
            f"orphan doc(s) under docs/sagas/ (no matching saga "
            f"in SAGAS): {orphans}. Delete or rename.",
        )

    def test_index_references_every_doc(self):
        """The index README.md should link to every per-saga doc so
        readers don't miss any from the table at the top."""
        index_text = (SAGAS_DIR / "README.md").read_text()
        missing_links = sorted(
            k for k in SAGAS if f"({k}.md)" not in index_text
        )
        self.assertFalse(
            missing_links,
            f"docs/sagas/README.md missing link(s) to: {missing_links}. "
            f"Add a row to the 'All sagas' table.",
        )


class SagaDocSectionsPresent(unittest.TestCase):
    """Each per-saga doc must contain the load-bearing sections so
    readers can find Purpose / Inputs / Steps / Revert / Idempotency
    in the same place every time."""

    REQUIRED_HEADINGS = (
        "## Purpose",
        "## Trigger",
        "## Inputs",
        "## Step overview",
        "## Revert",
        "## Idempotency",
        "## Step details",
    )

    def test_each_doc_has_required_headings(self):
        failures: list[str] = []
        for kind in sorted(SAGAS):
            path = SAGAS_DIR / f"{kind}.md"
            if not path.exists():
                continue   # covered by the other test
            text = path.read_text()
            missing = [h for h in self.REQUIRED_HEADINGS
                       if h not in text]
            if missing:
                failures.append(f"{kind}: missing {missing}")
        self.assertFalse(
            failures,
            "per-saga doc(s) missing required sections:\n  " +
            "\n  ".join(failures),
        )


if __name__ == "__main__":
    unittest.main()
