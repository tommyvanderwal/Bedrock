"""VM failover state machine.

Three async tasks, each independently safe to run continuously:

  ① suspend_on_no_quorum_task
      When this node has been marked "no quorum" for ≥ 20 s,
      virsh-suspend every locally-running pet/vipet VM. Suspended
      state is held in RAM (no disk writes) and persisted to a
      local file keyed by the QUORUM-LOSS timestamp (the no-quorum
      marker's mtime), so the kill timer counts 5 min from the
      connection drop — not from suspend — and survives bedrock-d
      restarts. Cattle VMs are not suspended (local LV, no failover
      meaning).

  ② takeover_after_peer_down_task
      For every peer whose last heartbeat is ≥ 35 s old AND we have
      rqlite quorum, look at the vms table: any VM where
      `vms.host == dead_peer` AND `peers_after_dead(vms.failover_order,
      me, dead_peer)` triggers a takeover sequence here:
        a. drbdadm disconnect each affected resource (terminate the
           inbound replication that would otherwise refuse local
           writes)
        b. drbdadm primary each one (DRBD bumps the current-UUID)
        c. record_uuid_after_promote — write the new UUID back to
           rqlite, quorum-confirmed
        d. is_safe_to_start_vm — strong-read confirmation
        e. virsh define + virsh start
        f. UPDATE vms SET host = me so the cluster knows.

  ③ kill_suspended_after_5min_task
      Any VM still down 5 minutes after quorum was lost gets
      virsh-destroyed and removed from the local-state file. They've
      been taken over elsewhere by now; the local memory copy is just
      consuming RAM.

The 15 s gap between suspend (T+20) and takeover (T+35) is by design:
the dying node has up to ~5 s after the no-quorum signal to issue
its virsh suspend; the surviving node then has 10 s of settling
margin for in-flight DRBD writes to drain before it disconnects the
replication and promotes locally.

5 minutes is aligned with Active Directory's Kerberos 5-min
authentication window — past that, the cluster has long since
moved the VM and a frozen suspended copy is purely waste.

The local "no quorum" signal is the file ``/run/bedrock-no-quorum``,
written by netd's election layer when this node's weighted-vote
sum falls below majority. We use the marker only as the trigger
for the suspend timer here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("bedrock.vm_failover")

# Timing constants — load-bearing, match docs/cluster-quorum-spec.md
# and the VM-failover design discussion. Bump cautiously.
#
# Per Tommy's spec: suspend at T+20s wall-clock from partition. The
# no-quorum marker drops ~9s after partition (netd's single
# SELF_DEMOTE_MISSES detector), so threshold = 5s puts the suspend at
# partition+~14s. The no_quorum_responder in mgmt/orchestrator.py
# also suspends every running VM at marker+1s — this vm_failover
# task is the selective belt-and-suspenders for the pet/vipet case
# specifically (no_quorum_responder is unconditional / cattle too).
SUSPEND_AFTER_NO_QUORUM_S  = 5.0     # T+~20 wall-clock from partition
TAKEOVER_AFTER_PEER_DOWN_S = 35.0    # T+35: surviving node promotes
                                     #       (5 s extra over T+30 lets DRBD
                                     #        in-flight writes settle before
                                     #        the new primary disconnects)
KILL_AFTER_QUORUM_LOSS_S   = 5 * 60  # kill 5 min after QUORUM LOSS (not after
                                     # suspend): the clock starts when the
                                     # connection drops (no-quorum marker mtime),
                                     # and the ~20 s suspend happens *within*
                                     # that window, not added to it. Per Tommy:
                                     # "when the connection is lost, the VM is
                                     # turned off after 5 minutes; from ~20 s it
                                     # gets suspended first."

TICK_S = 5.0     # all three tasks run on this cadence

# Local "this node currently does NOT see cluster quorum" marker.
# Written by netd's election layer when our weighted-vote sum falls
# below majority.
NO_QUORUM_MARKER   = Path("/run/bedrock-no-quorum")
SUSPENDED_VMS_FILE = Path("/var/lib/bedrock/suspended-vms.json")


# ─────────────────────────────────────────────────────────────────────
# Local helpers
# ─────────────────────────────────────────────────────────────────────


def _now() -> float:
    return time.time()


_STATE_JSON = Path("/etc/bedrock/state.json")


def _self_node_name() -> str:
    try:
        return (json.loads(_STATE_JSON.read_text()) or {}).get("node_name") or ""
    except Exception:
        return ""


def _load_suspended_record() -> dict[str, float]:
    """Load the {vm_name: quorum_loss_ts} map. Returns {} on any error.
    The timestamp is the QUORUM-LOSS episode start (no-quorum marker
    mtime), NOT the suspend time. This file is the persistent state that
    lets the kill timer survive bedrock-d restarts: a VM stays in the
    record until either resumed (we remove the entry) or killed at
    quorum_loss_ts + 5min."""
    if not SUSPENDED_VMS_FILE.exists():
        return {}
    try:
        return json.loads(SUSPENDED_VMS_FILE.read_text())
    except Exception:
        return {}


def _save_suspended_record(record: dict[str, float]) -> None:
    """tmp+rename atomic write. Empty record removes the file."""
    if not record:
        try:
            SUSPENDED_VMS_FILE.unlink(missing_ok=True)
        except OSError:
            pass
        return
    SUSPENDED_VMS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SUSPENDED_VMS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=2))
    os.replace(tmp, SUSPENDED_VMS_FILE)


def drop_suspended(vm_names: list[str]) -> None:
    """Remove ``vm_names`` from the suspended-vms record (VM-01).

    Called by the recovery path (mgmt/orchestrator._reconcile_paused_vms)
    the moment it ``virsh resume``s a VM on quorum-return: a VM that came
    back inside the 5-min window is healthy and must NOT be killed by
    kill_suspended_after_5min_task. No-op for names not in the record."""
    if not vm_names:
        return
    record = _load_suspended_record()
    changed = False
    for vm in vm_names:
        if record.pop(vm, None) is not None:
            changed = True
            log.info("vm_failover: dropped resumed VM %r from suspended "
                     "record (no longer eligible for the 5-min kill)", vm)
    if changed:
        _save_suspended_record(record)


def _virsh_domstate(vm_name: str) -> str:
    """Return the libvirt domain state ('running', 'paused', 'shut off',
    …) or '' if the lookup fails. Used by the kill task to re-check a
    VM's live state before destroying it."""
    rc, out, _ = _virsh("domstate", vm_name)
    return out.strip() if rc == 0 else ""


