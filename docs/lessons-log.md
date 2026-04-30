# Bedrock lessons log

A running journal of *non-obvious* findings — decisions reversed,
misdiagnoses corrected, surprises encountered. Each entry has:

- **What we thought** — the original assumption or hypothesis
- **What we found** — the corrected understanding, with evidence
- **What we changed** — the resulting code or operational pattern
- **Reference** — the scenario report or commit where it was investigated

Per-module operational specs (the *current* state, not the journey)
live next to the code as `<module>.md` files.

---

## L1 — DRBD `--max-peers=7` must be set at metadata creation time
**2026-04-30** · [scenario](scenarios/storage-tiers-1to4-2026-04-30.md)

**What we thought:** `drbdadm create-md` does the right thing by default;
adding peers later is just `drbdadm adjust`.

**What we found:** the default is `--max-peers=1`. Trying to add a 3rd
peer to an already-up resource fails with "node-id cannot be self" and
similar cryptic errors. Growing past 1 peer requires a brief
resource-down to regenerate metadata with `--max-peers=7`.

**What we changed:** every `create-md` call in `tier_storage.py` now
includes `--max-peers=7`. Per BEDROCK.md, this matches the project
convention. Existing testbed metadata had to be regenerated once.

---

## L2 — DRBD external metadata is the right call (more important than expected)
**2026-04-30** · [scenario](scenarios/storage-tiers-1to4-2026-04-30.md)

**What we thought:** internal vs external metadata is a minor
operational preference.

**What we found:** **external metadata is what makes "promote local LV
with existing data to DRBD-replicated" zero-copy.** A separate ~32 MB
meta LV gets initialized; the data LV's filesystem is preserved
byte-for-byte. Cattle → pet conversion exercised this and verified MD5
hashes survive. Internal metadata would overwrite the last ~32 MB of the
running filesystem.

