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
to reach `url=http://100.86.181.10:3900`, returning empty bytes for
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

**What we changed:** operators chasing weird DRBD adjust errors
should `drbdsetup show <res>` first to ground the investigation.

---

## How to add new entries

When you find something non-obvious, append a new `## L<N> — short
title` section at the bottom with the date and the four headings. Don't
edit historical entries — they're a record of what we knew when. If a
later finding supersedes an earlier one, write a new entry that
references it.

Per-module specs (the companion `<module>.md` next to each
substantial `.py`) should be revised in place to reflect *current*
implementation; this log is the journey.

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


## L32 — Cluster identity belongs in RFC 6598 (`100.64.0.0/10`), not RFC 1918

**Date**: 2026-05-11
**Files**: `installer/lib/cluster_addr.py`, `installer/lib/mgmt_install.py`,
`installer/lib/netd.py`, `mgmt/app.py`

We originally carved cluster loopback `/32` identities from
`100.X.Y.0/24`. That's RFC 1918 private space, which means an
operator using `100.X.Y` internally (Hetzner default networks, AWS
VPC defaults, plenty of homelabs) silently collides with us. Our
"throwaway" `/32`s end up on real LAN hosts; routes fight each
other.

Fix: derive the cluster's `/24` from `sha256(cluster_uuid)` inside
RFC 6598 Shared Address Space (`100.64.0.0/10`). IANA reserved
that block for the ISP-to-CPE link of carrier-grade NAT, with an
explicit rule that operator LANs SHOULD NOT use it. 16,384 distinct
`/24`s available; two Bedrock clusters in the same operator network
collide with probability ≈ 0.006%. Cluster-internal traffic never
leaves the cluster, so we don't care that ISPs use this space
upstream — the only edge case is a node plugged directly into a
CGNAT-ed ISP link, which Bedrock isn't designed for.

The takeaway: when picking a private address block for an isolated
overlay, pick the IANA range whose *intent* matches your use case
(non-public, can't conflict with LANs). RFC 1918 is the wrong
default for cluster overlays specifically because operators have
already claimed every popular /16 inside it.


## L33 — IPv4 link-local across multiple NICs needs `/32` host routes

**Date**: 2026-05-10
**Files**: `installer/lib/netd.py`

RFC 3927 / APIPA assigns each NIC its own `169.254.X.Y/16`, and the
kernel auto-installs `169.254.0.0/16 dev <nic> scope link` per NIC.
With multiple mesh NICs that all have link-local addresses, the
auto-installed `/16`s are ambiguous — the kernel picks the
lowest-metric one when routing to `169.254.151.72`, which may be
the wrong physical NIC for that destination. ARP fails silently on
the wrong wire, the peer is "unreachable," the path appears dead.

Linux has no equivalent of IPv6's `%iface` zone identifier for IPv4
link-local. RFC 3927 explicitly notes IPv4 LL "is not designed for
use across multiple interfaces simultaneously" and recommends a
single LL interface per host.

Fix: bedrock-net installs a `/32` host route per observed peer
link-address, pinned to the NIC that received its probe — e.g.
`169.254.151.72/32 dev enp2s0 scope link`. Longest-prefix-match
beats the auto `/16`, the kernel uses the right NIC, ARP succeeds.

The takeaway: when using IPv4 link-local on a multi-NIC host, you
must explicitly install `/32` host routes per peer. The kernel's
auto-routing isn't sufficient.


## L34 — RFC 3927's ARP probe doesn't cross L2 segments — collisions need an out-of-band mechanism

**Date**: 2026-05-11
**Files**: `installer/lib/netd.py`

Two peers on different isolated L2 segments can independently
negotiate the same `169.254.X.Y` because ARP probes don't traverse
bridges. Within-segment, NetworkManager catches conflicts cleanly
(probe, see reply, pick another). Cross-segment is invisible to
RFC 3927.

Bedrock-net detects the collision because both peers' probes reach
the same discoverers (any node with NICs on both segments). The
local `/32` route install conflicts: `EEXIST` with a different
`dev`.

Fix: the discoverer fires a 3× gratuitous ARP announcement on the
loser's segment from its own MAC, claiming the colliding address.
RFC 3927 §2.5 defense kicks in on the loser — sees a different
MAC asserting its IP, defends once, sees the announcement persist
on retry, renumbers via fresh ARP probe. The discoverer's own
MAC is fine (two IPs on one MAC is kernel-legal; APIPA only checks
MAC inequality).

Two safety guards:
1. `(peer_node, peer_nic)` discriminator: if the same peer interface
   shows up via multiple of our NICs, it's an L2 *merge* (operator
   bridged two segments), not a collision. Firing the countermeasure
   would chase a legitimate peer off its address. Skip.
2. 30 s per-`(addr, my_nic)` cooldown to prevent re-firing every
   sweep while the loser is mid-renumber. Multiple discoverers in
   parallel each maintain their own cooldown — by design, since
   RFC 3927 defense is idempotent against multiple defends.

The takeaway: extending APIPA's safety properties beyond a single
L2 segment requires a higher-layer protocol with both the *peer
identity* and the *link address* in one signed envelope. Standard
mDNS / LLDP / RFC 3927 don't carry that triple; bedrock-net's
signed probe payload does, which is what makes the collision
detection forge-proof.


## L35 — Single-writer means followers can't write LINK_* entries

**Date**: 2026-05-09
**Files**: `installer/lib/netd.py`

