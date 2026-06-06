# DRBD generation-UUID + quorum — the Bedrock foundation

> ✅ **RCA CONFIRMED (2026-06-06, empirical, source-built 9.3.2, reproduced 3× + fix validated 3×):**
> It **is** a real DRBD bug, and the **trigger is `drbdadm resume-io`**. A frozen quorum-lost
> minority Primary does *not* mint spontaneously — but `resume-io` on it fires the armed mint
> (zero writes, node still `suspended:quorum`). `resume-io` is exactly what bedrock-d's
> `ensure_drbd_write_permission` issues → that caused the original split-brain. The §5b ARM-guard
> patch (`!PRIMARY_LOST_QUORUM` at the `lost_contact` arm) **prevents it — validated**: unpatched
> mints 3×, patched mints 0×. **Decision (Tommy): do NOT ship the kernel patch** — the root cause
> is bedrock-d issuing `resume-io` with no justification (on a quorum-lost frozen node it can't even
> un-freeze, so it does nothing useful and only triggers the mint). Fix *that*, precisely. The
> validated patch is kept as a **documented fallback only** (revisit if confidence changes). The fix:
> define exactly *when* bedrock-d may resume the arbiter DRBD — event-triggered by the freeze, a ~10s
> confirm window, resume only if this node is still the confirmed exclusive winner. Full RCA + evidence:
> `bug-reports-upstream/drbd-quorum-lost-primary-uuid-rotation/` (`research/EMPIRICAL-FINDINGS.md`,
> `evidence/PATCH-VALIDATION.md`). (An interim pass here wrongly concluded "not a bug" before the
> `resume-io` trigger was found — disregard that; this banner supersedes it.)


**Status:** foundational, critical for the whole product. The arbiter (`cluster`
resource) and **every VM disk** use the same DRBD-9 setup. This is the ground truth,
proven from the DRBD kernel source (LINBIT/drbd `9.3.2`, HEAD `a46cbd9`, 2026-06-01)
and a live kernel-log capture on the sims. **Zero assumptions — every claim cites a
source file:line or a captured log.** Full deep-study dossier: `docs/.drbd-study-raw.txt`.

---

## 0. The Bedrock design invariant (the requirement we are building to)

> **Quorum = ALL.** No write reaches disk unless **every** replica node is present.
> If **any** node goes missing, DRBD **freezes IO and waits**. DRBD must **never**
> generate a new current-UUID **by itself**. A new UUID may be minted **only** when
> **bedrock-d explicitly tells DRBD to continue writing**, on whichever side
> **bedrock-d** (not DRBD) decides. There must **never** be a new UUID anywhere
> without bedrock-d's explicit "continue" signal.
>
> This must hold on a **4-node** cluster as well (not the production target, but it
> must work correctly there — so all validation runs on 4 nodes).

Rationale: a new current-UUID is DRBD's "a generation diverged here" marker. If only
bedrock-d ever triggers it, then divergence happens **only** where bedrock-d chose a
writer — never autonomously, never on two sides at once. That is what makes
split-brain impossible *and* recovery a clean incremental resync.

The rest of this document is (1) how DRBD actually behaves vs. that invariant, fully
sourced, and (2) exactly what we must configure/change to satisfy it.

---

## 1. DRBD generation UUIDs — the minimum to know (User Guide §16.2)

```
  current-uuid        the generation this node's data IS now
  bitmap-uuid[peer]   per-peer: "from THIS generation I track the delta for <peer>
                      in the on-disk bitmap" — enables INCREMENTAL (delta) resync
  history-uuid[...]   previous currents (so a returning node is still recognised)
```

Incremental vs. split-brain on reconnect (`drbd_uuid_compare()`,
`drbd_receiver.c:4678`):

```
  GOOD (incremental):                     BAD (split-brain):
  master.current = E806                    master.current = E806  (child of C8E7)
  master.bm[old] = C8E7  ──┐               old.current    = CB50  (child of C8E7)
  old.current    = C8E7  ──┘ MATCH          neither current is in the other's
  → ship only the bitmap delta (fast)       history/bitmap → StandAlone, after-sb,
                                            FULL resync or --discard-my-data
```

The invariant in §0 is precisely "never let the frozen side become `CB50`."

---

