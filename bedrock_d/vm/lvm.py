"""LV-pair sizing + lifecycle helpers for per-resource DRBD storage.

Per docs/storage-architecture.md, every DRBD resource gets ONE thin
data LV and ONE thin meta LV in the single ``thinpool``. ``max-peers``
is baked at create-md (default 7); bitmap space is pre-reserved
so adding peers later doesn't need downtime.

This module owns:

- ``meta_size_mb_for(data_gb, max_peers=7)`` — pure math; testable
- ``lv_names_for(resource)`` — canonical `bedrock-data-<r>` +
  `bedrock-meta-<r>` (one place; no string-concat scatter)
- ``lvcreate_pair(host, resource, data_gb)`` — actually does it
  via shell on ``host``; idempotent (checks ``lvs`` first)
- ``lvremove_pair(host, resource)`` — same shape, reverse direction
- ``lvextend_data(host, resource, new_gb)`` — online grow

# Why these are math-only helpers vs. saga steps

The saga's step bodies just call these. Keeping the math here makes
it independently unit-testable + keeps the saga step bodies short.
"""
from __future__ import annotations

import math
import subprocess
from dataclasses import dataclass
from typing import Optional


# Constants from docs/storage-architecture.md
VG_NAME              = "bedrock"
THINPOOL             = "thinpool"
DEFAULT_MAX_PEERS    = 7

# DRBD9 external metadata layout (constants from upstream docs):
#   - 4 MiB superblock header
#   - 32 MiB activity log (fixed)
#   - per-peer bitmap: 1 bit per 4 KiB of data
#
# Bitmap bits per GiB of data = (1 GiB / 4 KiB) = 262_144 bits/peer/GiB
# That's 32 KiB of bitmap per GiB of data per peer.
DRBD_HEADER_MB       = 4
DRBD_AL_MB           = 32
DRBD_BITMAP_KIB_PER_GB_PER_PEER = 32   # 256 Kbits = 32 KiB


@dataclass(frozen=True)
class LvPair:
    """Names of the two LVs that back one DRBD resource."""
    data_lv: str          # e.g. "bedrock-data-vm-foo-disk0"
    meta_lv: str          # e.g. "bedrock-meta-vm-foo-disk0"


def lv_names_for(resource: str) -> LvPair:
    """Canonical LV names for a DRBD resource.

    Resource names look like ``vm-<name>-disk<N>`` (per-VM-disk) or
    ``cluster`` (the cluster-singleton). Either way the LV pair
    is just ``bedrock-data-<resource>`` and ``bedrock-meta-<resource>``."""
    if not resource:
        raise ValueError("resource name required")
    return LvPair(
        data_lv=f"bedrock-data-{resource}",
        meta_lv=f"bedrock-meta-{resource}",
    )


