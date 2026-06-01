# Raft Theory Review → bedrock-net Failover Design

*Basis for the failover-detection design decision. Claims grounded in Ongaro &
Ousterhout's Raft paper, Ongaro's PhD dissertation, etcd/raft + hashicorp/raft,
and verified against `installer/lib/election.py` + `netd.py`. Adversarially
fact-checked; verifier corrections applied inline (notably: rqlite
`linearizable` is the message-based ReadIndex read, NOT the level named
`strong`).*

---

## 1. Raft as a model

**Terms (epochs) are a logical clock, not a wall clock.** Each term begins with
an election; every RPC carries the sender's term; a higher term seen → revert to
follower; a lower term → reject. `currentTerm`/`votedFor` are persisted before
responding (Fig 2). **Terms advance on election events, not elapsed time — so
Raft's safety never depends on clock rate** (dissertation §3.1: bad clocks/delays
"can, at worst, cause availability problems").

**The two timers live on DIFFERENT nodes:**

| Timer | Lives on | Kind |
|---|---|---|
| Heartbeat / broadcast interval | **Leader** | fixed (ms) |
| Election timeout | **Each follower** | randomized per attempt |

Required inequality `broadcastTime ≪ electionTimeout ≪ MTBF` (§5.6) is a
**liveness condition only** — too-long a timeout merely delays failover; a
too-long *clock lease* crosses into a **safety** violation (split-brain). Hold
that asymmetry: timeouts are liveness, leases are safety.

**Who monitors whom in a 5+ node cluster — STAR, leader-centered, never
any-to-any:**
- Leader sends empty AppendEntries to all N−1 followers — **O(N) per interval**.
- Each follower monitors exactly one thing: "did I hear the leader within my
  election timeout?" Followers do **not** ping each other or the leader.
- RequestVote is the only any-to-any traffic, and only transiently during an
  election. **There is never O(N²) liveness gossip** — that's the SWIM/gossip
  family, which Raft deliberately avoids. *This is the elegant "avoid any-to-any"
  primitive you were reaching for — it's structural, not an add-on.*
- Leader failure is detected by **followers** (each via its own timeout);
  follower failure by the **leader** (failed AppendEntries). Core Raft has **no
  leader-side self-quorum-check** — that's the CheckQuorum extension.

## 2. Primitives table

| Primitive | Function | Assumes (clocks) | Safety / Liveness |
|---|---|---|---|
| **Terms** | order leadership epochs; detect/reject stale leader on contact | none | **SAFETY** (≤1 leader *per term*) |
| **Randomized election timeout** | break split votes | local monotonic timer only | LIVENESS |
| **AppendEntries heartbeat** | suppress follower timeouts; assert authority | broadcast ≪ election (liveness) | LIVENESS |
| **CheckQuorum** *(etcd/hashicorp; dissertation §6.2 behavior)* | **leader self-fences** — steps down if it can't reach a majority within an election timeout | none (counts acks it already gets) | LIVENESS — *the elegant fix for the gap* |
| **PreVote** *(§9.6)* | candidate trial-round before incrementing term; stops a partitioned/rejoining node disrupting a healthy leader | none | LIVENESS (rejoin case only; NOT membership-removal — Fig 4.7) |
| **ReadIndex** *(§6.4; rqlite level `linearizable`)* | linearizable read, no log write: one heartbeat round to a majority confirms still-leader | **NONE** | **SAFETY-preserving, clock-free** |
| **Leader lease** *(§6.4.1; etcd `ReadOnlyLeaseBased`)* | serve reads with zero messages until lease expiry | **BOUNDED CLOCK DRIFT** | **Safety-FRAGILE** — *your proposal's class* |
| **Leadership transfer** *(§3.10, TimeoutNow)* | graceful handoff via "permission to disrupt" | leader expires lease first | LIVENESS |
| **Learners / witnesses** | non-voting catch-up / lightweight voter | none | SAFETY-neutral |

Everything is clock-free for safety **except the leader lease** (and your
stamped-X, by inheritance). ReadIndex gets you the same "am I still the
quorum-blessed leader?" fact via messages + quorum intersection, no clock.

## 3. Single-directional / asymmetric links

