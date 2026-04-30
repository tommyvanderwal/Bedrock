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

---

## L24 — Every Garage interaction goes through the admin API, not the CLI
**2026-04-30** · post-clean-run audit

**What we thought:** The `garage` CLI is fine for most calls — only
`worker list` parsing was fragile (L18). Bucket create / key info /
layout assign / etc. all "just work" via the CLI.

**What we found:** Every CLI call we make has the same class of
problem L18 surfaced: stdout is *human-readable* output the docs
explicitly say not to parse, and CLI label changes between Garage
releases would silently break us. Three concrete cases were already
load-bearing:

1. `garage layout show` parsed for "Current cluster layout version: N"
   — used to compute the next ApplyClusterLayout version. A label
   change silently sets next_version=1, which the API rejects but
   under a confusing error.
2. `garage key info scratch-key --show-secret` parsed for "Key ID:" /
   "Secret key:" — a label change leaves us with `ak=None, sk=None`
   and a non-functional s3fs mount on first boot.
3. `garage block list-errors` parsed by line-counting (skip the
   "Hash" header) — a header rename miscounts and could let a drain
   complete with errored blocks still on the departing node.

The Garage v2 admin API exposes structured JSON for every operation
we use. There is no CLI-only operation we depend on.

**What we changed:** Added `_garage_api()` + `_garage_admin_token()`
helpers in `tier_storage.py` and migrated all 13 CLI calls to v2
admin API endpoints (`GetClusterStatus`, `GetClusterLayout`,
`UpdateClusterLayout`, `ApplyClusterLayout`, `ConnectClusterNodes`,
`CreateBucket`, `CreateKey`, `AllowBucketKey`, `GetBucketInfo`,
`GetKeyInfo`, `SetWorkerVariable`, `ListBlockErrors`, `LaunchRepair-
Operation`, plus the existing `ListWorkers`). Helper handles both
local (urllib) and remote (curl-over-ssh) calls. Token is shared
cluster-wide and read from `/etc/garage.toml` — no separate plumbing.

**General rule:** If a Garage operation has an admin API endpoint,
use it. The CLI is for interactive operator use, not for orchestration.

**Source:**
- OpenAPI v2.1.0:
  https://garagehq.deuxfleurs.fr/api/garage-admin-v2.json
- Reference manual:
  https://garagehq.deuxfleurs.fr/documentation/reference-manual/admin-api/
- Pre-migration research: `/tmp/garage-api-migration-research.md`
  (per-command classification table)

---

## L25 — Testbed SSH key lives in `/root/.ssh`, not `~tommy/.ssh`
**2026-04-30** · clean-rerun setup

**What we thought:** `spawn.py` reads `~/.ssh/id_ed25519` (the user
running the script) and bakes the matching pubkey into cloud-init.
So the dev user can `ssh root@<sim-ip>` directly.

**What we found:** `spawn.py` is invoked under `sudo` (it needs root
for `virsh --connect qemu:///system` and `cloud-localds`). Inside
sudo, `Path.home()` resolves to `/root`, not `/home/tommy`. So the
key baked into every sim's cloud-init `ssh_authorized_keys` is
`/root/.ssh/id_ed25519.pub` (label `root@HP-G1a`). Plain `ssh root@`
from the `tommy` shell finds no matching identity in
`/home/tommy/.ssh/`, falls through to password auth, fails.

This wasn't surfaced by `spawn.py ssh <i>` and `spawn.py exec <i>`
because those subcommands were also typically invoked under sudo (or
via the e2e script that already used sudo). The breakage shows up
when a person/agent uses raw `ssh root@<ip>` after spawning.

