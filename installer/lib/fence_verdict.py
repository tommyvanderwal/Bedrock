"""Fence-peer verdict bridge — SYNCHRONOUS witness-claim model (replaces the verdict file).

THE MODEL (docs/drbd-fence-peer-arbiter-design.md). A DRBD fence-peer callout is a
synchronous ACT, not a file lookup. On peer loss DRBD spawns `bedrock-fence-peer` (the
Primary frozen, DRBD waiting for the exit code). The handler POSTs to bedrock-d's
:8001 HTTP-loopback `/internal/fence-decision {resource, peer_octet}`. bedrock-d then:

  1. Feeds DRBD's AUTHORITATIVE "peer down" evidence into netd's election
     (shared_state.drbd_down_peers) — DRBD detects a lost peer in ~3-6 s, mesh liveness
     only after DOWN_HYSTERESIS (~10 s), so this collapses the detection lag.
  2. Lets netd converge the election on the real partition AND drive the EXCLUSIVE witness
     claim (ensure_witness_claim) — the claim is what makes an even split safe (only one
     side reserves the witness vote; two stale files could both read "leader" -> split-brain).
  3. Returns the converged verdict; handler -> exit 4 (WIN, outdate peer, continue) /
     6 (LOSE, outdate self, yield) / 1 (undecided, freeze — DRBD's safe default).

Why a call and not a file (Tommy's correction): the file was netd's 1 Hz election outcome
materialised — downstream of the very event it arbitrates, so it lagged the partition (the
loser's file still said "leader" at +3 s -> minority Primary won -> split-brain, reproduced
on the testbed). And it could not perform the exclusive witness CLAIM, which must happen
*then and there* on the split. The call feeds DRBD's fast detection in and waits for the
real, evidence-accelerated arbitration. This is how DRBD's external fencing is meant to be
used (a synchronous actuating callout), like crm-fence-peer.

This module is the endpoint-side `feed_down()` + `decide_fence()` (called by mgmt's
/internal/fence-decision) and the self-contained HANDLER_SCRIPT DRBD spawns. stdlib only.
"""

from __future__ import annotations

import re
import time

# The cluster-singleton DRBD resource — the ONLY resource arbitrated via netd's election +
# witness claim (decide_fence). Everything else (vm-*) is arbitrated via rqlite ownership
# (decide_vm_fence). At module scope so mgmt's endpoint can branch on it.
ARBITER_RES = "cluster"

# A per-VM DRBD resource is named `vm-<vm_name>-disk<N>` (bedrock_d/vm/create.py). The vm_name
# is everything between the `vm-` prefix and the FINAL `-disk<digits>` (greedy middle so a
# vm_name that itself contains `-disk` still splits at the last one).
_VM_RES_RE = re.compile(r"^vm-(.+)-disk\d+$")

# The endpoint waits this long for netd to converge before giving up -> 'undecided' (freeze).
# Must be < the handler's HTTP timeout, which must be < DRBD's (unbounded) handler wait.
DECIDE_DEADLINE_S = 18.0
# netd's published (outcome, down_acked) must hold this long before we trust it: a
# simultaneously-isolated master's per-peer down-evidence arrives over a few election ticks,
# so we wait for the membership to settle (a transient must not decide). ~3 election ticks.
DECIDE_STABLE_S = 2.5
POLL_S = 0.4
# fence_view is stale (netd wedged) if older than this -> fail safe (freeze).
FRESH_S = 3.0


def feed_down(shared_state, peer_octet, *, now: float | None = None) -> None:
    """Record DRBD's authoritative 'peer <octet> is down' evidence for netd's election
    (shared_state.drbd_down_peers, monotonic ts). netd forces that peer's liveness False
    and expires the evidence. Best-effort; never raises."""
    if shared_state is None or peer_octet is None:
        return
    now = time.monotonic() if now is None else now
    try:
        with shared_state.netd_lock:
            shared_state.drbd_down_peers[int(peer_octet)] = now
    except (OSError, ValueError, TypeError):
        pass