**SAFETY is unconditional** under arbitrary asymmetry/one-way/partition: at most
one leader *per term*, by terms + at-most-one-vote-per-term + majority
intersection — none depend on link symmetry or clocks. **But "one leader per
term" ≠ "one leader per instant"**: Raft tolerates a partitioned ex-leader of
term T plus a new leader of T+1; the stale one can't commit (no majority) and
steps down on contact. *Conflating these is exactly how "just read rqlite's
leader field" reintroduces split-brain.*

**LIVENESS is only CONTAINED, never solved:**
- **CheckQuorum** — leader self-fences on **inbound** quorum loss (the
  receive-but-not-send leader looks healthy outbound but steps down). The
  self-demote predicate **must key off inbound quorum, not outbound reach.**
- **PreVote** — a one-way-link node can't inflate its term to disrupt.
- **PreVote + CheckQuorum must ship together** (PreVote alone can deadlock).
- Residual gap is **topological**: if no node is connected to a responding
  majority, **no consensus protocol can be live** (FLP) — correct behavior is to
  **block (NoQuorum)**, which is safety-preserving. Don't design assuming
  failover always succeeds.

## 4. The operator's proposal, evaluated

**Restated:** leader stamps `X` = its last-quorum time into the heartbeat;
NTP-synced followers count the failover timeout from `X`; plus one-way-link
protection.

**It is a real Raft technique — the leader lease (§6.4.1) — and it's
structurally *worse* than the textbook lease.** You re-derived lease propagation
(LeaseGuard, arXiv:2512.15659, formalizes exactly this: "the log is the lease").
But the textbook lease depends on a **rate** relationship (leaseholder's own
monotonic elapsed time); your stamped-`X` compares an **absolute timestamp**
against each follower's wall-clock `now` — an **offset** comparison, which is
exactly what an NTP step corrupts and monotonic clocks are immune to. You add an
absolute-time dependence where rate-only would do — a strict increase in
assumption surface, worse than the thing Ongaro already "does not recommend."

**Clock budget — the margin rule:**
> `clock_error_bound < (t_promote − t_self_demote)`

With demote @ 9 s, promote @ 10 s, NTP ~1 s: gap = **1 s**, need `1 s < 1 s` →
**FALSE. Zero margin — reject as written.** And worse than zero because:
1. **Relative error doubles** — two nodes each ±ε → 2ε worst-case relative skew.
2. **NTP's ~1 s is steady-state offset, not a pause/step bound** — one GC stall,
   VM live-migration freeze, or NTP step makes the master's self-demote fire
   *late* while the followers' timers run on schedule. A Bedrock node is a
   hypervisor running asyncio+GC under NTP — all three hazards are live.
3. **A fast follower is a SAFETY break** — expires `X` early, promotes a second
   master while the first is still legitimately quorum-present → two mgmt_masters
   driving .254 / DRBD-primary.

**What it fixes:** the pure-asymmetric case (isolated master's `X` freezes →
followers time out → majority re-elects) and the minority-follower case — but
note the minority-follower fix is really *"the follower now does its own quorum
reasoning,"* not the timestamp.
**What it doesn't:** a master whose outbound beats reach a minority while still
majority-acked (needs the §4.2.3/CheckQuorum analog) — you named "single-
directional breaks" but `X` solves only one direction.

