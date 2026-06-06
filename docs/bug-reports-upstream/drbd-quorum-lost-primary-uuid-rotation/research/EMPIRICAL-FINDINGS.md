# Empirical RCA — DRBD 9.3.2, source-built, 4-node testbed (2026-06-06)

**Headline: it IS a real DRBD bug, and the trigger is `drbdadm resume-io`.** A frozen
quorum-lost minority Primary does **not** spontaneously mint a new current-UUID — but the moment
something calls **`drbdadm resume-io`** on it, it mints a new generation (marking the absent peers
weak) with **zero writes, while still `suspended:quorum`**. `resume-io` is exactly what bedrock-d's
`ensure_drbd_write_permission` issues. So the original incident's `new current UUID … weak:FFFF…FC`
was DRBD firing its unguarded UUID-rotation path in response to a bedrock-d `resume-io` on the
isolated minority master.

> Honesty note: an earlier pass here concluded "not a bug — needs force-promote + write." That was
> premature: the force-promote test demoted sim-1 first (P→S), which cleared the armed flag, so it
> needed a write. With sim-1 **kept Primary** (flag intact from losing peers), plain `resume-io`
> fires the mint. The original source-level "unguarded leak" analysis was right.

Setup: source-built kmod-drbd `9.3.2` (LINBIT release tarball) on all 4 sims (AlmaLinux 10.1,
kernel 6.12.0-124.8.1.el10_1). Self-contained resource `bugtest` on loop files,
`quorum all; on-no-quorum suspend-io; auto-promote no`. Harness `testbed/drbd_uuid_bug/repro.sh`.
Evidence: `evidence/resumeio-round-{1,2,3}/` and `evidence/correct-failover-round-{1,2,3}/`.

## Test matrix (sim-1 Primary, loses sim-3/sim-4, keeps sim-2)

| Action on sim-1 | Minted? | Notes |
|---|---|---|
| Frozen `suspended:quorum`, left alone (zero writes) | **NO** (4×) | stays at C indefinitely |
| `primary --force` while already Primary | **NO** | no-op |
| Heal — peers RETURN same generation | **NO** | susp_uuid edge fires but nothing to record |
| Demote → `primary --force` (S→P) | **NO** | demote cleared the armed flag |
| …then WRITE | YES (`EF63…`) | legitimate write-route mint after re-arm |
| **`drbdadm resume-io` (NO write, stays Primary, stays frozen)** | **YES, 3× (`F7AE…`,`6E00…`,`F58D…` all `weak:FFFFFFFFFFFFFFFC`)** | **THE BUG / THE TRIGGER** |

## Mechanism (now empirically + source confirmed)

1. A Primary that loses peer-data contact **arms** `__NEW_CUR_UUID` (`drbd_state.c:3096-3099`,
   the `lost_contact_to_peer_data` branch — gated on `role==R_PRIMARY && drbd_data_accessible`,
   **NOT on quorum**). The flag persists while the node is frozen.
2. The node freezes on quorum loss (`susp-io: no→quorum`) and, left alone, never mints — the
   execute paths don't fire while purely frozen.
3. **`drbdadm resume-io` triggers the armed execute** (the `4466` susp_uuid path /
   `drbd_check_peers_new_current_uuid`) → `drbd_uuid_new_current` → **new current-UUID minted,
   absent peers stamped weak (`FFFFFFFFFFFFFFFC`)** — even though the node is **still
   `suspended:quorum`** and wrote nothing. This is the unguarded leak: the mint is **not** gated
   on `have_quorum`/`!PRIMARY_LOST_QUORUM`, unlike the write route (`drbd_sender.c:3443`) and the
   disconnect route (`drbd_receiver.c:9886`, *"do not create the new UUID immediately!"*).

## RCA of the original incident

bedrock-d ran **`drbdadm resume-io` on the isolated minority master** (sim-1) — its
`ensure_drbd_write_permission` resumes IO on the side it believes should write. On the minority
that fired the armed mint → a sibling generation `L`. The majority then promoted its own writer
`M`. On heal: two children of the common ancestor → split-brain / full resync. *(The bedrock-d
code path + whether the current code still resume-io's the minority is being confirmed by the
`bedrockd-uuid-trigger-rca` workflow.)*

## bedrock-d RCA (code-confirmed, `bedrockd-uuid-trigger-rca` workflow, high confidence)

The exact bedrock-d path: `converge()` (`cluster_arbiter.py:1342`) calls
`ensure_drbd_write_permission()` (`:1382`) **before** the should-host/demote decision; that fires
`_drbd_resume_io()` → `drbdadm resume-io cluster` whenever
`last_election_outcome ∈ {leader,follower}` **and** `_drbd_suspended_quorum()` — with **no
independent DRBD-quorum recheck and no strict-leader rqlite read**. On a still-Primary armed
frozen node that resume-io fires the mint. The original split-brain = the election blessing the
armed-Primary side → resume-io → sibling generation, vs. the real winner's generation.

**Why it's mostly safe at HEAD (death-oracle, commit 94f6351):** a lone minority Primary computes
`NoQuorum` (never resumed); the far side becomes `Follower` and holds the arbiter DRBD **Secondary**
(arm needs Primary → can't mint); `demote_arbiter_host()` refuses to resume-io a frozen
quorum-lost node. So the headline exposure is closed — but the dangerous resume-io path is intact.

**Residual exposures (why "partially safe"):**
- **Stale denominator** (`lesson_node_drain_denominator_splitbrain`): the election denominator is
  read at `level='none'` from the local rqlite replica; an un-applied node-leave/maintenance can
  transiently flip a minority Primary to `LEADER`, satisfying the resume gate → mint. *The one path
  that can still feed an armed Primary a leader outcome.*
- **R3 hung-actuation**: a self-demoting minority master whose `secondary`/`.254` release hangs
  while netd keeps its slot FRESH could still resume-io its armed Primary on a stale read.
- VM-disk force-primary sites (`vm_failover.py:495`, `vm/create.py:233`) are a *related but separate*
  class — they DO gate on `_rqlite_quorate()` + strict-leader strong-read, and VM `.res` lacks
  `quorum all`, so they don't freeze/mint the same way (a separate asymmetry to review).

## How to be sure it won't happen again — two-pronged

1. **DRBD kernel ARM-guard (defense in depth, the durable guarantee):** add
   `&& !test_bit(PRIMARY_LOST_QUORUM, &device->flags)` at the `3096-3099` arm. Then the flag is
   never set on a quorum-lost Primary, so **even a wrong `resume-io` cannot mint.** *(Being
   validated now: build patched module → re-run the resume-io test 3× → expect NO mint.)*
2. **Bedrock orchestration (root cause):** bedrock-d must `resume-io` **only** the
   witness-blessed side, **never** the minority. The death-oracle witness + claim is meant to
   guarantee this; the RCA workflow checks whether the current code still has a window where the
   isolated minority master can `resume-io` before conceding.

## Still-correct from the earlier pass

- The validated config (`quorum all; on-no-quorum suspend-io; auto-promote no`) stays.
- The data path is safe: the minority commits **zero user bytes** regardless (it stays frozen);
  the leak is a metadata generation-UUID bump → false split-brain / full resync, not data loss.
- Correct failover (minority left frozen, never resume-io'd) → minority yields cleanly on heal,
  incremental resync of only the majority's writes (`evidence/correct-failover-round-*`).
