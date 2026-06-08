# DRBD fence-peer as the bedrock-d arbiter interface — design + code-path reasoning

**Status:** IMPLEMENTED + VALIDATED end-to-end on the 4-node testbed (2026-06-06). Replaces the
`ensure_drbd_write_permission` / `resume-io` approach (the spurious-UUID root cause — see
`bug-reports-upstream/drbd-quorum-lost-primary-uuid-rotation/`). Code: `installer/lib/fence_verdict.py`
(endpoint-side `decide_fence`/`feed_down` + the HTTP-calling handler), `mgmt/app.py`
(`POST /internal/fence-decision`), `installer/lib/netd.py` (`_election_tick` consumes the DRBD
"peer down" evidence + publishes `fence_view`), `installer/lib/state_shared.py` (`drbd_down_peers`
+ `fence_view`), `installer/lib/tier_storage.py` (`fencing resource-only` + handler in the `cluster`
.res), `installer/lib/cluster_arbiter.py` (deploys the handler, `drbdadm adjust`). Campaign:
`testbed/drbd_uuid_bug/arbiter_campaign.py`.

> **THE MODEL IS A SYNCHRONOUS CALL, NOT A FILE (2026-06-06, Tommy's correction).**
> DRBD's external fencing is meant to be a synchronous *act* — claim the witness then and there on the
> split — not a passive read of pre-computed state. The first two attempts used a `/run/.../fence-verdict.json`
> file (netd's 1 Hz election outcome, materialised). Both were wrong for the same reason: the file is
> *downstream of the very event it arbitrates*, so it lagged the partition (the loser's file still said
> `leader` at +3 s → minority Primary won → split-brain, reproduced on the testbed), and a file can't
> perform the **exclusive witness claim** that makes an even split safe (two stale files can both read
> `leader`; one witness claim has exactly one winner).
>
> **Now:** `bedrock-fence-peer` POSTs to bedrock-d's `:8001` HTTP-loopback `/internal/fence-decision`.
> bedrock-d feeds DRBD's **authoritative** per-peer "down" evidence into netd's election
> (`shared_state.drbd_down_peers`) — DRBD detects a lost peer in ~3 s, mesh liveness only after
> `DOWN_HYSTERESIS ≈ 10 s`, so this **collapses the detection lag**. netd forces those peers' liveness
> False, converges the election on the real partition, and drives the **exclusive** witness claim
> (`ensure_witness_claim`); it publishes the converged verdict to `shared_state.fence_view`.
> `decide_fence()` waits for that view to (a) ACK the evidence and (b) be STABLE, then returns
> win/lose → handler `exit 4`/`6`/`1`. The file + its two-gate polling are deleted.
> Validated (single B): isolate-Primary → DRBD freezes @+3.4 s (safe, no writes), **LOSE(6) @+15 s**
> (vs ~23 s for the file — the evidence-feed is faster), **no sibling mint** (gen unchanged), winner
> promotes, heal clean (resync +30 s, no split-brain, one master). See `lesson_fence_peer_stale_verdict`.

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

## The decision flow (synchronous call + DRBD-evidence-fed election)

The handler asks "should I (a Primary that just lost peer P) continue or yield?" The authority is
netd's election + the **exclusive witness claim**, and both legitimately live in netd (it owns the
witness socket and the global membership view). The handler does **not** do witness IO itself (that
would race netd) and does **not** read a pre-computed file (stale — see the banner). Instead:

1. **Handler → endpoint.** `bedrock-fence-peer` maps `DRBD_PEER_NODE_ID → loopback octet` from the
   local `drbdadm dump cluster` (config-only, no netlink → safe inside a fence callout) and POSTs
   `{resource, peer_octet}` to `:8001 /internal/fence-decision` (loopback, no TLS). One blocking call.
2. **Endpoint feeds DRBD's evidence into netd.** `feed_down()` records `peer_octet` in
   `shared_state.drbd_down_peers`. On its next tick `_election_tick` forces that peer's `peer_liveness`
   **False** (overriding mesh hysteresis — DRBD's detection is authoritative and ~7 s faster). This is
   the key: DRBD's fast per-peer detection becomes an *input* to netd's global election, collapsing the
   `DOWN_HYSTERESIS` lag. A peer with a probe fresher than `FENCE_PEER_FRESH_S` (3 s) is provably back
   *now*, so its stale evidence is ignored + cleared (heal recognised immediately).
3. **netd converges + claims.** The election recomputes on the real partition; on the Leader path
   `ensure_witness_claim` drives the **exclusive** witness claim (the even-split tiebreak). netd
   publishes `{outcome, down_acked, stable_since, updated, self_octet}` to `shared_state.fence_view`.
4. **Endpoint waits for the converged verdict.** `decide_fence()` polls `fence_view` until it is
   (a) **FRESH** (`updated` within 3 s — else netd wedged → freeze), (b) **ACKed** (`peer_octet ∈
   down_acked` — netd's election has incorporated this loss), and (c) **STABLE** (`stable_since` held
   ≥ `DECIDE_STABLE_S` — a simultaneously-isolated master's per-peer evidence arrives over a few ticks,
   so wait for the membership to settle). Then `leader → win`, `follower|noquorum → lose`. Deadline
   (`DECIDE_DEADLINE_S ≈ 18 s`) → `undecided`. Handler maps `win→exit 4`, `lose→exit 6`, `undecided→exit 1`.

Why the loser is fast and the winner is correct: a fully-isolated old master, once all its peers'
down-evidence is in, is a clear sub-majority → `noquorum` → **LOSE** in ~seconds (no waiting on acks).
The winning side promotes via `cluster_arbiter` (force-promote fires the fence on the *promote* path),
and by then netd has already elected it Leader (acks propagated) → **WIN** → outdate the old master →
regain quorum. **Measured (isolate the Primary):** DRBD freezes @+3.4 s (safe, no writes), endpoint
returns **LOSE @+15 s** (vs ~23 s for the old file), no sibling mint, heal clean. "Instant does not
exist in distributed systems" — the handler **waits for agreement** (the converged, claimed verdict),
it does not race; but DRBD's evidence makes that agreement arrive faster.

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

### The loser's clean demote — RESOLVED (force-release, 2026-06-07)

**Status: FIXED + validated.** The realistic test — fail, wait for FULL reconvergence, only then fail
again (NOT failover-during-convergence, which is a different/unrealistic scenario) — passes **B×3 = 3/3**:
each failover force-demotes the loser, the winner promotes, heal resyncs clean (~10–15 s), one master
persists, and between failovers the cluster fully reconverges (every loser returns to a clean,
promote-able Secondary within the ~1 min + resync budget). Plus the fence arbitration itself: no
spurious sibling mint in *every* partition iteration, correct LOSE(6)/WIN(4).

**The fix** (`_force_release_drbd`, commit `e34690c`): on observed loss the frozen branch of
`demote_arbiter_host` HARD-releases — `drbdsetup secondary --force=yes` (demotes a frozen Primary in
~12 ms, EIOing held IO, **never** resume-io), then SIGKILLs the mount holders BY SERVICE NAME
(rqlited-arbiter + weed-filer + weed-s3 — **never** `fuser -m`, which on an EIO zombie falls back to
`/` and kills the box), then `umount -l`. The loser is Secondary before heal → reconnects 0-pri →
`after-sb-0pri discard-zero-changes` auto-resolves. The original root cause, for the record:

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

## Extending to VM disks — SHIPPED (P3, 2026-06-08): same hook, rqlite-ownership decision

> **Reversal of the earlier "NO" verdict.** An earlier draft argued against extending fence-peer to
> VM disks because the *witness-CLAIM* decision is arbiter-only. That objection dissolves once you
> separate the **hook** (DRBD's `fence-peer` callout) from the **decision authority**: the cluster
> singleton decides via netd-election + witness CLAIM; a per-VM disk decides via a **`level=strong`
> rqlite read of `vms.host`**. Same synchronous-callout hook, different authority. Tommy's call:
> *"a 2-way and a 3-way DRBD should always freeze and wait for bedrock-D to decide which side is in
> the Bedrock majority"* — a per-VM disk has no quorum tiebreaker of its own, so it must never
> self-decide. This section documents what shipped.

### Config (per-VM `vm-<name>-disk<N>`, `bedrock_d/vm/drbd_config.py`)
- `fencing resource-and-stonith` — on **any** replication-link loss DRBD **suspends ALL IO**
  (`susp_fen`) and calls the handler, blocking until its exit. This is the always-freeze. (The
  cluster singleton uses `resource-only`; VM disks need the stronger `-and-stonith` because they
  have no `quorum`/witness backstop, so the freeze itself must come from fencing.)
- `handlers { fence-peer bedrock-fence-peer; }` — the same self-contained handler, which now routes
  by resource class: `cluster` → octet/netd path; `vm-*` → node_name/rqlite path.
- `options on-suspended-primary-outdated force-secondary` + `ping-int 5` + create-md
  `--bitmap-block-size=1048576` (the Bedrock DRBD defaults, now on VM disks too).

### Decision (`fence_verdict.decide_vm_fence`, endpoint-side)
`level=strong` read of `vms.host` + `failover_order` for the VM. The strong read **doubles as the
"am I in the cluster majority?" gate** — it RAISES in the minority partition (no leader to confirm),
which maps to `undecided` → DRBD stays frozen (the safe default). Then:
- `host == me` → **win** (blessed home, in majority → outdate peer, resume).
- `host == lost_peer` AND I'm next in `failover_order` → **win** (the *sanctioned takeover*: the
  callout fires *during* `takeover_after_peer_down_task`'s `drbdadm primary`, **before** it writes
  `vms.host = me` at the last saga step, so we recognise the successor by the lost-host identity +
  the predetermined order — `peers_after_dead`, the same authority the takeover itself uses).
- else → **lose**. Mirrors `is_safe_to_start_vm` / `_vms_on_dead_peer` so the DRBD-level gate and the
  orchestrator can never disagree about who runs the VM.

This is the intended separation of the two read-consistency classes (`feedback_read_consistency_classes`):
the VM fence decision is **strict-leader, no local fallback** (a stale local replica could read an old
host → split-brain).

### Heal — the loser's clean recovery (`reconcile_vm_fence_heal_task`, the #34 analogue for VM disks)
A frozen minority loser cannot self-resolve: isolated, its fence-peer got `undecided`, so it never
self-outdated; on reconnect it is a frozen Primary the winner can't merge with (`after-sb-2pri
disconnect`). On quorum return the 4th vm_failover task drives it back to a clean pair, both moves
idempotent off one `drbdsetup status` parse:
1. **Reconnect any StandAlone `vm-*`** (`drbdadm connect`) — the takeover leaves the WINNER StandAlone
   (it disconnected the dead inbound peer), and a just-demoted loser can land StandAlone when its
   first handshake aborts (`-27`). Reconnecting the winner triggers DRBD's own *"remote has more
   recent data → force secondary"* on the frozen loser (clean, because no-mint = the loser's UUID is
   a strict ancestor); reconnecting the demoted Secondary completes the resync. Non-destructive.
2. **Backstop force-release** a frozen Primary the cluster no longer owns (`vms.host != me`, stable
   12 s) via `drbdsetup secondary --force=yes` — for the case nothing reconnected it so the native
   force-secondary never fired. The 12 s window keeps this off a node that is only transiently
   suspended because it is *winning* a fence (resolves in ~1-3 s).

### What this fixes (the earlier "dragons" D3/D4/D5)
- **D3 / D4** (minority/hung old Primary keeps writing while the survivor promotes → divergence): the
  `resource-and-stonith` freeze stops the minority's writes at the DRBD-detection moment (~3-6 s with
  `ping-int 5`), long before the T+35 s takeover. Empirically: **no split-brain, no-mint** across runs.
- **D5** (tautological takeover strong-read): `decide_vm_fence` adds an **independent** strong-read
  gate at the actual promote (`vms.host`/`failover_order`), not just the self-confirming UUID read.
- **D8** (denominator-shrink split-brain, `lesson_node_drain_denominator_splitbrain`): still an
  *election* bug the fence-peer can't fix — the `#7` all-applied watermark remains the fix.

### Validation (testbed, stock DRBD 9.x, 2026-06-08 — `testbed/drbd_uuid_bug/vm_fence_campaign.py`)
Realistic gate (full reconverge between scenarios), pet VM (2-way):
- **W** (isolate the replica): host **WINS**, VM never stops, single Primary throughout; the winner
  legitimately mints a new UUID (drives the replica's resync); clean reconverge. **PASS**.
- **F** (isolate the host): froze at **T+2.9 s**, successor fence-peer **WIN** at T+7.5 s during its
  `drbdadm primary`, VM running on the successor at **T+28 s**; isolated host is Primary-**but-frozen**
  (exactly one live Primary → **no split-brain**); frozen loser's UUID **never rotated** (no-mint);
  on heal the loser auto-demotes + resyncs to UpToDate/UpToDate with no manual action. **PASS**.

### Still open / deferred
- `migrate` live failed on a libvirt `qemu+ssh` **host-key verification** error (testbed migration SSH
  known_hosts not seeded) — orthogonal to P3; note for the migrate path.
- `migrate.py` post-promote UUID-record should **fail the saga** (not warn-continue) on unreadable UUID.
- vipet/3-peer even-split VM-failover stall should be made **operator-visible** (today a silent skip).
