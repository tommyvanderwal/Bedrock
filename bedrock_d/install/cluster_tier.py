"""Cluster-tier DRBD lifecycle sagas.

The cluster-tier is the DRBD-replicated volume that backs
``/var/lib/bedrock/cluster`` — home of the rqlite-arbiter data dir,
SeaweedFS filer's leveldb3, and S3 IAM database. One pair of LVs
(``bedrock-data-cluster`` + ``bedrock-meta-cluster``) per node;
DRBD glues them into a synchronous mirror with the master writable.

This module owns the **transition** sagas — the saga executor runs
them once per cluster-size jump:

- ``cluster_tier_promote_master`` runs on the mgmt-master when the
  cluster first reaches N=2. Converts the local critical LV into a
  DRBD primary, preserving the filer leveldb3 contents byte-for-byte
  via external metadata. Idempotent: safe to re-run after a crash;
  steps that already completed no-op.

- ``cluster_tier_join_peer`` runs on a joiner once the master's
  promote saga has finished (cluster.json shows
  ``tiers.critical.mode == "drbd"``). The peer creates its own LV
  pair, joins as DRBD Secondary, and waits for the initial sync to
  carry the master's data over.

For larger transitions (N=2 → N=3, peer removal, etc.) see the
``tier_storage.promote_critical_to_3way`` / ``drbd_remove_peer``
helpers — those run in the calm orchestrator's reconcile loop, not
as join-time sagas.

Both sagas are pure thin wrappers over ``tier_storage`` helpers; the
heavy lifting (LV creation, drbdadm calls, snapshot+restore, fstab,
symlink) lives in that module so this file stays declarative.

# Kopia / backup snapshot compatibility

The layout chosen here keeps LVM thin snapshots usable by Kopia (or
any other backup tool that reads from a snapshot). Both the
``bedrock-data-*`` LV under DRBD and the per-VM data LVs are thin
LVs in the same pool — so ``lvcreate --snapshot --thinpool``
captures a point-in-time view without copying blocks. The recipe a
backup driver follows is:

  fsfreeze --freeze /var/lib/bedrock/cluster
  lvcreate --snapshot -n cluster-snap-<ts> bedrock-vg/bedrock-data-cluster
  fsfreeze --unfreeze /var/lib/bedrock/cluster
  mount -o ro,nouuid /dev/bedrock-vg/cluster-snap-<ts> /mnt/snap-<ts>
  kopia snapshot create /mnt/snap-<ts>
  umount /mnt/snap-<ts>
  lvremove bedrock-vg/cluster-snap-<ts>

DRBD is **unaware** of the snapshot — it's mirroring at the block
layer below LVM. As long as we don't move the data LV onto a thick
LV, a raw partition, or a non-LVM device under DRBD, the snapshot
path stays open. Keep this constraint in mind for any future
refactor of ``tier_storage.promote_local_to_drbd_master``.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from bedrock_d.orchestrator.sagas import saga, step

log = logging.getLogger(__name__)

CLUSTER_JSON = Path("/etc/bedrock/cluster.json")
STATE_JSON = Path("/etc/bedrock/state.json")


def _load_cluster() -> dict:
    try:
        from lib import cluster_state
        return cluster_state.load_cluster()
    except Exception:
        return {}


def _self_node_name() -> str:
    try:
        return json.loads(STATE_JSON.read_text()).get("node_name") or ""
    except Exception:
        import socket
        return socket.gethostname()


def _self_loopback() -> str:
    try:
        return json.loads(STATE_JSON.read_text()).get("loopback_ip") or ""
    except Exception:
        return ""


def _critical_tier_mode(cluster: dict) -> str:
    return ((cluster.get("tiers") or {}).get("critical") or {}).get("mode", "local")


# ─────────────────────────────────────────────────────────────────────
# Master-side: promote the local critical LV to DRBD primary
# ─────────────────────────────────────────────────────────────────────


@saga("cluster_tier_promote_master")
class ClusterTierPromoteMaster:
    """Run on the mgmt-master once cluster size N≥2 to convert the
    local critical-tier LV into a DRBD primary that will be mirrored
    to peers as they join.

    The saga is launched by the orchestrator's
    ``cluster_tier_watcher`` task — see ``mgmt/orchestrator.py``.

    Params (set by the orchestrator at submit time):
      - ``peer_node`` (str): node name of the first peer to mirror to
      - ``peer_loopback`` (str): peer's loopback IP for the DRBD link

    Idempotent at every step: re-running after a crash picks up where
    it left off. Step boundaries are placed so a power loss between
    them leaves the system in a recoverable state (the docstring of
    each step names the recoverable invariant).
    """

    @step("check_preconditions")
    def step_check_preconditions(self, ctx):
        """Confirm we're still the master and critical is still local.

        If either condition has flipped (because of a failover during
        the saga's lifetime), bail out gracefully — the new master's
        watcher will fire a fresh promote saga of its own."""
        cluster = _load_cluster()
        master = cluster.get("mgmt_master") or ""
        self_name = _self_node_name()
        if master != self_name:
            raise RuntimeError(
                f"no longer mgmt_master (cluster.json names {master!r}, "
                f"self={self_name!r}); aborting promote")
        mode = _critical_tier_mode(cluster)
        if mode == "drbd":
            log.info("cluster_tier: critical already in DRBD mode — "
                     "this saga is a no-op")
            ctx["_already_drbd"] = True
            return
        peer = ctx.get("peer_node") or ""
        peer_lo = ctx.get("peer_loopback") or ""
        if not peer or not peer_lo:
            raise RuntimeError(
                f"missing peer params: peer_node={peer!r} "
                f"peer_loopback={peer_lo!r}")
        nodes = cluster.get("nodes") or {}
        if peer not in nodes:
            raise RuntimeError(
                f"peer {peer!r} not in cluster.json nodes "
                f"({sorted(nodes)}); ABORT — bootstrap window race")

    @step("promote_local_to_drbd")
    def step_promote_local_to_drbd(self, ctx):
        """The big move: stop singletons, snapshot the leveldb3 dir,
        umount local LV, create meta LV, write .res, create-md,
        drbdadm up + primary, mount DRBD device, restore snapshot,
        update fstab, swap symlink, restart singletons.

        All wrapped in ``tier_storage.transition_to_n2_master`` —
        this step is a thin wrapper so step-resume granularity matches
        what's idempotent inside that helper. If a future
        crash-recovery analysis says we need finer granularity, split
        this step into its constituents."""
        if ctx.get("_already_drbd"):
            return
        from lib import tier_storage as _ts
        self_lo = _self_loopback()
        if not self_lo:
            raise RuntimeError("self loopback_ip missing from state.json")
        result = _ts.transition_to_n2_master(
            self_loopback_ip=self_lo,
            peer={"name": ctx["peer_node"],
                  "loopback_ip": ctx["peer_loopback"]},
        )
        ctx["_promote_result"] = result

    @step("record_tier_state_rqlite")
    def step_record_tier_state_rqlite(self, ctx):
        """``transition_to_n2_master`` updates cluster.json locally;
        mirror to rqlite so every node's view_builder fold sees the
        new mode. set_tier_state with default write_rqlite=True does
        this when the caller is the mgmt_master — which we are."""
        if ctx.get("_already_drbd"):
            return
        from lib import tier_storage as _ts
        cluster = _load_cluster()
        cur = (cluster.get("tiers") or {}).get("critical") or {}
        # set_tier_state is idempotent (INSERT OR REPLACE in rqlite)
        # and write_rqlite=True (default) fires the rqlite mirror.
        _ts.set_tier_state(
            "critical",
            mode=cur.get("mode", "drbd"),
            master=cur.get("master"),
            peers=cur.get("peers"),
            backend_path=cur.get("backend_path",
                                 "/var/lib/bedrock/cluster"),
        )


# ─────────────────────────────────────────────────────────────────────
# Peer-side: join the DRBD secondary
# ─────────────────────────────────────────────────────────────────────


@saga("cluster_tier_join_peer")
class ClusterTierJoinPeer:
    """Run on a joiner after the master's promote saga has finished.

    The joiner's node_join saga submits this saga as a follow-up
    step, blocking until it completes. The saga polls cluster.json
    for ``tiers.critical.mode == "drbd"`` before proceeding (the
    master must have promoted first; otherwise the secondary has no
    primary to sync from).

    Idempotent: re-running after a crash is safe because
    ``join_drbd_peer`` checks for existing LVs and skips creation.

    Params:
      - ``wait_timeout_s`` (int, default 120): how long to wait for
        the master to finish promoting before giving up
    """

    @step("wait_master_drbd")
    def step_wait_master_drbd(self, ctx):
        """Poll cluster.json every 2 s for the master's promote to
        reach the ``mode=drbd`` state. Times out after
        ``wait_timeout_s`` seconds — if the master never gets there
        the saga fails loudly so the operator notices."""
        timeout = int(ctx.get("wait_timeout_s") or 120)
        deadline = time.monotonic() + timeout
        last_mode = "?"
        while time.monotonic() < deadline:
            cluster = _load_cluster()
            mode = _critical_tier_mode(cluster)
            last_mode = mode
            if mode == "drbd":
                # Carry the peer list forward to the next step so we
                # don't have to re-read cluster.json there.
                ctx["_peers"] = (
                    (cluster.get("tiers") or {}).get("critical") or {}
                ).get("peers") or []
                ctx["_master"] = cluster.get("mgmt_master") or ""
                return
            time.sleep(2)
        raise RuntimeError(
            f"timeout: master never promoted critical tier to DRBD "
            f"after {timeout}s (last mode={last_mode!r})")

    @step("join_as_secondary")
    def step_join_as_secondary(self, ctx):
        """Allocate the peer LV pair, write the .res, drbdadm up as
        secondary. join_drbd_peer is idempotent — it checks for
        existing LVs and skips if present."""
        from lib import tier_storage as _ts
        cluster = _load_cluster()
        nodes = cluster.get("nodes") or {}
        # Rebuild the full peer list (master + every node currently
        # in the critical-tier peer set, including self).
        peers = []
        for name in (ctx.get("_peers") or []):
            n = nodes.get(name) or {}
            peers.append({"name": name,
                          "loopback_ip": n.get("loopback_ip", "")})
        self_name = _self_node_name()
        self_lo = _self_loopback()
        if not any(p["name"] == self_name for p in peers):
            peers.append({"name": self_name, "loopback_ip": self_lo})
        master = ctx.get("_master") or ""
        master_lo = (nodes.get(master) or {}).get("loopback_ip", "")
        _ts.transition_to_n2_peer(
            self_loopback_ip=self_lo,
            master={"name": master, "loopback_ip": master_lo},
            peers=peers,
        )
