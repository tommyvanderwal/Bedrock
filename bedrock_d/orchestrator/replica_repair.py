"""ReplicaRepair saga — rebuild one replica after permanent host loss.

Submitted by the self-heal calm loop (``self_heal.py``), one resource at
a time. Crash-safe like every runtime saga: rqlite-backed, idempotent
steps, picked up by the boot resume sweep if bedrock-d dies mid-repair.

Two shapes, selected by ``ctx['kind']``:

  - ``singleton``: add a node to the 3-way ``cluster`` resource.
    Delegates to ``tier_storage.promote_cluster_to_3way`` (the existing,
    tested singleton expand path) and records the new member in
    ``cluster_drbd_membership``.

  - ``pet`` / ``vipet``: add a node to a per-VM DRBD resource — lvcreate
    the data+meta pair on the target, extend the .res to include the new
    peer (keeping every existing peer's stable DRBD node-id), distribute
    + ``drbdadm adjust`` on all holders, bring the new replica up so it
    resyncs from the primary, and append the new node to the VM's
    ``failover_order`` + the resource's ``peers``.

ctx inputs (from self_heal._submit_repair):
  - resource:  str          (DRBD resource name)
  - kind:      'singleton' | 'pet' | 'vipet'
  - target:    node_name     (the node to host the new replica)
  - vm_name:   str           ('' for the singleton)
"""
from __future__ import annotations

import json
import logging
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "installer"))

from bedrock_d.orchestrator.sagas import saga, step  # noqa: E402

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Pure helpers — unit-tested directly
# ─────────────────────────────────────────────────────────────────────


def parse_node_ids(res_text: str) -> dict[str, int]:
    """Map node_name → node-id from a rendered .res file's ``on``
    blocks. Empty dict if the file is missing/unparsable."""
    out: dict[str, int] = {}
    for m in re.finditer(
            r"on\s+(\S+)\s*\{[^}]*?node-id\s+(\d+)\s*;", res_text,
            flags=re.DOTALL):
        out[m.group(1)] = int(m.group(2))
    return out


def next_free_node_id(used: dict[str, int], max_peers: int = 7) -> int:
    """Smallest node-id in 0..max_peers-1 not already taken. Keeps
    existing assignments stable (DRBD node-ids are permanent, L3)."""
    taken = set(used.values())
    for i in range(max_peers):
        if i not in taken:
            return i
    raise RuntimeError(f"no free DRBD node-id (max_peers={max_peers})")


# ─────────────────────────────────────────────────────────────────────
# Saga
# ─────────────────────────────────────────────────────────────────────


