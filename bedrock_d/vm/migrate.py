"""VmMigrate saga — `bedrock vm migrate` (live). The single migrate path.

The Bedrock migration shape (per BEDROCK.md ::Live migration):

  1. Enable dual-primary on every DRBD resource backing the VM
     (allow-two-primaries).
  2. Promote the target node's DRBD replica(s) to Primary.
  3. virsh migrate --live  (RAM copy only; both sides read+write the
     same DRBD bytes locally). The domain stays DEFINED on the source
     — no --undefinesource — so the source remains a failover target
     for the (now-migrated) VM.
  4. Record each resource's post-promote DRBD UUID on the new primary
     so a later host-death failover passes the INV-5 exact-equality
     check (without this, every migrate silently breaks HA — VM-02).
  5. Demote the source node's DRBD replica(s) to Secondary.
  6. Disable dual-primary (back to one-primary at a time).
  7. Update vms.host = target in rqlite.

Each step is idempotent — re-running a half-completed migrate
converges to "VM on target, source secondary, single primary".

ctx inputs:
  - vm_name:    str
  - target:     node_name (must be a peer of every resource + reachable)

ctx fills:
  - resources:  list[str]   (every DRBD resource backing the VM)
  - source:     node_name (current vms.host)
  - source_host: LAN IP
  - target_host: LAN IP
  - target_lo:   target loopback /32 (migrate URI)
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
        """Target must be a peer of this VM's resources AND reachable.
        Source comes from vms.host (where the VM currently runs).
        Enumerates EVERY DRBD resource backing the VM (multi-disk)."""
        from bedrock_d import state as _st
        with _st.RqliteClient() as client:
            d_rows = client.query(
                "SELECT name, peers FROM drbd_resources WHERE name LIKE ?",
                params=[f"vm-{ctx['vm_name']}-disk%"])
            v_rows = client.query(
                "SELECT host FROM vms WHERE vm_name = ?",
                params=[ctx["vm_name"]])
        if not d_rows or not v_rows:
            raise RuntimeError(
                f"no replicated record for vm {ctx['vm_name']!r} "
                f"(cattle VMs cannot migrate)")
        peers = json.loads(d_rows[0]["peers"] or "[]")
        if ctx["target"] not in peers:
            raise ValueError(
                f"target {ctx['target']!r} is not a peer of VM "
                f"{ctx['vm_name']!r}; peers={peers}")
        if v_rows[0]["host"] == ctx["target"]:
            raise ValueError(
                f"VM {ctx['vm_name']!r} already on {ctx['target']!r}")
        ctx["resources"] = [r["name"] for r in d_rows]
        ctx["source"] = v_rows[0]["host"]
        # Resolve LAN IPs + the target's loopback /32 (the migrate URI;
        # mesh-routed over whatever physical path netd picked best).
        from lib import rqlite_client
        with rqlite_client.RqliteClient() as _rc:
            rows = _rc.query(
                "SELECT node_name, host, loopback_ip FROM nodes "
                "WHERE node_name IN (?, ?)",
                params=[ctx["source"], ctx["target"]],
                level="none",
            )
        info = {r["node_name"]: r for r in rows}
        ctx["source_host"] = (info.get(ctx["source"]) or {}).get("host", "")
        tgt = info.get(ctx["target"]) or {}
        ctx["target_host"] = tgt.get("host", "")
        ctx["target_lo"] = tgt.get("loopback_ip") or ctx["target_host"]
        if not ctx["source_host"] or not ctx["target_host"]:
            raise RuntimeError(
                f"source/target missing host in rqlite nodes table: "
                f"{ctx['source']!r}={ctx['source_host']!r}, "
                f"{ctx['target']!r}={ctx['target_host']!r}")

    @step("enable_dual_primary")
    def step_enable_dual(self, ctx):
        """``drbdadm net-options --allow-two-primaries=yes`` on both
        peers for every resource. Without this, ``drbdadm primary`` on
        the target fails (resource only permits one primary)."""
        for host in (ctx["source_host"], ctx["target_host"]):
            for r in ctx["resources"]:
                _lvm._run_on(
                    host,
                    f"drbdadm net-options --allow-two-primaries=yes {r}",
                    check=False)

    @step("drbd_primary_on_target")
    def step_promote_target(self, ctx):
        """Promote every resource's replica on the target to Primary so
        virsh can live-migrate. After this BOTH nodes are Primary (the
        bounded dual-primary window)."""
        for r in ctx["resources"]:
            _lvm._run_on(
                ctx["target_host"], f"drbdadm primary {r}", check=False)

    @step("virsh_migrate_live")
    def step_migrate(self, ctx):
        """The actual live migration: virsh on the source asks target's
        libvirt to take over. RAM state copies; disk state is already
        replicated via DRBD (zero-copy). The domain stays defined on the
        source (NO --undefinesource) so the source remains a failover
        target. --migrateuri pins the transfer to the target's loopback
        /32 (mesh-routed)."""
        rc, _out, err = _lvm._run_on(
            ctx["source_host"],
            f"virsh migrate --live --verbose --unsafe "
            f"--migrateuri tcp://{ctx['target_lo']} "
            f"{ctx['vm_name']} qemu+ssh://root@{ctx['target_lo']}/system",
            check=False, timeout=600,
        )
        if rc != 0:
            raise RuntimeError(
                f"virsh migrate failed (rc={rc}): stderr={err[:400]!r}")

    @step("record_uuids_after_migrate")
    def step_record_uuids(self, ctx):
        """Record each resource's post-promote DRBD current-UUID on the
        new primary (the target). The promote in step_promote_target
        bumped the UUID; without recording it, a later host-death
        failover is REFUSED by the INV-5 exact-equality gate (VM-02).

        We read the UUID off the target's debugfs over SSH (a clean,
        quote-safe command) and write it to rqlite from here (the saga
        runs on the master, which has rqlite access). Mirrors the read
        in vm.failover.read_local_drbd_uuid."""
        from lib import bedrock_state as _bs
        for r in ctx["resources"]:
            debugfs = (f"/sys/kernel/debug/drbd/resources/{r}"
                       f"/volumes/0/data_gen_id")
            rc, out, err = _lvm._run_on(
                ctx["target_host"],
                f"head -1 {debugfs} 2>/dev/null", check=False, timeout=10)
            uuid = out.strip().lower()
            if rc != 0 or not uuid.startswith("0x"):
                log.warning("vm_migrate: could not read post-promote UUID "
                            "for %s on %s (rc=%d): %s",
                            r, ctx["target"], rc, (err or out)[:200])
                continue
            uuid = uuid[2:]
            _bs.drbd_resource_uuid_set(r, uuid)
            log.info("vm_migrate: recorded UUID for %s = %s", r, uuid[:12])

    @step("drbd_secondary_on_source")
    def step_demote_source(self, ctx):
        """Demote every resource on the source back to Secondary. VM is
        now running on the target; the source's DRBD just passes through
        replicated writes."""
        for r in ctx["resources"]:
            _lvm._run_on(
                ctx["source_host"], f"drbdadm secondary {r}", check=False)

    @step("disable_dual_primary")
    def step_disable_dual(self, ctx):
        """Tighten back to single-primary on every resource. Default
        DRBD policy protects against split-brain by refusing two
        primaries; we only opened that window for the migrate."""
        for host in (ctx["source_host"], ctx["target_host"]):
            for r in ctx["resources"]:
                _lvm._run_on(
                    host,
                    f"drbdadm net-options --allow-two-primaries=no {r}",
                    check=False)

    @step("update_vms_host")
    def step_update_host(self, ctx):
        """Reflect the move in rqlite. Last step; once this commits the
        dashboard + downstream consumers see the VM on its new home."""
        from bedrock_d import state as _st
        import time as _t
        with _st.RqliteClient() as client:
            client.execute(
                "UPDATE vms SET host = ?, updated_at = ? "
                "WHERE vm_name = ?",
                params=[ctx["target"], int(_t.time()), ctx["vm_name"]],
            )