def _virsh(*args: str, timeout: float = 30.0) -> tuple[int, str, str]:
    """Run a virsh command. Returns (rc, stdout, stderr)."""
    try:
        r = subprocess.run(
            ["virsh", *args], capture_output=True, timeout=timeout,
        )
        return r.returncode, r.stdout.decode(errors="replace"), r.stderr.decode(errors="replace")
    except subprocess.TimeoutExpired as e:
        return 124, "", f"timeout after {timeout}s: {e}"
    except Exception as e:
        return 1, "", f"{type(e).__name__}: {e}"


def _local_pet_vipet_vms(states: tuple = ("running",)) -> list[str]:
    """List local VMs in the given libvirt state(s) that are pet or
    vipet (cattle skipped — they have no DRBD, no failover meaning,
    leave them alone). Queries rqlite for vm_type (level='none',
    works without quorum).

    `states` defaults to ('running',). Pass ('paused',) to find
    already-suspended pet/vipet VMs for adoption into the
    suspended-vms record."""
    try:
        from lib import rqlite_client
    except ImportError:
        import sys as _sys
        _sys.path.insert(0, "/usr/local/lib/bedrock")
        from lib import rqlite_client  # type: ignore
    names: list[str] = []
    for st in states:
        rc_v, out, _ = _virsh("list", "--name", f"--state-{st}")
        if rc_v != 0:
            continue
        names.extend(n.strip() for n in out.splitlines() if n.strip())
    running = list(dict.fromkeys(names))   # de-dup, preserve order
    if not running:
        return []
    placeholders = ",".join("?" * len(running))
    with rqlite_client.RqliteClient() as rc:
        rows = rc.query(
            f"SELECT vm_name, vm_type FROM vms "
            f"WHERE vm_name IN ({placeholders})",
            params=running, level="none",
        )
    return [r["vm_name"] for r in rows if r.get("vm_type") in ("pet", "vipet")]


