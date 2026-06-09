"""Cluster + node + topology read endpoints (the dashboard's main views)."""
from __future__ import annotations
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException
from common import (load_cluster, get_nodes, build_cluster_state,
                    build_physical_topology, get_witness_status, get_last_state)
from tasks import registry as task_registry
router = APIRouter(tags=["cluster"])



# ── REST API (for curl/scripting) ──────────────────────────────────────────

@router.get("/api/cluster")
def api_cluster():
    # Serve cached state. Fresh data lands every 3s via the push loop.
    return get_last_state()




@router.get("/api/topology")
def api_topology():
    """Physical topology rollup — switches and routers each cluster
    NIC sees, grouped by device_key (MAC). Computed every 3 s by the
    state push loop from each node's /run/bedrock/switch_neighbors.json.
    Not consensus state — purely a derived view for the dashboard."""
    return get_last_state().get("topology", {"switches": {}, "links": [],
                                          "node_count": 0, "switch_count": 0,
                                          "link_count": 0,
                                          "computed_at": 0.0})




@router.get("/cluster-info")
def cluster_info():
    """Discovery endpoint — lets `bedrock join` find this cluster."""
    state_file = Path("/etc/bedrock/state.json")
    cluster = load_cluster()
    info = {
        "cluster_name": cluster.get("cluster_name", "bedrock"),
        "cluster_uuid": cluster.get("cluster_uuid", "unknown"),
        "nodes": list(cluster.get("nodes", {}).keys()),
    }
    if state_file.exists():
        s = json.loads(state_file.read_text())
        info["cluster_uuid"] = s.get("cluster_uuid", info["cluster_uuid"])
        info["mgmt_url"] = s.get("mgmt_url", "")
        info["witness_host"] = s.get("witness_host", "")
    return info




# Node registration goes through the join-handshake flow
# (`POST /api/join/request` → operator approval → `POST /api/join/approve`):
# SSH-pubkey fan-out, loopback-IP allocation, and node_register+node_loopback
# logging, with cluster.key shipped AEAD-sealed under an ECDH session key
# (see lib/join_handshake.py) rather than in plaintext.


@router.get("/api/nodes")
def list_nodes():
    return load_cluster().get("nodes", {})
