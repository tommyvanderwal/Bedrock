"""VM failover — pure helpers.

State-machine logic + side-effecting orchestration live in
``bedrock_d/orchestrator/vm_failover.py``. This module is the pure
helpers those tasks call: read a DRBD UUID off disk, look up the
cluster's last-known UUID from rqlite, decide who is next in line
after a dead primary, write the post-promote UUID back through
quorum.

Design points worth keeping in front:

- **rqlite.drbd_resources.current_uuid is the cluster's record of
  the last node-id that successfully promoted this resource**. It's
  written via a normal (Raft-replicated) UPDATE by whichever node
  just ran ``drbdadm primary``. Quorum confirms before the write
  returns.
- **Strong-reads** of that column are used by the surviving node
  immediately before it starts a VM, to verify that no later
  takeover-by-someone-else has happened that the local cache might
  not see yet. The strong-read forces a Raft leader round-trip.
- **The local DRBD current-uuid** is read from debugfs
  (``/sys/kernel/debug/drbd/resources/<r>/volumes/0/data_gen_id``);
  works for UP resources. Falls back to ``drbdadm dump-md`` for
  detached resources (rare in the failover path; DRBD must be UP
  to do anything useful with it).
- **vms.failover_order** holds the predetermined sequence. The
  takeover protocol asks ``peers_after_dead(failover_order, me,
  dead_host)`` to determine "am I next in line"; pure list arithmetic,
  no I/O.
"""

from __future__ import annotations

import json
import subprocess
from typing import Optional


# ─────────────────────────────────────────────────────────────────────
# Pure logic — no I/O
# ─────────────────────────────────────────────────────────────────────


def peers_after_dead(failover_order: list[str], me: str,
                     dead_host: str) -> bool:
    """True if `me` is the next non-dead entry in `failover_order`
    after `dead_host`. False otherwise (not in list, not next, or
    dead_host not in list).

    Caller passes a list of currently-dead hosts via repeated calls
    if more than one node has died — for a single dead primary,
    `dead_host` is the only one to skip past.

    Examples:
      peers_after_dead(["A", "B"],      "B", "A")   → True   (pet)
      peers_after_dead(["A", "B", "C"], "B", "A")   → True   (vipet, B is secondary)
      peers_after_dead(["A", "B", "C"], "C", "A")   → False  (B should take it first)
      peers_after_dead(["A", "B", "C"], "C", "B")   → True   (C is tertiary; cascade
                                                              if B was already taken
                                                              over from earlier)
      peers_after_dead([],              "X", "A")   → False  (cattle, no failover)
    """
    if not failover_order or dead_host not in failover_order or me not in failover_order:
        return False
    dead_idx = failover_order.index(dead_host)
    me_idx = failover_order.index(me)
    return me_idx == dead_idx + 1


# ─────────────────────────────────────────────────────────────────────
# Local DRBD I/O
# ─────────────────────────────────────────────────────────────────────


def read_local_drbd_uuid(resource_name: str) -> str:
    """Return the current-UUID for a DRBD resource on this node, as
    a lowercase hex string without the `0x` prefix. Returns `""` if
    the resource isn't configured here.

    Primary source: debugfs (works while DRBD is UP). Fallback:
    `drbdadm dump-md` (works when DRBD is detached). Mirrors the
    same logic cluster_arbiter._read_local_drbd_uuid uses for the
    `cluster` singleton."""
    debugfs = (
        f"/sys/kernel/debug/drbd/resources/{resource_name}"
        f"/volumes/0/data_gen_id"
    )
    try:
        with open(debugfs, "r") as f:
            first = f.readline().strip()
        if first.startswith("0x"):
            return first[2:].lower()
    except OSError:
        pass
    try:
        out = subprocess.check_output(
            ["drbdadm", "dump-md", resource_name], timeout=3,
        )
        for line in out.decode().splitlines():
            s = line.strip()
            if s.startswith("current-uuid"):
                parts = s.split()
                if len(parts) >= 2:
                    return parts[1].rstrip(";").lower().replace("0x", "")
    except Exception:
        pass
    return ""


# ─────────────────────────────────────────────────────────────────────
# rqlite reads + writes for the DRBD-UUID record
# ─────────────────────────────────────────────────────────────────────


