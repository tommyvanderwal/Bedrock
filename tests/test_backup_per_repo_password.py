"""Per-repo password override (4b): each backup target can carry its OWN kopia
password; the resolution at backup time is  per-repo override → node-wide
backup.key → PUBLIC default. The override file exists only when the operator set
a real password, so existing real-backup.key clusters fall through UNCHANGED.

The export is a bash snippet (kopia takes the password via KOPIA_PASSWORD), so the
load-bearing test actually RUNS the snippet and reads back the resolved value."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mgmt"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "installer"))

import backup as bk  # noqa: E402


def _resolve(snippet: str) -> str:
    """Run the export snippet in bash and echo the resolved KOPIA_PASSWORD."""
    r = subprocess.run(
        ["bash", "-c", snippet + '; printf %s "$KOPIA_PASSWORD"'],
        capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_resolution_order_override_then_key_then_public(tmp_path, monkeypatch):
    key = tmp_path / "backup.key"
    monkeypatch.setattr(bk, "ENCRYPTION_KEY_FILE", key)
    monkeypatch.setattr(bk, "CREDENTIALS_DIR", tmp_path)
    tgt = bk._target_password_file("t1")

    # 1. nothing set → the PUBLIC default
    assert _resolve(bk._kopia_password_export("t1")) == bk.PUBLIC_REPO_PASSWORD
    # 2. node-wide backup.key only → that key (existing-cluster behaviour kept)
    key.write_text("node-wide-real-key")
    assert _resolve(bk._kopia_password_export("t1")) == "node-wide-real-key"
    # 3. per-repo override present → it WINS over backup.key
    tgt.write_text("this-repos-own-password")
    assert _resolve(bk._kopia_password_export("t1")) == "this-repos-own-password"


def test_no_target_id_uses_node_key_not_override(tmp_path, monkeypatch):
    key = tmp_path / "backup.key"
    monkeypatch.setattr(bk, "ENCRYPTION_KEY_FILE", key)
    monkeypatch.setattr(bk, "CREDENTIALS_DIR", tmp_path)
    bk._target_password_file("t1").write_text("override")   # must be ignored
    key.write_text("node-key")
    assert _resolve(bk._kopia_password_export("")) == "node-key"


def test_public_default_when_absolutely_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(bk, "ENCRYPTION_KEY_FILE", tmp_path / "nope.key")
    monkeypatch.setattr(bk, "CREDENTIALS_DIR", tmp_path)
    assert _resolve(bk._kopia_password_export("")) == bk.PUBLIC_REPO_PASSWORD


def test_sync_writes_real_then_removes_when_default(tmp_path, monkeypatch):
    monkeypatch.setattr(bk, "CREDENTIALS_DIR", tmp_path)
    f = bk._target_password_file("t1")
    # a real password → override file written 0600
    monkeypatch.setattr(bk.bs, "backup_target_repo_password", lambda tid: "realpw")
    bk._sync_target_password_file("t1")
    assert f.read_text() == "realpw" and (f.stat().st_mode & 0o777) == 0o600
    # back to default ('' from rqlite) → override removed → falls back to backup.key
    monkeypatch.setattr(bk.bs, "backup_target_repo_password", lambda tid: "")
    bk._sync_target_password_file("t1")
    assert not f.exists()


def test_sync_is_best_effort_on_rqlite_error(tmp_path, monkeypatch):
    monkeypatch.setattr(bk, "CREDENTIALS_DIR", tmp_path)
    f = bk._target_password_file("t1")
    f.write_text("last-known")
    f.chmod(0o600)

    def _boom(tid):
        raise RuntimeError("rqlite transient")

    monkeypatch.setattr(bk.bs, "backup_target_repo_password", _boom)
    bk._sync_target_password_file("t1")          # must not raise
    assert f.read_text() == "last-known"         # left as-is (keeps backing up)
