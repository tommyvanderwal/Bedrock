# The witness is a death-oracle, not a vote

*The one idea: a witness tells an isolated node **whether the master is DEAD or
just unreachable-from-me**. It is not a vote two sides fight over. This is the
whole of how a clean partition is handled without split-brain — and it removes
the multi-witness contest, the settle-barrier, and the majority-of-witnesses
read entirely. Compare this doc with `election.compute`, `cluster_arbiter`, and
`witness.py`; if they ever disagree, one of them is wrong.*

---

## The four rules

```
 1. DEATH-ORACLE     A node treats the master as ALIVE if the master's witness
                     slot is FRESH and flagged HOSTING — even when the mesh
                     can't reach it. No takeover while the master is alive.

 2. MAJORITY WINS    Defer to a live master ONLY if you do not hold a
                     node-majority yourself. A clean node-majority always
                     takes over (the minority master must yield).

 3. INCUMBENT CLAIMS The master claims the witnesses it can reach when it is
                     PIVOTAL (its node-votes are within reach of quorum), to
                     keep its own side quorate. Challengers defer (rule 1), so
                     the claim is uncontested — there is never a witness race.

 4. SELF-DEMOTE      A master that cannot reach quorum even with the witnesses
                     it can reach RELEASES .254, stops the arbiter, and CLEARS
                     its HOSTING flag (slot → non-master). Now a survivor may
                     take over.
```

## The slot gains one bit

```
   tag bitflags:   bit 0 = CLAIM    (I am using this witness's pivotal vote)
                   bit 1 = HOSTING  (I am ACTUALLY the arbiter host right now)   ◄── new
```
`HOSTING` is **actuation-truth**: set only while this node is genuinely
DRBD-Primary on `cluster` + `.254`-bound + arbiter rqlite up. A master that is
alive but cannot actuate (or has demoted) reads as **not** hosting, so freshness
alone never pins `.254`.

The death-oracle is therefore exactly: `master alive ⟺ slot FRESH ∧ slot.HOSTING`.

---

## What actually happens, by partition shape

