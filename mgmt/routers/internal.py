"""Internal loopback endpoints — the CDC convergence fast-path and the synchronous DRBD
fence-peer arbitration. All are reached only from 127.0.0.1 (the local rqlited / the
bedrock-fence-peer handler / the local CLI); none are LAN-exposed.

No router prefix: the three paths don't share one (`/api/internal/*` + `/internal/*`)."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from dependencies import require_peer

router = APIRouter(tags=["internal"])


# CDC fast-path receiver. The leader, on an applied rqlite commit, fans this
# out to every node so the central loop converges near-instantly instead of
# waiting for its poll floor. Idempotent + cheap: it only nudges the loop to
# re-read; the loop still does the authoritative master-first read itself.
@router.post("/api/internal/check-now")
def internal_check_now(node: str = Depends(require_peer)):
    from mgmt import orchestrator as _orch
    return {"woke": _orch.signal_check_now()}


# CDC source. The local rqlited — only when it is the Raft leader — POSTs an
# applied-commit event here (loopback only; rqlited dials 127.0.0.1). We don't
# need the payload: any committed change means "converge now". We wake our own
# loop, then fan the nudge out to every other node so the whole cluster
# converges near-instantly instead of each waiting out its poll floor. The
# fan-out runs in a thread (blocking signed HTTP) and is best-effort — the poll
# floor backstops any peer it misses.
@router.post("/api/internal/cdc")
async def internal_cdc(request: Request):
    ch = request.client.host if request.client else ""
    if ch not in ("127.0.0.1", "::1"):
        raise HTTPException(403, "cdc endpoint is loopback-only")
    await request.body()                      # drain so rqlited gets its 200
    from mgmt import orchestrator as _orch
    _orch.signal_check_now()                  # our own loop
    asyncio.create_task(asyncio.to_thread(_orch.fanout_check_now_blocking))
    return {"ok": True}


# ── DRBD fence-peer arbitration (synchronous, loopback-only) ─────────

class FenceDecisionRequest(BaseModel):
    resource: str
    # Cluster-singleton path: the lost peer's loopback last-octet (fed into netd's
    # election). Per-VM path leaves it -1 and uses peer_node instead.
    peer_octet: int = -1
    # Per-VM path: the lost peer's node_name (to recognise the sanctioned takeover
    # when vms.host still points at the lost host). Empty on the cluster path.
    peer_node: str = ""


@router.post("/internal/fence-decision")
def internal_fence_decision(req: FenceDecisionRequest, request: Request):
    """Synchronous DRBD fence-peer arbitration. `bedrock-fence-peer` (spawned by DRBD on a
    Primary peer-loss) POSTs here. SYNC handler (def, not async): it runs in FastAPI's
    threadpool, so the up-to-~18 s block never stalls the asyncio event loop. Two resource
    classes, two authorities (see lib/fence_verdict.py + docs/drbd-fence-peer-arbiter-design.md):

      * the `cluster` singleton -> decide_fence(): feed DRBD's AUTHORITATIVE per-peer "down"
        evidence into netd's election (collapsing the ~10 s mesh-hysteresis lag), let netd
        converge + drive the EXCLUSIVE witness claim, return the verdict. (Replaces the racy
        /run/bedrock/fence-verdict.json file.)
      * a per-VM disk (`vm-*`) -> decide_vm_fence(): a per-VM DRBD has no witness of its own,
        so the authority is a level='strong' rqlite read of vms.host (which doubles as the
        'am I in the cluster majority?' gate — it fails in the minority) + failover_order.

    Both map win/lose/undecided -> handler exit 4/6/1."""
    ch = request.client.host if request.client else ""
    if ch not in ("127.0.0.1", "::1"):
        raise HTTPException(403, "fence-decision endpoint is loopback-only")
    try:
        from lib import fence_verdict as _fv
    except ImportError:
        import sys as _sys
        _sys.path.insert(0, "/usr/local/lib/bedrock")
        from lib import fence_verdict as _fv  # type: ignore
    if req.resource == _fv.ARBITER_RES:
        state = getattr(request.app.state, "bedrock", None)
        if state is None:
            return {"verdict": "undecided", "detail": "no shared state"}
        verdict = _fv.decide_fence(state, req.peer_octet)
    else:
        verdict = _fv.decide_vm_fence(req.resource, req.peer_node or None)
    return {"verdict": verdict, "resource": req.resource}
