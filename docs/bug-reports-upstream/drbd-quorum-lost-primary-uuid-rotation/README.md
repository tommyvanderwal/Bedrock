# DRBD: quorum-lost frozen diskful Primary self-mints a new current-UUID

**This folder is the stable home for the upstream bug report on this defect and all its
supporting evidence.** Other upstream bug reports get their own sibling subfolders under
`docs/bug-reports-upstream/`.

- **`BUGREPORT.md`** — the corrected, validated long-form report to send to LINBIT (drbd-user).
- **`research/EMPIRICAL-FINDINGS.md`** — the RCA: the `resume-io` trigger, the test matrix, the
  bedrock-d code path, the residual exposures.
- **`evidence/PATCH-VALIDATION.md`** + `evidence/resumeio-{,PATCHED-}round-{1,2,3}/` — the
  before/after proof (stock mints 3×, patched mints 0×) and the `correct-failover-round-*` controls.
- `research/wf*.json` + `wf*-script.mjs` — raw outputs + scripts of the multi-agent investigations.
- Reproducer harness: `testbed/drbd_uuid_bug/repro.sh`.

---

## The defect in one paragraph

On a diskful **Primary** that loses quorum but **keeps at least one peer** (e.g. a 2-of-4
minority), DRBD mints a **new current-UUID** the moment **`drbdadm resume-io`** is called on it
(which an HA orchestrator like bedrock-d does via `ensure_drbd_write_permission`) — while still
`suspended:quorum`, with **zero** writes (`new current UUID: <hex> weak: <mask>`). Left alone it
never mints; the `resume-io` is the trigger (reproduced 3×; the one-line ARM-guard patch stops it,
validated 3×). The
two sibling routes into `drbd_uuid_new_current()` — the **write** route (`drbd_sender.c:3443`,
gated `have_quorum[NOW]`) and the **disconnect** route (`drbd_receiver.c:9886`, gated
`!PRIMARY_LOST_QUORUM`, with the verbatim comment *"…therefore do not create the new UUID
immediately!"*) — are quorum-guarded. The **state-change route** (`finish_state_change()` arm at
`drbd_state.c:3096-3099` → `w_after_state_change()` execute at `:4466-4471`, plus a second edge at
`:4295-4305`) is **not**. The result on heal is a spurious **full resync** / false split-brain.
**Data is never lost** (the data path is genuinely frozen); the cost is operational (needless
resync) and a false divergence signal.

## Verdict

| Question | Answer |
|---|---|
| Does the data path stay safe? | **Yes.** `quorum all` + `suspend-io` admit **zero** user bytes on the minority (two independent gates: entry `inc_ap_bio`/`may_inc_ap_bio`, submit `drbd_req.c:2057-2066`). The leak is **metadata only** (the UUID superblock on the separate `md_bdev`). |
| Is there a **race-free config** that prevents the rotation? | **No.** `on-no-quorum=io-error` is the only knob that touches it, but it errors *every* guest bio on any quorum loss (`drbd_req.c:2766`) → guest FS remount-ro / VM crash. Disqualified. Nothing else (`quorum`, `quorum-minimum-redundancy`, `fencing`, `after-sb-*`, `on-suspended-primary-outdated`, module params, sysfs) is consulted at the arm or execute. `drbdadm outdate` loses a synchronous in-lock race. |
| Is the native after-sb / outdate workaround viable? | **No — too fragile.** `after-sb-0pri discard-zero-changes` can't even run for scenario B (it's a **two-Primary heal**, `pcount=2` → `drbd_asb_recover_2p`, where `discard-zero-changes` is a config error); and it's unsafe the moment a real dirty bit exists. `outdate` is a race the daemon loses. |
| Is it a bug? | **Yes (high-confidence on the mechanism).** The guarded siblings + the explicit *"do not create the new UUID immediately!"* comment document the intended invariant; the state-change route violates it. We file it as a question to LINBIT to confirm no intended use. |
| The fix | **A 1-condition kernel guard at the ARM:** add `&& !test_bit(PRIMARY_LOST_QUORUM, &device->flags)` at `drbd_state.c:3097-3099`. The ARM is the single choke point upstream of every execute edge; `PRIMARY_LOST_QUORUM` is already set in the same `finish_state_change` pass (`:2885-2886`). Preserves all legitimate bumps. We bundle `drbd9x`, so we ship it ourselves and drop it when upstream lands a fix. |

## Why the ARM, not the execute edges

Guarding the ARM stops `__NEW_CUR_UUID` ever being set (`:3134-3136`), so none of the downstream
execute edges can fire. Guarding an execute edge would be **wrong**: `:4466-4471` is a shared
funnel that also carries the legitimate Secondary→Primary promotion bump (armed separately at
`:3131-3132`), and `:3804-3807` is the legitimate post-fencing `all_peer_disks_outdated` resume.

## Heal mechanics (why it's a full resync, and what an efficient heal costs)

- The resync **strategy** is decided 100% from generation metadata in `drbd_uuid_compare()`
  (current-UUID, per-peer **bitmap-UUID** tags, history, flags). The **dirty-block bitmap is
  never read** for the strategy — only to *size* the transfer afterward. (Two different
  "bitmaps": the bitmap-UUID *generation tag* vs. the dirty-block *changed-block map*.)
- Both sides keep the common ancestor `C` in their bitmap-UUID slot → `RULE_BITMAP_BOTH` →
  `SPLIT_BRAIN_AUTO_RECOVER` (the *recoverable* kind, not the history-match `DISCONNECT`). But
  `AUTO_RECOVER` has `.disconnect=true` and, with default `after-sb=disconnect` + a two-Primary
  heal, drops to StandAlone.
- If the rotation is **prevented** (the fix), the loser's current stays `= C =` survivor's
  bitmap slot → clean `RULE_BITMAP_PEER` → `SYNC_TARGET_USE_BITMAP`, an **incremental** resync of
  only **W = the survivor's writes-since-partition** (loser wrote 0). Bounded, never forced full.

## Open item to settle empirically (step 2)

A source-only reading argued the rotation is *armed* during the freeze but only *realized at
un-freeze/quorum-regain*; our live kernel log shows it *during* the partition (2PC with the kept
peer). The live capture is ground truth — step 2 re-captures `show-gi` + dmesg timestamps to
nail the exact trigger beyond doubt.

## Validation plan (steps 2–3)

1. Rebuild the 4-node testbed on the **latest released DRBD** (confirm the bug is present in the
   newest LINBIT code, not just our pinned 9.3.2). Reproduce scenario B **3×**, full heal
   between each, capturing **resync volume** + `show-gi` + dmesg.
2. Apply the ARM-guard patch, recompile, rebuild the testbed cleanly with the patched module.
   Re-run **3×**, confirming **no** `new current UUID` during the partition and an **incremental**
   heal. Capture resync volume for a direct before/after comparison.

## Source provenance

LINBIT/drbd `9.3.2`, HEAD `a46cbd9` (2026-06-01), cloned at `/tmp/drbdsrc` and
`/home/tommy/projects/drbdsrc_clone`. Bedrock bundles `kmod-drbd9x-9.3.2` via
`installer/iso-build/build-iso.sh`. Companion analysis: `docs/drbd-uuid-quorum-foundations.md`.