@saga("replica_repair")
class ReplicaRepair:
    """Restore redundancy for ONE resource onto ONE target node."""

    @step("validate")
    def step_validate(self, ctx):
        for k in ("resource", "kind", "target"):
            if not ctx.get(k):
                raise ValueError(f"missing required ctx field: {k}")
        from lib import rqlite_client
        with rqlite_client.RqliteClient() as rc:
            rows = rc.query(
                "SELECT name, minor, data_size_bytes, max_peers, peers "
                "FROM drbd_resources WHERE name = ?",
                params=[ctx["resource"]], level="none")
        if not rows:
            raise RuntimeError(
                f"resource {ctx['resource']!r} has no drbd_resources row")
        row = rows[0]
        ctx["minor"] = int(row["minor"])
        ctx["data_size_bytes"] = int(row["data_size_bytes"])
        ctx["max_peers"] = int(row.get("max_peers") or 7)
        try:
            ctx["existing_peers"] = json.loads(row.get("peers") or "[]")
        except (TypeError, json.JSONDecodeError):
            ctx["existing_peers"] = []
        if ctx["target"] in ctx["existing_peers"]:
            # Idempotent: already added (resumed run).
            log.info("replica_repair: %s already includes %s — nothing to do",
                     ctx["resource"], ctx["target"])
            ctx["already_member"] = True

    @step("repair")
    def step_repair(self, ctx):
        if ctx.get("already_member"):
            return
        if ctx["kind"] == "singleton":
            self._repair_singleton(ctx)
        else:
            self._repair_vm_replica(ctx)

    # ─── singleton ───────────────────────────────────────────────────

    def _repair_singleton(self, ctx):
        try:
            from installer.lib import tier_storage as _ts
        except ImportError:
            sys.path.insert(0, "/usr/local/lib/bedrock")
            from lib import tier_storage as _ts  # type: ignore
        from lib import rqlite_client, bedrock_state as _bs
        with rqlite_client.RqliteClient() as rc:
            nrows = rc.query(
                "SELECT loopback_ip FROM nodes WHERE node_name = ?",
                params=[ctx["target"]], level="none")
        lo = (nrows[0]["loopback_ip"] if nrows else "") or ""
        _ts.promote_cluster_to_3way(
            {"name": ctx["target"], "loopback_ip": lo})
        now = int(time.time())
        with rqlite_client.RqliteClient() as rc:
            rc.execute(
                "INSERT OR REPLACE INTO cluster_drbd_membership "
                "(node_name, joined_at, updated_at) VALUES (?, ?, ?)",
                params=[ctx["target"], now, now])
            rqlite_client.bump_revision(rc)
        log.warning("replica_repair: singleton restored onto %s",
                    ctx["target"])

    # ─── per-VM replica ──────────────────────────────────────────────

    def _repair_vm_replica(self, ctx):
        from bedrock_d.vm import lvm as _lvm, drbd_config as _cfg
        from lib import rqlite_client, bedrock_state as _bs

        resource = ctx["resource"]
        target = ctx["target"]
        new_peers = list(ctx["existing_peers"]) + [target]

        # Resolve host + loopback for every peer (existing + new).
        ph = ",".join("?" * len(new_peers))
        with rqlite_client.RqliteClient() as rc:
            nrows = rc.query(
                f"SELECT node_name, host, loopback_ip FROM nodes "
                f"WHERE node_name IN ({ph})",
                params=new_peers, level="none")
        info = {r["node_name"]: r for r in nrows}
        for n in new_peers:
            if not info.get(n, {}).get("host") or not info.get(n, {}).get("loopback_ip"):
                raise RuntimeError(f"node {n!r} missing host/loopback in rqlite")
        target_host = info[target]["host"]

        # Learn existing node-ids from a holder's live .res so we keep
        # them stable, then assign the smallest free id to the new peer.
        node_ids: dict[str, int] = {}
        res_path = _cfg.res_file_path(resource)
        for p in ctx["existing_peers"]:
            text = _lvm._run_on(info[p]["host"], f"cat {res_path}",
                                check=False)[1]
            node_ids = parse_node_ids(text)
            if node_ids:
                break
        # Fall back to positional ids if no holder had a readable .res
        # (fresh cluster edge case): assign 0..n-1 to existing peers.
        for i, p in enumerate(ctx["existing_peers"]):
            node_ids.setdefault(p, i)
        node_ids[target] = next_free_node_id(node_ids, ctx["max_peers"])

        peers_meta = [
            _cfg.Peer(node_name=n, host=info[n]["host"],
                      loopback_ip=info[n]["loopback_ip"],
                      node_id=node_ids[n])
            for n in new_peers
        ]

        # 1. lvcreate the data+meta pair on the new target.
        data_gb = max(1, ctx["data_size_bytes"] // (1024 ** 3))
        _lvm.lvcreate_pair(target_host, resource, data_gb,
                           max_peers=ctx["max_peers"])

        # 2. Render the extended .res and distribute to ALL peers.
        config = _cfg.render(resource, minor=ctx["minor"], peers=peers_meta,
                             max_peers=ctx["max_peers"])
        for p in new_peers:
            _lvm._run_on(info[p]["host"],
                         f"cat > {res_path} << 'EOF'\n{config}EOF")

        # 3. New replica: create-md + up so it resyncs from the primary.
        #    Existing holders: drbdadm adjust to pick up the new peer.
        _lvm._run_on(target_host,
                     f"drbdadm create-md --force --max-peers={ctx['max_peers']} "
                     f"{resource} 2>&1 | tail -2", check=False)
        for p in ctx["existing_peers"]:
            _lvm._run_on(info[p]["host"], f"drbdadm adjust {resource}",
                         check=False)
        _lvm._run_on(target_host, f"drbdadm up {resource}", check=False)

        # 4. Persist the new membership + extend failover_order so the
        #    new node is a recognised takeover target.
        now = int(time.time())
        with rqlite_client.RqliteClient() as rc:
            fo_rows = rc.query(
                "SELECT failover_order FROM vms WHERE vm_name = ?",
                params=[ctx.get("vm_name", "")], level="none")
            rc.execute(
                "UPDATE drbd_resources SET peers = ?, updated_at = ? "
                "WHERE name = ?",
                params=[json.dumps(new_peers), now, resource])
            if fo_rows:
                try:
                    order = json.loads(fo_rows[0].get("failover_order") or "[]")
                except (TypeError, json.JSONDecodeError):
                    order = []
                if target not in order:
                    order.append(target)
                rc.execute(
                    "UPDATE vms SET failover_order = ?, updated_at = ? "
                    "WHERE vm_name = ?",
                    params=[json.dumps(order), now, ctx.get("vm_name", "")])
            rqlite_client.bump_revision(rc)
        log.warning("replica_repair: %s replica restored onto %s "
                    "(peers now %s)", resource, target, new_peers)
