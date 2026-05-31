"""storage_mount — the SMB/NFS managed-mount option/command logic (pure parts;
real mounting is e2e). The witness mountpoint must be STRONG (noac / cache=none),
the kopia mountpoint CACHED, and S3 must be rejected as non-mountable."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "installer"))

from lib import storage_mount as sm  # noqa: E402

NFS = {"type": "nfs", "fs_server": "nas.lan", "fs_share": "/volume1/bedrock"}
SMB = {"type": "smb", "fs_server": "nas.lan", "fs_share": "bedrock"}


def test_mountpoints_are_split_by_usage():
    assert str(sm.mountpoint("ep1", sm.KOPIA)) == "/mnt/bedrock/kopia/ep1"
    assert str(sm.mountpoint("ep1", sm.WITNESS)) == "/mnt/bedrock/witness/ep1"
    with pytest.raises(ValueError):
        sm.mountpoint("ep1", "bogus")


def test_nfs_source_and_options():
    assert sm._mount_source(NFS) == "nas.lan:/volume1/bedrock"
    # witness = strong: hard + noac. kopia = cached: hard, NO noac.
    assert "noac" in sm._mount_opts(NFS, sm.WITNESS)
    assert "hard" in sm._mount_opts(NFS, sm.WITNESS)
    assert "noac" not in sm._mount_opts(NFS, sm.KOPIA)
    assert "soft" not in sm._mount_opts(NFS, sm.KOPIA)   # never soft


def test_smb_source_and_options():
    assert sm._mount_source(SMB) == "//nas.lan/bedrock"
    # witness = cache=none (the noac analog); kopia = cache=strict.
    assert "cache=none" in sm._mount_opts(SMB, sm.WITNESS)
    assert "cache=strict" in sm._mount_opts(SMB, sm.KOPIA)
    assert "cache=none" not in sm._mount_opts(SMB, sm.KOPIA)


def test_extra_fs_options_appended():
    ep = dict(NFS, fs_options="rsize=1048576")
    assert "rsize=1048576" in sm._mount_opts(ep, sm.WITNESS)


def test_build_mount_cmd_nfs():
    cmd = sm.build_mount_cmd("ep1", NFS, sm.WITNESS)
    assert cmd[:3] == ["mount", "-t", "nfs"]
    assert "nas.lan:/volume1/bedrock" in cmd
    assert "/mnt/bedrock/witness/ep1" in cmd


def test_build_mount_cmd_smb_requires_creds():
    with pytest.raises(ValueError):
        sm.build_mount_cmd("ep1", SMB, sm.WITNESS)           # no cred_file
    cmd = sm.build_mount_cmd("ep1", SMB, sm.WITNESS, cred_file=Path("/x.cifs"))
    assert cmd[:3] == ["mount", "-t", "cifs"]
    assert any("credentials=/x.cifs" in a and "cache=none" in a for a in cmd)


def test_s3_is_not_mountable():
    with pytest.raises(ValueError):
        sm._mount_source({"type": "s3"})
    with pytest.raises(ValueError):
        sm._mount_opts({"type": "s3"}, sm.WITNESS)


def test_test_endpoint_s3_does_a_real_probe(monkeypatch):
    """S3 test is no longer a no-op: it runs a real PUT/GET/DELETE round-trip via
    witness_s3.probe_writable (empty string = OK). We stub probe_writable so the
    unit test needs no live bucket, and assert test_endpoint threads it through."""
    from lib import witness_s3 as w3
    ep = {"type": "s3", "s3_endpoint": "https://x", "s3_bucket": "b",
          "s3_access_key": "AK"}
    # success: probe returns "" → ok True
    monkeypatch.setattr(w3.S3Config, "from_endpoint",
                        classmethod(lambda cls, e, sk: object()))
    monkeypatch.setattr(w3, "probe_writable", lambda cfg, **k: "")
    ok, reason = sm.test_endpoint(ep, s3_secret_key="SK")
    assert ok is True and "round-trip" in reason
    # failure: probe returns a reason → ok False, reason surfaced
    monkeypatch.setattr(w3, "probe_writable",
                        lambda cfg, **k: "read-after-write mismatch")
    ok, reason = sm.test_endpoint(ep, s3_secret_key="SK")
    assert ok is False and "read-after-write" in reason


# ── lifecycle: desired-set derivation + reconcile planning ──────────────
def _view():
    return {
        "storage_endpoints": {
            "nas1": {"type": "nfs", "fs_server": "nas.lan", "fs_share": "/b"},
            "smb1": {"type": "smb", "fs_server": "win.lan", "fs_share": "b",
                     "fs_username": "svc"},
            "s3a":  {"type": "s3", "s3_endpoint": "https://s3", "s3_bucket": "b"},
        },
        "backup_targets": {
            "t1": {"endpoint_id": "nas1"},          # → kopia mount of nas1
            "t2": {"endpoint_id": "s3a"},           # s3 → NO mount
            "tL": {"endpoint_id": ""},              # legacy inline target → none
        },
        "witnesses": {
            "w1": {"endpoint_id": "smb1"},          # → witness mount of smb1
            "w2": {"endpoint_id": "nas1"},          # → witness mount of nas1 too
            "wE": {"endpoint_id": "s3a"},           # s3 witness → NO mount
        },
    }


def test_desired_mounts_from_view_splits_usage_and_skips_s3():
    specs = sm.desired_mounts_from_view(_view())
    keys = {s.key for s in specs}
    assert keys == {("nas1", sm.KOPIA), ("smb1", sm.WITNESS),
                    ("nas1", sm.WITNESS)}
    # s3 endpoints never produce a mount; legacy ''-endpoint targets neither
    assert not any(eid == "s3a" for eid, _ in keys)


def test_reconcile_plan_is_set_difference():
    desired = {("a", "kopia"), ("b", "witness")}
    present = {("b", "witness"), ("c", "kopia")}
    to_mount, to_unmount = sm._reconcile_plan(desired, present)
    assert to_mount == {("a", "kopia")}
    assert to_unmount == {("c", "kopia")}        # only ever within /mnt/bedrock


def test_reconcile_mounts_mounts_missing_and_unmounts_extra(monkeypatch):
    calls = {"mount": [], "unmount": []}
    monkeypatch.setattr(sm, "current_bedrock_mounts",
                        lambda: {("old", sm.WITNESS)})
    monkeypatch.setattr(sm, "mount_endpoint",
                        lambda eid, ep, usage, **kw: calls["mount"].append((eid, usage, kw)))
    monkeypatch.setattr(sm, "unmount_endpoint",
                        lambda eid, usage: calls["unmount"].append((eid, usage)))
    specs = [sm.MountSpec("nas1", {"type": "nfs"}, sm.KOPIA),
             sm.MountSpec("smb1", {"type": "smb", "fs_username": "svc"}, sm.WITNESS)]
    out = sm.reconcile_mounts(specs, unseal_password=lambda eid: "p4ss")
    assert set(out["mounted"]) == {("nas1", sm.KOPIA), ("smb1", sm.WITNESS)}
    assert out["unmounted"] == [("old", sm.WITNESS)]      # no longer desired
    # smb mount received the unsealed password + username; nfs got neither
    smb = next(c for c in calls["mount"] if c[0] == "smb1")
    assert smb[2]["username"] == "svc" and smb[2]["password"] == "p4ss"


def test_reconcile_isolates_a_failing_mount(monkeypatch):
    monkeypatch.setattr(sm, "current_bedrock_mounts", lambda: set())

    def _flaky(eid, ep, usage, **kw):
        if eid == "bad":
            raise RuntimeError("share unreachable")

    monkeypatch.setattr(sm, "mount_endpoint", _flaky)
    monkeypatch.setattr(sm, "unmount_endpoint", lambda *a: None)
    specs = [sm.MountSpec("bad", {"type": "nfs"}, sm.KOPIA),
             sm.MountSpec("good", {"type": "nfs"}, sm.WITNESS)]
    out = sm.reconcile_mounts(specs, log=lambda m: None)
    assert out["mounted"] == [("good", sm.WITNESS)]       # good still mounted
    assert out["failed"] == [("bad", sm.KOPIA)]           # bad isolated, logged
