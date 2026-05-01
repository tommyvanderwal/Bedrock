#!/usr/bin/env python3
"""End-to-end Phase 6 demo for the cluster-protocol v1.

Spins up:
  - the dev Echo witness on UDP 12321
  - bedrock-rust daemon "node1" (leader) on TCP 8201, IPC at /tmp/n1.sock
  - bedrock-rust daemon "node2" (follower) on TCP 8202, IPC at /tmp/n2.sock,
    pointed at node1 as its replication source

Then drives the cluster *entirely through log entries* by appending
typed payloads to node1's log via Python IPC. After each transition
both sides' view-builder regenerates cluster.json/state.json from the
log alone, demonstrating that the L28 / L30 / L27 workarounds are
unnecessary when the log is the source of truth.

Run from the repo root with:
    python3 dev-witness/demo_two_node.py
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
import uuid as uuidlib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "installer"))

from lib import log_entries as le      # noqa: E402
from lib import rust_ipc               # noqa: E402
from lib import view_builder            # noqa: E402

BIN = REPO / "rust" / "target" / "release" / "bedrock-rust"
N1_LOG = Path("/tmp/bedrock-demo/n1/log")
N2_LOG = Path("/tmp/bedrock-demo/n2/log")
N1_VIEW_DIR = Path("/tmp/bedrock-demo/n1/view")
N2_VIEW_DIR = Path("/tmp/bedrock-demo/n2/view")
N1_SOCK = "/tmp/bedrock-demo/n1.sock"
N2_SOCK = "/tmp/bedrock-demo/n2.sock"

WITNESS_KEY_DIR = Path("/var/lib/bedrock-witness-dev")


def banner(s: str) -> None:
    print(f"\n=== {s} ===")


def reset_dirs() -> None:
    base = Path("/tmp/bedrock-demo")
    if base.exists():
        shutil.rmtree(base)
    for p in (N1_LOG, N2_LOG, N1_VIEW_DIR, N2_VIEW_DIR):
        p.mkdir(parents=True)


def init_log(log_dir: Path, cluster_uuid: str) -> None:
    subprocess.check_call([
        str(BIN), "--log-dir", str(log_dir),
        "log", "init", "--cluster-uuid", cluster_uuid,
    ])


def start_witness() -> subprocess.Popen:
    runner = REPO / "dev-witness" / "run.py"
    p = subprocess.Popen(
        ["python3", str(runner)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True,
    )
    # Wait for the witness to print its pubkey and bind.
    pub = None
    for _ in range(50):
        line = p.stdout.readline()
        if not line:
            time.sleep(0.1)
            continue
        print(f"  [witness] {line.rstrip()}")
        if line.startswith("witness_pubkey (hex):"):
            pub = line.split(":", 1)[1].strip()
        if "running on UDP" in line:
            break
    if not pub:
        raise RuntimeError("witness did not print pubkey")
    return p, pub


def start_daemon(*, log_dir: Path, sock: str, peer_listen: str,
                 connect_to: str | None, role: str,
                 cluster_key_hex: str, witness_pub_hex: str,
                 sender_id: int) -> subprocess.Popen:
    cmd = [
        str(BIN),
        "--log-dir", str(log_dir),
        "--ipc-sock", sock,
        "daemon",
        "--peer-listen", peer_listen,
        "--role", role,
        "--cluster-key", cluster_key_hex,
        "--witness-host", "127.0.0.1",
        "--witness-port", "12321",
        "--witness-pubkey", witness_pub_hex,
        "--sender-id", str(sender_id),
        "--heartbeat-ms", "1500",
        "--lease-ttl-ms", "10000",
    ]
    if connect_to:
        cmd += ["--peer", connect_to]
    env = os.environ.copy()
    env["RUST_LOG"] = "info"
    return subprocess.Popen(cmd, env=env)


def wait_for_socket(path: str, timeout_s: float = 5.0) -> None:
    start = time.time()
    while time.time() - start < timeout_s:
        if Path(path).exists():
            return
        time.sleep(0.1)
    raise RuntimeError(f"socket {path} did not appear")


def main() -> None:
    if not BIN.exists():
        raise SystemExit(f"build the daemon first: cargo build --release  ({BIN})")

    reset_dirs()
    cluster_uuid = str(uuidlib.uuid4())

    banner("init both logs with the same cluster UUID")
    init_log(N1_LOG, cluster_uuid)
    init_log(N2_LOG, cluster_uuid)

    banner("start the dev witness")
    witness_proc, witness_pub = start_witness()

    banner("start node-1 (leader) and node-2 (follower)")
    cluster_key = os.urandom(32).hex()
    print(f"  cluster_key = {cluster_key[:12]}…")
    print(f"  witness_pub = {witness_pub[:12]}…")

    n1 = start_daemon(
        log_dir=N1_LOG, sock=N1_SOCK,
        peer_listen="127.0.0.1:8201", connect_to=None,
        role="leader",
        cluster_key_hex=cluster_key, witness_pub_hex=witness_pub,
        sender_id=1,
    )
    wait_for_socket(N1_SOCK)
    n2 = start_daemon(
        log_dir=N2_LOG, sock=N2_SOCK,
        peer_listen="127.0.0.1:8202", connect_to="127.0.0.1:8201",
        role="follower",
        cluster_key_hex=cluster_key, witness_pub_hex=witness_pub,
        sender_id=2,
    )
    wait_for_socket(N2_SOCK)
    time.sleep(1.5)  # let peer link establish

    try:
        banner("via IPC: node-1 appends typed entries describing a 1→2 cluster")
        with rust_ipc.Daemon(N1_SOCK) as d1:
            for payload, label in [
                (le.cluster_init("bedrock-demo", cluster_uuid),  "cluster_init"),
                (le.node_register("node-1", "127.0.0.1", "10.99.0.10",
                                  role="mgmt+compute"),         "node_register node-1"),
                (le.mgmt_master("node-1"),                       "mgmt_master = node-1"),
                (le.tier_state("scratch", "local",
                               backend_path="/var/lib/bedrock/local/scratch"),
                                                                 "tier scratch=local"),
                (le.tier_state("bulk",    "local",
                               backend_path="/var/lib/bedrock/local/bulk"),
                                                                 "tier bulk=local"),
                (le.tier_state("critical", "local",
                               backend_path="/var/lib/bedrock/local/critical"),
                                                                 "tier critical=local"),
            ]:
                idx, h = d1.append(payload)
                print(f"  appended idx={idx} hash={h.hex()[:12]} — {label}")

            banner("scale to 2 nodes (entries describing the join + promote)")
            for payload, label in [
                (le.node_register("node-2", "127.0.0.1", "10.99.0.11",
                                  role="compute"),               "node_register node-2"),
                (le.drbd_node_id_assigned("bulk", "node-1", 0),  "node-1 = bulk id 0"),
                (le.drbd_node_id_assigned("bulk", "node-2", 1),  "node-2 = bulk id 1"),
                (le.drbd_node_id_assigned("critical", "node-1", 0), "node-1 = critical id 0"),
                (le.drbd_node_id_assigned("critical", "node-2", 1), "node-2 = critical id 1"),
                (le.tier_state("bulk", "drbd-nfs", master="node-1",
                               peers=["node-1", "node-2"]),     "tier bulk=drbd-nfs"),
                (le.tier_state("critical", "drbd-nfs", master="node-1",
                               peers=["node-1", "node-2"]),     "tier critical=drbd-nfs"),
                (le.tier_state("scratch", "garage", master=None,
                               peers=["node-1", "node-2"],
                               garage_endpoint="http://10.99.0.10:3900"),
                                                                 "tier scratch=garage"),
            ]:
                idx, h = d1.append(payload)
                print(f"  appended idx={idx} hash={h.hex()[:12]} — {label}")

            banner("transfer-mgmt from node-1 to node-2 — single log entry")
            idx, h = d1.append(le.mgmt_master("node-2"))
            print(f"  appended idx={idx} hash={h.hex()[:12]} — mgmt_master = node-2")

            status1 = d1.status()
            print(f"\nnode-1 log latest: index={status1['latest_index']} hash={status1['latest_hash'].hex()[:12]}…")

        # Wait for node-2 to catch up via TCP replication.
        time.sleep(1.5)
        with rust_ipc.Daemon(N2_SOCK) as d2:
            status2 = d2.status()
            print(f"node-2 log latest: index={status2['latest_index']} hash={status2['latest_hash'].hex()[:12]}…")
            verified = d2.verify()
            print(f"node-2 log verify: {verified} entries, hash chain intact")

        if status1["latest_index"] != status2["latest_index"]:
            raise SystemExit(
                f"FAIL: replication didn't catch up "
                f"(node-1 idx={status1['latest_index']} node-2 idx={status2['latest_index']})"
            )
        if status1["latest_hash"] != status2["latest_hash"]:
            raise SystemExit(
                f"FAIL: node-1 hash {status1['latest_hash'].hex()} ≠ node-2 {status2['latest_hash'].hex()}"
            )
        print("✓ both nodes hold the same log tail (index + hash match)")

        banner("rebuild materialised views on both nodes")
        v1 = view_builder.rebuild(
            sock_path=N1_SOCK,
            cluster_json=N1_VIEW_DIR / "cluster.json",
            state_json=N1_VIEW_DIR / "state.json",
            this_node="node-1",
        )
        v2 = view_builder.rebuild(
            sock_path=N2_SOCK,
            cluster_json=N2_VIEW_DIR / "cluster.json",
            state_json=N2_VIEW_DIR / "state.json",
            this_node="node-2",
        )
        # Both cluster.json files must be byte-identical.
        c1 = (N1_VIEW_DIR / "cluster.json").read_text()
        c2 = (N2_VIEW_DIR / "cluster.json").read_text()
        if c1 != c2:
            raise SystemExit("FAIL: cluster.json differs between nodes")
        print("✓ cluster.json byte-identical on both nodes")
        # And the per-node state.json must match each node's POV.
        s1 = (N1_VIEW_DIR / "state.json").read_text()
        s2 = (N2_VIEW_DIR / "state.json").read_text()
        print("--- node-1 state.json ---")
        print(s1)
        print("--- node-2 state.json ---")
        print(s2)

        # Verify the L28 fix is structural now: both sides see the
        # post-transfer mgmt_master = node-2 without any out-of-band
        # rsync of cluster.json.
        import json as _json
        c = _json.loads(c1)
        master = None
        for n_name, info in c["nodes"].items():
            if info.get("role") == "mgmt+compute":
                master = n_name
        print(f"\nmgmt master per cluster.json: {master}  (last log entry was mgmt_master=node-2)")
        if master != "node-2":
            raise SystemExit("FAIL: mgmt_master not folded correctly into cluster.json")
        print("✓ L28 obsoleted: mgmt-master change is one log entry, propagates by replication, both nodes' cluster.json regenerate identically")

        # And the L27 / drbd_node_id concern: cluster.json carries the
        # canonical id assignment; any new peer learns ids from the log,
        # not from local fresh-allocation order.
        ids = c["tiers"]["bulk"].get("drbd_node_ids")
        print(f"drbd_node_ids[bulk] from log: {ids}")
        if ids != {"node-1": 0, "node-2": 1}:
            raise SystemExit("FAIL: drbd_node_ids not folded correctly")
        print("✓ L27 obsoleted: DRBD node-ids are log entries; both sides see the same map")

        banner("DONE — all phases work end-to-end")
    finally:
        for p in (n2, n1, witness_proc):
            try:
                p.send_signal(signal.SIGTERM)
                p.wait(timeout=2)
            except Exception:
                p.kill()


if __name__ == "__main__":
    main()
