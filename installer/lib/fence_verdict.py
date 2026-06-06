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

import time

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


# The handler DRBD spawns. Self-contained (no bedrock imports): maps the lost peer's DRBD
# node-id -> loopback octet from the local drbd config, then makes ONE blocking HTTP call to
# bedrock-d's loopback fence-decision endpoint. Any error/timeout -> exit 1 (freeze, safe).
HANDLER_SCRIPT = r'''#!/usr/bin/env python3
"""bedrock-fence-peer — DRBD fence-peer handler (synchronous witness-claim model).

DRBD spawns this when the arbiter Primary loses a peer (fencing resource-only). It POSTs to
bedrock-d's :8001 HTTP-loopback /internal/fence-decision, which feeds DRBD's authoritative
peer-loss into netd's election, lets netd converge + drive the exclusive witness claim, and
returns the verdict:  4 = WIN (outdate peer, continue)  6 = LOSE (outdate self, yield)
1 = undecided (leave IO frozen). Self-contained (no bedrock imports): maps peer node-id ->
loopback octet from the local drbd config (drbdadm dump = config-only, no kernel/netlink ->
safe inside a fence callout), then one blocking HTTP call. Any error/timeout -> exit 1.
"""
import json, os, re, subprocess, sys, syslog, urllib.request

ARBITER_RES = "cluster"
ENDPOINT = "http://127.0.0.1:8001/internal/fence-decision"
HTTP_TIMEOUT_S = 25.0

res = os.environ.get("DRBD_RESOURCE", "?")
peer_id = os.environ.get("DRBD_PEER_NODE_ID", "?")
syslog.openlog("bedrock-fence-peer")


def peer_octet_for(node_id):
    """Map a DRBD peer node-id -> its loopback last-octet via the LOCAL drbd config.
    Each `on <host> { node-id N; address ipv4 100.83.252.X:port; }` (on-block address is
    the loopback; it precedes the connection sections). Static, no rqlite, no netlink."""
    try:
        out = subprocess.run(["drbdadm", "dump", ARBITER_RES],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    pending = None
    for line in out.splitlines():
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


# Only the arbiter resource is wired to this handler; refuse anything else -> leave frozen.
# (VM-disk fence arbitration via rqlite ownership is a separate handler — see P3.)
if res != ARBITER_RES:
    syslog.syslog(syslog.LOG_ERR, "fence-peer on unexpected res=%s peer=%s -> exit 1" % (res, peer_id))
    sys.exit(1)

octet = peer_octet_for(peer_id)
if octet is None:
    syslog.syslog(syslog.LOG_ERR,
                  "fence-peer res=%s peer=%s: cannot map node-id -> octet -> exit 1" % (res, peer_id))
    sys.exit(1)

syslog.syslog("fence-peer res=%s peer=%s (octet=%s): asking bedrock-d" % (res, peer_id, octet))
body = json.dumps({"resource": res, "peer_octet": octet}).encode()
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
