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
construction (see post-alpha-rewrite-notes.md D-02 + D-05). Each
node's `100.X.Y.<idx>/32` lives on `lo` permanently — rqlite binds
there and never has to deal with IP changes between restarts. The
arbiter (D-04) is handled by a SEPARATE systemd unit
(bedrock-rqlited-arbiter.service, not in this file yet); the
per-node rqlite this module sets up is one of the three voters.

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

# Paths mirror the conventions used by daemon_setup.py for bedrock-rust.
CLUSTER_JSON     = Path("/etc/bedrock/cluster.json")
STATE_JSON       = Path("/etc/bedrock/state.json")
RQLITED_ENV      = Path("/etc/bedrock/rqlited.env")
DATA_DIR         = Path("/var/lib/bedrock/rqlite")

RAFT_PORT = 4002
HTTP_PORT = 4001


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _sorted_node_index(cluster: dict, node_name: str) -> Optional[int]:
    """Return this node's 1-based index in the sorted-by-name node
    list. Used as the rqlite node-id — stable across restarts as
    long as the node name doesn't change (which it never does in
    Bedrock).

    Returns None if the node isn't in cluster.json yet (pre-join
    state — caller falls back to a sentinel like 0 which rqlite
    refuses to accept, surfacing the bootstrap-ordering issue
    early).
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
    my_role = state.get("role", "")

    if not my_node or not my_loopback:
        raise RuntimeError(
            f"rqlite_setup: cannot render env yet — "
            f"node_name={my_node!r} loopback_ip={my_loopback!r}. "
            f"Run after state.json is populated (post-init/join)."
        )

    node_idx = _sorted_node_index(cluster, my_node)
    if node_idx is None:
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
