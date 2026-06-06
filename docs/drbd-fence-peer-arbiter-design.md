# DRBD fence-peer as the bedrock-d arbiter interface — design + code-path reasoning

**Status:** design, code-verified against DRBD 9.3.2 (`/tmp/drbdsrc`, HEAD `a46cbd9`). To be
validated on the 4-node testbed before implementing in bedrock-d. Supersedes the
`ensure_drbd_write_permission` / `resume-io` approach (which was the spurious-UUID root cause —
see `bug-reports-upstream/drbd-quorum-lost-primary-uuid-rotation/`).

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
5. `bedrock-unfence-peer` clears any per-peer state on heal.

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