Originally every node's bedrock-net daemon called
`rust_ipc.Daemon().append()` to write its observed LINK_UP entries
to the local log. Followers writing to their own log diverges the
hash chain from master's — master's subsequent entries can't
replicate forward because their hash chain assumed an empty follower
log. Result: when a new node joins, follower-sim-2's log can't
accept master's entries 25+ because its own writes have already
occupied those indices.

Fix: only the mgmt master calls `append()`. Followers keep their
in-memory neighbour table for routing decisions but don't write
log entries. The path table in cluster.json is therefore
master-centric — inter-peer paths (peers that don't include master)
aren't visible in the log. That's a known limit; v1.x adds a
follower-POSTs-to-master API so master can append on their behalf.

The takeaway: any process that writes to bedrock-rust's log MUST
verify it's running on the mgmt master first. Even daemons that
seem "local" (like a per-node mesh discovery daemon) violate
single-writer if they all append independently.


## L36 — Witness configuration belongs at N=2, not at `bedrock init`

**Date**: 2026-05-21
**Files**: `bedrock_d/install/cluster_init.py`,
`installer/lib/mgmt_install.py`, `installer/bedrock`

**What we thought**: `bedrock init` should collect the witness host
up front so the cluster is "configured" from day 1.

**What we found**: at N=1 the cluster has no quorum problem — the
witness is a tiebreaker that only becomes load-bearing on the
N=1→N=2 transition. Asking the operator for a witness during
init forces a choice they can't reasonably make yet (the
BedRock-Echo box may not even be deployed; the operator is just
standing up the master). The supplied value just sat in
`state.json["witness_host"]` unused until the first peer joined.

**What we changed**: dropped `witness_host` from
`run_cluster_init()`, `install_full()`, the `bedrock init`
argparse, the `bedrock status` print, and the saga's identity
step. A 2-node cluster without a configured witness runs in
"stay put" mode — current master holds `.254` + singletons,
survivor never auto-promotes. Cluster keeps serving from whichever
node is master but can't survive that master dying without
operator intervention. Documented as the intentional default for
cattle-only 2-node deployments.

The future witness-add UX lives at the dashboard level (likely a
prompt when the operator accepts the first joiner); it writes a
row into the rqlite `witnesses` table — no saga needed, netd picks
up the new probe target on the next tick.

**Note**: `bedrock join --witness HOST` is unchanged. That flag is
the master endpoint the joiner dials (overloaded name; separate
concern).

---

## L37 — Master returning from kill steals master back from the takeover peer
**Date**: 2026-05-21
**Files**: `installer/lib/cluster_arbiter.py`
**Scenario**: Failover B recovery (`testbed/2-sim N=2 + Echo`)

**What we thought**: after a clean Failover B (peer takes over via
witness slot), restarting the killed node would heal into the
cluster as Secondary. The witness slot for the new master is fresh
and tagged LMS; the returning node should read it and stand down.

**What we found**: two concurrent arbiter bugs cause the *returning*
node to seize back the master role on boot.

1. **`demote when no longer singleton` on the current master**.
   sim-2's arbiter holds `.254` + Primary after Failover B. The
   instant sim-1 reconnects, sim-2's converge tick logs `arbiter:
   demoting this node (was singleton host)` and unwinds the whole
   stack — `ip addr del 100.64.105.254/32`, `systemctl stop
   bedrock-rqlited-arbiter`, `umount`, `drbdadm secondary`. The
   "singleton" heuristic conflates *"I was alone"* with *"I should
   stop being master now that peers are back"*. Correct rule:
   demote only when arbitration explicitly hands the role away.

2. **Takeover protocol on the rejoining node checks only its own
   slot**. sim-1's arbiter sees `tier-cluster DRBD present` and
   walks the takeover guard, finds its own pre-shutdown slot
   `last_master=1, self=1`, concludes *"no prior master to take
   over from"*, and immediately runs `drbdadm primary`. It never
   reads the *peer's* slot, which still has a fresh LMS-tagged
   marker from sim-2. With (1) already having demoted the rightful
   master, the primary-grab succeeds.

Observed sequence:
```
14:58:35 sim-2  arbiter: promotion complete (ip=100.64.105.254 mount=…)   # Failover B success
14:59:30 sim-2  arbiter: demoting this node (was singleton host)          # bug #1 fires on peer rejoin
15:02:23 sim-1  arbiter: takeover protocol — no prior master to take      # bug #2 misreads own slot
                  over from (last_master=1, self=1); proceeding