def _vms_on_dead_peer(dead_peer: str, me: str) -> list[dict]:
    """Return rows (vm_name, vm_type, failover_order JSON-decoded) for
    VMs where vms.host == dead_peer AND this node is next in line
    per failover_order."""
    try:
        from lib import rqlite_client
        from bedrock_d.vm.failover import peers_after_dead
    except ImportError:
        import sys as _sys
        _sys.path.insert(0, "/usr/local/lib/bedrock")
        from lib import rqlite_client  # type: ignore
        from bedrock_d.vm.failover import peers_after_dead  # type: ignore
    with rqlite_client.RqliteClient() as rc:
        rows = rc.query(
            "SELECT vm_name, vm_type, failover_order FROM vms "
            "WHERE host = ?",
            params=[dead_peer], level="none",
        )
    out = []
    for r in rows:
        try:
            order = json.loads(r.get("failover_order") or "[]")
        except (TypeError, json.JSONDecodeError):
            order = []
        if peers_after_dead(order, me, dead_peer):
            out.append({
                "vm_name": r["vm_name"],
                "vm_type": r.get("vm_type", ""),
                "failover_order": order,
            })
    return out


def _vm_disks(vm_name: str) -> list[str]:
    """Return EVERY DRBD resource name backing this VM, so the takeover
    sequence promotes + UUID-checks all of them (VM-04 — multi-disk).
    Reads drbd_resources (level='none'; works without a fresh leader).
    Falls back to the single-disk convention if the table is empty so
    a legacy single-disk VM still fails over."""
    try:
        from lib import rqlite_client
    except ImportError:
        import sys as _sys
        _sys.path.insert(0, "/usr/local/lib/bedrock")
        from lib import rqlite_client  # type: ignore
    with rqlite_client.RqliteClient() as rc:
        rows = rc.query(
            "SELECT name FROM drbd_resources WHERE name LIKE ? "
            "ORDER BY name",
            params=[f"vm-{vm_name}-disk%"], level="none",
        )
    names = [r["name"] for r in rows]
    return names or [f"vm-{vm_name}-disk0"]


def _peers_observed_down(max_age_s: float) -> list[str]:
    """Names of peers that have been observed at some point but whose
    newest mesh-neighbour `last_seen` is now older than `max_age_s`.

    Reads from mgmt.orchestrator._STATE.netd — the unified daemon's
    shared Daemon object — so the freshness view is exactly what the
    election tick saw. A peer counts as "down" iff:
      - it is in d.ever_seen_peers (we have positively observed it
        in this lifetime), AND
      - every Neighbour entry for it has last_seen older than the
        threshold (or there are no entries at all because
        sweep_hysteresis aged them out).

    `last_seen` is set off of monotonic-now by netd, so we compare
    against time.monotonic(). Self is never returned."""
    try:
        from mgmt import orchestrator as _orch
    except Exception:
        return []
    state = getattr(_orch, "_STATE", None)
    if state is None or state.netd is None:
        return []
    now_mono = time.monotonic()
    me = ""
    try:
        me = state.self_node_name or _self_node_name()
    except Exception:
        me = _self_node_name()
    with state.netd_lock:
        d = state.netd
        ever = set(getattr(d, "ever_seen_peers", set()))
        newest: dict[str, float] = {}
        for n in getattr(d, "neighbours", {}).values():
            peer = getattr(n, "peer_node", "")
            if not peer or peer == me:
                continue
            ls = float(getattr(n, "last_seen", 0.0) or 0.0)
            if ls > newest.get(peer, 0.0):
                newest[peer] = ls
    down: list[str] = []
    for peer in ever:
        if peer == me:
            continue
        latest = newest.get(peer, 0.0)
        # No live neighbour entry at all, or the freshest one is stale.
        if latest <= 0.0 or (now_mono - latest) >= max_age_s:
            down.append(peer)
    return down