## 2. How DRBD mints a new current-UUID — two stages (sourced)

**Stage 1 — ARM a flag (synchronous, `finish_state_change`, `drbd_state.c`).**
On a Primary losing contact with a peer's data (`lost_contact_to_peer_data()`, peer
disk → `D_UNKNOWN`), with `role==Primary` and `drbd_data_accessible(NEW)` true,
`create_new_uuid` is set — `drbd_state.c:3096-3099`. **This is gated on local disk
UpToDate, NOT on quorum.** Then `drbd_state.c:3135`:
`if (create_new_uuid && !susp_uuid[OLD]) set_bit(__NEW_CUR_UUID, &device->flags)`.
Only arms a flag; no on-disk change. `PRIMARY_LOST_QUORUM` latches in the *same*
state change (`drbd_state.c:2885-2886`); `susp_quorum` at `2473-2474`. **The flag is
armed in both the lose-all and the lose-some case.**

**Stage 2 — EXECUTE (`drbd_uuid_new_current()`, `drbd_main.c:5273`).** Mints a random
current, rotates the old current into `bitmap-uuid` for weak/absent peers,
`drbd_md_sync`. The function carries **no quorum/suspend guard of its own**. It runs
in `w_after_state_change`, **asynchronously on the `drbd_worker` thread** (queued via
`queue_after_state_change_work`) — *not* inline (a first-pass claim of "synchronous"
was REFUTED in verification). It is reached from these routes:

| Route | Source | Quorum/suspend guard? | Result |
|---|---|---|---|
| **Write** | `drbd_sender.c:3443` `if (have_quorum[NOW] && drbd_data_accessible(...))` | **YES** | SAFE — a frozen zero-write Primary never reaches it |
| **Disconnect** | `drbd_receiver.c:9865` `if (!drbd_suspended(device))` **and** `:9886` `!test_bit(PRIMARY_LOST_QUORUM, …)`, with the literal comment *"when we lost quorum … do not create the new UUID immediately!"* | **YES** (doubly) | SAFE for a frozen quorum-lost Primary |
| **Immediate, post-state-change** | `drbd_state.c:4295-4305` (peer disk → `D_FAILED`/`D_INCONSISTENT`) **and** `4466-4471` (`susp_uuid` false→true edge) | **NONE** | **THE LEAK — DRBD self-rotates with no quorum, violating §0** |

So DRBD's *own* stated invariant ("no quorum ⇒ don't bump") is enforced on the write
and disconnect routes — but the two immediate `w_after_state_change` routes **omit the
guard**. That omission is the entire problem.

---

## 3. The leak, captured live (no assumption)

Kernel log from the losing Primary (sim-1) the instant a 2v2 partition formed
(`quorum all` on the **4-way** arbiter; sim-1 kept sim-2, lost sim-3 + sim-4):

```
PingAck did not arrive in time.                      ← lose sim-4
susp-io( no -> quorum )                              ← IO frozen
quorum( yes -> no )
bedrock-9fa125: pdsk( UpToDate -> DUnknown )         ← CLEAN disconnect (NOT D_FAILED)
PingAck did not arrive in time.                      ← lose sim-3
bedrock-853bdf: pdsk( UpToDate -> DUnknown )         ← CLEAN disconnect
helper command: /sbin/drbdadm quorum-lost
Preparing remote state change 1227845330: 1->all     ← node 1 (sim-2, the KEPT peer) drives a 2PC
bedrock-56f13b: Committing remote state change (primary_nodes=1)
drbd1101: new current UUID: CB5081C7F838246D weak: FFFFFFFFFFFFFFFC   ← SELF-ROTATION, here
```

Proven, exactly:
1. The rotation is **DRBD-internal**, fired by the **two-phase-commit DRBD runs with the
   surviving peer** (sim-2) — **bedrock-d is not involved**, which is *exactly* the §0
   violation. (This also corrects an earlier wrong theory that bedrock-d's graceful
   rqlite stop / "yanking the service" caused it — it did not; a hard kill would not
   have prevented it.)
2. Disks went `UpToDate → DUnknown` **cleanly**, ruling out the `4295` path → the leak
   here is the **`susp_uuid` false→true edge, `drbd_state.c:4466`**.
