"""Cluster control plane: election, witness, protocol-4 heartbeats.

Independent of lib/netd.py (mesh routing). Communicates with the rest of
bedrock-d only via BedrockState (cluster_lock, drbd_down_peers, outcomes).

See docs/FUTURE_CLUSTER_NETD_CROSSINGS.md for optional cross-layer improvements.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import msgpack

try:
    from . import election as _election_mod
except ImportError:
    sys.path.insert(0, "/usr/local/lib/bedrock")
    from lib import election as _election_mod  # type: ignore

# ── Constants ────────────────────────────────────────────────────────

CLUSTER_KEY_FILE = Path("/etc/bedrock/cluster.key")
STATE_JSON       = Path("/etc/bedrock/state.json")

HB_PORT = 7734
ELECTION_INTERVAL_S = 1.0
TICK_INTERVAL = 0.25
MASTER_LOSS_MISSES = 10
SELF_DEMOTE_MISSES = 9
DRBD_DOWN_TTL_S = 15.0

HB_VERSION = 1
WITNESS_FILE_IO_INTERVAL_S = 3.0
WITNESS_HEALTH_INTERVAL_S = 60.0
_LAST_UNCONFIGURED_WARN = 0.0


@dataclass
class ClusterDaemon:
    cluster_key: bytes
    cluster_uuid: str
    my_node: str
    my_loopback: str
    hb_send_sock: Optional[socket.socket] = None
    hb_recv_sock: Optional[socket.socket] = None
    peer_hb: dict = field(default_factory=dict)
    peer_acks: dict = field(default_factory=dict)
    missed_master_beats: int = 0
    hb_believed_master: str = ""
    hb_transitioning: bool = False
    hb_arbiter_uuid: str = ""
    hb_ack_target: str = ""
    applied_epoch_cache: int = -1
    noquorum_master_ticks: int = 0
    demoted_in_cycle: bool = False
    _persisted_believed_master: Any = "<unset>"
    shared_state: Optional[Any] = None
    stopped: bool = False


def encode_heartbeat(*, cluster_uuid: str, node: str, ts: float,
                     believed_master: str, transitioning: bool,
                     arbiter_uuid: str, ack_target: str,
                     key: bytes) -> bytes:
    """Sign-then-pack a node-to-node election heartbeat (protocol 4).

    Fields (BAD-1):
      * believed_master — who the sender currently believes is mgmt
        master ("" if none / lost).
      * transitioning — True iff the sender has lost the master and is
        advertising ITSELF as master-to-be.
      * arbiter_uuid — the sender's `cluster`-singleton DRBD current-UUID
        (eligibility proof a voter classifies against its own history).
      * ack_target — the candidate the sender is acking as master-to-be
        ("" = not acking anyone). A peer grants its 100 votes to the
        candidate named here.

    Same wrap layout as the discovery probe / advertisement so receivers
    reuse the HMAC verification flow."""
    body = msgpack.packb({
        "cluster_uuid":     cluster_uuid,
        "node":             node,
        "ts":               float(ts),
        "believed_master":  believed_master or "",
        "transitioning":    bool(transitioning),
        "arbiter_uuid":     arbiter_uuid or "",
        "ack_target":       ack_target or "",
    }, use_bin_type=True)
    sig = hmac.new(key, body, hashlib.sha256).digest()
    return msgpack.packb({"v": HB_VERSION, "body": body, "sig": sig},
                          use_bin_type=True)


def decode_heartbeat(buf: bytes, *, key: bytes) -> Optional[dict]:
    """Verify an election heartbeat and return the body dict, or None on
    any signature / schema failure. Silent on failure for the same
    reason decode_probe is."""
    try:
        wrap = msgpack.unpackb(buf, raw=False)
        if not isinstance(wrap, dict):
            return None
        if wrap.get("v") != HB_VERSION:
            return None
        body_bytes = wrap.get("body")
        sig = wrap.get("sig")
        if not body_bytes or not sig:
            return None
        expected = hmac.new(key, body_bytes, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected):
            return None
        body = msgpack.unpackb(body_bytes, raw=False)
        if not isinstance(body, dict):
            return None
        for k in ("cluster_uuid", "node", "ts", "believed_master",
                  "transitioning", "arbiter_uuid", "ack_target"):
            if k not in body:
                return None
        return body
    except Exception:
        return None

def open_hb_recv_socket() -> socket.socket:
    """Single non-blocking UDP socket bound to 0.0.0.0:HB_PORT for
    incoming election heartbeats (protocol 4). Like the advertisement
    socket: plain unicast, the heartbeat body identifies its sender so
    no IP_PKTINFO is needed."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except OSError:
        pass
    s.bind(("", HB_PORT))
    s.setblocking(False)
    return s


