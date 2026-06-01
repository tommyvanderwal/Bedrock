# Quorum-aware follower — design sketch

*The fix: a node that still **sees** the master but cannot itself **see a
majority** must return **NoQuorum**, not **Follower**. Clock-free, no new
heartbeat field. This sketch shows the state, the per-tick math, the exact
partition timeline, and the rqlite interaction.*

---

## 1. Two leadership layers — who owns what

```
 bedrock-net ELECTION  (realtime, 1s tick)        rqlite  (real Raft, KV store)
 ════════════════════════════════════════         ════════════════════════════
 decides mgmt_master                               stores cluster state
 weighted vote: 100/node + 1/confirmed-witness     leader elected for KV liveness
 owns .254 VIP + arbiter rqlite + DRBD-primary     knows NOTHING of the witness
                                                   or the 2-node tie-break
        │  writes mgmt_master  (RESULT of a              ▲
        │  confirmed promote, single-writer)             │
        └────────────────────────────────────────────►  cluster_info.mgmt_master
        ▲                                                │
        │  reads current_master  (level=none, local      │
        └─  replica, stale-tolerant — NEVER reconciled ──┘
            BACK into rqlite; that was the split-brain RCA)
```

**Rule that stays:** bedrock-net is the sole writer/actuator of `mgmt_master`.
We borrow Raft's *mechanism* (quorum reasoning, CheckQuorum), never its *leader
identity* — Raft's "one leader" is per-*term*, so rqlite's leader field can name
a stale leader.

---

## 2. Topology — the mesh is what makes a LOCAL quorum check possible

```
        A ◄──────► B          Every node broadcasts its election heartbeat to
        ▲ ╲      ╱ ▲          every peer, and keeps peer_hb[p].seen_at for ALL
        │   ╲  ╱   │          peers (refreshed in hb_drain @ 4Hz).
        │    ╳     │
        ▼   ╱  ╲   ▼          → O(N²) mesh. UNLIKE real Raft (star: a follower
        C ◄──────► D ...        hears ONLY the leader and CANNOT compute quorum,
                                so Raft puts CheckQuorum on the leader).
                                Here every node already hears everyone, so EVERY
                                node can compute its OWN quorum, no new traffic.
```

---

## 3. What every node computes each election tick (purely local)

```
  reachable    = { p : now_mono - peer_hb[p].seen_at <= FRESH_S(1.5s) } ∪ {self}
  my_votes     = 100 * |reachable| + confirmed_witnesses
  majority     = total_votes // 2 + 1            (total = 100*N_active + N_witness)
  master_fresh = current_master ∈ reachable      (current_master read from rqlite)
```

No global knowledge. No clock comparison. Just "how many nodes' heartbeats are
fresh in MY view."

---

## 4. State machine + decision matrix

```
   ┌─────────────────────────── one node, one tick ───────────────────────────┐
   │                                                                           │
   │   am I master? ──yes──► my_votes>=majority? ──yes──►  L E A D E R          │
   │        │                      │                                           │
   │        no                     no ───────────────────►  N O Q U O R U M    │
   │        │                                              (self-demote: drop  │
   │   master_fresh? ─yes─► my_votes>=majority? ─yes─► F O L L O W E R   .254)  │
   │        │                      │                                           │
   │        │                      no ──────────────────►  N O Q U O R U M  ◄══╪═ THE FIX
   │        no                                            (was: FOLLOWER — bug)│
   │        │                                                                  │
   │   master gone: my_votes>=majority? ─yes─► win octet? ─► LEADER / FOLLOWER  │
   │                       │                                                   │
   │                       no ─────────────────────────►  N O Q U O R U M      │
   └───────────────────────────────────────────────────────────────────────────┘

   NoQuorum is debounced: it must hold SELF_DEMOTE_MISSES(9) consecutive ticks
   before it commits (drops /run/bedrock-no-quorum → pauses VMs / releases .254).
   Any tick that recovers quorum resets the counter — that is the blip filter.
```

| I see master? | my_votes ≥ majority? | today | with fix |
|---|---|---|---|
| I am master | yes | Leader | Leader |
| I am master | no | NoQuorum | NoQuorum |
| yes (follower) | yes | Follower | Follower |
| **yes (follower)** | **no** | **Follower ← bug** | **NoQuorum** |
| no (master gone) | yes | promote/defer | promote/defer |
| no (master gone) | no | NoQuorum | NoQuorum |

The data for the fixed row is **already in `compute()`** — only the follower
branch (`election.py:182`) skips computing `my_votes`.

---

