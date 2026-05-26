"""NodeLeave saga — `bedrock node leave <target>` flow.

Runs on the MASTER to cleanly remove ``<target>`` from the cluster.
Each step is idempotent: re-running on a node already removed is a
sequence of no-ops.

# Why it lives here

The leave operation is multi-step + crash-safe-critical: if the
master writes ``node_unregister`` to rqlite but then dies before
dropping the voter slot, the cluster runs with stale Raft
membership and consecutive leaves can brick quorum. The saga
makes the sequence visible and resumable.

# What this saga does NOT do

Cluster-DRBD membership re-shuffling (when the leaver was carrying
the tier-critical resource and we now need to promote another node
into the 3-peer set) is owned by the **calm orchestrator** — it's a
deliberate resource-aware decision, not a critical-path operation.
node_leave logs that the cluster-DRBD set may now be below design
redundancy; the orchestrator picks it up on its next reconcile.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "installer"))

from bedrock_d.orchestrator.sagas import (  # noqa: E402
    FileSagaBackend, SagaExecutor, SagaState, saga, step,
)

log = logging.getLogger(__name__)

# Use the same progress file as init/join — different `kind`, no
# collision. Letting all bootstrap-time sagas share the file means
# operators have ONE place to look for "what was the last big op".
INIT_PROGRESS_PATH = Path("/var/lib/bedrock/init-progress.json")


@saga("node_leave")
class NodeLeave:
    """`bedrock node leave <target>` — master-orchestrated removal.

    ctx inputs:
      - target_name: str (the node being removed)
      - reason: str (audit-log reason; default "leave")
      - self_name: str (the master running this; for the self-check)

    ctx outputs:
      - target_host: str (LAN IP of target, looked up from snapshot)
      - target_loopback: str (used to derive voter slot id)
      - target_voter_id: str (last octet of target loopback)
    """

    @step("validate_target")
    def step_validate(self, ctx):
        """Look up the target in the current snapshot. Fail loud if:
        - target doesn't exist (typo) → operator gets a useful error
        - target is self → master can't leave itself (different cmd)"""
        if ctx.get("target_name") == ctx.get("self_name"):
            raise RuntimeError(
                f"cannot leave-from-self ({ctx['self_name']!r}); "
                f"run `bedrock node leave` from a different node"
            )
        from lib import view_builder as _vb
        snapshot = _vb.rebuild(this_node=ctx["self_name"])
        target_info = (snapshot.get("nodes") or {}).get(ctx["target_name"])
        if target_info is None:
            # Idempotent: if the target was already removed earlier
            # (saga re-run, partial-success retry), don't fail.
            ctx["already_gone"] = True
            log.info("node_leave: %r already gone from snapshot",
                     ctx["target_name"])
            return
        ctx["target_host"] = target_info.get("host", "")
        ctx["target_loopback"] = target_info.get("loopback_ip", "")
        last = ctx["target_loopback"].rsplit(".", 1)
        ctx["target_voter_id"] = (last[1] if len(last) == 2 and
                                  last[1].isdigit() else "")

    @step("rqlite_node_unregister")
    def step_unregister(self, ctx):
        """Write node_unregister row to rqlite. Master single-writer;
        Raft replicates. Idempotent — duplicate unregister rows are
        a harmless append."""
        if ctx.get("already_gone"):
            return
        from bedrock_d import state as _st
        rev = _st.node_unregister(
            node_name=ctx["target_name"],
            reason=ctx.get("reason", "leave"),
        )
        log.info("node_leave: rev=%s node_unregister(%s)",
                 rev, ctx["target_name"])

    @step("rqlite_voter_remove")
    def step_voter_remove(self, ctx):
        """DELETE /remove on rqlite to drop the leaver's voter slot.
        Critical: without this the leaver's offline rqlited costs a
        vote against the live cluster on next election. Consecutive
        leaves without this would brick quorum at N/2 voters."""
        if ctx.get("already_gone"):
            return
        voter_id = ctx.get("target_voter_id") or ""
        if not voter_id:
            log.warning("node_leave: no voter_id derivable from "
                        "loopback %r; skipping rqlite /remove",
                        ctx.get("target_loopback"))
            return
        # /remove is idempotent on the server side — removing an
        # already-removed voter is a 200 OK no-op.
        rc = subprocess.run(
            ["curl", "-fsSL", "-X", "DELETE",
             "--cert", "/etc/bedrock/node.crt",
             "--key",  "/etc/bedrock/node.key.pem",
             "--cacert", "/etc/bedrock/ca.crt",
             "https://127.0.0.1:4001/remove",
             "-d", json.dumps({"id": voter_id})],
            capture_output=True, timeout=10,
        )
        if rc.returncode != 0:
            log.warning("node_leave: /remove voter_id=%s rc=%d stderr=%s",
                        voter_id, rc.returncode,
                        rc.stderr.decode(errors="replace")[:200])
        else:
            log.info("node_leave: rqlite raft voter id=%s removed",
                     voter_id)

    @step("propagate_daemon_config")
    def step_propagate(self, ctx):
        """Regen the master's own daemon.toml so its peer_sender_ids
        drops the leaver immediately. Net peers pick up the change
        from rqlite revision tick. Idempotent."""
        # _propagate_daemon_config is a function in the bedrock CLI
        # (legacy). For now we just bump the revision; the
        # rqlite_subscriber on each node regenerates its own daemon
        # config on revision change.
        try:
            from bedrock_d import state as _st
            with _st.RqliteClient() as client:
                _st.bump_revision(client)
        except Exception as e:
            log.warning("node_leave: bump_revision failed: %s", e)

    @step("stop_remote_services")
    def step_stop_remote(self, ctx):
        """Best-effort SSH to the leaver and stop bedrock services.
        Failure is non-fatal — the cluster has already removed the
        node from its view; the leaver will simply stop heartbeating
        and the witness slot ages out within ~15 s."""
        host = ctx.get("target_host", "")
        if not host or ctx.get("already_gone"):
            return
        cmd = [
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=5",
            f"root@{host}",
            "systemctl stop bedrock-d bedrock-rqlited "
            "bedrock-rqlited-arbiter 2>/dev/null; "
            "rm -f /run/bedrock-no-quorum; true",
        ]
        rc = subprocess.run(cmd, capture_output=True, timeout=20)
        if rc.returncode != 0:
            log.warning("node_leave: SSH stop on %s rc=%d; witness "
                        "slot will age out naturally", host, rc.returncode)
        else:
            log.info("node_leave: %s bedrock services stopped via SSH",
                     ctx["target_name"])

    @step("verify_membership_drop")
    def step_verify(self, ctx):
        """Read back the snapshot; the target should be gone. Bounds
        the eventual-consistency window of "did the unregister
        actually take?" so the operator sees the result, not a stale
        view."""
        if ctx.get("already_gone"):
            return
        from lib import view_builder as _vb
        for _ in range(10):  # 10 × 0.5 s
            snapshot = _vb.rebuild(this_node=ctx["self_name"])
            if ctx["target_name"] not in (snapshot.get("nodes") or {}):
                log.info("node_leave: %r removed from snapshot",
                         ctx["target_name"])
                return
            time.sleep(0.5)
        # Failed to disappear in 5s — flag but don't fail-fast; the
        # subscriber may be running slow. Operator sees the warning;
        # cluster eventually consistent.
        log.warning("node_leave: %r still in snapshot after 5s — "
                    "subscriber may be backed up", ctx["target_name"])


