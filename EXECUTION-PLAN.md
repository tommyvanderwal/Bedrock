# Bedrock — Execution Plan

> Companion to [`DISCREPANCY-REVIEW-2026-05-28.md`](DISCREPANCY-REVIEW-2026-05-28.md).
> Records the **locked architectural decisions** and the concrete work that follows
> from them. We work through the 8 "biggest architectural discrepancies" (BAD-1…BAD-8)
> one at a time; each gets locked here before any code is written. Finding IDs (Q-01,
> SG-03, …) refer to the review catalog.

## Top-level decisions captured so far

- **BAD-1 (quorum/election/witness/LMS):** IMPLEMENT for v1.0 — not defer. Full design
  locked below.
- **BAD-2 / BAD-3 (bedrock_d rewrite + storage):** FINISH the rewrite; **bedrock-d owns
  starting libvirt + DRBD resources at boot** (replaces the current "install.sh disables,
  packages.py re-enables" mess). Detail pending (next).
- **Work order:** fix the things we *know* will fail first; run the 1→2→3→4 deploy when
  Tommy says "run it" (to get real-world perspective on the rest); then RCA + fix the
  remainder. BAD-1 is first.

---

## BAD-1 — Cluster base-layer election + arbiter failover  ·  **LOCKED 2026-05-28**

This is the lowest tier of the cluster: it runs in **bedrock-d** with **no rqlite
dependency**, because it is what *recovers* rqlite. It owns node-to-node heartbeats, the
election, witness I/O, the arbiter DRBD resource + the `100.X.Y.254/32` VIP, and the
per-node local `state.json`.

### Two-tier ownership
1. **bedrock-d (base layer):** election, node-to-node heartbeats, witness I/O, arbiter
   DRBD + `.254`, local `state.json` (records *who this node believes is master* +
   the local 7-day arbiter-UUID history — both survive reboot, need no rqlite).
2. **rqlite (recovered by tier 1):** source of truth for **active node set** and
   **configured witness set**, and arranger of all higher-level resources (per-VM DRBD,
   services). `mgmt_master` is written here **as a result, after** the arbiter rqlite is
   back — never as the trigger (you may not even have rqlite quorum to write to until the
   arbiter is up).

### Vote model
- **node = 100 votes; each valid witness = 1 vote.**
- `total_votes = 100·N_active_nodes + N_configured_witnesses` (both counts from rqlite).
- `majority = total_votes // 2 + 1`.
- `my_votes = 100·(node acks incl. self) + (valid+confirmed witnesses)`.
- Rationale for 100/1: a witness — even several — can only ever break an exact node-tie,
  never overrule a real node.

### Witness validity & the denominator rule
- **Configured witnesses** for the cluster come from rqlite; every node knows the set.
- A witness is **valid** only if it currently holds a slot entry for **every active node**
  in the cluster (per rqlite). Entries may be stale/hours-old — fine — but *missing any
  active node's slot ⇒ the witness is invalid* and contributes **0**.
- **All configured witnesses count in the denominator; only valid+confirmed ones add to
  the tally.** A configured-but-invalid/unreachable witness therefore *raises* the
  majority bar and biases the cluster toward "do not fail over." (3 configured, 1 valid,
  2 nodes: `total = 203`, `majority = 102`, lone survivor `= 100+1 = 101 < 102` → no
  takeover → safe. This naturally enforces "a majority of configured witnesses must be
  valid" with no extra rule.)
- "Confirmed" (for the candidate's own use at failover) = the candidate **wrote its own
  slot to that witness and read it back** this cycle, verifying its own `ts_writer` is
  present in the doubly-AEAD-encrypted payload.
- The witness is consulted **only at the two decision moments** (failover, cold boot),
  not continuously. A node that already holds LMS keeps running even with no witness.

### UUID eligibility (the split-brain guard)
- Node votes are **active acknowledgements**, not passive reachability. A peer grants its
  100 votes to a candidate only by replying (in its node-to-node heartbeat) "I see
  *candidate* as master-to-be (transitioning)", and only if **the peer has also lost the
  master** *and* finds the candidate **eligible**.
- **Eligibility = arbiter-DRBD UUID check against the voter's own local 7-day history:**
  - candidate's UUID is **superseded** in the voter's history → **refuse** (stale).
  - candidate's UUID is **unseen** → assume newer than anything in the last 7 days → votable.
  - candidate's UUID matches **current** → votable.
- A stale candidate can therefore never auto-win even on node count; the cluster stays
  NoQuorum until an up-to-date node appears or an operator runs **`seize`** (operator
  prerogative; never automatic). Safety over availability — "only the paranoid survive."

### Last-man-standing (LMS)
- In a 2-node cluster, as long as the master sees **itself + the witness**, it does **not**
  demote. It verifies its own witness slot is current (own-timestamp readback) and watches
  the peer's slot.
- If the peer is unreachable directly **and** the peer's witness slot is stale > 10 s, the
  master concludes the peer is gone and **sets its LMS bit — locally in `state.json` AND on
  the witness (readback-confirmed)**. From then on it runs **witnessless**.
- LMS **never auto-clears** (review INV-7): cleared only by the owner writing `lms=0`
  (owner+witness both online), operator decommission of the node (removed from rqlite →
  peers ignore its slot), or witness re-key. **Witness state-loss is worst-case-assumed,
  not a clear.** (Fixes the `quorum-design-notes.md` contradiction, finding D-01.)
- On the *other* node's cold boot: it checks the witness; **no entry, or an entry with LMS
  set → it must not take over** (defer to the LMS holder / worst-case).

### Timing
| Event | Budget |
|---|---|
| Node-to-node heartbeat | 1 s |
| Leader-loss detection | **10 consecutive misses = 10 s** (1–2 stragglers ⇒ still alive) |
| Old master self-demote | **~9 s** — release `.254`, stop arbiter rqlite, `drbdadm secondary` — 1 s *before* the survivor promotes (INV-1 margin) |
| Survivor promote | **~10 s**, then ~1 s+ to seize DRBD + start arbiter services |
| Cold boot | single node → promote now; **2+ nodes → ~30 s** patience (let a slower node catch up; no rush; cleaner convergence), gated by the cold-boot UUID-vs-own-slot check |

The old master self-demotes at ~9 s **only when it loses both peers and the witness**. If
it keeps the witness (2-node `100+1 ≥ majority`), it stays up as LMS and there is no
takeover (the survivor sees a fresh master slot).

### Failover protocol (steady-state leader loss)
1. **Detect** — candidate misses the master's node-to-node heartbeat 10× (10 s).
2. **Advertise** — candidate marks itself "master-to-be" in its node-to-node heartbeats,
   carrying its arbiter-DRBD UUID (eligibility proof).
3. **Collect acks** — peers that also lost the master and accept the candidate's UUID reply
   "I see *candidate* as transitioning." Each ack = 100 votes. A peer still hearing the
   master never acks.
4. **Witness tiebreaker** — candidate writes its LMS slot to each valid witness and reads
   it back; if the master's slot there is stale > 10 s and nobody else claims master, each
   valid witness adds +1.
5. **Quorum** — acks + witness tiebreakers ≥ majority (denominator includes all configured
   witnesses).
6. **(Concurrent)** old master self-demoted at ~9 s.
7. **Promote** (bedrock-d, no rqlite): `drbdadm primary --force tier-critical`, mount,
   `ip addr add .254`, start arbiter rqlite + filer/s3; record believed-master in local
   `state.json`.
8. **After arbiter rqlite rejoins** → write `mgmt_master` (result); rqlite then arranges
   higher-level resources.

### Code changes
- **`installer/lib/election.py`** — `VOTES_PER_NODE 10 → 100`; witness term = count of
  *valid+confirmed* witnesses with denominator = *all configured* witnesses; replace the
  passive "reachable peer" tally + lowest-octet rule with **ack-based voting** (a vote =
  a peer's heartbeat ack naming this candidate as transitioning); add **UUID-eligibility**
  gate using the local 7-day history.
- **`installer/lib/netd.py`** — add an **election/transition payload to the node-to-node
  heartbeat** (sender's believed-master, transitioning flag, advertised arbiter-UUID, ack
  target); replace `DOWN_HYSTERESIS_S=10` + `NOQUORUM_HOLDDOWN_TICKS=5` (and the disabled
  `LONE_MASTER_WATCHDOG_S=28`) with the single **10-missed-beat** detector and the **~9 s
  self-demote**; **stop recomputing `ws.own_tag` every tick** (line ~1150) — LMS becomes an
  explicit decision owned by the takeover/demote path (fixes finding **Q-01 / BAD-4**).
- **`installer/lib/witness.py`** — implement the **per-node-membership validity** check
  (slot present for every active node, members from rqlite) and the **drain_replies
  membership filter** (drop slots for nodes no longer in rqlite — fixes **Q-02 / BAD-5**);
  add `TAG_TRANSITIONING` if we keep a witness-side transition marker (TBD vs heartbeat-only).
- **`installer/lib/cluster_arbiter.py`** — takeover gated by the unified election outcome
  (quorum + eligible + master-slot-stale > 10 s); own the LMS set/clear; record believed-
  master to `state.json`; write `mgmt_master` only *after* arbiter rqlite is back.
- **`installer/lib/state.py` (+ schema)** — add the **local 7-day arbiter-UUID history**
  to `state.json` (drives eligibility) and persist believed-master (fixes **ST-01**; the
  rqlite-side UUID-history mirror, **ST-02**, is optional/post-v1.0 cross-check).

### Doc changes
- Rewrite **`docs/cluster-quorum-spec.md`** to the 100/1 model, the advertise→ack vote
  mechanism, the denominator rule, the eligibility/UUID-history guard, and the 9 s/10 s/30 s
  timing.
- Fix **`docs/quorum-design-notes.md`** contradictions: witness-state-loss is *not* a clear
  (D-01), single timing table (D-03), one vote model (D-04).
- Banner/rewrite **`docs/state-flow.md`** off the old blessed-master/HMAC/holddown model (D-02).

### Findings this closes
Q-01, Q-02, Q-03, Q-04, Q-05, Q-06, Q-07, ST-01, ST-02, D-01, D-02, D-03, D-04.

---

## BAD-2 … BAD-8 — pending (worked next, one at a time)

- **BAD-2 / BAD-3** — finish the `bedrock_d` rewrite; bedrock-d owns libvirt+DRBD boot;
  resolve the per-tier vs per-resource storage layout + VG name + DRBD port collision.
  *(decisions partially captured above; full detail next)*
- **BAD-4** — folded into BAD-1 (netd LMS race).
- **BAD-5** — folded into BAD-1 (witness membership filter).
- **BAD-6** — wire saga power-loss resume at boot.
- **BAD-7** — VM-failover correctness (node_leave crash, resumed-VM-killed, migrate UUID,
  multi-disk).
- **BAD-8** — boot/runtime safety (libvirtd; kopia; stale reference docs).