## 5. Exact partition timeline

5 nodes, **A** = master. Partition at **T=0**: `{A,B}` | `{C,D,E}`.
`FRESH_S=1.5s`, 1s tick, `SELF_DEMOTE_MISSES=9`, `MASTER_LOSS_MISSES=10`.
B can still hear A (B = the minority follower the fix targets).

```
 T=0      ╳ partition.  {A*,B} lose C,D,E ;  {C,D,E} lose A*,B
          │
 T..1.5s  C,D,E still inside B's 1.5s freshness window
          │   → B sees {A,B,C,D,E}=500 ≥ 251 → FOLLOWER  (correct: could be a blip)
          │
 T+1.5s   freshness expires; stale peers drop from every view
          │
          ├─ A* (master)  : sees {A,B}=200 < 251 → Leader-branch → NoQuorum   ┐
          ├─ B  (follower): sees A*, {A,B}=200<251 → NoQuorum  ◄── THE FIX      │ debounce
          └─ C,D,E (majority): master A* not fresh → missed_master_beats++      ┘
          │
          │   ...9-tick (A*,B) / 10-tick (C,D,E) debounce, blip-resettable...
          │
 T+10s    A* self-demote → release .254 + DRBD secondary      ┐ minority fully
          B  self-fence  → drop no-quorum marker → pause VMs   ┘ stood down
          │
          │   ◄─────────── 1s gap = INV-1 release-before-promote ───────────►
          │
 T+11s    C/D/E promote: lowest-octet wins → DRBD primary + .254 + arbiter rqlite
          → writes mgmt_master  (RESULT of confirmed promote, into rqlite)
```

**Follower self-fences at T+10s**: *detected* at ~T+2s (first tick its local
`my_votes` drops below majority), *committed* after the 9-tick debounce — the
same instant the old master releases, 1s before the survivors promote.

---

## 6. rqlite interaction

```
 WRITE (unchanged):  promote confirmed  ──►  set mgmt_master   (single writer)
                     never the reverse (no reconcile-from-rqlite — RCA)

 READ steady-state:  current_master  ◄── level=none (local replica, no quorum
                     needed, stale-tolerant — used only as "who do I follow")

 READ promote-gate (OPTIONAL, no clock — inherits rqlite CheckQuorum/PreVote):
   ┌──────────────┐  "am I in the Raft majority?"   ┌───────────────────────┐
   │  candidate   │ ── level = linearizable ───────►│ rqlite ReadIndex      │
   │ (about to    │     (NOT level=strong!)         │ (1 heartbeat round to │
   │  promote)    │ ◄── majority-confirmed ─────────│  a quorum, no clock)  │
   └──────┬───────┘     timeout → DEFER / NoQuorum   └───────────────────────┘
          │             (never promote-on-timeout)
          ▼
     proceed to drbd-primary + .254 + arbiter only if confirmed
```

⚠ `level=strong` routes through the Raft *log* ("don't use in production"). The
policy word "strong" (= strict-leader, no local fallback) is implemented by
rqlite **`linearizable`**. Verify rqlite's hashicorp/raft build has
CheckQuorum + PreVote enabled before relying on this gate.

---

## 7. Scope of the fix

| Case | Closed by | Note |
|---|---|---|
| Clean partition, minority follower | **follower's own `my_votes` (this fix)** | local count == true connectivity |
| Isolated master (any partition) | leader self-demote (already present) | tighten: add ~1-tick grace so it can't flap |
| Asymmetric (B sees more than is mutually connected) | leader self-demote propagating + (optional) `last_quorum_ts` / rqlite gate | local count alone is insufficient here |

**Not** in this fix: a wall-clock lease (`last_quorum_ts`). At 9s/10s with
NTP~1s it sits on the split-brain boundary (`clock_error < promote−demote` →
`1s < 1s` false). Keep it a last resort, with seconds of margin, monotonic
refresh-recency, and only on top of the self-demote. See
`docs/raft-failover-review.md` for the full derivation.

---

## Guards (don't regress these)

- **Casting-vote +1** is credited ONLY in the steady-state-master branch — the
  2-node split-brain proof. Don't let a shared `my_votes` helper leak it into
  the follower/promote path.
- **Witness appear/disappear = membership change** (it changes the denominator →
  the majority threshold). Gate it with the all-applied watermark, like node
  drain (`lesson_node_drain_denominator_splitbrain`).
- A graceful `node leave` needs a "permission to disrupt" flag that bypasses any
  sticky-master suppression (Raft §4.2.3 leadership-transfer exception).
