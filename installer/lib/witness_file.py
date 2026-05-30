"""Fileshare witness backend — the slot protocol over a shared directory.

A fileshare witness is a directory both reachable by every node (an NFS/SMB
mount, or a syncing object-store prefix). It plays the SAME role as a bedrock
Echo, but the directory IS the central slot store instead of a UDP daemon:

  * each node writes its OWN slot to ``slot-<node_id>.bin`` via tmp+rename
    (atomic — a reader never sees a torn write);
  * each node reads EVERY ``slot-*.bin`` to learn the others' slots.

The slot bytes are the EXACT AEAD-sealed blob the Echo backend stores
(``witness._encode_slot`` / ``_decode_slot``), so the validity/confirmation
predicates (``_slots_valid`` / ``_slots_confirmed``) and the whole quorum math
apply unchanged — a fileshare witness is "valid + confirmed" under the same
rules as an Echo: the directory holds a fresh slot for every active member AND
this node's own slot reads back with our current marker.

Freshness comes from each slot's own ``ts_writer`` (vs the reader's clock),
NOT from any reply/round-trip timestamp — there is no request/response here.
A nice consequence: a node's OWN valid+confirmed verdict depends only on its
OWN clock (it wrote ts_writer and reads now from the same clock; other members'
slots only need to be PRESENT, not fresh), so cross-node clock skew can't flip
a fileshare witness the way it could a naive shared-store design.

Atomicity caveat: ``os.replace`` (rename-over-existing) is atomic on POSIX and
NFS, but some SMB/CIFS servers do NOT honour atomic rename-over-existing. A
fileshare witness over CIFS must be mounted against a server that does (or use
an NFS/object-store backend); that constraint belongs to the backend wiring,
not this transport module.

This module is TRANSPORT-ONLY and has NO netd/election-path coupling: it just
reads/writes files. Wiring it into the 1Hz election tick (OFF the hot path,
because SMB/S3 latency must not stall mesh+election) is a separate step.
"""
from __future__ import annotations

import os
import time
from typing import Dict, Optional

try:                       # imported both as a package and bare on sys.path
    from . import witness   # type: ignore  # slot codec + validity predicates
except ImportError:         # pragma: no cover
    import witness          # type: ignore


_SLOT_PREFIX = "slot-"
_SLOT_SUFFIX = ".bin"


def slot_filename(node_id: int) -> str:
    return f"{_SLOT_PREFIX}{int(node_id)}{_SLOT_SUFFIX}"


def write_own_slot(ws: "witness.WitnessState", base_dir: str,
                   *, now_ms: Optional[int] = None) -> None:
    """Write THIS node's slot to ``<base_dir>/slot-<my_node_id>.bin`` atomically
    (tmp in the same dir + os.replace, which is atomic on POSIX and the only
    way to never expose a torn slot to a concurrent reader). The payload is the
    same AEAD-sealed blob the Echo backend stores. Raises on IO error — the
    caller decides (a write failure means this witness can't certify us, which
    is the split-brain-safe direction)."""
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    blob = witness._encode_slot(ws, now_ms)
    path = os.path.join(base_dir, slot_filename(ws.my_node_id))
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "wb") as f:
        f.write(blob)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)            # atomic; never a partial slot for a reader


def read_slots(ws: "witness.WitnessState", base_dir: str) -> Dict[int, "witness.Slot"]:
    """Read + decode every ``slot-*.bin`` in ``base_dir`` into {node_id: Slot}.

    Mirrors ``drain_replies``' acceptance rules: a blob that fails AEAD
    verification is silently dropped, and a slot whose node_id is not a current
    member (``ws.member_ids``) is dropped too (INV-7 path b — decommissioning a
    node stops its stale slot from counting, without touching the witness).
    Never raises; an unreadable directory/file yields no slot for it."""
    slots: Dict[int, "witness.Slot"] = {}
    try:
        names = os.listdir(base_dir)
    except OSError:
        return slots
    for name in names:
        if not (name.startswith(_SLOT_PREFIX) and name.endswith(_SLOT_SUFFIX)):
            continue
        try:
            with open(os.path.join(base_dir, name), "rb") as f:
                blob = f.read()
        except OSError:
            continue
        s = witness._decode_slot(ws.cluster_key, blob)
        if s is None:
            continue                 # not AEAD-valid for this cluster_key
        if ws.member_ids is not None and s.node_id not in ws.member_ids:
            continue                 # decommissioned / non-member slot
        slots[s.node_id] = s
    return slots