**Head-to-head with the no-new-clock alternative** (gate the Follower branch on
the follower's own quorum tally — data already in `compute()`):

| | Stamped-`X` (lease) | Follower-own-quorum gate |
|---|---|---|
| New clock assumption | **Yes** (offset, worst on this HW) | **No** |
| New heartbeat field | Yes | No |
| Fixes minority-follower | Yes | **Yes, completely** |
| Fixes pure-asymmetric | Yes (faster) | Via leader self-demote (already present) |
| Failure mode if assumption breaks | **Split-brain (safety)** | Premature NoQuorum (liveness) |
| Class | Safety-fragile | **Pure safety improvement** |

**Recommendation: the combination, in order:** (1) ship the follower-own-quorum
gate now — pure safety, zero assumptions; (2) lean on the leader self-demote you
already have (that's your CheckQuorum); (3) only if you still need sub-rqlite-
convergence asymmetric detection, add `X` — with **seconds** of demote→promote
margin (e.g. 7 s / 12 s), **monotonic refresh-recency not absolute-offset**, and
**on top of** the self-demote (etcd: "CheckQuorum MUST be enabled if
ReadOnlyLeaseBased"). Strongly prefer (1)+(2).

## 5. Bedrock recommendation

**"bedrock-net owns mgmt_master, not rqlite-Raft" — holds for actuation/writes,
reinforced here.** Raft's "one leader" is per-*term*; reading rqlite's leader
field can return a node that is no longer the real leader (dissertation §6.2
stale-leadership) — precisely the `lesson_lms_writeback_race` race. rqlite's Raft
leader is elected for KV-log liveness, knows nothing of the witness weighting or
the 2-node tie-break, and has different membership. **bedrock-net stays sole
writer/actuator. Borrow Raft's mechanism, not its leader identity.**

**The one amendment:** a strict-leader **READ** of rqlite as a *promote gate* is
NOT the rejected write-back, and `feedback_read_consistency_classes` already
mandates strict-leader for takeover/promote. A node may promote only if a
strict-leader rqlite read confirms it's in the Raft majority — inheriting
rqlite's CheckQuorum/PreVote/asymmetric handling with **no new clock
assumption**. Two things to verify:
- ⚠️ **Use rqlite read level `linearizable`, NOT `strong`.** `linearizable` is
  the ReadIndex/§6.4 message-based read ("does not use clocks or leases").
  `strong` routes through the Raft log and the rqlite author says *"Don't use
  Strong in production."* The policy word "strong" in memory means *strict-leader,
  no local fallback*; the rqlite **level** that implements it is `linearizable`.
- ⚠️ **Verify rqlite's embedded hashicorp/raft has CheckQuorum + PreVote
  enabled** — they're options, not defaults.

### Decision-ready next steps
1. **Now (pure safety, no clock):** gate the Follower branch in `election.py` on
   the follower's own weighted-majority tally — return NoQuorum when the master
   is reachable but `my_votes < majority`. **Preserve the casting-vote asymmetry
   exactly** (the +1 is credited only in the steady-state-master branch — the
   2-node split-brain proof; don't let it leak into follower/promote).
2. **Now (tighten your CheckQuorum):** the master branch self-demotes the instant
   `my_votes < majority`, no grace — *more* aggressive than canonical CheckQuorum
   and may flap; add a short grace (≈1 election interval). Confirm the self-demote
   actually relinquishes .254 / demotes DRBD (in `cluster_arbiter`), not just
   returns NoQuorum locally — that actuation is the real gap behind the asymmetric
   case.
3. **Recommended (no clock):** make the promote path read rqlite at level
   `linearizable` to confirm Raft-majority membership before claiming
   mgmt_master. Fail safe: bound the wait, default to defer/NoQuorum on timeout
   (never promote-on-timeout); keep the local /32-route fast-path independent
   (`lesson_netd_logged_up_no_rqlite_gate`).
4. **Only if (3) is too slow:** add `last_quorum_ts` — seconds of margin,
   monotonic refresh-recency, on top of the self-demote.
5. **Cross-cutting guards:** (a) treat witness appearance/disappearance as a
   membership change under the same all-applied watermark as node drain
   (`lesson_node_drain_denominator_splitbrain`) — a flapping Echo changes the
   denominator hence the majority threshold; (b) a graceful `node leave` must
   carry a "permission to disrupt" flag that bypasses any sticky-master
   suppression you add (the §4.2.3 leadership-transfer exception).

**Bottom line:** keep bedrock-net authoritative for the .254/DRBD-primary
actuation; the missing piece is **quorum-awareness on the follower + a real
self-demote on the leader, both clock-free** — not a wall-clock lease. Stamped-`X`
is a real technique but lands in the lease class and, at 9 s/10 s with NTP-1 s,
is provably on the split-brain boundary.

*Key code: `election.py:182-191` (the follower gap), master self-demote
(`my_votes < majority`), casting-vote +1 (master-branch only); `netd.py`
ELECTION_INTERVAL_S, heartbeat encode (~`encode_heartbeat`) = where
`last_quorum_ts` would go.*
