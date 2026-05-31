"""2-node casting-vote saga — witness-loss rescue (storage-unification #7).

When a 2-node cluster's only witness is confirmed corrupt (the #6 own-readback
health check flagged it — a LYING store, not mere unreachability), the two nodes
drop to 100 votes each out of a 201 denominator: neither can reach the 101
majority alone, so a partition would HALT BOTH. The casting vote rescues this:
arm an explicit +1 bound to the CURRENT MASTER's name, so the incumbent stays
sticky at 101 and a partition leaves the master up (no failover; if the master
dies the cluster halts — accepted, see the design doc). Then fully remove the bad
witness from the DENOMINATOR so the math is clean.

This module owns ONLY the decision: given the current cluster view + this node's
name, what is the SINGLE next vote-config transition (if any). The executor
(`drive`) applies it via bedrock_state. Keeping the decision pure makes the
split-brain-critical sequencing unit-testable with no cluster.

SAFETY MODEL (verified design — see project_storage_unification_design.md,
"VOTE-CHANGE SAFETY PRINCIPLE" + the casting-vote section):

  * Every transition is ONE step per call; the saga re-enters and advances only
    when the previous step is ALL-NODES-APPLIED (min(nodes.applied_epoch) over
    ACTIVE nodes >= vote_config_epoch; the arbiter is not a nodes row → excluded
    by construction — counting it would re-open the split-brain hole).
  * Bar-LOWERING steps (arm casting, drop a witness from the denominator) are the
    dangerous direction and are exactly the ones gated by the all-applied wait.
    The netd election tick applies the SAME gate, so a follower that has not yet
    applied never lets the master lower its bar prematurely.
  * FORWARD (witness lost): arm casting=master → wait all-applied → disable the
    witness (denominator drop) → wait all-applied. The bad witness stays in the
    denominator (biases to no-failover, SAFE) until the casting vote is armed +
    everyone has it; never witness-gone-here while witness-present-there.
  * REVERSE (witness healthy again): disarm casting FIRST → wait all-applied →
    re-enable the witness. Never the reverse order.
  * ABORT on master change is automatic via name-binding: the casting +1 is only
    credited when casting_vote_node == self, so a stale name credits nobody; the
    new master simply re-arms to its own name (forward) or disarms (reverse).
  * N=2 ONLY. If the cluster grew to N>=3 a normal majority already survives one
    witness loss, so any armed casting vote is unwound.
"""
from __future__ import annotations

from typing import Optional


# Action = (verb, arg). verb in {arm_casting, disable_witness, disarm_casting,
# enable_witness}; arg = node name (casting) or witness_id (witness). None = the
# saga is idle or WAITING for the current step to become all-applied.
Action = Optional[tuple]


def active_nodes(cluster: dict) -> dict:
    """Active nodes for the election denominator: state=='active' and not in
    maintenance. Matches netd's _is_active semantics (the arbiter is never a
    nodes row, so it is excluded here and in the watermark by construction)."""
    out = {}
    for name, info in (cluster.get("nodes") or {}).items():
        info = info or {}
        if info.get("state", "active") == "active" and not info.get("maintenance"):
            out[name] = info
    return out


def applied_watermark(cluster: dict) -> int:
    """min(applied_epoch) over ACTIVE nodes — the all-nodes-applied watermark the
    master gates bar-lowering on. 0 when there are no active nodes (gate closed)."""
    nodes = active_nodes(cluster)
    if not nodes:
        return 0
    return min(int((info or {}).get("applied_epoch") or 0)
               for info in nodes.values())


def decide_casting_action(cluster: dict, my_node: str) -> Action:
    """The single next vote-config transition for the master to make, or None
    (idle / waiting for all-applied). Pure: no IO, safe to call every tick.

    Only the master drives the saga; a non-master always gets None. The arbiter
    asymmetry HELPS here — vote-config is master-only, so a partitioned follower
    can never arm or lower the bar.
    """
    if (cluster.get("mgmt_master") or None) != my_node:
        return None

    nodes = active_nodes(cluster)
    witnesses = cluster.get("witnesses") or {}
    epoch = int(cluster.get("vote_config_epoch") or 0)
    all_applied = applied_watermark(cluster) >= epoch
    casting = cluster.get("casting_vote_node") or None

    corrupt = {wid for wid, w in witnesses.items() if (w or {}).get("corrupt")}
    disabled = {wid for wid, w in witnesses.items() if (w or {}).get("disabled")}

    # The casting vote is an N=2 mechanism that compensates for a witness that has
    # left the NUMERATOR (corrupt). Needed from the moment a witness goes corrupt
    # on an N=2 cluster until it is healthy again.
    need_casting = (len(nodes) == 2) and bool(corrupt)

    if need_casting:
        # ── FORWARD: arm casting → (wait) → disable the corrupt witness(es) ──
        if casting != my_node:
            return ("arm_casting", my_node)          # step 1 (bumps epoch)
        if not all_applied:
            return None                              # wait: arm not yet everywhere
        to_disable = sorted(corrupt - disabled)
        if to_disable:
            return ("disable_witness", to_disable[0])  # step 2 (bumps epoch)
        return None                                  # forward complete

    # ── REVERSE / cleanup: not (or no longer) needed ──────────────────────────
    # Covers: witness recovered (corrupt cleared), cluster grew to N>=3, or a
    # stale casting name from a previous master. Disarm FIRST (raises the bar,
    # safe), then — once that is all-applied — re-add any witness we disabled.
    if casting is not None:
        return ("disarm_casting", my_node)           # step R1 (bumps epoch)
    if not all_applied:
        return None                                  # wait: disarm not yet everywhere
    # Re-enable only a witness that is no longer corrupt (a still-corrupt store
    # stays out — it lied; the operator clears it when replaced).
    to_enable = sorted(disabled - corrupt)
    if to_enable:
        return ("enable_witness", to_enable[0])      # step R2 (bumps epoch)
    return None


def drive(cluster: dict, my_node: str, *, client=None, log=None) -> Action:
    """Decide + execute ONE saga step. Returns the action taken (or None). Called
    periodically on every node from netd's off-hot-path worker; only the master
    ever acts. Idempotent + re-entrant: each call advances at most one transition
    and the next call re-derives from fresh rqlite state.
    """
    action = decide_casting_action(cluster, my_node)
    if action is None:
        return None
    try:
        from . import bedrock_state as _bs        # type: ignore
    except ImportError:                            # pragma: no cover
        from lib import bedrock_state as _bs       # type: ignore

    verb, arg = action
    if log:
        log(f"casting-saga: {verb} {arg} "
            f"(epoch={cluster.get('vote_config_epoch')}, "
            f"watermark={applied_watermark(cluster)})")
    if verb == "arm_casting":
        _bs.casting_vote_arm(arg, client=client)
    elif verb == "disarm_casting":
        _bs.casting_vote_disarm(client=client)
    elif verb == "disable_witness":
        _bs.witness_disable(arg, client=client)
    elif verb == "enable_witness":
        _bs.witness_enable(arg, client=client)
    return action
