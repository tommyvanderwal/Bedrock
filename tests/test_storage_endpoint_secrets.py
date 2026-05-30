"""storage_endpoints credential handling — secrets are AEAD-wrapped with the
cluster key before they touch rqlite (so an rqlite snapshot/backup never carries
plaintext). Tests the seal/unseal round-trip without needing a real cluster.key."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "installer"))

from lib import bedrock_state  # noqa: E402
from lib import witness        # noqa: E402


def test_seal_unseal_roundtrip(monkeypatch):
    monkeypatch.setattr(witness, "load_cluster_key", lambda *a, **k: b"k" * 32)
    sealed = bedrock_state._seal_secret("hunter2")
    assert sealed and sealed != "hunter2"      # wrapped, not the plaintext
    assert "hunter2" not in sealed             # plaintext nowhere in the blob
    bytes.fromhex(sealed)                       # it is valid hex
    assert bedrock_state.unseal_secret(sealed) == "hunter2"


def test_seal_empty_stays_empty():
    assert bedrock_state._seal_secret("") == ""
    assert bedrock_state.unseal_secret("") == ""


def test_unseal_wrong_key_returns_empty(monkeypatch):
    # AEAD auth failure (wrong cluster key) → "" so a consumer that needs the
    # secret sees empty and must fail loud, never a garbage/partial password.
    monkeypatch.setattr(witness, "load_cluster_key", lambda *a, **k: b"k" * 32)
    sealed = bedrock_state._seal_secret("s3cr3t")
    monkeypatch.setattr(witness, "load_cluster_key", lambda *a, **k: b"x" * 32)
    assert bedrock_state.unseal_secret(sealed) == ""