def _rqlite_quorate() -> bool:
    """True if local rqlite has a quorate leader reachable. Reuses
    netd's _rqlite_ready probe semantics — a level='strong' SELECT 1
    succeeds only when the cluster has a leader and we can talk to
    it."""
    try:
        try:
            from lib import rqlite_client
        except ImportError:
            import sys as _sys
            _sys.path.insert(0, "/usr/local/lib/bedrock")
            from lib import rqlite_client  # type: ignore
        with rqlite_client.RqliteClient() as rc:
            rc.query("SELECT 1", level="strong")
        return True
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────
# ① suspend_on_no_quorum
# ─────────────────────────────────────────────────────────────────────


async def suspend_on_no_quorum_task():
    """Every TICK_S: if the no-quorum marker is older than
    SUSPEND_AFTER_NO_QUORUM_S, ensure every local pet/vipet VM is
    suspended AND recorded in suspended-vms.json (so the kill timer
    can fire 5 min later).

    Two-path adoption: mgmt/orchestrator's no_quorum_responder polls
    the marker every 1s and virsh-suspends VMs as part of its
    cleanup — that path wins the suspend race but does not write
    to suspended-vms.json. This tick adopts already-paused pet/vipet
    VMs into the record at the marker's mtime (close enough to the
    actual suspend time for the kill timer to be accurate)."""
    log.info("vm_failover: suspend_on_no_quorum_task started "
             "(no-quorum-age threshold = %.1fs)",
             SUSPEND_AFTER_NO_QUORUM_S)
    while True:
        await asyncio.sleep(TICK_S)
        try:
            if not NO_QUORUM_MARKER.exists():
                continue
            marker_mtime = NO_QUORUM_MARKER.stat().st_mtime
            marker_age = _now() - marker_mtime
            if marker_age < SUSPEND_AFTER_NO_QUORUM_S:
                continue
            running = _local_pet_vipet_vms()
            paused = _local_pet_vipet_vms(states=("paused",))
            record = _load_suspended_record()
            dirty = False
            # 1. Suspend any still-running pet/vipet VMs
            for vm in running:
                if vm in record:
                    continue
                rc_v, _, err = _virsh("suspend", vm)
                if rc_v == 0:
                    record[vm] = marker_mtime
                    dirty = True
                    log.warning(
                        "vm_failover: suspended VM %r at no-quorum age "
                        "%.1fs (kill at quorum-loss+%ds if no recovery)",
                        vm, marker_age, KILL_AFTER_QUORUM_LOSS_S,
                    )
                else:
                    log.error(
                        "vm_failover: failed to suspend VM %r: %s",
                        vm, err.strip(),
                    )
            # 2. Adopt already-paused pet/vipet VMs into the record.
            # mgmt/orchestrator's no_quorum_responder pauses them in
            # its own cleanup pass — without this adoption step, the
            # kill_suspended_after_5min_task never sees them.
            # Anchor to marker_mtime (= quorum-loss episode start), the
            # SAME anchor as the path-1 suspend above. The kill clock
            # runs 5 min from QUORUM LOSS, not from suspend/adoption, so
            # both paths must use the marker time (Tommy 2026-05-29). The
            # marker is idempotent (created once per no-quorum episode by
            # election.set_no_quorum_marker, cleared by netd on quorum
            # return), so its mtime is the partition start even if
            # bedrock-d restarted mid-partition — a VM still paused 5 min
            # after the connection dropped is overdue and correctly
            # killed, regardless of when this daemon adopted it.
            for vm in paused:
                if vm in record:
                    continue
                record[vm] = marker_mtime
                dirty = True
                log.warning(
                    "vm_failover: adopted already-paused VM %r "
                    "(kill at quorum-loss+%ds if no recovery)", vm,
                    KILL_AFTER_QUORUM_LOSS_S,
                )
            if dirty:
                _save_suspended_record(record)
        except Exception as e:
            log.warning("vm_failover: suspend tick: %s", e)


# ─────────────────────────────────────────────────────────────────────
# ② takeover_after_peer_down
# ─────────────────────────────────────────────────────────────────────


