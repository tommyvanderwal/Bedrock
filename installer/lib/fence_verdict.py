"""Fence-peer verdict bridge — the replacement for ensure_drbd_write_permission/resume-io.

THE MODEL (docs/drbd-fence-peer-arbiter-design.md). DRBD's fence-peer handler is the
synchronous "a Primary lost a peer — external arbiter, may I continue?" callout. On the
arbiter `cluster` resource (quorum all + on-no-quorum suspend-io + fencing resource-only),
when the Primary loses a peer DRBD spawns `bedrock-fence-peer` and *acts on its exit code*:
  exit 4 (P_OUTDATED) -> outdate the lost peer  => WE WIN, regain quorum, continue
  exit 6 (P_PRIMARY)  -> outdate myself         => WE LOSE, yield (stays frozen, never mints)
  exit 1 (broken)     -> "leave IO frozen"      => UNDECIDED, fail safe

The verdict is the SAME election netd already computes (election.compute -> leader/follower/
noquorum). The original resume-io bug was acting on a *stale, unconfirmed* cached outcome every
converge tick. The fix here is **fresh + stable or freeze**:
  * netd `record()`s the outcome each election tick with a wall-clock `updated` stamp and a
    `stable_since` stamp (reset whenever the outcome changes).
  * the handler `decide()`s only on an outcome that is (a) FRESH — netd wrote it within
    `FRESH_S` (else netd is wedged -> freeze) — and (b) STABLE — unchanged for at least
    `STABLE_S` (election convergence; a transient denominator/flap must not decide).
  * anything else -> undecided -> the handler leaves IO frozen (DRBD's safe default).

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
# The outcome must hold this long before we act on it — covers election convergence and a
# transient flap (e.g. the level='none' denominator shrink). Tunable; validated on the testbed.
STABLE_S = 8.0

# netd-side stability tracking (module state, lives in the netd process).
_last_outcome: str | None = None
_stable_since: float = 0.0


def record(outcome: str, *, now: float | None = None) -> None:
    """Called by netd each election tick with the current outcome (leader/follower/noquorum).
    Tracks stability and atomically writes VERDICT_FILE. Never raises (best-effort)."""
    global _last_outcome, _stable_since
    if not outcome:
        return
    now = time.time() if now is None else now
    if outcome != _last_outcome:
        _last_outcome = outcome
        _stable_since = now
    try:
        VERDICT_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = VERDICT_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(
            {"outcome": outcome, "updated": now, "stable_since": _stable_since}))
        os.replace(tmp, VERDICT_FILE)
    except OSError:
        pass


def decide(*, now: float | None = None, fresh_s: float = FRESH_S,
           stable_s: float = STABLE_S, path: Path = VERDICT_FILE) -> str:
    """Canonical verdict from the recorded file. Returns 'win' | 'lose' | 'undecided'.

    win       = outcome 'leader',  fresh, stable >= stable_s
    lose      = outcome 'noquorum'/'follower', fresh, stable >= stable_s
    undecided = stale (netd wedged) OR not yet stable OR unknown -> caller leaves IO frozen.
    """
    now = time.time() if now is None else now
    try:
        v = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return "undecided"
    if now - float(v.get("updated", 0)) > fresh_s:
        return "undecided"                              # netd not ticking -> fail safe
    if now - float(v.get("stable_since", now)) < stable_s:
        return "undecided"                              # not converged yet -> wait/freeze
    o = (v.get("outcome") or "").lower()
    if o == "leader":
        return "win"
    if o in ("noquorum", "follower"):
        return "lose"
    return "undecided"


# The handler DRBD spawns. Self-contained (no bedrock imports): it inlines the same
# fresh+stable decision as decide() and polls until the outcome converges or a deadline.
HANDLER_SCRIPT = r'''#!/usr/bin/env python3
"""bedrock-fence-peer — DRBD fence-peer handler for the arbiter `cluster` resource.

Spawned by drbdadm when the arbiter Primary loses a peer (fencing resource-only). Returns the
bedrock-d election verdict as a DRBD fence exit code, acting ONLY on a fresh+stable outcome:
  4 = peer outdated (WE WIN, continue)   6 = outdate self (WE LOSE, yield)   1 = undecided (freeze)
Polls up to a deadline so a just-forming partition has time to converge; fails safe to 1.
"""
import json, os, sys, time, syslog
from pathlib import Path

VERDICT_FILE = Path("/run/bedrock/fence-verdict.json")
ARBITER_RES = "cluster"
FRESH_S, STABLE_S, POLL_S = 3.0, 8.0, 0.5
DEADLINE_S = STABLE_S + 6.0     # poll past the stable window before giving up -> freeze

res = os.environ.get("DRBD_RESOURCE", "?")
peer = os.environ.get("DRBD_PEER_NODE_ID", "?")
syslog.openlog("bedrock-fence-peer")

def _decide(now):
    try:
        v = json.loads(VERDICT_FILE.read_text())
    except (OSError, ValueError):
        return "undecided"
    if now - float(v.get("updated", 0)) > FRESH_S:
        return "undecided"
    if now - float(v.get("stable_since", now)) < STABLE_S:
        return "undecided"
    o = (v.get("outcome") or "").lower()
    if o == "leader":
        return "win"
    if o in ("noquorum", "follower"):
        return "lose"
    return "undecided"

# Only the arbiter resource is wired to this handler; refuse anything else (-> leave frozen).
if res != ARBITER_RES:
    syslog.syslog(syslog.LOG_ERR, "fence-peer on unexpected res=%s peer=%s -> exit 1" % (res, peer))
    sys.exit(1)

syslog.syslog("fence-peer res=%s peer=%s: deciding" % (res, peer))
deadline = time.time() + DEADLINE_S
while True:
    now = time.time()
    d = _decide(now)
    if d == "win":
        syslog.syslog("fence-peer res=%s peer=%s -> WIN (exit 4, outdate peer)" % (res, peer))
        sys.exit(4)
    if d == "lose":
        syslog.syslog("fence-peer res=%s peer=%s -> LOSE (exit 6, outdate self)" % (res, peer))
        sys.exit(6)
    if now >= deadline:
        syslog.syslog(syslog.LOG_WARNING,
                      "fence-peer res=%s peer=%s -> UNDECIDED (exit 1, leave IO frozen)" % (res, peer))
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
