"""Lint: Bedrock systemd units must follow the rewrite-plan §3.5 rules.

- Only bedrock-d.service is WantedBy=multi-user.target (the sole
  Bedrock-managed top-level daemon).
- No cross-Bedrock Requires= between units. The saga executor /
  orchestrator inside bedrock-d explicitly starts each downstream
  unit when its preconditions are met; cross-unit Requires creates
  dependency cycles (we hit one in 0.8-alpha: bedrock-rqlited
  Requires=bedrock-d, bedrock-d's orchestrator needed rqlite —
  deadlock at first boot).

Exemptions:
- bedrock-mdns, bedrock-redirect: cosmetic browser-facing helpers
  that boot on their own; not part of the cluster-decision path.
- bedrock-vg-loop: pre-LVM-activation oneshot; legitimate
  multi-user.target dep.
- bedrock-cert-refresh: started by its own timer.
"""
from __future__ import annotations

import pathlib
import re


REPO = pathlib.Path(__file__).resolve().parents[1]
CONFIGS = REPO / "installer" / "configs"


# Units that may legitimately be WantedBy=multi-user.target despite
# the "bedrock-d is the only one" rule. These are not on the cluster-
# decision path.
WANTEDBY_EXEMPT = {
    "bedrock-d.service",
    "bedrock-mdns.service",
    "bedrock-redirect.service",
    # bedrock-rqlited: the per-node consensus FOUNDATION. It must
    # auto-start at boot INDEPENDENTLY of bedrock-d (which only READS
    # rqlite for its election). The cluster_init/node_join saga starts
    # it ONCE; on a later reboot nothing else would — and bedrock-d
    # can't bootstrap it from a lost state.json (circular). Observed
    # sim-1 2026-05-29: master reboot → rqlited never came back →
    # whole-node deadlock. It is installed disabled and the saga
    # `systemctl enable`s it once rqlited.env is rendered, so it only
    # auto-starts post-init. (NOT bedrock-rqlited-arbiter — that's the
    # floating singleton, started by cluster_arbiter on the master.)
    "bedrock-rqlited.service",
    # bedrock-vg-loop boots at local-fs-pre.target (LVM activation),
    # not multi-user.target — separately checked.
}


def _unit_files():
    return sorted(p for p in CONFIGS.glob("bedrock-*.service") if p.is_file())


def _get_directive(text: str, key: str) -> str:
    m = re.search(rf"^{re.escape(key)}=(.*)$", text, flags=re.MULTILINE)
    return (m.group(1).strip() if m else "")


def test_no_ghost_bedrock_net_service():
    """bedrock-net.service was the pre-unification daemon. It must
    not exist anymore — its presence creates broken Requires= /
    After= references on any unit that still names it."""
    assert not (CONFIGS / "bedrock-net.service").exists(), (
        "bedrock-net.service is a ghost from before daemon unification — "
        "delete it. Other units should reference bedrock-d.service instead."
    )


def test_no_unit_references_bedrock_net():
    """No remaining file may name bedrock-net.service in After=,
    Requires=, or Wants= clauses."""
    offenders: list[str] = []
    for p in _unit_files():
        text = p.read_text()
        # Match the literal word, bounded so 'bedrock-d' doesn't
        # false-match.
        if re.search(r"\bbedrock-net\.service\b", text):
            offenders.append(p.name)
    assert not offenders, (
        f"these units still reference the retired bedrock-net.service: "
        f"{offenders}. Replace with bedrock-d.service (After=) — or "
        f"drop entirely if it was a Requires=."
    )


def test_only_bedrock_d_is_wantedby_multiuser():
    """bedrock-d is the only Bedrock-managed top-level daemon.
    Every other unit starts via the saga executor / orchestrator."""
    offenders: list[str] = []
    for p in _unit_files():
        if p.name in WANTEDBY_EXEMPT:
            continue
        wantedby = _get_directive(p.read_text(), "WantedBy")
        if "multi-user.target" in wantedby:
            offenders.append(f"{p.name} → WantedBy={wantedby}")
    assert not offenders, (
        "these units are WantedBy=multi-user.target but should not "
        "auto-start at boot — the cluster_init / node_join saga "
        "(inside bedrock-d) is responsible for enabling them at the "
        "right step. Set WantedBy= (empty) instead:\n  "
        + "\n  ".join(offenders)
    )


def test_no_cross_bedrock_requires():
    """No Bedrock unit may Requires= another Bedrock unit. Use
    After= for ordering hints only; the orchestrator decides
    actual lifecycle."""
    offenders: list[str] = []
    for p in _unit_files():
        text = p.read_text()
        for line in text.splitlines():
            if not line.startswith("Requires="):
                continue
            for dep in line[len("Requires="):].strip().split():
                if dep.startswith("bedrock-") and dep.endswith(".service"):
                    offenders.append(f"{p.name}: Requires={dep}")
    assert not offenders, (
        "Bedrock units may not Requires= other Bedrock units (creates "
        "dependency cycles). Use After= for ordering only; the saga "
        "executor / orchestrator owns actual lifecycle:\n  "
        + "\n  ".join(offenders)
    )