def get_recorded_uuid(resource_name: str,
                      *, level: str = "strong") -> Optional[str]:
    """Read the cluster's last-known authoritative DRBD UUID for a
    resource. Defaults to level='strong' (Raft leader round-trip),
    which is what the pre-start safety check needs to ensure we are
    not racing a takeover-by-someone-else. Pass level='none' for
    forensic lookups that don't need linearizability."""
    try:
        from lib import rqlite_client
    except ImportError:
        import sys as _sys
        _sys.path.insert(0, "/usr/local/lib/bedrock")
        from lib import rqlite_client  # type: ignore
    with rqlite_client.RqliteClient() as rc:
        row = rc.query_one(
            "SELECT current_uuid FROM drbd_resources WHERE name = ?",
            params=[resource_name], level=level,
        )
    if row is None:
        return None
    return (row.get("current_uuid") or "").lower() or None


def record_uuid_after_promote(resource_name: str) -> str:
    """Read the local DRBD current-UUID (must be post-`drbdadm primary`)
    and write it to rqlite via UPDATE. The write goes through Raft so
    quorum confirms before the function returns. Returns the UUID that
    was written, so the caller can sanity-check.

    Caller responsibility: only invoke this AFTER a successful
    `drbdadm primary` on this node. Otherwise the local UUID is the
    pre-promote value and recording it does nothing useful."""
    try:
        from lib import bedrock_state as _bs
    except ImportError:
        import sys as _sys
        _sys.path.insert(0, "/usr/local/lib/bedrock")
        from lib import bedrock_state as _bs  # type: ignore
    local = read_local_drbd_uuid(resource_name)
    if not local:
        raise RuntimeError(
            f"record_uuid_after_promote: DRBD {resource_name} has no "
            f"readable current-UUID. Was drbdadm primary actually run?"
        )
    _bs.drbd_resource_uuid_set(resource_name, local)
    return local


# ─────────────────────────────────────────────────────────────────────
# Pre-start safety
# ─────────────────────────────────────────────────────────────────────


class _Verdict:
    __slots__ = ("safe", "reason")

    def __init__(self, safe: bool, reason: str):
        self.safe = bool(safe)
        self.reason = reason

    def __bool__(self) -> bool:
        return self.safe

    def __repr__(self) -> str:
        return f"<Verdict safe={self.safe} reason={self.reason!r}>"


def is_safe_to_start_vm(vm_name: str, disks: list[str]) -> _Verdict:
    """Pre-start safety check for a failover-takeover. For each
    DRBD resource (`disks`) backing this VM:

      1. Strong-read the cluster's recorded current-UUID.
      2. Read the local DRBD current-UUID.
      3. They MUST match exactly. Mismatch means either:
         - the cluster knows about a UUID this node doesn't have
           (our local DRBD is behind — promoting + starting the VM
           would silently lose writes). Refuse.
         - or the cluster's record hasn't been updated yet by some
           in-flight write (rare; the surviving node's caller did
           record_uuid_after_promote BEFORE calling this — so this
           case shouldn't happen for the takeover path).

    If `disks` is empty (cattle VM) the call returns safe=True — no
    DRBD to check.

    Returns a _Verdict that is truthy on safe, with `.reason`
    populated on refusal for logging."""
    if not disks:
        return _Verdict(True, "no DRBD disks (cattle VM)")
    for resource in disks:
        local = read_local_drbd_uuid(resource)
        if not local:
            return _Verdict(
                False, f"{resource}: no local DRBD UUID readable",
            )
        recorded = get_recorded_uuid(resource, level="strong")
        if recorded is None:
            return _Verdict(
                False,
                f"{resource}: no recorded UUID in rqlite "
                f"(local={local[:12]}); refusing to start without "
                f"a quorum-confirmed baseline",
            )
        if recorded.lower() != local.lower():
            return _Verdict(
                False,
                f"{resource}: UUID mismatch — local={local[:12]}, "
                f"recorded={recorded[:12]}. Refusing to start VM "
                f"{vm_name!r} — either local DRBD is behind or a "
                f"later takeover has happened we don't yet know of. "
                f"Operator must reconcile (drbdadm invalidate, or "
                f"verify which node has the authoritative data).",
            )
    return _Verdict(True, f"{len(disks)} disk(s) UUID-matched")
