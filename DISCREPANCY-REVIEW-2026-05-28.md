# Bedrock — Platform Discrepancy Review (2026-05-28)

> **What this is.** A full code ↔ documentation ↔ architecture discrepancy
> audit of the entire Bedrock tree, run as an 11-dimension multi-agent review
> (quorum/election/witness, code-tree boundary, state+rqlite, storage/DRBD/SeaweedFS,
> sagas/orchestrator, VM lifecycle+failover, networking/mesh, install/ISO/mgmt,
> operator-overrides/CLI, tests/testbed, doc-internal-consistency) followed by a
> manual synthesis. **88 raw findings.** Every `critical`/`high` claim referenced in
> the "Biggest architectural discrepancies" section below was spot-checked against the
> actual source before inclusion.
>
> **Baseline of intent.** The review measured the code against the authoritative docs
> (`BEDROCK.md`, `cluster-quorum-spec.md`, `quorum-design-notes.md`, `state-flow.md`,
> `operator-overrides.md`, `storage-architecture.md`, `daemon-unification.md`, the
> `docs/actions/*` and `docs/sagas/*` contracts) **and** against the design decisions
> recovered from the 2026-05-28 quorum-design session.
>
> **How to read.** Each finding has a stable ID (prefix = area), a severity
> (`critical` > `high` > `medium` > `low`), a category, file:line refs, the doc it
> contradicts, and a minimal recommendation. IDs: **Q**=quorum/election/witness,
> **T**=code-tree, **ST**=state/rqlite, **SG**=storage, **SA**=sagas, **VM**=vm,
> **N**=net, **I**=install/ISO/mgmt, **OP**=operator/CLI, **TE**=tests/testbed,
> **D**=docs.

---

## Executive summary

The **biggest single theme**: Bedrock is mid-way through *two* large architectural
moves and neither is finished, so for several subsystems there are **two
implementations** — one live, one documented-but-dead — and the docs describe a mix
of both.

1. **The post-alpha rewrite (`installer/lib` → `bedrock_d` sagas) is half-landed.**
   The *install/cluster* sagas (cluster_init, node_join, node_leave, cluster_tier,
   rename, vm_failover) are live; the *VM-lifecycle* sagas (create/destroy/grow/migrate)
   are **dead code** — the running path is still `installer/lib/vm.py` driven in-process
   by the CLI and `mgmt/app.py`. The two trees import each other via `sys.path` shims.

2. **The quorum/election rework partially landed.** The *witness transport* rework
   succeeded — `installer/lib/witness.py` and the testbed stub are now passive
   AEAD (ChaCha20-Poly1305) K/V slot stores, and the arbiter takeover protocol enforces
   exact-UUID equality (INV-5) and worst-case-missing-slot (INV-7 read side). **But** the
   deeper consensus design locked in `quorum-design-notes.md` (drop the 10/1 vote
   weighting, 30 s patience window, multi-phase advertise→ack→actuate→broadcast,
   `state.json` LMS/transitioning/maintenance fields, transition bits + sentinel
   zero-UUID, cold-boot DRBD-UUID check, peer-ahead eligibility veto, 7-day rqlite
   UUID-history table) is **almost entirely unimplemented** — the code still runs the
   old immediate weighted-vote model.

3. **`cluster.json` was deleted (2026-05-26) but the cleanup is incomplete.** Dead
   `CLUSTER_JSON` constants remain in 7 modules, ~6 docs still describe it as a live
   per-revision projection, several testbed scripts still read it (and now see only the
   stale N=1 bootstrap snapshot), and the **node_leave saga crashes** because it calls
   the deleted `view_builder.rebuild()`.

4. **Docs have diverged hard.** `state-flow.md` still documents the *old*
   blessed-master/HMAC/15 s-holddown witness with no warning banner;
   `quorum-design-notes.md` self-contradicts on the vote model **and** says
   witness-state-loss clears LMS — directly contradicting both `cluster-quorum-spec.md`
   INV-7 and the cornerstone design rule "*witness losing state does NOT clear LMS;
   worst case is assumed*"; `ports.md`/`api.md`/`files.md` describe the pre-rewrite
   world; and 7 live modules cite a `docs/post-alpha-rewrite-notes.md` that no longer
   exists.

The good news: the *foundational* layers are sound. The mesh/routing daemon is mature
and its load-bearing sysctls are correct; the saga engine itself is solid; the witness
transport rework is clean; the single-local-`state.json` model landed correctly with
atomic writes; rqlite is the canonical store and the no-quorum/recovery paths correctly
escalate to `level='strong'`.

---

## Biggest architectural discrepancies (review these first)

These are the ones where **implementation and intended architecture have genuinely
diverged** — i.e. decisions, not just doc edits.

### BAD-1 — Quorum/election: the *transport* landed, the *consensus model* did not
The AEAD passive-slot witness + exact-UUID takeover from `cluster-quorum-spec.md` is in
the code. The richer model locked in `quorum-design-notes.md` is not: no 30 s patience
window, no single unified promote-decision, no multi-phase advertise→ack→actuate, no
`state.json` `last_man_standing`/`transitioning`/`maintenance_mode` fields, no
transition bit / sentinel zero-UUID marker, no cold-boot DRBD-UUID-vs-slot check, no
peer-ahead eligibility veto, no 7-day UUID-history table (only a single `current_uuid`
column). `election.py` still uses the old `10/node + 1/witness` weighting that
`quorum-design-notes.md` explicitly says it removes.
*(Findings Q-03, Q-05, Q-06, Q-07, ST-01, ST-02.)*
**This is the largest fork and gates the most downstream work.**

### BAD-2 — The `bedrock_d` rewrite is half-landed; the VM-saga tree is dead code
`bedrock_d/vm/{create,destroy,grow,migrate}.py` are reachable only by tests — the CLI
(`bedrock vm …`) imports `lib.vm` and runs DRBD/SSH **in-process on whatever node the
operator is on**, and the dashboard `POST /api/vms` also calls `lib.vm`. This
contradicts the rewrite plan's "CLI is a thin HTTP client" goal and means the
rewrite-target VM code exists in full but is unreachable.
*(Findings T-01, T-02, T-08, T-10.)*

### BAD-3 — Two incompatible storage layouts coexist
Live path = per-**tier** (`tier-critical` / `tier-<tier>-meta`, and the legacy VM path
uses *internal* DRBD metadata). Documented path = per-**resource**
(`bedrock-data-<r>` / `bedrock-meta-<r>`, *external* metadata) implemented in
`bedrock_d/vm/lvm.py` but not wired for the cluster singleton. The singleton is named
`tier-critical` in the live code but `cluster` in the `bedrock_d` module + docs. VG is
`bedrock` in code, `bedrock-vg` in docs, and often literally `almalinux` (adopted).
DRBD port bases differ across trees (7000 / 7700 / 7789) and the 7700-base VM range
(8900-9599) collides with `weed-master:9333`.
*(Findings SG-01, SG-02, SG-03, SG-04, SG-07.)*

### BAD-4 — `netd` per-tick LMS recompute races the explicit takeover/demote writes (INV-3)
`netd.py:1150` unconditionally sets `ws.own_tag = TAG_LMS if (am_hosting and not any_peer_up) else 0`
every 1 Hz tick, concurrently with `cluster_arbiter`'s protocol writes and its step-5
readback. It can clear an LMS bit the protocol means to hold, or make the readback never
observe `lms=1` and spuriously refuse a takeover. INV-3 says tag transitions are explicit
local *decision* events, not a steady-state function. **Safety bug in the part that landed.**
*(Finding Q-01.)*

### BAD-5 — Stuck-LMS recovery is inert: the witness membership filter is unimplemented
INV-7(b) — decommission a dead node so peers ignore its slot — is the *primary* documented
escape from a stuck `lms=1`. `witness.py drain_replies()` has only a TODO; the member-id
set is never plumbed in, so `bedrock node leave` does **not** unstick the cluster. The only
working escape today is the re-key-witness override.
*(Findings Q-02, OP-02, TE-09.)*

### BAD-6 — Saga power-loss resume is never wired
`SagaExecutor.resume_in_flight()` is called only by tests — never at daemon boot — and
there is no retry endpoint. BEDROCK.md's core guarantee ("power-loss at any step is
recoverable on boot") is **not delivered** for runtime sagas (vm_create,
cluster_tier_promote, node_leave). A crash mid-promote leaves an `in_progress` op nothing
resumes.
*(Findings SA-02, SA-07.)*

### BAD-7 — VM failover (the "shipped + e2e-validated" machine) has real holes
- **node_leave saga crashes** at step 1 — calls deleted `view_builder.rebuild()` (CRITICAL, SA-01).
- A VM **resumed** when quorum returns inside 5 min is **still killed at T+5 min** —
  `suspended-vms.json` is never cleared on resume (VM-01). Hits the common
  short-partition-recovers case.
- **Every live-migrate** leaves `drbd_resources.current_uuid` stale → a later failover is
  **REFUSED** by the INV-5 exact-equality gate (VM-02). Silent HA regression per migrate.
- **Three divergent migrate implementations**; the saga's `--undefinesource` removes the
  domain from the source, breaking pet/vipet failback (VM-03).
- Failover is **hard-wired to `disk0`** — multi-disk VMs lose disk1+ on takeover (VM-04).

### BAD-8 — Boot safety + the docs-vs-reality gap
- **libvirtd auto-starts at boot** (`packages.py` `enable --now`) despite `install.sh`
  disabling it for the documented quorum-aware boot model → VMs/DRBD can come up before
  the orchestrator establishes quorum (I-02).
- **kopia** is staged into the ISO but never installed → all backup/restore actions fail
  `command-not-found` (I-01).
- **`quorum-design-notes.md` says witness-state-loss clears LMS** — the exact opposite of
  `cluster-quorum-spec.md` INV-7 and your stated rule (D-01, CRITICAL doc contradiction).
- **`state-flow.md`** still documents the old blessed-master/HMAC/holddown witness with no
  out-of-date banner (D-02); `ports.md`/`api.md`/`files.md` describe the pre-rewrite world
  (I-03, I-04, I-05); 7 modules cite a missing `post-alpha-rewrite-notes.md` (T-12).

---

## Cross-cutting themes (how the 88 findings cluster)

| Theme | Findings | Net |
|---|---|---|
| Half-landed `bedrock_d` rewrite (dead VM sagas, import cycles, legacy opt-out bodies) | T-01,T-02,T-08,T-10, I-07, SA-04 | structural |
| Quorum-design-notes model unimplemented | Q-03,Q-05,Q-06,Q-07, ST-01,ST-02 | scope decision |
| `cluster.json` half-removed (dead constants, stale docs, broken scripts, crashing saga) | SA-01,SA-05, ST-03,ST-04, N-03,N-05, TE-06, I-05,I-07 | cleanup |
| Witness/LMS safety gaps | Q-01,Q-02,Q-04, D-01 | safety |
| VM failover correctness | VM-01,VM-02,VM-03,VM-04,VM-07 | correctness |
| Storage model split + ports | SG-01..SG-04,SG-07 | decision |
| Saga durability not wired | SA-02,SA-07 | correctness |
| Stale reference docs (ports/api/files) | I-03,I-04,I-05, N-04, T-09,T-11 | docs |
| Doc-vs-doc timing/vote contradictions | D-01,D-03,D-04, Q-03 | docs |
| Operator-override surface incomplete | OP-01..OP-05 | scope |
| Tests red / coverage gaps / stale testbed scripts | TE-01..TE-08 | hygiene |
| Boot/runtime safety (libvirtd, kopia, 8080 collision) | I-01,I-02, T-05 | safety |

---

## Full finding catalog

Severity legend: 🔴 critical · 🟠 high · 🟡 medium · ⚪ low. Confidence shown per finding.


**Totals:** 88 findings — 🔴 2 critical · 🟠 25 high · 🟡 33 medium · ⚪ 28 low.


### Quorum / Election / Witness / LMS

_The witness transport rework (HMAC/blessed_master/active-arbiter → passive AEAD ChaCha20-Poly1305 K/V slot store) HAS landed: installer/lib/witness.py, testbed/bedrock_echo_stub.py, and witness.md are all on the new model, and no blessed_*/send_claim/holddown remnants survive in the witness layer (the residual HMAC in netd.py is the unrelated mesh-probe codec). The arbiter takeover protocol (cluster_arbiter._run_takeover_protocol) implements steps 1-5 with exact-UUID equality (INV-5 honored), missing-slot-is-worst-case and stale+lms=1 refusal (INV-7 honored on the read side), and own-write readback (step 5). Election/witness/arbiter logic lives entirely in installer/lib (NOT duplicated in bedrock_d — clean boundary). HOWEVER the code implements a SIMPLE weighted-vote (10/node + 1/witness) immediate-decision model, while docs/quorum-design-notes.md locks in a substantially DIFFERENT design — vote model without 10/1 weighting, 30s patience window, single-unified promote-decision, multi-phase transition consensus, state.json with LMS/transitioning/maintenance fields, transition bits 1+2, sentinel-zero-UUID marker, cold-boot DRBD-UUID check, and peer-ahead eligibility veto — almost none of which exists in code. The most dangerous concrete gap: netd's per-tick own_tag recompute (netd.py:1148) overwrites the explicit LMS transitions the takeover/demote protocol writes, racing the step-5 readback and substituting a steady-state heuristic for INV-3's explicit local-decision transitions._


#### Q-01 🟠 netd per-tick LMS recompute overwrites explicit takeover/demote tag writes (INV-3 violation, breaks step-5 readback)
`high` · unsafe-invariant · confidence: medium

Every netd election tick unconditionally recomputes ws.own_tag = TAG_LMS if (am_hosting AND not any_peer_up) else 0 (netd.py:1148-1150). cluster_arbiter._run_takeover_protocol step 4 calls _witness.set_own_slot(ws, marker, tag=TAG_LMS) (cluster_arbiter.py:525) and then step 5 sleeps up to 4.5s waiting to read its own lms=1 back from the witness (cluster_arbiter.py:529-541). But converge() runs in mgmt/orchestrator.converge_retry via asyncio.to_thread CONCURRENTLY with the netd thread, which can overwrite own_tag back to 0 on its next 1Hz tick if its am_hosting/any_peer_up heuristic disagrees (e.g. DRBD/IP not yet promoted at step 4, so arbiter_status reports am_hosting=False). That can make the step-5 readback never observe lms=1 and the takeover REFUSE spuriously, or conversely clear an LMS bit that the protocol intends to hold. Spec INV-3 says tag transitions are local DECISION events ('set on this-node-decided-to-go-solo, cleared on this-node-self-demoted'), not a steady-state function recomputed every tick from observables. The two writers (netd heuristic vs cluster_arbiter protocol) are not coordinated.

**Code:** `installer/lib/netd.py:1124-1152`, `installer/lib/cluster_arbiter.py:520-541`, `installer/lib/cluster_arbiter.py:663-676`
**Docs:** docs/cluster-quorum-spec.md#invariants (INV-3, INV-7); docs/cluster-quorum-spec.md#arbiter-takeover-protocol (steps 4-5)

**Fix:** Make the explicit takeover/demote tag writes authoritative: either gate the netd own_tag recompute behind a flag that cluster_arbiter sets while a takeover/demote is in flight, or move ALL own_tag ownership to one place. Minimal fix: have netd only SET lms when am_hosting&&!any_peer_up but never CLEAR it unless an explicit demote has run (track an in-memory 'lms_intent' on shared state owned by cluster_arbiter).

#### Q-02 🟠 Witness rqlite `nodes` table membership filter not implemented — stuck-LMS decommission override is inert
`high` · missing-impl · confidence: high

INV-7's primary operator escape hatch for a stuck lms=1 belonging to a dead node is 'decommission the node from the rqlite nodes table → peers ignore its slot'. The spec slot-lifecycle section and operator-overrides.md both require drain_replies to drop any slot whose node_id is not a current member of the local rqlite `nodes` table. witness.py:286-297 contains only a TODO acknowledging this is NOT implemented ('Until that lands, removed nodes stale lms=1 slots still block takeover even after node leave'). operator-overrides.md:123-130 explicitly confirms: 'Filter is currently not implemented... the takeover will refuse. The filter needs to be added before this override is effective.' So the documented primary recovery path for a stuck LMS does not work today.

**Code:** `installer/lib/witness.py:286-310`
**Docs:** docs/cluster-quorum-spec.md#slot-lifecycle (membership filter); docs/cluster-quorum-spec.md#invariants (INV-7 path (b)); docs/operator-overrides.md#override-decommission-stuck-lms-holder (lines 123-130)

**Fix:** Plumb the current rqlite `nodes` member-id set into drain_replies (a member_ids: set[int] field on WitnessState refreshed each netd tick from cluster_state.load_cluster), and skip slots whose node_id is not in it. Until then, document that operators must use the re-key-witness override (which DOES work) instead of node leave to clear a stuck LMS.

#### Q-03 🟡 quorum-design-notes locked design (no-10/1-weighting vote, 30s patience, multi-phase consensus, rich state.json) is almost entirely unimplemented
`medium` · code-vs-doc · confidence: high

election.py uses the OLD weighted vote (VOTES_PER_NODE=10 + VOTE_PER_WITNESS=1, docstring at line 20 says 'unchanged from the original Rust prototype'). quorum-design-notes.md locked decision (lines 44-61) explicitly REMOVES the 10/1 weighting ('Removes the awkward 10/1 weighting from the older spec'). Further, NONE of these locked-in design elements exist in code: (1) 30s patience window before any witness-assisted decision (grep finds no patience/cold-boot wait in election or netd); (2) single unified promote-decision function; (3) multi-phase advertise→ack→actuate→broadcast failover (phases 1-6); (4) state.json fields last_man_standing/transitioning/maintenance_mode/transition_id/last_drbd_uuid_observed (grep across the whole repo finds zero writers — election does not write state.json at all); (5) maintenance-mode 0-vote handling; (6) DRBD-UUID-history-chain eligibility veto. The design notes describe a TARGET that the code has not adopted; cluster-quorum-spec.md is closer to the code but still ahead of it (see other findings).

**Code:** `installer/lib/election.py:20-151`, `installer/lib/netd.py:1124-1152`, `installer/lib/state.py:1-63`
**Docs:** docs/quorum-design-notes.md#decisions-locked-in (LMS is +1 vote; removes 10/1 weighting); docs/quorum-design-notes.md (30s patience window; single unified promote-decision; multi-phase failover phases 1-6; state.json fields)