3. `weak: FFFFFFFFFFFFFFFC` → bits 2,3 set ⇒ sim-3 (node 2) + sim-4 (node 3) stamped
   weak; old current `C8E7` rotated into their bitmap slots.
4. **Lose-all-at-once has no surviving 2PC partner ⇒ this path never runs ⇒ no
   rotation.** The presence of a 2PC partner on the losing side is the whole reason (B)
   rotates and (A) does not.

---

## 4. What this means for the §0 invariant

- **`quorum all; on-no-quorum suspend-io`** already gives the *write* half of §0: a
  Primary without all nodes **freezes and writes nothing** — committed data never
  diverges. The data layer is worst-case-secure **today**. (`calc_quorum_at` QOU_ALL →
  `voters`, `drbd_state.c:1370`; freeze = `susp_quorum`.)
- **The UUID half of §0 is violated today** by the `4466`/`4295` leak: DRBD self-mints
  a new current-UUID while frozen, with no bedrock-d signal. This does **not** lose
  data (the side is frozen), but it makes heal a **full resync / `--discard-my-data`**
  instead of incremental, and under `quorum all` the resulting StandAlone keeps nodes
  sub-quorum and frozen until every cross-generation link is resolved.

**Therefore, to satisfy §0 on the current DRBD, the leak must be closed so DRBD cannot
self-rotate — leaving bedrock-d as the sole trigger.**

---

## 5. The design to build (satisfies §0)

### 5a. DRBD config (the freeze half — keep)
```
options {
  quorum all;                 # QOU_ALL: writes require ALL voters present (drbd_state.c:1370)
  on-no-quorum suspend-io;    # lose any node → freeze IO, write nothing (susp_quorum)
  auto-promote no;            # only bedrock-d's drbdadm primary ever promotes
  on-suspended-primary-outdated force-secondary;  # reconnect ergonomics only — fires
                              # AFTER any rotation (needs cached_susp), verification-confirmed;
                              # does NOT prevent the rotation.
}
```

### 5b. Close the leak so DRBD never self-rotates (the UUID half — REQUIRED)
**There is no race-free `drbdsetup`/`drbd.conf` option that prevents the rotation**
(4-workflow-confirmed, 2026-06-06). The only knob that touches it, `on-no-quorum=io-error`,
errors *every* guest bio on any quorum loss (`drbd_req.c:2766`) → guest FS remount-ro / VM
crash — disqualified for a VM appliance. The native `after-sb`/`outdate` workarounds are too
fragile (`after-sb-0pri discard-zero-changes` can't even run for scenario B's two-Primary heal;
`drbdadm outdate` loses a synchronous in-lock race). So the clean fix is a **minimal kernel
guard — and it goes at the ARM, not the execute edges:**

- **Patch site: the ARM, `drbd_state.c:3097-3099`** (`finish_state_change()`), add one
  condition:
  ```diff
   	if (role[NEW] == R_PRIMARY && !test_bit(UNREGISTERED, &device->flags) &&
  -	    drbd_data_accessible(device, NEW))
  +	    drbd_data_accessible(device, NEW) &&
  +	    !test_bit(PRIMARY_LOST_QUORUM, &device->flags))
   		create_new_uuid = true;
  ```
  `PRIMARY_LOST_QUORUM` is already set earlier in the **same** `finish_state_change` pass
  (`:2885-2886`), so it is in scope and authoritative here. Guarding the ARM stops
  `__NEW_CUR_UUID` ever being set (`:3134-3136`), so **none** of the downstream execute edges
  (`:4466-4471` susp_uuid edge, `:4295-4305` peer-D_FAILED edge, the worker, the write route)
  can fire it — one choke point covers all of them.
- **Do NOT guard the execute edges.** `:4466-4471` is a *shared funnel* that also carries the
  legitimate Secondary→Primary promotion bump (armed separately at `:3131-3132`), and
  `:3804-3807` is the legitimate post-fencing `all_peer_disks_outdated` resume. Guarding there
  would break those.