def decide_fence(shared_state, peer_octet, *, deadline_s: float = DECIDE_DEADLINE_S,
                 stable_s: float = DECIDE_STABLE_S, poll_s: float = POLL_S) -> str:
    """Synchronous fence decision for an arbiter Primary that lost `peer_octet`.

    Feeds the down-evidence, then waits for netd's published `fence_view` to (a) be FRESH
    (netd ticking), (b) ACK the evidence (peer_octet in down_acked — netd's election has
    incorporated this loss), and (c) be STABLE (the converged partition view), then maps the
    outcome -> 'win' (leader) | 'lose' (follower/noquorum) | 'undecided' (deadline -> freeze).
    The exclusive witness claim is driven by netd's ensure_witness_claim, so a stable
    'leader' here already means the claim is confirmed if the split was witness-pivotal."""
    if shared_state is None or peer_octet is None:
        return "undecided"
    try:
        peer_octet = int(peer_octet)
    except (ValueError, TypeError):
        return "undecided"
    feed_down(shared_state, peer_octet)
    deadline = time.monotonic() + deadline_s
    while True:
        now = time.monotonic()
        feed_down(shared_state, peer_octet, now=now)   # keep evidence alive across the wait
        try:
            with shared_state.netd_lock:
                v = dict(shared_state.fence_view or {})
        except (OSError, RuntimeError):
            v = {}
        outcome = (v.get("outcome") or "").lower()
        fresh = (now - float(v.get("updated", 0.0))) <= FRESH_S
        acked = peer_octet in (v.get("down_acked") or [])
        stable = (now - float(v.get("stable_since", now))) >= stable_s
        if fresh and acked and stable and outcome:
            if outcome == "leader":
                return "win"
            if outcome in ("follower", "noquorum"):
                return "lose"
        if now >= deadline:
            return "undecided"
        time.sleep(poll_s)


def vm_name_for_resource(resource: str) -> str | None:
    """`vm-<name>-disk<N>` -> `<name>`; None for anything that isn't a per-VM disk."""
    m = _VM_RES_RE.match(resource or "")
    return m.group(1) if m else None


def _self_node_name() -> str:
    """This node's name from /etc/bedrock/state.json ("" on any error)."""
    try:
        import json
        from pathlib import Path
        return (json.loads(Path("/etc/bedrock/state.json").read_text()) or {}).get("node_name") or ""
    except Exception:
        return ""


def decide_vm_fence(resource: str, peer_node: str | None = None, *,
                    my_node: str | None = None) -> str:
    """Synchronous fence decision for a per-VM DRBD disk whose replication link to
    `peer_node` just dropped (this node is the frozen/promoting Primary).

    The authority is rqlite, NOT netd's election: a per-VM disk has no witness of its own, so
    "which side is in the cluster majority" is exactly "which DRBD peer can still satisfy a
    level='strong' read". Mirrors is_safe_to_start_vm / _vms_on_dead_peer so the DRBD-level
    gate and the orchestrator's takeover can never disagree about who runs the VM:

      * strong-read vms.host + failover_order. A node in the MINORITY partition cannot confirm
        the Raft leader -> the read RAISES -> 'undecided' (DRBD stays frozen, the safe default;
        the orchestrator suspends the VM and fails it over to the majority side). NO local
        fallback — a stale local replica could read an old host and split-brain.
      * host == me  -> 'win' (blessed home AND, since the strong read succeeded, in the
        majority -> outdate the lost peer, resume).
      * host == peer_node AND I'm next in failover_order after it -> 'win'. This is the
        sanctioned takeover: the fence-peer fires DURING takeover_after_peer_down_task's
        `drbdadm primary`, BEFORE it writes vms.host=me, so we recognise the successor by the
        lost-host identity + the predetermined order (peers_after_dead), exactly as the
        takeover task does. Without this the takeover would read the dead host, LOSE, and
        outdate itself — breaking every failover.
      * otherwise -> 'lose' (a stale Primary the cluster says no longer owns this VM -> yield).
    """
    vm_name = vm_name_for_resource(resource)
    if not vm_name:
        return "undecided"
    me = my_node or _self_node_name()
    if not me:
        return "undecided"
    try:
        from lib import rqlite_client
        from bedrock_d.vm.failover import peers_after_dead
    except ImportError:
        import sys as _sys
        _sys.path.insert(0, "/usr/local/lib/bedrock")
        from lib import rqlite_client  # type: ignore
        from bedrock_d.vm.failover import peers_after_dead  # type: ignore
    # STRONG read — fails loud in the minority partition (no leader confirmation), which IS the
    # "am I in the cluster majority?" gate. See the read-consistency-classes memory: a
    # DRBD-takeover decision is strict-leader, never flexible/local-fallback.
    try:
        with rqlite_client.RqliteClient() as rc:
            row = rc.query_one(
                "SELECT host, failover_order FROM vms WHERE vm_name = ?",
                params=[vm_name], level="strong",
            )
    except Exception:
        return "undecided"          # minority / rqlite unreachable -> freeze (safe)
    if row is None:
        return "undecided"          # unknown VM -> don't guess, stay frozen
    host = row.get("host") or ""
    if host == me:
        return "win"
    if peer_node and host == peer_node:
        import json
        try:
            order = json.loads(row.get("failover_order") or "[]")
        except (TypeError, ValueError):
            order = []
        if peers_after_dead(order, me, host):
            return "win"            # sanctioned takeover from the lost host
    return "lose"


