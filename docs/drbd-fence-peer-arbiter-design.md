# DRBD fence-peer as the bedrock-d arbiter interface — design + code-path reasoning

**Status:** IMPLEMENTED + VALIDATED end-to-end on the 4-node testbed (2026-06-06). Replaces the
`ensure_drbd_write_permission` / `resume-io` approach (the spurious-UUID root cause — see
`bug-reports-upstream/drbd-quorum-lost-primary-uuid-rotation/`). Code: `installer/lib/fence_verdict.py`
(verdict bridge + handler), `installer/lib/netd.py` (`_election_tick` publishes the verdict),
`installer/lib/tier_storage.py` (`fencing resource-only` + handler in the `cluster` .res),
`installer/lib/cluster_arbiter.py` (deploys the handler, `drbdadm adjust`). Campaign:
`testbed/drbd_uuid_bug/arbiter_campaign.py`.

> **CRITICAL CORRECTION (2026-06-06): the verdict gate is TWO conditions, not "fresh+stable".**
> The first implementation gated the handler on a *fresh + stable* verdict and **split-brained on the
> very first isolate-the-Primary test**: DRBD detects a lost peer in ~3 s, but netd's membership is
> gated by `DOWN_HYSTERESIS ≈ 10 s`, so at +3 s the file still said `leader` and was both fresh (netd
> rewrote it ~1 s ago) and stable (leader for minutes) — yet wrong. The handler WON on that stale value
> and the minority Primary minted a sibling. The fix (see "The verdict gate" below): netd now publishes
> the **reachable set** (loopback octets it currently reaches, incl self) alongside the outcome, and the
> handler acts only once the LOST peer is **absent from `reachable`** (proof netd has seen *this*
> partition) **and** the (outcome, reachable) tuple is **stable** (no false-leader transient while the
> set shrinks). Validated: isolate-Primary → loser LOSE(6) @~23 s, **no sibling mint**, winner
> force-promotes (its fence-peer fires WIN on the promote path → outdates the old master → regains
> quorum), heal clean (no split-brain, one master). See `lesson_fence_peer_stale_verdict` in memory.

## The idea (the heart of it)

A partition freezes both sides (`quorum all` is deliberately dumb). Exactly one side must be told
"keep writing", the other "yield". That authority is **bedrock-d's election + external witness**.
DRBD already ships the right interface for *"a Primary lost a peer — external arbiter, what do I
do?"*: the **fence-peer handler**. DRBD calls our script and **acts on its exit code**. We answer
from the witness. No polling, no `resume-io`, no race.

This replaces the racy poll→resume with DRBD's own native, synchronous callout — exactly what
Pacemaker uses, except the authority is **us**, not a CIB.

## Config (arbiter `cluster` resource)

```
resource cluster {
  options { quorum all; on-no-quorum suspend-io; auto-promote no;
            on-suspended-primary-outdated force-secondary; }
  net     { fencing resource-only; }          # <-- the change: was unset (= dont-care)
  handlers {
    fence-peer   "/usr/lib/bedrock/bedrock-fence-peer";
    unfence-peer "/usr/lib/bedrock/bedrock-unfence-peer";   # clears state on heal
  }
}
```
`fencing resource-only` (FP_RESOURCE), not `resource-and-stonith` (FP_STONITH): we want the
handler to *decide*, and let `quorum`'s `suspend-io` own the freeze. STONITH would add a redundant
`susp_fen` freeze and imply we actually power-fence, which we don't.

## Code path 1 — when DRBD calls us

