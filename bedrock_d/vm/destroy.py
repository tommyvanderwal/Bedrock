"""VmDestroy saga — `bedrock vm delete` flow.

Reverses everything VmCreate set up, in safe order: kill the running
domain first, undefine, drop DRBD, remove LVs, delete rqlite rows.
Handles every disk of the VM (multi-disk) and both shapes — replicated
disks (DRBD data+meta pair) and cattle disks (a single local LV). Each
step is idempotent — running destroy twice on a half-destroyed VM
converges.

ctx inputs:
  - vm_name: str

ctx fills as it runs:
  - resources: list[str]      (vm-<name>-disk0, disk1, … from rqlite)
  - peers:     list[node_name]
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
        """Read drbd_resources to learn the peer list + every disk
        resource. If no rows (cattle, or idempotent re-run) we still
        tear down the local cattle LV + domain on the home node; the
        ``vms`` row gives us the host. ``already_gone`` short-circuits
        when nothing is left."""
        from bedrock_d import state as _st
        with _st.RqliteClient() as client:
            d_rows = client.query(
                "SELECT name, peers FROM drbd_resources WHERE name LIKE ?",
                params=[f"vm-{ctx['vm_name']}-disk%"])
            v_rows = client.query(
                "SELECT host FROM vms WHERE vm_name = ?",
                params=[ctx["vm_name"]])
        if not d_rows and not v_rows:
            log.info("vm_destroy: %s already removed", ctx["vm_name"])
            ctx["already_gone"] = True
            return
        if d_rows:
            ctx["resources"] = [r["name"] for r in d_rows]
            ctx["peers"] = json.loads(d_rows[0]["peers"] or "[]")
        else:
            # Cattle VM: no DRBD rows. The disks are local LVs on the
            # home node — discover every one by listing the canonical
            # bedrock-data-vm-<name>-diskN LVs so multi-disk cattle clean
            # up fully (the data LV name carries the resource name).
            home = v_rows[0]["host"]
            ctx["peers"] = [home]
            host = _peer_hosts([home])
            resources = []
            if host:
                rc, out, _ = _lvm._run_on(
                    host[0],
                    f"lvs --noheadings -o lv_name {_lvm.VG_NAME} "
                    f"2>/dev/null | grep -oP 'bedrock-data-\\Kvm-{ctx['vm_name']}-disk[0-9]+' "
                    f"|| true",
                    check=False, timeout=10)
                resources = sorted(set(out.split()))
            ctx["resources"] = resources or [f"vm-{ctx['vm_name']}-disk0"]

    @step("virsh_destroy_running")
    def step_destroy_domain(self, ctx):
        """``virsh destroy`` on every peer. On nodes where the VM is
        defined-but-not-running, destroy errors and we ignore it."""
        if ctx.get("already_gone"):
            return
        for host in _peer_hosts(ctx["peers"]):
            _lvm._run_on(
                host, f"virsh destroy {ctx['vm_name']} 2>/dev/null || true",
                check=False)

    @step("virsh_undefine")
    def step_undefine(self, ctx):
        """``virsh undefine`` on every peer. Removes the domain XML
        from libvirt. Idempotent."""
        if ctx.get("already_gone"):
            return
        for host in _peer_hosts(ctx["peers"]):
            _lvm._run_on(
                host,
                f"virsh undefine --nvram {ctx['vm_name']} "
                f"2>/dev/null || true",
                check=False)

    @step("drbd_down")
    def step_drbd_down(self, ctx):
        """``drbdadm down <resource>`` on every peer, per disk. A
        resource that isn't up returns non-zero, which is fine."""
        if ctx.get("already_gone"):
            return
        for host in _peer_hosts(ctx["peers"]):
            for r in ctx["resources"]:
                _lvm._run_on(
                    host, f"drbdadm down {r} 2>/dev/null || true",
                    check=False)

    @step("drbd_wipe_md")
    def step_wipe_md(self, ctx):
        """Wipe each external meta LV so a future re-create starts
        clean. ``drbdadm wipe-md`` zeroes the meta superblock + AL +
        bitmap. Idempotent — wiping already-wiped meta is a no-op."""
        if ctx.get("already_gone"):
            return
        for host in _peer_hosts(ctx["peers"]):
            for r in ctx["resources"]:
                _lvm._run_on(
                    host,
                    f"drbdadm wipe-md --force {r} 2>/dev/null || true",
                    check=False)

    @step("remove_drbd_res_file")
    def step_remove_res(self, ctx):
        """Drop /etc/drbd.d/<resource>.res on every peer, per disk.
        Idempotent (rm -f)."""
        if ctx.get("already_gone"):
            return
        for host in _peer_hosts(ctx["peers"]):
            for r in ctx["resources"]:
                _lvm._run_on(host, f"rm -f {_cfg.res_file_path(r)}",
                             check=False)

    @step("lvremove_pair")
    def step_lvremove(self, ctx):
        """Drop every disk's LVs on every peer. lvremove_pair removes
        the data + meta pair AND the cattle single-LV name (the cattle
        disk LV is the same as the replicated data_lv minus the meta),
        so it is safe for both shapes. Idempotent."""
        if ctx.get("already_gone"):
            return
        for host in _peer_hosts(ctx["peers"]):
            for r in ctx["resources"]:
                _lvm.lvremove_pair(host, r)

    @step("delete_rqlite_rows")
    def step_delete_rows(self, ctx):
        """Final cleanup: remove the drbd_resources + vms rows.
        Operator-visible 'gone' state."""
        from bedrock_d import state as _st
        with _st.RqliteClient() as client:
            client.execute(
                "DELETE FROM drbd_resources WHERE name LIKE ?",
                params=[f"vm-{ctx['vm_name']}-disk%"])
            client.execute(
                "DELETE FROM vms WHERE vm_name = ?",
                params=[ctx["vm_name"]])


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
    # Some peers are node_names, but the cattle path stores the host's
    # node-name in peers too; fall through to treating an unknown entry
    # as a literal host so cattle teardown still reaches the box.
    return [hosts.get(n, n) for n in peer_names if n]