15:02:23 sim-1  arbiter: promotion complete (ip=100.64.105.254 mount=…)
```

Cluster is functionally available throughout (one node always
owns `.254`), but the master role thrashed unnecessarily: a
crash-restart cycle ping-pongs Primary back to the (less
trustworthy) node that just died. That's the opposite of what
you want — you want to *stay* with the proven-surviving peer.

A third minor finding: `drbdadm up tier-critical` is not invoked
on boot before the arbiter tries `drbdadm primary`. The first
several converge ticks fail with `Unknown resource` until DRBD is
brought up manually (or by another path). The arbiter must `up`
the resource as a prerequisite of promote, or a separate
reactor step has to handle it before promotion is attempted.

**What we changed**: nothing yet — captured for v1.0 fix.
Concrete changes needed:
- Remove the `was-singleton` demote path. Master demotes only on:
  (a) loss of quorum, (b) successful peer takeover protocol
  proven via witness slot, (c) operator-initiated `bedrock node
  leave`. Re-joining a peer is not a demote trigger.
- Rejoining-node takeover guard must read **peer slot**, not just
  self slot. If peer slot is fresh (age < N seconds) AND
  `tag.lms=1`, peer is the current master — stand down and become
  Secondary regardless of own slot history.
- Promote step must `drbdadm up <resource>` before `drbdadm
  primary`. Currently relies on something else to bring the
  resource up on boot.

**Reference**: this scenario, no commit yet.

---

## L38 — Local CLI must not use `state["mgmt_url"]` for its own API calls
**Date**: 2026-05-21
**Files**: `installer/lib/vm.py`, `installer/bedrock` (multiple callsites)
**Scenario**: VM lifecycle smoke test on the 2-sim testbed

**What we thought**: `state["mgmt_url"]` is a single source of truth
for the cluster's management endpoint; CLI code can dial it
transparently regardless of where the CLI runs.

**What we found**: `state["mgmt_url"]` is the **LAN-facing HTTPS URL**
(e.g. `https://192.168.2.51:8443`). It exists for *remote* consumers
(browser dashboards on other nodes, peer-node API calls). When the
local `bedrock` CLI on the master dialed it for `vm list` / `vm
delete`, two things broke:

1. **Cert SAN mismatch**: the mgmt cert is issued for the cluster
   hostname / `100.64.105.254`, not the LAN IP, so
   `urllib.request.urlopen` raised
   `CERTIFICATE_VERIFY_FAILED: certificate is not valid for
   '192.168.2.51'`.
2. **Even after pointing to `127.0.0.1:8001`** (the loopback HTTP
   listener that bypasses TLS), `vm list` and `vm delete` returned
   `HTTP Error 401 Unauthorized` — the loopback API requires a
   Bearer operator token. `/api/cluster` is exempt (that's why
   `bedrock status` works), but `/api/vms/*` is not.

**What we changed**:
- `lib/vm.py`: stopped reading `state["mgmt_url"]` for local calls;
  both `_api_get` and `_api_post` now hardcode
  `http://127.0.0.1:8001`. Same fix had to be applied earlier to
  `bedrock status` and `cmd_operator` — this completes the pattern
  for VM operations.
- Open: the loopback API needs to either trust local UID 0 (it's
  already loopback-only) or the CLI needs to obtain a token from
  `/etc/bedrock/local-cli-token` on disk (rotated by `bedrock-d`).
  Today the workaround is `BEDROCK_OPERATOR_TOKEN=… bedrock vm …`,
  which is fine for CI but not for an admin SSH'd into a node.

**Side finding**: a *running* `bedrock vm create` log line says
`[state] vm_created write skipped: rqlite POST … Connection
refused`. The VM was still created (lvcreate + image fetch + virsh
define succeeded), but the rqlite write to record the new VM never
happened. Need to confirm whether the orchestrator picks this up on
next snapshot fold or whether the state is silently lost.

**Reference**: this scenario, no commit yet.

---

## L39 — Testbed sim's second PV (`loop0`) comes up read-only after `virsh destroy`
**Date**: 2026-05-21
**Files**: testbed-only — `/var/lib/bedrock-vg-extra.img` setup
**Scenario**: Failover B recovery on `bedrock-sim-1`

**What we thought**: a `virsh destroy` + `virsh start` cycle is
equivalent to an unclean power cycle and the VM should come back to
a usable state, including its loopback-backed second PV.

**What we found**: after `virsh destroy`, the next boot left
`/dev/loop0` attached **read-only**:

```
NAME       SIZELIMIT OFFSET AUTOCLEAR RO BACK-FILE
/dev/loop0         0      0         0  1 /var/lib/bedrock-vg-extra.img
                                       ^ RO=1
```

The backing file `/var/lib/bedrock-vg-extra.img` was still 0644 and
writable at the filesystem level — the RO flag was on the loop
device itself. LVM happily activated the VG over the mixed-mode PVs
and basic LVs worked, but any operation that needed to write VG
metadata (e.g. `lvcreate -V … --thin`) failed with:

```
Error writing device /dev/loop0 at 33280 length 4608.
WARNING: bcache_invalidate: block (1, 0) still dirty.
Failed to write metadata to /dev/loop0.
Failed to write VG bedrock.
```

That's what surfaced as the `bedrock vm create` failure before it
could even reach the rest of the create flow.

Fix is `losetup -d /dev/loop0 && losetup --find --show
/var/lib/bedrock-vg-extra.img && pvscan --cache && vgchange -ay
bedrock`.

**Status**: testbed-only — production nodes don't use loop-backed
PVs. Captured because the symptom (`lvcreate` failing with
`Error writing device`) was confusing in the moment and the
underlying RO state isn't obvious from `vgs` / `lvs` alone — only
`losetup -l` exposes it.

**Reference**: this scenario, no commit.

---

## L40 — Build pipeline must rebuild the Svelte UI, not just tar it
**Date**: 2026-05-22
**Files**: `installer/iso-build/build-iso.sh`, `testbed/publish-to-s3.sh`
**Scenario**: ISO upload returns 405 Method Not Allowed on the dashboard

**What we thought**: `mgmt.tar.gz` packaging was a pure tar operation
— the mgmt/ source tree is the deliverable. Whoever's working on
the UI runs `npm run build` themselves when they change a `.svelte`
or `.ts` file, and the next package picks it up.

