#!/usr/bin/env python3
"""Bedrock HA failover orchestrator.

Runs on each node. Sends heartbeats to the witness, monitors peer via
both network paths (direct cable + switch LAN), and uses 2-of-3 quorum
to decide failover.

Quorum voters:
  1. This node (always 1 vote for itself)
  2. Peer node (reachable via direct cable OR switch LAN)
  3. Witness (reachable via switch LAN only)

Need 2 of 3 to have quorum. If peer is reachable (either path), no
failover needed. If peer is unreachable but witness confirms it dead,
this node has quorum (self + witness = 2) and can take over.

Usage:
    bedrock-failover.py --node node1 --peer node2
    bedrock-failover.py --node node2 --peer node1
"""

import argparse
import json
import logging
import subprocess
import sys
import time
import socket
import urllib.request
import urllib.error

WITNESS_URL = "http://192.168.2.252:9443"
HEARTBEAT_INTERVAL = 3      # seconds between heartbeats
DEAD_THRESHOLD = 3           # consecutive "no quorum for peer" checks before takeover
CHECK_INTERVAL = 2           # seconds between peer status checks

# Peer addresses — direct cable and switch LAN
PEER_ADDRS = {
    "node1": ["10.99.0.1", "192.168.2.141"],
    "node2": ["10.99.0.2", "192.168.2.142"],
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bedrock")

# Map DRBD resources to VM names
RESOURCE_VM_MAP = {
    "vm-test-disk0": "vm-test",
    "vm-win-disk0": "vm-win",
}


def http_post(url, timeout=3):
    try:
        req = urllib.request.Request(url, method="POST", data=b"")
        urllib.request.urlopen(req, timeout=timeout)
        return True
    except Exception:
        return False


def http_get_json(url, timeout=3):
    try:
        resp = urllib.request.urlopen(url, timeout=timeout)
        return json.loads(resp.read())
    except Exception:
        return None


def tcp_ping(host, port=22, timeout=2):
    """Check if a host is reachable via TCP (SSH port)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((host, port))
        s.close()
        return True
    except Exception:
        return False


def run(cmd, timeout=10):
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return result.stdout.strip(), result.returncode
    except subprocess.TimeoutExpired:
        return "", 1


def send_heartbeat(node):
    """Send heartbeat for this node and all its resources."""
    http_post(f"{WITNESS_URL}/heartbeat/{node}")
    for resource in RESOURCE_VM_MAP:
        http_post(f"{WITNESS_URL}/heartbeat/{node}/{resource}")


def check_peer_direct(peer):
    """Check if peer is reachable via any direct network path (SSH port)."""
    addrs = PEER_ADDRS.get(peer, [])
    for addr in addrs:
        if tcp_ping(addr, port=22, timeout=2):
            return True, addr
    return False, None


def check_witness_says_peer_dead(peer):
    """Ask witness if peer is dead. Returns True if peer is dead, False if alive, None if witness unreachable."""
    status = http_get_json(f"{WITNESS_URL}/status")
    if status is None:
        return None  # can't reach witness
    peer_info = status.get("nodes", {}).get(peer)
    if peer_info is None:
        return True  # witness has never seen the peer = dead
    return not peer_info.get("alive", False)


def get_local_drbd_roles():
    """Get current DRBD roles for all resources."""
    roles = {}
    for resource in RESOURCE_VM_MAP:
        out, rc = run(f"drbdadm role {resource}")
        roles[resource] = out.strip() if rc == 0 else "Unknown"
    return roles


def get_local_vms():
    """Get running VM names."""
    out, _ = run("virsh list --name --state-running")
    return set(out.split()) if out else set()


def takeover_resource(resource):
    """Promote DRBD and start VM for a resource."""
    vm_name = RESOURCE_VM_MAP[resource]
    log.warning(f"TAKEOVER: promoting {resource} and starting {vm_name}")

    out, rc = run(f"drbdadm primary {resource}", timeout=30)
    if rc != 0:
        log.error(f"Failed to promote {resource}: {out}")
        return False

    out, rc = run(f"virsh start {vm_name}", timeout=30)
    if rc != 0:
        log.error(f"Failed to start {vm_name}: {out}")
        return False

    log.warning(f"TAKEOVER COMPLETE: {vm_name} running on this node")
    return True


def main():
    parser = argparse.ArgumentParser(description="Bedrock HA failover")
    parser.add_argument("--node", required=True, help="This node's name (node1 or node2)")
    parser.add_argument("--peer", required=True, help="Peer node's name")
    parser.add_argument("--dry-run", action="store_true", help="Log actions but don't execute")
    args = parser.parse_args()

    node = args.node
    peer = args.peer
    dry_run = args.dry_run

    log.info(f"Starting bedrock-failover on {node} (peer={peer}, witness={WITNESS_URL})")
    log.info(f"Peer addresses: {PEER_ADDRS.get(peer, [])}")
    log.info(f"Quorum: self + (peer via direct/switch OR witness) = 2 of 3")
    if dry_run:
        log.info("DRY RUN MODE - no actions will be taken")

    peer_dead_count = 0
    last_heartbeat = 0
    takeover_done = False

    while True:
        now = time.time()

        # Send heartbeat every HEARTBEAT_INTERVAL
        if now - last_heartbeat >= HEARTBEAT_INTERVAL:
            send_heartbeat(node)
            last_heartbeat = now

        # === QUORUM CHECK ===
        # Vote 1: self (always yes)
        # Vote 2: can we reach peer directly? (either path)
        peer_reachable, peer_via = check_peer_direct(peer)
        # Vote 3: can we reach witness? what does it say?
        witness_says_dead = check_witness_says_peer_dead(peer)
        witness_reachable = witness_says_dead is not None

        if peer_reachable:
            # Peer is alive — we have quorum (self + peer = 2/3), all is well
            if peer_dead_count > 0:
                log.info(f"Peer {peer} is reachable again via {peer_via} (was gone for {peer_dead_count} checks)")
            peer_dead_count = 0
            takeover_done = False

        elif witness_reachable and witness_says_dead:
            # Peer unreachable on all paths AND witness confirms dead
            # Quorum: self + witness = 2/3
            if not takeover_done:
                peer_dead_count += 1
                log.warning(
                    f"Peer {peer} UNREACHABLE (direct+switch) and DEAD per witness "
                    f"({peer_dead_count}/{DEAD_THRESHOLD})"
                )

                if peer_dead_count >= DEAD_THRESHOLD:
                    log.warning(f"QUORUM: self + witness = 2/3 — initiating takeover")
                    roles = get_local_drbd_roles()
                    running_vms = get_local_vms()

                    for resource, vm_name in RESOURCE_VM_MAP.items():
                        role = roles.get(resource, "Unknown")
                        if role == "Secondary" and vm_name not in running_vms:
                            log.warning(f"Resource {resource} is Secondary, VM {vm_name} not running — taking over")
                            if not dry_run:
                                takeover_resource(resource)
                            else:
                                log.info(f"DRY RUN: would promote {resource} and start {vm_name}")

                    takeover_done = True
                    peer_dead_count = 0

        elif witness_reachable and not witness_says_dead:
            # Peer unreachable from us but witness says it's alive
            # Possible: our network to peer is down but peer is fine
            # Do NOT take over — peer is running its VMs
            log.info(f"Peer {peer} unreachable from us but witness says alive — network issue, holding")
            peer_dead_count = 0

        else:
            # Can't reach peer AND can't reach witness
            # Only 1 vote (self) — no quorum, do NOTHING
            # This prevents split-brain: both nodes isolated = both freeze
            log.warning(f"ISOLATED: peer unreachable, witness unreachable — no quorum, holding position")
            peer_dead_count = 0

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