def open_hb_send_socket() -> socket.socket:
    """Single non-blocking UDP socket for outgoing election heartbeats.
    No NIC bind — the kernel picks egress via the cluster /32 route to
    the peer's loopback (one heartbeat per peer, mirroring the
    advertisement send socket)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setblocking(False)
    return s
def _failover_ack_target(cluster_daemon, node_loopbacks: dict, peer_liveness: dict) -> str:
    """Who THIS node votes for once the master is lost.

    The candidate set = self + every reachable peer that has advertised
    (in its election heartbeat) that it is `transitioning` (claiming
    master-to-be). We vote for the LOWEST-loopback-octet candidate whose
    advertised arbiter-DRBD UUID is eligible against our own local 7-day
    history (lib.state.classify_arbiter_uuid): a superseded UUID is
    REFUSED (the split-brain guard — a stale candidate can never win our
    vote even on node count), current/unseen is votable.

    Returns the chosen candidate's node name, or "" if no candidate is
    eligible (we abstain — the cluster stays NoQuorum until an
    up-to-date node appears or the operator runs `seize`)."""
    try:
        try:
            from . import state as _lstate
        except ImportError:
            from lib import state as _lstate  # type: ignore
    except Exception:
        return ""

    def _octet(name: str) -> int:
        try:
            return int(node_loopbacks.get(name, "").rsplit(".", 1)[1])
        except (IndexError, ValueError):
            return 9999

    # Self is always a candidate (we are transitioning if we end up
    # picking ourselves). Peers are candidates only if they advertise
    # transitioning=True.
    candidates: dict[str, str] = {cluster_daemon.my_node: cluster_daemon.hb_arbiter_uuid}
    for peer, peer_heartbeat in cluster_daemon.peer_hb.items():
        if not peer_liveness.get(peer):
            continue
        if peer_heartbeat.get("transitioning"):
            candidates[peer] = peer_heartbeat.get("arbiter_uuid") or ""

    for name in sorted(candidates, key=lambda n: (_octet(n), n)):
        if _lstate.is_uuid_eligible(candidates[name]):
            return name
    return ""


def _parse_echo_addr(addr):
    """Parse a witness addr into (ipv4_literal, port) for a DIRECTED Echo probe,
    or None if it is not a usable IPv4 UNICAST literal.

    Deliberately strict — accepts ONLY an IPv4 unicast literal, because the
    directed probe runs INSIDE the 1Hz election tick:
      * a HOSTNAME would make sock.sendto do a SYNCHRONOUS getaddrinfo that
        blocks the whole election/heartbeat loop (a slow resolver during a
        partition could trip the missed-beat detector → spurious failover);
      * a MULTICAST / BROADCAST / 0.0.0.0 addr would re-flood the segment with
        an authenticated probe every second;
      * an IPv6 addr is unreachable on the AF_INET witness socket (it would
        gaierror + be silently swallowed).
    A rejected addr is simply not directed-probed; the witness still works via
    broadcast if it is on the local L2. Never raises."""
    import ipaddress
    addr = (addr or "").strip()
    if not addr or addr.startswith("["):          # bracket ⇒ IPv6, unreachable
        return None
    port = 12321
    host = addr
    n_colon = addr.count(":")
    if n_colon == 1:
        host, _, ps = addr.partition(":")
        try:
            port = int(ps)
        except ValueError:
            return None
    elif n_colon > 1:
        return None                               # bare IPv6 ⇒ unreachable
    if not (1 <= port <= 65535):
        return None
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None                               # hostname/garbage ⇒ no DNS
    if (ip.version != 4 or ip.is_multicast or ip.is_unspecified
            or ip.is_reserved or ip.is_loopback or ip.is_link_local):
        return None
    return (host, port)
def _election_tick(cluster_daemon, witness_state, _witness, _election, prev_outcome):
    """One election tick. Side-effects:
      - heartbeats / re-discovers the witness
      - on Leader outcome: DRIVES cluster_arbiter.promote_to_arbiter_host()
        (the base layer drives the promote; the arbiter writes
        mgmt_master to rqlite as a RESULT, only after the arbiter rqlite
        is back — H5/INV-6) and ensures the LMS bit when last-standing
        (H6). The arbiter, not netd, writes mgmt_master.
      - on NoQuorum: after the self-demote streak, drops the no-quorum
        marker + demotes the singletons if we were hosting
      - on transition back to Leader/Follower from NoQuorum: nothing
        (orchestrator's no_quorum_responder clears the marker after
         cleanup)
    Returns the new outcome string (for logging on transition only)."""
    # 1. Witness IO (best-effort).
    try:
        if _witness.needs_reprobe(witness_state):
            _witness.broadcast_probe(witness_state, ["255.255.255.255"])
            # Also directly probe CONFIGURED Echo witnesses (added BY IP /
            # routed off the local broadcast domain). The address list is set
            # from cluster state on the previous tick (cluster isn't loaded
            # until step 3 below). Both probes elicit replies keyed by echo_id,
            # so a configured + a broadcast-found Echo dedupe in discovered.
            if witness_state.configured_echo_addrs:
                _witness.unicast_probe(witness_state, witness_state.configured_echo_addrs)
        else:
            _witness.heartbeat_all(witness_state)
        _witness.drain_replies(witness_state)
    except Exception as e:
        sys.stderr.write(f"bedrock-cluster: witness IO error: {e!r}\n")

    # 2. Peer liveness from protocol-4 heartbeats only (independent of netd).
    peer_liveness: dict[str, bool] = {}

    # 3. Cluster snapshot from local rqlite (level='none' — works
    #    without quorum). Lightweight read each tick.
    try:
        try:
            from . import cluster_state as _cs
        except ImportError:
            import sys as _sys2
            _sys2.path.insert(0, "/usr/local/lib/bedrock")
            from lib import cluster_state as _cs  # type: ignore
        cluster = _cs.load_cluster()
    except Exception:
        return prev_outcome
    nodes = cluster.get("nodes") or {}
    if not nodes or cluster_daemon.my_node not in nodes:
        # Not yet bootstrapped; nothing to elect.
        return prev_outcome

    # The election denominator counts ACTIVE nodes only (C1): a node is
    # 'active' once it has finished its join saga (node_set_active) and
    # not in maintenance. A mid-join 'joining' node is excluded so the
    # master can't be tipped into NoQuorum while a peer is joining; a
    # drained ('maintenance') node likewise drops out of the tally.
    # Self is ALWAYS kept (cluster_init self-registers active, and we
    # must never vote ourselves out of our own denominator).
    def _is_active(name: str, info: dict) -> bool:
        if name == cluster_daemon.my_node:
            return True
        info = info or {}
        return (info.get("state", "active") == "active"
                and not info.get("maintenance"))

    active_nodes = {name: (info or {})
                    for name, info in nodes.items()
                    if _is_active(name, info)}
    node_loopbacks = {name: info.get("loopback_ip", "")
                      for name, info in active_nodes.items()}
    current_master = cluster.get("mgmt_master") or None

    # Plumb the active-member id set onto the witness so drain_replies
    # drops decommissioned/joining/drained nodes' slots and is_valid()
    # can certify the witness only when it holds a slot for every active
    # node (cluster-quorum-spec witness-validity + INV-7 path b). The
    # member set mirrors the election denominator (active nodes only).
    # node_id = last octet of loopback_ip (lib.rqlite_setup convention).
    member_ids: set[int] = set()
    for info in active_nodes.values():
        loop = info.get("loopback_ip", "")
        try:
            member_ids.add(int(loop.rsplit(".", 1)[1]))
        except (IndexError, ValueError):
            pass
    witness_state.member_ids = member_ids or None
    _witnesses = cluster.get("witnesses") or {}
    # ── Vote-config epoch + all-nodes-applied watermark (#7) ──────────────
    # Bar-LOWERING vote-config changes (credit the casting vote; drop a 'disabled'
    # witness from the DENOMINATOR) must NOT take effect at the master until EVERY
    # active node has applied the new config — else a lagging follower still
    # computing the OLD (still-quorate-for-it) config PLUS the master's lowered bar
    # = split-brain. Watermark = min(applied_epoch) over ACTIVE nodes; the arbiter
    # is not a nodes row so it's excluded by construction (counting it re-opens the
    # hole — see project_storage_unification_design / VOTE-CHANGE SAFETY PRINCIPLE).
    vote_config_epoch = int(cluster.get("vote_config_epoch") or 0)
    _applied_epochs = [int(info.get("applied_epoch") or 0)
                       for info in active_nodes.values()]
    all_applied_epoch = min(_applied_epochs) if _applied_epochs else 0
    config_fully_applied = all_applied_epoch >= vote_config_epoch
    # Advertise that THIS node has ingested the current epoch (its tick now uses the
    # new config). Churn-free: only write when our cached epoch is behind —
    # node_applied_epoch_set is ALSO monotonic + no-bump-on-noop (L57); this cache
    # just spares the rqlite round-trip on the 4 Hz steady state.
    if vote_config_epoch > cluster_daemon.applied_epoch_cache:
        try:
            try:
                from . import bedrock_state as _bs_ae     # type: ignore
            except ImportError:                            # pragma: no cover
                from lib import bedrock_state as _bs_ae    # type: ignore
            _bs_ae.node_applied_epoch_set(cluster_daemon.my_node, vote_config_epoch)
            cluster_daemon.applied_epoch_cache = vote_config_epoch
        except Exception as _e:
            sys.stderr.write(
                f"bedrock-cluster: applied_epoch advertise failed: {_e}\n")
    # DENOMINATOR: a 'disabled' witness leaves the count ONLY once the config is
    # fully applied cluster-wide. Until then it stays counted (higher bar = safe).
    # (corrupt ≠ disabled: corrupt drops the NUMERATOR immediately below; disabled
    # drops the DENOMINATOR under this gate — the two halves of the saga.)
    if config_fully_applied:
        n_configured_witnesses = sum(
            1 for w in _witnesses.values() if not w.get("disabled"))
    else:
        n_configured_witnesses = len(_witnesses)
    # 2-node casting-vote rescue (#7): the armed node (the incumbent master) gets
    # +1 in election.compute's steady-state-master branch. Crediting +1 is also
    # bar-lowering → gate it on the SAME all-applied watermark (pass None until
    # then, so the master holds the higher bar while a follower catches up). The
    # saga only ever arms it on N=2, and compute() ignores it everywhere except
    # when self IS that master — so an unarmed cluster sees no change.
    casting_vote_node = ((cluster.get("casting_vote_node") or None)
                         if config_fully_applied else None)
    # Bind voting witnesses to the configured set: only a reply whose echo_id
    # matches a configured witness_id is admitted/counted (drops a rogue Echo
    # and a just-removed witness's stale entry from the tally). EMPTY → None
    # (no filter): a lagging local replica (level='none', node replaying its
    # Raft log) can momentarily read ZERO witness rows without error; an empty
    # SET would then drop every live, legit Echo. None means "membership not
    # known here, don't filter" — and when there genuinely are 0 witnesses,
    # count_valid_confirmed returns 0 anyway (n_configured<=0), so no over-count.
    # Bind the tally to NON-CORRUPT witnesses: a witness flagged corrupt (its
    # own-readback failed somewhere — a lying store) is dropped from the vote
    # NUMERATOR stickily (it can never count again until the flag clears). This
    # is the SAFE direction (removes a vote → raises the bar); the DENOMINATOR
    # stays at len(_witnesses) until the casting-vote saga (#7) drops it under the
    # all-nodes-applied watermark. None (no filter) only when there are 0 live
    # witness ids, matching the lagging-replica convention below.
    witness_state.configured_witness_ids = {
        wid for wid, w in _witnesses.items()
        if not w.get("corrupt") and not w.get("disabled")
    } or None
    # Refresh the directed-probe list for next tick's witness IO: every
    # backend=='echo' witness's (host, port). Lets an Echo added BY IP that is
    # off the broadcast domain still get probed + vote.
    witness_state.configured_echo_addrs = [
        ep for ep in (
            _parse_echo_addr(w.get("addr", ""))
            for w in _witnesses.values()
            if (w.get("backend") or "echo") == "echo"
        ) if ep is not None
    ]
    # Refresh the fileshare-witness list (backend=='fileshare') for the
    # off-hot-path slot-IO worker: (witness_id, local mount path). The worker
    # writes/reads slot-<NN>.bin there and caches a verdict in witness_state.file_witnesses
    # that count_valid_confirmed folds. The election tick itself NEVER touches
    # the share (SMB/S3 latency stays off the 1Hz path); it only sets this list.
    # witness_id stays in configured_witness_ids above (set() over ALL backends),
    # so the tally's identity binding covers fileshare witnesses too.
    # storage_endpoints (S3/SMB/NFS) from the view — used to resolve an
    # endpoint-backed witness to its mount path (fileshare) or S3 client (s3).
    _endpoints = cluster.get("storage_endpoints") or {}
    try:
        from . import storage_mount as _sm
    except ImportError:                       # pragma: no cover
        from lib import storage_mount as _sm  # type: ignore

    def _fileshare_path(w):
        # An endpoint-backed fileshare witness lives at the Bedrock-managed
        # witness mountpoint (/mnt/bedrock/witness/<id>); a legacy one carries
        # its operator-provided path inline in `addr`.
        eid = w.get("endpoint_id")
        if eid:
            return str(_sm.mountpoint(eid, _sm.WITNESS))
        return w.get("addr", "")

    witness_state.configured_file_witnesses = [
        (wid, p)
        for wid, w in _witnesses.items()
        if (w.get("backend") or "echo") == "fileshare"
        for p in (_fileshare_path(w),) if p
    ]
    # S3 witnesses → lightweight refs (no secret on the hot path / in the
    # snapshot). The worker unseals the S3 secret from rqlite to build the client.
    witness_state.configured_s3_witness_refs = [
        (wid, w["endpoint_id"], _endpoints[w["endpoint_id"]])
        for wid, w in _witnesses.items()
        if (w.get("backend") or "echo") == "s3"
        and w.get("endpoint_id") and w["endpoint_id"] in _endpoints
    ]
    # M10 multi-witness: count CONFIGURED witnesses that are INDIVIDUALLY
    # valid+confirmed (capped at n_configured), not a hard-coded 0/1.
    # Single-witness testbed still yields 0/1; multiple valid witnesses
    # each contribute +1 to the tally.
    n_valid_witnesses = _witness.count_valid_confirmed(
        witness_state, n_configured_witnesses)

    # Surface a silent misconfiguration (fail-loud): an Echo is answering but
    # its echo_id matches no configured witness_id (a likely echo_id !=
    # witness_id typo, or a rogue) — so it never votes. Rate-limited to ~60s.
    if witness_state.seen_unconfigured_echo_ids:
        global _LAST_UNCONFIGURED_WARN
        now_w = time.monotonic()
        if now_w - _LAST_UNCONFIGURED_WARN >= 60.0:
            _LAST_UNCONFIGURED_WARN = now_w
            sys.stderr.write(
                "bedrock-cluster: WARNING — Echo(es) answering with echo_id(s) "
                f"{sorted(witness_state.seen_unconfigured_echo_ids)} that match NO "
                f"configured witness_id {sorted(witness_state.configured_witness_ids or [])}"
                " — they will NOT vote. Provision each Echo with "
                "--echo-id == its `bedrock witness add <id>` id.\n")

    # 3b. Local arbiter-DRBD UUID — the eligibility proof we advertise
    # AND the input we fold into our own 7-day history (so a voter on
    # THIS node can classify a candidate's advertised UUID against what
    # we've actually observed). Read once per tick.
    my_arbiter_uuid = _read_cluster_uuid()
    if my_arbiter_uuid:
        try:
            try:
                from . import state as _lstate
            except ImportError:
                from lib import state as _lstate  # type: ignore
            _lstate.record_arbiter_uuid(my_arbiter_uuid)
        except Exception as e:
            sys.stderr.write(f"bedrock-cluster: uuid-history record error: {e!r}\n")

    # 3. Election heartbeat liveness from peers (protocol 4). A peer
    # heartbeat is "fresh" only within a tight window (~1.5 beats) — a
    # peer we heard from just now. The single leader-loss detector counts
    # consecutive ticks with NO fresh heartbeat from the believed master;
    # at MASTER_LOSS_MISSES (~10 s) the survivor promotes and at one less
    # (~9 s) the old master self-demotes (NoQuorum), giving the INV-1
    # release-before-promote margin.
    now_mono = time.monotonic()
    fresh_s = ELECTION_INTERVAL_S * 1.5

    def _hb_fresh(node: str) -> bool:
        peer_heartbeat = cluster_daemon.peer_hb.get(node)
        return bool(hb and (now_mono - peer_heartbeat.get("seen_at_monotonic", 0.0)) <= fresh_s)

    if current_master and current_master != cluster_daemon.my_node:
        if _hb_fresh(current_master):
            cluster_daemon.missed_master_beats = 0
        else:
            cluster_daemon.missed_master_beats += 1
    else:
        # We are master, or no master is set — nothing to miss.
        cluster_daemon.missed_master_beats = 0

    # Peer reachability for the election folds in fresh election
    # heartbeats: a peer we still hear an HB from is reachable for vote
    # purposes even if its mesh link briefly flapped. A peer whose
    # heartbeat has gone silent drops out of the tally within the same
    # ~1.5 s window the master-loss detector uses.
    for peer in cluster_daemon.peer_hb:
        if _hb_fresh(peer):
            peer_liveness[peer] = True

    # 3. Build the ack map from peers' heartbeats: a peer acks US iff
    # its (fresh) ack_target names this node. compute() only consults
    # acks once the master is gone, so a stale ack while a master is
    # alive is harmless.
    peer_acks: dict[str, bool] = {}
    for peer, peer_heartbeat in cluster_daemon.peer_hb.items():
        if not _hb_fresh(peer):
            continue
        if peer_heartbeat.get("ack_target") == cluster_daemon.my_node:
            peer_acks[peer] = True
    cluster_daemon.peer_acks = peer_acks

    # Leader-loss is gated by the missed-beat detector: a master that is
    # still beating is followed; only after MASTER_LOSS_MISSES do we
    # treat it as gone and let a candidate promote.
    master_lost = (
        current_master is not None
        and current_master != cluster_daemon.my_node
        and cluster_daemon.missed_master_beats >= MASTER_LOSS_MISSES
    )
    # Until the 10-miss detector fires, keep the master "alive" in the
    # liveness map so compute() follows it through a brief mesh-link
    # flap (the single detector — not logged_up — owns leader-loss).
    if current_master and current_master != cluster_daemon.my_node and not master_lost:
        peer_liveness[current_master] = True

    # DEATH-ORACLE: is the current master's witness slot FRESH and HOSTING?
    # (docs/witness-death-oracle.md). If so, the master is alive even when the
    # mesh can't reach it (a clean partition), so we must NOT hide it from
    # compute — the far side follows it instead of taking over.
    master_witness_alive = False
    if current_master and current_master != cluster_daemon.my_node:
        _mlo = node_loopbacks.get(current_master, "")
        try:
            master_octet = int(_mlo.rsplit(".", 1)[-1]) if _mlo else 0
            _mslot = _witness.read_slot(witness_state, master_octet) if master_octet else None
            master_witness_alive = bool(
                _mslot and not _mslot.is_stale() and _mslot.hosting)
        except (ValueError, AttributeError):
            master_witness_alive = False
    # The master is only HIDDEN from compute (so a survivor may promote) when the
    # mesh missed it AND the witness confirms it is no longer hosting.
    master_effectively_gone = master_lost and not master_witness_alive

    # DRBD fence evidence from shared_state (independent of netd mesh table).
    _bedrock_state = getattr(cluster_daemon, "shared_state", None)
    _drbd_down_octets: set = set()
    if _bedrock_state is not None:
        _now_m = time.monotonic()
        with _bedrock_state.cluster_lock:
            drbd_down_peers = _bedrock_state.drbd_down_peers
            for octet, down_at in list(drbd_down_peers.items()):
                if (_now_m - _ts) > DRBD_DOWN_TTL_S:
                    del drbd_down_peers[octet]
            _drbd_down_octets = set(drbd_down_peers.keys())
        if _drbd_down_octets:
            for _nm, _lb in node_loopbacks.items():
                try:
                    if int(str(_lb).rsplit(".", 1)[-1]) in _drbd_down_octets:
                        peer_liveness[_nm] = False
                except (ValueError, AttributeError):
                    pass

    # 4. Decide. The election tallies node acks + valid witnesses; the
    # witness slot arbitration (UUID match, claim bit, readback) is
    # handled in cluster_arbiter.promote_to_arbiter_host() per the spec.
    result = _election.compute(
        self_name=cluster_daemon.my_node,
        self_loopback=cluster_daemon.my_loopback,
        peer_liveness=peer_liveness,
        node_loopbacks=node_loopbacks,
        # Hidden only when the 10-miss detector fired AND the witness says the
        # master no longer hosts — so a witness-fresh+HOSTING master is followed,
        # not taken over (the death-oracle), and a brief straggle never demotes.
        current_mgmt_master=(None if master_effectively_gone else current_master),
        n_configured_witnesses=n_configured_witnesses,
        n_valid_witnesses=n_valid_witnesses,
        peer_acks=peer_acks,
        casting_vote_node=casting_vote_node,
        master_witness_alive=master_witness_alive,
    )

    # 4a. Publish the live fence verdict to shared state (in-memory; REPLACES the
    # /run/bedrock/fence-verdict.json file). The /internal/fence-decision endpoint reads
    # this synchronously on a DRBD fence callout. `down_acked` = the DRBD-reported-down
    # octets this election incorporated (so the endpoint knows its evidence landed);
    # `stable_since` resets when (outcome, down_acked) changes so the endpoint waits for
    # convergence before answering. The witness CLAIM that makes an even split safe is
    # driven by ensure_witness_claim on the Leader path below — evidence-accelerated, so
    # `outcome==leader` already means "claim confirmed if pivotal". See lib/fence_verdict.py.
    if _bedrock_state is not None:
        try:
            _self_oct = int(str(cluster_daemon.my_loopback).rsplit(".", 1)[-1]) if cluster_daemon.my_loopback else 0
        except (ValueError, AttributeError):
            _self_oct = 0
        _now_mono = time.monotonic()
        _down_sorted = sorted(_drbd_down_octets)
        _key = (result.outcome.value, tuple(_down_sorted))
        try:
            with _bedrock_state.cluster_lock:
                _prev = _bedrock_state.fence_view or {}
                _stable = (_prev.get("stable_since", _now_mono)
                           if _prev.get("key") == _key else _now_mono)
                _bedrock_state.fence_view = {
                    "outcome": result.outcome.value,
                    "down_acked": _down_sorted,
                    "self_octet": _self_oct,
                    "updated": _now_mono,
                    "stable_since": _stable,
                    "key": _key,
                }
        except Exception as _e:  # never let the verdict bridge break the election tick
            sys.stderr.write(f"bedrock-cluster: fence_view publish error: {_e!r}\n")

    # 4b. Publish our own election-heartbeat fields for the next
    # hb_send_round so peers see our stance.
    #   believed_master — who we currently follow ("" if mid-failover).
    #   transitioning   — we have lost the master AND are advertising
    #                     ourselves as master-to-be (the lowest-octet
    #                     eligible contender among the reachable set).
    #   ack_target      — the contender we vote for (ourselves if we ARE
    #                     the lowest-octet eligible contender, else the
    #                     contender we defer to). This is computed
    #                     independently of compute()'s quorum gate so the
    #                     vote can BOOTSTRAP: peers ack the prospective
    #                     winner before it has reached quorum.
    cluster_daemon.hb_arbiter_uuid = my_arbiter_uuid or ""
    if master_lost:
        ack_target = _failover_ack_target(cluster_daemon, node_loopbacks, peer_liveness)
        cluster_daemon.hb_believed_master = ""
        cluster_daemon.hb_transitioning = (ack_target == cluster_daemon.my_node)
        cluster_daemon.hb_ack_target = ack_target
    elif result.outcome == _election.Outcome.LEADER:
        cluster_daemon.hb_believed_master = cluster_daemon.my_node
        cluster_daemon.hb_transitioning = False
        cluster_daemon.hb_ack_target = ""
    elif result.outcome == _election.Outcome.FOLLOWER:
        cluster_daemon.hb_believed_master = current_master or ""
        cluster_daemon.hb_transitioning = False
        cluster_daemon.hb_ack_target = ""
        # 2PC abort: if we set CLAIM|TRANSITIONING as Leader (takeover
        # phase 1) and then lost the election before hosting (deferred to
        # a returning master, vote flipped), drop the announced intent —
        # a Follower must never keep a witness claim pinned. The hosting
        # case is handled by the converge demote (which also clears the
        # tag); this covers the not-yet-hosting window the old per-tick
        # wipe used to (bugged, but it covered it).
        if witness_state.own_tag & _witness.TAG_TRANSITIONING:
            _witness.set_own_slot(witness_state, marker=witness_state.own_marker, tag=0)
    else:  # NoQuorum — advertise nothing definitive.
        cluster_daemon.hb_believed_master = ""
        cluster_daemon.hb_transitioning = False
        cluster_daemon.hb_ack_target = ""
        # Same 2PC abort as the Follower branch: a NoQuorum node mid-
        # takeover drops its announced intent. (The witness is usually
        # unreachable from here so the cleared tag may not ship until
        # heal — the claim-never-times-out INV-7 path still applies if
        # we die — but on heal the first heartbeat retracts it.)
        if witness_state.own_tag & _witness.TAG_TRANSITIONING:
            _witness.set_own_slot(witness_state, marker=witness_state.own_marker, tag=0)

    # 5. Log transitions.
    if prev_outcome != result.outcome.value:
        sys.stderr.write(
            f"bedrock-cluster: election {prev_outcome or '<init>'} → "
            f"{result.outcome.value} ({result.reason}; "
            f"votes={result.my_votes}/{result.majority} of {result.total_votes})\n"
        )

    # 5b. Persist who we believe is master (survives reboot; cold boot
    # reads it before rqlite quorum exists — see lib/state.py).
    believed = (cluster_daemon.my_node if result.outcome == _election.Outcome.LEADER
                else (current_master if result.outcome == _election.Outcome.FOLLOWER
                      and not master_lost else None))
    if believed != getattr(cluster_daemon, "_persisted_believed_master", "<unset>"):
        try:
            try:
                from . import state as _lstate
            except ImportError:
                from lib import state as _lstate  # type: ignore
            _lstate.set_believed_master(believed)
            cluster_daemon._persisted_believed_master = believed
        except Exception as e:
            sys.stderr.write(f"bedrock-cluster: believed-master persist error: {e!r}\n")

    # 6. Act on outcome.
    if result.outcome == _election.Outcome.NO_QUORUM:
        # Single self-demote detector. Count consecutive
        # NoQuorum ticks; an old master that has lost quorum self-demotes
        # at SELF_DEMOTE_MISSES (~9 s) — 1 s before a survivor promotes
        # at MASTER_LOSS_MISSES (~10 s), so .254 / arbiter rqlite is
        # released first (INV-1 margin). The same counter also rides out
        # the ~5 s startup window (neighbours=0 looks like NoQuorum) so a
        # fresh cluster_daemon doesn't self-mark on every restart.
        cluster_daemon.noquorum_master_ticks = getattr(cluster_daemon, "noquorum_master_ticks", 0) + 1
        if cluster_daemon.noquorum_master_ticks < SELF_DEMOTE_MISSES:
            return result.outcome.value
        _election.set_no_quorum_marker(result.reason)
        # If we were hosting the cluster singletons (.254 VIP, arbiter
        # rqlite, filer) at the moment quorum was lost, demote them
        # directly. cluster_arbiter.converge() can't help here — it
        # reads state.json["role"], which is only updated by the
        # rqlite subscriber, and rqlite is by definition unreachable
        # in NoQuorum. Without this, an isolated master keeps the
        # singletons up and would serve stale data to a still-attached
        # peer (the operator's workstation, in the e2e isolation test).
        # `demoted_in_cycle` fires the demote ONCE per NoQuorum episode
        # (not every tick — a noop replay does no harm but the log
        # churn is misleading). The arbiter owns the LMS clear on demote.
        if not getattr(cluster_daemon, "demoted_in_cycle", False):
            try:
                try:
                    from . import cluster_arbiter as _ca
                except ImportError:
                    from lib import cluster_arbiter as _ca  # type: ignore
                status = _ca.arbiter_status()
                hosting = (status.get("service_active")
                           or status.get("ip_present")
                           or status.get("mounted"))
                if hosting:
                    sys.stderr.write(
                        "bedrock-cluster: NoQuorum + currently hosting "
                        "arbiter — demoting singletons (release .254, "
                        "stop arbiter rqlite, drbdadm secondary)\n"
                    )
                    _ca.demote_arbiter_host()
                    # Only set the once-per-cycle latch AFTER we
                    # actually demoted. Otherwise an early NoQuorum at
                    # cluster_daemon startup (neighbours=0 → no quorum, but
                    # hosting=False because singletons haven't started
                    # yet) latches the flag and a later real isolation
                    # skips the demote.
                    cluster_daemon.demoted_in_cycle = True
            except Exception as e:
                sys.stderr.write(
                    f"bedrock-cluster: NoQuorum self-demote failed: {e!r}\n"
                )
    elif result.outcome == _election.Outcome.LEADER:
        # H5 / INV-6 two-tier ordering: netd (the base layer) DRIVES the
        # promote; mgmt_master is written by the arbiter as a RESULT, only
        # after the arbiter rqlite is back. netd does not write
        # set_mgmt_master here directly — driving the promote from the
        # rqlite role would be backwards (it needs the role to already be
        # set). The promote needs NO rqlite (witness + local only), so
        # there's no deadlock: promote_to_arbiter_host runs the takeover
        # protocol, brings up DRBD primary + .254 + arbiter rqlite +
        # filer, then writes mgmt_master once arbiter_status() confirms
        # hosting. Idempotent — on an already-hosting node it's a no-op.
        try:
            try:
                from . import cluster_arbiter as _ca
            except ImportError:
                from lib import cluster_arbiter as _ca  # type: ignore
            _ca.promote_to_arbiter_host()
            # H6: maintain our witness claim. node_has_majority is computed
            # from the SAME election result that put us in the Leader branch:
            # do our node-votes alone (100 per reachable active node, incl
            # self) already meet majority? If yes, the witness is not pivotal
            # → release any claim. If no, the witness is pivotal (we're Leader
            # so node+witness crosses the line) → claim it. The arbiter owns
            # the bit; this is idempotent and only flips on a real transition.
            node_has_majority = (
                _election.VOTES_PER_NODE * len(result.reachable_peers)
                >= result.majority
            )
            _ca.ensure_witness_claim(witness_state, node_has_majority=node_has_majority)
        except Exception as e:
            sys.stderr.write(
                f"bedrock-cluster: arbiter promote/lms tick failed: {e!r} "
                f"(will retry next tick)\n"
            )

    # Reset NoQuorum counter + demote-once flag when we leave the
    # NoQuorum state. Without the demote_in_cycle reset, an isolated
    # master that briefly recovers and then re-isolates would skip the
    # second demote, leaving .254 + arbiter rqlite live on a node that
    # no longer has quorum.
    if result.outcome != _election.Outcome.NO_QUORUM:
        cluster_daemon.noquorum_master_ticks = 0
        cluster_daemon.demoted_in_cycle = False

    # Publish our own witness slot MARKER every tick (the current DRBD
    # generation), but NEVER flip the witness-claim tag from a steady-state
    # heuristic (Q-01 / BAD-4). The claim bit is an explicit local
    # DECISION owned solely by cluster_arbiter: set when the witness is
    # pivotal, released when node-majority returns (ensure_witness_claim)
    # or on self-demote. Recomputing it here every tick raced the takeover
    # step-5 readback and could clear a claim the protocol meant to
    # hold. netd only refreshes the marker and leaves witness_state.own_tag exactly
    # as the arbiter last set it.
    try:
        uuid_hex = my_arbiter_uuid
        witness_state.own_marker = uuid_hex.encode("ascii") if uuid_hex else b""
    except Exception as e:
        sys.stderr.write(f"bedrock-cluster: own-slot publish error: {e!r}\n")

    return result.outcome.value
def _read_cluster_uuid() -> str:
    """The live current-UUID of the `cluster` singleton DRBD resource,
    role-bit masked. Delegates to `cluster_arbiter._read_local_drbd_uuid()`
    so the marker we PUBLISH to the witness here and the value the takeover
    protocol reads LOCALLY there are byte-identical (debugfs-first, bit-0
    masked, dump-md only when the resource is genuinely detached). Returns
    "" if unavailable — the witness slot then stays empty and the takeover
    UUID check no-ops. (Single source of truth for the read avoids the
    apples-vs-oranges bug where the published marker and the local read came
    from different sources / generations.)"""
    try:
        from . import cluster_arbiter as _ca
    except ImportError:
        from lib import cluster_arbiter as _ca  # type: ignore
    return _ca._read_local_drbd_uuid()


def _run_silent_capture(cmd: list[str]) -> tuple[int, str, str]:
    """Helper: capture stdout/stderr of a subprocess without raising."""
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode, r.stdout or "", r.stderr or ""


WITNESS_FILE_IO_INTERVAL_S = 3.0   # off-hot-path slot-IO cadence (< the 12s
#                                    witness freshness window even at S3 ~1.2s/op)


def _drive_s3_witnesses(witness_state, *, log=None):
    """Resolve the lightweight S3-witness refs to (witness_id, S3Config) by
    unsealing each endpoint's S3 secret from rqlite, then run one slot-IO cycle.
    The secret read + the HTTP IO both happen HERE (off the 1Hz tick). A witness
    whose secret can't be read/unsealed is skipped — it gets no verdict this pass
    and ages out of the tally (counts 0, the split-brain-safe direction)."""
    try:
        from . import witness_s3 as _w3       # type: ignore
        from . import bedrock_state as _bs     # type: ignore
    except ImportError:                        # pragma: no cover
        from lib import witness_s3 as _w3       # type: ignore
        from lib import bedrock_state as _bs    # type: ignore
    specs = []
    for wid, eid, ep in list(witness_state.configured_s3_witness_refs):
        try:
            secret = _bs.storage_endpoint_secret(eid, "s3_secret_key")
            specs.append((wid, _w3.S3Config.from_endpoint(ep, secret)))
        except Exception as e:
            if log is not None:
                log(f"s3 witness {wid} ({eid}) resolve failed: {e}")
    witness_state.configured_s3_witnesses = specs
    _w3.run_io_cycle(witness_state, log=log)


WITNESS_HEALTH_INTERVAL_S = 60.0    # own-readback health-check cadence (#6)


def _witness_health_check(witness_state, _witness_file, *, log=None):
    """Own-readback health probe for EVERY configured slot-store witness (s3 +
    fileshare). On a 'corrupt' verdict (the store accepted our slot write but
    can't return it — a lying/non-coherent store) flag the witness corrupt in
    rqlite: any node may flag, it's idempotent first-flag, and it drops the
    witness from the vote tally + signals the operator. 'unreachable'/'ok' do
    nothing (a transient just ages the slot out — split-brain-safe)."""
    try:
        from . import witness_s3 as _w3       # type: ignore
        from . import bedrock_state as _bs     # type: ignore
    except ImportError:                        # pragma: no cover
        from lib import witness_s3 as _w3       # type: ignore
        from lib import bedrock_state as _bs    # type: ignore

    def _flag(wid, reason):
        try:
            if _bs.witness_flag_corrupt(wid, reason) and log is not None:
                log(f"WITNESS {wid} FLAGGED CORRUPT (own-readback): {reason}")
        except Exception as e:
            if log is not None:
                log(f"could not flag witness {wid} corrupt: {e}")

    # S3 witnesses — clients resolved this cycle by _drive_s3_witnesses.
    for wid, cfg in list(witness_state.configured_s3_witnesses):
        try:
            status, detail = _w3.health_check(witness_state, _w3.S3Client(cfg))
        except Exception as e:
            if log is not None:
                log(f"s3 witness {wid} health probe error: {e}")
            continue
        if status == "corrupt":
            _flag(wid, f"s3: {detail}")
    # Fileshare witnesses — (witness_id, mount path).
    for wid, path in list(witness_state.configured_file_witnesses):
        try:
            status, detail = _witness_file.health_check(witness_state, path)
        except Exception as e:
            if log is not None:
                log(f"fileshare witness {wid} health probe error: {e}")
            continue
        if status == "corrupt":
            _flag(wid, f"fileshare: {detail}")


def _drive_casting_saga(my_node, log):
    """Drive ONE step of the 2-node casting-vote saga (#7) off the hot path. Loads
    the cached cluster view (level='none', revision-cached → ~free) and lets
    casting_saga decide the next vote-config transition; only the master ever acts,
    and only when a witness is corrupt on an N=2 cluster (else a pure no-op). Kept
    here in the witness worker — same cadence + same off-tick isolation as the #6
    health check that flags the corrupt witness this saga reacts to."""
    try:
        from . import cluster_state as _cs        # type: ignore
        from . import casting_saga as _saga        # type: ignore
    except ImportError:                            # pragma: no cover
        from lib import cluster_state as _cs       # type: ignore
        from lib import casting_saga as _saga      # type: ignore
    cluster = _cs.load_cluster(level="none")
    _saga.drive(cluster, my_node, log=log)


def _witness_file_worker(witness_state, _witness_file, should_stop, my_node="",
                         *, interval: float = WITNESS_FILE_IO_INTERVAL_S):
    """Background thread body: drive fileshare-witness slot IO OFF the 1Hz
    election tick (an SMB/S3 share's multi-hundred-ms latency must never stall
    mesh routing + election). Each pass calls witness_file.run_io_cycle, which
    writes our slot + caches a verdict in witness_state.file_witnesses for the tick to
    fold. A no-op while no fileshare witness is configured (the common case),
    so it is always safe to run.

    Fail-loud + always-alive: every iteration is wrapped in a broad except that
    logs and continues — a bug or an unexpected error must never silently kill
    witnessing (a dead worker would let every fileshare witness age out and
    quietly disable that arbitration path). Sleeps in small slices so shutdown
    is prompt (<=0.2s)."""
    _log = lambda m: sys.stderr.write(f"bedrock-cluster: {m}\n")
    _last_health = time.monotonic()   # first own-readback probe at +60s
    while not should_stop():
        try:
            if witness_state.configured_file_witnesses:
                _witness_file.run_io_cycle(witness_state, log=_log)
        except Exception as e:   # fail-loud, never let the worker die
            sys.stderr.write(
                f"bedrock-cluster: witness-file worker error: {e!r}\n")
        # S3 witnesses on the SAME off-hot-path worker: resolve each ref (unseal
        # the S3 secret from rqlite + build the SigV4 client) then run its slot
        # IO. Isolated in its own try so an S3 error never kills the fileshare
        # path (or the worker). No-op until a backend=='s3' witness is configured.
        try:
            if witness_state.configured_s3_witness_refs:
                _drive_s3_witnesses(witness_state, log=_log)
        except Exception as e:
            sys.stderr.write(f"bedrock-cluster: witness-s3 worker error: {e!r}\n")
        # ~1-min own-readback health check (#6): flag a lying store corrupt.
        if time.monotonic() - _last_health >= WITNESS_HEALTH_INTERVAL_S:
            _last_health = time.monotonic()
            try:
                if witness_state.configured_s3_witnesses or witness_state.configured_file_witnesses:
                    _witness_health_check(witness_state, _witness_file, log=_log)
            except Exception as e:
                sys.stderr.write(
                    f"bedrock-cluster: witness health-check error: {e!r}\n")
        # 2-node casting-vote saga (#7): react to a corrupt-flagged witness —
        # arm the casting vote + drop the witness from the denominator, one
        # all-applied-gated step per pass. Master-only + N=2-only inside; a pure
        # no-op otherwise. Isolated try so a saga error never kills witnessing.
        try:
            if my_node:
                _drive_casting_saga(my_node, _log)
        except Exception as e:
            sys.stderr.write(f"bedrock-cluster: casting-saga error: {e!r}\n")
        slept = 0.0
        while slept < interval and not should_stop():
            time.sleep(0.2)
            slept += 0.2
def _cluster_node_loopbacks(my_node: str) -> dict:
    """nodes -> loopback_ip mapping (best-effort). Used to know who to address
    advertisements/heartbeats to. Mesh routing decisions themselves never depend
    on this — membership is membership-of-record, not routing-of-record. Returns
    {} on any error.

    Reads via the REVISION-CACHED load_cluster (level='none'), NOT a fresh
    `SELECT ... FROM nodes` — this is called a few times per second and the nodes
    table never changes at idle, so the direct SELECT was pure rqlite load (RCA:
    ~3 uncached q/s into rqlited). load_cluster does one cheap revision read and
    returns the SHARED cached snapshot that the 4 Hz election tick already
    populated this tick — so on a cache hit there's no nodes scan + no rebuild."""
    try:
        try:
            from . import cluster_state as _cs
        except ImportError:
            import sys as _sys2
            _sys2.path.insert(0, "/usr/local/lib/bedrock")
            from lib import cluster_state as _cs  # type: ignore
        nodes = _cs.load_cluster(level="none").get("nodes") or {}
    except Exception:
        return {}
    out: dict[str, str] = {}
    for nm, info in nodes.items():
        if nm == my_node:
            continue
        lo = (info or {}).get("loopback_ip") or ""
        if lo:
            out[nm] = lo
    return out
def hb_send_round(cluster_daemon: ClusterDaemon, now_ts: float) -> None:
    """Send one signed election heartbeat (protocol 4) to every active cluster peer."""
    if cluster_daemon.hb_send_sock is None:
        return
    buf = encode_heartbeat(
        cluster_uuid=cluster_daemon.cluster_uuid,
        node=cluster_daemon.my_node,
        ts=now_ts,
        believed_master=cluster_daemon.hb_believed_master,
        transitioning=cluster_daemon.hb_transitioning,
        arbiter_uuid=cluster_daemon.hb_arbiter_uuid,
        ack_target=cluster_daemon.hb_ack_target,
        key=cluster_daemon.cluster_key,
    )
    for peer_node, peer_lo in _cluster_node_loopbacks(cluster_daemon.my_node).items():
        try:
            cluster_daemon.hb_send_sock.sendto(buf, (peer_lo, HB_PORT))
        except OSError:
            pass


def hb_drain(cluster_daemon: ClusterDaemon) -> None:
    """Drain incoming election heartbeats into cluster_daemon.peer_hb. Updates the
    per-peer last-heartbeat record (with monotonic receive time, used by
    the missed-beat detector)."""
    if cluster_daemon.hb_recv_sock is None:
        return
    while True:
        try:
            buf, src = cluster_daemon.hb_recv_sock.recvfrom(65536)
        except (BlockingIOError, socket.timeout):
            break
        except OSError:
            break
        body = decode_heartbeat(buf, key=cluster_daemon.cluster_key)
        if not body:
            continue
        if body.get("cluster_uuid") != cluster_daemon.cluster_uuid:
            continue
        peer = body.get("node") or ""
        if not peer or peer == cluster_daemon.my_node:
            continue
        cluster_daemon.peer_hb[peer] = {
            "believed_master":  body.get("believed_master") or "",
            "transitioning":    bool(body.get("transitioning")),
            "arbiter_uuid":     body.get("arbiter_uuid") or "",
            "ack_target":       body.get("ack_target") or "",
            "seen_at_monotonic": time.monotonic(),
        }

def load_state() -> tuple[bytes, str, str, str]:
    from . import netd as _netd
    return _netd.load_state()


def run_cluster_daemon(shared_state=None):
    """Cluster election + witness loop (protocol 4). Independent of lib/netd.py"""
    while True:
        try:
            cluster_key, cluster_uuid, my_node, my_loopback = load_state()
            break
        except RuntimeError as e:
            if shared_state is None:
                raise
            sys.stderr.write(
                f"bedrock-cluster: waiting for cluster bootstrap: {e}\n"
            )
            if shared_state.stop_event.wait(2.0):
                return
    if not my_loopback:
        try:
            try:
                from . import rqlite_client as _rc_mod
            except ImportError:
                sys.path.insert(0, "/usr/local/lib/bedrock")
                from lib import rqlite_client as _rc_mod  # type: ignore
            with _rc_mod.RqliteClient() as _rc:
                row = _rc.query_one(
                    "SELECT loopback_ip FROM nodes WHERE node_name = ?",
                    params=[my_node], level="none",
                )
            my_loopback = (row or {}).get("loopback_ip", "")
        except Exception:
            pass
    if my_loopback:
        from . import netd as _netd
        _netd.ensure_loopback_ip(my_loopback)

    cluster_daemon = ClusterDaemon(
        cluster_key=cluster_key,
        cluster_uuid=cluster_uuid,
        my_node=my_node,
        my_loopback=my_loopback,
    )
    if shared_state is not None:
        cluster_daemon.shared_state = shared_state
        with shared_state.cluster_lock:
            shared_state.cluster = cluster_daemon

    cluster_daemon.hb_recv_sock = open_hb_recv_socket()
    cluster_daemon.hb_send_sock = open_hb_send_socket()

    try:
        from . import witness as _witness, election as _election
        from . import witness_file as _witness_file
    except ImportError:
        sys.path.insert(0, "/usr/local/lib/bedrock")
        from lib import witness as _witness, election as _election  # type: ignore
        from lib import witness_file as _witness_file  # type: ignore

    try:
        my_node_id = int(my_loopback.rsplit(".", 1)[1]) if my_loopback else 0
    except (ValueError, IndexError):
        my_node_id = 0
    witness_state = _witness.WitnessState(
        cluster_uuid=cluster_uuid,
        cluster_key=_witness.load_cluster_key(),
        my_node_id=my_node_id,
        my_node_name=my_node,
    )
    try:
        witness_state.sock = _witness.open_socket()
    except OSError as _e:
        sys.stderr.write(
            f"bedrock-cluster: witness socket open failed: {_e}; "
            f"election runs without witness vote\n"
        )
        witness_state.sock = None
    if shared_state is not None:
        with shared_state.cluster_lock:
            shared_state.netd_ws = witness_state

    print(f"bedrock-cluster: cluster_uuid={cluster_uuid} node={my_node} "
          f"loopback={my_loopback or '<not yet assigned>'}",
          file=sys.stderr, flush=True)

    last_election_outcome = None
    last_election_at = 0.0

    def _should_stop() -> bool:
        if cluster_daemon.stopped:
            return True
        if shared_state is not None and shared_state.stop_event.is_set():
            return True
        return False

    _wf_thread = threading.Thread(
        target=_witness_file_worker,
        args=(witness_state, _witness_file, _should_stop, my_node),
        name="bedrock-witness-file", daemon=True,
    )
    _wf_thread.start()

    while not _should_stop():
        try:
            hb_drain(cluster_daemon)
            now = time.time()
            if now - last_election_at >= ELECTION_INTERVAL_S:
                last_election_outcome = _election_tick(
                    cluster_daemon, witness_state, _witness, _election, last_election_outcome)
                hb_send_round(cluster_daemon, now)
                last_election_at = now
                if shared_state is not None and last_election_outcome:
                    with shared_state.cluster_lock:
                        shared_state.last_election_outcome = last_election_outcome
                        shared_state.no_quorum_marker_present = (
                            last_election_outcome == "noquorum"
                        )
        except Exception as e:
            sys.stderr.write(f"bedrock-cluster: tick error: {e!r}\n")
        time.sleep(TICK_INTERVAL)

    _wf_thread.join(timeout=2.0)


def main():
    try:
        run_cluster_daemon()
    except RuntimeError as e:
        sys.stderr.write(f"bedrock-cluster: {e}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
