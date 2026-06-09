"""Lint: only bedrock_d.state (and its legacy backers) may import
from rqlite_client directly.

The rule (codebase-rewrite-plan §3.3): one module owns rqlite I/O.
New code under bedrock_d/ imports from ``bedrock_d.state``. The
legacy ``lib/`` modules can still talk to rqlite_client
directly because that's where the implementation currently lives;
Stage 7 of the rewrite moves them and this allowlist shrinks.

If you're adding new code that talks to rqlite, add a typed helper
in lib/bedrock_state.py + re-export from bedrock_d.state.
Don't add yourself to ALLOWED below."""
from __future__ import annotations

import pathlib
import re


REPO = pathlib.Path(__file__).resolve().parents[1]

# Modules ALLOWED to import rqlite_client / bedrock_state directly.
# Shrinks as Stage 7 moves implementations under bedrock_d/.
ALLOWED = {
    # The state module IS the wrapper.
    "bedrock_d/state.py",
    # The saga executor's production backend wraps RqliteClient
    # explicitly; that's its purpose.
    "bedrock_d/orchestrator/sagas/rqlite_backend.py",
    # Legacy implementations (will move in Stage 7).
    "lib/bedrock_state.py",
    "lib/rqlite_setup.py",
    "lib/view_builder.py",     # snapshot reader; not a writer
    "lib/netd.py",             # election tick reads cluster.json + rqlite
    "lib/cluster_arbiter.py",
    "lib/casting_saga.py",     # #7 saga executor: arms/disables vote config

    "lib/operator_auth.py",
    "lib/join_handshake.py",
    "lib/tier_storage.py",
    # decide_vm_fence does a level='strong' vms.host read INSIDE the DRBD
    # fence-decision endpoint path; it must hit rqlite directly (the strict-
    # leader read IS the majority gate) and cannot route through a cached
    # bedrock_d.state facade. See docs/explainers/02-bedrock-perspective.md.
    "lib/fence_verdict.py",
    # mgmt is a peer of bedrock_d at the moment; treat as legacy
    # until Stage 8 splits it.
    "mgmt/app.py",
    "mgmt/orchestrator.py",
    "mgmt/backup.py",
    "mgmt/victoria.py",
    # In-flight VM-lifecycle saga cutover (BAD-2/3): these reach rqlite
    # via lib.rqlite_client through the sys.path shim today; they move
    # onto bedrock_d.state when the cutover lands and this list shrinks.
    "bedrock_d/orchestrator/vm_failover.py",
    # Self-heal repair loop + its replica_repair saga: same in-flight
    # cutover band as vm_failover — read cluster state + drive DRBD
    # replica restoration via lib.rqlite_client/bedrock_state through
    # the shim today; move onto bedrock_d.state when the cutover lands.
    "bedrock_d/orchestrator/self_heal.py",
    "bedrock_d/orchestrator/replica_repair.py",
    "bedrock_d/vm/create.py",
    "bedrock_d/vm/destroy.py",
    "bedrock_d/vm/grow.py",
    "bedrock_d/vm/migrate.py",
    "bedrock_d/vm/failover.py",
    # Pure rqlite READ facade (like view_builder) — a reader, not a writer.
    "lib/cluster_state.py",
}

PATTERN = re.compile(
    r"^\s*(from .*rqlite_client\b|"
    r"import\s+.*rqlite_client\b|"
    r"from .*bedrock_state\b|"
    r"import\s+.*bedrock_state\b)",
    re.MULTILINE,
)


def _iter_source_files():
    for sub in ("bedrock_d", "lib", "mgmt"):
        root = REPO / sub
        if not root.exists():
            continue
        for py in root.rglob("*.py"):
            if "__pycache__" in py.parts:
                continue
            yield py


def test_only_allowed_modules_import_rqlite_directly():
    """If this test fails after you added a new file: route your
    rqlite calls through ``bedrock_d.state`` instead, or add the
    file to ALLOWED if it has a legitimate reason (e.g. it's a
    new state-mutator helper that itself becomes part of the
    canonical state surface)."""
    bad: list[str] = []
    for src in _iter_source_files():
        rel = str(src.relative_to(REPO))
        if rel in ALLOWED:
            continue
        text = src.read_text(errors="replace")
        m = PATTERN.search(text)
        if m:
            bad.append(f"{rel}: {m.group(0).strip()}")
    assert not bad, (
        "These files import rqlite_client / bedrock_state directly "
        "but aren't in tests/test_state_source_lint.ALLOWED. Route "
        "through bedrock_d.state or add the file to ALLOWED with a "
        "comment explaining why:\n  " + "\n  ".join(bad)
    )
