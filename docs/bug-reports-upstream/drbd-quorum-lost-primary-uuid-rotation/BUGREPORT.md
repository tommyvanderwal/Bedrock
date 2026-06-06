# [DRBD 9.x] `drbdadm resume-io` on a quorum-lost, frozen Primary mints a new current-UUID (zero writes) → false split-brain / full resync on heal

**Status:** ready to send. Reproduced 3× and fixed-3× on a source build of the `drbd-9.3.2`
release tarball. Channel below.

**Channel (per upstream `README.md`):** LINBIT coordinates via the mailing lists, not GitHub
issues/PRs. Send this prose to **`drbd-user@lists.linbit.com`**; follow with a checkpatch-clean
`[PATCH]` (Signed-off-by) to **`drbd-dev@lists.linbit.com`**, CC `drbd-user`. (A GitHub issue
mirror is optional, for a searchable reference number.)

---

## Environment

- DRBD kernel module: **9.3.2** (built from the `drbd-9.3.2` release tarball,
  `https://pkg.linbit.com/downloads/drbd/9/drbd-9.3.2.tar.gz`; the bug is also present in the
  drbd-9.3 branch tip `a46cbd9` and the elrepo `kmod-drbd9x-9.3.2` binary)
- drbd-utils: **9.34.0**
- Kernel: **6.12.0-124.8.1.el10_1.x86_64**, AlmaLinux 10.1
- Resource options: `quorum all; on-no-quorum suspend-io; auto-promote no;` (4 diskful nodes)
- `protocol C`, internal metadata

## Summary

On a diskful **Primary** that has lost quorum and frozen (`suspended:quorum`, zero application
writes admitted), calling **`drbdadm resume-io <res>`** causes DRBD to **generate a new
current-UUID** — stamping the absent peers weak (`weak: FFFFFFFFFFFFFFFC`) — **while the node is
still `suspended:quorum` and has written nothing.** A node that committed zero bytes thus mints a
new data generation. When the partition heals against a peer that legitimately advanced its own
generation, the two are sibling children of the common ancestor → DRBD declares **split brain**
(StandAlone, requiring manual `--discard-my-data` in the two-primary / default-`after-sb` heal
sequences a real failover hits). Data is not lost (the node never wrote); the harm is the spurious
generation fork and the split-brain it risks. (If the diverged node is demoted to Secondary before
heal, the fork still reconciles to an *incremental* resync via the surviving common ancestor — so
this is a correctness/split-brain bug, not necessarily a full-resync one.)

The new-current-UUID generation is **armed** on peer-data loss without a quorum check, and the
**execute** that `resume-io` triggers is **not** gated on quorum — unlike the two sibling routes
into `drbd_uuid_new_current()`, which *are* quorum-gated (one with an explicit comment saying not
to mint on quorum loss). This is an **incomplete-guard / asymmetry** bug.

## Minimal reproducer (4 nodes, deterministic)

Resource `r0`, `quorum all; on-no-quorum suspend-io; auto-promote no`, all `UpToDate`.

```
# node A = Primary, B/C/D = Secondary, all UpToDate
A# drbdadm primary r0
A# drbdadm get-gi r0          # record current-UUID C0

# isolate A from C and D, but KEEP B (A is now a 2-of-4 minority with a surviving peer)
A# iptables -I INPUT  -s <C_ip> -j DROP; iptables -I OUTPUT -d <C_ip> -j DROP
A# iptables -I INPUT  -s <D_ip> -j DROP; iptables -I OUTPUT -d <D_ip> -j DROP
#  (and symmetrically block A on C and D)

A# drbdadm status r0          # -> role:Primary suspended:quorum, quorum:no, blocked:upper
A# drbdadm get-gi r0          # current-UUID == C0 (UNCHANGED — frozen, correct so far)

#  >>> THE TRIGGER: resume IO on the still-Primary, still-quorum-lost node <<<
A# drbdadm resume-io r0

A# drbdadm status r0          # STILL role:Primary suspended:quorum  (it did NOT regain quorum)
A# drbdadm get-gi r0          # current-UUID has CHANGED to a new value  <-- BUG
A# dmesg | tail
#   drbd r0/0 drbdX: new current UUID: <NEW> weak: FFFFFFFFFFFFFFFC
```

Observed across 3 independent runs (current-UUIDs `F7AE071392E4BC4B`, `6E00B2F12C149731`,
`F58DFD7F63BA24D1`, all `weak: FFFFFFFFFFFFFFFC`); the node stayed `suspended:quorum` and wrote
zero bytes each time. **Control:** without the `resume-io`, the frozen Primary never mints (held
for minutes, 4× — `get-gi` unchanged). So `resume-io` is necessary and sufficient for the mint.

## Expected vs. actual

