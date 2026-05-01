"""Materialised-view builder.

Replays the bedrock-rust log via IPC and folds typed entries into the
two on-disk JSON files Bedrock has historically maintained ad-hoc:

  /etc/bedrock/cluster.json  — cluster_name, cluster_uuid, nodes,
                                tiers, witnesses, params
  /etc/bedrock/state.json    — this node's role + mgmt_url + witness_host

The log is canonical (design §3); these JSON files are caches that any
consumer (the FastAPI app, `bedrock storage status`, the operator
running `cat`) can read. Rebuild them with `rebuild()` whenever the
log moves; on a real cluster a small daemon will do this in response
to commit events. v0.1 just provides the rebuild function — callers
invoke it explicitly after an append.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import log_entries as le
from . import rust_ipc


CLUSTER_JSON = Path("/etc/bedrock/cluster.json")
STATE_JSON = Path("/etc/bedrock/state.json")


def fold(entries: list[dict]) -> dict:
    """Fold an ordered list of decoded log-entry payloads into a
    cluster-shaped dict. Pure function — easy to unit-test.

    Returns:
        {
            "cluster_name": str,
            "cluster_uuid": str,
            "nodes": {name: {host, drbd_ip, role, pubkey}},
            "tiers": {tier: {mode, master, peers, drbd_node_ids,
                              backend_path, garage_endpoint, version}},
            "witnesses": {wid: {addr, witness_pubkey, encrypted_witness_key}},
            "params": {key: value},
            "mgmt_master": str | None,
            "log_index": int,    # index of the last folded entry
        }
    """
    out: dict[str, Any] = {
        "cluster_name": None,
        "cluster_uuid": None,
        "nodes": {},
        "tiers": {},
        "witnesses": {},
        "params": {},
        "mgmt_master": None,
        "log_index": 0,
    }
    for entry in entries:
        payload = le.decode(entry["payload"])
        kind = payload.get("t")
        out["log_index"] = entry["index"]

        if kind == le.CLUSTER_INIT:
            out["cluster_name"] = payload["name"]
            out["cluster_uuid"] = payload["uuid"]

        elif kind == le.NODE_REGISTER:
            n = payload["node_name"]
            existing = out["nodes"].get(n, {})
            existing.update({
                "host": payload["host"],
                "drbd_ip": payload["drbd_ip"],
                "role": payload.get("role", "compute"),
                "pubkey": payload.get("pubkey", ""),
            })
            out["nodes"][n] = existing

        elif kind == le.NODE_UNREGISTER:
            out["nodes"].pop(payload["node_name"], None)
            # Also strip from tier peer lists.
            for t in out["tiers"].values():
                if payload["node_name"] in t.get("peers", []):
                    t["peers"] = [p for p in t["peers"] if p != payload["node_name"]]
                t.get("drbd_node_ids", {}).pop(payload["node_name"], None)

        elif kind == le.MGMT_MASTER:
            old = out["mgmt_master"]
            new = payload["node_name"]
            out["mgmt_master"] = new
            for n_name, info in out["nodes"].items():
                if n_name == new:
                    info["role"] = "mgmt+compute"
                elif n_name == old and info.get("role") == "mgmt+compute":
                    info["role"] = "compute"

        elif kind == le.TIER_STATE:
            tier = payload["tier"]
            existing = out["tiers"].get(tier, {})
            existing["mode"] = payload["mode"]
            if payload.get("master") is not None:
                existing["master"] = payload["master"]
            if payload.get("peers"):
                existing["peers"] = list(payload["peers"])
            if payload.get("backend_path") is not None:
                existing["backend_path"] = payload["backend_path"]
            if payload.get("garage_endpoint") is not None:
                existing["garage_endpoint"] = payload["garage_endpoint"]
            existing["version"] = existing.get("version", 0) + 1
            out["tiers"][tier] = existing

        elif kind == le.DRBD_NODE_ID:
            tier = payload["tier"]
            t = out["tiers"].setdefault(tier, {"mode": "local"})
            ids = t.setdefault("drbd_node_ids", {})
            ids[payload["node_name"]] = payload["node_id"]

        elif kind == le.WITNESS_REGISTER:
            wid = payload["witness_id"]
            out["witnesses"][wid] = {
                "addr": payload["addr"],
                "witness_pubkey": payload["witness_pubkey"],
                "encrypted_witness_key": payload["encrypted_witness_key"],
            }

        elif kind == le.WITNESS_UNREGISTER:
            out["witnesses"].pop(payload["witness_id"], None)

        elif kind == le.PARAM_CHANGE:
            out["params"][payload["key"]] = payload["value"]

        # Bootstrap entry, free-form payloads, and unknown kinds are
        # ignored — they just record history without affecting the
        # materialised view.
    return out


def rebuild(sock_path: str = rust_ipc.DEFAULT_SOCK,
            cluster_json: Path = CLUSTER_JSON,
            state_json: Path = STATE_JSON,
            *,
            this_node: str | None = None) -> dict:
    """Pull the whole log via IPC, fold it, and rewrite the JSON caches.

    `this_node` is used to project the cluster-wide view onto
    state.json (each node's state.json holds *its* role; cluster.json
    is identical on every node).
    """
    with rust_ipc.Daemon(sock_path) as d:
        entries = list(d.read(from_index=1))
    view = fold(entries)

    cluster_json.parent.mkdir(parents=True, exist_ok=True)
    cluster_json.write_text(json.dumps(_cluster_view(view), indent=2))

    if this_node and this_node in view["nodes"]:
        state_json.parent.mkdir(parents=True, exist_ok=True)
        existing = {}
        if state_json.exists():
            try:
                existing = json.loads(state_json.read_text())
            except json.JSONDecodeError:
                existing = {}
        existing.update(_state_view(view, this_node))
        state_json.write_text(json.dumps(existing, indent=2))

    return view


def _cluster_view(v: dict) -> dict:
    """The cluster.json shape — cluster-wide canonical view."""
    return {
        "cluster_name": v["cluster_name"],
        "cluster_uuid": v["cluster_uuid"],
        "nodes": v["nodes"],
        "tiers": v["tiers"],
        "witnesses": v["witnesses"],
        "params": v["params"],
        "log_index": v["log_index"],
    }


def _state_view(v: dict, node_name: str) -> dict:
    """The state.json shape — this node's POV."""
    me = v["nodes"].get(node_name, {})
    master = v.get("mgmt_master")
    master_host = (
        v["nodes"].get(master, {}).get("host", "") if master else ""
    )
    return {
        "node_name": node_name,
        "cluster_name": v["cluster_name"],
        "cluster_uuid": v["cluster_uuid"],
        "role": me.get("role", "compute"),
        "mgmt_ip": me.get("host", ""),
        "drbd_ip": me.get("drbd_ip", ""),
        "mgmt_url": f"http://{master_host}:8080" if master_host else "",
        "witness_host": master_host,  # v0.1 — phase 6 swaps in real witnesses
    }
