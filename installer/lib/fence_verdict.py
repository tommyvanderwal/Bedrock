"""Fence-peer verdict bridge — the replacement for ensure_drbd_write_permission/resume-io.

THE MODEL (docs/drbd-fence-peer-arbiter-design.md). DRBD's fence-peer handler is the
synchronous "a Primary lost a peer — external arbiter, may I continue?" callout. On the
arbiter `cluster` resource (quorum all + on-no-quorum suspend-io + fencing resource-only),
when the Primary loses a peer DRBD spawns `bedrock-fence-peer` and *acts on its exit code*:
  exit 4 (P_OUTDATED) -> outdate the lost peer  => WE WIN, regain quorum, continue
  exit 6 (P_PRIMARY)  -> outdate myself         => WE LOSE, yield (stays frozen, never mints)
  exit 1 (broken)     -> "leave IO frozen"      => UNDECIDED, fail safe

The verdict is the SAME election netd already computes (election.compute -> leader/follower/
noquorum). The danger is acting on a verdict that is STALE — written by netd before netd
itself had detected the partition. DRBD detects a lost peer FAST (~3-6 s); netd's membership
is gated by DOWN_HYSTERESIS (~10 s). So at +3 s the file still says "leader" and is both
"fresh" (netd rewrote it ~1 s ago) and "stable" (leader for minutes) — yet WRONG. A naive
fresh+stable gate WINS on that stale value and a minority Primary mints a sibling -> split-brain.
(Empirically reproduced on the testbed, 2026-06-06.)

THE FIX — two gates that together prove netd has SEEN this partition and CONVERGED on it:
  netd records, each election tick: {outcome, updated, stable_since, reachable, self_octet}
  where `reachable` = the loopback last-octets netd currently reaches (INCLUDING self), and
  `stable_since` resets whenever the (outcome, reachable) tuple changes — not just the outcome.

  The handler, for the lost peer P (mapped to its loopback octet via the local DRBD config),
  returns a decision ONLY when:
    (a) FRESH       — updated within FRESH_S            (else netd wedged -> freeze)
    (b) P-EXCLUDED  — P's octet is NOT in `reachable`   (else netd has not yet seen THIS
                      partition -> wait; this is what closes the DRBD-vs-netd detection gap)
    (c) CONVERGED   — (outcome,reachable) stable >= STABLE_S (else the reachable set is still
                      shrinking through a transient -> a brief {self+2}=leader during a full
                      isolation must NOT decide WIN -> wait)
  then: outcome 'leader' -> win(4); else -> lose(6).  Anything else, up to a deadline -> freeze(1).

This module is the netd-side `record()` + the canonical `decide()` (also inlined, dependency-
free, into HANDLER_SCRIPT so the handler DRBD spawns needs no bedrock imports). stdlib only.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

VERDICT_FILE = Path("/run/bedrock/fence-verdict.json")

# netd ticks the election ~1 Hz; >FRESH_S stale means netd is wedged -> fail safe (freeze).
FRESH_S = 3.0
# Convergence guard: the (outcome, reachable) tuple must hold this long before we act, so a
# reachable set shrinking step-by-step during DOWN_HYSTERESIS settling (a transient
# {self+2}=leader on a node that is really isolated) never decides. Short because the
# P-excluded gate already covers the DRBD-vs-netd detection lag. Validated on the testbed.
STABLE_S = 3.0

# netd-side stability tracking (module state, lives in the netd process).
_last_key: tuple | None = None
_stable_since: float = 0.0


def record(outcome: str, *, reachable_octets=None, self_octet=None,
           now: float | None = None) -> None:
    """Called by netd each election tick with the current outcome + the set of loopback
    octets netd currently reaches (incl self). Resets stability when EITHER the outcome OR
    the reachable set changes, then atomically writes VERDICT_FILE. Never raises."""
    global _last_key, _stable_since
    if not outcome:
        return
    now = time.time() if now is None else now
    reach = sorted({o for o in (reachable_octets or ()) if o is not None})
    key = (outcome, tuple(reach))
    if key != _last_key:
        _last_key = key
        _stable_since = now
    try:
        VERDICT_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = VERDICT_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "outcome": outcome, "updated": now, "stable_since": _stable_since,
            "reachable": reach, "self_octet": self_octet,
        }))
        os.replace(tmp, VERDICT_FILE)
    except OSError:
        pass


def decide(peer_octet, *, now: float | None = None, fresh_s: float = FRESH_S,
           stable_s: float = STABLE_S, path: Path = VERDICT_FILE) -> str:
    """Canonical verdict from the recorded file for a lost peer whose loopback octet is
    `peer_octet`. Returns 'win' | 'lose' | 'undecided'.

    win       = peer excluded + converged + outcome 'leader'
    lose      = peer excluded + converged + outcome 'noquorum'/'follower'
    undecided = stale OR peer still reachable (netd hasn't seen this partition) OR not yet
                converged OR peer unresolved -> caller leaves IO frozen (DRBD's safe default).
    """
    now = time.time() if now is None else now
    try:
        v = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return "undecided"
    if now - float(v.get("updated", 0)) > fresh_s:
        return "undecided"                              # netd not ticking -> fail safe
    reach = v.get("reachable")
    if not isinstance(reach, list) or peer_octet is None:
        return "undecided"                              # no membership / unresolved peer -> freeze
    if peer_octet in reach:
        return "undecided"                              # netd still sees the peer -> hasn't seen the cut
    if now - float(v.get("stable_since", now)) < stable_s:
        return "undecided"                              # reachable set still converging -> wait
    o = (v.get("outcome") or "").lower()
    if o == "leader":
        return "win"
    if o in ("noquorum", "follower"):
        return "lose"
    return "undecided"


# The handler DRBD spawns. Self-contained (no bedrock imports): it maps the lost peer's
# DRBD node-id -> loopback octet from the local DRBD config, then applies the same
# fresh + peer-excluded + converged gate as decide(), polling until convergence or a deadline.
HANDLER_SCRIPT = r'''#!/usr/bin/env python3
"""bedrock-fence-peer — DRBD fence-peer handler for the arbiter `cluster` resource.

DRBD spawns this when the arbiter Primary loses a peer (fencing resource-only). It returns the
bedrock-d election verdict as a fence exit code, but ONLY once netd has itself SEEN this same
partition and converged on it — never on a verdict that merely predates the cut:
  4 = peer outdated (WIN, continue)   6 = outdate self (LOSE, yield)   1 = undecided (freeze)

Gate (see lib/fence_verdict.py): netd publishes {outcome, updated, stable_since, reachable}
where `reachable` = loopback octets netd currently reaches (incl self). We act only when
(a) fresh, (b) the LOST peer's octet is ABSENT from `reachable` (proof netd saw this partition —
closes the ~7 s gap between DRBD's fast peer-loss detection and netd's hysteresis), and
(c) (outcome,reachable) stable >= STABLE_S (a converging transient must not decide). Else poll
to a deadline, then freeze. The peer node-id -> octet map comes from the LOCAL drbd config
(drbdadm dump: config-only, no kernel/netlink -> safe to call inside a fence callout).
"""
import json, os, re, subprocess, sys, time, syslog
from pathlib import Path

VERDICT_FILE = Path("/run/bedrock/fence-verdict.json")
ARBITER_RES = "cluster"
FRESH_S, STABLE_S, POLL_S, DEADLINE_S = 3.0, 3.0, 0.5, 25.0

res = os.environ.get("DRBD_RESOURCE", "?")
peer_id = os.environ.get("DRBD_PEER_NODE_ID", "?")
syslog.openlog("bedrock-fence-peer")


def peer_octet_for(node_id):
    """Map a DRBD peer node-id -> its loopback last-octet via the LOCAL drbd config.
    Each `on <host> { node-id N; address ipv4 100.83.252.X:port; }` (the on-block address
    is the loopback; it precedes the connection sections). Static, no rqlite."""
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


def decide(now, peer_octet):
    try:
        v = json.loads(VERDICT_FILE.read_text())
    except (OSError, ValueError):
        return "undecided"
    if now - float(v.get("updated", 0)) > FRESH_S:
        return "undecided"
    reach = v.get("reachable")
    if not isinstance(reach, list) or peer_octet is None:
        return "undecided"
    if peer_octet in reach:
        return "undecided"                  # netd hasn't seen THIS partition yet
    if now - float(v.get("stable_since", now)) < STABLE_S:
        return "undecided"                  # reachable set still converging
    o = (v.get("outcome") or "").lower()
    if o == "leader":
        return "win"
    if o in ("noquorum", "follower"):
        return "lose"
    return "undecided"


if res != ARBITER_RES:
    syslog.syslog(syslog.LOG_ERR, "fence-peer on unexpected res=%s peer=%s -> exit 1" % (res, peer_id))
    sys.exit(1)

peer_octet = peer_octet_for(peer_id)
syslog.syslog("fence-peer res=%s peer=%s (octet=%s): deciding" % (res, peer_id, peer_octet))
deadline = time.time() + DEADLINE_S
while True:
    now = time.time()
    d = decide(now, peer_octet)
    if d == "win":
        syslog.syslog("fence-peer res=%s peer=%s -> WIN (exit 4, outdate peer)" % (res, peer_id))
        sys.exit(4)
    if d == "lose":
        syslog.syslog("fence-peer res=%s peer=%s -> LOSE (exit 6, outdate self)" % (res, peer_id))
        sys.exit(6)
    if now >= deadline:
        syslog.syslog(syslog.LOG_WARNING,
                      "fence-peer res=%s peer=%s -> UNDECIDED (exit 1, leave IO frozen)" % (res, peer_id))
        sys.exit(1)
    time.sleep(POLL_S)
'''


HANDLER_PATH = "/usr/local/lib/bedrock/bedrock-fence-peer"


def deploy_handler(path: str = HANDLER_PATH) -> None:
    """Write the self-contained handler script + make it executable. Idempotent."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not (p.exists() and p.read_text() == HANDLER_SCRIPT):
        tmp = p.with_suffix(".tmp")
        tmp.write_text(HANDLER_SCRIPT)
        os.chmod(tmp, 0o755)
        os.replace(tmp, p)