def is_valid_confirmed(ws: "witness.WitnessState", base_dir: str,
                       *, now_local_ms: Optional[int] = None) -> bool:
    """True iff this fileshare witness is valid AND confirmed RIGHT NOW: the
    directory holds a fresh slot for every active member, and our own slot
    reads back with our current marker. Uses the EXACT Echo predicates, so a
    fileshare witness votes under identical rules. (Caller writes its own slot
    first so the readback can confirm.)"""
    slots = read_slots(ws, base_dir)
    return (witness._slots_valid(ws, slots)
            and witness._slots_confirmed(ws, slots, now_local_ms))


def run_io_cycle(ws: "witness.WitnessState", *,
                 now_ms: Optional[int] = None,
                 now_mono: Optional[float] = None,
                 log=None) -> None:
    """One off-hot-path IO pass over the configured fileshare witnesses.

    For each ``(witness_id, path)`` in ``ws.configured_file_witnesses``: write
    our own slot, then evaluate validity, and cache the result as a
    ``FileWitnessVerdict`` in ``ws.file_witnesses`` for the election tick to
    fold via ``count_valid_confirmed``. The election tick NEVER calls this —
    only the worker thread does — because SMB/S3 latency must not stall the 1Hz
    tick.

    Per-witness isolation + Echo-equivalent blip tolerance:
      * one witness's IO ERROR (share unreachable / unwritable) is logged and
        SKIPPED — its PRIOR verdict is left to age out of the tally over the
        freshness window, exactly like a missed Echo reply, rather than being
        flipped to False on a single transient hiccup;
      * a successful pass that simply isn't valid+confirmed (a member's slot is
        absent, our readback didn't match) records a FRESH False — a real
        "not certifying right now", counted as 0 immediately.

    Verdicts for witnesses no longer configured are pruned (absent from the
    rebuilt map) so a removed fileshare witness drops out at once
    (belt-and-suspenders with the tally's configured-id binding). Only OSError
    is caught here (the expected IO failures); anything else propagates to the
    worker loop, which logs loudly and keeps the thread alive (fail-loud, never
    silently dead).

    THREAD SAFETY: this builds a FRESH verdict map and atomically reassigns
    ``ws.file_witnesses`` — it never mutates the live dict in place. The
    election tick reads ``ws.file_witnesses`` on another thread; under the GIL
    the reassign is atomic, so a mid-read tick simply finishes iterating the old
    map (which is no longer being mutated). No lock, and never a "dict changed
    size during iteration"."""
    if now_mono is None:
        now_mono = time.monotonic()
    prior = ws.file_witnesses
    new_verdicts: Dict[str, "witness.FileWitnessVerdict"] = {}
    for wid, path in list(ws.configured_file_witnesses):
        try:
            write_own_slot(ws, path, now_ms=now_ms)
            ok = is_valid_confirmed(ws, path, now_local_ms=now_ms)
        except OSError as e:
            if log is not None:
                log(f"fileshare witness {wid} ({path}) IO error: {e!r}")
            # Carry the PRIOR verdict forward UNCHANGED so a transient blip ages
            # out over the freshness window instead of flipping to False now.
            if wid in prior:
                new_verdicts[wid] = prior[wid]
            continue
        new_verdicts[wid] = witness.FileWitnessVerdict(
            witness_id=wid, valid_confirmed=ok, evaluated_monotonic=now_mono)
    ws.file_witnesses = new_verdicts        # atomic swap (see THREAD SAFETY)
