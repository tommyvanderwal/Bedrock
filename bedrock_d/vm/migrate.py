"""VmMigrate saga — `bedrock vm migrate` (live).

The Bedrock migration shape (per BEDROCK.md ::Live migration):

  1. Enable dual-primary on the DRBD resource (allow-two-primaries).
  2. Promote the target node's DRBD replica to Primary.
  3. virsh migrate --live --persistent  (RAM copy only; both sides
     read+write the same DRBD bytes locally).
  4. Demote the source node's DRBD replica to Secondary.
  5. Disable dual-primary (back to one-primary at a time).
  6. Update vms.host = target in rqlite.

Each step is idempotent — re-running a half-completed migrate
converges to "VM on target, source secondary, single primary".

ctx inputs:
  - vm_name:    str
  - target:     node_name (must be in resource peers + reachable)

ctx fills:
  - resource:   str
  - source:     node_name (current vms.host)
  - source_host: LAN IP
  - target_host: LAN IP
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "installer"))

from bedrock_d.orchestrator.sagas import saga, step  # noqa: E402
from . import lvm as _lvm

log = logging.getLogger(__name__)


@saga("vm_migrate")
class VmMigrate:
    """Live-migrate a running VM from source to target node."""

    @step("validate_request")
    def step_validate(self, ctx):
        """Target must be a peer of this resource AND reachable.
        Source comes from vms.host (where the VM currently runs)."""
        from bedrock_d import state as _st
        resource = f"vm-{ctx['vm_name']}-disk0"
        with _st.RqliteClient() as client:
            d_rows = client.query(
                "SELECT peers FROM drbd_resources WHERE name = ?",
                params=[resource])
            v_rows = client.query(
                "SELECT host FROM vms WHERE vm_name = ?",
                params=[ctx["vm_name"]])
        if not d_rows or not v_rows:
            raise RuntimeError(f"no record for vm {ctx['vm_name']!r}")
        peers = json.loads(d_rows[0]["peers"] or "[]")
        if ctx["target"] not in peers:
            raise ValueError(
                f"target {ctx['target']!r} is not a peer of "
                f"resource {resource}; peers={peers}")
        if v_rows[0]["host"] == ctx["target"]:
            raise ValueError(
                f"VM {ctx['vm_name']!r} already on {ctx['target']!r}")
        ctx["resource"] = resource
        ctx["source"] = v_rows[0]["host"]
        # Resolve LAN IPs
        cluster = json.loads(Path("/etc/bedrock/cluster.json").read_text())
        nodes = cluster.get("nodes", {})
        ctx["source_host"] = (nodes.get(ctx["source"]) or {}).get("host", "")
        ctx["target_host"] = (nodes.get(ctx["target"]) or {}).get("host", "")
        if not ctx["source_host"] or not ctx["target_host"]:
            raise RuntimeError("source/target missing host in cluster.json")

    @step("enable_dual_primary")
    def step_enable_dual(self, ctx):
        """``drbdadm net-options --allow-two-primaries=yes`` on
        both peers. Without this, ``drbdadm primary`` on target
        fails because resource only permits one primary at a time."""
        for host in (ctx["source_host"], ctx["target_host"]):
            _lvm._run_on(
                host,
                f"drbdadm net-options --allow-two-primaries=yes "
                f"{ctx['resource']}",
                check=False,
            )

    @step("drbd_primary_on_target")
    def step_promote_target(self, ctx):
        """Promote target's DRBD replica to Primary so virsh can
        live-migrate. After this BOTH nodes are Primary (dual-
        primary window). Migration happens; we demote source
        after."""
        _lvm._run_on(
            ctx["target_host"],
            f"drbdadm primary {ctx['resource']}",
            check=False,
        )

    @step("virsh_migrate_live")
    def step_migrate(self, ctx):
        """The actual live migration: virsh on the source asks
        target's libvirt to take over. RAM state copies; disk
        state is already replicated via DRBD (zero-copy). The
        --persistent flag carries the VM definition to the target
        (so virsh undefine on source after isn't needed for
        future starts)."""
        rc, out, err = _lvm._run_on(
            ctx["source_host"],
            f"virsh migrate --live --persistent --undefinesource "
            f"--verbose {ctx['vm_name']} "
            f"qemu+ssh://{ctx['target_host']}/system",
            check=False, timeout=600,
        )
        if rc != 0:
            raise RuntimeError(
                f"virsh migrate failed (rc={rc}): "
                f"stderr={err[:400]!r}")

    @step("drbd_secondary_on_source")
    def step_demote_source(self, ctx):
        """Demote source back to Secondary. VM is now running on
        target; source's DRBD just passes through replicated writes."""
        _lvm._run_on(
            ctx["source_host"],
            f"drbdadm secondary {ctx['resource']}",
            check=False,
        )

    @step("disable_dual_primary")
    def step_disable_dual(self, ctx):
        """Tighten back to single-primary. Default DRBD policy
        protects against split-brain by refusing two primaries;
        we only opened that window for the migrate. Now closing it."""
        for host in (ctx["source_host"], ctx["target_host"]):
            _lvm._run_on(
                host,
                f"drbdadm net-options --allow-two-primaries=no "
                f"{ctx['resource']}",
                check=False,
            )

    @step("update_vms_host")
    def step_update_host(self, ctx):
        """Reflect the move in rqlite. Last step; once this commits
        the dashboard + downstream consumers see the VM on its
        new home."""
        from bedrock_d import state as _st
        import time as _t
        with _st.RqliteClient() as client:
            client.execute(
                "UPDATE vms SET host = ?, updated_at = ? "
                "WHERE vm_name = ?",
                params=[ctx["target"], int(_t.time()),
                        ctx["vm_name"]],
            )