# The handler DRBD spawns. Self-contained (no bedrock imports): maps the lost peer's DRBD
# node-id -> loopback octet from the local drbd config, then makes ONE blocking HTTP call to
# bedrock-d's loopback fence-decision endpoint. Any error/timeout -> exit 1 (freeze, safe).
HANDLER_SCRIPT = r'''#!/usr/bin/env python3
"""bedrock-fence-peer — DRBD fence-peer handler (synchronous decision model).

DRBD spawns this on a Primary peer-loss (fencing resource-only for the `cluster` singleton,
resource-and-stonith for per-VM disks) and waits, IO frozen, for the exit code. It POSTs to
bedrock-d's :8001 HTTP-loopback /internal/fence-decision, which arbitrates by resource class:
  * `cluster`  -> netd's election + the exclusive witness claim (peer mapped to loopback octet)
  * `vm-*`     -> rqlite ownership (peer mapped to node_name, compared with vms.host)
and returns the verdict:  4 = WIN (outdate peer, continue)  6 = LOSE (outdate self, yield)
1 = undecided (leave IO frozen). Self-contained (no bedrock imports): maps the peer node-id
from the local drbd config (drbdadm dump = config-only, no kernel/netlink -> safe inside a
fence callout), then one blocking HTTP call. Any error/timeout -> exit 1 (freeze, safe).
"""
import json, os, re, subprocess, sys, syslog, urllib.request

ARBITER_RES = "cluster"
ENDPOINT = "http://127.0.0.1:8001/internal/fence-decision"
HTTP_TIMEOUT_S = 25.0

res = os.environ.get("DRBD_RESOURCE", "?")
peer_id = os.environ.get("DRBD_PEER_NODE_ID", "?")
syslog.openlog("bedrock-fence-peer")


def _dump(resource):
    """`drbdadm dump <resource>` stdout (config-only, no kernel/netlink -> safe inside a
    fence callout); "" on any error. Normalised so each on-block carries both `node-id N`
    and its own `address ipv4 <loopback>:port`."""
    try:
        return subprocess.run(["drbdadm", "dump", resource],
                              capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def peer_octet_for(node_id, dump):
    """Map a DRBD peer node-id -> its loopback last-octet from the on-block address
    (`on <host> { node-id N; ... address ipv4 100.83.252.X:port; }`). The on-block address
    follows its node-id and precedes the connection sections, so the node-id->address pairing
    is unambiguous."""
    pending = None
    for line in dump.splitlines():
        m = re.search(r"node-id\s+(\d+)", line)
        if m:
            pending = m.group(1)
            continue
        m = re.search(r"address\s+ipv4\s+\d+\.\d+\.\d+\.(\d+):", line)
        if m and pending is not None:
            if pending == str(node_id):
                return int(m.group(1))
            pending = None
    return None


def peer_name_for(node_id, dump):
    """Map a DRBD peer node-id -> its node_name from the on-block header
    (`on <node_name> { node-id N; ... }`). Used by the per-VM path so bedrock-d can compare
    the lost peer against vms.host. node-id appears ONLY in on-blocks, so the open `on` name
    is the owner."""
    pending = None
    for line in dump.splitlines():
        m = re.match(r"\s*on\s+(\S+)\s*\{", line)
        if m:
            pending = m.group(1)
            continue
        m = re.search(r"node-id\s+(\d+)", line)
        if m and pending is not None and m.group(1) == str(node_id):
            return pending
    return None


# Route by resource class. The `cluster` singleton -> netd-election arbitration (peer octet);
# a per-VM disk (`vm-*`) -> rqlite-ownership arbitration (peer node_name). Anything else is
# not wired to a decision -> leave IO frozen (safe).
if res == ARBITER_RES:
    octet = peer_octet_for(peer_id, _dump(res))
    if octet is None:
        syslog.syslog(syslog.LOG_ERR,
                      "fence-peer res=%s peer=%s: cannot map node-id -> octet -> exit 1" % (res, peer_id))
        sys.exit(1)
    payload = {"resource": res, "peer_octet": octet}
    syslog.syslog("fence-peer res=%s peer=%s (octet=%s): asking bedrock-d" % (res, peer_id, octet))
elif res.startswith("vm-"):
    # peer_node lets bedrock-d recognise the sanctioned takeover (vms.host still == the lost
    # host). "" still decides the steady-state host==me case; the takeover case then freezes
    # (safe) rather than mis-promoting.
    peer_name = peer_name_for(peer_id, _dump(res)) or ""
    payload = {"resource": res, "peer_node": peer_name}
    syslog.syslog("fence-peer res=%s peer=%s (node=%s): asking bedrock-d" % (res, peer_id, peer_name or "?"))
else:
    syslog.syslog(syslog.LOG_ERR, "fence-peer on unexpected res=%s peer=%s -> exit 1" % (res, peer_id))
    sys.exit(1)

body = json.dumps(payload).encode()
req = urllib.request.Request(ENDPOINT, data=body,
                             headers={"Content-Type": "application/json"}, method="POST")
try:
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as r:
        verdict = (json.loads(r.read().decode()).get("verdict") or "").lower()
except Exception as e:
    syslog.syslog(syslog.LOG_WARNING,
                  "fence-peer res=%s peer=%s: call failed (%r) -> exit 1 (freeze)" % (res, peer_id, e))
    sys.exit(1)

if verdict == "win":
    syslog.syslog("fence-peer res=%s peer=%s -> WIN (exit 4, outdate peer)" % (res, peer_id))
    sys.exit(4)
if verdict == "lose":
    syslog.syslog("fence-peer res=%s peer=%s -> LOSE (exit 6, outdate self)" % (res, peer_id))
    sys.exit(6)
syslog.syslog(syslog.LOG_WARNING,
              "fence-peer res=%s peer=%s -> %s (exit 1, freeze)" % (res, peer_id, verdict or "undecided"))
sys.exit(1)
'''


HANDLER_PATH = "/usr/local/lib/bedrock/bedrock-fence-peer"


def deploy_handler(path: str = HANDLER_PATH) -> None:
    """Write the self-contained handler script + make it executable. Idempotent."""
    import os
    from pathlib import Path
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not (p.exists() and p.read_text() == HANDLER_SCRIPT):
        tmp = p.with_suffix(".tmp")
        tmp.write_text(HANDLER_SCRIPT)
        os.chmod(tmp, 0o755)
        os.replace(tmp, p)