def run_node_leave(*, target_name: str, reason: str = "leave",
                   self_name: Optional[str] = None) -> None:
    """Entry point for `bedrock node leave` via the saga path.

    Caller is the master node; `self_name` defaults to gethostname()
    but can be overridden by tests.
    """
    if self_name is None:
        import socket as _sock
        self_name = _sock.gethostname()

    INIT_PROGRESS_PATH.parent.mkdir(parents=True, exist_ok=True)
    backend = FileSagaBackend(path=INIT_PROGRESS_PATH)
    requested_by = (os.environ.get("SUDO_USER")
                    or os.environ.get("USER") or "operator")

    # Find existing in-flight / failed node_leave for this target.
    # Multiple leaves are routine (different targets); we match on
    # target_name in the params to avoid hijacking someone else's op.
    raw = (json.loads(backend.path.read_text())
           if backend.path.exists() else {})
    existing_id = None
    existing_state = None
    for op in (raw.get("ops") or {}).values():
        if (op.get("kind") == "node_leave"
                and op.get("state") != "completed"
                and (op.get("params") or {}).get("target_name") == target_name):
            existing_id = op["id"]
            existing_state = op["state"]
            break

    params = {
        "target_name": target_name,
        "reason": reason,
        "self_name": self_name,
    }
    executor = SagaExecutor(backend=backend, this_node=self_name)

    if existing_id is not None:
        log.info("node_leave: picking up existing op id=%d (state=%s)",
                 existing_id, existing_state)
        result = (executor.retry(existing_id)
                  if existing_state == "failed"
                  else executor.execute_one(existing_id))
    else:
        op_id = executor.submit(
            kind="node_leave", target_node=self_name,
            params=params, requested_by=requested_by,
        )
        log.info("node_leave: submitted new op id=%d (target=%s)",
                 op_id, target_name)
        result = executor.execute_one(op_id)

    if result.state != SagaState.COMPLETED:
        raise RuntimeError(
            f"node_leave failed at step {result.last_step!r}: "
            f"{result.error}"
        )