- Mirrors the guard the write route (`drbd_sender.c:3443`, `have_quorum[NOW]`) and disconnect
  route (`drbd_receiver.c:9886`, `!PRIMARY_LOST_QUORUM`, *"do not create the new UUID
  immediately!"*) already carry. Preserves every legitimate bump: keep-quorum-lose-a-secondary
  (flag never set), S→P promotion (separate arm), write route (already guarded).

Bedrock builds/bundles the `drbd9x` module in its ISO, so this is a **patch-and-rebuild we ship
ourselves**, and we file it upstream (LINBIT routes to the **drbd-user mailing list**, *not*
GitHub). Full report + reproducer + the patch:
`docs/bug-reports-upstream/drbd-quorum-lost-primary-uuid-rotation/`. With the guard in place a
frozen Primary — whether it lost all peers or kept one — **never** mints a UUID; it stays at the
common ancestor `C`, so heal is a clean `RULE_BITMAP_PEER` → `SYNC_TARGET_USE_BITMAP` incremental
of only the survivor's writes-since-partition.

### 5c. bedrock-d is the sole "continue writing" trigger
The only legitimate UUID generation is on the side bedrock-d blesses, *when* it blesses
it:
- bedrock-d picks the writer side from its election (all cluster nodes + the external
  Echo witness — a layer **separate** from DRBD quorum) and runs **`drbdadm primary
  --force`** on the chosen Primary. Promotion (`role S→P`) mints that side's new current
  (`drbd_state.c:3131` create_new_uuid on S→P; observed: sim-3 → `E806` on force-promote)
  — rotating the old current into the bitmap for the absent nodes, which is exactly the
  marker that makes their later return an **incremental** resync.
- bedrock-d **`drbdadm resume-io`** on the chosen side (and its co-quorate followers, so
  protocol-C can ack). bedrock-d only ever sends *resume/continue*; it never issues
  *suspend* (DRBD does the instant freeze itself).
- The non-blessed side gets **no** signal → stays frozen at the common ancestor → with
  5b, never self-rotates → returns as a clean incremental SyncTarget.

### 5d. Validation (on 4 nodes, per §0)
Re-run the partition tests on the 4-node testbed. With 5b in place, the losing Primary
must show **no** `new current UUID` line in its kernel log during the partition, and
heal must be an incremental resync with no `--discard-my-data`. Keep `quorum all`
throughout — 4-node is the stress case we must pass.

---

## 6. Status of each piece

| Piece | State |
|---|---|
| `quorum all` + `on-no-quorum suspend-io` (freeze; data safe) | shipped + live-enforced |
| `auto-promote no` (no DRBD self-promote) | shipped + live-enforced + unit-guarded |
| bedrock-d `primary --force` + `resume-io` as the continue-signal | shipped (force-on-no-quorum + ensure_drbd_write_permission) |
| **ARM-guard patch (`drbd_state.c:3097-3099`, `!PRIMARY_LOST_QUORUM`)** | **patch written; build + 3× reproduce + 3× verify on latest-DRBD testbed IN PROGRESS** |
| No race-free config alternative (io-error disqualified; after-sb/outdate too fragile) | confirmed by 4 workflows 2026-06-06 |
| Upstream bug report to LINBIT (drbd-user list) | draft written (`docs/bug-reports-upstream/…`); submit is operator's call |

---

## 7. Sources

- DRBD kernel source (LINBIT/drbd `9.3.2`, HEAD `a46cbd9`, cloned to `/tmp/drbdsrc`):
  `drbd_state.c` — `:1370` QOU_ALL threshold, `:2473`/`:2885-2886` susp/`PRIMARY_LOST_QUORUM`
  latch, `:3096-3099`/`:3135` arm, **`:4295-4305` + `:4466-4471` the leak**;
  `drbd_main.c:5273` `drbd_uuid_new_current`; `drbd_sender.c:3443` (guarded write route);
  `drbd_receiver.c:9865`/`:9886` (guarded disconnect route, *"do not create the new UUID
  immediately!"*), `:4678` `drbd_uuid_compare`, `:8089` `maybe_force_secondary`.
- DRBD 9 User Guide §16.2 (generation identifiers), §2.23/§5.23 (quorum).
- Live kernel-log capture (§3) from the 2v2 partition on the sims.
- Deep-study dossier, 8 source-readers + adversarial verification (8 CONFIRMED /
  2 REFUTED): `docs/.drbd-study-raw.txt`.
- Companions: `docs/cluster-convergence.md`, `docs/witness-death-oracle.md`,
  `docs/05-drbd-internals.md`.
