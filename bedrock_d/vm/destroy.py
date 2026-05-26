"""VmDestroy saga — `bedrock vm delete` flow.

Reverses everything VmCreate set up, in safe order: kill the
running domain first, undefine, drop DRBD, remove LVs, delete
rqlite rows. Each step is idempotent — running destroy twice on
a half-destroyed VM converges.

ctx inputs:
  - vm_name: str

ctx fills as it runs:
  - resource: str (vm-<name>-disk0)
  - peers:    list[node_name]
  - minor:    int (used by some down-shaping checks)
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "installer"))

from bedrock_d.orchestrator.sagas import saga, step  # noqa: E402
from . import drbd_config as _cfg
from . import lvm as _lvm

log = logging.getLogger(__name__)


@saga("vm_destroy")
class VmDestroy:
    """Tear down a VM end-to-end."""

    @step("load_resource_metadata")
    def step_load(self, ctx):
        """Read drbd_resources to learn the peer list + minor.
        If the row is gone (idempotent re-run), set ``already_gone``
        and let subsequent steps no-op."""
        from bedrock_d import state as _st
        resource = f"vm-{ctx['vm_name']}-disk0"
        with _st.RqliteClient() as client:
            rows = client.query(
                "SELECT minor, peers FROM drbd_resources WHERE name = ?",
                params=[resource],
            )
        if not rows:
            log.info("vm_destroy: %s already removed from "
                     "drbd_resources", resource)
            ctx["already_gone"] = True
            return
        ctx["resource"] = resource
        ctx["minor"]    = int(rows[0]["minor"])
        ctx["peers"]    = json.loads(rows[0]["peers"] or "[]")

    @step("virsh_destroy_running")
    def step_destroy_domain(self, ctx):
        """``virsh destroy`` on the home node (the one currently
        running the VM). On every other peer the VM is defined but
        not running, so destroy returns an error we ignore."""
        if ctx.get("already_gone"):
            return
        for host in _peer_hosts(ctx["peers"]):
            _lvm._run_on(
                host, f"virsh destroy {ctx['vm_name']} 2>/dev/null || true",
                check=False,
            )

    @step("virsh_undefine")
    def step_undefine(self, ctx):
        """``virsh undefine`` on every peer. Removes the domain
        XML from libvirt. Idempotent."""
        if ctx.get("already_gone"):
            return
        for host in _peer_hosts(ctx["peers"]):
            _lvm._run_on(
                host,
                f"virsh undefine --nvram {ctx['vm_name']} "
                f"2>/dev/null || true",
                check=False,
            )

    @step("drbd_down")
    def step_drbd_down(self, ctx):
        """``drbdadm down <resource>`` on every peer. Idempotent —
        a resource that isn't up returns non-zero, which is fine."""
        if ctx.get("already_gone"):
            return
        for host in _peer_hosts(ctx["peers"]):
            _lvm._run_on(
                host,
                f"drbdadm down {ctx['resource']} 2>/dev/null || true",
                check=False,
            )

    @step("drbd_wipe_md")
    def step_wipe_md(self, ctx):
        """Wipe the external meta LV so a future re-create starts
        clean. ``drbdadm wipe-md`` zeroes the meta superblock + AL +
        bitmap. Idempotent — wiping already-wiped meta is a no-op."""
        if ctx.get("already_gone"):
            return
        for host in _peer_hosts(ctx["peers"]):
            _lvm._run_on(
                host,
                f"drbdadm wipe-md --force {ctx['resource']} "
                f"2>/dev/null || true",
                check=False,
            )

    @step("remove_drbd_res_file")
    def step_remove_res(self, ctx):
        """Drop /etc/drbd.d/<resource>.res on every peer. Idempotent
        (rm -f)."""
        if ctx.get("already_gone"):
            return
        res_path = _cfg.res_file_path(ctx["resource"])
        for host in _peer_hosts(ctx["peers"]):
            _lvm._run_on(host, f"rm -f {res_path}", check=False)

    @step("lvremove_pair")
    def step_lvremove(self, ctx):
        """Drop both LVs (data + meta) on every peer. Idempotent."""
        if ctx.get("already_gone"):
            return
        for host in _peer_hosts(ctx["peers"]):
            _lvm.lvremove_pair(host, ctx["resource"])

    @step("delete_rqlite_rows")
    def step_delete_rows(self, ctx):
        """Final cleanup: remove the drbd_resources + vms rows.
        Operator-visible 'gone' state."""
        from bedrock_d import state as _st
        resource = ctx.get("resource") or f"vm-{ctx['vm_name']}-disk0"
        with _st.RqliteClient() as client:
            client.execute(
                "DELETE FROM drbd_resources WHERE name = ?",
                params=[resource],
            )
            client.execute(
                "DELETE FROM vms WHERE vm_name = ?",
                params=[ctx["vm_name"]],
            )


def _peer_hosts(peer_names: list[str]) -> list[str]:
    if not peer_names:
        return []
    from lib import rqlite_client
    placeholders = ",".join("?" * len(peer_names))
    with rqlite_client.RqliteClient() as _rc:
        rows = _rc.query(
            f"SELECT node_name, host FROM nodes WHERE node_name IN ({placeholders})",
            params=peer_names, level="none",
        )
    hosts = {r["node_name"]: r["host"] for r in rows}
    return [hosts[n] for n in peer_names if hosts.get(n)]
