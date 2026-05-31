"""The DEFAULT kopia repo password is a deliberately-PUBLIC constant, so backups
work with zero operator setup and any Bedrock install can read a default repo
(recoverability over secrecy — kopia forces a password, so we publish a harmless
one). A real password is opt-in per repo. These tests pin that behaviour."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mgmt"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "installer"))

import backup as bk  # noqa: E402


def test_public_constant_is_the_documented_string():
    # This string is published on purpose (docs/GitHub). If it ever changes,
    # old default repos become unreadable — so the value is load-bearing.
    assert bk.PUBLIC_REPO_PASSWORD == \
        "PublicBedrockNotAPasswordBecauseKopiaForcesThisEvenWhenNotNeeded"


def test_ensure_seeds_public_default_when_absent(tmp_path, monkeypatch):
    key = tmp_path / "backup.key"
    monkeypatch.setattr(bk, "ENCRYPTION_KEY_FILE", key)
    assert not key.exists()
    p = bk._ensure_repo_password_file()
    assert p == key
    assert key.read_text() == bk.PUBLIC_REPO_PASSWORD
    assert (key.stat().st_mode & 0o777) == 0o600


def test_ensure_never_overwrites_a_real_password(tmp_path, monkeypatch):
    key = tmp_path / "backup.key"
    key.write_text("a-real-operator-password")
    monkeypatch.setattr(bk, "ENCRYPTION_KEY_FILE", key)
    bk._ensure_repo_password_file()
    assert key.read_text() == "a-real-operator-password"   # untouched


def test_is_default_true_for_public_and_absent(tmp_path, monkeypatch):
    key = tmp_path / "backup.key"
    monkeypatch.setattr(bk, "ENCRYPTION_KEY_FILE", key)
    assert bk.repo_password_is_default(key) is True          # absent → no real pw
    key.write_text(bk.PUBLIC_REPO_PASSWORD + "\n")            # trailing ws tolerated
    assert bk.repo_password_is_default(key) is True
    key.write_text("real-secret")
    assert bk.repo_password_is_default(key) is False
