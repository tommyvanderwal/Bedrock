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

---

## L13 — Cloud-init regenerates SSH host keys after sshd starts
**2026-04-30** · clean-run Phase 1-2

**What we thought:** Cloud images come with stable SSH host keys; once
the VM is sshable, the keys are final.

**What we found:** AlmaLinux 9 cloud-init module
`cc_ssh_genkeytypes` runs *after* sshd has already started. Sshd
loads the image's pre-baked keys initially; cloud-init then
regenerates fresh per-VM keys; sshd doesn't see them until it
reloads. If `bedrock join` runs in this window, its `ssh-keyscan`
captures pre-regen keys → later actual ssh connections see
post-regen keys → "host key changed" warnings.

**What we changed:** durable mitigation — every operator script that
ssh-keyscans testbed nodes runs `cloud-init status --wait` first,
then proceeds. Built into the new clean-run Phase 1.

**Source:**
- [`cc_ssh_genkeytypes` cloud-init module](https://cloudinit.readthedocs.io/en/latest/topics/modules.html#ssh)

---

## L14 — `ssh-keygen -R` is the right tool for cleaning hashed known_hosts entries
**2026-04-30** · clean-run Phase 2

**What we thought:** A `sed -i '/<ip>/d'` would clean stale entries
for testbed IPs from /root/.ssh/known_hosts.

**What we found:** OpenSSH writes hashed entries by default
(`HashKnownHosts yes`). Each entry looks like
`|1|<salted-hash>|<salted-hash> <key-type> <key-data>` — the IP is
not present in plain text. sed regex matching the IP literal
silently does nothing. After sed "cleanup," all stale entries are
still there.

**What we changed:** use `ssh-keygen -f /root/.ssh/known_hosts -R <ip>`
which knows how to compare against hashed entries and removes the
right lines.

**Source:**
- [`ssh-keygen(1)` `-R` flag](https://man7.org/linux/man-pages/man1/ssh-keygen.1.html#CERTIFICATE_AUTHORITY_OPTIONS)
- [`ssh_config(5)` HashKnownHosts](https://man7.org/linux/man-pages/man5/ssh_config.5.html)

---

## L15 — Local scratch data lost on N=1→N=2 promote (asymmetric with reverse)
**2026-04-30** · clean-run Phase 2

**What we thought:** Promoting from N=1 to N=2 preserves all tier
data — bulk and critical do via DRBD external metadata's zero-copy
trick. We assumed scratch was symmetrical.

**What we found:** `s3fs_mount_scratch()` unmounts the local LV
without copying data into the new Garage bucket. The tier_storage
code has a comment "skip rsync-into-S3 for now; that's a documented
operator step" — but in practice it surprises the operator
(SENTINEL.txt MD5 disappears mid-run).

**What we changed:** **TODO** — implement
`migrate_scratch_into_garage()` as the symmetric counterpart of
`migrate_scratch_out_of_garage()`. Per Tommy: "data may be lost
ONLY when losing a node, never during a default/normal migration."

**Source:**
- (the current behavior is in `tier_storage.s3fs_mount_scratch()`
  around the line `# (skip rsync-into-S3 for now; that's a documented
  operator step)`)

---

## L16 — `transfer_mgmt_role` NFS-client remount needs `umount -l`
**2026-04-30** · clean-run Phase 5

**What we thought:** Plain `umount` followed by `mount` would
re-establish NFS clients against the new master.

**What we found:** when the previous NFS server (old master) has
just been demoted, the kernel's NFS connection to it is in a stale
state. Plain `umount` returns success without actually unmounting
(the NFS client is still trying to talk to the dead server).
Subsequent `mount` is a no-op (path is "already mounted" in kernel
state, just to a dead destination). The kernel state stays connected
to the OLD server while fstab points at the NEW one. Symptom:
`md5sum /bedrock/bulk/SENTINEL.txt → Input/output error`.

**What we changed:** `transfer_mgmt_role()` now uses `umount -l`
(lazy unmount) which detaches the mount from the namespace
immediately and the next `mount` always picks up fresh config from
fstab.

**Source:**
- [`umount(8)` `-l` lazy unmount](https://man7.org/linux/man-pages/man8/umount.8.html)

---

## L17 — Mgmt-app Python deps must be installed on EVERY node, not just the initial mgmt
**2026-04-30** · clean-run Phase 5

**What we thought:** Only the mgmt node needs paramiko, fastapi,
uvicorn, websockets, pydantic, python-multipart. Peer nodes use
plain stdlib for their agent code.

**What we found:** When `transfer_mgmt_role` rsyncs `/opt/bedrock/mgmt`
to the new master and starts `bedrock-mgmt.service`, the service
fails immediately with `ModuleNotFoundError: No module named 'paramiko'`.
Agent-installed peers never had the pip deps.

Per Tommy: "any one could in principle become the master" — so the
right design is to install ALL mgmt deps on every node by default.
Each node is then ready to take over without runtime pip install.

**What we changed (interim):** `transfer_mgmt_role()` now runs
pip install on the new master before starting services. Real fix
queued: move the `pip install` from `mgmt_install.install_full()`
into `packages.install_base()` so every node gets the deps at
bootstrap time.

**Source:**
- (`mgmt_install.install_full()` line ~138 currently does
  `pip3 install -q fastapi uvicorn paramiko websockets pydantic
  python-multipart`)

---

## L18 — `garage worker list` parser must handle multi-word worker names
**2026-04-30** · clean-run Phase 5

**What we thought:** Splitting the `garage worker list` output on
whitespace and grabbing `cols[5]` would give the Queue value for a
"Block resync worker #N" row.

**What we found:** "Block resync worker #N" is FOUR
whitespace-separated tokens, so `cols[5]` is `#N` (part of the name),
not the Queue value. Real Queue is at `cols[8]`. Result: the
`garage_drain_node` polling loop saw `queue=#1` (a non-`0`,
non-`-` value) and never recognized completion → 300 s timeout.

**What we changed:** parser now uses a regex anchored on
`Block resync worker #\d+` to extract State and Queue
positionally relative to the worker-name marker, immune to
column-counting bugs.

**Source:**
- the actual `garage worker list` output format from
  v2.3.0 (Garage's `worker_list` admin command)

---

## L19 — In-flight code fixes need explicit push to running sim nodes
**2026-04-30** · clean-run Phase 6

**What we thought:** A `git commit` of a tier_storage.py fix
makes the fix active on running sim nodes. (False conflation
between dev-box source tree and sim-node `/usr/local/lib/bedrock/`.)

**What we found:** Sim nodes have their own copy of
`tier_storage.py` from when they ran `install.sh`. A commit on the
dev box's tree doesn't update the sim's copy. Helpers continued to
fail with the original bug after I committed the fix.

**What we changed:** during empirical testing, scp the new file to
each sim node and `rm -rf /usr/local/lib/bedrock/lib/__pycache__`
before re-running. Long-term: install.sh + bedrock CLI should
support a `bedrock self-update` subcommand that pulls the latest
code from the install repo (or testbed automation re-runs install.sh).

---

## L20 — `drbdadm adjust` shrinking full-mesh resources is unreliable
**2026-04-30** · clean-run Phase 6

**What we thought:** LINBIT's blessed online peer-removal flow
(edit config, `drbdadm --dry-run adjust`, `drbdadm adjust`) handles
all reductions including full-mesh shrink.

**What we found:** when shrinking a 3-way (or 4-way) resource to
N-1 way by `drbdadm adjust`, the kernel reports
`Combination of local address(port) and remote address(port) already
in use` and the adjust fails. The path between the surviving two
peers is being treated as "new" by adjust even though it already
exists. This is an adjust bug or edge case for full-mesh shrinks.

**What we changed:** `drbd_remove_peer` already uses
`drbdsetup disconnect` + `drbdsetup del-peer` as a fallback — but
this run shows the fallback should probably be the *primary* path
for tier resources. Real fix queued: change `drbd_remove_peer` to
prefer `drbdsetup disconnect/del-peer` directly, and use `adjust`
only as the post-hoc config reconciliation step.

---

## L21 — `drbdsetup down` is not a complete teardown; `drbdadm down` is
**2026-04-30** · clean-run Phase 7

**What we thought:** `drbdsetup down <res>` fully tears down a DRBD
resource — kernel state cleared, underlying LV released. `drbdadm`
is just a wrapper around `drbdsetup`.

**What we found:** `drbdsetup down` does NOT release the underlying
LV in all cases. After running it, `lsblk /dev/bedrock/tier-bulk`
still showed `bedrock-tier--bulk → drbd1100` (the device-mapper chain
was still bound). Subsequent `mount /dev/bedrock/tier-bulk` failed
with "already mounted or mount point busy."

`drbdadm down` orchestrates the FULL teardown via the .res file:
umount → secondary → detach → disconnect → del-minor → del-resource.
`drbdsetup down` only runs `del-resource`, leaving minor and disk
attached if they were previously attached.

**What we changed:** `drbd_demote_to_local` rewritten to call
`drbdadm down` (with .res still in place) BEFORE moving the .res
aside. The crash window between drbdadm-down and mv-aside is brief
and self-recoverable (drbd-utils don't auto-up an already-down
resource).

---

## L22 — rsync `-X` (xattrs) breaks s3fs → XFS migration
**2026-04-30** · clean-run Phase 7

**What we thought:** `rsync -aHX` is the right "preserve everything"
flag set for migrating data between filesystems.

**What we found:** s3fs reports SELinux/extended-attribute contexts
inconsistently with what XFS expects. Mid-copy rsync hits
`lremovexattr("dest/file","security.selinux") failed: Permission
denied` and aborts with exit code 23, files partially copied.

**What we changed:** `migrate_scratch_out_of_garage` uses `rsync
-aH --inplace` (no `-X`). Permissions, times, hardlinks are
preserved; xattrs are not — acceptable for scratch tier where the
content is regenerable anyway.

---

## L23 — DRBD .res files must be distributed to EVERY participating node
**2026-04-30** · code review (post clean-run)

**What we thought:** Each helper that adds or removes a DRBD peer
writes its own .res file locally; that's enough because peers will
write their own copies via their own helper calls.

**What we found:** `promote_critical_to_3way()` was writing the new
3-peer config and running `drbdadm adjust` on the local node ONLY.
It did NOT distribute the new .res to existing peers (sim-2). Those
peers continued to have a 2-peer config on disk (from their original
`join_drbd_peer` call) while the kernel picked up the new 3rd peer
via cluster gossip / explicit drbdadm adjust on the master. **The
on-disk config on existing peers diverged from kernel state.** This
would surface on next reboot when DRBD reads the .res to bring the
resource up: it would only know about 2 peers and the 3rd would be
"new" again on first contact.

**What we changed:** `promote_critical_to_3way()` now distributes
the new .res via SSH-fanout to all existing peers and runs `drbdadm
adjust` on each. Generalizing the rule: **every helper that mutates
DRBD topology MUST distribute the new .res to every node that
participates in the resource AND run drbdadm adjust there.**
`drbd_remove_peer()` already did this; `promote_local_to_drbd_master`
and `join_drbd_peer` work because they're paired (each side writes
its own copy with the same peer list, so they end up identical) —
but if the peer list ever differs between the two calls (operator
error), they'd diverge silently.

**Audit summary** of every place that mutates `/etc/drbd.d/*.res`:

| Function | Local write? | Distributed? | Notes |
|---|---|---|---|
| `promote_local_to_drbd_master` | yes | no | OK at N=1→N=2 (paired with join_drbd_peer); fragile if operator typoed |
| `join_drbd_peer` | yes | no | Same — relies on master having matching config |
| `promote_critical_to_3way` | yes | **NOW yes** | was the bug; now distributes via SSH |
| `drbd_remove_peer` | yes | yes | already correct |
| `drbd_demote_to_local` | local move-aside only | n/a | resource is going away; no peers to update |

**Future improvement:** add a single `_distribute_drbd_res(full_res,
hosts)` helper that every topology-mutating function calls. Today
each function reimplements the SSH-fanout + base64-encode dance
slightly differently; consolidating reduces room for drift.