def _takeover_one(vm_name: str, disks: list[str], me: str) -> bool:
    """Execute the takeover sequence for a single VM. Returns True on
    success (VM running on this node), False on any refusal."""
    try:
        from bedrock_d.vm.failover import (
            record_uuid_after_promote, is_safe_to_start_vm,
        )
        from lib import bedrock_state as _bs
    except ImportError:
        import sys as _sys
        _sys.path.insert(0, "/usr/local/lib/bedrock")
        from bedrock_d.vm.failover import (  # type: ignore
            record_uuid_after_promote, is_safe_to_start_vm,
        )
        from lib import bedrock_state as _bs  # type: ignore

    # a. disconnect inbound DRBD replication
    for resource in disks:
        rc = subprocess.run(
            ["drbdadm", "disconnect", resource],
            capture_output=True, timeout=10,
        )
        if rc.returncode != 0:
            log.warning(
                "vm_failover: drbdadm disconnect %s rc=%d stderr=%s",
                resource, rc.returncode,
                rc.stderr.decode(errors='replace')[:200],
            )

    # b. promote each disk
    for resource in disks:
        rc = subprocess.run(
            ["drbdadm", "primary", resource],
            capture_output=True, timeout=10,
        )
        if rc.returncode != 0:
            # Retry with --force; the spec's takeover protocol allows
            # this because the surviving side has the safety checks
            # (UUID + recorded_uuid match) coming up next.
            rc = subprocess.run(
                ["drbdadm", "--", "--force", "primary", resource],
                capture_output=True, timeout=10,
            )
        if rc.returncode != 0:
            log.error(
                "vm_failover: drbdadm primary %s FAILED (rc=%d, %s) — "
                "refusing takeover of VM %r",
                resource, rc.returncode,
                rc.stderr.decode(errors='replace')[:200], vm_name,
            )
            return False

    # c. record post-promote UUID in rqlite, quorum-confirmed
    for resource in disks:
        try:
            uuid = record_uuid_after_promote(resource)
            log.info(
                "vm_failover: recorded post-promote UUID for %s = %s",
                resource, uuid[:12],
            )
        except Exception as e:
            log.error(
                "vm_failover: record_uuid_after_promote(%s) FAILED: %s — "
                "refusing takeover of VM %r",
                resource, e, vm_name,
            )
            return False

    # d. strong-read sanity check
    verdict = is_safe_to_start_vm(vm_name, disks)
    if not verdict:
        log.error(
            "vm_failover: pre-start safety REFUSED takeover of %r: %s",
            vm_name, verdict.reason,
        )
        return False

    # e. virsh define + start
    rc_d = _virsh("dominfo", vm_name)
    if rc_d[0] != 0:
        log.warning(
            "vm_failover: VM %r is not defined here yet — XML must "
            "be on shared storage or reconstructed. (TODO: implement "
            "automatic re-define from cluster state)", vm_name,
        )
    rc_s = _virsh("start", vm_name)
    if rc_s[0] != 0:
        log.error(
            "vm_failover: virsh start %r FAILED: %s",
            vm_name, rc_s[2].strip(),
        )
        return False

    # f. update vms.host so the cluster knows
    try:
        _bs.vm_state_change(name=vm_name, host=me, state="running")
        log.warning(
            "vm_failover: TAKEOVER COMPLETE — VM %r now running on %r",
            vm_name, me,
        )
    except Exception as e:
        log.warning(
            "vm_failover: vm_state_change(%r) write failed: %s",
            vm_name, e,
        )
    return True


