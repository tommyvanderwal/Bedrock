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


def test_test_endpoint_s3_is_noop_ok():
    ok, reason = sm.test_endpoint({"type": "s3"})
    assert ok is True and "s3" in reason