def meta_size_mb_for(data_gb: int,
                     max_peers: int = DEFAULT_MAX_PEERS) -> int:
    """Pure function: how many MiB of meta LV to provision for
    ``data_gb`` of data and ``max_peers`` future peers.

    Formula (DRBD9 external metadata):

        header + AL + (max_peers × bitmap_per_peer)
        = 4 MiB + 32 MiB + (max_peers × data_gb × 32 KiB)

    Rounded UP to integer MiB, then up to nearest 4 MiB (LVM extent
    alignment friendly).

    Examples:
      data_gb=20,    max_peers=7 → 36 + 4.4 → 44 MiB → 44 MiB
      data_gb=100,   max_peers=7 → 36 + 22  → 58 MiB → 60 MiB
      data_gb=1024,  max_peers=7 → 36 + 224 → 260 MiB
      data_gb=10240, max_peers=7 → 36 + 2240 → 2276 MiB

    The meta LV is thin-provisioned so consumed bytes scale with
    actual peer activity. Even huge virtual sizes are cheap until
    peers actually dirty bitmap bits.
    """
    if data_gb < 1:
        raise ValueError("data_gb must be >= 1")
    if max_peers < 1 or max_peers > 15:
        raise ValueError("max_peers must be 1..15 (DRBD9 limit)")
    bitmap_kib = max_peers * data_gb * DRBD_BITMAP_KIB_PER_GB_PER_PEER
    bitmap_mb = math.ceil(bitmap_kib / 1024)
    total_mb = DRBD_HEADER_MB + DRBD_AL_MB + bitmap_mb
    # Round up to nearest 4 MiB so the LV is extent-aligned and
    # has a touch of headroom for DRBD format drift.
    return ((total_mb + 3) // 4) * 4


def lv_path(lv: str, vg: str = VG_NAME) -> str:
    """Device-mapper path for a thin LV."""
    return f"/dev/{vg}/{lv}"


# ─── Shell-out helpers (host-aware) ─────────────────────────────────


def _run_on(host: str, cmd: str, *, check: bool = True,
            timeout: int = 60) -> tuple[int, str, str]:
    """SSH-on-the-master pattern: if ``host`` is empty or matches
    the local hostname, run locally; else SSH. Used by the saga
    step bodies — keeps each step body short.

    Returns (rc, stdout, stderr). Raises CalledProcessError if
    ``check`` and rc != 0."""
    if host and host not in ("localhost", _local_hostname()):
        full = (
            f"ssh -o StrictHostKeyChecking=no -o BatchMode=yes "
            f"-o ConnectTimeout=5 root@{host} '{cmd}'"
        )
    else:
        full = cmd
    r = subprocess.run(full, shell=True, capture_output=True,
                       text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise subprocess.CalledProcessError(
            r.returncode, full, output=r.stdout, stderr=r.stderr,
        )
    return r.returncode, r.stdout, r.stderr


def _local_hostname() -> str:
    import socket
    try:
        return socket.gethostname()
    except OSError:
        return ""


# ─── LV lifecycle ───────────────────────────────────────────────────


def lv_exists(host: str, lv: str, vg: str = VG_NAME) -> bool:
    """Quick existence check via ``lvs``. Idempotency-check seam
    for the create / remove sagas."""
    rc, _out, _err = _run_on(host, f"lvs --noheadings -o lv_name {vg}/{lv}",
                              check=False, timeout=10)
    return rc == 0


def lvcreate_pair(host: str, resource: str, data_gb: int, *,
                  max_peers: int = DEFAULT_MAX_PEERS,
                  vg: str = VG_NAME,
                  thinpool: str = THINPOOL) -> LvPair:
    """Create the data + meta LV pair for ``resource`` on ``host``.
    Idempotent — skips either LV if it already exists. Returns the
    pair of LV names (also computable from ``lv_names_for``)."""
    pair = lv_names_for(resource)
    meta_mb = meta_size_mb_for(data_gb, max_peers=max_peers)

    if not lv_exists(host, pair.data_lv, vg=vg):
        _run_on(host,
                f"lvcreate -y -V {data_gb}G --thin "
                f"-n {pair.data_lv} {vg}/{thinpool}")
    if not lv_exists(host, pair.meta_lv, vg=vg):
        _run_on(host,
                f"lvcreate -y -V {meta_mb}M --thin "
                f"-n {pair.meta_lv} {vg}/{thinpool}")
    return pair


def lvremove_pair(host: str, resource: str, *,
                  vg: str = VG_NAME) -> None:
    """Remove both LVs. Idempotent — missing LV is success."""
    pair = lv_names_for(resource)
    for lv in (pair.data_lv, pair.meta_lv):
        if lv_exists(host, lv, vg=vg):
            _run_on(host, f"lvremove -fy {vg}/{lv}", check=False)


def lvextend_data(host: str, resource: str, new_gb: int, *,
                  vg: str = VG_NAME) -> None:
    """Extend the data LV to ``new_gb``. Idempotent (LVM no-ops if
    target size <= current). Companion ``lvextend_meta`` runs
    automatically inside the grow saga when meta room is short."""
    pair = lv_names_for(resource)
    _run_on(host, f"lvextend -L {new_gb}G --no-resize-fs "
                  f"{vg}/{pair.data_lv} 2>&1 || true", check=False)


def lvextend_meta(host: str, resource: str, data_gb: int, *,
                  max_peers: int = DEFAULT_MAX_PEERS,
                  vg: str = VG_NAME) -> None:
    """Extend the meta LV to match meta_size_mb_for(data_gb).
    Idempotent; LVM no-ops if target <= current."""
    pair = lv_names_for(resource)
    new_mb = meta_size_mb_for(data_gb, max_peers=max_peers)
    _run_on(host, f"lvextend -L {new_mb}M {vg}/{pair.meta_lv} "
                  f"2>&1 || true", check=False)


# Backwards-compat aliases — at runtime the saga steps call these.
data_lv_for = lambda r: lv_names_for(r).data_lv  # noqa: E731
meta_lv_for = lambda r: lv_names_for(r).meta_lv  # noqa: E731