async def takeover_after_peer_down_task():
    """Every TICK_S: if rqlite is quorate AND any known peer's last
    heartbeat is ≥ TAKEOVER_AFTER_PEER_DOWN_S old, look at VMs that
    were running on that peer and take over the ones where we are
    next in failover_order."""
    log.info(
        "vm_failover: takeover_after_peer_down_task started "
        "(peer-down threshold = %.1fs)", TAKEOVER_AFTER_PEER_DOWN_S,
    )
    me = ""
    # one-shot in-progress set so we don't re-attempt the same VM
    # every tick while the first attempt's drbd ops are still in
    # flight. Keyed by vm_name; reset when the takeover completes
    # (success or failure logged).
    in_progress: set[str] = set()
    while True:
        await asyncio.sleep(TICK_S)
        try:
            if not me:
                me = _self_node_name()
                if not me:
                    continue
            if not _rqlite_quorate():
                continue
            down_peers = _peers_observed_down(TAKEOVER_AFTER_PEER_DOWN_S)
            for dead in down_peers:
                vms = _vms_on_dead_peer(dead, me)
                for vm in vms:
                    if vm["vm_name"] in in_progress:
                        continue
                    in_progress.add(vm["vm_name"])
                    log.warning(
                        "vm_failover: starting takeover of %r "
                        "(was on dead peer %r, failover_order=%s)",
                        vm["vm_name"], dead, vm["failover_order"],
                    )
                    try:
                        _takeover_one(
                            vm["vm_name"], _vm_disks(vm["vm_name"]), me,
                        )
                    finally:
                        in_progress.discard(vm["vm_name"])
        except Exception as e:
            log.warning("vm_failover: takeover tick: %s", e)


# ─────────────────────────────────────────────────────────────────────
# ③ kill_suspended_after_5min
# ─────────────────────────────────────────────────────────────────────


async def kill_suspended_after_5min_task():
    """Every TICK_S: any VM whose record entry (the quorum-loss episode
    start = no-quorum marker mtime) is older than KILL_AFTER_QUORUM_LOSS_S
    gets virsh-destroyed and removed from the record. The clock runs from
    QUORUM LOSS, not from suspend — a VM that has been unreachable 5 min
    after the connection dropped has long since been taken over elsewhere,
    and the frozen local copy is just consuming RAM."""
    log.info(
        "vm_failover: kill_suspended_after_5min_task started "
        "(kill threshold = %ds from quorum loss)", KILL_AFTER_QUORUM_LOSS_S,
    )
    while True:
        await asyncio.sleep(TICK_S)
        try:
            record = _load_suspended_record()
            if not record:
                continue
            now = _now()
            to_kill = [vm for vm, ts in record.items()
                       if (now - ts) >= KILL_AFTER_QUORUM_LOSS_S]
            if not to_kill:
                continue
            for vm in to_kill:
                # Belt-and-suspenders for VM-01: a VM resumed on
                # quorum-return should already be gone from the record,
                # but if the resume path missed it (e.g. resumed by an
                # operator out-of-band), re-check the live state and
                # NEVER destroy a VM that is no longer 'paused'. Just
                # evict the stale record entry.
                domstate = _virsh_domstate(vm)
                if domstate and domstate != "paused":
                    log.info(
                        "vm_failover: VM %r is %r (not paused) at kill "
                        "time — recovered; dropping from record without "
                        "destroying", vm, domstate,
                    )
                    record.pop(vm, None)
                    continue
                rc_v, _, err = _virsh("destroy", vm)
                if rc_v == 0:
                    log.warning(
                        "vm_failover: killed VM %r %ds after quorum loss "
                        "(taken over on a peer by now; freeing memory)",
                        vm, KILL_AFTER_QUORUM_LOSS_S,
                    )
                else:
                    log.error(
                        "vm_failover: virsh destroy %r failed: %s",
                        vm, err.strip(),
                    )
                record.pop(vm, None)
            _save_suspended_record(record)
        except Exception as e:
            log.warning("vm_failover: kill tick: %s", e)


# ─────────────────────────────────────────────────────────────────────
# Public entry point — called from mgmt/orchestrator.start_all
# ─────────────────────────────────────────────────────────────────────


def start_failover_tasks() -> list:
    """Spawn the three failover tasks on the current asyncio loop.
    Returns the task handles (caller doesn't have to keep them; the
    loop owns them). Idempotent at the loop-task level via the
    orchestrator's _TASKS_STARTED guard."""
    return [
        asyncio.create_task(suspend_on_no_quorum_task()),
        asyncio.create_task(takeover_after_peer_down_task()),
        asyncio.create_task(kill_suspended_after_5min_task()),
    ]