**Fix:** Decide whether quorum-design-notes.md is aspirational backlog or binding. If binding, it should be reconciled with election.py/netd.py before v1.0; if aspirational, mark it clearly as 'not-yet-implemented design queue' to stop it reading as a contract the code violates. At minimum reconcile the contradiction between the two docs on the vote weighting (10/1 vs +1).

#### Q-04 🟡 Mid-takeover transition marker (all-zero UUID + transitioning tag bit) not implemented
`medium` · missing-impl · confidence: high

The design (KEY DECISION #4) calls for writing an all-zero UUID + a transitioning flag mid-takeover so UUIDs stop matching the peer (tripping the are-UUIDs-equal safety) and logging details to VictoriaLogs. witness.py defines ONLY TAG_LMS = 0x01 (line 56); there is no TAG_TRANSITIONING (bit 1) or TAG_MAINTENANCE (bit 2). The takeover (cluster_arbiter.py:525) writes the REAL current DRBD UUID with tag=TAG_LMS, never a zero sentinel, and never sets a transitioning bit. No code writes 0x0000...marker. Crash-during-takeover recovery (design phases 1/4) has nothing to key off.

**Code:** `installer/lib/witness.py:55-56`, `installer/lib/cluster_arbiter.py:520-541`
**Docs:** docs/quorum-design-notes.md (Phase-1/phase-3 advertisement: sentinel marker 0x0000... + tag.transitioning=1); docs/quorum-design-notes.md#witness-slot-tag-bitflag (bit 1 = transitioning, bit 2 = maintenance)

**Fix:** If the multi-phase/sentinel design is intended for v1.0, add TAG_TRANSITIONING/TAG_MAINTENANCE constants + the zero-marker write. If not, note in cluster-quorum-spec.md that transition-bit/sentinel is post-v1.0 so the design notes don't read as an unmet contract.

#### Q-05 🟡 Cold-boot protocol (DRBD-UUID compare against own slot marker) not implemented
`medium` · missing-impl · confidence: medium

The spec's cold-boot protocol requires a starting node to compare local drbdadm current-uuid against slot[self].marker and refuse to promote if local is OLDER (cluster advanced without us). The 'What this spec replaces' table already flags 'cold-boot DRBD check: not implemented' as the target. Code: boot path is mgmt/orchestrator.boot_orchestrator → _wait_for_role → election.compute, which is mesh-vote-only and contains no DRBD-UUID-vs-slot comparison; cluster_arbiter._run_takeover_protocol only runs the UUID compare on the FAILOVER path when a prior different master exists (last_master_id != my_id), and short-circuits with 'no prior master to take over from; proceeding' (line 437-441) on the cold-boot/first-promote case. So a stale node that cold-boots as the recorded master can promote without the spec's local-older refuse check.

**Code:** `installer/lib/cluster_arbiter.py:311-394`, `mgmt/orchestrator.py:29-41`, `installer/lib/election.py:66-151`
**Docs:** docs/cluster-quorum-spec.md#cold-boot-protocol (steps 1-4); docs/cluster-quorum-spec.md#what-this-spec-replaces (cold-boot DRBD check: 'not implemented')

**Fix:** Add the slot.marker-vs-local-UUID cold-boot comparison to the promote path (or to _run_takeover_protocol's fast-path) so a node whose local DRBD generation is older than its own last-published slot marker refuses to promote until reconciled.

#### Q-06 🟡 Peer-ahead eligibility veto (refuse if any peer has newer DRBD generation) not implemented
`medium` · missing-impl · confidence: medium

The design's load-bearing eligibility rule: a node refuses to claim master if ANY known peer has a more up-to-date DRBD generation (peer's UUID not in my history chain) — even if that peer is unreachable — to avoid silently losing writes; operator 'seize' is the only override. Code has no history-chain comparison anywhere. election.py is pure mesh-vote and never inspects DRBD UUIDs. cluster_arbiter step 3 (line 503-518) only checks EXACT EQUALITY of local UUID vs the (single) dying master's slot marker; it does not enumerate all peers, does not consult the DRBD history chain, and does not handle the 'peer is ahead but unreachable' case. The 'newer wins via history chain' tie-break and operator-seize path are entirely absent (docs/operator-overrides.md marks seize 'Outline only').

**Code:** `installer/lib/election.py:66-151`, `installer/lib/cluster_arbiter.py:503-518`
**Docs:** docs/quorum-design-notes.md (Candidate eligibility — strict: refuse if any peer has more up-to-date tier-critical generation, reachable or not); docs/quorum-design-notes.md (Election flow: ELIGIBILITY check using history chain)

**Fix:** Treat as known v1.0 gap. At minimum the exact-equality check at cluster_arbiter.py:510 already refuses on local!=slot (which conservatively covers 'local older'), but the multi-peer / history-chain eligibility and the seize override remain unimplemented and should be tracked explicitly against quorum-design-notes.

#### Q-07 ⚪ Spec's self-demote vs peer-takeover ordering relies on netd 5-tick NoQuorum, not on the spec's witness-reachability self-demote
`low` · code-vs-arch · confidence: medium

The spec's self-demote protocol (cluster-quorum-spec.md self-demote section) is triggered by the local election concluding NoQuorum for >=5 ticks OR a fresh-boot UUID-mismatch, and explicitly stops services then writes tag.lms=0. In code the NoQuorum self-demote is implemented in netd._election_tick (NOQUORUM_HOLDDOWN_TICKS=5, netd.py:987) calling cluster_arbiter.demote_arbiter_host(), which DOES end by writing set_own_slot(tag=0) (cluster_arbiter.py:673). This part broadly matches. However the spec's cluster_arbiter.demote_arbiter_host docstring promise ('A node currently arbiter-host self-demotes when its local election concludes NoQuorum for >=5 ticks') is enforced from netd, not from cluster_arbiter, and the 28s LONE_MASTER_WATCHDOG_S (netd.py:136) is defined but the watchdog is intentionally NOT wired (netd.py:1117-1122). Net: behavior is close but the demote ownership/timing is split across netd vs cluster_arbiter in a way the spec attributes solely to the self-demote protocol. Low risk, but worth noting the spec and code attribute the trigger to different modules.

**Code:** `installer/lib/netd.py:127`, `installer/lib/netd.py:978-1028`, `installer/lib/cluster_arbiter.py:108-113`
**Docs:** docs/cluster-quorum-spec.md#arbiter-self-demote-protocol; docs/cluster-quorum-spec.md#timing-knobs (NoQuorum self-demote streak = 5 ticks)

**Fix:** No functional change required; align the docs to state that NoQuorum self-demote is actuated by netd's election tick calling cluster_arbiter.demote_arbiter_host(), and that LONE_MASTER_WATCHDOG_S is defined-but-disabled.

#### Q-08 ⚪ state_shared.BedrockState comment still references removed blessed_* witness fields
`low` · doc-vs-doc · confidence: high

state_shared.py:68 comments that netd_ws holds 'WitnessState (sock + discovered Echo endpoints + blessed_* fields)' and is published 'so cluster_arbiter can fire witness claims at the moment of promotion'. The blessed_* fields and 'witness claims' no longer exist (the WitnessState dataclass has slots/own_marker/own_tag, no blessed_* — witness.py:92-111). Stale comment that contradicts the landed passive-slot model and could mislead a reader into thinking a claim mechanism still exists.

**Code:** `installer/lib/state_shared.py:67-72`
**Docs:** docs/cluster-quorum-spec.md#what-this-spec-replaces

**Fix:** Update the netd_ws comment to describe slots/own_tag and the readback-based takeover, removing the blessed_*/claim language.

### Code-tree boundary (installer/lib vs bedrock_d vs mgmt)

_Three Python trees coexist with a clear-but-leaky ownership split. installer/lib/*.py is authoritative for node-local daemon logic (election, witness, cluster_arbiter, netd, rqlite_setup, cluster_state, tier_storage, seaweedfs, AND state mutators bedrock_state.py/rqlite_client.py) and is the live VM lifecycle implementation (lib/vm.py). mgmt/*.py owns the management plane and the actual running orchestrator (mgmt/orchestrator.py holds rqlite_subscriber, no_quorum_responder, boot_orchestrator, cluster_tier_watcher, etc.). bedrock_d/** is the partially-landed "saga rewrite": its install/cluster-tier/rename/node-join-leave sagas ARE wired and live (cluster_init/node_join/node_leave default via BEDROCK_INIT_SAGA, cluster_tier via watcher, cluster_rename + vm_failover via API), but its VM lifecycle sagas (vm/create|destroy|grow|migrate) are DEAD in production — both the CLI (lib.vm in-process) and dashboard (POST /api/vms → lib.vm) bypass them. Import direction is bidirectional: bedrock_d imports installer/lib via sys.path shims while installer/lib (agent_install, mgmt_install) and mgmt import bedrock_d sagas. The unified daemon (bedrock-d) is real and matches bedrock-d-concept.md (mdns/redirect/cert as separate units) but contradicts daemon-unification.md (which says fold them in); loopback port is 8001 in code vs 8080 in both unification docs, and there is a genuine 8080 bind collision in serve_main's bootstrap path._


#### T-01 🟠 bedrock_d VM lifecycle sagas (create/destroy/grow/migrate) are dead code in production
`high` · dead-code · confidence: high

The documented VM-create flow (docs/sagas/README.md: 'vm_create | bedrock_d/vm/create.py | Triggered by POST /api/vms') does not match the running code. Two LIVE paths both bypass the sagas: (1) the CLI `bedrock vm create` (installer/bedrock:517) imports `lib.vm` and calls `vm_mod.create_vm(...)` IN-PROCESS on the CLI's node; (2) the dashboard's `POST /api/vms` handler `api_vm_create` (mgmt/app.py:2515) runs `_vm_create`/`_vm_create_replicated` which `from lib import vm as _vm` and calls `_vm._create_pet`/`_vm._create_vipet` via a task_registry, again not a saga. The only caller that can reach the bedrock_d VM sagas is the generic `POST /api/operations` endpoint (mgmt/routes_operations.py:84 _load_all_vm_sagas), which no shipped CLI verb or dashboard action invokes for VMs. So bedrock_d/vm/{create,destroy,grow,migrate}.py are exercised only by tests/test_vm_sagas.py. This is the single largest tree-boundary divergence: the rewrite-target VM tree exists in full but is unreachable.

**Code:** `bedrock_d/vm/create.py:47`, `bedrock_d/vm/destroy.py`, `bedrock_d/vm/grow.py`, `bedrock_d/vm/migrate.py`, `installer/bedrock:510-523`, `mgmt/app.py:2515`, `mgmt/app.py:2631`
**Docs:** docs/sagas/README.md#all-sagas; docs/codebase-rewrite-plan.md#1-goals (CLI thin HTTP client)

**Fix:** Pick one VM-op path and mark the other clearly. Either (a) route `api_vm_create` + `cmd_vm` through `/api/operations`/the saga and delete lib/vm.py create/migrate/delete, or (b) add a prominent 'NOT WIRED — target of codebase-rewrite-plan stage 11; legacy lib.vm is live' banner atop each bedrock_d/vm saga and correct docs/sagas/README.md 'Triggered by' column to say 'POST /api/operations (not yet on the dashboard/CLI path)'. Confidence high that the sagas are currently dead.

#### T-02 🟡 bedrock vm CLI runs DRBD/SSH logic in-process, contradicting 'CLI is a thin HTTP client'
`medium` · code-vs-arch · confidence: medium

codebase-rewrite-plan.md §1.5 states 'CLI is a thin HTTP client … No direct imports of installer.lib.* from the CLI.' But `cmd_vm` (installer/bedrock:512) does exactly `from lib import vm as vm_mod` and runs create/migrate/delete locally. lib.vm.create_vm/_create_pet then drive DRBD allocation and SSH fan-out (lib/vm.py:250 _create_pet uses run_on over SSH) from whatever node the operator ran the CLI on — which may not be the mgmt master that holds the DRBD/arbiter authority. Other CLI verbs (cluster rename, node leave) DO follow the thin-client model (POST /api/operations), so VM is the inconsistent outlier.

**Code:** `installer/bedrock:510-523`, `installer/lib/vm.py:118`, `installer/lib/vm.py:250`
**Docs:** docs/codebase-rewrite-plan.md#1-goals

**Fix:** Route `bedrock vm *` through `POST /api/operations` (or `POST /api/vms`) like cluster rename does, so VM provisioning always executes on the master. Minimal: at least guard create_vm to refuse when not the master. Confidence medium (CLI may be operator-run only on the master by convention, but nothing enforces it).

#### T-03 🟡 daemon-unification.md and bedrock-d-concept.md contradict each other on mdns/redirect/cert; code follows the concept doc
`medium` · doc-vs-doc · confidence: high

daemon-unification.md §'In scope (folded into bedrock-d)' lists bedrock-mdns, bedrock-redirect, and bedrock-cert-refresh as folded INTO bedrock-d (mdns thread, second uvicorn/catch-all, asyncio task). bedrock-d-concept.md §'At arm's length' says the opposite: they stay as separate small systemd units. The actual entry script installer/bedrock-d:88-93 explicitly does NOT import or own them ('They keep their own small systemd units … bedrock-d doesn't import or own them'), and the three .service units still exist. So the code matches bedrock-d-concept.md and contradicts daemon-unification.md. The bedrock-d.service Description ('netd + mgmt + orchestrator + dashboard') also omits these, consistent with the concept doc.

**Code:** `installer/bedrock-d:88-93`, `installer/configs/bedrock-mdns.service`, `installer/configs/bedrock-redirect.service`, `installer/configs/bedrock-cert-refresh.service`
**Docs:** docs/daemon-unification.md#in-scope-folded-into-bedrock-d; docs/bedrock-d-concept.md (At arm's length)

**Fix:** Update daemon-unification.md's table to move bedrock-mdns/bedrock-redirect/bedrock-cert-refresh out of 'In scope' into a 'stays separate (at arm's length)' row, matching code + bedrock-d-concept.md. Confidence high.

#### T-04 🟡 Loopback CLI/IPC port is 8001 in code but documented as 8080 in both unified-daemon docs
`medium` · code-vs-doc · confidence: high

daemon-unification.md (architecture block, line 36) and bedrock-d-concept.md (line 12) both say the loopback HTTP listener the CLI dials is 8080. The code uses 127.0.0.1:8001 everywhere: serve_main binds it (mgmt/app.py:5028), the CLI dials it (installer/bedrock:299,337 'http://127.0.0.1:8001'), and lib/vm.py:58 _LOCAL_API='http://127.0.0.1:8001'. The bedrock-d entry script docstring even notes the move ('Port 8080 belongs to weed-volume … HTTP loopback:8001'). The docs were not updated when 8080 was vacated for weed-volume.

**Code:** `mgmt/app.py:5006`, `mgmt/app.py:5028`, `installer/bedrock:299`, `installer/bedrock:337`, `installer/lib/vm.py:58`
**Docs:** docs/daemon-unification.md (architecture block); docs/bedrock-d-concept.md

**Fix:** Change '8080 (HTTP, loopback)' to '8001 (HTTP, loopback)' in daemon-unification.md line 36 and bedrock-d-concept.md line 12. Confidence high.

#### T-05 🟡 Bootstrap HTTP listener binds 127.0.0.1:8080 which collides with weed-volume's 0.0.0.0:8080
`medium` · unsafe-invariant · confidence: medium

serve_main's no-cert bootstrap branch binds uvicorn on 127.0.0.1:8080 (mgmt/app.py:5042). Its own comment claims this avoids the weed-volume conflict 'because weed binds 0.0.0.0:8080 and we bind 127.0.0.1' — but 0.0.0.0:8080 already covers the loopback address, so a 127.0.0.1:8080 bind will EADDRINUSE-fail whenever weed-volume is already up (it runs on every node, bound 0.0.0.0:8080 per bedrock-weed-volume.service:21-24 and seaweedfs.py:74). The docstring just above (lines 5014-5018) even declares 8080 'reserved for weed-volume' and says everything moved to 8001, contradicting the line-5042 bind. On a fresh node where weed-volume starts before the first cert exists, the operator-facing bootstrap dashboard would fail to bind.

**Code:** `mgmt/app.py:5036-5042`, `installer/configs/bedrock-weed-volume.service:21-24`, `installer/lib/seaweedfs.py:74`
**Docs:** docs/storage-architecture.md#port-map; docs/reference/ports.md

**Fix:** Move the no-cert bootstrap listener off 8080 (e.g. reuse 8001, or an unused bootstrap port). Minimal fix: change mgmt/app.py:5042 to a non-8080 port and update the comment. Confidence medium-high (collision is real; depends on systemd start ordering, which favors weed coming up).

#### T-06 🟡 Duplicated no-quorum VM-suspend logic across mgmt/orchestrator and bedrock_d/orchestrator/vm_failover
`medium` · duplication · confidence: medium

Two suspend-on-no-quorum implementations run in the same bedrock-d process. mgmt/orchestrator._run_no_quorum_cleanup (line 523) virsh-suspends EVERY running VM at marker+1s. bedrock_d/orchestrator/vm_failover.suspend_on_no_quorum_task (line 296) selectively suspends pet/vipet VMs and records them to suspended-vms.json for the kill timer. The vm_failover.py header (lines 69-71) acknowledges the overlap ('the no_quorum_responder also suspends every running VM at marker+1s — this vm_failover task is the selective belt-and-suspenders'). Functionally redundant for pet/vipet (suspended twice) and the two paths own different aspects (DRBD-demote decision, kill-timer record) split across trees, which is exactly the 'two daemons drifted' failure mode daemon-unification.md set out to eliminate — now reincarnated as two modules in two trees.

**Code:** `mgmt/orchestrator.py:523-540`, `bedrock_d/orchestrator/vm_failover.py:296`, `bedrock_d/orchestrator/vm_failover.py:65-72`
**Docs:** docs/daemon-unification.md (Goal: no 'two daemons drifted')

**Fix:** Consolidate the suspend trigger into one owner (prefer bedrock_d/orchestrator/vm_failover since it owns the kill-timer record and DRBD-aware ordering) and have mgmt/orchestrator's no_quorum cleanup delegate to it rather than independently suspending. Confidence medium (both are intentionally live per project_vm_failover_shipped memory; the duplication is by design today but fragile).

#### T-07 ⚪ bedrock_d/orchestrator/__init__.py claims to own tasks that actually live in mgmt/orchestrator.py
`low` · inconsistency · confidence: high

bedrock_d/orchestrator/__init__.py docstring states the package 'Owns: rqlite revision watcher, saga executor, service reconciler, membership rebalancer. Runs as the asyncio task inside bedrock-d.' In reality the rqlite revision watcher (rqlite_subscriber), service reconciler (_start_local_services / boot_orchestrator), and the calm-loop tasks live in mgmt/orchestrator.py (lines 169, 317, 458, 864). bedrock_d/orchestrator/ contains only vm_failover.py plus the sagas/ subpackage (the saga executor). The package docstring describes responsibilities that the mgmt tree fulfills, blurring the documented boundary.

**Code:** `bedrock_d/orchestrator/__init__.py:1-7`, `mgmt/orchestrator.py:169`, `mgmt/orchestrator.py:317`, `mgmt/orchestrator.py:458`

**Fix:** Narrow the bedrock_d/orchestrator/__init__.py docstring to what it actually owns (saga engine + vm_failover tasks), or note that the calm-loop tasks currently live in mgmt/orchestrator.py pending the rewrite. Confidence high on the mislabel.

#### T-08 ⚪ Bidirectional import coupling between bedrock_d and installer/lib via sys.path shims
`low` · code-vs-arch · confidence: high

The two trees import each other. bedrock_d reaches DOWN into installer/lib through repeated `sys.path.insert(0, .../installer)` shims then `from lib import ...` (bedrock_d/state.py:52, vm/create.py:32, vm/destroy.py:23, vm/grow.py:24, vm/migrate.py:33, sagas/rqlite_backend.py:25, install/*.py). Simultaneously installer/lib reaches UP into bedrock_d (agent_install.py:137 imports run_node_join; mgmt_install.py:83 imports run_cluster_init). This creates an import cycle at the package level and means neither tree can be relocated/packaged independently — every bedrock_d module assumes /usr/local/lib/bedrock is on sys.path. codebase-rewrite-plan.md §3.3 calls for bedrock_d.state to be the one owner with installer/lib re-exporting, but today it's the inverse: bedrock_d.state is a thin re-export OF installer/lib/bedrock_state (bedrock_d/state.py:65).

**Code:** `bedrock_d/state.py:52`, `bedrock_d/vm/create.py:32`, `bedrock_d/orchestrator/sagas/rqlite_backend.py:25`, `installer/lib/agent_install.py:137`, `installer/lib/mgmt_install.py:83`
**Docs:** docs/codebase-rewrite-plan.md#3-3 (single module owns rqlite); bedrock_d/state.py:3-18

**Fix:** Document the intended dependency direction in bedrock-d-concept.md (today: installer/lib is the implementation, bedrock_d re-exports/wraps; rewrite target is the reverse). No code change needed for v1.0; just make the as-built direction explicit so it isn't mistaken for the rewrite-target direction. Confidence high.

#### T-09 ⚪ docs/reference/ports.md is stale: mgmt dashboard listed on 8080, references removed bedrock-mgmt.service and dead /run files
`low` · code-vs-doc · confidence: high

docs/reference/ports.md:30 lists 'FastAPI mgmt dashboard | 8080 | all IPs', but the dashboard binds 0.0.0.0:8443 (mgmt/app.py:5033) and 8080 is now weed-volume per storage-architecture.md:332. ports.md:26 scopes the section to 'the node running bedrock-mgmt.service' — that unit no longer exists (replaced by bedrock-d.service per daemon-unification.md). ports.md:30 also cites /run/bedrock/physical_topology.json caching, but daemon-unification.md:77 says those /run/bedrock/*.json IPC files 'die'. No entry exists for 8443 (real dashboard) or 8001 (loopback CLI). This also contradicts storage-architecture.md which assigns 8080 to weed-volume (doc-vs-doc).

**Code:** `docs/reference/ports.md:26`, `docs/reference/ports.md:30`, `mgmt/app.py:5033`, `installer/configs/bedrock-d.service`
**Docs:** docs/reference/ports.md; docs/storage-architecture.md#port-map; docs/daemon-unification.md (What dies)

**Fix:** Refresh docs/reference/ports.md: dashboard=8443, loopback CLI=8001, weed-volume=8080; drop bedrock-mgmt.service and /run/bedrock topology cache references. Confidence high.

#### T-10 ⚪ Broken doc reference: cluster_arbiter and 6 other live modules cite nonexistent docs/post-alpha-rewrite-notes.md
`low` · code-vs-doc · confidence: high

installer/lib/cluster_arbiter.py:3 ('Per docs/post-alpha-rewrite-notes.md D-04..D-08') and six other production modules (rqlite_setup.py, seaweedfs.py, tier_storage.py, netd.py, mgmt/orchestrator.py, plus tests/test_netd_phase_a.py) cite docs/post-alpha-rewrite-notes.md, which does not exist in docs/. These are the load-bearing arbiter/rqlite-mobility design references (D-04..D-08 cited inline), so the authoritative rationale for the .254/lo arbiter design is unreachable. The arbiter design itself is also flagged in cluster-quorum-spec.md and MEMORY (feedback_arbiter_owner_rust) as in flux.

**Code:** `installer/lib/cluster_arbiter.py:3`, `installer/lib/rqlite_setup.py`, `installer/lib/seaweedfs.py`, `installer/lib/tier_storage.py`, `installer/lib/netd.py`, `mgmt/orchestrator.py`
**Docs:** docs/cluster-quorum-spec.md; docs/quorum-design-notes.md

**Fix:** Either restore docs/post-alpha-rewrite-notes.md or repoint these citations to the surviving spec (docs/cluster-quorum-spec.md / docs/quorum-design-notes.md) where D-04..D-08 content now lives. Confidence high that the referenced file is missing.

#### T-11 ⚪ Legacy procedural cluster_init/node_join/node_leave bodies retained as full duplicates of the saga path
`low` · duplication · confidence: high

mgmt_install.install_full and agent_install.install default to the bedrock_d sagas (BEDROCK_INIT_SAGA!='0', the default) but keep the entire old procedural implementation below as a BEDROCK_INIT_SAGA=0 opt-out (mgmt_install.py:513 lines, agent_install.py:482 lines — most of which is the legacy body). Both files self-document this as 'preserved … for one release while the saga path bakes … deleted once the saga path passes a clean testbed e2e + 0.8-beta tag.' So this is intentional transitional duplication, not a bug, but the two implementations must stay behaviorally identical until the opt-out is removed — a known drift hazard given the ISO-payload drift lessons (L iso_payload_drift).

**Code:** `installer/lib/mgmt_install.py:61-130`, `installer/lib/agent_install.py:122-170`, `bedrock_d/install/cluster_init.py`, `bedrock_d/install/node_join.py`
**Docs:** docs/sagas/README.md#when-sagas-run; docs/codebase-rewrite-plan.md

**Fix:** Track removal of the legacy procedural bodies as a 0.8-beta cleanup task; until then add a one-line 'KEEP IN SYNC WITH bedrock_d/install/* until 0.8-beta' marker so a future editor doesn't fix a bug in only one path. Confidence high (this is acknowledged transitional code, flagged for completeness).

### State files & rqlite

_The state-store rework largely landed correctly: there is exactly ONE local cluster-state file (`/etc/bedrock/state.json`, atomic tmp+rename in installer/lib/state.py with a defensive empty-write trap), no `master-state.json` or competing cluster-state file, and `cluster.json` was correctly demoted from a live projection to a write-once install-time bootstrap artifact (the runtime projection `view_builder.rebuild()` is deleted; consumers read rqlite via `cluster_state.load_cluster()` at level='none', and the no-quorum recovery + VM pre-start safety paths correctly escalate to level='strong'). The four "state" modules are NOT true duplicates — they have distinct roles (state.py=state.json I/O, bedrock_state.py=rqlite mutators, state_shared.py=in-process daemon state, cluster_state.py=rqlite read facade, bedrock_d/state.py=thin re-export), though the layering is confusing and several stale doc/comment references to cluster.json remain. The most material gap is the DRBD-UUID-history store: the design (decision #5/#6, quorum-design-notes.md) specifies a 7-day-retained rqlite UUID-history table (uuid, ts_set, ts_superseded) plus a per-node UUID-history field in state.json, but neither exists — only a single current-value `current_uuid` column on `drbd_resources` is implemented. INV-5 exact-equality and INV-6 (no rqlite on the takeover critical path) hold in the per-VM failover code; the arbiter/tier-critical UUID is never recorded to rqlite at all. Several docs (notably state-flow.md) still describe the superseded HMAC/blessed-master/15s-holddown witness model even though the AEAD-slot rework has landed in code._


#### ST-01 🟠 DRBD UUID-history table (7-day retention) specified but not implemented; only a single current-value column exists
`high` · missing-impl · confidence: high

The design calls for a replicated rqlite UUID-history table holding (uuid, ts_set, ts_superseded) with 7-day retention, plus a per-node local DRBD-UUID-history observation log (in state.json). The schema instead has only `drbd_resources.current_uuid` + `uuid_ts_set` — a single current value per resource, no superseded-timestamp, no history rows, no retention/pruning. `drbd_resource_uuid_set` (bedrock_state.py:702) does an in-place UPDATE that overwrites the prior UUID with no history. No code writes a uuid-history field to state.json (grep found zero). Consequence: the documented history-chain tie-break ('the node whose current UUID is NOT in the other's history chain promotes', quorum-design-notes.md:379-381) and replay-on-rqlite-came-back-online backlog (lines 326-335) have no data structure to operate on; the cold-boot per-node fallback (mirror #3, lines 357-361) is absent. The implemented exact-equality check is a strictly weaker mechanism than the documented history-chain comparison.

**Code:** `installer/lib/bedrock_schema.sql:285-311`, `installer/lib/bedrock_state.py:702-725`, `bedrock_d/vm/failover.py:115-160`
**Docs:** docs/quorum-design-notes.md:326-366 (UUID-history backlog / rqlite history table (uuid,ts_set,ts_superseded) keeps 7 days); docs/quorum-design-notes.md:35-36 (DRBD UUID history list as a state.json field capped at 7 days); design decision #5 (UUID history table in rqlite retained 7 days)

**Fix:** Either implement the history table + state.json history field as designed, or update quorum-design-notes.md / decision #5 to record that v1.0 ships with a single current-UUID column and exact-equality only (and that the history-chain tie-break + backlog replay are deferred). Low-cost path: add a comment in bedrock_schema.sql near drbd_resources noting the history table is deferred.

#### ST-02 🟡 Arbiter / tier-critical DRBD current-UUID is never recorded to rqlite, despite being the documented authority for master takeover
`medium` · missing-impl · confidence: medium

`record_uuid_after_promote` / `drbd_resource_uuid_set` are wired only into the per-VM failover path (bedrock_d/orchestrator/vm_failover.py and bedrock_d/vm/failover.py). The cluster-singleton 'tier-critical' resource is read for its current-UUID (cluster_arbiter.py:574, into the witness slot marker) but is never written into a `drbd_resources` row, so rqlite carries no record of the arbiter LUN's UUID. The 'rqlite must agree with the heartbeat' authority model (decision #6) therefore has no rqlite side for the arbiter resource — the witness-slot marker is the only mirror. This is consistent with INV-6 (rqlite off the takeover critical path) for the realtime takeover, but the documented 'rqlite must agree' cross-check and forensic record are absent for the cluster singleton.

**Code:** `installer/lib/cluster_arbiter.py:574 (reads tier-critical current-uuid only)`, `installer/lib/bedrock_state.py:702-725`, `bedrock_d/vm/failover.py:138-160 (record_uuid_after_promote used only by per-VM failover)`
**Docs:** docs/state-flow.md:263-265 (new master records tier-critical DRBD current-UUID and updates its witness slot); docs/quorum-design-notes.md:345-366 (master heartbeats tier-critical current UUID; rqlite must agree; heartbeat may lead but never lag); design decision #6 (DRBD-UUID authority: master heartbeats arbiter-DRBD-LUN current-uuid; rqlite must agree)

**Fix:** Confirm whether the witness-slot marker is intended to be the sole arbiter-UUID authority for v1.0 (in which case update quorum-design-notes.md decision #6 to scope the rqlite mirror to per-VM resources only), or wire promote_to_arbiter_host to call record_uuid_after_promote('cluster'/'tier-critical') so the arbiter resource gets a drbd_resources row.

#### ST-03 🟡 state-flow.md still documents the superseded HMAC / blessed-master / 15s-holddown witness model that the AEAD-slot rework replaced in code
`medium` · doc-vs-doc · confidence: high

cluster-quorum-spec.md is the TARGET design and its 'What this spec replaces' table (lines 190-205) explicitly says the OLD code used HMAC + an active blessed_master witness with a 15s claim-holddown, to be replaced by AEAD slots + a passive K/V store. The rework HAS landed in installer/lib/witness.py (ChaCha20-Poly1305 AEAD, per-node Slot ownership, TAG_LMS, no blessed_master/claim logic). But docs/state-flow.md — which is in the authoritative doc set and within the state dimension's read list — still describes cluster.key as an HMAC key signing 'claims' (line 416), an active witness that 'records current authority' and 'rejects the second claim' with a '15 s holddown' (lines 309-312, 338-340). This is a doc-vs-doc contradiction: state-flow.md contradicts cluster-quorum-spec.md and the shipped code. Note state-flow.md:268 ALSO correctly says 'The witness has NO concept of blessed master', so the doc is internally inconsistent.

**Code:** `installer/lib/witness.py:48-103 (AEAD ChaCha20Poly1305 slots, TAG_LMS, per-node slot ownership — rework landed)`
**Docs:** docs/state-flow.md:415-416 ('cluster.key — 32-byte HMAC key ... signs witness probes/heartbeats/claims'); docs/state-flow.md:338-340 ('Witness 15 s holddown rejects the second claim'); docs/state-flow.md:294,309-312 ('witness records current authority ... refuses for the holddown window'); docs/cluster-quorum-spec.md:190-205 ('What this spec replaces': HMAC->AEAD, active blessed_master->passive K/V slot store)

**Fix:** Rewrite the witness-claim passages in state-flow.md (lines 294, 309-312, 338-340, 415-416) to the AEAD passive-slot model: cluster.key is an AEAD key, no 'claims'/'holddown'/'blessed master'; takeover is gated by each node verifying slot[M].marker == local drbdadm current-uuid plus LMS/staleness interpretation per cluster-quorum-spec.md.

#### ST-04 ⚪ Dead CLUSTER_JSON / CLUSTER_FILE path constants in 7 runtime modules (cluster.json projection removed but constants left behind)
`low` · dead-code · confidence: high

After the 2026-05-26 cluster.json-projection removal, these modules still define a module-level `CLUSTER_JSON`/`CLUSTER_FILE = Path('/etc/bedrock/cluster.json')` constant but no code references it (verified by grep — each grep returned only the definition line). The actual reads were migrated to cluster_state.load_cluster()/rqlite. The only legitimately-used cluster.json paths are rqlite_setup.py:104,210 (bootstrap-time env render default param) and tier_storage.py:1858-1859 (unlink on node leave). Leftover constants are harmless but misleading — a reader could assume these modules still consume the file.

**Code:** `installer/lib/netd.py:114`, `installer/lib/seaweedfs.py:80`, `installer/lib/cluster_arbiter.py:63`, `mgmt/app.py:121`, `mgmt/victoria.py:20`, `mgmt/backup.py:44`, `mgmt/orchestrator.py:50`
**Docs:** installer/lib/view_builder.py:393-397 (rebuild DELETED; consumers query rqlite directly); installer/lib/cluster_state.py:15-20 (no projection layer; cluster.json gone)

**Fix:** Delete the unused CLUSTER_JSON/CLUSTER_FILE constants from netd.py, seaweedfs.py, cluster_arbiter.py, mgmt/app.py, mgmt/victoria.py, mgmt/backup.py, mgmt/orchestrator.py. Keep the bootstrap usage in rqlite_setup.py and the cleanup in tier_storage.py.

#### ST-05 ⚪ Stale doc/comment references describe cluster.json as a live per-revision projection that no longer exists
`low` · code-vs-doc · confidence: high

state-flow.md and 01-rqlite-state-store.md still present cluster.json as a live projection refreshed every rqlite revision, and describe view_builder.rebuild() as an active writer. In code, rebuild() is deleted (view_builder.py:393-397), the orchestrator only projects state.json inline (orchestrator.py:225-242 comment correctly acknowledges 'cluster.json is no longer written'), and cluster.json is written only once at install/bootstrap (mgmt_install.py:253, agent_install.py:283, cluster_init.py:306) for rqlite_setup's env render. The orchestrator.py:170-172 docstring contradicts its own body comment. These are doc/comment drift, not behavioral bugs.

**Code:** `mgmt/orchestrator.py:170-172 (docstring still says 'project to cluster.json + state.json')`, `installer/lib/state.md:5-9 (state.json 'projected view ... refreshed by rqlite_subscriber')`
**Docs:** docs/state-flow.md:18-21 (orchestrator 'projects to /etc/bedrock/{cluster,state}.json'); docs/state-flow.md:90-95,411-413 (cluster.json 'projected cluster state ... refreshed by orchestrator every revision'); docs/01-rqlite-state-store.md:89-91 (view_builder.rebuild() 'writing the dict to /etc/bedrock/cluster.json + this node's state.json')

**Fix:** Update docs/state-flow.md and docs/01-rqlite-state-store.md to state cluster.json is a write-once bootstrap artifact (not a live projection), remove the rebuild() description, and fix the orchestrator.py:170-172 docstring + state.md:5-9 to say only state.json is projected on each revision.

#### ST-06 ⚪ state_shared.py claims mesh/switch /run JSON files were replaced by in-memory views, but netd still writes them and mgmt scrapes them
`low` · inconsistency · confidence: medium

state_shared.py's docstring presents netd_status_view as having replaced the on-disk /run/bedrock/mesh_neighbors.json. In reality netd still writes both /run/bedrock/switch_neighbors.json and mesh_neighbors.json every tick (netd.py:1380-1381) because the mgmt master cross-node physical-topology rollup SSH-cats them from peers (app.py:311-312). The replacement is only true for the LOCAL node's /api/mesh read path. These are transient tmpfs topology caches explicitly documented as 'NOT replicated, NOT folded into cluster.json' (netd.py:2537-2542), so they do NOT violate the one-cluster-state-file rule — but the state_shared.py docstring overstates the migration.

**Code:** `installer/lib/state_shared.py:99-107 (netd_status_view 'replaces reading /run/bedrock/mesh_neighbors.json from disk')`, `installer/lib/netd.py:1380-1381 (still calls write_switch_state_file + write_mesh_state_file every tick)`, `mgmt/app.py:311-312 (mgmt SSH-scrapes peers' /run/bedrock/{switch,mesh}_neighbors.json)`
**Docs:** docs/daemon-unification.md (unified daemon replaces /run/bedrock/*.json file IPC with in-process shared state)

**Fix:** Tighten the state_shared.py:99-107 docstring to say netd_status_view replaces the LOCAL on-disk read for this node's dashboard endpoints; the /run files persist for the master's cross-node SSH scrape until that path is also migrated to a peer API.

### Storage / DRBD / SeaweedFS / tiers

_The storage subsystem has two parallel, mutually incompatible implementations and neither fully matches the authoritative docs. installer/lib/tier_storage.py implements a per-TIER abstraction (scratch/bulk/critical with LVs named tier-<tier>) that is the only path actually wired into init/join/promote sagas. bedrock_d/vm/{lvm,drbd_config}.py + bedrock_d/install/cluster_tier.py implement the per-RESOURCE model the docs describe (LVs named bedrock-data-<r>/bedrock-meta-<r>), and the per-VM half of that tree (bedrock_d/vm/create.py) is the live VM-create path, while the cluster-tier half delegates straight back into tier_storage's tier-critical resource. The result is a naming/port/layout split where the docs (one thinpool, bedrock-vg, bedrock-data-cluster, ports 7700-7799) match neither tree consistently: the VG is named "bedrock" everywhere in code (not bedrock-vg), DRBD ports use three different bases (7000+/7700+/7789+) that collide with weed-master:9333, and the cluster singleton is "tier-critical" in the live path but "cluster" in the bedrock_d saga module. SeaweedFS code largely follows storage-architecture.md (volume+s3 everywhere, deterministic odd master subset, leveldb3 filer on the critical DRBD volume, three collections), with a few drift points. TRIM/discard passdown is documented end-to-end but only verified at runtime, never configured by the installer._


#### SG-01 🟠 Two incompatible DRBD LV naming/layout models coexist; only the per-tier one is wired into the live cluster path
`high` · code-vs-doc · confidence: high

storage-architecture.md (LVM layout block, lines 36-45 and rqlite schema lines 347-357) prescribes per-resource LVs named `bedrock-data-<resource>` + `bedrock-meta-<resource>` for BOTH the cluster singleton (`bedrock-data-cluster`) and every VM disk. bedrock_d/vm/lvm.py:lv_names_for() implements exactly that. But the LIVE cluster-tier path used by init/join is installer/lib/tier_storage.py, which names LVs `tier-<tier>` + `tier-<tier>-meta` and abstracts storage as three TIERS (scratch/bulk/critical) — a model that does not appear in storage-architecture.md at all (the doc explicitly rejects per-tier thinpools, lines 52-56). The cluster_tier saga (bedrock_d/install/cluster_tier.py) is a thin wrapper that calls tier_storage.transition_to_n2_master, so the documented `bedrock-data-cluster`/`bedrock-meta-cluster` LVs are never created — the singleton lives on `tier-critical`/`tier-critical-meta` instead. The two trees can never interoperate on the cluster singleton.

**Code:** `installer/lib/tier_storage.py:163-167`, `installer/lib/tier_storage.py:886-944`, `bedrock_d/vm/lvm.py:55-66`, `bedrock_d/vm/drbd_config.py:48-107`, `bedrock_d/install/cluster_tier.py:104-200`
**Docs:** docs/storage-architecture.md#lvm-layout-per-node; docs/storage-architecture.md#rqlite-schema-storage-related-tables

**Fix:** Pick one model and delete the other. Given the docs and the per-VM live path both use bedrock-data-<r>/bedrock-meta-<r>, migrate the cluster-tier live path off tier_storage's tier-critical naming, or update storage-architecture.md to document the tier-* scheme as the real one. Confidence high that they diverge; which to keep is Tommy's call.

#### SG-02 🟠 DRBD port allocation uses three different bases across trees; resulting ports fall outside the documented 7700-7799 range and collide with weed-master:9333
`high` · inconsistency · confidence: high

storage-architecture.md port map (line 338) documents DRBD as `7700-7799 per-link IP`. No code path produces ports in that range. tier_storage uses 7000+minor → cluster-singleton minor 1101 = port 8101. bedrock_d/vm/drbd_config uses 7700+minor; with VM minors 1200-1899 (create.py VM_MINOR_BASE/MAX) that yields ports 8900-9599 — which overlaps weed-master 9333 (storage-architecture.md line 331) and weed-master-grpc 19333 is safe but 9333 is squarely inside 8900-9599. Legacy installer/lib/vm.py uses 7789+minor (minors 1-99 → 7790-7888). So a cluster-singleton DRBD resource and a per-VM DRBD resource computed by the two trees would never agree on a port even for the same minor, and the per-VM range is on a collision course with the SeaweedFS master HTTP port.

**Code:** `installer/lib/tier_storage.py:904`, `installer/lib/tier_storage.py:1035`, `bedrock_d/vm/drbd_config.py:28-36`, `bedrock_d/vm/create.py:41-43`, `installer/lib/vm.py:255`
**Docs:** docs/storage-architecture.md#port-map-one-place

**Fix:** Unify on one base and confirm the chosen VM-minor range maps entirely below 9333 (or move weed-master off 9333). Update the port-map table to the real range. The 9333 collision is the load-bearing risk. Confidence high on the arithmetic; whether 9333 actually binds inside that window depends on minor allocation in practice (high that it can).

#### SG-03 🟡 VG is named "bedrock" in all code but "bedrock-vg" in BEDROCK.md and storage-architecture.md
`medium` · code-vs-doc · confidence: high

BEDROCK.md line 27 and the storage section (lines 46-47) and storage-architecture.md TL;DR explicitly name the VG `bedrock-vg`. Every code constant is `bedrock`: tier_storage.DEFAULT_VG='bedrock', lvm.VG_NAME='bedrock', vm.VG_NAME='bedrock'. The cluster_tier.py docstring example recipe even writes `bedrock-vg/bedrock-data-cluster` (lines 43-48) while the lvm helpers it indirectly relies on use `bedrock`. Note tier_storage also auto-ADOPTS whatever VG the OS installer made (typically `almalinux`), so the running VG name is often neither `bedrock` nor `bedrock-vg`. Any operator doc, snapshot recipe, or hardcoded `/dev/bedrock-vg/...` path will be wrong.

**Code:** `installer/lib/tier_storage.py:89`, `bedrock_d/vm/lvm.py:32`, `installer/lib/vm.py:20`, `bedrock_d/install/cluster_tier.py:43-48`
**Docs:** BEDROCK.md#storage-architecture-critical-decisions; docs/storage-architecture.md#tldr

**Fix:** Make BEDROCK.md/storage-architecture.md say `bedrock` (matching code) or rename the constant to `bedrock-vg`. Also fix the misleading `bedrock-vg/...` paths in cluster_tier.py's docstring recipe. Confidence high.

#### SG-04 🟡 Cluster-singleton DRBD resource has two different names ("tier-critical" vs "cluster") between the live arbiter path and the bedrock_d saga module
`medium` · code-vs-arch · confidence: high

cluster_arbiter.py (the live owner of .254 + the singleton mount) hardcodes TIER_RESOURCE='tier-critical' and operates DRBD resource `tier-critical` at /var/lib/bedrock/cluster. bedrock_d/install/cluster_tier.py's module docstring describes the resource as `bedrock-data-cluster`/`bedrock-meta-cluster` (lvm.lv_names_for('cluster')), matching the doc. Because cluster_tier delegates to tier_storage.transition_to_n2_master('critical'), the actual on-disk resource is `tier-critical`, NOT `cluster`. The bedrock_d naming for the singleton is therefore dead/aspirational. A reviewer trusting cluster_tier.py would look for the wrong resource name in drbdadm status.

**Code:** `installer/lib/cluster_arbiter.py:53`, `installer/lib/tier_storage.py:165-167`, `bedrock_d/install/cluster_tier.py:5-8`, `bedrock_d/vm/lvm.py:55-66`
**Docs:** docs/storage-architecture.md#254-32-cluster-singleton

**Fix:** Reconcile the singleton resource name to one value across cluster_arbiter, tier_storage, and the cluster_tier docstring. Confidence high.

#### SG-05 🟡 Documented rqlite storage schema (tier_critical_membership, seaweed_master_membership, thinpool tier-fast/tier-bulk) is largely unimplemented
`medium` · missing-impl · confidence: high

storage-architecture.md (rqlite schema lines 359-369) defines tier_critical_membership and seaweed_master_membership tables that the orchestrator is supposed to own, and the drbd_resources.thinpool column comment lists `tier-fast`/`tier-bulk` values. Grep across installer/lib, bedrock_d, and mgmt shows NO code creates or writes tier_critical_membership; seaweed_master_membership is only mentioned in code comments (seaweedfs.py:249, 459) as a 'will eventually own this' fallback — the actual master set is computed by the deterministic lowest-octet rule, never read from rqlite. No `tier-fast`/`tier-bulk` thinpool exists anywhere (only the single `thinpool` plus the tier-* LV names). The documented calm-orchestrator promotion of a non-arbiter node into the arbiter set (doc lines 96-108, 201-202) has no implementing code.

**Code:** `installer/lib/seaweedfs.py:246-262`, `installer/lib/seaweedfs.py:448-465`, `installer/lib/tier_storage.py:1147`
**Docs:** docs/storage-architecture.md#rqlite-schema-storage-related-tables; docs/storage-architecture.md#cluster-singleton-drbd-resource-is-capped-at-3-peers

**Fix:** Either implement the membership tables + arbiter-set promotion loop, or mark these as post-v1.0 in storage-architecture.md so the doc stops reading as shipped. The arbiter-set promotion gap is the operationally significant one (degraded redundancy never self-heals). Confidence high on the absence.

#### SG-06 🟡 TRIM/discard passdown is documented end-to-end but never configured by the installer (only checked at runtime)
`medium` · missing-impl · confidence: medium

BEDROCK.md (lines 69-71) and storage-architecture.md promise TRIM/discard end-to-end: guest FS → QEMU discard=unmap → DRBD discard-zeroes-if-aligned → LVM thin thin_pool_discards=passdown → NVMe. Only two of four layers are set in code: fstab mounts use `discard` (tier_storage) and the libvirt XML uses discard='unmap' (vm.py:413). NO code sets LVM `thin_pool_discards=passdown` in /etc/lvm/lvm.conf, and `lvcreate -T` is called without it (tier_storage.ensure_thinpool:694) — passdown IS the LVM default, but the doc lists it as an explicit requirement. The DRBD `disk` sections (drbd_config.py:94, tier_storage render) do NOT set the documented `discard-zeroes-if-aligned`/rs-discard-granularity options. mgmt/routes_support.py:35-74 only AUDITS passdown at runtime and even treats the default as OK — so a node where an operator disabled passdown would silently break the chain with nothing to re-enforce it.

**Code:** `installer/lib/tier_storage.py:694`, `installer/lib/vm.py:413`, `bedrock_d/vm/drbd_config.py:94-107`, `mgmt/routes_support.py:35-74`
**Docs:** BEDROCK.md#storage-architecture-critical-decisions; docs/storage-architecture.md#tldr

**Fix:** Have the installer explicitly assert/set thin_pool_discards=passdown and add the DRBD discard options to the rendered .res, or soften the doc to 'relies on LVM/DRBD defaults'. Confidence medium (passdown default makes the LVM gap mostly cosmetic; the missing DRBD discard option is real).

#### SG-07 🟡 Per-VM DRBD uses internal metadata in the live legacy path, contradicting the locked external-metadata design
`medium` · code-vs-doc · confidence: high

storage-architecture.md (DRBD per-resource layout, lines 58-69) and BEDROCK.md (lines 52-53) mandate ONE thin data LV + ONE thin meta LV per DRBD resource (external metadata). bedrock_d/vm/drbd_config.py implements external meta correctly. But installer/lib/vm.py — still the live path for several operations (its _drbd_2way_conf/_drbd_3way_conf at lines 329-410 emit `on` blocks with NO meta-disk line, i.e. internal metadata) — records `meta_lv=''` in drbd_resources (vm.py:194-197) and its own comment admits 'Legacy pet/vipet path uses internal DRBD metadata (no separate meta LV)' (vm.py:183-184). So VMs created via the legacy path violate the one-thin-meta-LV-per-resource invariant. drbd_config.py notes the two shapes 'coexist fine', but the doc presents external meta as the single locked design.

**Code:** `installer/lib/vm.py:183-197`, `installer/lib/vm.py:329-357`, `bedrock_d/vm/drbd_config.py:1-19`, `bedrock_d/vm/create.py:147-176`
**Docs:** docs/storage-architecture.md#drbd-per-resource-layout

**Fix:** Confirm whether installer/lib/vm.py is still reachable for VM create (bedrock_d/vm/create.py:218-220, 256-258 still import lib.vm helpers, so it is partly live). Migrate VM create fully onto bedrock_d/vm/drbd_config or document the internal-meta legacy exception in storage-architecture.md. Confidence high that the legacy path uses internal meta; medium on how often it's still invoked.

#### SG-08 ⚪ Dead/no-op state plumbing in tier_storage: cluster.json log-append, save_cluster shim, and drbd_node_id persistence to a removed file
`low` · dead-code · confidence: high

Per MEMORY (cluster.json deleted 2026-05-26, replaced by rqlite reads). tier_storage still carries substantial dead scaffolding around it: _log_append_typed (lines 204-228) is documented as a no-op stub returning None for all callers; save_cluster (275-283) is an explicit no-op shim; get_drbd_node_id/free_drbd_node_id (819-883) still call load_cluster()+save_cluster() and mutate an in-memory dict whose save_cluster is a no-op, then separately mirror to bedrock_state — so the cluster.json half is pure dead code. The module-level comment block (lines 173-228) still describes the obsolete bedrock-rust IPC log pipeline as if live ('When the bedrock-rust daemon IPC socket exists...'), but bedrock-rust was deleted (MEMORY lesson_python_failover_stack). drbd_remove_peer:1514 still reads drbd_node_ids out of load_cluster() which now sources rqlite, so it works, but the save path is dead.

**Code:** `installer/lib/tier_storage.py:204-228`, `installer/lib/tier_storage.py:275-283`, `installer/lib/tier_storage.py:819-883`

**Fix:** Delete the no-op save_cluster/cluster.json mutation halves and the bedrock-rust IPC comment block; keep only the bedrock_state mirror. Low severity (functionally inert) but it actively misleads readers about where DRBD node-id state lives. Confidence high.

#### SG-09 ⚪ SeaweedFS weed-master Raft set can be even-numbered at N=4 (3) but the doc's 'lowest-octet 3' wording and N-scaling table need reconciling; filer comments still cite SQLite
`low` · code-vs-doc · confidence: medium

storage-architecture.md says weed-master is a fixed Raft-3 across the lowest-octet 3 nodes (line 256, table line 246). seaweedfs._master_set() caps at 3 (correct) but write_master_config()._master subset logic (lines 184-194) computes a DIFFERENT 'largest odd ≤ N' rule (N=5 → 5 masters), which contradicts both _master_set (caps at 3) and the doc's fixed-3 design. So two functions in the SAME file disagree on the master count for N≥4. Separately, the module header (seaweedfs.py:14-24) and write_filer_config still describe a 'SQLite metadata DB' in places (header line 16-18 says 'SQLite metadata DB. Single instance on the master') while the actual config writes leveldb3 (line 238) — the doc and the rest of the code correctly say leveldb3, so the header is stale.

**Code:** `installer/lib/seaweedfs.py:184-194`, `installer/lib/seaweedfs.py:246-262`, `installer/lib/seaweedfs.py:1-25`
**Docs:** docs/storage-architecture.md#seaweedfs-topology

**Fix:** Make write_master_config() use _master_set() (cap 3) so the two functions agree and match the doc; fix the stale 'SQLite' wording in the module header. Confidence medium on the master-count divergence being live (write_master_config peers_arg is only logged, not obviously consumed — verify whether master.toml peer list is actually used vs the env-file SEAWEED_MASTER_PEERS from _master_set).

#### SG-10 ⚪ node_reset_local hardcodes VG 'bedrock' for LV removal, ignoring the adopted-VG path
`low` · unsafe-invariant · confidence: high

detect_vg()/ensure_vg() establish that the real VG is usually the adopted OS VG (e.g. `almalinux`), stored in storage.json, and the module VG global reflects that. But node_reset_local (lines 1834-1837) hardcodes `lvremove -fy bedrock/{lv}` instead of using the resolved VG variable. On a node whose VG was adopted as `almalinux`, reset would silently fail to remove the tier LVs (the `bedrock/...` path doesn't exist), leaving stale tier-critical/tier-bulk/tier-scratch LVs behind — so a subsequent `bedrock init`/`join` could collide with leftover data LVs. The surrounding service/mount/fstab cleanup is correct; only the lvremove uses the wrong VG.

**Code:** `installer/lib/tier_storage.py:1834-1837`, `installer/lib/tier_storage.py:115-145`

**Fix:** Use the resolved VG (f"{VG}/{lv}") in node_reset_local's lvremove loop, matching the rest of the module. Confidence high on the hardcode; medium on real-world impact since testbed VGs are often literally `bedrock`.

### Sagas / orchestrator

_The saga engine (executor + rqlite/file backends) is solid and matches its documented contract: intent->idempotent steps->durable progress, with a clean resume model and per-step crash safety. The five install/cluster sagas (cluster_init, node_join, node_leave, cluster_tier_promote_master/join_peer, cluster_rename) each have a matching docs/sagas file and the step lists largely agree. However, there are two correctness defects that break documented contracts: (1) node_leave calls view_builder.rebuild(), a function that was deliberately DELETED, so the leave saga AttributeErrors at its first and last steps; (2) the BEDROCK.md "power-loss recoverable on boot" guarantee is not wired — SagaExecutor.resume_in_flight() is never called by bedrock-d, so a crashed runtime saga is never auto-resumed. Several doc<->code contradictions exist around node_leave's validate_target preconditions and the leftover cluster.json projection layer in the saga README. The cluster_tier_join_peer logic is duplicated (inline in node_join + standalone saga) with divergent data sources._


#### SA-01 🔴 node_leave saga calls deleted view_builder.rebuild() — crashes at validate_target and verify_membership_drop
`critical` · code-vs-doc · confidence: high

node_leave.py step_validate (line 75) does `from lib import view_builder as _vb; snapshot = _vb.rebuild(this_node=ctx['self_name'])` and step_verify (line 190-193) does the same. But view_builder.rebuild was explicitly DELETED — view_builder.py:393 carries a comment 'rebuild — DELETED. cluster.json projection layer was removed; every consumer now queries rqlite directly via cluster_state.load_cluster()'. The module now only exposes build_snapshot/empty_snapshot/fold_into. So node_leave's first step (validate_target) raises AttributeError before any membership change happens, and verify_membership_drop would too. The whole node_leave saga is non-functional via the saga path. This matches the L-series lessons concern that 'node leave must call rqlite /remove' (the saga IS designed to, in step_voter_remove) — but it dies before reaching that step. Confirmed by grep: no `rebuild` symbol or alias exists in view_builder.py.

**Code:** `bedrock_d/install/node_leave.py:74-76`, `bedrock_d/install/node_leave.py:190-193`, `installer/lib/view_builder.py:393-398`
**Docs:** docs/sagas/node_leave.md#step-overview

**Fix:** Replace both `_vb.rebuild(this_node=...)` calls with `cluster_state.load_cluster()` (level='none' read, the documented replacement) or `view_builder.build_snapshot()`. The returned dict still has a 'nodes' key so the downstream `.get('nodes')` logic is unchanged.

#### SA-02 🟠 Power-loss saga resume is documented but never wired: resume_in_flight() is never called by the daemon
`high` · missing-impl · confidence: high

The executor's resume_in_flight() (finds pending/in_progress ops for this node on boot and re-runs them) is the mechanism that delivers BEDROCK.md's core 'power-loss recoverable on boot' guarantee for runtime sagas. grep shows resume_in_flight is only ever called in tests (tests/test_saga_executor.py, tests/test_file_backend.py) — never in mgmt/orchestrator.py startup, never in bedrock_d daemon init, never in routes_operations. The orchestrator startup (mgmt/orchestrator.py:1007) launches cluster_tier_watcher + converge_retry + vm_failover tasks but no saga-resume sweep. There is also no /api/operations/{id}/retry endpoint (routes_operations only has submit/get/list), so the executor.retry() path the docs reference ('the operator can retry via POST /api/operations') has no HTTP surface either. Net effect: a bedrock-d crash mid vm_create / cluster_tier_promote / node_leave leaves an in_progress op that nothing ever picks back up.

**Code:** `bedrock_d/orchestrator/sagas/executor.py:310-331`, `mgmt/orchestrator.py:1007-1018`, `mgmt/routes_operations.py:97-127`
**Docs:** BEDROCK.md (line 15: 'Power-loss at any step is recoverable: on boot, pick up where the operation_steps log says we left off'); docs/sagas/README.md#conventions; bedrock_d/orchestrator/sagas/executor.py:8-18 (docstring contract)

**Fix:** Add a startup task in mgmt/orchestrator.py that builds a RqliteSagaBackend-backed SagaExecutor and calls resume_in_flight() once after rqlite is reachable (gated on having a leader so a crashed promote resumes). Separately, add a retry endpoint or document that retry is test-only. Minimal: one resume sweep at orchestrator boot.

#### SA-03 🟡 node_leave validate_target does not enforce the 'refuse if target is only voter / only mgmt-master' preconditions its doc promises
`medium` · code-vs-doc · confidence: high

node_leave.md states validate_target refuses if 'target is the ONLY voter (would leave the cluster with no Raft quorum)' and 'target is the only mgmt-master'. The actual step_validate only checks two things: target != self_name, and target exists in the snapshot. There is no quorum-safety check and no mgmt-master check. Given this is a safety-over-availability platform ('only the paranoid survive'), leaving a node that is the sole remaining voter would brick Raft — exactly the bricking scenario lesson_node_leave_rqlite_remove warns about, but the guard the doc claims exists is absent. (Note: step_voter_remove correctly issues the /remove that the lesson requires; the gap is the missing pre-flight refusal.)

**Code:** `bedrock_d/install/node_leave.py:64-88`
**Docs:** docs/sagas/node_leave.md#validate_target (line 39, 72-80)

**Fix:** Add the documented preconditions to step_validate: count live voters/nodes in the snapshot and raise if removing the target would drop below quorum, and refuse if target == current mgmt_master. Or, if the design changed, update node_leave.md to drop those claims.

#### SA-04 🟡 cluster_tier_join_peer logic is duplicated (inline in node_join + standalone saga) with divergent state sources
`medium` · duplication · confidence: high

node_join.step_cluster_tier_join_peer (456-525) and the standalone ClusterTierJoinPeer saga (cluster_tier.py:208-278) both wait for tiers.critical.mode==drbd then call tier_storage.transition_to_n2_peer with a rebuilt peer list. They are near-identical but DIVERGE in their source of truth: the inline node_join version reads /etc/bedrock/cluster.json directly (json.loads of the file at line 475-484) while the standalone saga reads via cluster_state.load_cluster() (rqlite, line 72-77, the post-cluster.json-deletion path). Per memory, cluster.json is still projected so both work today, but the two code paths can drift (different timeout: inline=120s hardcoded vs standalone wait_timeout_s param; different peer-list assembly). The docs explicitly flag this as a known duplication but it remains a maintenance hazard and an inconsistency in which durable store is authoritative.

**Code:** `bedrock_d/install/node_join.py:456-525`, `bedrock_d/install/cluster_tier.py:208-278`
**Docs:** docs/sagas/cluster_tier_join_peer.md (lines 15-21 acknowledge the duplication); docs/sagas/node_join.md#cluster_tier_join_peer (line 232-247)

**Fix:** Extract a single helper (e.g. tier_storage.join_critical_as_secondary(self, master, cluster)) and have both the inline node_join step and the standalone saga call it, reading cluster state from one source (load_cluster). Removes drift in timeout + peer-list logic.

#### SA-05 ⚪ Saga README and init docs still reference the deleted cluster.json projection / 'overwrites this once rqlite is up' as live mechanism
`low` · doc-vs-doc · confidence: medium

Per the project memory, cluster.json was replaced 2026-05-26 by direct rqlite reads via cluster_state.load_cluster(); view_builder._cluster_view is what built cluster.json and rebuild() was deleted. Yet cluster_rename.md#propagation still lists 'cluster.json on every node | rqlite_subscriber -> view_builder._cluster_view' as the live propagation path, and cluster_init step write_bootstrap_cluster_json comments 'The orchestrator's rqlite-snapshot task overwrites this once rqlite is up' (cluster_init.py:283-285) while node_join write_bootstrap_cluster_json says 'the mgmt-side rqlite_subscriber overwrites this from canonical rqlite state' (node_join.py:233). It is unclear post-deletion whether anything still writes /etc/bedrock/cluster.json — the sagas (cluster_tier.py, node_join inline) still READ it, so a writer must still exist, but the documented writer (view_builder projection) was removed. This is an internal-consistency / doc-accuracy gap that obscures which file is authoritative.

**Code:** `bedrock_d/install/cluster_init.py:280-307`, `bedrock_d/install/node_join.py:230-268`
**Docs:** docs/sagas/README.md (line 65 'state.json / cluster.json / rqlite'); docs/sagas/cluster_rename.md#propagation (cluster.json on every node via view_builder._cluster_view); docs/actions (general)

**Fix:** Verify whether cluster.json is still projected (and by what, post-view_builder.rebuild deletion). If yes, document the new writer in README/cluster_rename.md; if no, the saga reads of cluster.json (cluster_tier.py:_load_cluster falls back to load_cluster, but node_join.py:475 reads the file directly) are reading a stale/absent file and should switch to load_cluster().

#### SA-06 ⚪ FileSagaBackend full-file rewrite assumes single-writer; concurrent bootstrap sagas would clobber next_id/ops
`low` · unsafe-invariant · confidence: medium

FileSagaBackend rewrites the entire init-progress.json on every mutation via tmp+os.replace (crash-atomic for a single writer). cluster_init and node_join also bypass the backend with their own _update_op_params() which does an independent read-modify-write of the same file (cluster_init.py:212, node_join.py:638). There is no file lock. The atomicity comment justifies this as 'sagas are infrequent (1 init per cluster, 1 join per node)'. That holds for distinct kinds, but node_leave shares the SAME file (node_leave.py:46 comment 'Use the same progress file as init/join') and multiple node_leave ops for different targets, or a leave concurrent with a resumed join, could interleave read-modify-write and lose an op row (last-writer-wins on the whole-file replace). This mirrors lesson_orchestrator_atomic_write (concurrent fold writes raced and concatenated JSON) — same class of hazard, different file.

**Code:** `bedrock_d/orchestrator/sagas/file_backend.py:78-116`, `bedrock_d/install/cluster_init.py:207-218`, `bedrock_d/install/node_join.py:637-644`
**Docs:** bedrock_d/orchestrator/sagas/file_backend.py:39-45 (Atomicity note)

**Fix:** Either document and enforce that only one bootstrap saga touches init-progress.json at a time (a flock on the file in _write), or give node_leave its own progress file. Low severity because real-world concurrency here is rare, but it's an unguarded single-writer invariant on a safety-critical platform.

#### SA-07 ⚪ cluster_tier_watcher executes the saga inline (execute_one) but a crash between submit and completion has no resume path
`low` · inconsistency · confidence: medium

cluster_tier_watcher submits the promote saga and immediately runs it via asyncio.to_thread(ex.execute_one, op_id), then adds the key to an in-memory submitted_for set. If bedrock-d crashes mid-promote, the op is left in_progress in rqlite; on restart the in-memory submitted_for set is empty so the watcher re-evaluates, but it only re-submits when tier_mode != 'drbd' — and submits a NEW op rather than resuming the in_progress one (no resume_in_flight, finding #2). The doc says 'if the saga fails the operator can retry via POST /api/operations' but there is no retry endpoint (finding #2). So a power-loss during the N=1->N=2 promote relies on the watcher firing a fresh promote whose check_preconditions/transition_to_n2_master idempotency must absorb the half-done prior op — workable but undocumented and untested as the recovery path.

**Code:** `mgmt/orchestrator.py:929-946`
**Docs:** docs/sagas/cluster_tier_promote_master.md#trigger (lines 19-31); BEDROCK.md (line 15 power-loss recoverable)

**Fix:** Have cluster_tier_watcher (or the boot resume sweep) call resume_in_flight()/retry on any existing in_progress cluster_tier_promote_master op before submitting a new one, so the half-finished op is the one resumed. At minimum document that recovery is via a brand-new promote relying on transition_to_n2_master idempotency.

### VM lifecycle & failover

_The per-VM pet/vipet failover state machine (suspend T+20 / takeover T+35 / kill T+5min) is implemented in bedrock_d/orchestrator/vm_failover.py with pure helpers in bedrock_d/vm/failover.py, and the split between the two modules is clean (no logic duplication between them — the docstrings' division of labor holds). The timings in code match the project memory and the design intent. However several real gaps exist: (1) the suspended-vms.json kill record is never cleared when a VM is resumed on quorum-return, so a VM that recovers inside 5 minutes is still killed at T+5min; (2) the migrate operation exists in THREE divergent implementations (mgmt/app.py, bedrock_d/vm/migrate.py saga, installer/lib/vm.py) and the saga's --undefinesource breaks pet/vipet failback; (3) no migrate path records the post-promote DRBD UUID, which leaves drbd_resources.current_uuid stale and will cause is_safe_to_start_vm to REFUSE a later legitimate failover takeover; (4) failover and the bedrock_d create saga are hard-wired to a single disk0 while the mgmt plane and docs are fully multi-disk; (5) the design's transition-marker / all-zero-UUID mid-takeover step is not implemented anywhere. The pet/vipet/cattle model and adopt-already-paused logic are otherwise present and correct._


#### VM-01 🟠 Resumed VM is still killed at T+5min — suspended-vms.json never cleared on resume
`high` · unsafe-invariant · confidence: high

vm_failover._load_suspended_record docstring (line 111) explicitly states the record holds a VM 'until either resumed (we remove the entry) or killed at T0+5min'. But NO code path removes an entry on resume. The recovery path that resumes a VM is mgmt/orchestrator._reconcile_paused_vms (line 593-595: `virsh resume`), which does not touch /var/lib/bedrock/suspended-vms.json. So a pet/vipet VM that was suspended on a no-quorum blip and then resumed when quorum returned within 5 minutes keeps its record entry; kill_suspended_after_5min_task (line 549-554) will then `virsh destroy` the now-running, healthy VM at suspend_ts+5min. This is an availability/data-loss hazard precisely in the common 'short partition, node recovers' case the suspend mechanism is meant to protect.

**Code:** `bedrock_d/orchestrator/vm_failover.py:107-118`, `bedrock_d/orchestrator/vm_failover.py:533-569`, `mgmt/orchestrator.py:593-595`
**Docs:** bedrock_d/orchestrator/vm_failover.py docstring lines 28-31

**Fix:** Have the resume path (mgmt/orchestrator._reconcile_paused_vms) load the suspended record and pop any VM it resumes, then save. Alternatively, make kill_suspended_after_5min_task re-check `virsh domstate` and skip/evict any VM that is no longer 'paused' before destroying.

#### VM-02 🟠 Migrate paths never record post-promote DRBD UUID → later failover takeover is refused
`high` · code-vs-arch · confidence: high

Both live-migrate implementations run `drbdadm primary` on the target (bedrock_d/vm/migrate.py step_promote_target line 100-110; mgmt/app.py line 3462), which bumps the DRBD current-UUID. Neither path calls record_uuid_after_promote / drbd_resource_uuid_set to update drbd_resources.current_uuid in rqlite. The takeover path's pre-start safety is_safe_to_start_vm (bedrock_d/vm/failover.py:182-228) does an EXACT-equality compare (INV-5) between the local DRBD UUID and the rqlite-recorded UUID and REFUSES on mismatch. So after a migrate, the recorded UUID is stale relative to the new primary's; a subsequent failover of that VM would be refused with 'UUID mismatch', forcing manual operator reconcile — a silent regression in HA introduced by every migrate.

**Code:** `bedrock_d/vm/migrate.py:100-110`, `bedrock_d/vm/migrate.py:155-168`, `mgmt/app.py:3459-3473`, `bedrock_d/vm/failover.py:138-160`, `bedrock_d/vm/failover.py:182-228`
**Docs:** cluster-quorum-spec.md INV-5 (line 155); KEY DESIGN DECISION 6 (DRBD-UUID authority)

**Fix:** Add a step to both migrate paths (and ideally fold migrate into a single implementation) that calls record_uuid_after_promote(resource) on the new primary after the migrate succeeds, mirroring what the takeover path does. Per the design, the master/primary owner must heartbeat its current-uuid and rqlite must agree.

#### VM-03 🟠 Three divergent VM-migrate implementations; saga --undefinesource breaks pet/vipet failback
`high` · duplication · confidence: high

Migrate exists three times with materially different behavior. (a) mgmt/app.py:_vm_migrate is the live one the dashboard/CLI actually call (installer/lib/vm.py:migrate_vm just POSTs to /api/vms/{name}/migrate). It uses `virsh migrate --live --verbose --unsafe --migrateuri tcp://<loopback>` and does NOT undefine the source. (b) bedrock_d/vm/migrate.py VmMigrate saga uses `virsh migrate --live --persistent --undefinesource` (line 122) with no --unsafe and no --migrateuri. The --undefinesource flag removes the VM definition from the source node — for a pet/vipet VM that is fatal to HA, because the source can no longer be a failover target (vm_failover._takeover_one expects the VM defined on all peers, and even logs a TODO when it is not). (c) docs/actions/vm-migrate.md says the source is mgmt/app.py:_vm_migrate while docs/sagas/vm_migrate.md documents the saga — two docs describe two different code paths as if each is canonical. The saga is apparently dead/unwired relative to the dashboard path but still documented as authoritative.

**Code:** `mgmt/app.py:3436-3481`, `bedrock_d/vm/migrate.py:1-168`, `installer/lib/vm.py:431-446`, `docs/actions/vm-migrate.md:13`, `docs/sagas/vm_migrate.md:1-44`
**Docs:** docs/actions/vm-migrate.md:13; docs/sagas/vm_migrate.md:106-114

**Fix:** Pick one migrate implementation as authoritative (the mgmt live path is the one in use), delete or clearly mark the other as unused, and reconcile the two docs. If the saga is to be the future path, drop --undefinesource for pet/vipet (keep the definition on every peer) and add --migrateuri + UUID recording.

#### VM-04 🟠 Failover and create saga hard-wired to single disk0 while mgmt plane + docs are multi-disk
`high` · missing-impl · confidence: high

The mgmt plane is fully multi-disk: get_vm_disks (mgmt/app.py:442) enumerates vda/vdb/vdc, the dashboard create form adds vm-<name>-disk1/disk2 (vm-create.md:39-46), and delete tears down every disk (vm-lifecycle.md:84-110). But the failover takeover hard-codes a single resource: vm_failover._vm_disks returns exactly [f'vm-{name}-disk0'] (line 215-219). So a multi-disk pet/vipet VM that fails over would have only disk0 disconnected, promoted, and UUID-checked; additional disks (disk1+) would not be promoted to Primary and not safety-checked, so `virsh start` either fails or, worse, starts the VM against a still-Secondary or stale data disk. The bedrock_d/vm/create.py saga also only ever provisions disk0 (no loop over extra disks), so the saga and the dashboard create path disagree on multi-disk support.

**Code:** `bedrock_d/orchestrator/vm_failover.py:215-219`, `bedrock_d/vm/create.py:78-131`, `mgmt/app.py:442-576`, `docs/actions/vm-create.md:39-46`, `docs/actions/vm-lifecycle.md:84-110`
**Docs:** docs/actions/vm-create.md:39-46; docs/actions/vm-lifecycle.md:106-110

**Fix:** Make _vm_disks enumerate all DRBD resources for the VM (query drbd_resources by vm prefix, or read the libvirt XML/disks[] as the mgmt plane does) so the takeover sequence covers every disk. Confirm whether multi-disk pet/vipet is actually a supported v1.0 shape; if not, document the restriction and reject multi-disk converts to pet/vipet.

#### VM-05 🟡 Mid-takeover transition marker (all-zero UUID + transition flag) is not implemented
`medium` · missing-impl · confidence: medium

Design decision #4 calls for writing an all-zero UUID + a transition flag mid-takeover so the UUIDs stop matching the peer (tripping the are-UUIDs-equal safety) with details logged to victorialogs. The VM takeover sequence in _takeover_one (lines 374-479) goes straight disconnect → primary → record_uuid_after_promote → is_safe_to_start_vm → start, with no transition-marker write at any point. A grep for 'transition'/all-zero/zero-UUID across bedrock_d/vm and vm_failover finds nothing. This is a documented safety mechanism for the cluster-arbiter takeover that has no analog in the per-VM takeover path.

**Code:** `bedrock_d/orchestrator/vm_failover.py:374-479`, `bedrock_d/vm/failover.py:138-160`
**Docs:** KEY DESIGN DECISION 4 (transition marker)

**Fix:** Confirm with the design owner whether the transition marker is meant to apply to per-VM DRBD-UUID takeover or only to the cluster-arbiter LUN. If the former, add the transition write before drbdadm primary; if the latter, note in the failover docstring that the transition marker is arbiter-only so future readers don't assume it is missing here.

#### VM-06 🟡 Takeover assumes VM may be undefined but pet/vipet create defines on all peers — stale comment / no auto-define
`medium` · inconsistency · confidence: medium

_takeover_one (vm_failover.py:451-458) checks `virsh dominfo` and, if the VM is not defined locally, only logs a warning with a TODO ('implement automatic re-define from cluster state') then proceeds to `virsh start` anyway (line 459), which will fail. For VMs created via the create saga (create.py step_define line 275-284 defines on every peer) and via the dashboard convert path (defines on all peers per vm-create.md:191), the VM IS defined everywhere, so this branch should not trigger. But the saga --undefinesource migrate (see separate finding) and any manual operation can leave a peer without the definition, in which case takeover silently fails at `virsh start`. The TODO is an acknowledged missing piece of the documented failover contract (the failover should result in the VM running on the next-in-line node).

**Code:** `bedrock_d/orchestrator/vm_failover.py:451-465`, `bedrock_d/vm/create.py:275-284`, `docs/actions/vm-create.md:171-192`
**Docs:** KEY DESIGN DECISION 7 (election→promote→advertise); docs/actions/vm-create.md:191

**Fix:** Either implement the auto-redefine from cluster state before `virsh start`, or hard-fail the takeover with a clear log/refuse if the domain is undefined rather than attempting a start that will error. Ensure no migrate path leaves a pet/vipet undefined on a peer.

#### VM-07 ⚪ DRBD port-allocation formula disagrees across code and vm-create.md
`low` · code-vs-doc · confidence: high

bedrock_d/vm/drbd_config.py:drbd_port_for uses port = 7700 + minor (DRBD_PORT_BASE=7700, line 28-34). docs/actions/vm-create.md gives two different and mutually inconsistent values: line 145 'port = 7789 + minor # historical; convert uses 7000+minor' and lines 152-153 'port = 7000 + minor # config uses 7000+minor'. None of the three (7700/7789/7000) agree. With VM minors at 1200-1899, 7700+minor lands at 8900-9599, far from the documented 8200/8789 ranges.

**Code:** `bedrock_d/vm/drbd_config.py:28-34`, `docs/actions/vm-create.md:144-153`
**Docs:** docs/actions/vm-create.md:145; docs/actions/vm-create.md:152

**Fix:** Update docs/actions/vm-create.md to state port = 7700 + minor to match drbd_config.py (the authoritative bedrock_d create path), and drop the stale 7000/7789 historical notes.

#### VM-08 ⚪ Cattle VMs suspended by no_quorum_responder are never resumed or killed by the failover machine
`low` · inconsistency · confidence: medium

no_quorum_responder._run_no_quorum_cleanup (mgmt/orchestrator.py:534-537) suspends EVERY running VM unconditionally, including cattle. The vm_failover suspend/kill tasks deliberately operate only on pet/vipet (_local_pet_vipet_vms filters vm_type to pet/vipet, line 179), so cattle suspended by the no-quorum responder are never recorded in suspended-vms.json and never killed by kill_suspended_after_5min_task. Resume relies solely on _reconcile_paused_vms after quorum returns (which does resume cattle if still 'running' in the log). The vm_failover module docstring (line 10-11) states 'Cattle VMs are not suspended' — which contradicts the actual no_quorum_responder behavior that suspends them. A cattle VM on a node that never regains quorum stays paused indefinitely with no kill timer. Functionally low-risk (cattle have no failover), but the docstring is misleading and the two suspend paths are inconsistent about cattle.

**Code:** `mgmt/orchestrator.py:523-539`, `bedrock_d/orchestrator/vm_failover.py:148-179`, `bedrock_d/orchestrator/vm_failover.py:533-569`
**Docs:** bedrock_d/orchestrator/vm_failover.py:9-11

**Fix:** Reconcile the vm_failover docstring with reality (cattle ARE suspended by the no-quorum responder, just not failed over), or make no_quorum_responder skip cattle to match the documented intent. Confirm cattle resume on quorum-return is actually exercised.

### Networking / mesh

_The mesh-network code (installer/lib/netd.py, cluster_addr.py, l2disc.py, mdns_responder.py, discovery.py) is mature and closely matches the three-protocol design (discovery 7732 / ICMP latency / advertisement 7733) and the load-bearing sysctls (rp_filter=2, arp_ignore=1, arp_announce=2 + fib_multipath_hash_policy=1 in ensure_routing_sysctls). The key safety invariants the brief asked about hold in code: logged_up is set unconditionally and does NOT gate on rqlite write success (sweep_hysteresis), the 100.64.0.0/10 derived /24 + RFC3927 per-NIC link-local scheme is implemented, and .254 is the arbiter VIP. The main discrepancies are doc-vs-code drift that has accumulated since two design changes (D-13 panic-route "via master", D-14 sub-1ms latency floor) and the rqlite migration (cluster.json deleted 2026-05-26): the two narrative mesh docs still describe the OLD behaviour, including a worked metric example that is now numerically wrong, a stale dashboard URL on the wrong port, and cluster.json as the routing membership source when the code reads rqlite. There is also one dead constant and a latent node-index/arbiter-VIP range overlap._


#### N-01 🟡 Panic catch-all route is 'via master' in code but docs say 'via freshest neighbour'
`medium` · code-vs-doc · confidence: high

compute_routes() implements a panic-via-master rule (D-13): it resolves the cluster /24 catch-all via the mgmt-master's best direct/transit path (_mgmt_master_loopback) and only falls back to the freshest neighbour when the master is unknown/unreachable. Both narrative docs still describe the primary rule as '<cluster_prefix>.0/24 via <freshest direct neighbour> metric 999' / 'via the freshest neighbour overall', with no mention of routing via master. The implementation reference installer/lib/netd.md:139-140 IS correct ('via the master's best path (or freshest neighbour at bootstrap)'), confirming the two user-facing docs are the stale ones. This matters operationally: the .254 arbiter VIP at the top of the /24 reaches the master through this route, so an operator reading the docs would expect a different next-hop than what 'ip route' shows.

**Code:** `installer/lib/netd.py:2926-2985`
**Docs:** docs/06-mesh-network.md:278-281 (Routing layer §3 Panic-neighbour catch-all); docs/network-walkthrough.md:543-544; docs/network-walkthrough.md:1011

**Fix:** Update docs/06-mesh-network.md §'Routing layer' class 3 and docs/network-walkthrough.md lines 543-544 (and the §5 timeline) to describe the via-master rule with freshest-neighbour as the bootstrap/master-unreachable fallback, matching netd.py compute_routes() and netd.md.

#### N-02 🟡 local_metric latency term lost its documented value: code floors sub-1ms latency to 0, docs (and a worked example) still use latency_us/100
`medium` · code-vs-doc · confidence: high

Code computes lat_cost = max(0, latency_us - 1000) / 100 (a sub-1ms 'LAN noise floor', citing post-alpha-rewrite-notes.md D-14), so any RTT below 1 ms contributes 0 to the metric. Both docs show lat_cost = latency_us / 100 with NO floor. docs/network-walkthrough.md goes further and presents a worked number: 'every 100 µs → 1' and 'score for every path ≈ 100 (bandwidth) + 1 (latency at 100 µs) ≈ 101'. With the real code, a 100 µs link yields lat_cost=0, so the score is 100, not 101, and the latency-term translation table ('every 100 µs → 1', 'every 1 ms → 10') is wrong below 1 ms. The behaviour (bandwidth dominates on a healthy sub-ms LAN) is intentional, but the docs misrepresent it.

**Code:** `installer/lib/netd.py:2640-2663`
**Docs:** docs/06-mesh-network.md:238-245 (local_metric pseudocode); docs/network-walkthrough.md:428-470 (the metric formula box + worked example)

**Fix:** Update the local_metric pseudocode in docs/06-mesh-network.md and the formula box + worked example + translation table in docs/network-walkthrough.md to show the max(0, latency_us - 1000)/100 floor and recompute the example (≈100, not ≈101).

#### N-03 🟡 Mesh docs still treat cluster.json as the membership/routing source; code reads rqlite (cluster.json was deleted 2026-05-26)
`medium` · code-vs-doc · confidence: high

Per the project memory, cluster.json was deleted on 2026-05-26 and replaced by direct rqlite reads via cluster_state.load_cluster() (level='none'). netd.py now sources node loopbacks, mgmt_master, and membership entirely from rqlite (cluster_state / rqlite_client). But the mesh docs and netd.md still describe cluster.json as membership-of-record: 06-mesh §'Cluster log' references the bedrock-rust log and the operational-verification block literally tells operators to `cat /etc/bedrock/cluster.json | jq '.paths | length'` (a file that no longer exists), and the network-walkthrough big-picture diagram and t=0 timeline both read /etc/bedrock/cluster.json. netd.md repeatedly cites a 'cluster.json read' for i_am_mgmt_master and the panic route. (Note: emit_link_event still appends LINK_* rows to rqlite's paths table, so '.paths' is now an rqlite table, not a JSON file.)

**Code:** `installer/lib/netd.py:937-967 (_election_tick reads cluster_state.load_cluster())`, `installer/lib/netd.py:2164-2191 (_cluster_node_loopbacks via RqliteClient)`, `installer/lib/netd.py:2989-3015 (_mgmt_master_loopback via RqliteClient)`
**Docs:** docs/06-mesh-network.md:53-60 (Cluster log / membership); docs/06-mesh-network.md:377 (cat /etc/bedrock/cluster.json | jq '.paths'); docs/network-walkthrough.md:503; docs/network-walkthrough.md:1066-1075 (big-picture diagram); installer/lib/netd.md:30,128,165,200,203

**Fix:** Replace cluster.json references in the routing/verification sections of both mesh docs and in netd.md with the rqlite-backed reads (cluster_state.load_cluster() / nodes + cluster_info tables); fix the operator-verification command to query rqlite instead of catting cluster.json.

#### N-04 ⚪ Stale dashboard URL example: docs show https://<loopback>:8080, but 8080 is now weed-volume; mgmt LAN port is 8443
`low` · code-vs-doc · confidence: high

The 'why we need both layers' section uses https://100.104.109.2:8080 as the canonical inter-node dashboard URL. mgmt/app.py explicitly states port 8080 is now reserved for weed-volume (which binds 0.0.0.0:8080 on every node), the LAN-reachable operator/dashboard endpoint is 8443 HTTPS, and the loopback-only mgmt API moved to 8001. dashboard_install.py confirms https://<node>:8443. This matches the lesson note 'mgmt_url must be HTTPS on 8443, not HTTP on 8080'. The doc example would point an operator at the wrong service.

**Code:** `mgmt/app.py:5001-5042 (uvicorn binds 8443 HTTPS LAN, 8080 reserved for weed-volume, local mgmt moved to 8001)`, `installer/lib/dashboard_install.py:2 (dashboard reachable at https://<any-node>:8443)`
**Docs:** docs/network-walkthrough.md:148-149 ('we want it to write https://100.104.109.2:8080')

**Fix:** Change the example to https://100.104.109.2:8443 in docs/network-walkthrough.md line 149.

#### N-05 ⚪ Dead constant CLUSTER_JSON in netd.py — defined but never read
`low` · dead-code · confidence: high

CLUSTER_JSON is declared as a module constant but is never referenced anywhere else in netd.py (verified by grep: the only occurrence is the definition). All cluster reads go through rqlite (cluster_state / rqlite_client). STATE_JSON and CLUSTER_KEY_FILE next to it are live. The leftover constant is a residue of the pre-2026-05-26 cluster.json era and is misleading given the docs already wrongly imply netd reads that file.

**Code:** `installer/lib/netd.py:114 (CLUSTER_JSON = Path('/etc/bedrock/cluster.json'))`

**Fix:** Delete the CLUSTER_JSON constant at netd.py:114.

#### N-06 ⚪ node_loopback_ip range guard allows index 254, which collides with the arbiter VIP; docstring says 1..250
`low` · inconsistency · confidence: medium

cluster_addr.node_loopback_ip docstring states the valid index range is 1..250, but the actual validation guard is `if node_index < 1 or node_index > 254`. Since ARBITER_OCTET = 254 is the cluster arbiter VIP (100.X.Y.254/32), a node assigned index 254 would derive the same /32 as the arbiter VIP. In practice the registration allocator at mgmt/app.py:1207 iterates range(1, 250) (1..249), so 254 is never reached today — but a direct caller honoring the docstring's '1..250' (or the guard's '1..254') could collide. Three sources disagree on the ceiling: docstring (250), guard (254), allocator (249).

**Code:** `installer/lib/cluster_addr.py:60-69 (guard 1..254)`, `installer/lib/cluster_arbiter.py:61 (ARBITER_OCTET = 254)`, `mgmt/app.py:1207 (registration loop range(1, 250))`
**Docs:** docs/06-mesh-network.md:33-34 (Master .1, joiners lowest free index)

**Fix:** Tighten the guard to reject node_index >= 254 (or >= the arbiter octet) and reconcile the docstring/guard/allocator on a single max-index value to permanently exclude the arbiter octet.

#### N-07 ⚪ IcmpPinger.pending docstring claims per-(seq, peer) keying but the map is keyed by seq alone
`low` · inconsistency · confidence: medium

The IcmpPinger docstring says 'The pending map is per-(seq, peer) so reply dis-ambiguation works without collision.' The implementation keys pending solely by seq (pinger.pending[seq]); disambiguation between peers sharing the same per-NIC socket is actually done after pop by comparing addr[0] to the stored peer_link_addr_expected (icmp_drain_replies). Because seq is a single per-NIC counter incremented across all peers on that NIC, two outstanding probes to different peers always have distinct seq, so there is no functional bug — but the stated keying scheme does not match the code and could mislead a future maintainer who relies on the docstring.

**Code:** `installer/lib/netd.py:1948-1963 (IcmpPinger dataclass + docstring)`, `installer/lib/netd.py:2012-2013 (pinger.pending[seq] = ...)`, `installer/lib/netd.py:2030-2035 (pop by seq, then validate addr)`

**Fix:** Reword the docstring to describe the actual scheme: per-NIC monotonic seq as the key, with post-pop source-address validation to reject a reply whose seq matches but whose source differs.

### Install / ISO / mgmt plane

_The install/ISO machinery is in good shape and largely self-consistent: install.sh, build-iso.sh, and the kickstart agree on payload contents, the LIB_FILES list exactly matches installer/lib/*.py (no payload drift), ISO filenames correctly bind to their S3 prefix via BEDROCK_REPO, all three install paths converge on install.sh, neither path auto-runs `bedrock init`, and SeaweedFS is wired into both the cluster_init and node_join sagas. The dominant problem is that docs/reference/{api,ports,files}.md are heavily stale: they still describe the pre-rewrite world (cluster.json files, bedrock-rust log replication, port-8080 HTTP mgmt, a 9443 HTTP witness, /api/nodes/register, synchronous VM verbs) while the code has moved to rqlite-backed state, a unified bedrock-d on 8443/8001, a UDP/12321 Echo witness, a join-handshake flow with operator auth, and a fire-and-forget task-based VM API. There are two real runtime/safety issues: install.sh's stated "libvirtd not auto-started" boot model is silently undone by packages.install_base() re-enabling libvirtd, and the kopia binary that mgmt/backup.py shells out to is staged into the ISO payload but never installed onto any node. The mgmt_install legacy procedural path is dead-ish (saga is default) but still carries stale cluster.json/bedrock-rust assumptions._


#### I-01 🟠 kopia binary staged into ISO payload but never installed onto nodes
`high` · missing-impl · confidence: high

mgmt/backup.py builds every backup/restore command as a bare `kopia ...` invocation run over SSH on the home node (e.g. _kopia_connect_cmd line 434 `kopia {g} repository connect s3`, _kopia_create_cmd line 467). backup.py's own docstring (line 13) asserts "Each Bedrock node has the kopia binary". build-iso.sh stages a kopia 0.21.1 binary into PAYLOAD_DIR (lines 211-219). But install.sh never fetches or installs kopia — its curl URL set covers bedrock, bedrock-d, bedrock-mdns/redirect/cert-refresh, binaries/rqlited, binaries/weed, lib/*, mgmt.tar.gz, bedrock_d.tar.gz and configs, with no kopia at all. Neither packages.py, exporters.py, mgmt_install.py, agent_install.py, nor the cluster_init/node_join sagas copy kopia to /usr/local/bin. Result: any backup or restore action fails with `kopia: command not found` on a freshly installed node.

**Code:** `installer/install.sh:415-635`, `installer/iso-build/build-iso.sh:211-219`, `mgmt/backup.py:424-446`, `mgmt/backup.py:449-473`, `docs/reference/files.md:99-101`

**Fix:** Add a `curl -fsSL ${BEDROCK_REPO}/kopia -o /usr/local/bin/kopia; chmod +x` step to install.sh (it is already in the payload root, mirroring how `weed`/`rqlited` are fetched from binaries/). For the offline path the file is at /var/lib/bedrock-install/kopia. Confirm the publish-to-s3 step uploads kopia to the prefix root.

#### I-02 🟠 install.sh disables libvirtd but bootstrap re-enables it, violating the documented quiet-boot model
`high` · unsafe-invariant · confidence: high

install.sh lines 511-517 explicitly `systemctl disable drbd` and `systemctl disable libvirtd`, with a comment stating: "DRBD + libvirtd are NOT auto-started at boot. The mgmt service's orchestrator decides when it's safe ... This is the quorum-aware boot model." But install.sh then runs `bedrock bootstrap` at line 729 (the very end), which calls packages.install_base() (installer/bedrock invokes it), and packages.py line 136 runs unconditional `systemctl enable --now libvirtd`. Nothing disables it again afterward. Net effect: libvirtd is left enabled+running and will auto-start at every boot — the exact opposite of the documented safety model, where libvirtd start is supposed to be gated on cluster contact / role. This risks a node bringing up VMs/DRBD before the orchestrator has established quorum on boot.

**Code:** `installer/install.sh:511-517`, `installer/install.sh:728-729`, `installer/lib/packages.py:136`, `installer/bedrock`

**Fix:** Either remove the `enable --now libvirtd` from packages.py (let the orchestrator start it imperatively, matching the install.sh intent), or remove the misleading disable+comment block from install.sh if libvirtd-at-boot is in fact intended. Reconcile with cluster-protocol-overview boot orchestration. Pick one owner of libvirtd's enable-state.

#### I-03 🟠 ports.md is stale: wrong mgmt/witness ports, missing rqlite + SeaweedFS ports, dead bedrock-rust entry
`high` · code-vs-doc · confidence: high

Multiple port facts in ports.md no longer match the code. (1) Line 30 documents 8080/TCP as "FastAPI mgmt dashboard" on all IPs, but app.py serve_main() (lines 4994-5042) binds the dashboard on 8443 HTTPS (LAN) + 127.0.0.1:8001 (CLI), and uses 8080 only as a transient 127.0.0.1 bootstrap before a cert exists; the code comment (lines 5014-5018) states 8080 is now reserved for weed-volume. (2) bedrock-weed-volume.service binds 0.0.0.0:8080 on every node, weed-s3 binds 8333, weed-master binds 9333 (+grpc 18080/18888/19333) — none of these are in ports.md, which lists only filer 8888. (3) Line 40 documents the witness as 9443/TCP HTTP with /health,/cluster-info,/register,/status, but lib/witness.py line 46 (and dev-witness/run.py, and cluster-quorum-spec.md) use UDP/12321 bedrock-echo with an AEAD K/V-slot protocol. (4) Line 20 still lists 8200/TCP "bedrock-rust peer link" though the Rust daemon is gone (daemon_setup.py:4).

**Code:** `docs/reference/ports.md:20`, `docs/reference/ports.md:30`, `docs/reference/ports.md:40`, `mgmt/app.py:4994-5042`, `installer/configs/bedrock-weed-volume.service:23-24`, `installer/configs/bedrock-weed-s3.service:23`, `installer/configs/bedrock-weed-master.service:23`, `installer/lib/witness.py:46`

**Fix:** Rewrite ports.md to: mgmt 8443 HTTPS + 8001 loopback (drop 8080 as mgmt; add 8080 weed-volume); add rqlite 4001/4002; add weed-s3 8333, weed-master 9333 + grpc ports; change witness to UDP/12321 echo; delete the 8200 bedrock-rust row.

#### I-04 🟠 api.md documents removed/renamed VM and node-registration endpoints; misses join/auth/task surface
`high` · code-vs-doc · confidence: high

api.md's REST/WS contract has diverged from app.py. (1) POST /api/nodes/register (api.md lines 17-23, with full body + side-effects) is explicitly removed in code (app.py:1728 comment) and replaced by the /api/join/request → approve handshake. (2) VM verbs in api.md (lines 65-104) — /api/vms/create, /{name}/shutdown, /{name}/poweroff, /{name}/convert, /{name}/resources — do not exist; code has POST /api/vms (create), /{name}/stop, /{name}/force-stop, /{name}/ha-level (replaces convert), /{name}/compute + /{name}/disks (replace resources). The API is now fire-and-forget returning {status:"accepted", task_id}, not the synchronous blobs documented. (3) api.md omits the entire auth surface (/api/login, /api/whoami, /api/operators*, require_operator Bearer-token gating at app.py:930-997) and the join endpoints (/api/join/request|status|pending|approve|reject, app.py:1045-1518). (4) api.md line 181 states "No authentication today" — false; there is full operator token auth. (5) WS section (lines 142-164) lists channels cluster/event/vm.state/rpc.response but misses task.create + task.update (mgmt/tasks.py:149,196).

**Code:** `docs/reference/api.md:17-23`, `docs/reference/api.md:65-104`, `docs/reference/api.md:142-164`, `docs/reference/api.md:180-183`, `mgmt/app.py:1728`, `mgmt/app.py:2431-2751`, `mgmt/app.py:930-997`, `mgmt/app.py:1045-1518`

**Fix:** Regenerate api.md from the live FastAPI route table (the @app decorators + routes_*.register_routes). At minimum delete /api/nodes/register and the old VM verbs, document the task-based VM API, the join handshake, operator auth + Bearer requirement, and the task.* WS channels.

#### I-05 🟡 files.md describes the deleted cluster.json + bedrock-rust replication model as current
`medium` · code-vs-doc · confidence: high

files.md still presents the pre-rewrite file model. Line 11: cluster.json "Written by mgmt master via save_cluster() ... replicated to followers via the bedrock-rust log" — but app.py save_cluster() (line 139) is now a no-op and load_cluster() (line 126-135) reads rqlite via cluster_state.load_cluster(); bedrock-rust is deleted (daemon_setup.py:1-12). Line 13: daemon.toml "rendered by orchestrator's daemon_setup.render_from_snapshot ... read by bedrock-rust" — that render path and consumer no longer exist. Lines 122-131 list systemd units bedrock-mgmt.service (ExecStart=app.py), bedrock-vm, bedrock-vl as written by mgmt_install.install_full/exporters; but the mgmt API now runs inside the unified bedrock-d.service, and dashboard_install.py actively disables any stale bedrock-mgmt.service (lines 52-55). Line 12 also says cluster.key is read by "bedrock-rust (witness AEAD auth)" — now lib/witness.py.

**Code:** `docs/reference/files.md:11-13`, `docs/reference/files.md:122-131`, `mgmt/app.py:126-141`, `installer/lib/cluster_state.py`, `installer/lib/dashboard_install.py:40-55`, `installer/lib/daemon_setup.py:1-12`

**Fix:** Update files.md: cluster.json is no longer a written file (rqlite-backed projection only); drop daemon.toml/bedrock-rust rows; replace bedrock-mgmt.service with bedrock-d.service (ExecStart=/usr/local/bin/bedrock-d) and note bedrock-vm/bedrock-vl remain separate units; point cluster.key reader at lib/witness.py.

#### I-06 🟡 mgmt_install.install_full docstring + legacy body claim a podman witness on 9443 at init
`medium` · code-vs-doc · confidence: high

mgmt_install.py module docstring (lines 1-9) lists "bedrock-witness (podman container, port 9443) — if no external witness" as something install_full sets up. The actual default path (cluster_init saga) installs no witness at all — seed_cluster_state (cluster_init.py:515-558) seeds cluster_info/node/operator/obs_backends/mgmt_master with no witness configuration, and the legacy install_full body explicitly comments "No witness configured at init" (mgmt_install.py:217). The witness is the UDP/12321 echo store (witness.py:46), not a 9443 podman HTTP container. The docstring describes an implementation that does not exist.

**Code:** `installer/lib/mgmt_install.py:1-9`, `bedrock_d/install/cluster_init.py:515-558`, `installer/lib/witness.py:46`

**Fix:** Fix the mgmt_install.py docstring to drop the 9443 podman-witness claim and state that no witness is configured at init (operator wires the Echo witness host later), matching the saga behavior and cluster-quorum-spec.

#### I-07 🟡 Legacy procedural install paths still write cluster.json and assume bedrock-rust replication
`medium` · dead-code · confidence: high

Both install_full() and agent_install.install() default to the bedrock_d saga path (BEDROCK_INIT_SAGA defaults to "1"), but retain large legacy procedural bodies as the BEDROCK_INIT_SAGA=0 opt-out. Those bodies still embody the obsolete model: mgmt_install.py:236-253 writes a non-bootstrap /etc/bedrock/cluster.json with save-style content, and agent_install.py:243-286 + comments (lines 9, 246, 470-471) describe followers staying in sync "via the replicated log + view_builder" / "the bedrock-rust log" — replication machinery that no longer exists. If an operator ever sets BEDROCK_INIT_SAGA=0 (the documented escape hatch), they get a node that writes a cluster.json the rest of the system no longer reads and relies on a replication path that is gone.

**Code:** `installer/lib/mgmt_install.py:88-253`, `installer/lib/agent_install.py:141-286`, `installer/lib/mgmt_install.py:236-253`

**Fix:** Since cluster_init.py's own header says the legacy body is to be deleted "once the saga path passes a clean testbed e2e + 0.8-beta tag", either delete the legacy bodies now (and the BEDROCK_INIT_SAGA flag) or, at minimum, make them raise a clear "legacy path retired" error rather than producing a half-broken node.

#### I-08 ⚪ /api/topology cache-path claim in ports.md does not match in-memory implementation
`low` · code-vs-doc · confidence: medium

ports.md line 30 says /api/topology is "cached at /run/bedrock/physical_topology.json". The handler (app.py:1640-1650) returns _last_state.get("topology", ...) — an in-memory value recomputed every 3 s by the state push loop from each node's /run/bedrock/switch_neighbors.json. There is no read of a /run/bedrock/physical_topology.json cache file in this path.

**Code:** `docs/reference/ports.md:30`, `mgmt/app.py:1640-1650`

**Fix:** Drop the physical_topology.json cache claim from ports.md (or point it at switch_neighbors.json, the actual per-node input), and note the rollup is held in _last_state in memory.

#### I-09 ⚪ ISO naming table in install-and-iso.md omits the bedrock-installer- prefix shown by build-iso.sh
`low` · doc-vs-doc · confidence: high

Minor internal inconsistency within install-and-iso.md / build-iso.sh: the prose in install-and-iso.md (lines 63, 75) and build-iso.sh (NET_OUT_NAME=bedrock-installer-${VERSION}.iso) agree on the `bedrock-installer-<version>.iso` form, and the S3-prefix dispatch rule (ISO version → matching /<version>/ prefix, no /dev fallback) is correctly honored by build-iso.sh (S3_BASE=.../$VERSION wired into BEDROCK_REPO) and the kickstart firstboot. This is consistent; flagged only to confirm verification: the filename↔S3-prefix binding the doc requires is implemented correctly, and no /dev fallback exists in the net firstboot ExecStart.

**Code:** `docs/install-and-iso.md:103-113`, `installer/iso-build/build-iso.sh:84-85`

**Fix:** No code change needed — verified consistent. Optionally note in the doc that VERSION=dev yields literally `bedrock-installer-dev.iso` (matches the table row).

### Operator overrides & CLI

_The bedrock CLI (installer/bedrock) is the sole operator-facing entrypoint (other bedrock-* binaries are daemons/helpers with no operator subcommand surface; only rqlite_setup.py has an internal argparse). It implements bootstrap/init/join/status/cluster rename/node {list,maintenance,leave}/vm/witness/storage/operator/observability. Of the ten overrides cataloged in docs/operator-overrides.md, only the stuck-LMS decommission maps to a real command (node leave), and even that is undermined: the rqlite-nodes membership filter the override depends on is still an unimplemented TODO in installer/lib/witness.py, so a node leave does NOT actually clear a stale lms=1 slot — both the doc and the code flag this. The documented --accept-data-loss flag, the operator_override audit row (a mandatory general principle), and the confirmation prompts the catalog requires are entirely absent from code. Separately, the node_leave saga and its saga-doc disagree on the rqlite remove path, voter-only validation, and daemon-config propagation; the CLI module docstring advertises three storage subcommands (promote-critical-3way, remove-peer, collapse-to-n1) that argparse never registers; and several referenced verbs (node reset, storage demote-critical, witness register) do not exist under those names._


#### OP-01 🟠 Documented `--accept-data-loss` flag for `node leave` is not implemented
`high` · missing-impl · confidence: high

operator-overrides.md specifies the stuck-LMS decommission as `bedrock node leave --target <node> --reason "..." --accept-data-loss`. The argparse parser for `node leave` registers only a positional `target` and `--reason`; there is no `--accept-data-loss` flag anywhere in the tree (grep for accept.data.loss / accept_data_loss returns nothing). The saga run_node_leave() takes no such parameter either. The primary documented INV-7 stuck-LMS resolution override therefore cannot be invoked as documented.

**Code:** `installer/bedrock:970-977`, `bedrock_d/install/node_leave.py:205-263`
**Docs:** docs/operator-overrides.md: Override: Decommission stuck-LMS holder (CLI shape, lines 94-103)

**Fix:** Either add the `--accept-data-loss` flag (gating the data-loss-accepting branch + audit reason) or update operator-overrides.md to the actual `bedrock node leave <target> --reason ...` shape. Flag is load-bearing for the doc's safety story, so prefer implementing it.

#### OP-02 🟠 Stuck-LMS decommission override is ineffective: witness membership filter is an unimplemented TODO
`high` · missing-impl · confidence: high

The decommission override's effectiveness depends on surviving nodes ignoring witness slots for nodes no longer in rqlite's nodes table. witness.py drain_replies() has an explicit TODO (lines 287-297): 'Today ws has no membership-set field... Until that lands, removed nodes' stale lms=1 slots still block takeover even after node leave.' read_slot()/election still return the dead node's slot. operator-overrides.md itself warns the filter is not implemented and the override is not effective until it lands. So the documented primary path to unstick a stuck-LMS cluster does not currently work end-to-end.

**Code:** `installer/lib/witness.py:287-310`
**Docs:** docs/operator-overrides.md: Override: Decommission stuck-LMS holder (lines 122-131, 'Filter is currently not implemented'); docs/cluster-quorum-spec.md INV-7

**Fix:** Implement the rqlite-nodes membership filter in drain_replies (plumb the current member-id set through the netd tick) before relying on `node leave` for stuck-LMS recovery, or document the override as non-functional in v1.0.

#### OP-03 🟡 Mandatory `operator_override` audit row is never written by any override
`medium` · missing-impl · confidence: high

Principle #3 mandates that every override writes an un-suppressible operator_override row into rqlite (timestamp, operator, verb, target, reason, pre/post state). No operator_override table or write exists anywhere (grep across installer/, bedrock_d/, mgmt/, *.sql returns only the doc and unrelated comments). node leave records only a free-text `reason` on the node_unregister log entry; no operator identity, pre/post snapshot, or dedicated audit row is captured.

**Code:** `installer/bedrock:382-492`, `bedrock_d/install/node_leave.py:90-103`
**Docs:** docs/operator-overrides.md: General principles, #3 Audit trail (lines 38-43); per-command 'Write operator_override audit row to rqlite' (lines 115, 162)

**Fix:** Add an operator_override audit row (or table) written by the node_leave saga and any future override; at minimum capture operator, verb, target, reason, timestamp. If deferred to v1.x, mark principle #3 as not-yet-implemented in the doc.

#### OP-04 🟡 `node leave` has no consequence-confirmation prompt despite override safety principle
`medium` · code-vs-doc · confidence: high

The override catalog requires every override to prompt with the specific consequence and require an explicit confirmation string (not y/N) — for decommission, typing the cluster name + target node name. The implemented `bedrock node leave` path (both saga and legacy branches) performs no confirmation prompt at all; it proceeds immediately. The only interactive prompts in the CLI are for `join` (Continue? Y/n) and operator-password entry. No authenticated-operator gate is enforced for `node leave` either (principle #1).

**Code:** `installer/bedrock:382-410`, `installer/bedrock:964-977`
**Docs:** docs/operator-overrides.md: General principles #2 Confirmation (lines 34-37); Decommission step 4 'type the cluster name + target node name' (lines 113-114)

**Fix:** Add a confirmation prompt (cluster-name + target typed) and an operator-auth gate to the node-leave override path, or scope the doc's principles to the overrides that are actually shipped in v1.0.

#### OP-05 🟡 CLI module docstring advertises three storage subcommands that argparse does not register
`medium` · code-vs-doc · confidence: high

The CLI's own module docstring lists `bedrock storage promote-critical-3way <peer>` (N=2→N=3), `bedrock storage remove-peer <name>`, and `bedrock storage collapse-to-n1`. The storage subparser (lines 1005-1022) registers only init, status, promote, demote, _peer-promote, _local-reset, and cmd_storage handles only those. promote-critical-3way / remove-peer / collapse-to-n1 are not reachable from the CLI (the underlying tier_storage.promote_critical_to_3way / drbd_remove_peer functions exist but have no CLI verb; only cluster_tier.py references promote_critical_to_3way). Running these advertised commands errors out as unknown subcommands.

**Code:** `installer/bedrock:15-18`, `installer/bedrock:1005-1022`, `installer/bedrock:620-757`

**Fix:** Either wire promote-critical-3way / remove-peer / collapse-to-n1 into the storage subparser + cmd_storage, or remove them from the docstring. The collapse-to-n1 / single-node-mode case is also an outline-only override in operator-overrides.md, so its absence is consistent there but the docstring overpromises.

#### OP-06 🟡 node_leave saga doc describes a different CLI shape and step behaviors than the code
`medium` · doc-vs-doc · confidence: high

Multiple mismatches between docs/sagas/node_leave.md and the implementation: (1) Trigger documented as `bedrock node leave --target <node-name>` but CLI uses a positional `target` (argparse line 973). (2) validate_target documented to refuse if target is the only voter / only mgmt-master, but step_validate (lines 64-88) only checks self-leave + target-exists. (3) rqlite_voter_remove documented as `DELETE /nodes/<id>`, but code does `DELETE /remove` with a JSON `{"id": voter_id}` body (lines 121-129). (4) propagate_daemon_config documented to render rqlited.env and SSH-push + restart each peer staggered, but step_propagate (lines 138-152) only calls bump_revision and explicitly relies on each peer's subscriber regenerating its own config — no SSH push or restart.

**Code:** `bedrock_d/install/node_leave.py:64-152`, `installer/bedrock:970-977`
**Docs:** docs/sagas/node_leave.md: Trigger (line 19); step table (lines 39-44); rqlite_voter_remove (lines 92-97); propagate_daemon_config (lines 99-104); validate_target (lines 74-80)

**Fix:** Update docs/sagas/node_leave.md to match the implemented positional CLI, the /remove endpoint+body, the rqlite-revision-driven propagation, and the actual validate_target checks (or add the missing only-voter guard the doc claims). operator-overrides.md uses the same `--target` shape and should be reconciled too.

#### OP-07 ⚪ Documented `bedrock node reset` and `bedrock storage demote-critical` verbs do not exist
`low` · code-vs-doc · confidence: high

node_leave.md instructs the operator to run `bedrock node reset` on the target after leave, and `bedrock storage demote-critical` to hand off the master role. Neither verb is registered: node subcommands are only list/maintenance/leave; storage has `demote` (not `demote-critical`) and an internal hidden `_local-reset` (mapping to tier_storage.node_reset_local) but no operator-facing `node reset`. An operator following the doc hits unknown-command errors.

**Code:** `installer/bedrock:964-977`, `installer/bedrock:1005-1022`, `installer/lib/tier_storage.py:1704`
**Docs:** docs/sagas/node_leave.md: lines 14, 67 (`bedrock node reset`), line 75 (`bedrock storage demote-critical`)

**Fix:** Add a `bedrock node reset` operator verb wrapping node_reset_local (or rename the hidden _local-reset), and reconcile demote-critical vs the actual `storage demote` verb in node_leave.md.

#### OP-08 ⚪ Health-check remediation string references nonexistent `bedrock witness register`
`low` · code-vs-doc · confidence: high

routes_support.py surfaces remediation text '`bedrock witness register <host>` to add one.' The actual CLI verb is `bedrock witness add <id> <host[:port]> <pubkey>` (argparse line 997); there is no `witness register` subcommand. The operator copy-pasting the suggested remediation gets an unknown-command error, and the suggestion also omits the required pubkey argument.

**Code:** `mgmt/routes_support.py:143`, `installer/bedrock:994-1003`

**Fix:** Change the remediation string to the real `bedrock witness add <id> <host[:port]> <pubkey>` form.

#### OP-09 ⚪ Outline-only overrides have no CLI verbs (expected, but no stub/error surface)
`low` · missing-impl · confidence: medium

Six cataloged overrides — cluster rekey-witness, cluster seize, storage invalidate, node clear-no-quorum, force-single-node, rekey-CA — are marked 'Outline only' / 'Not yet specified' in the doc and have no CLI verbs (grep confirms no rekey-witness/seize/clear-no-quorum/invalidate verbs). This matches the doc's status, so it is a tracked gap rather than a contradiction. Note clear_no_quorum_marker() logic exists in election.py:173 but is auto-internal, not an operator command, so the documented 'force-clear no-quorum marker' override has no manual trigger.

**Code:** `installer/bedrock:932-1053`
**Docs:** docs/operator-overrides.md: status table (lines 16-26); seize (line 200); drbd invalidate (line 210); clear-no-quorum (line 219); rekey-witness (line 153); single-node mode (line 221)

**Fix:** No action required for v1.0 since the doc marks these outline-only; when implementing, ensure the no-quorum override exposes a manual trigger distinct from the automatic election.clear_no_quorum_marker(). Keep the doc status table in sync as verbs land.

### Tests & testbed

_The witness rework HAS landed: both the production witness (installer/lib/witness.py) and the testbed stub (testbed/bedrock_echo_stub.py) are passive AEAD K/V slot stores that match cluster-quorum-spec.md; neither uses the old claim/blessed/holddown logic, so the stub is current. No test imports a deleted module (test_import_smoke verifies all trees import). However, the testbed shell scripts have drifted badly from the current install/join contract: multiple scripts use the removed `bedrock join --witness` flag (argparse rejects it, exit 2) and the removed `bedrock init --cluster-name` flag, and several read /etc/bedrock/cluster.json for live cluster state even though the runtime stopped projecting cluster.json (mgmt/orchestrator.py only writes state.json now; cluster.json is a one-time N=1 bootstrap artifact). The pytest suite has 5 failing tests today (cluster_init step list, state-source lint allowlist, and three netd tests that assert pre-rqlite behavior). There is also a complete absence of unit-test coverage for the three load-bearing failover modules (witness.py, election.py, cluster_arbiter.py) that implement INV-1..INV-7. The most-current scripts (setup_4node_cluster.sh, test_e2e_offline.sh, pet-failover scripts) are otherwise aligned with the join-approval flow and echo-stub usage._


#### TE-01 🟠 5 unit tests fail today; suite is red on a clean checkout
`high` · inconsistency · confidence: high

`python3 -m pytest tests/` reports 5 failed, 255 passed. The failures are: (1) test_cluster_init_step_set_matches_documented_flow — EXPECTED_STEPS omits the new `bootstrap_cluster_ca` step that cluster_init.py:403 actually declares (between provision_storage_n1 and render_rqlited_env); (2) test_only_allowed_modules_import_rqlite_directly — ALLOWED set is missing bedrock_d/vm/{create,destroy,grow,migrate,failover}.py, bedrock_d/orchestrator/vm_failover.py, and installer/lib/cluster_state.py, all of which import rqlite_client directly; (3-5) three netd tests assert behavior the code no longer has (see separate findings).

**Code:** `tests/test_cluster_init_saga.py:33`, `tests/test_state_source_lint.py:23`, `tests/test_netd_phase_a.py:124`, `tests/test_netd_phase_a.py:316`, `tests/test_netd_phase_a.py:357`
**Docs:** docs/cluster-quorum-spec.md

**Fix:** Add `bootstrap_cluster_ca` to EXPECTED_STEPS in test_cluster_init_saga.py. Add the 7 missing files to ALLOWED in test_state_source_lint.py (or route them through bedrock_d.state). These are contract tests whose job is to be updated deliberately when the contract changes — they have not been.

#### TE-02 🟠 test_netd_phase_a multipath test asserts metric-LAST, but netd now emits metric-FIRST
`high` · code-vs-doc · confidence: high

test_multipath_metric_moves_to_tail asserts _normalize_route_line moves `metric N` to the TAIL (after all nexthops). The current _normalize_route_line docstring (netd.py:3071, item 3) and implementation explicitly do the OPPOSITE: move `metric` to BEFORE the first nexthop because `ip route replace` rejects metric-after-nexthop as a syntax error. The test now asserts a form iproute2 would reject. The test is stale and contradicts the load-bearing route-apply path.

**Code:** `tests/test_netd_phase_a.py:124`, `installer/lib/netd.py:3071`

**Fix:** Update the test's expected string to the metric-first form (matching netd.py:3071 item-3 behavior), or delete the test if compute_routes coverage already exercises the form. Confirm against `ip route replace` semantics.

#### TE-03 🟠 test_netd_phase_a panic-route tests mock cluster.json but netd now reads rqlite
`high` · code-vs-doc · confidence: high

test_master_does_not_install_panic_via_self and test_panic_uses_master_link_addr_not_freshest_when_both_known set up the master via _write_cluster_json() + a patched netd.CLUSTER_JSON file. But netd._mgmt_master_loopback (netd.py:2990) now queries rqlite (`SELECT ci.mgmt_master ... FROM cluster_info ci LEFT JOIN nodes n`) at level='none' — it no longer reads the cluster.json file, despite the function's own docstring still saying 'Read ... from cluster.json'. The fixtures therefore have no effect; the master lookup returns empty and the code falls back to freshest-neighbour, breaking the assertions. The whole D-13/D-14/D-15 test file references docs/post-alpha-rewrite-notes.md (file does not exist) and is built around the pre-rqlite cluster.json model.

**Code:** `tests/test_netd_phase_a.py:296`, `tests/test_netd_phase_a.py:357`, `installer/lib/netd.py:2990`
**Docs:** docs/post-alpha-rewrite-notes.md (missing)

**Fix:** Rework these tests to mock the rqlite master lookup (patch netd._mgmt_master_loopback or the RqliteClient) instead of writing cluster.json. Also fix the stale netd.py:2990 docstring to say rqlite, and remove/replace the missing docs/post-alpha-rewrite-notes.md reference in the test header.

#### TE-04 🟠 Testbed scripts use removed `bedrock join --witness` flag (argparse rejects it)
`high` · code-vs-doc · confidence: high

The current `bedrock join` parser (installer/bedrock:943-950) accepts only a positional `node_ip` and `--yes`; there is no `--witness` option. I verified argparse exits 2 ('unrecognized arguments: --witness') for `bedrock join --witness X --yes`. Yet test_e2e.sh:111 (`bedrock join --yes --witness $MGMT`), e2e_mesh.sh:45-49 (`bedrock join --witness $SIM1 --yes`), and even the actively-maintained test_e2e_offline.sh:335/349/362 (`bedrock join --witness $IP1 --yes`) all pass `--witness`. Every one of these join invocations would fail at the join step. setup_4node_cluster.sh:148 uses the correct positional form (`bedrock join $IP1 --yes`).

**Code:** `testbed/test_e2e.sh:111`, `testbed/e2e_mesh.sh:45`, `testbed/test_e2e_offline.sh:335`, `installer/bedrock:943`

**Fix:** Replace `bedrock join --witness <ip>` with the positional `bedrock join <ip>` in test_e2e_offline.sh, e2e_mesh.sh, and test_e2e.sh. Prioritize test_e2e_offline.sh since it is the maintained path.

#### TE-05 🟠 Testbed scripts read /etc/bedrock/cluster.json for live cluster state, which is no longer refreshed
`high` · code-vs-arch · confidence: high

mgmt/orchestrator.py:224-228 states explicitly: 'cluster.json is no longer written — consumers query rqlite directly via cluster_state.load_cluster() (level=none).' Only cluster_init's write_bootstrap_cluster_json step writes /etc/bedrock/cluster.json once at N=1 (with empty paths/nodes-but-self), and nothing updates it afterward. Multiple testbed scripts still read cluster.json for current state: test_scale_lifecycle.sh:97-179 reads cluster.json mgmt_master/nodes after scaling; chaos.py:142-150 reads cluster.json 'paths' for its convergence assertions; e2e_mesh.sh:55,71 reads cluster.json 'paths' after join; test_e2e_offline.sh:149,206 reads cluster.json mgmt_master (though it mostly uses rqlite elsewhere). After the cluster grows past N=1, these reads return the stale bootstrap snapshot (N=1, empty paths), so the assertions are meaningless or wrong. Note the in-code contradiction too: cluster_state.py:15-19 says the projection layer is 'gone' while orchestrator.py's module docstring (line 9-11) still claims it projects cluster.json.

**Code:** `testbed/test_scale_lifecycle.sh:97`, `testbed/chaos.py:142`, `testbed/e2e_mesh.sh:55`, `testbed/test_e2e_offline.sh:149`, `mgmt/orchestrator.py:224`
**Docs:** docs/01-rqlite-state-store.md

**Fix:** Switch these scripts to query rqlite (e.g. `SELECT mgmt_master FROM cluster_info`, `SELECT ... FROM nodes`, `FROM paths`) as test_e2e_offline.sh does for size/loopbacks. chaos.py's path-table validation against cluster.json should read the rqlite `paths` table instead. Lower urgency for the lines in test_e2e_offline.sh since most of it already uses rqlite.

#### TE-06 🟡 test_scale_lifecycle.sh uses removed `bedrock init --cluster-name` flag and old net-install
`medium` · code-vs-doc · confidence: high

Line 227 runs `bedrock init --cluster-name test-scale`, but the current init parser (installer/bedrock:940) defines only `--name`; `--cluster-name` is rejected by argparse. The script also installs via `curl -fsSL $REPO/install.sh | bash` (lines 226, 240) which is the old net-install flow, not the current ISO/firstboot path, and `bedrock join $master_ip` (line 241) with no operator join-approval step — the current contract requires /api/join/approve (see setup_4node_cluster.sh and test_e2e_offline.sh).

**Code:** `testbed/test_scale_lifecycle.sh:227`, `testbed/test_scale_lifecycle.sh:226`, `installer/bedrock:940`

**Fix:** Update to `bedrock init --name`, switch install to the ISO/firstboot assumption (like test_e2e_offline.sh), and add the join-approval flow. Or retire test_scale_lifecycle.sh in favor of test_e2e_offline.sh which already covers the 1->4->1 lifecycle.

#### TE-07 🟡 test_e2e.sh is broadly stale: net-install, no join-approval, --witness flag
`medium` · code-vs-doc · confidence: high

test_e2e.sh (May 18) predates the current install/join contract: it installs via `curl -sSL $REPO/install.sh | bash` (line 48, old net-install serve.py path, not the ISO firstboot path the testbed now defaults to per spawn.py), uses the rejected `bedrock join --yes --witness $MGMT` (line 111), and has no operator join-approval step. Its dashboard endpoints (/cluster-info, /api/vms/{vm}/migrate, /api/cluster) do still exist in mgmt/app.py, so those parts are fine, but the install+join scaffolding cannot succeed.

**Code:** `testbed/test_e2e.sh:48`, `testbed/test_e2e.sh:111`, `testbed/test_e2e.sh:135`

**Fix:** Either retire test_e2e.sh (superseded by test_e2e_offline.sh which exercises the current ISO+approval flow) or rewrite its setup section to match. At minimum mark it deprecated so nobody runs it expecting it to work.

#### TE-08 🟡 No unit-test coverage for the load-bearing failover modules (witness/election/cluster_arbiter)
`medium` · missing-impl · confidence: high

The cluster-quorum spec calls the arbiter takeover protocol 'load-bearing — every step matters' and defines INV-1..INV-7 (exact-UUID-equality takeover INV-5, never-timeout LMS + worst-case-missing-slot INV-7, single .254 owner INV-1). None of witness.py, election.py, or cluster_arbiter.py has a dedicated unit test under tests/. The only references in tests/ are incidental (a comment in test_cluster_init_saga.py:112 and an ALLOWED entry in test_state_source_lint.py:34). The Slot.is_stale / lms / takeover-decision logic and the AEAD encode/decode round-trip are entirely unverified by fast tests; correctness rests solely on the slow VM-based pet-failover scripts.

**Code:** `installer/lib/witness.py`, `installer/lib/election.py`, `installer/lib/cluster_arbiter.py`
**Docs:** docs/cluster-quorum-spec.md#invariants

**Fix:** Add a small pure-Python test exercising witness encode/decode round-trip, Slot.is_stale/lms, and the takeover step-2 decision matrix (stale+lms0, stale+lms1-refuse, fresh+lms0, fresh+lms1) plus INV-5 exact-equality and INV-7 missing-slot-is-worst-case. These run in milliseconds and guard the most safety-critical invariants in the system.

#### TE-09 ⚪ Witness stub omits the in-process membership/INV-7 filter the production reader documents as a TODO
`low` · inconsistency · confidence: medium

The stub correctly implements the passive K/V design (matches spec, no claim/blessed). But production witness.py:289-297 documents that the rqlite `nodes`-table membership filter required by INV-7's decommission-clears-stuck-LMS path is NOT yet implemented (slots from removed nodes still block takeover after `node leave`). This is a code gap rather than a test gap, but it means a testbed scenario exercising 'decommission a dead LMS node to unstick the cluster' (the INV-7(b) override) cannot pass yet, and no testbed script currently tries it. Worth noting because operator-overrides.md documents that path as supported.

**Code:** `installer/lib/witness.py:289`, `testbed/bedrock_echo_stub.py:90`
**Docs:** docs/cluster-quorum-spec.md#invariants; docs/operator-overrides.md

**Fix:** Track the witness.py:289 TODO as the gating item for INV-7(b); do not add a testbed scenario asserting decommission-clears-LMS until the membership filter lands, or it will fail. Stub itself needs no change.

### Doc-vs-doc consistency

_Top four doc-vs-doc contradictions._


#### D-01 🔴 Spec vs design-notes contradict on whether witness-loses-state clears LMS
`critical` · doc-vs-doc · confidence: high

cluster-quorum-spec.md INV-7 (162): witness-loses-state is NOT a clear; if the witness reboots empty, readers assume worst case and refuse takeover. quorum-design-notes.md (451-453) says an lms=1 slot stays set until owner writes 0, or the witness loses state (e.g. ESP32 reboot), or operator manually clears - treating ESP32 reboot as a legitimate clear. Design decision #1 sides with the spec. The design-notes wording would permit unsafe automatic takeover after a witness reboot.

**Code:** `docs/cluster-quorum-spec.md:162`, `docs/quorum-design-notes.md:451-453`
**Docs:** docs/cluster-quorum-spec.md:157-164

**Fix:** Fix quorum-design-notes.md 451-453 to match INV-7: witness state loss is worst-case-assumed, not a clear.

#### D-02 🟠 state-flow.md describes the OLD blessed-master/holddown/HMAC witness model with no out-of-date warning
`high` · doc-vs-doc · confidence: high

state-flow.md, linked from README.md as current, uses the superseded model: witness blessing already decided this side wins (257); refuses for the holddown window (307-312); Witness 15s holddown rejects the second claim (338); cluster.key 32-byte HMAC key signs witness claims (415-416). cluster-quorum-spec.md retires all of these. architecture.md got a warning banner; state-flow.md got none.

**Code:** `docs/state-flow.md:257`, `docs/state-flow.md:307-312`, `docs/state-flow.md:338`, `docs/state-flow.md:415-416`
**Docs:** docs/cluster-quorum-spec.md:10; docs/architecture.md:3-21

**Fix:** Add an out-of-date banner to state-flow.md, or rewrite to the passive-slot exact-UUID-takeover model.

#### D-03 🟠 Patience/self-demote timing contradicts: spec 5s, design-notes 30s, BEDROCK.md/state-flow 5 ticks
`high` · doc-vs-doc · confidence: high

quorum-design-notes.md locks Cold-boot patience window = 30s (17, glossary 620). cluster-quorum-spec.md sets cold-boot witness wait 5s (185) and NoQuorum self-demote streak 5 ticks (186). state-flow.md uses a 5-tick hold-down (219). Design decision #2 says 2+ nodes wait ~30s. The docs conflate the cold-boot wait with the NoQuorum streak and give incompatible numbers.

**Code:** `docs/quorum-design-notes.md:17`, `docs/cluster-quorum-spec.md:185-186`, `docs/state-flow.md:219`
**Docs:** docs/cluster-quorum-spec.md:179-186

**Fix:** Reconcile into one timing table: cold-boot multi-node patience = 30s, NoQuorum streak a separate shorter knob.

#### D-04 🟠 quorum-design-notes.md self-contradicts on the 10/1 weighted-vote model; conflicts with BEDROCK.md/state-flow/spec
`high` · doc-vs-doc · confidence: high

quorum-design-notes.md says Removes the awkward 10/1 weighting (60) and Each pool member is 1 vote (48), but later locks vote-weights stay as the existing formula: 10 votes per node + 1 per witness (256), with examples 16 of 31 (268) and 11 of 21 (272). BEDROCK.md:98, state-flow.md, and cluster-quorum-spec.md Scenario A (129) all use 10/1.

**Code:** `docs/quorum-design-notes.md:48-60`, `docs/quorum-design-notes.md:256-272`, `docs/BEDROCK.md:98`, `docs/cluster-quorum-spec.md:129`
**Docs:** docs/quorum-design-notes.md:46-61

**Fix:** Pick one vote model and propagate; delete whichever statement does not match.