**What we found**: that assumption silently rotted. On 2026-05-13
the Svelte UI was built. On 2026-05-20 commit `f6842c6` renamed
backend endpoints — including the ISO upload route from
`/api/isos/upload` to `/api/isos`. `mgmt/ui/src/lib/api.ts` was
updated to match. **No one ran `npm run build`.** The compiled
chunk `Cal9XJuY.js` in `mgmt/ui/build/_app/immutable/chunks/`
stayed 7 days stale, still POSTing to the old `/api/isos/upload`.
The backend, correctly, returned 405.

Worse, `testbed/publish-to-s3.sh` excludes `mgmt/ui/src/` from the
tarball (it's only meant to ship the prebuilt bundle). So even
the right source code can't reach the deployed dashboard — only
the stale build does. Operators see a dashboard with broken
buttons that can't be fixed without rebuilding upstream.

This is structurally the same shape as L11 (ISO payload layout
drifts silently): a packager assumes someone else did the
build step, and no CI check catches it.

**What we changed**: both `build-iso.sh` and `publish-to-s3.sh`
now detect when `mgmt/ui/src/` is newer than `mgmt/ui/build/` and
run `(cd mgmt/ui && npm run build)` before tarring. If npm isn't
installed they print a loud WARN that the shipped UI bundle will
be stale, and continue — better a known-broken artifact than a
silent failure later.

The check is `find mgmt/ui/src -newer mgmt/ui/build -print -quit`
which exits as soon as it finds one file newer than the build dir,
making it cheap to run every package.

**Reference**: this scenario, commit pending.

---

## L41 — Linux thunderbolt-net real ceiling is 10-25 Gbps; report 15 Gbps, not 25
**Date**: 2026-05-22
**Files**: `installer/lib/netd.py`
**Scenario**: 2-physical-node Thunderbolt iperf3 matrix on Ryzen 7640HS / AlmaLinux 10.1

**What we thought**: Thunderbolt 3 is a 40 Gbps wire; Thunderbolt 4
the same; USB4 v2 / Thunderbolt 5 is 80 Gbps. Reporting `tb0` as
~20 Gbps with a `25000` speed bucket in the topology view felt
conservative.

**What we found**: an iperf3 matrix on the two physical nodes
(MTU 1500 vs 65520, GRO on vs off) hit a hard 12.0 Gbps wall on
TCP across **every** combination — 4 streams × exactly 3.00 Gbps
each. UDP scaled with MTU (5 Gbps → 12 Gbps) but receiver loss
stayed at 35% beyond 8 Gbps. The "exactly 12.0 Gbps regardless
of settings" pattern is the giveaway: it's a software ceiling,
not a wire limit.

Root cause is `thunderbolt-net`'s driver design: single RX queue,
single NAPI instance, all receive softirq work serialized on one
CPU. `ksoftirqd` pegs ~99% on the receiver. No RSS support. The
kernel's "TCP performance may be compromised" warning at boot is
the conservative version of this fact.

Documented community ceilings:

| Platform | Real-world Linux unidir |
|---|---|
| AMD Phoenix / Hawk Point (Ryzen 7040/8040 USB4) | ~11-12 Gbps |
| AMD Strix Halo (Ryzen AI Max, USB4 v2 / TB5) | ~10 Gbps |
| Intel Maple Ridge (TB4), untuned | 8-17 Gbps |
| Intel Maple Ridge (TB4), IRQ-pinned + qdisc-tuned | 25-26 Gbps |
| Intel Barlow Ridge (TB5) | no public Linux numbers yet |
| macOS 26.2+ over the same hardware (native RDMA-over-TB) | 20-78 Gbps |

The earlier "AMD negotiates at 2.5 GT/s" claim was wrong. Per
Mario Limonciello's kernel patch series, the USB4 spec mandates
that PCIe ports tunneling traffic over USB4 advertise Gen1
2.5 GT/s — on both Intel and AMD. The real AMD/Intel gap is in
the host controller's DMA engine + thunderbolt-net interaction,
not link rate negotiation.

**What we changed**:
- `nic_speed_mbps("thunderbolt0")` now returns 15000 instead of
  20000. 15 is the honest midpoint of the documented range.
- `bucket_speed()` gained an explicit 15000 step between 10000
  and 25000, so a thunderbolt-net link doesn't round up to "25G"
  in the cluster_cables table and oversell the achievable
  throughput. The mesh-link preference still picks tb0 over a
  2.5G LAN bridge.
- Docstrings updated to spell out the platform breakdown so the
  next reader doesn't repeat the marketing-rate trap.

**What we did NOT change**: no IRQ-pinning / qdisc / NAPI-thread
tweaks. They add up to maybe 1-2 Gbps on Intel platforms and zero
on AMD; not worth the per-platform code path. If a multi-queue
patch ever lands upstream (or Apple's RDMA-over-Thunderbolt gets
a Linux counterpart), revisit.

**Follow-up measurement that sharpens the picture (2026-05-22):**
ran a CPU-frequency matrix on the same two AMD nodes to confirm
the bottleneck layer:

| Governor / freq cap | Throughput | Receiver CPU6 softirq | CPU idle |
|---|---|---|---|
| performance (~5 GHz) | 12.0 Gbps | 41.5% | 57.2% |
| powersave (dynamic) | 11.9 Gbps | 42.0% | 56.6% |
| powersave @ 1 GHz cap | 7.26 Gbps | 82.6% | 16.5% |

At full clock the receiver core handling softirq is **not
saturated** — it sits at 42%. The "single-CPU softirq bound"
narrative is only partly right on this hardware. The actual
ceiling at 5 GHz is the **AMD Pink Sardine NHI controller's DMA
engine** delivering bytes from the USB4 fabric to host memory;
softirq has headroom waiting for packets that don't arrive.

The Intel-tuned-box story is different: there the NHI delivers
~25 Gbps and softirq is genuinely the wall (98-99% on one core).
Same driver, different silicon, different bottleneck — which is
why Intel boxes respond to IRQ-pinning + qdisc tuning and AMD
boxes don't.

Implication for Bedrock: skip IRQ/qdisc tuning entirely. It
helps on Intel by a few Gbps if you have a P-core to pin to, and
does nothing on AMD. The per-platform code isn't worth the
complexity for ≤2 Gbps of upside. Bedrock's mesh-link preference
just needs an honest speed bucket — which is now 15000.

**Source-level RCA (2026-05-22 follow-up):** read the kernel
source. The 12 Gbps wall is set by a single constant in
`drivers/thunderbolt/nhi.c::nhi_enable_int_throttling()`:

```c
/* Throttling is specified in 256ns increments */
u32 throttle = DIV_ROUND_UP(128 * NSEC_PER_USEC, 256);
...
for (i = 0; i < MSIX_MAX_VECS; i++)
    iowrite32(throttle, nhi->iobase + REG_INT_THROTTLING_RATE + i*4);
```

**128 µs hardcoded** per MSI-X vector. 1 / 128 µs = 7,812 IRQ/sec.
With 2 active vectors × 64-packet NAPI default × 1500-byte MTU
that's 11.99 Gbps — matches the measured 12.0 Gbps wall exactly.

Other source facts:
- `TBNET_RING_SIZE = 256`, single ring depth RX and TX.
- NAPI weight is the kernel default 64 (no explicit weight).
- **Zero AMD-specific code** — `pci_device_id nhi_ids[]` lists
  only Intel device IDs plus a `PCI_CLASS_SERIAL_USB_USB4`
  catch-all that AMD Phoenix matches. Same throttle, same ring
  depth, same NAPI weight on both vendors.
- Driver defines no `ethtool_ops`, so `ethtool -C` can't tune
  the throttle and `ethtool -S` reports "no stats available".
- No multi-queue patches in mainline or net-next.

The "AMD is slower than Intel" community framing turns out to be
partially wrong. Both vendors are bounded by the same constant
for a single point-to-point cable. The Intel-25 Gbps reports
come from aggregate across multiple TB cables in a ring
topology, or from Intel NHIs activating more MSI-X vectors than
AMD's 2-vector configuration. Per-pair, both top out near 12
Gbps from this constant.

The patch to break the ceiling is trivial in code (lower the
128 to e.g. 32) but requires either:
  - An in-tree kernel patch + LKML conversation (Mario
    Limonciello at AMD maintains thunderbolt-net),
  - A locally-built kernel module override (out of Bedrock's
    distribution scope), or
  - The macOS-26.2-style native RDMA-over-Thunderbolt path,
    which has no Linux counterpart as of kernel 6.18.

Windows reference (sparse): Microsoft's "USB4 Bridge" driver
on Intel TB4 hits ~15–16 Gbps unidir Mac↔Windows (Brejcha 2024),
~50% better than Linux on the same Intel hardware, suggesting
Windows doesn't apply the same throttle constant. No
Windows-on-AMD-Phoenix iperf3 numbers are published anywhere.

**Reference**: this scenario, commit pending. Source file
references: `drivers/thunderbolt/nhi.c` (throttle constant) +
`drivers/net/thunderbolt/main.c` (ring + NAPI setup) +
`drivers/thunderbolt/nhi_regs.h` (REG_INT_THROTTLING_RATE).

---

**Deeper RCA — the actual Intel/AMD difference (2026-05-22 third pass):**

The "same throttle, different throughput" puzzle resolves at
`drivers/thunderbolt/nhi.c:1169-1190`:

```c
static void nhi_check_quirks(struct tb_nhi *nhi) {
    if (nhi->pdev->vendor == PCI_VENDOR_ID_INTEL) {
        /* Intel hardware supports auto clear of the interrupt
         * status register right after interrupt is being issued. */
        nhi->quirks |= QUIRK_AUTO_CLEAR_INT;
        ...
```

**The throttle isn't the binding bottleneck on AMD — `QUIRK_AUTO_CLEAR_INT`
not being set is.**

- **Intel path** (quirk set): the NHI auto-clears the interrupt
  status bit at IRQ assert. The controller can re-arm immediately
  and coalesce more completions during the NAPI drain window.
  Result on TB4: ~400 IRQ/sec, ~10,000 packets per IRQ, ~25 Gbps.
- **AMD path** (quirk NOT set — vendor-ID-gated): the driver
  must manually MMIO-write to `REG_RING_INT_CLEAR` in the hard-IRQ
  handler (`nhi.c:448-460`) *before* NAPI runs. Until that
  uncached MMIO write retires, the controller can't re-arm. Any
  completions arriving during the NAPI window get coalesced into
  the *next* IRQ. The IRQ rate climbs until it hits the 128 µs
  throttle ceiling. Result: ~7,600 IRQ/sec, ~66 packets per IRQ,
  ~12 Gbps.

The kernel comment at `nhi.c:112-115` explains *why* AMD's path
disables auto-clear:

> *"Other routers explicitly disable auto-clear to prevent
> conditions that may occur where two MSIX interrupts are
> simultaneously active and reading the register clears both
> of them."*

So AMD's NHI has a documented hardware race condition with
auto-clear of MSI-X status registers. Mario Limonciello (AMD's
USB4 kernel maintainer) wrote the conservative fallback path in
commit `468c49f44759` ("thunderbolt: Disable interrupt auto
clear for rings") to avoid the race. The cost is the per-IRQ
batch starvation we measure.

**The per-descriptor flag is set unconditionally**, at
`nhi.c:251`:

```c
descriptor->flags = RING_DESC_POSTED | RING_DESC_INTERRUPT;
```

So the silicon is asked to interrupt on every completed frame.
On Intel the auto-clear lets the controller coalesce anyway. On
AMD it can't.

**Proof the AMD silicon CAN batch more**: same Pink Sardine
controller carrying tunneled PCIe traffic (NVMe-over-TB) hits
25-30 Gbps. That path uses the NVMe controller's own MSI-X
policy and bypasses the NHI ring entirely. So the silicon DMA
fabric isn't the limit — only the NHI ring's manual-clear
protocol is.

**Trivial-looking experiments that would move AMD's wall** (none
attempted yet; not Bedrock scope but logged for the curious):

1. **3-line change**: set `RING_DESC_INTERRUPT` only on every Nth
   descriptor (e.g. every 64th) in `nhi.c:251`. The hardware
   stops asserting IRQ on every frame; it batches at descriptor
   granularity. Safe: the 128 µs throttle still bounds worst-case
   latency. Predicted: AMD goes from 12 → ~25 Gbps (hitting the
   single-softirq-core wall Intel hits today).
2. **Risky**: enable `QUIRK_AUTO_CLEAR_INT` for AMD Pink Sardine
   and benchmark. Could expose the dual-MSI-X-clear race the
   conservative path was written to avoid. Worth scoping with
   Mario Limonciello.

**Hardware-buying corollary** — for boxes that should beat the
12 Gbps wall today without kernel hacking, use platforms with
**discrete Intel TB controllers**, regardless of host CPU vendor:

| Host CPU | Discrete TB chip | Predicted thunderbolt-net |
|---|---|---|
| Ryzen 7640HS (integrated AMD USB4) | none | ~12 Gbps (observed) |
| Ryzen 7800X3D on ASUS ProArt X670E | Intel JHL8540 Maple Ridge | ~20-25 Gbps |
| Ryzen AI Max 395 on Minisforum MS-S1 Max | Intel JHL9580 Barlow Ridge (TB5) | ~25-40 Gbps (untested) |
| Any Intel Core Ultra | integrated/discrete Intel | ~25 Gbps (TB4), more on TB5 |

The `QUIRK_AUTO_CLEAR_INT` gate is by vendor ID of the NHI PCI
device — not by the host CPU. AMD CPU + discrete Intel TB chip
= Intel quirk path enabled. The PCI vendor of the controller is
what matters.

**Multi-queue extension (logged for completeness):**

The driver currently allocates **1 RX ring, 1 TX ring, 2 active
MSI-X vectors, 1 NAPI instance** per `thunderbolt-net` device.
The NHI hardware supports up to 12 hops per direction and the
driver requests 6-16 MSI-X vectors via `pci_alloc_irq_vectors`.
Infrastructure exists; the driver just doesn't use it.

A multi-queue rewrite (e.g. 4 RX rings + 4 TX rings + 8 MSI-X
vectors + 4 NAPI instances) would spread softirq work across 4
CPUs. Predicted ceilings:

- **AMD Phoenix integrated USB4**: 4 × ~12 Gbps single-queue cap
  = ~48 Gbps theoretical, bounded by the TB3/TB4 wire at 40 Gbps.
  Real-world ~30+ Gbps likely.
- **Intel TB4 + multi-queue**: 4 × ~25 Gbps = ~100 Gbps
  theoretical, bounded by wire at 40 Gbps. Real-world wire-rate.
- **Intel TB5 (Barlow Ridge) + multi-queue**: 4 × ~25 Gbps
  bounded by wire at 80 Gbps. Real-world ~60+ Gbps likely.

Caveats:
- A **single TCP flow** can only use one RX queue (in-order
  delivery requirement). Multi-queue helps **aggregate**
  throughput across multiple concurrent flows. For Bedrock
  that's still useful: DRBD per-resource, SeaweedFS volume
  sync, mgmt traffic, witness heartbeats — many flows.
- The Thunderbolt protocol has the concept of "hops" — virtual
  channels between endpoints. Each NHI ring corresponds to a
  hop. Negotiating 4 RX hops + 4 TX hops with the peer is a
  protocol-level change, not just a driver-internal one.
- Rewrite is **substantial**, not a 3-line change. New flow
  steering, NAPI multiplexing, hop negotiation. A real upstream
  contribution, weeks of work.

So Tommy's multi-queue arithmetic is correct in principle. The
3-line descriptor-coalescing patch above is the cheap path to
unlock the AMD-specific gap (12→25 Gbps). The multi-queue rewrite
is the only path to wire rate, applies to both vendors, and is
a much bigger upstream conversation.

Captured as a future upstream contribution; not a Bedrock v1.0
deliverable.

---

**Key takeaways (closing the lesson):**

1. **"TCP performance may be compromised" kernel warning on
   thunderbolt-net is cosmetic** for our workload. TCP is not
   compromised — it's the kernel hedging on driver limits that
   don't bind on real cluster traffic.

2. **The surprising Intel-vs-AMD gap is one vendor-ID check, not
   silicon.** `drivers/thunderbolt/nhi.c:1169`:
   ```c
   if (nhi->pdev->vendor == PCI_VENDOR_ID_INTEL)
       nhi->quirks |= QUIRK_AUTO_CLEAR_INT;
   ```
   Intel NHIs auto-clear the MSI-X status bit in hardware at IRQ
   assert and re-arm immediately. AMD NHIs need a manual MMIO
   clear in the hard-IRQ handler that serializes against re-arm.
   Same wire, same driver — Intel ~10,000 packets/IRQ + ~25 Gbps,
   AMD ~66 packets/IRQ + ~12 Gbps. The reason is a real AMD
   silicon race condition with dual-MSI-X-clears (kernel comment
   at `nhi.c:112-115`), so the conservative fallback path isn't
   wrong — it just costs throughput.

3. **AMD silicon CAN move faster; the NHI ring path can't reach
   it.** Tunneled PCIe (NVMe-over-TB) hits 25-30 Gbps on the
   same Pink Sardine controller. Different MSI-X policy,
   different code path, no manual-clear roundtrip. So the
   12 Gbps wall is a driver-policy artifact, not a hardware
   ceiling.

4. **Bedrock hardware-sizing rule: controller vendor decides
   tier, not host CPU.** Any platform with a discrete Intel TB
   chip (JHL8540 Maple Ridge, JHL9580 Barlow Ridge) gets the
   Intel-tier performance regardless of CPU. AMD CPU on a board
   with an Intel TB controller (ASUS ProArt X670E, Minisforum
   MS-S1 Max) sidesteps the 12 Gbps wall entirely.

5. **Bedrock code action: nothing further.** The 15 Gbps speed
   bucket landed in `netd.py` is the honest cross-platform
   midpoint. No auto-Jumbo, no GRO-off, no IRQ-pinning code —
   none of those move TCP throughput on the measured workload.

6. **Open upstream contribution** (deferred, not Bedrock scope):
   3-line patch to `nhi.c:251` setting `RING_DESC_INTERRUPT` only
   on every Nth descriptor lifts the AMD single-queue cap from
   ~12 to ~25 Gbps. Single-stream and aggregate both benefit.
   Path of least resistance for anyone with time to chase it.

**Lesson closed.**

---

## L42 — httpx 0.28's `cert=` and `verify=` are silently dropped when a custom transport is provided
**Date:** 2026-05-25
**Files:** `installer/lib/rqlite_client.py`

**What we thought:** to configure mTLS on httpx.Client, pass
`cert=(crt, key)` and `verify=ca_path` to the Client constructor.
That's the documented surface for client TLS.

**What we found:** two subtle httpx 0.28 behaviours stacked:

1. The `cert=(crt, key)` tuple did not reliably present the client
   cert during the TLS handshake. rqlited responded with
   `TLSV13_ALERT_CERTIFICATE_REQUIRED` even though the file paths
   were correct and the same files worked fine via curl
   (`--cert/--key/--cacert`).
2. When the Client is constructed with a custom `transport=`, the
   Client-level `verify=` and `cert=` arguments are **silently
   dropped**. They only configure the *default* transport. Anyone
   passing `transport=httpx.HTTPTransport(retries=0)` (which we do
   to control retry behaviour) gets unverified-and-unauthenticated
   TLS by default — exactly what we don't want.

The combination meant the first fix attempt (build an SSLContext and
pass `verify=ctx` on the Client) appeared to fail with the same
"unable to get local issuer certificate" error.

**What we changed:** build the `ssl.SSLContext` explicitly with
`create_default_context(cafile=ca)` + `load_cert_chain(certfile,
keyfile)`, then pass it to the **transport's** `verify=` argument
(not the Client's):

```python
ctx = ssl.create_default_context(cafile=str(ca_crt))
ctx.load_cert_chain(certfile=str(node_crt), keyfile=str(node_key))
httpx.Client(
    base_url=...,
    transport=httpx.HTTPTransport(verify=ctx, retries=0),
)
```

This single change made mTLS work end-to-end against rqlited.

**Operational gotcha during the rollout:** the long-running
`bedrock-d` daemon imports rqlite_client.py once at module load
and caches its connection pool. After replacing the file on disk,
we have to **restart bedrock-d** before the new code takes effect.
This is true for any module-level state change.

---

## L43 — Long-running daemons need explicit restart after lib/ updates
**Date:** 2026-05-25
**Files:** general operational note

**What we thought:** updating a `lib/*.py` file in
`/usr/local/lib/bedrock/lib/` would take effect on the next
RqliteClient construction.

**What we found:** `bedrock-d` imports the modules once at process
start. Subsequent file edits don't affect the running process. We
saw stale module behaviour even after the file on disk had the
correct content. Any rolling upgrade or hotfix that touches a
lib module needs an accompanying `systemctl restart bedrock-d`.

**What we changed:** noting it explicitly here. Long-term, the
roll-out story for cluster-wide lib changes (e.g. the CA rotation
case in `docs/operator-overrides.md`) needs to include a
controlled-restart step.

---

## L44 — Testbed cloud-init `cloud-init eth0` NM connection wins eth0 over our bridge slave
**Date:** 2026-05-26
**Files:** `testbed/cloud-init/user-data.tmpl`

**What we thought:** the `br0-eth0.nmconnection` file we write into
`/etc/NetworkManager/system-connections/` reliably puts eth0 in as
a slave of br0, so the VM comes up at the intended static
`192.168.2.20N` we templated into br0. Every testbed sim works this
way.

**What we found:** the cloud-image VM startup is a two-phase race
that the testbed loses non-deterministically. cloud-init also
writes an auto-generated NM connection at
`/etc/NetworkManager/system-connections/cloud-init-eth0.nmconnection`
with `autoconnect-priority=120`. Our `br0-eth0.nmconnection` has no
priority specified — NetworkManager treats that as priority 0. At
NM start-up:

- If cloud-init's `cloud-init eth0` activates first → eth0 grabs a
  DHCP lease as a standalone interface (typically `192.168.2.62`
  from the home router). `br0` is left with no slave, sits in
  `NO-CARRIER state DOWN`, and `192.168.2.201` (the IP we wrote
  into `br0`) is unreachable.
- If our `br0-eth0` activates first → eth0 becomes the bridge
  slave, br0 comes UP with `.201`, cloud-init's connection sits
  inactive.

This is racy. On a long-running session both outcomes are observed
on the *same* VM after various restart cycles. The symptom from the
host was an extremely confusing `192.168.2.201 dev br0 FAILED` ARP
entry — neither flushable without root, and the cluster nodes could
still reach each other over old ARP caches, so we mis-diagnosed the
real fault for several hours as a Bedrock TLS issue.

The diagnostic that finally cracked it:
```
sim-1$ ip -br addr show br0 eth0
br0   DOWN    192.168.2.201/24     ← the IP we wanted, no carrier
eth0  UP      192.168.2.62/24      ← DHCP-grabbed standalone
sim-1$ nmcli -t -f NAME,DEVICE,STATE connection show
cloud-init eth0:eth0:activated     ← won the race
br0-eth0::                          ← couldn't claim eth0
```

**What we changed:** two-layer fix in
`testbed/cloud-init/user-data.tmpl`:

1. Added `autoconnect-priority=200` to both `br0.nmconnection` and
   `br0-eth0.nmconnection` — beats cloud-init's 120, so the bridge
   stack consistently wins the race at every boot.
2. Added `nmcli con delete "cloud-init eth0" 2>/dev/null || true`
   to the `runcmd:` block — removes the file at first-boot so the
   competing connection doesn't even exist for subsequent NM
   restarts. Belt-and-suspenders.

**Impact on the dev workflow:** the symptom looked exactly like a
cluster-internal mesh / TLS / sshd issue and we spent significant
testbed validation time chasing it as a Bedrock bug. It is purely
a testbed-cloud-init issue and only affects VMs spawned via
`BEDROCK_TESTBED_USE_CLOUD_IMG=1`. Production installs (ISO path)
don't go through cloud-init and aren't affected.

---

## L45 — systemd `MemoryHigh`/`MemoryMax` silently throttled the whole stack
**2026-05-29** · commits fb10ab9 → 944d880 → d072991

**What we thought:** capping each weed / rqlited / bedrock-d unit with
systemd `MemoryHigh`/`MemoryMax` was a tidy way to keep Bedrock's memory
footprint small and leave RAM for guest VMs.

**What we found:** `MemoryHigh` is a *throttle*, not a ceiling. When a
service's heap crosses it the kernel parks the process's threads in
`mem_cgroup_handle_over_high` on **every allocation** — the service stays
"active" but crawls, and does not recover on its own. `bedrock-weed-s3`
(`MemoryHigh=128M`) collapsed from ~1 GB/s to **~25 KB/s** during the
first VM backup (the S3 read path holds 8 MiB chunk buffers in a
hardcoded 256-slot `readerCache`, which blew straight past 128 MB;
`memory.events` showed **223 726** `high` throttle events) and never came
back without a restart. The decisive proof: a *fresh* `weed s3` instance
read the identical object in **20 ms** while the throttled production one
timed out — so every layer underneath was healthy (disk 3.9 GB/s, inter-
node net 200 MB/s, filer 20 ms, volume needles ~30 ms) and the cgroup was
the sole cause. Raising the live limit 128M→1G restored 867 MB/s
instantly, no restart. The same trap sat on `bedrock-d` (512M),
`bedrock-rqlited` (192M) and the arbiter (128M) — a strong candidate for
the long-standing *"the testbed goes slow/weird after running a while"*
class of phantom flakiness, as long-lived daemons accumulate heap and
cross their high-water mark.

**What we changed:** removed `MemoryHigh`/`MemoryMax` from **every**
Bedrock unit — they are never to be set again. Memory is now bounded
**in-process**, where the runtime can act intelligently:
- `GOMEMLIMIT` on every Go service (weed-s3/filer/master/volume, rqlited,
  arbiter) — a Go-runtime *soft* target: the GC paces toward it and the
  process keeps serving, instead of the kernel freezing it. weed 4.25 is
  built with go1.26 and has no in-code `SetMemoryLimit`, so it honors the
  env var directly.
- `weed volume -index=leveldb` — the volume's dominant RAM driver is the
  needle index, which `-index=memory` loads entirely into RAM (~11 B ×
  every object). leveldb keeps it on disk (~a few MB/volume) for one extra
  lookup per Get. A little more disk, far less RAM — exactly the trade we
  want.
- `bedrock-d` is Python (no `GOMEMLIMIT`); it runs unbounded, as systemd
  defaults intend. The host OOM-killer is the only real backstop, by design.

Guarded against regression by
`tests/test_service_units_shape.py::test_no_cgroup_memory_limits`.

**Reference:** full RCA in this session. The s3 chunk-buffer pool
(`s3ReaderCacheDownloaderLimit=256`, ~2 GiB worst case) and the prefetch
count (`DefaultPrefetchCount=4`) are both hardcoded in weed 4.25 with no
flag — so `GOMEMLIMIT` is the *only* lever for the s3 read path.
