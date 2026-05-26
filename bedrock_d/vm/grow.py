"""VmGrow saga — `bedrock vm grow` (online disk extend).

Per docs/storage-architecture.md, growing a VM disk online is
``lvextend meta`` (if needed) + ``lvextend data`` + ``drbdadm
resize`` on every peer. DRBD recalculates bitmap requirements
in-place; the data device stays attached the whole time.

ctx inputs:
  - vm_name:  str
  - new_gb:   int  (new total disk size, MUST be > current)

ctx fills:
  - resource: str
  - peers:    list[node_name]
  - old_gb:   int (current data_size_bytes / GiB)
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


@saga("vm_grow")
class VmGrow:
    """Online disk extend. Guest still needs to grow its own
    partition/FS — that's a separate operator action (typically
    ``growpart`` + ``resize2fs`` inside the guest)."""

    @step("load_current_size")
    def step_load(self, ctx):
        """Read the drbd_resources row to learn current size + peers."""
        from bedrock_d import state as _st
        resource = f"vm-{ctx['vm_name']}-disk0"
        with _st.RqliteClient() as client:
            rows = client.query(
                "SELECT data_size_bytes, peers FROM drbd_resources "
                "WHERE name = ?", params=[resource],
            )
        if not rows:
            raise RuntimeError(f"no drbd_resource for {ctx['vm_name']}")
        ctx["resource"] = resource
        ctx["old_gb"] = int(rows[0]["data_size_bytes"]) // (1024 ** 3)
        ctx["peers"] = json.loads(rows[0]["peers"] or "[]")

    @step("validate_new_size")
    def step_validate(self, ctx):
        """``new_gb`` must be strictly greater than current. Equal
        is a no-op — but we make it an explicit error so the
        operator doesn't think a grow happened when it didn't."""
        new_gb = int(ctx["new_gb"])
        if new_gb <= ctx["old_gb"]:
            raise ValueError(
                f"new_gb={new_gb} <= current {ctx['old_gb']} GiB; "
                f"grow only extends. Use a separate procedure to shrink."
            )

    @step("lvextend_meta_on_peers")
    def step_extend_meta(self, ctx):
        """Extend the meta LV first. If the new bitmap fits in
        current meta space (often the case for small grows), LVM
        no-ops. Always running this is cheap + makes the grow
        order correct: meta-then-data."""
        new_gb = int(ctx["new_gb"])
        for host in _peer_hosts(ctx["peers"]):
            _lvm.lvextend_meta(host, ctx["resource"], new_gb)

    @step("lvextend_data_on_peers")
    def step_extend_data(self, ctx):
        """Extend the data LV on every peer. Order matters per the
        previous step: meta first (in case bitmap needed more
        room), then data (which is what drbdadm resize sees)."""
        new_gb = int(ctx["new_gb"])
        for host in _peer_hosts(ctx["peers"]):
            _lvm.lvextend_data(host, ctx["resource"], new_gb)

    @step("drbd_resize")
    def step_drbd_resize(self, ctx):
        """``drbdadm resize <resource>`` on every peer. DRBD reads
        the new data device size + recalculates bitmap. Online —
        no detach, no remount. Guest OS is unaware until the
        operator runs growpart/resize2fs."""
        for host in _peer_hosts(ctx["peers"]):
            _lvm._run_on(
                host, f"drbdadm resize {ctx['resource']}",
                check=False,
            )

    @step("update_drbd_resources_row")
    def step_update_row(self, ctx):
        """Reflect the new size in rqlite. Last step — once this
        commits, future grow operations see the new baseline."""
        from bedrock_d import state as _st
        import time as _t
        new_bytes = int(ctx["new_gb"]) * 1024 * 1024 * 1024
        new_meta_mb = _lvm.meta_size_mb_for(int(ctx["new_gb"]))
        new_meta_bytes = new_meta_mb * 1024 * 1024
        with _st.RqliteClient() as client:
            client.execute(
                "UPDATE drbd_resources "
                "SET data_size_bytes = ?, meta_size_bytes = ?, "
                "    updated_at = ? "
                "WHERE name = ?",
                params=[new_bytes, new_meta_bytes, int(_t.time()),
                        ctx["resource"]],
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