**What we changed:** all tier resources use external meta LVs
(`bedrock/tier-<tier>-meta`, 32 MB thick, outside the thin pool so a
thin-pool fill doesn't ENOSPC the meta writes).

---

## L3 — DRBD node-ids are PERMANENT for a resource
**2026-04-30** · [scenario](scenarios/storage-tiers-deep-dive-2026-04-30.md)

**What we thought:** node-id was assignable on each config write.

**What we found:** kernel state remembers the node-id assignments from
when each peer first joined. If the on-disk config renumbers them (as
my `render_drbd_res()` did via `enumerate(peers)`), `drbdadm adjust`
fails because it tries to delete and re-create connections that should
be left alone. Symptoms: `Failure: (162) peer node id cannot be my own
node id`.

**What we changed:** queued (not yet implemented) — persist
`tiers.<tier>.drbd_node_ids = {peer_name: id}` in `cluster.json`.
`render_drbd_res()` consumes this map. New peers get the next free
integer; existing peers keep their assigned id forever.

---

## L4 — DRBD live peer removal: the LINBIT-blessed path is `drbdadm adjust`, not `drbdsetup` direct
**2026-04-30** · [scenario](scenarios/storage-tiers-deep-dive-2026-04-30.md)

**What we thought (round 1):** to remove a peer live, run
`drbdadm forget-peer <res> <peer-name>` while the resource is up.

**What that gave us:** "Device is configured!" — drbdadm fell through to
the offline `drbdmeta` path because the on-disk config no longer
contained the peer (we'd already removed it).

**What we thought (round 2):** the right commands are
`drbdsetup disconnect <res> <id>` then `drbdsetup del-peer <res> <id>`.
This worked empirically — sim-1 ghost cleared, live migration succeeded.

**What we found (correction from research):** those commands work but
they're the **fallback**. LINBIT's recommended live procedure is:

1. Edit `/etc/drbd.d/<res>.res` to remove the peer
2. Distribute to every surviving node
3. `drbdadm --dry-run adjust <res>` (preview)
4. `drbdadm adjust <res>` (apply; issues `del-peer` internally)
5. Optional: `drbdadm forget-peer <res>:<peer>` to free the bitmap slot

Source: `LINBIT/drbd-utils` `user/v9/drbdadm_adjust.c` lines 858–868
(adjust automatically schedules `del_peer_cmd` for kernel connections
without a config match) and line 806 (`/* disconnect implicit by
del-peer */`).

**The deeper reason — config-first is crash-safe:** if power is lost
mid-procedure, the on-disk config files are the source of truth on the
next boot. With config-first ordering, the persistent state already
reflects the desired end state; the kernel will reconcile to it.
Reverse ordering (kernel mutate first, config later) opens a window
where a crash leaves persistent state behind kernel state, which
matters for systems that reload config on boot.

**What we changed:** `drbd_remove_peer()` (queued) will use the LINBIT
path: edit config, distribute, dry-run, apply. `drbdsetup`
direct as fallback for cases where the config has already diverged.

---

## L5 — Garage RF=1 supports graceful node drain (originally got this wrong)
**2026-04-30** · [scenario](scenarios/storage-tiers-deep-dive-2026-04-30.md)

**What we thought:** Garage at RF=1 can't safely drain a node — there's
no replica to copy from when the layout removes a node, so any
partition that was on that node is lost. Therefore the operational play
must be "bump RF cluster-wide to 2 first, wait for re-replication, then
remove, then drop back to RF=1." This is impractical at scale (8 nodes
× 1 TB → would need 16 TB to fit on 7 × 1 TB).

**What we found:** Garage's `block_resync` worker on the *departing*
node copies blocks to their new owners (per the new layout) **before**
deleting them locally — offload-then-delete. While the resync is in
progress, reads fall back to the departing node via the multi-version
layout history (Garage source `rpc_helper.rs:570`,
`layout/history.rs`). The original "lost data" we observed was
procedural error: I declared drain "done" without waiting, then
stopped Garage on the departing node before the resync had any chance
to run.

**Bonus finding:** the data was actually preserved the whole time.
The "empty bytes" we saw was the *s3fs client* hung against the dead
sim-1 endpoint (next entry).

**What we changed:** `garage_drain_node()` (queued) waits for
`garage worker list` on the departing node to show all `Block resync`
workers Idle with Queue=0 and `garage block list-errors` empty. Runs
`garage repair --all-nodes --yes tables` then `... blocks` before
declaring success. *Then* (and only then) it's safe to stop Garage on
the departing node. Total wall-clock: roughly minutes per TB at
gigabit, much faster on 10 GbE.

---

## L6 — s3fs hard-pinning to one Garage endpoint is a single point of failure
**2026-04-30** · [scenario](scenarios/storage-tiers-deep-dive-2026-04-30.md)

**What we thought:** point s3fs at any Garage cluster member; the
cluster handles cross-node lookup internally.

**What we found:** that's *true* only if the specific endpoint host
stays alive. When sim-1 went down, every other node's s3fs hung trying
to reach `url=http://10.99.0.10:3900`, returning empty bytes for
unreachable blocks — masquerading as Garage data loss when actually it
was client-side. Garage cluster (sim-2/3/4) had the data the whole
time.

**What we changed:** queued — `s3fs_mount_scratch()` will use
`url=http://127.0.0.1:3900` (each node's own local Garage daemon). The
local daemon participates in the cluster's RPC routing; if the local
Garage is down, that node is presumably also unhealthy. No
cross-node-failure cascade through the FUSE client.

---

## L7 — vipet (3-way DRBD) with a permanently-dead 3rd peer blocks live migration
**2026-04-30** · [scenario](scenarios/storage-tiers-1to4-2026-04-30.md)

**What we thought:** quorum (2/3) is enough for promote-to-Primary, so
sim-2 ↔ sim-3 migration should work even with sim-1 dead.

**What we found:** the dual-primary handshake during live migration
won't succeed while any peer is in `Connecting` state. Symptom:
`Could not open '/dev/drbd1000': Read-only file system` on the
migration target. Once we cleared the sim-1 ghost via
`drbdsetup del-peer`, migration succeeded in 1.51 s.

**What we changed:** documented as a precondition for live migration:
*all configured peers must be either Connected or fully removed before
attempting a migration*. The `drbd_remove_peer()` flow (queued)
handles this; the `drbdsetup` direct path is the fallback when
`drbdadm adjust` cannot complete (e.g. config has already been edited).

---

## L8 — Cloud images use plain XFS, not LVM (testbed-specific)
**2026-04-30** · [scenario](scenarios/storage-tiers-1to4-2026-04-30.md)

**What we thought:** AlmaLinux cloud images use the same LVM thin layout
as the physical lab (VG `almalinux`, thin pool `thinpool`).

**What we found:** the `AlmaLinux-9-GenericCloud` qcow2 has no LVM at
all — root is on plain XFS over a partition (`vda4`).

**What we changed:** `testbed/spawn.py` attaches a second 100 GB qcow2
disk per sim node; `tier_storage.find_data_disk()` picks the first
unused candidate (`/dev/vdb`, `/dev/sdb`, or `/dev/nvme1n1`) and
`pvcreate`+`vgcreate`s VG `bedrock` on it. This keeps the testbed
isolated from the OS root and avoids fighting cloud-init growpart.

---

## L9 — XFS labels are limited to 12 characters
**2026-04-30** · [scenario](scenarios/storage-tiers-1to4-2026-04-30.md)

**What we thought:** descriptive labels like `bedrock-scratch` are fine.

**What we found:** `mkfs.xfs -L bedrock-scratch` fails with the help
text printed; the limit is 12 chars (mentioned only in the help output,
not the man page summary).

**What we changed:** use the bare tier name as the label (`scratch`,
`bulk`, `critical` — all under 12 chars).

---

## L10 — config-first is crash-safe (the meta-pattern)
**2026-04-30** · current discussion

**What we thought:** "kernel state changes first, on-disk config last"
keeps the running system in the desired state for as long as possible.

**What we found:** for systems whose on-disk config is the source of
truth at boot (DRBD via `/etc/drbd.d/*.res`, NFS via `/etc/exports.d`,
mounts via `/etc/fstab`, Garage via layout-versions persisted to its
LMDB), the *opposite* ordering is crash-safe:

- **Config-first:** persistent state already encodes the *desired
  end state*. A crash mid-operation, on next boot, brings the system
  to the desired state via normal startup. The kernel reconciles.
- **Kernel-first:** persistent state still reflects the *previous*
  state. A crash leaves a window where on next boot the system
  reverts kernel state to the old config — losing the operator's
  intended change.

**What we changed:** the operational pattern for *every* state-changing
operation in tier_storage:

1. Compute the new config (target end state)
2. Write it to disk on every relevant node
3. Apply it to the kernel via the system's reconciliation tool
   (`drbdadm adjust`, `exportfs -ra`, `mount -a`,
   `garage layout apply`)
4. Verify

This is also the LINBIT-recommended pattern for DRBD (#L4) and matches
how `garage layout apply` works (the layout itself is persisted before
the worker starts moving blocks). Generalizing it as a Bedrock
invariant makes power-loss-mid-operation a recoverable scenario for
every state transition we manage.

---

## L11 — `drbdsetup show` reveals kernel reality; `drbdadm dump` shows config
**2026-04-30** · empirical session

**What we thought:** `drbdadm status` is the place to look when debugging.

**What we found:** `drbdadm status` is the human-friendly view — but to
diagnose config-vs-kernel divergence (the core L3/L4 issue) you need
`drbdsetup show <res>` (kernel reality) compared against
`drbdadm dump <res>` (parsed config view) or just the raw `.res` file.
The mismatch in node-ids was invisible until I ran `drbdsetup show`.

**What we changed:** added a "kernel state debug" recipe to
`tier_storage.md`'s troubleshooting section. Operators chasing weird
DRBD adjust errors should `drbdsetup show <res>` first to ground the
investigation.

---

## How to add new entries

When you find something non-obvious, append a new `## L<N> — short
title` section at the bottom with the date and the four headings. Don't
edit historical entries — they're a record of what we knew when. If a
later finding supersedes an earlier one, write a new entry that
references it.

Per-module specs (`tier_storage.md`, etc.) should be revised in place
to reflect *current* implementation; this log is the journey.