### Clean even split (2+2) — nobody died, so nothing moves
```
   {A1,A3}  ──X── (mesh down) ──X──  {B2,B4}=master side,  witness reachable from both

   B2 (master): 200 nodes < 201, but PIVOTAL → claims the witness → 201 → stays LEADER,
                keeps .254 + rqlite quorum + its VMs.                       ◄── in place
   A1 (far side): mesh says "B2 gone", but B2's slot is FRESH+HOSTING
                → master ALIVE → A1 is a FOLLOWER, does NOT take over, does NOT claim.
                → A1's side has no rqlite write-quorum → READ-ONLY, waits for heal.
                  Its VMs suspend; movable ones (replica on B) consolidate to B
                  (DRBD-UUID-gated); the rest wait. On heal: no steal-back.
```
One quorate side (the incumbent's). The far side stands down. No contest, no race.

### Lopsided split (1+3) — the majority wins, the lone master yields
```
   B2=master (1 node)  ──X──  {A1,A3,A4} = 3 nodes

   B2: 100 + witness 1 = 101 < 201 → CANNOT reach quorum → SELF-DEMOTE (rule 4):
       release .254, stop arbiter, clear HOSTING.
   A-side: 300 ≥ 201 → NODE-MAJORITY → does NOT defer (rule 2) → takes over.
       Its takeover waits until it reads B2's slot as NOT HOSTING (or stale) — so
       B2 releases FIRST, then A promotes.  (INV-1 via the HOSTING transition.)
```

### Master genuinely DOWN — survivors take over; an even survivor-split just waits
```
   master M down → its slot goes STALE (or HOSTING=0).  Survivors run takeover.
   The ONLY case two survivors could contend is an even survivor-split — and:

       master still counts in the denominator (no auto-shrink):
       node_votes = 50·(N−1) ,  majority = (100N+W)//2 + 1
       deficit    = ⌈W/2⌉ + 51  ≥ 51   →  NOT pivotal (≥ 50)  →  both NoQuorum → WAIT

   So an even survivor-split NEVER reaches quorum on either side. No dual-takeover,
   no witness contest — ever.  This is why no settle-barrier / SCSI-PR register /
   majority-of-witnesses read is needed.
```

---

## Why this is split-brain-free (the whole argument, short)

- **Two live masters can't coexist:** a far node only takes over when the master's
  slot is stale **or** HOSTING=0; a live, actuating master keeps it FRESH+HOSTING,
  so its takeover is refused — *unless* the far node has a node-majority, in which
  case the master is a provable minority that self-demotes (rule 4) and releases
  HOSTING before the majority promotes.
- **A clean even split never moves the master:** neither side has a node-majority,
  the incumbent holds the witness uncontested (challengers defer), so it stays
  quorate and everything follows it.
- **Witnesses are never a contestable vote:** the only place two nodes could fight
  over a witness is an even survivor-split, which is *proven* never pivotal.
- **The data layer is backstopped independently:** VM consolidation to the quorate
  side is gated by the per-VM DRBD-generation (bit-0-masked) check, and every
  irreversible commit is a strict-leader rqlite read — so even a transient `.254`
  overlap cannot diverge data.

## What this deliberately does NOT solve (kept separate)

- **R3 — hung actuation.** A minority master that *decides* to self-demote but whose
  `drbdadm secondary`/`.254` release HANGS, while netd keeps its slot FRESH+HOSTING.
  The node-majority then takes over → a transient dual-`.254`. Contained by DRBD
  `after-sb-2pri disconnect` + the strong-read VM gates (no data divergence), but a
  truly clean answer wants a fence/STONITH. Tracked as R3, not here.

---

## Map to code (search anchors)
- `election.compute` — `master_is_alive = current_master ∧ (mesh-reachable ∨ master_witness_alive)`. Deference: `FOLLOWER` iff `master_is_alive ∧ (mesh_reachable_master ∨ ¬i_have_node_majority)`. So a **mesh-reachable** master is always followed (steady state); rule 2's node-majority bound only diverts to takeover when the master is alive *only via the witness* (a real partition). A node-majority then falls through to the promote/ack branch.
- `netd._election_tick` — computes `master_witness_alive = master_slot FRESH ∧ master_slot.HOSTING`; the master is hidden from `compute` only when `master_lost (mesh) ∧ ¬master_witness_alive`.
- `witness.py` — `TAG_HOSTING = 0x02`, `Slot.hosting`.
- `cluster_arbiter` — `ensure_witness_claim` publishes the complete tag each Leader tick: `HOSTING` while genuinely hosting (actuation-truth), `+ CLAIM` only when pivotal; `demote_arbiter_host` clears it (`tag=0`); takeover step 2 refuses iff the master slot is `FRESH ∧ HOSTING`, proceeds on `stale ∨ ¬HOSTING`; self-demote when `node_votes + reachable_witnesses < majority` (the election NoQuorum path).
- **RCA note:** `HOSTING` is sticky — netd republishes `own_tag` every heartbeat, so a transient witness-read miss in `ensure_witness_claim` does NOT drop the flag; only an explicit demote (`tag=0`) clears it. So `HOSTING ⟹ actually hosting`, and a live host never falsely reads as non-hosting within the freshness window.

---

## Live test coverage (testbed sims, N=4 / W=1 → majority 201)

The proven isolation (drop `br0` except the workstation + down mesh/DRBD NICs
`enp2s0..enp5s0`, keep `enp1s0`/`br0` so SSH + witness survive) drives three
scripts that together exercise every death-oracle branch:

- **`testbed/test_death_oracle_partitions.sh`**
  - *A — isolate a non-master* → it sees the master slot FRESH+HOSTING → **defers**
    (no takeover, master keeps `.254`). Restore → rejoins, no steal.
  - *B — isolate the master* → its slot goes STALE (it cannot refresh the witness)
    → `master_witness_alive` flips false → the 3-node majority promotes (node-majority
    bound), the isolated master self-demotes (`tag=0`, releases `.254`). Exactly one
    `.254` holder throughout; the returning ex-master defers (no steal-back).
- **`testbed/test_death_oracle_claim.sh`**
  - *C — isolate TWO non-masters* → master keeps one follower = 200 votes, `deficit=1`
    (pivotal: `0<1<50`) → master **CLAIMS** the witness (`hosting=1 claim=1`) → reaches
    `201/201` → keeps `.254`; the two isolated singletons defer. On heal the master
    **releases** the claim (`hosting=1 claim=0`) once node-majority returns.

**Observed (benign):** during a claim the Echo stub (workstation, eventually-consistent)
can log `witness claim set but readback not yet confirmed (witness slow/unreachable);
netd will keep publishing`. The master counts its own claim optimistically and holds
`201/201` while netd re-publishes each tick until the readback confirms — the "witness
slow" graceful-degradation path, not a fault.

**One live edge intentionally left to the math:** a true 2+2 split where the *far*
pair is itself a 2-node pair (200 votes, also pivotal) and must defer purely on the
master's FRESH+HOSTING bit — not on lack of node-majority. The defer-on-HOSTING half
is covered by A, the pivotal-claim half by C, and the "even split is never two-sided-
pivotal when the master is DOWN" half by `deficit ≥ 51` (unit test
`test_even_survivor_split_is_never_pivotal`). A live 2+2-pair partition needs
selective per-peer iptables (not blanket NIC-down) and is deferred as marginal
confidence at real bricking risk.
