"""Self-heal — replica repair after PERMANENT host loss (SG-05).

When a node has been gone for a calm-down period (default 65 min;
configurable so tests can set ~1 min) the cluster treats it as
permanently lost and rebuilds the redundancy that node was providing —
**one resource at a time**, in a strict priority order:

  1. the arbiter / ``cluster`` singleton first (small + critical);
  2. **pets** that dropped to a single replica → restore the 2nd,
     ordered by HA-importance high → normal → low;
  3. **vipets** that dropped to 2-way → restore the 3rd, same order.

HARD GATE (invariant): a repair NEVER pushes a target node past 80 %
disk usage. The size charged against the target is the resource's
**actual thin-LV usage** (real allocated blocks via ``lvs data_percent``
* ``lv_size``), not its provisioned maximum. If no eligible node can
take a resource without crossing the gate, that resource stays degraded
and the dashboard surfaces it — we never act unsafely; the operator adds
capacity / a node.

Ownership: this is a **calm loop that runs only on the mgmt-master**
(single writer, has quorum). It reads cluster state from rqlite, picks
exactly one repair per pass, and submits a ``replica_repair`` saga
(crash-safe like every other runtime op). The pure planning + gate
logic below has no I/O so it is unit-tested directly.

The 65-min calm-down is tracked in-process on the leader, seeded the
first tick a node is observed gone and cleared the moment it returns.
A leader change re-arms the timer from zero — that only ever DELAYS a
repair, never triggers one early, which is the safe direction on a
"only the paranoid survive" platform.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Optional

log = logging.getLogger("bedrock.self_heal")

# Calm-down before a gone node is treated as permanently lost. Override
# via BEDROCK_SELF_HEAL_CALM_S (tests set ~60). Load-bearing default.
CALM_DOWN_S = float(os.environ.get("BEDROCK_SELF_HEAL_CALM_S", 65 * 60))

# Hard disk-usage ceiling for any repair target.
DISK_GATE = 0.80

TICK_S = 30.0  # calm loop cadence

# A peer whose freshest mesh heartbeat is older than this is "gone right
# now" — the seed for the calm-down clock. Matches the takeover path's
# peer-down threshold so "gone" means the same thing in both places; the
# long CALM_DOWN_S is what actually gates a repair.
PEER_DOWN_S = 35.0

_PRIORITY_RANK = {"high": 0, "normal": 1, "low": 2}

CLUSTER_RESOURCE = "cluster"
SINGLETON_CAP = 3  # cluster singleton is capped at a 3-way set


# ─────────────────────────────────────────────────────────────────────
# Pure planning + the disk gate — no I/O, unit-tested directly
# ─────────────────────────────────────────────────────────────────────


def _want_replicas(vm_type: str) -> int:
    return {"cattle": 1, "pet": 2, "vipet": 3}.get(vm_type, 1)


def fits_under_gate(used_bytes: int, total_bytes: int,
                    add_bytes: int, gate: float = DISK_GATE) -> bool:
    """True iff placing ``add_bytes`` on a node currently at
    ``used_bytes`` of ``total_bytes`` keeps it at/under ``gate``.
    A node with unknown capacity (total<=0) never fits — we refuse
    rather than guess."""
    if total_bytes <= 0:
        return False
    return (used_bytes + add_bytes) <= (gate * total_bytes)


def pick_target(*, current_peers: list[str], candidates: list[str],
                add_bytes: int, usage: dict[str, dict],
                gate: float = DISK_GATE) -> Optional[str]:
    """Choose the node to host a new replica.

    ``candidates`` are active, non-lost nodes in deterministic order.
    A node is eligible iff it does NOT already hold the resource and it
    stays under the disk gate after adding ``add_bytes`` (sized from
    actual thin usage). Returns the first eligible node, or None if none
    fits (→ resource stays degraded)."""
    held = set(current_peers)
    for node in candidates:
        if node in held:
            continue
        u = usage.get(node) or {}
        if fits_under_gate(int(u.get("used_bytes", 0)),
                           int(u.get("total_bytes", 0)),
                           add_bytes, gate):
            return node
    return None


def compute_repair_plan(*, active_nodes: list[str], lost_nodes: list[str],
                        singleton_peers: list[str],
                        vm_resources: list[dict],
                        usage: dict[str, dict],
                        resource_sizes: dict[str, int],
                        gate: float = DISK_GATE) -> dict:
    """Decide the SINGLE next repair action, in the locked priority
    order. Pure — every input is already-materialised cluster state.

    Args:
      active_nodes:    node_names currently present + healthy (not lost,
                       not in maintenance). Deterministically ordered.
      lost_nodes:      node_names permanently lost (gone ≥ calm-down).
      singleton_peers: node_names currently hosting the ``cluster``
                       resource.
      vm_resources:    one dict per replicated VM disk:
                         {"resource", "vm_name", "vm_type", "priority",
                          "peers": [node_name,...]}
      usage:           node_name → {"used_bytes","total_bytes"} (the
                       node's local thinpool/disk, post any in-flight
                       repair the caller already applied).
      resource_sizes:  resource_name → actual thin-usage bytes on a
                       current holder (what gets charged to the target).

    Returns one of:
      {"action": "repair", "resource", "kind", "target", "reason"}
      {"action": "none"}                      — nothing needs repair
      {"action": "degraded", "resource", "kind", "reason"}
                                              — needs repair, nothing fits
    """
    lost = set(lost_nodes)
    candidates = [n for n in active_nodes if n not in lost]

    # ── (1) arbiter / cluster singleton — restore toward a 3-way set ──
    healthy_singleton = [n for n in singleton_peers if n not in lost]
    target_singleton = min(SINGLETON_CAP, len(candidates))
    if len(healthy_singleton) < target_singleton:
        add = int(resource_sizes.get(CLUSTER_RESOURCE, 0))
        tgt = pick_target(current_peers=healthy_singleton,
                          candidates=candidates, add_bytes=add,
                          usage=usage, gate=gate)
        if tgt:
            return {"action": "repair", "resource": CLUSTER_RESOURCE,
                    "kind": "singleton", "target": tgt,
                    "reason": f"singleton at {len(healthy_singleton)}-way, "
                              f"want {target_singleton}-way"}
        return {"action": "degraded", "resource": CLUSTER_RESOURCE,
                "kind": "singleton",
                "reason": "no node fits the cluster singleton under the "
                          "80% disk gate"}

    # ── (2)+(3) per-VM replicas, pets before vipets, then high→low ──
    def order_key(r: dict) -> tuple:
        # pets (want 2) before vipets (want 3); then HA-importance.
        type_rank = 0 if r.get("vm_type") == "pet" else 1
        return (type_rank, _PRIORITY_RANK.get(r.get("priority"), 1),
                r.get("resource", ""))

    degraded: Optional[dict] = None
    for r in sorted(vm_resources, key=order_key):
        want = _want_replicas(r.get("vm_type", ""))
        healthy_peers = [p for p in (r.get("peers") or []) if p not in lost]
        if len(healthy_peers) >= want:
            continue   # this resource is whole
        add = int(resource_sizes.get(r["resource"], 0))
        tgt = pick_target(current_peers=healthy_peers,
                          candidates=candidates, add_bytes=add,
                          usage=usage, gate=gate)
        if tgt:
            return {"action": "repair", "resource": r["resource"],
                    "kind": r.get("vm_type", ""), "target": tgt,
                    "vm_name": r.get("vm_name", ""),
                    "reason": f"{r.get('vm_type')} {r.get('vm_name')!r} at "
                              f"{len(healthy_peers)}-way, want {want}-way"}
        # Remember the first thing that needs repair but doesn't fit so
        # we can surface it if nothing else fits either.
        if degraded is None:
            degraded = {"action": "degraded", "resource": r["resource"],
                        "kind": r.get("vm_type", ""),
                        "vm_name": r.get("vm_name", ""),
                        "reason": "no node fits this replica under the "
                                  "80% disk gate"}

    if degraded is not None:
        return degraded
    return {"action": "none"}


# ─────────────────────────────────────────────────────────────────────
# I/O helpers — read cluster state + node disk usage
# ─────────────────────────────────────────────────────────────────────


def _rqlite():
    try:
        from lib import rqlite_client
    except ImportError:
        import sys as _sys
        _sys.path.insert(0, "/usr/local/lib/bedrock")
        from lib import rqlite_client  # type: ignore
    return rqlite_client


def _ssh(host: str, cmd: str, timeout: int = 20) -> str:
    """Run a command on a peer via root ssh. Mirrors tier_storage.ssh's
    single-quoting so remote ``$``/awk survive the local shell."""
    import shlex
    import subprocess
    full = (f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
            f"-o ConnectTimeout=8 root@{host} {shlex.quote(cmd)}")
    r = subprocess.run(full, shell=True, capture_output=True, text=True,
                       timeout=timeout)
    return r.stdout.strip() if r.returncode == 0 else ""


def node_disk_usage(host: str) -> dict:
    """Return {"used_bytes","total_bytes"} for a node's bedrock thinpool
    via ``lvs`` over SSH. Usage = lv_size * data_percent/100 (actual
    allocated thin blocks); capacity = lv_size of the thinpool. Empty
    dict (→ treated as "doesn't fit") if the node is unreachable."""
    raw = _ssh(host,
               "lvs --units b --nosuffix --separator '|' --noheadings "
               "-o lv_size,data_percent --select 'lv_attr=~\"^t\"' "
               "2>/dev/null")
    total = 0
    used = 0
    for line in raw.splitlines():
        parts = [p.strip() for p in line.split("|") if p.strip() != ""]
        if len(parts) < 2:
            continue
        try:
            size_b = int(float(parts[0]))
            pct = float(parts[1])
        except ValueError:
            continue
        total += size_b
        used += int(size_b * pct / 100.0)
    if total <= 0:
        return {}
    return {"used_bytes": used, "total_bytes": total}


def resource_thin_usage(host: str, data_lv: str, vg: str) -> int:
    """Actual allocated bytes of a resource's thin data LV on a current
    holder (``lv_size * data_percent``). 0 if unreadable — the gate then
    treats it as free, which the caller must guard against by only
    trusting a positive reading."""
    raw = _ssh(host,
               f"lvs --units b --nosuffix --separator '|' --noheadings "
               f"-o lv_size,data_percent {vg}/{data_lv} 2>/dev/null")
    line = raw.splitlines()[0] if raw else ""
    parts = [p.strip() for p in line.split("|") if p.strip() != ""]
    if len(parts) < 2:
        return 0
    try:
        return int(float(parts[0]) * float(parts[1]) / 100.0)
    except ValueError:
        return 0


def _load_cluster_state() -> dict:
    """Read everything the planner needs from rqlite (level='none').
    Returns a dict with active_nodes, singleton_peers, vm_resources,
    node_hosts. Raises on rqlite error so the caller can retry."""
    rqlite_client = _rqlite()
    with rqlite_client.RqliteClient() as rc:
        nodes = rc.query(
            "SELECT node_name, host, maintenance FROM nodes", level="none")
        vms = rc.query(
            "SELECT vm_name, vm_type, priority FROM vms "
            "WHERE vm_type IN ('pet','vipet')", level="none")
        drbd = rc.query(
            "SELECT name, data_lv, peers FROM drbd_resources", level="none")
        # The cluster singleton's membership lives in the `tiers` table
        # (tier_storage.set_tier_state), NOT drbd_resources.
        trows = rc.query(
            "SELECT peers FROM tiers WHERE tier_name = ?",
            params=[CLUSTER_RESOURCE], level="none")
    node_hosts = {n["node_name"]: n.get("host", "") for n in nodes}
    active = [n["node_name"] for n in nodes if not n.get("maintenance")]
    vm_meta = {v["vm_name"]: v for v in vms}
    try:
        singleton_peers = json.loads(
            (trows[0].get("peers") if trows else None) or "[]")
    except (TypeError, json.JSONDecodeError):
        singleton_peers = []
    vm_resources: list[dict] = []
    # The singleton's data LV name is deterministic (bedrock-data-cluster);
    # per-VM LVs come from their drbd_resources rows below.
    data_lvs: dict[str, str] = {CLUSTER_RESOURCE: f"bedrock-data-{CLUSTER_RESOURCE}"}
    for row in drbd:
        name = row["name"]
        if name == CLUSTER_RESOURCE:
            continue
        try:
            peers = json.loads(row.get("peers") or "[]")
        except (TypeError, json.JSONDecodeError):
            peers = []
        data_lvs[name] = row.get("data_lv", "")
        # vm-<name>-diskN → look up the VM's type + priority.
        vm_name = _vm_name_from_resource(name)
        meta = vm_meta.get(vm_name)
        if not meta:
            continue   # cattle or unknown — no replica target
        vm_resources.append({
            "resource": name, "vm_name": vm_name,
            "vm_type": meta.get("vm_type", ""),
            "priority": meta.get("priority", "normal"),
            "peers": peers,
        })
    return {
        "active_nodes": sorted(active),
        "node_hosts": node_hosts,
        "singleton_peers": singleton_peers,
        "vm_resources": vm_resources,
        "data_lvs": data_lvs,
    }


def _vm_name_from_resource(resource: str) -> str:
    """vm-<name>-disk<N> → <name>. Returns '' for non-VM resources."""
    if not resource.startswith("vm-"):
        return ""
    body = resource[len("vm-"):]
    idx = body.rfind("-disk")
    return body[:idx] if idx >= 0 else body


def _self_node_name() -> str:
    from pathlib import Path
    try:
        return (json.loads(Path("/etc/bedrock/state.json").read_text())
                or {}).get("node_name") or ""
    except Exception:
        return ""


def _is_leader(self_name: str) -> bool:
    try:
        rqlite_client = _rqlite()
        with rqlite_client.RqliteClient() as rc:
            row = rc.query_one(
                "SELECT mgmt_master FROM cluster_info WHERE id = 1",
                level="none")
        return (row or {}).get("mgmt_master") == self_name
    except Exception:
        return False


def _peers_down_now(max_age_s: float = PEER_DOWN_S) -> set[str]:
    """Names of peers the mesh layer currently considers unreachable
    (newest neighbour last_seen older than max_age_s). Reuses
    vm_failover's mesh-freshness view so 'down' means the same thing
    here as in the takeover path."""
    try:
        from bedrock_d.orchestrator import vm_failover as _vmf
        return set(_vmf._peers_observed_down(max_age_s))
    except Exception:
        return set()


# ─────────────────────────────────────────────────────────────────────
# Calm loop
# ─────────────────────────────────────────────────────────────────────


async def self_heal_task():
    """Leader-only calm loop: detect permanent host loss and submit one
    ``replica_repair`` saga per pass, in the locked priority order,
    under the 80% disk gate."""
    log.info("self_heal: started (calm-down=%.0fs, disk-gate=%.0f%%)",
             CALM_DOWN_S, DISK_GATE * 100)
    # node_name → monotonic time we first saw it gone this leadership.
    down_since: dict[str, float] = {}
    while True:
        await asyncio.sleep(TICK_S)
        try:
            self_name = _self_node_name()
            if not self_name or not _is_leader(self_name):
                down_since.clear()   # re-arm calm-down on role loss
                continue
            await asyncio.to_thread(_self_heal_pass, self_name, down_since)
        except Exception as e:
            log.warning("self_heal: tick failed: %s", e)


def _self_heal_pass(self_name: str, down_since: dict[str, float]) -> None:
    """One leader pass: refresh the lost-node set against the calm-down,
    compute the plan, and submit at most one repair saga."""
    state = _load_cluster_state()
    active = state["active_nodes"]
    node_hosts = state["node_hosts"]

    # Detect permanently-lost nodes: gone (mesh-unreachable) continuously
    # for ≥ CALM_DOWN_S. A node back from the dead clears its timer.
    now = time.monotonic()
    down_now = _peers_down_now(PEER_DOWN_S)   # peers gone right now
    for n in list(down_since):
        if n not in down_now or n not in active:
            down_since.pop(n, None)
    for n in down_now:
        if n in active and n != self_name:
            down_since.setdefault(n, now)
    lost = [n for n, t0 in down_since.items()
            if (now - t0) >= CALM_DOWN_S]
    if not lost:
        return

    # Size every resource that might need repair from a current holder,
    # and gather each candidate node's disk usage.
    usage: dict[str, dict] = {}
    for n in active:
        if n in lost:
            continue
        host = node_hosts.get(n, "")
        if host:
            u = node_disk_usage(host)
            if u:
                usage[n] = u

    resource_sizes = _measure_resource_sizes(state, lost, node_hosts)

    plan = compute_repair_plan(
        active_nodes=active, lost_nodes=lost,
        singleton_peers=state["singleton_peers"],
        vm_resources=state["vm_resources"],
        usage=usage, resource_sizes=resource_sizes,
    )
    if plan["action"] == "none":
        return
    if plan["action"] == "degraded":
        log.warning("self_heal: %s %r stays DEGRADED — %s (operator must "
                    "add capacity / a node)", plan["kind"],
                    plan["resource"], plan["reason"])
        return

    log.warning("self_heal: repairing %s %r → target %r (%s)",
                plan["kind"], plan["resource"], plan["target"],
                plan["reason"])
    _submit_repair(self_name, plan)


def _measure_resource_sizes(state: dict, lost: list[str],
                            node_hosts: dict[str, str]) -> dict[str, int]:
    """Charge each repairable resource by its ACTUAL thin usage on a
    surviving holder. Resources we can't measure are charged 0 only if
    no holder is reachable — but pick_target then still gates on the
    target node's own total, so a 0 here can't bypass the ceiling for a
    full node; it just under-counts a single resource we couldn't read."""
    try:
        from bedrock_d.vm import lvm as _lvm
        vg = _lvm.VG_NAME
    except Exception:
        vg = "bedrock"
    data_lvs = state["data_lvs"]
    lostset = set(lost)
    sizes: dict[str, int] = {}

    def holder_host(peers: list[str]) -> str:
        for p in peers:
            if p not in lostset:
                h = node_hosts.get(p, "")
                if h:
                    return h
        return ""

    # singleton
    sing = state["singleton_peers"]
    h = holder_host(sing)
    if h and data_lvs.get(CLUSTER_RESOURCE):
        sizes[CLUSTER_RESOURCE] = resource_thin_usage(
            h, data_lvs[CLUSTER_RESOURCE], vg)
    # per-VM
    for r in state["vm_resources"]:
        h = holder_host(r.get("peers") or [])
        lv = data_lvs.get(r["resource"], "")
        if h and lv:
            sizes[r["resource"]] = resource_thin_usage(h, lv, vg)
    return sizes


def _submit_repair(self_name: str, plan: dict) -> None:
    """Submit ONE replica_repair saga and run it inline (so the calm
    loop never has two repairs racing). The saga is rqlite-backed, so a
    crash mid-repair is picked up by the boot resume sweep."""
    try:
        from bedrock_d.orchestrator.sagas import SagaExecutor
        from bedrock_d.orchestrator.sagas.rqlite_backend import (
            RqliteSagaBackend,
        )
        from bedrock_d import state as _st
        from bedrock_d.orchestrator import replica_repair  # noqa: F401
        backend = RqliteSagaBackend(_st.RqliteClient())
        ex = SagaExecutor(backend=backend, this_node=self_name)
        op_id = ex.submit(
            kind="replica_repair", target_node=self_name,
            params={"resource": plan["resource"], "kind": plan["kind"],
                    "target": plan["target"],
                    "vm_name": plan.get("vm_name", "")},
            requested_by="self_heal")
        ex.execute_one(op_id)
        log.info("self_heal: replica_repair op=%d submitted", op_id)
    except Exception as e:
        log.warning("self_heal: repair submit failed: %s "
                    "(will retry next calm pass)", e)