`conn_disconnect()` (`drbd_receiver.c:10123-10126`), after a connection drops:
```c
if (resource->role[NOW] == R_PRIMARY &&
    connection->fencing_policy != FP_DONT_CARE &&
    conn_highest_pdsk(connection) >= D_UNKNOWN)
        conn_try_outdate_peer_async(connection);
```
- Fires **only on the current Primary (master)**, **per lost peer**, only if `fencing` is set
  (default `FP_DONT_CARE` → never fires; that's why bedrock never saw it). A 2v2 split where the
  master loses 2 peers → **2 handler calls**.
- `_async` → runs in its own kernel thread (`drbd_receiver.c:10126` → `kthread`), so the main DRBD
  worker is **not** blocked while our handler runs.
- **Consequence for the design:** fence-peer only covers *"should the existing master continue or
  step down"*. Promoting a **new** master on a Secondary-only winning side is still bedrock-d's
  `drbdadm primary --force` (which itself outdates absent peers → regains quorum). So:
  fence-peer = old-master continue/yield; force-promote = elect a new master. `resume-io` is gone.

## Code path 2 — what our exit code does

`conn_try_outdate_peer()` (`drbd_nl.c:840-941`):
- `fencing_policy == FP_DONT_CARE` → `return true` immediately, handler never called
  (`drbd_nl.c:871`).
- else `drbd_maybe_khelper(..., "fence-peer")` → `call_usermodehelper(..., UMH_WAIT_PROC)`
  (`drbd_nl.c:767`) → **blocks the async thread until our script exits**, reads its exit code.
- exit-code switch (`drbd_nl.c:879-917`), the verdict vocabulary (`drbd.h:413-419`):
  | exit | meaning | DRBD action | our use |
  |---|---|---|---|
  | 4 `P_OUTDATED` | "peer was fenced/outdated" | `__downgrade_peer_disk_states(D_OUTDATED)` — mark the **lost peer** outdated | **I WIN** |
  | 6 `P_PRIMARY` | "peer is active, outdate myself" | `__downgrade_disk_states(resource, D_OUTDATED)` — mark **myself** outdated | **I LOSE / yield** |
  | 5 `P_DOWN` | peer unreachable | outdate peer if local UpToDate | (≈ win, peer dead) |
  | 3 / unknown / broken | — | `abort_state_change` → **"Eventually leave IO frozen"** | undecided → stay safe |
- Handler env (`drbd_nl.c:690-738`): `DRBD_RESOURCE`, **`DRBD_PEER_NODE_ID`** (which peer we lost),
  `DRBD_CSTATE`, `UP_TO_DATE_NODES=0x…` mask. Enough for the script to ask bedrock-d *"for resource
  X, I lost node N — did I win?"*.
- No kernel timeout on the handler → **the handler self-bounds**: it may block up to a confirm
  window (~election convergence, ~10 s) waiting for the witness verdict, then return. **The
  blocking handler IS the confirm window** — no separate state machine.

## Code path 3 — winner regains quorum (the outdate→quorum chain)

Winner's handler returns **4** → lost peers go `D_UNKNOWN → D_OUTDATED`. On the committing state
change, `calc_quorum()` (`drbd_state.c:1547-1553`):
```c
/* When all the absent nodes are D_OUTDATED (no one D_UNKNOWN) ... remove them from voters */
if (qd.unknown)  voters = qd.outdated + qd.quorumless + qd.unknown + qd.up_to_date + qd.present;
else             voters = qd.up_to_date + qd.present;     /* <-- outdated peers DROP OUT */
```
Once **all** lost peers are `D_OUTDATED` (`qd.unknown == 0`), voters = just the present side →
`quorum all` is satisfied with the present nodes → quorum regained → `susp_quorum` cleared
(`drbd_state.c:2473-2474`) → winner **unfreezes and writes**. That write legitimately mints a new
current-UUID marking the outdated peers weak (it *is* the new generation) — **correct**, because
the losers are `D_OUTDATED`, not sibling generations, so on heal they cleanly resync (SyncTarget),
**no false split-brain.**

## Code path 4 — loser yields, never mints (kills the bug)

Old master on the **losing** side: handler returns **6** → `__downgrade_disk_states(D_OUTDATED)`
marks **its own** disk outdated. It did **not** outdate its peers, so it does **not** regain
quorum → stays `suspended:quorum` → **never unfreezes → the armed `__NEW_CUR_UUID` never fires →
no mint.** On heal it is `D_OUTDATED` → SyncTarget → clean incremental resync.

This is the precise fix: the bug was the loser minting a **sibling** generation (via `resume-io`).
Here the loser is **outdated**, which is *not* a sibling — outdated nodes yield cleanly. And there
is no `resume-io` to mis-fire.

## The verdict gate (two conditions — the part that took a testbed split-brain to get right)

The handler asks "should I (a Primary that just lost peer P) continue or yield?" The authority is
netd's election, but **netd detects the partition slower than DRBD does** (DRBD ~3 s on a cut peer;
netd's reachable membership is gated by `DOWN_HYSTERESIS ≈ 10 s`). A handler that trusts a merely
*recent* "leader" verdict acts on netd's *pre-partition* view and wins when it should lose. So netd
publishes, every election tick, `{outcome, updated, stable_since, reachable, self_octet}` where
`reachable` is the set of loopback last-octets it currently reaches (incl self), computed from the
**same** `election.compute` result as `outcome` (they can never disagree). The handler maps
`DRBD_PEER_NODE_ID → loopback octet` from the local `drbdadm dump cluster` (config-only, no netlink →
safe inside a fence callout) and acts only when **all three** hold:

1. **FRESH** — `updated` within `FRESH_S` (3 s). Else netd is wedged → leave IO frozen.
2. **PEER-EXCLUDED** — the lost peer's octet is **not** in `reachable`. This is the load-bearing
   gate: it proves netd has independently observed *this* partition, closing the DRBD-vs-netd
   detection gap. Until then the handler waits.
3. **CONVERGED** — the `(outcome, reachable)` tuple has been **stable ≥ `STABLE_S`** (3 s).
   `stable_since` resets whenever *either* changes, so the reachable set shrinking step-by-step
   during hysteresis settling (a brief `{self+2}=leader` on a node that is really isolated) never
   decides — only the settled membership does.

Then: `outcome == leader → exit 4` (win), else `→ exit 6` (lose). Up to a deadline (`STABLE_S+…`,
~25 s), anything else → `exit 1` (freeze — DRBD's safe default). **Measured timing** (isolate the
Primary): fence-peer fires +1.6 s, decides LOSE +23 s (≈ DRBD detect + ~10 s netd hysteresis + the
stability window) — consistent with the ~10 s `MASTER_LOSS_MISSES` .254 failover cadence. "Instant
does not exist in distributed systems": the handler **waits for agreement**, it does not race.

## What bedrock-d implements

1. `.res`: add `fencing resource-only` + the two handlers (above). Keep `quorum all`,
   `on-no-quorum suspend-io`, `auto-promote no`, `on-suspended-primary-outdated force-secondary`.
2. **`bedrock-fence-peer`** (small script DRBD runs): reads `DRBD_RESOURCE` + `DRBD_PEER_NODE_ID`,
   queries the **running bedrock-d** over the loopback API for the witness/election verdict for
   *this node vs that peer* (waiting up to the confirm window for convergence), and `exit 4` if we
   win (outdate the peer) / `exit 6` if we lose (outdate ourselves) / `exit 3`(or non-decisive) to
   leave IO frozen if undecided.
3. **Delete** `ensure_drbd_write_permission` / `_drbd_resume_io` — no longer the mechanism.
4. Keep `_drbd_promote` (`drbdadm primary --force`) for electing a **new** master on a
   Secondary-only winning side; it outdates absent peers → regains quorum the same way.
   **Confirmed in DRBD source** (`drbd_nl.c:1177,1213`, the `drbd_set_role` path): promoting a node
   with `D_UNKNOWN` peers under `fencing` calls `conn_try_outdate_peer` → our handler → WIN(4) →
   outdate the isolated old master → quorum returns. So the fence-peer serves BOTH the old master
   (conn-loss path → LOSE) and the new master (promote path → WIN).
5. `bedrock-unfence-peer` clears any per-peer state on heal.

### Known follow-up — the loser's clean demote (the gating bug for *clean* auto-failover)

**Campaign result (2026-06-06):** the fence-peer arbitration is solid — **no spurious sibling mint
in 9/9 partition iterations** (A×3 WIN, B×3 LOSE, C 2v2), correct LOSE(6)/WIN(4) every time, clear
timing (cut → DRBD detect ~5–10 s → fence fires → WAIT for netd convergence → decide @~13–24 s →
quorum regain / winner force-promote → heal). A **single** clean failover works end-to-end. But the
**heal path** has a real, separate bug, root-caused precisely:

The losing old master returns `exit 6` → `D_OUTDATED` → `on-suspended-primary-outdated
force-secondary` *should* demote it. It can't: the node is **frozen** (`suspended:quorum`, IO to the
DRBD device blocked by `quorum all`), and the arbiter FS + arbiter-rqlite hold the device **open** —
`drbdadm secondary` / force-secondary refuse "`-12 Device is held open`", and you cannot drain a
frozen device to umount it. So the loser **stays a frozen Primary**. On heal, when it reconnects to a
*winning-side follower* (which already resynced to the new master's generation), DRBD sees 1-Primary
(`after-sb-1pri`) / 2-Primary (`after-sb-2pri disconnect`) instead of the clean 0-Primary case its
`after-sb-0pri discard-zero-changes` policy would auto-resolve → the pair goes **StandAlone**, which
breaks `quorum all` for the whole non-master set → resync stalls. (Three fixes already landed that
make this *recoverable* and stop it cascading: `/proc/mounts`-based mount detection + lazy-umount of
the EIO zombie (f745d31), and clearing a stale mount in the promote path (5d89355).)

**Data is never at risk** — the loser is frozen (no writes, gen verified unchanged), and recovery is
mechanical (`umount -l` the loser + `drbdadm connect --discard-my-data` its StandAlone links → resync
from the master). But auto-failover is not *clean* until the loser demotes deterministically. **Proper
fix (next piece):** on observed loss/outdate, bedrock-d must FORCE-release the frozen device — kill
the arbiter-rqlite + mount holders, `umount -l`, then `drbdsetup secondary --force` — so the demote
completes to Secondary *before* heal, letting `after-sb-0pri` auto-resolve. This gates VM-disk fence
arbitration (P3) too, since VM disks would inherit the same frozen-loser-demote path. Tracked in
`lesson_fence_peer_stale_verdict` + `lesson_arbiter_eio_zombie_mount`.

## Validation results (testbed, STOCK/unpatched 9.3.2, 2026-06-06)

Harness `testbed/drbd_uuid_bug/fence_validate.sh`; evidence
`bug-reports-upstream/drbd-quorum-lost-primary-uuid-rotation/evidence/fence-validate/`.
Controllable stub handler at `/usr/local/bin/test-fence-peer` (reads `/tmp/fence-verdict`,
exits 4=win / 6=lose). **All confirmed.**

- **V1 — invoked: YES.** `drbdsetup show` shows `fencing resource-only`. On the 2v2 cut the master
  ran `/sbin/drbdadm fence-peer` **twice** (once per lost peer), env carried
  `DRBD_PEER_NODE_ID=3` then `=2`. Async (`[outdate-async]`), main worker not blocked.
- **V2 — WIN (exit 4): master continues, legit mint.** `returned 4 (peer was fenced)` →
  `pdsk(DUnknown → Outdated)` for both peers → **`quorum( no -> yes )`** → `susp-io(quorum → uuid → no)`
  → **one** `new current UUID … weak:FFFFFFFFFFFFFFFC`. Disk states `UpToDate/Outdated/Outdated/UpToDate`,
  role Primary, **unfrozen**. The mint is correct (winner's new generation; losers Outdated, not siblings).
- **V3 — LOSE (exit 6): master frozen, NEVER mints.** `returned 6 (peer is active)`; the self-outdate
  is **`Refusing to be Outdated while Connected (-6)`** (it still has its partner sim-2), so the node
  stays `UpToDate` but **`suspended:quorum`**, and **`MINT_LINES=0`** — the bug does not fire.
- **V4:** handler runs on the async `outdate-async` thread; IO stays frozen by quorum throughout.
- **V5:** both cases healed to all-UpToDate; the win-case losers were `Outdated` → clean resync, **zero
  `new current UUID` on the losers**.

### One nuance to carry into the bedrock-d handler
The LOSE verdict's `exit 6` self-outdate is **refused while the loser still has a connected partner**
(its own minority partition). That is harmless — the node stays frozen and never mints — and it
**yields on heal via the already-set `on-suspended-primary-outdated force-secondary`** (demotes to
Secondary when it meets the newer-generation winner). So the loser path = *fence-peer keeps it frozen
(no mint)* + *force-secondary demotes it on heal*. (Optionally the loser handler can simply leave IO
frozen — `exit 1` → "leave IO frozen" — for the same safe outcome with less log noise.)

### Net
The fence-peer-as-arbiter model is **validated on stock DRBD**: WIN → outdate-losers → regain-quorum →
continue (legit mint); LOSE → stay frozen → never mint → yield via force-secondary. **No `resume-io`,
no kernel patch, no spurious sibling-generation fork.** The only remaining engineering is the real
`bedrock-fence-peer` handler that returns 4/6 from the witness verdict (exclusive — only one side may
get WIN), and removing `ensure_drbd_write_permission`/`_drbd_resume_io`.

---

## Does this extend to VM disks? NO — different mechanism (dragon-hunt, code-verified, high confidence)

**Verdict: do NOT extend the fence-peer / witness-CLAIM model to VM disks.** It is coupled to three
substrate properties the VM tier deliberately lacks:
1. The witness slot is **one-per-node, arbiter-only** (single `MARKER_KIND_DRBD_ARBITER_UUID`,
   `installer/lib/witness.py:30,77`) — it cannot carry a per-VM win/lose verdict.
2. fence-peer fires only on a node **already Primary with `fencing` set** (`drbd_receiver.c:10123`);
   the VM-failover taker is a **Secondary** doing `disconnect`+`primary --force`
   (`bedrock_d/orchestrator/vm_failover.py:485-497`), and VM `.res` has **no `fencing`**
   (`bedrock_d/vm/drbd_config.py:108-115`) → `FP_DONT_CARE` → never fires.
3. fence-peer exists *because the arbiter can't read the rqlite it's recovering*; VM disks **have**
   rqlite (`_rqlite_quorate` + the `level=strong` UUID gate). Two intentionally-separate consistency
   classes (`feedback_read_consistency_classes`).

VM disks stay on the shipped availability-first model: `auto-promote no` + `allow-two-primaries no`
+ `after-sb-*` + `_rqlite_quorate` (liveness) + the **fail-loud** `get_recorded_uuid(level=strong)`
gate + suspend(T+20)/takeover(T+35)/kill(T+5m).

### Real dragons found — in the EXISTING VM-failover path (not the extension)
- **D4 (serious):** hung-old-primary gap is **timing, not interlock**. Old primary suspends ~T+5-9s
  (userspace marker), survivor force-promotes at T+35s; a hung-but-not-dead old primary
  (`no_quorum_responder` `asyncio.wait_for(30s)` times out, logs, *continues*) keeps flushing while
  the survivor promotes (peer-down = heartbeat-age, not death-confirmed) → concurrent writes →
  divergence. The VM analogue of cluster R3, **without** the `suspend-io` kernel backstop.
- **D5 (serious):** the takeover `level=strong` UUID read is **tautological** — `_takeover_one`
  promotes → writes its own UUID → reads it back → compares to its own local UUID (no DRBD-mutating
  call between); proves "my write committed", not "no peer raced a promote". The code's own
  docstring (`bedrock_d/vm/failover.py:182-183`) admits it. Genuine on cold-start, blind on takeover.
- **D3 (inherent):** no quorum → a minority VM Primary keeps writing + rotating UUID → sibling
  generations → `after-sb` discard (data loss on the discarded side), not clean Outdated→SyncTarget.
- **D8 (inherited):** the denominator-shrink split-brain (`lesson_node_drain_denominator_splitbrain`,
  deferred) lets a minority pass `_rqlite_quorate` and reach the strong-read; fence-peer wouldn't fix
  it (election bug) — the `#7` all-applied watermark is the fix.

### Recommended VM-disk hardening (instead of fence-peer), priority order
1. **Positive death-confirmed interlock before `drbdadm primary`** in `_takeover_one`: require the
   home node provably down/suspended (witness `HOSTING=0` / stale slot — the death-oracle signal),
   not merely heartbeat-silent. Userspace; closes D4/D5 — the one real gap fence-peer was meant to fill.
2. **Reorder the strong-read to PRE-promote** (mirror arbiter step 3) so it catches a behind/raced disk.
3. `migrate` UUID-record should **fail the saga** (not warn-continue) on unreadable post-promote UUID.
4. (vipet/3-peer only, v1.x) `quorum majority` + `suspend-io` kernel backstop — needs a per-VM resume
   authority that does not exist; never on cattle(1)/pet(2).
5. Make the even-split VM-failover stall **operator-visible** (today a silent logged skip).

**Sequencing:** the cluster-tier fence-peer handler is still STATUS=design (the real `bedrock-fence-peer`
does not exist yet; `cluster_arbiter.py:1382` still calls the to-be-removed `_drbd_resume_io`). Land +
validate the cluster-tier handler first, then revisit VM disks.
