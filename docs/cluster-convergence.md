# Cluster convergence — how the three layers interact, converge, and stay split-brain-free

*This is the single cross-layer reference. It describes the THREE layers of a
Bedrock cluster, the strictly-downward dependency between them, the boot and
failover convergence order with its timing, the single-writer map (who writes
what), the layered split-brain-prevention argument, and — honestly — the
**residual edge cases that are not yet airtight**. Per-layer detail lives in
`cluster-quorum-spec.md` (witness/arbiter), `vip-route-decoupling.md` (network),
and `failover-quorum-aware-follower.md` (election↔rqlite).*

---

## 1. The three layers and what each OWNS

```
 ┌─ LAYER 3 ── RQLITE (KV state store, its OWN Raft) ─────────────────────────┐
 │   Owns: the cluster's durable state — nodes, vms, tiers, witnesses,         │
 │   mgmt_master, drbd_resources.current_uuid, operators.                      │
 │   The ARBITER rqlite instance runs ON the DRBD arbiter LUN, so it moves     │
 │   WITH the .254 host. Membership-of-record; NOT a real-time authority.      │
 │   installer/lib/{cluster_state,bedrock_state}.py                            │
 ├─ LAYER 2 ── CLUSTER-WITH-WITNESSES (who is master; arbiter actuation) ──────┤
 │   Owns: the weighted-vote election (100/node + 1/valid witness), the        │
 │   witness CLAIM, and actuation of the ARBITER LUN — DRBD-Primary on the     │
 │   `cluster` singleton + .254 VIP + arbiter rqlite + SeaweedFS filer.        │
 │   installer/lib/{election,cluster_arbiter,witness}.py                       │
 ├─ LAYER 1 ── NETWORK (reachability + routing; NO leader concept) ────────────┤
 │   Owns: mesh discovery, per-NIC link-local, path-vector routing, the        │
 │   election HEARTBEAT transport, the .254 VIP as an advertised /32, and the  │
 │   lowest-octet /24 catch-all. Converges FIRST, knows nothing of "master".   │
 │   installer/lib/netd.py                                                     │
 └────────────────────────────────────────────────────────────────────────────┘
```

A useful mental model: **Layer 1 is the data plane, Layers 2-3 are the control
plane.** Layer 2 is the real-time authority for "who is master"; Layer 3 is the
durable record of everything (and only catches up *after* Layer 2 has acted).

---

## 2. Dependency direction — strictly DOWN, so nothing deadlocks

**Every dependency arrow points up the stack (3 needs 2 needs 1); none point
down.** This is the property that makes boot and failover deadlock-free, and it
is enforced, not incidental:

- **The election is a pure function with ZERO rqlite dependency.**
  `election.compute()` (`election.py:79`) takes only observables — peer
  liveness, the active-node set, witness counts, acks — and returns
  Leader / Follower / NoQuorum. Its docstring states the reason: *it is what
  RECOVERS rqlite*, so it cannot depend on rqlite.
