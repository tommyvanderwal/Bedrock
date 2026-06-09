"""Per-node rqlite setup — config materialisation + helpers.

The rqlite daemon (`rqlited`) needs a small set of CLI flags that
depend on this node's identity within the cluster (its node-id, its
loopback /32 IP, the list of peers to join). This module reads
cluster.json + state.json and writes /etc/bedrock/rqlited.env which
the systemd unit (configs/bedrock-rqlited.service) sources.

Three responsibilities:

  1. **render_env_file()** — write /etc/bedrock/rqlited.env from
     current cluster.json + state.json. Idempotent; safe to run
     before every service start.

  2. **bootstrap_for_init()** — first-time setup on the mgmt master:
     `-bootstrap-expect 1` for a brand-new N=1 cluster, no -join.
     Called once during `bedrock init`.

  3. **add_node()** — extend an existing cluster on `bedrock join`:
     `-join <leader-loopback>:4002`, no `-bootstrap-expect`. Called
     by agent_install on each new node.

The Bedrock identity model gives rqlite stable addresses by
construction. Each node's `100.X.Y.<idx>/32` lives on `lo`
permanently — rqlite binds there and never has to deal with IP
changes between restarts. The arbiter runs as a SEPARATE systemd
unit (bedrock-rqlited-arbiter.service); its env is rendered here by
render_arbiter_env_file(). The per-node rqlite this module sets up
is one of the three voters.

Run with: python3 rqlite_setup.py --render-env
       or: python3 rqlite_setup.py --init      (mgmt master only)
       or: python3 rqlite_setup.py --join <leader-loopback>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Iterable, Optional

# Standard /etc/bedrock config + /var/lib/bedrock data paths.
CLUSTER_JSON     = Path("/etc/bedrock/cluster.json")
STATE_JSON       = Path("/etc/bedrock/state.json")
RQLITED_ENV      = Path("/etc/bedrock/rqlited.env")
DATA_DIR         = Path("/var/lib/bedrock/rqlite")

RAFT_PORT = 4002
HTTP_PORT = 4001

# Arbiter env path — separate so the arbiter rqlited unit reads its
# own config without colliding with the per-node rqlited's.
ARBITER_ENV     = Path("/etc/bedrock/rqlited-arbiter.env")
ARBITER_DATA_DIR = Path("/var/lib/bedrock/cluster/rqlite")
# Arbiter node-id: 254 to match its /24 octet and to stay distinct
# from any per-node id (the loopback last octet). 0 is reserved by
# rqlite as "auto"; we avoid it.
ARBITER_NODE_ID  = 254


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _sorted_node_index(cluster: dict, node_name: str) -> Optional[int]:
    """Return this node's 1-based index in the sorted-by-name node
    list, or None if the node isn't in cluster.json yet.

    The rqlite node-id is derived from the loopback's last octet, not
    this index: a sorted-name index shifts everyone when a new node
    sorts before an existing one, but the loopback octet is permanent
    and unique per node.
    """
    nodes = cluster.get("nodes") or {}
    if node_name not in nodes:
        return None
    names = sorted(nodes.keys())
    return names.index(node_name) + 1


def _peer_loopbacks(cluster: dict, my_node: str) -> list[str]:
    """All other nodes' loopback /32s (no port suffix yet). Empty
    list at N=1."""
    nodes = cluster.get("nodes") or {}
    out: list[str] = []
    for name, info in sorted(nodes.items()):
        if name == my_node:
            continue
        lo = (info or {}).get("loopback_ip", "")
        if lo:
            out.append(lo)
    return out


def render_env_file(
    *,
    cluster_path: Path = CLUSTER_JSON,
    state_path: Path = STATE_JSON,
    env_path: Path = RQLITED_ENV,
    data_dir: Path = DATA_DIR,
) -> dict:
    """Write the rqlited env file from current cluster + state.

    Returns the rendered env dict (also written to env_path).
    Idempotent: a re-render with identical inputs produces an
    identical file.

    Behaviour for the join/bootstrap flag:

      * If state.json says this node holds the mgmt-master role AND
        cluster.json has exactly one node (us): emit
        `-bootstrap-expect 1` so the rqlite cluster forms from
        this single node. No -join.
      * Otherwise: emit `-join` to every other node's loopback at
        the Raft port. No -bootstrap-expect.

    The systemd unit references variables via ${BEDROCK_RQLITED_*}.
    A "flag" variant (e.g. BEDROCK_RQLITED_JOIN_FLAG) is also
    written so the ExecStart line can include or omit the flag
    without a separate template — empty string means "no flag."
    """
    cluster = _read_json(cluster_path)
    state = _read_json(state_path)

    my_node = state.get("node_name", "")
    my_loopback = state.get("loopback_ip", "")

    if not my_node or not my_loopback:
        # state.json may have been lost to a 0-byte truncation (power-loss
        # in save()'s rename window) while cluster.json survived. Rather
        # than crash-loop the rqlited unit forever (node bricked, observed
        # sim-4 2026-05-29), self-heal this node's identity from
        # cluster.json + hostname, then re-read.
        try:
            from . import state as _state_mod
            _state_mod.recover_identity_from_cluster_json()
        except Exception:
            pass
        state = _read_json(state_path)
        my_node = state.get("node_name", "")
        my_loopback = state.get("loopback_ip", "")

    my_role = state.get("role", "")

    if not my_node or not my_loopback:
        raise RuntimeError(
            f"rqlite_setup: cannot render env yet — "
            f"node_name={my_node!r} loopback_ip={my_loopback!r}. "
            f"Run after state.json is populated (post-init/join). "
            f"(cluster.json self-heal could not supply identity either.)"
        )

    # rqlite node-id MUST be stable across the cluster's lifetime —
    # once a node-id is in the Raft store, you can't change it without
    # snapshot wipe. Sorted-name index breaks this: a new node whose
    # name sorts before an existing one would shift everyone's index.
    # The loopback's last octet is permanent (cluster_uuid-derived,
    # allocated sequentially on join) and unique per node, so use it.
    try:
        node_idx = int(my_loopback.rsplit(".", 1)[1])
    except (ValueError, IndexError):
        raise RuntimeError(
            f"rqlite_setup: cannot derive node-id from "
            f"loopback_ip={my_loopback!r}"
        )
    if my_node not in (cluster.get("nodes") or {}):
        raise RuntimeError(
            f"rqlite_setup: node {my_node!r} not in cluster.json yet. "
            f"Run after the node has been registered."
        )

    nodes = cluster.get("nodes") or {}
    is_solo_master = (len(nodes) == 1 and my_node in nodes
                      and "mgmt" in my_role)

    peers = _peer_loopbacks(cluster, my_node)

    env: dict[str, str] = {
        "BEDROCK_RQLITED_NODE_ID": str(node_idx),
        "BEDROCK_RQLITED_BIND_IP": my_loopback,
        "BEDROCK_RQLITED_DATA_DIR": str(data_dir),
        # CDC fast-path (the central event loop's event trigger): the Raft
        # LEADER POSTs each applied commit to this node's own bedrock-d on
        # loopback :8001, which wakes the central loop and fans the nudge
        # out to peers. Set uniformly on every node — rqlite CDC transmits
        # only from whichever node is currently leader, so it follows
        # failover automatically with no per-node difference. Loopback HTTP
        # means no TLS. rqlite batches (max_batch_delay 200ms / size 10) and
        # retries indefinitely, so a momentarily-down bedrock-d loses no
        # events; the loop's poll floor is the correctness backstop.
        "BEDROCK_RQLITED_CDC_FLAG":
            "-cdc-config http://127.0.0.1:8001/api/internal/cdc",
    }
    if is_solo_master:
        # N=1 fresh init — bootstrap a single-node Raft cluster.
        env["BEDROCK_RQLITED_BOOTSTRAP_FLAG"] = "-bootstrap-expect 1"
        env["BEDROCK_RQLITED_JOIN_FLAG"] = ""
    elif peers:
        # Established cluster — join via peer Raft addresses.
        join_targets = ",".join(f"{ip}:{RAFT_PORT}" for ip in peers)
        env["BEDROCK_RQLITED_JOIN_FLAG"] = f"-join {join_targets}"
        env["BEDROCK_RQLITED_BOOTSTRAP_FLAG"] = ""
    else:
        # No peers yet and we're not solo master — pre-join. Don't
        # produce a flag set that would cause rqlite to bootstrap
        # spuriously; let the service stay in a wait state until
        # the cluster.json catches up.
        raise RuntimeError(
            "rqlite_setup: no peers in cluster.json and not the "
            "solo mgmt-master. Refusing to render env — wait for "
            "the cluster snapshot to settle."
        )

    # Ensure data dir + parent exist before service start. Mode 0700;
    # the unit's PrivateTmp + ProtectSystem will further sandbox.
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    # Atomic env-file write: write to a temp file then rename. Avoids
    # half-written-env crashes if systemd reads concurrently.
    tmp_path = env_path.with_suffix(env_path.suffix + ".tmp")
    tmp_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{k}={v}" for k, v in env.items()]
    tmp_path.write_text("\n".join(lines) + "\n")
    os.replace(tmp_path, env_path)

    return env


def render_arbiter_env_file(
    *,
    cluster_path: Path = CLUSTER_JSON,
    state_path: Path = STATE_JSON,
    env_path: Path = ARBITER_ENV,
    data_dir: Path = ARBITER_DATA_DIR,
) -> dict:
    """Materialise /etc/bedrock/rqlited-arbiter.env for the arbiter
    rqlite daemon. The arbiter binds to `100.X.Y.254/32` and joins
    the existing per-node rqlite peers.

    Called from cluster_arbiter.promote_to_arbiter_host() right
    before systemctl start bedrock-rqlited-arbiter — at that point
    the .254/32 has just been claimed on lo, so the bind address
    is locally reachable.

    Raises RuntimeError if the cluster snapshot isn't ready (we
    can't derive the .254 IP without cluster_uuid).
    """
    from . import cluster_arbiter as ca

    cluster = _read_json(cluster_path)
    state = _read_json(state_path)

    arbiter_ip = ca.arbiter_loopback_ip()
    if not arbiter_ip:
        raise RuntimeError(
            "rqlite_setup: arbiter IP unknown — cluster.json must "
            "contain cluster_uuid before the arbiter can start"
        )

    # Peer list: this node's per-node rqlite FIRST (always reachable
    # — we're on the same host), then every other node's rqlite.
    # Order matters: rqlited tries -join entries left-to-right with
    # a 30s TCP connect timeout per entry. An unreachable peer first
    # in the list burns 30 seconds before falling back to the local
    # one, which would eat the entire 45s failover window. Local-first
    # means worst-case join time is <1s.
    my_node = state.get("node_name", "")
    my_loopback = state.get("loopback_ip", "")
    peers: list[str] = []
    if my_loopback:
        peers.append(my_loopback)
    for p in _peer_loopbacks(cluster, my_node):
        if p not in peers:
            peers.append(p)

    if not peers:
        raise RuntimeError(
            "rqlite_setup: no per-node rqlite peers known — arbiter "
            "can't bootstrap without at least one peer to join"
        )

    env: dict[str, str] = {
        "BEDROCK_ARBITER_NODE_ID": str(ARBITER_NODE_ID),
        "BEDROCK_ARBITER_BIND_IP": arbiter_ip,
        "BEDROCK_ARBITER_DATA_DIR": str(data_dir),
        # Bootstrap-expect 0 / no flag — the per-node rqlite cluster
        # is already formed by the time the arbiter joins, so we
        # always -join, never -bootstrap-expect.
        "BEDROCK_ARBITER_BOOTSTRAP_FLAG": "",
        "BEDROCK_ARBITER_JOIN_FLAG":
            "-join " + ",".join(f"{ip}:{RAFT_PORT}" for ip in peers),
    }

    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp_path = env_path.with_suffix(env_path.suffix + ".tmp")
    tmp_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{k}={v}" for k, v in env.items()]
    tmp_path.write_text("\n".join(lines) + "\n")
    os.replace(tmp_path, env_path)

    return env


def cli() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--render-env", action="store_true",
                   help="Materialise /etc/bedrock/rqlited.env from "
                        "cluster.json + state.json. Idempotent.")
    g.add_argument("--init", action="store_true",
                   help="First-time bootstrap on the mgmt master "
                        "(implies --render-env, then validates the "
                        "single-node bootstrap setup).")
    g.add_argument("--join", metavar="LEADER_LOOPBACK",
                   help="Joiner-side helper: produce an env where "
                        "BEDROCK_RQLITED_JOIN_FLAG points at the "
                        "given leader loopback. Useful when the "
                        "joiner's cluster.json hasn't replicated "
                        "yet but agent_install knows the leader.")
    args = p.parse_args()

    try:
        if args.render_env:
            env = render_env_file()
            for k, v in env.items():
                print(f"{k}={v}")
            return 0

        if args.init:
            # Render and confirm it's a bootstrap setup.
            env = render_env_file()
            if "bootstrap-expect" not in (env.get("BEDROCK_RQLITED_BOOTSTRAP_FLAG") or ""):
                print(
                    "rqlite_setup --init: expected -bootstrap-expect "
                    "for an init, got: "
                    f"{env.get('BEDROCK_RQLITED_BOOTSTRAP_FLAG')!r}. "
                    "Check that cluster.json has exactly this node.",
                    file=sys.stderr,
                )
                return 2
            return 0

        if args.join:
            # Render normally — if cluster.json already has the
            # leader's entry, peers list will include it. If not
            # (race during early join), override.
            try:
                env = render_env_file()
            except RuntimeError:
                env = {
                    "BEDROCK_RQLITED_NODE_ID": "0",  # sentinel; caller
                                                       # should re-render
                    "BEDROCK_RQLITED_BIND_IP": "0.0.0.0",
                    "BEDROCK_RQLITED_DATA_DIR": str(DATA_DIR),
                    "BEDROCK_RQLITED_BOOTSTRAP_FLAG": "",
                }
            env["BEDROCK_RQLITED_JOIN_FLAG"] = f"-join {args.join}:{RAFT_PORT}"
            env["BEDROCK_RQLITED_BOOTSTRAP_FLAG"] = ""
            # Write the override env
            lines = [f"{k}={v}" for k, v in env.items()]
            RQLITED_ENV.parent.mkdir(parents=True, exist_ok=True)
            RQLITED_ENV.write_text("\n".join(lines) + "\n")
            for k, v in env.items():
                print(f"{k}={v}")
            return 0

    except RuntimeError as e:
        print(f"rqlite_setup: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(cli())