**What we changed:** Documented as a project rule. From now on, all
testbed SSH from the dev box uses `sudo ssh root@<sim-ip>` (or
`spawn.py ssh <i>` / `spawn.py exec <i>`, themselves run with sudo).
No code change — the `Path.home()` behavior under sudo is *correct*
for what spawn.py is doing (it owns root's libvirt resources, so
using root's key is consistent).

Future improvement: have `cmd_prereqs` symlink `/root/.ssh/id_ed25519`
into `~tommy/.ssh/id_ed25519_testbed` and prepend it via SSH config
so the dev user can SSH without sudo. Out of scope for v1.0.

**Source:**
- spawn.py:43-44 — `SSH_KEY = Path.home() / ".ssh" / "id_ed25519"`
- spawn.py:131 — `pubkey = SSH_PUBKEY.read_text().strip()` baked
  into cloud-init user-data.

---

## L26 — `rsync` into s3fs needs `--omit-dir-times`
**2026-04-30** · clean-rerun Phase 2 (N=1 → N=2 promote)

**What we thought:** `migrate_scratch_into_garage()` could mirror the
`migrate_scratch_out_of_garage()` rsync flags exactly: `-aH --inplace`.
The two were designed as symmetric counterparts, just direction-
reversed; if the OUT direction works, the IN direction should too.

**What we found:** First fresh-testbed run of N=1→N=2 fails with:

```
rsync: [generator] failed to set times on
  "/var/lib/bedrock/mounts/scratch-s3fs/.": Input/output error (5)
rsync error: some files/attrs were not transferred (code 23)
```

`-a` implies `-t` which makes rsync set mtimes on the destination
*root directory* at the very end of the transfer. s3fs is a FUSE
bridge to S3, and S3 has no native concept of directory mtime — so
the FUSE op returns EIO. All file data is already copied successfully
by the time this fires; only the cosmetic post-transfer dir-mtime
step fails, but rsync exits non-zero anyway.

The OUT direction (s3fs → local) didn't surface this because the
*destination* root is a local XFS mount that supports setmtime fine.
Asymmetric s3fs limitations means the two directions need different
flag sets even though the data flow is symmetric. (Same pattern as
L22's `-X` drop.)

**Why it didn't surface earlier:** L15 added `migrate_scratch_into_
garage` after the prior clean-run; the pre-L15 testbed never ran
this code path. The 2026-04-30 clean-rerun is the first time it's
exercised on a fresh testbed. Listed as a backlog item ("re-run
validation pass on fresh testbed to confirm L15...") — this is what
re-running surfaces.

**What we changed:** `migrate_scratch_into_garage()` rsync command
now passes `--omit-dir-times`. File mtimes still preserved (so
re-run idempotency on size+mtime check is intact); only directory
mtimes are skipped. `migrate_scratch_out_of_garage()` left alone
since the local destination supports dir mtimes fine.

**Source:**
- `rsync(1)` — [`--omit-dir-times`](https://manpages.debian.org/testing/rsync/rsync.1.en.html#opt--omit-dir-times)
- s3fs-fuse — [POSIX limitations](https://github.com/s3fs-fuse/s3fs-fuse/wiki/Limitations)
  (S3 doesn't model directory metadata)
- `tier_storage.py` migrate_scratch_into_garage step 1.

---

## L27 — Adding a peer to a DRBD tier requires umount + .res + create-md, not just `drbdadm up`
**2026-04-30** · clean-rerun Phase 3 (N=2 → N=3 critical promote)

**What we thought:** When the CLI verb `bedrock storage promote-critical-3way <peer>`
calls `promote_critical_to_3way()` on the master, the master writes the
new .res with the third peer included, distributes it to the existing
peers, and updates kernel state. For the new peer side, just running
`drbdadm create-md ...; drbdadm up` over SSH should be enough.

**What we found:** The new peer ends up in `connection:Connecting` on
the master and `no resources defined!` locally — because:

1. The new peer's `/etc/drbd.d/tier-critical.res` doesn't exist. The
   master never sent the config to the new peer (the master-side
   `promote_critical_to_3way()` distributes only to *existing* peers,
   per its own comment "the new third peer's join_drbd_peer will write
   its own"). So `drbdadm` on the new peer has no resource to manage.
2. Even if the .res were copied verbatim, the new peer's local LV
   `tier-critical` is still mounted at `/var/lib/bedrock/local/critical`
   (set up by `setup_n1` during `bedrock join`). DRBD `attach` fails
   with "Can not open backing device (104)" because the kernel won't
   let DRBD claim a device that's already mounted.
3. `bedrock storage init` (which sets up local-LV tiers) and `bedrock
   storage promote-critical-3way` are sequential operations the
   operator runs, but the latter must do the *unmount* the former
   left in place — exactly what `transition_to_n2_peer` does for the
   N=1→N=2 case.

**What we changed:** Added a hidden `bedrock storage _peer-join-tier
--tier <t> --peers <json>` CLI subcommand. It (a) unmounts
`/var/lib/bedrock/local/<tier>`, (b) drops the corresponding fstab
line, (c) calls `tier_storage.join_drbd_peer(tier, peers)` which
writes the .res, runs `create-md --force --max-peers=7`, and `drbdadm
up`. The cluster-wide `promote-critical-3way` SSH-fans-out to the
new peer with this subcommand, passing the full peer list (existing
+ new).

**Bonus brittleness flagged for follow-up:** the new peer's local
`render_drbd_res` allocates DRBD node-ids fresh from 0 on each peer's
*own* `cluster.json`. By accident it matches the master's existing
allocation as long as `peers` is iterated in the same order on both
sides. If the master ever has a non-monotonic id assignment (because
of an earlier remove + re-add), the new peer's ids would diverge.
Real fix: the master should *push* its tier's `drbd_node_ids` map to
the new peer's cluster.json before `_peer-join-tier` runs. Logged here
as a future hardening; not load-bearing for v1.0 since the testbed
flow always grows monotonically.

**Source:**
- `tier_storage.py:join_drbd_peer` — the function that should be
  called on every new peer.
- `tier_storage.py:transition_to_n2_peer:1088-1100` — reference
  implementation of "unmount local first, then join_drbd_peer".
- `tier_storage.py:promote_critical_to_3way:1158-1166` — the existing
  master-side helper that distributes only to existing peers.

---

## L28 — `transfer_mgmt_role` must rsync `/etc/bedrock/cluster.json` from old master to new master
**2026-04-30** · clean-rerun Phase 4 (transfer-mgmt sim-1 → sim-2)

**What we thought:** `transfer_mgmt_role` rsyncs `/opt/bedrock/{mgmt,
iso,data,bin}` from the old master to the new master and updates the
per-tier `master` field in `cluster.json` on every node (step 11).
That's enough for the new master to take over.

**What we found:** After the role move, the new master's
`bedrock storage status` reports "Cluster: <none>" and "Nodes: 0"
even though the storage role move worked correctly (DRBD primary,
NFS export, sentinels intact, sim-3 NFS-clients re-pointed).

Cause: the *peers'* `/etc/bedrock/cluster.json` files only ever
contain tier state (modes, drbd_node_ids, peers lists). The canonical
`cluster_name`, `cluster_uuid`, and the full `nodes` map live only
on the master — written by `mgmt_install.install_full()` at
`bedrock init` time. Joiners never get a copy.

When step 11 of `transfer_mgmt_role` ran on sim-2 (the new master),
it merged the tier `master` field into sim-2's existing
cluster.json — but that file lacked the `nodes` map that step 11
*didn't* know to copy. Downstream CLI verbs (`remove-peer`,
`collapse-to-n1`) need that map to resolve peer-name → drbd_ip and
SSH-host, so they would also break.

**What we changed:** Added a step 5b in `transfer_mgmt_role` that
rsyncs `/etc/bedrock/cluster.json` from old → new master *before*
step 11's per-tier master update runs (so the per-tier override
applies to the freshly-rsynced full file).

The fix is master-only because the new master is the canonical owner
of `cluster.json` going forward; peers continue to keep just their
tier-state subset.

**Source:**
- `tier_storage.py:transfer_mgmt_role` step 5 (rsync /opt/bedrock/...)
  was missing /etc/bedrock/cluster.json; new step 5b adds it.
- `mgmt_install.install_full` writes the canonical cluster.json at
  init time; agent_install never does.

**Follow-ups (fixed in clean-rerun-2 commit):**
- `transfer_mgmt_role` step 12: pushes updated `state.json` to every
  node — `mgmt_url`, `witness_host`, and per-node `role` track the
  new master. `bedrock-mgmt` on the new master is restarted so its
  `/cluster-info` endpoint serves the new mgmt_url instead of the
  stale peer-era one. (Was surfaced in clean-rerun-2 when sim-1
  tried to re-`bedrock join` against sim-2's witness and got
  /cluster-info pointing at the long-gone sim-1 master.)
- `agent_install.install` is now transactional: registers with mgmt
  *first*, only writes state.json on success. A connection-refused
  on first join leaves the node fully clean (no stuck `cluster_uuid
  = "unknown"`) so retrying `bedrock join` works without a manual
  `bedrock storage _local-reset` in between. Was the second symptom
  in clean-rerun-2's "Add node1 back" phase.

---

## L29 — `_peer-s3fs` must pass `migrate_local_data=False`
**2026-04-30** · clean-rerun-2 Phase 2 (N=1 → N=2 promote, second pass)

**What we thought:** After L26's fix to `migrate_scratch_into_garage`,
the N=1 → N=2 promote should run end-to-end without manual
intervention. The master's data migrates into Garage; the peer just
s3fs-mounts the bucket.

**What we found:** The master side worked. The peer side failed inside
`bedrock storage _peer-s3fs` with:

```
RuntimeError: MD5 verification failed: local and Garage differ.
```

Cause: `s3fs_mount_scratch(..., migrate_local_data=True)` (the default)
runs `migrate_scratch_into_garage()` on the peer too. The peer's
`/var/lib/bedrock/local/scratch` is empty (only the bare
filesystem from `setup_n1`), but the Garage bucket already has the
*master's* SENTINEL. rsync from empty source is a no-op (no `--delete`),
then MD5 verify compares empty src manifest to non-empty dst manifest —
mismatch, RuntimeError, peer's symlink swap never happens, peer's
`/bedrock/scratch` stays pointing at the local LV.

The "skip migration on peer side" path was already coded as
`s3fs_mount_scratch(... migrate_local_data=False)` — I had used it by
hand last run when manually resuming after L26. The bug is that the
CLI's `_peer-s3fs` subcommand never passed the flag, so the peer
defaulted to `True`.

**Why it didn't bite the prior clean-run:** that run died at L26
*before* reaching the peer-side s3fs call. Manual recovery passed
`migrate_local_data=False` explicitly. The peer-side path was never
exercised end-to-end through the CLI before this rerun.

**What we changed:** `bedrock storage _peer-s3fs` now passes
`migrate_local_data=False`. The peer's `s3fs_mount_scratch` skips the
migration entirely — the master's data is the canonical scratch
content; a joining peer's local scratch is never carried across.

If a future workflow ever requires merging peer-local data into a
shared bucket (e.g. data-only-on-this-node disaster recovery), the
operator can call `migrate_scratch_into_garage()` directly with the
appropriate flags.

**Source:**
- `tier_storage.py:s3fs_mount_scratch` — has `migrate_local_data`
  param defaulting to True.
- `tier_storage.py:transition_to_n2_peer` — only unmounts bulk +
  critical, leaves scratch alone (so peer's local scratch is whatever
  setup_n1 left). Confirms the peer has no data worth migrating.

---

## L30 — There is no CLI verb yet to extend Garage to a new peer
**2026-04-30** · clean-rerun-2 Phase 4 (attempted transfer-mgmt → sim-3)

**What we thought:** When `transfer-mgmt` is asked to move the master
role to a node that's *only* a critical-tier peer (e.g. sim-3 was
added via `promote-critical-3way` but never extended into Garage),
the CLI's pre-flight check would reject it cleanly.

**What we found:** It does — `transfer_mgmt_role` refuses with
"new master 192.168.100.205's tier-bulk is not UpToDate; refusing
to promote." That's correct behavior.

But the deeper issue: there's no CLI verb to *extend* an existing
N-peer tier to a new peer. We have:
- `bedrock storage promote-critical-3way <peer>` — extends critical
  from 2-peer to 3-peer specifically.
- Nothing for bulk extension, nothing for arbitrary N+1.
- Nothing for extending the Garage cluster to a new node.

For tier-bulk, the same pattern as `promote_critical_to_3way` works
when applied manually (write_drbd_resource + adjust + ssh-fanout +
join_drbd_peer on the new peer). For Garage, joining the layout
works but the new peer's S3 endpoint rejects pre-existing access
keys with "Forbidden: No such key" even though the admin API
returns the key correctly. Likely a Garage key-table replication
quirk for keys created before the new peer joined; symptom is
opaque (`Forbidden: No such key`), root cause needs more
investigation. (We saw it after: `garage_form_cluster` from sim-3
with all 3 IPs, layout v1 applied with 3 roles, repair tables
succeeded on all 3, but sim-3's S3 GET still 403'd.)

**What we changed:** Nothing in code yet — this is logged as a
v1.0 follow-up. Workaround for v1.0: don't promote-mgmt-to /
collapse-to a node that wasn't part of the cluster's storage from
the start. Use `transfer-mgmt` only to nodes already participating
in *all* the tiers you'll need on the surviving node.

For the clean-rerun-2 scenario we ended the shrink at sim-2 (which
has bulk + critical + Garage from N=2 promote) instead of sim-3.

**Backlog items added:**
1. CLI: `bedrock storage extend-tier <tier> <peer>` for bulk +
   critical generically (replaces promote-critical-3way as the
   single way to extend any DRBD tier).
2. CLI: `bedrock storage extend-garage <peer>` to install Garage on
   a peer + extend layout + ImportKey existing scratch-key (needs
   research on the right way to replicate keys to a new joiner).
3. Until those exist: agent_install must not lie about scratch
   tier mode — joining an N≥2 cluster should leave scratch in
   "local" mode on the new peer until extend-garage is called,
   not show up as already-Garage in cluster.json.

**Source:**
- `tier_storage.py:transfer_mgmt_role:1543-1554` — pre-flight
  refusal logic (works correctly).
- `tier_storage.py:agent_install.install` — only calls setup_n1, no
  cluster-wide tier extension.
- Garage table replication for keys: needs research; the admin API's
  `ListKeys` shows the key on the new node, but the S3 server's key
  cache rejects it.

---

## L31 — `ssh()` quoting via `json.dumps` exposes `$N` to the local shell
**2026-04-30** · clean-rerun-2 Phase 5 (remove-peer with cross-node Garage drain)

**What we thought:** `ssh(host, cmd)` is safe for arbitrary command
strings — `json.dumps(cmd)` produces a properly-quoted shell argument
that round-trips through SSH.

**What we found:** A `remove-peer` that needed to drain Garage from a
*different* host failed with curl 22 / HTTP 403. The Bearer header
contained the literal `admin_token   = "47cb..."` line, not the token
value.

Cause: `_garage_admin_token(host=peer)` runs

```bash
awk -F'"' '/^admin_token/{print $2}' /etc/garage.toml
```

over `ssh()`. The helper wraps that string with `json.dumps()`, which
emits valid JSON (escaping the embedded `"` as `\"`) but NOT the `$`.
The full local shell command then looks like

```bash
ssh ... root@host "awk -F'\"' '/^admin_token/{print $2}' ..."
```

Inside double quotes, the LOCAL bash expands `$2` — to the local
shell's positional parameter $2, which is empty. So the cmd actually
reaching the remote awk is `print` (no field), which awk interprets as
`print $0` (the whole line). Token extraction returns the full
`admin_token = "47cb..."` line, which then goes verbatim into the
Bearer header → "No such key" / 403.

The bug existed since `ssh()` was first written, but only manifests
when the SSH'd command uses shell `$N`. Most of our SSH-fanout
commands don't (they use absolute paths, no shell variables).
`_garage_admin_token` and any future awk/sed on remote sides would
all silently break the same way.

**Why it didn't bite the prior clean-run:** that run never exercised
a *cross-node* `_garage_admin_token` call against a working multi-node
Garage cluster. Drain happened from a single-node Garage (sim-1
removed when only sim-1+sim-2 had Garage), and `surviving_admin_host`
was the local node so the call went via `run(cmd)` not `ssh(host, cmd)`.

**What we changed:** `ssh()` now uses `shlex.quote(cmd)` instead of
`json.dumps(cmd)`. shlex.quote single-quotes the command for the local
shell, so nothing is expanded — the remote shell receives the
command verbatim.

`json.dumps` was structurally wrong for shell quoting; it's a JSON
encoder, not a shell encoder. shlex.quote is the right tool. The
swap is one line and protects every existing and future SSH'd cmd.

**Source:**
- `tier_storage.py:ssh` before/after.
- Python stdlib [`shlex.quote`](https://docs.python.org/3/library/shlex.html#shlex.quote)
  — proper shell quoting.
- bash(1) "Double Quotes": `$`, ``\``, `"`, `\` are special inside
  double quotes; `\$N` would have escaped, but better not to lay
  the trap.