- **Routing reads nothing from rqlite.** `compute_routes()` builds routes purely
  from `logged_up` neighbours (`netd.py`, "compute_routes reads nothing from
  rqlite"). The `.254` VIP is resolved locally as a pure function of
  `cluster_uuid` (`cluster_addr.cluster_vip`), never an rqlite/master lookup —
  see `vip-route-decoupling.md`.
- **Local /32 routes never gate on an rqlite write.** `n.logged_up = True` is
  set unconditionally after the link-event emit, regardless of whether the
  master-side rqlite write succeeded (`lesson_netd_logged_up_no_rqlite_gate`).
  Without this the chicken-and-egg "routes need the rqlite leader, the leader
  needs reachable peers, peers need routes" would deadlock a cold cluster.
- **The arbiter takeover protocol makes NO rqlite call** (`cluster_arbiter.py`
  `_run_takeover_protocol`): it decides purely on the witness slot + the local
  DRBD UUID, because the rqlite it is about to bring up is the very service
  being recovered.

So the only "upward" reads (Layer 2 reading rqlite's `mgmt_master`) are used as a
*follow-pointer that the real-time detector can override*, never as the
master-selection input — see §5.

---

## 3. Convergence order

### Boot (cold start of a node)
```
 1. netd starts → mesh discovery + link-local + /32 routes come up
    (no rqlite, no master needed).                                  [Layer 1]
 2. netd election tick (1 s) reads local rqlite at level='none'
    (works without quorum) to learn the active-node set + last master.
 3. Election → Leader/Follower/NoQuorum from reachability + witness. [Layer 2]
 4. On Leader, netd DRIVES cluster_arbiter.promote_to_arbiter_host()
    → witness takeover (no rqlite) → DRBD Primary on `cluster` +
    mount + .254 + start arbiter rqlited + filer.                   [Layer 2→3]
 5. Arbiter rqlited joins the surviving per-node rqliteds → Raft
    quorum returns → set_mgmt_master(self) is written.             [Layer 3]
 6. boot_orchestrator waits ≤120 s for a role, then _start_local_services
    (level='strong') brings up this node's per-VM DRBD + SeaweedFS.
```
No step waits on a step above it. A cold single node reaches N=1 immediately
(`COLD_BOOT_PATIENCE_S` only delays the FIRST promote at N≥2 so a slower peer
can win cleanly).

### Failover (the master/`.254` host is lost)
```
 T+0     master's link lost. Two independent 1 s tick-counters start:
         - old master:  noquorum_master_ticks   (its own NoQuorum streak)
         - each survivor: missed_master_beats    (silence from the master)
 T+~9s   old master hits SELF_DEMOTE_MISSES=9 → demote_arbiter_host:
         filer/s3 down → release .254 → stop arbiter rqlited → umount →
         drbdadm secondary → write tag.claim=0.            [release FIRST]
 T+~10s  survivor hits MASTER_LOSS_MISSES=10 → election promotes the
         lowest-octet acked candidate → takeover protocol (witness slot +
         DRBD-UUID gate) → DRBD Primary + .254 + arbiter rqlited.  [then promote]
 T+~11s  arbiter rqlited rejoins → quorum returns → set_mgmt_master(survivor).
         converge_retry (5 s) re-drives the promote if it was briefly
         blocked on the old master not yet having released DRBD.
```
The **1-second / 1-tick gap between release (9) and promote (10)** is INV-1: the
old `.254` is gone one tick before the new one appears. The network catch-all
(lowest-octet) keeps the subnet routable across the blink (`vip-route-decoupling.md`).

---

## 4. Who writes what (single-writer map)

| State | Sole writer | Consumers / notes |
|---|---|---|
| Kernel routes, `.254` /32, mesh links | **netd** (Layer 1), per node | Pure local actuation; no rqlite |
| Who is master (the DECISION) | **netd election** (Layer 2), per node, real-time | Never read back from rqlite into the decision |
| `.254` + DRBD-Primary + arbiter rqlite + filer | **cluster_arbiter** on the elected Leader | Driven by the election; INV-1 ordered |
| Witness slot (per node) | **that node only** (AEAD-bound `n`) | Claim bit owned by cluster_arbiter |
| `mgmt_master` row in rqlite | **cluster_arbiter**, only AFTER a confirmed promote | A RESULT, never an input to the election |
| `drbd_resources.current_uuid` | the node that promoted (post-promote record) | Read level='strong' as the VM-start gate |
| Everything else (nodes, vms, tiers…) | the rqlite Raft leader | Membership-of-record |

**The load-bearing rule (`lesson_lms_writeback_race`):** *bedrock-net's real-time
election is the sole authority for who-should-be-master; rqlite's `mgmt_master`
is a downstream RESULT and is NEVER reconciled back into the election.* Reading
`mgmt_master` from rqlite is allowed only as (a) a follow-pointer the real-time
missed-beat detector masks to `None` the moment the master goes silent, and (b) a
"who was the last master to take over FROM" gate. Neither re-enters
`election.compute`.

---

## 5. Read-consistency classes (why staleness is safe where it's used)

| Class | rqlite level | Used for | Why safe |
|---|---|---|---|
| Convergence / observation | `none` (local replica, no quorum) | election tick, arbiter cluster_uuid/size, "am I isolated?", self-heal planning | Staleness biases toward *not* failing over (refuse/defer); never an irreversible commit |
| Strict-leader commit gate | `strong` / `linearizable` (one heartbeat round to a quorum, no clock) | VM-start DRBD-UUID gate, paused-VM reconcile, _start_local_services, _wait_for_role | A commit on stale state = split-brain; strict-leader read + **fail-loud refusal on no-quorum** prevents it |

No takeover / promote / fence decision is ever made on a stale `level='none'`
read. The arbiter takeover decides on the witness + local DRBD UUID (Layer 2),
not on rqlite at all (`feedback_read_consistency_classes`,
`lesson_recovery_path_strong_reads`).

---

## 6. Why no split-brain — the layered defense (for the cases that ARE covered)

Split-brain is blocked by a *stack* of independent mechanisms; an attacker must
defeat all of them:

1. **Strict weighted majority.** `total = 100·N + W`, `majority = total//2 + 1`.
   With node-majority on one side, the other side is `< majority` → NoQuorum.
   Casting-vote `+1` is credited ONLY to the steady-state incumbent
   (`election.py:166`), never to a follower/challenger — the 2-node proof.
2. **Quorum-aware follower.** A node that still *sees* the master but cannot see
   a majority returns NoQuorum (self-fences); it does not blindly follow into a
   minority (`failover-quorum-aware-follower.md`).
3. **INV-1 release-before-promote.** Self-demote at 9 ticks releases `.254`
   before any survivor promotes at 10.
4. **DRBD single-primary + generation gate.** `allow-two-primaries` is off;
   takeover refuses unless the local DRBD GENERATION (role-bit-masked, the way
   DRBD's own `drbd_uuid_compare` masks `& ~((u64)1)`) exactly equals the dead
   master's published marker → a node that missed writes is refused, fail-safe.
5. **Strong-read commit gates.** Every irreversible action (VM start, paused-VM
   reconcile, `mgmt_master` write) goes through a strict-leader read that fails
   loud on no-quorum — so even a *transient* dual-`.254` cannot commit divergent
   data.
6. **Master-decoupled data plane.** The network layer routes without knowing the
   master, so there is no boot deadlock and no upward dependency to race.

For the common operational cases this is airtight: single-node-loss failover,
minority partition, isolated master (±witness), odd-N, returning master
(steal-back guard + 30 s cold-boot patience), witness death, and
witness-reboot-empty-during-partition (the all-members-validity rule). See the
scenario table in `cluster-quorum-spec.md`.

---

## 7. Known residual risks — NOT yet airtight (read this before trusting §6 blanketly)

These are real edge cases the current implementation does **not** fully close.
Most are backstopped by the DRBD-generation + strong-read gates (so *data*
divergence is still blocked), but `.254`/quorum exclusivity is not guaranteed in
all of them. Severity and recommended fix noted.

| # | Residual risk | Why it's not airtight | Backstop today | Recommended fix | Sev |
|---|---|---|---|---|---|
| R1 | **Multi-witness (W≥2) exclusivity** | The election counts witnesses by *reachability* (`count_valid_confirmed`), not by *exclusive claim*. A witness reachable from BOTH sides of a split adds +1 to both → both sides can reach majority. The "majority-of-witnesses claim read" is designed but not built. | DRBD generation gate + strong-read gates block data commit; arbiter takeover step-2 claim read partially serializes | Gate `bedrock witness add` to **W≤1** until the exclusive majority-of-witnesses read is implemented; OR implement `⌈W/2⌉` exclusive-claim quorum | **High** |
| R2 | **Even split, single witness reachable from both sides** (Scenario C; N≥4 as k+k) | Both sides can compute `node_votes + 1 = majority` at the election layer; exclusivity then rests on the takeover step-4/5 claim **write-then-readback ordering** (~1 round-trip), which is not proven race-free | Lowest-octet tiebreak in the *ack-quorum* promote path; takeover step-2 fresh+claim read | A **deterministic** tiebreak written INTO the claim (lower octet wins the claim) rather than relying on read-ordering | Med-High |
| R3 | **Hung (not dead) master + no DRBD fence handler** | INV-1's 9-vs-10 margin is time-based on two *different* nodes; a wedged old master (GC/`virsh`/`drbdadm` hang) may not self-demote at 9 while the survivor `--force primary`s at 10 → transient dual-Primary on `cluster` until DRBD reconnects (`after-sb-2pri disconnect`). No `fencing resource-and-stonith` / `fence-peer` handler exists | `on-no-quorum suspend-io`; generation gate; strong-read commit gates (so no data commit) | Add a DRBD `fence-peer` handler (or a positive interlock) so the survivor's promote is blocked until the old Primary is provably down/fenced | Med |
| R4 | **Boot: arbiter LUN not `drbdadm up` on a returning node** | `_start_local_services` brings up per-VM DRBD but leaves the `cluster` singleton to `converge()`, which only ever runs `drbdadm primary` — never `up`. So a rebooted node hits "Unknown resource" and can never host the arbiter until a manual `drbdadm up cluster`. (Was a deliberate anti-steal-back measure — `lesson_master_return_steals_back`.) If the surviving host then dies, the cluster has a fully-synced replica it cannot promote. | Manual `drbdadm up cluster` recovers it | `drbdadm up cluster` to **Secondary** on boot (gated on the cluster-drbd-ready marker), and rely on the election + steal-back guards (30 s patience, `_peer_claims_master_now`) to prevent a premature promote — **needs sign-off** given the steal-back history | High (availability) |
| R5 | **No recovery from degraded DRBD** (StandAlone / Diskless / missing meta-LV) | `self_heal` only rebuilds a replica onto a *different* node after 65 min of permanent host loss; nothing reconnects a StandAlone, re-`create-md`s a missing `bedrock-meta-cluster`, or even surfaces `dstate`/`cstate`. Repeated partitions can strand a replica unpromotable, silently. | None (operator-only) | Detect + guarded reconnect/invalidate/re-create-md + resync-from-Primary; surface DRBD disk/conn state on the dashboard | High (availability) |
| R6 | **W ≥ 100 not rejected** | The `W < one node` safety bound is documented but `witness add` enforces no count cap, so an operator could in principle configure enough witnesses for a minority-node partition to sweep them | (moot if R1's W≤1 gate lands) | Enforce the bound at `witness add` (subsumed by R1's W≤1 gate) | Low |

**Bottom line on "never allow split-brain as now implemented":** for the
realistic *single-witness, clean-or-minority-partition, single-host-failure*
cases — yes, the layered defense (§6) holds. The residual gaps above are
multi-witness (R1, real and reachable), the even-split read-ordering race (R2),
and the hung-master/no-fence window (R3) — in all three, *data* divergence is
still blocked by the DRBD-generation + strong-read backstop, but `.254`/quorum
exclusivity is not provably airtight. R4/R5 are availability (not split-brain)
gaps. R1's W≤1 gate is the one quick safety win; the rest warrant design.

---

## 8. Pointers
- Witness / arbiter / takeover detail + scenario table → `cluster-quorum-spec.md`
- Operator overrides (stuck-claim decommission, etc.) → `operator-overrides.md`
- Network VIP-as-/32 decoupling + lowest-octet catch-all → `vip-route-decoupling.md`
- Quorum-aware follower (election↔rqlite) → `failover-quorum-aware-follower.md`
- Raft theory grounding → `raft-failover-review.md`