- **Expected:** a quorum-lost Primary that cannot write (still `suspended:quorum`) must not create
  a new data generation. `resume-io` should not mint on a node without quorum — consistent with
  the write and disconnect routes, which refuse to mint while `PRIMARY_LOST_QUORUM` is set.
- **Actual:** `resume-io` fires the armed generation bump; a new current-UUID is written and the
  absent peers are stamped weak, despite no quorum and no writes. Heal → split brain / full resync.

## Root cause (source-cited, DRBD 9.3.2, HEAD `a46cbd9`)

**ARM (no quorum gate)** — `drbd_state.c` (`finish_state_change`), the
`lost_contact_to_peer_data` branch:
```c
if (lost_contact_to_peer_data(peer_disk_state)) {
    if (role[NEW] == R_PRIMARY && !test_bit(UNREGISTERED, &device->flags) &&
        drbd_data_accessible(device, NEW))
        create_new_uuid = true;          /* sets __NEW_CUR_UUID; NOT gated on have_quorum */
```
`drbd_data_accessible()` is true on a local `D_UP_TO_DATE` disk, which a frozen quorum-lost
Primary still has. `PRIMARY_LOST_QUORUM` is set earlier in the *same* pass
(`if (role[NEW]==R_PRIMARY && !have_quorum[NEW]) set_bit(PRIMARY_LOST_QUORUM,...)`) but is **not**
consulted here. The flag persists while the node is frozen.

**EXECUTE triggered by `resume-io` (no quorum gate)** — clearing the IO suspend drives the
`susp_uuid` path (`drbd_state.c:4466`, `w_after_state_change`) →
`drbd_check_peers_new_current_uuid()` / `drbd_uuid_new_current()`, which mints. There is no
`have_quorum` / `!PRIMARY_LOST_QUORUM` check on this route.

**The asymmetry — the two sibling routes ARE guarded:**
- Write route, `drbd_sender.c:3443`: `if (device->have_quorum[NOW] && drbd_data_accessible(device, NOW)) drbd_uuid_new_current(...)`.
- Disconnect route, `drbd_receiver.c:9884-9888`: gated `!test_bit(PRIMARY_LOST_QUORUM, &device->flags)`, with the verbatim comment *"… therefore do not create the new UUID immediately!"*.

So DRBD already encodes the intended invariant "don't mint a new generation on a quorum-lost
Primary" on two routes; the `resume-io`-triggered route omits it.

## Suggested fix (one line, at the ARM) — validated

Gate the ARM so the flag is never set for a quorum-lost Primary; then no downstream execute
(including the `resume-io`/`susp_uuid` route) can mint. `PRIMARY_LOST_QUORUM` is already set in the
same pass.

```diff
--- a/drbd/drbd_state.c
+++ b/drbd/drbd_state.c
 	if (lost_contact_to_peer_data(peer_disk_state)) {
 		if (role[NEW] == R_PRIMARY && !test_bit(UNREGISTERED, &device->flags) &&
-		    drbd_data_accessible(device, NEW))
+		    drbd_data_accessible(device, NEW) &&
+		    !test_bit(PRIMARY_LOST_QUORUM, &device->flags))
 			create_new_uuid = true;
```

This mirrors the `drbd_receiver.c:9886` idiom and preserves every legitimate bump:
keep-quorum-lose-a-secondary (`PRIMARY_LOST_QUORUM` never set), Secondary→Primary promotion
(armed by a separate statement, untouched), and the write route (already guarded). It is applied
at the ARM, **not** an EXECUTE edge, because the execute sites are shared funnels that also carry
the legitimate promotion bump and the post-fencing `all_peer_disks_outdated` resume.

**Validation (same testbed, same trigger, only the patch differs):**

| `resume-io` on frozen minority | stock 9.3.2 | + patch |
|---|---|---|
| run 1 | mint `F7AE…` | **no mint** |
| run 2 | mint `6E00…` | **no mint** |
| run 3 | mint `F58D…` | **no mint** |

Patched: the frozen Primary stays at the common generation across all 3 runs, so heal is a clean
incremental resync.

## Impact

- False **split brain** / StandAlone (manual recovery) on heal of any quorum-lost Primary that an
  orchestration layer calls `resume-io` on (e.g. an HA manager resuming the side it elected), in
  the two-primary / default-`after-sb` heal sequences. The node need not have written anything.
- The generation fork is metadata-only; **no data loss** (the node was frozen). When the diverged
  node is demoted before heal the resync stays incremental (bounded by the writer's delta), so the
  cost is the split-brain handling / operator churn rather than a full-device resync.

## Related precedent

The 9.3.2 ChangeLog ("All fixes from 9.2.18") includes *"Fix when a diskless primary creates a new
current UUID, fixing possible silent data divergence later"* — i.e. LINBIT already treats an
uncontrolled new-current-UUID bump as a correctness concern. That fix was in the diskless arm; the
diskful frozen-Primary `resume-io` path reported here is unguarded by the same class of check.
