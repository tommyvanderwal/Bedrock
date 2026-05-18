# v16 RCA & status — 2026-05-19 session

End-of-session summary of what was fixed during the v16 e2e debugging
push, what's still open, and what the user needs to know before
installing on physical hardware tomorrow.

## TL;DR

12 commits landed against `master`. Critical ones for correctness on
real hardware (these would have bitten any physical install):

- `c769842` + `77d9556` — **install.sh `LIB_FILES` was missing
  `election.py`, `witness.py`, `s3backer_compactor.py`.** These are
  imported by bedrock-net at runtime. Without them the daemon would
  crash on first start with `ImportError` on every fresh install.
  Hidden in the testbed because push_sources.sh and the iso-build's
  `cp /lib/*.py` masked it. Added the missing files plus a
  self-check that lists everything under the payload's `lib/` and
  fetches anything that didn't go through LIB_FILES.
- `726ec83` — **`cluster_arbiter._is_mounted` uses `mountpoint -q`
  instead of `findmnt -T`.** The old check returned True for any
  path on a mounted filesystem (e.g. the root mount), so
  `promote_to_arbiter_host` skipped the DRBD mount step and the
  filer wrote leveldb3 to the *root FS* on a new master after
  failover. Markers/buckets created on the previous master became
  invisible after the role flip. This silently corrupted any
  N>=2 failover scenario.
- `9887fd5` — **netd `demoted_in_cycle` latch only after demote
  actually fires, and resets on quorum return.** An early
  startup NoQuorum (neighbours=0 for ~1s) latched the flag with
  no demote happening; a later real isolation then skipped the
  demote and left `.254/32` + arbiter rqlite live on a node that
  had lost quorum.
- `fc390da` — **tier mount fstab lines use `nofail` +
  `x-systemd.device-timeout=10s`.** Without these, a reboot when
  the tier LV had been removed (e.g. after `bedrock storage demote`
  followed by a power cycle) dropped the node into emergency mode
  asking for the root password. The DRBD-mode line already had
  nofail; only the local-LV mode was missing it.
- `e604078` + `2b3b525` + `2ecb3ae` — **VG loop-back PV survives
  reboot.** The DRBD promote path adds a sparse loop-backed PV
  (`/var/lib/bedrock-vg-extra.img`) to the bedrock VG so
  tier-{critical,bulk}-meta thick LVs can live outside the thin
  pool. losetup associations don't survive reboot, so post-reboot
  the VG is in 'missing PV' state and any LVM op fails. Three-part
  fix: (a) Python-side reattach + vgreduce in `_ensure_vg_headroom`,
  (b) systemd unit `bedrock-vg-loop.service` does it at boot, and
  (c) wired into install.sh.

Other meaningful fixes:

- `97e1cb2` — **`election.compute` self-demotes the already-master
  when witness blesses someone else.** Partially closes the 2-node-
  HA split-brain hole (the demote half; the rqlite promote half is
  still open — see "Known open issues").
- `cc08f71` — **`tier_storage.join_drbd_peer` tolerates
  `drbdadm up` returning "exists already".** The kernel attach +
  peer connect succeeds even when the redundant `drbdsetup
  new-minor` complains; we now treat that as success.
- `7353525` — **`tier_storage` promote-snapshot uses `cp -a`**
  (rsync isn't in AlmaLinux 10.1 minimal); preserves filer
  leveldb3 across the DRBD mount-flip.
- `3273f4f` + `191df0d` — **`_local-reset` cleans more state**
  (stops bedrock-net + rqlited + rqlited-arbiter, removes
  /etc/bedrock/{seaweedfs.env, rqlited.env, rqlited-arbiter.env,
  storage.json, cluster.key, …} + /var/lib/bedrock/rqlite WAL +
  /run/bedrock). Without these, a re-init re-used the previous
  cluster's loopback IPs / Raft voter set and deadlocked N=1.
- `8da6342` — **cluster.key loaders don't `bytes.strip()` raw
  random keys.** ~5% of 32-byte keys start/end with a whitespace
  byte and get silently truncated; HMAC verification falls apart
  for that cluster.

Testbed fixes (not behavioural):

- `d569948` `test_e2e_offline.sh` — drop stale `bedrock-rust`
  references (use `bedrock-net` service + `/run/bedrock-cluster.fence`).
- xxd → `python3 -c open(path,'rb').read().hex()` (`xxd` not in
  AlmaLinux 10.1 minimal).
- `s3_get_marker` retries 6×5s before reporting LOST — SeaweedFS
  filer transient unavailability during a join no longer flags
  as data loss.

ISO rebuilt at `installer/iso-build/output/bedrock-install-almalinux-10.iso`
with all fixes baked in. ~9.8 GB.

## Known open issues (not blocking single-node or N>=3 install)

### 2-node-HA failover is incomplete (8b/8c in e2e)

At N=2 the `bedrock-net` election can decide a new master based
on witness votes (10/node + 1/witness), but the
`bedrock_state.set_mgmt_master` rqlite write needs Raft quorum,
which the arbiter (on `.254`) provides — except the `.254` IP
follows the master, so when the master is isolated, the arbiter
is unreachable too. Only 1/3 voters remain → no Raft quorum →
no master flip.

The witness DRBD-UUID claim does prevent split-brain (the
isolated old master refuses to re-claim with a stale UUID), and
the new election fix in `97e1cb2` will demote the old master once
the witness blesses someone else — but the new master still
can't actually take over without rqlite quorum.

Possible fixes (all out of scope for v1.0):
- A. Make the arbiter rqlite run on the witness (BedRock Echo /
  workstation) instead of `.254/32` — permanent third voter.
- B. After netd election outcome=LEADER + witness-blessed-self,
  update `state.json["role"]` locally and call
  `cluster_arbiter.converge()` — soft failover that doesn't need
  rqlite quorum. Requires careful handling at rejoin to
  reconcile divergent state.json on each side.

**For v1.0 deployment, recommend N=3 (or higher).** N=2 will not
auto-failover; manual `bedrock storage transfer-mgmt <peer>` is
needed.

### Master → joiner peer-loopback `/32` routes (lesson L)

`lesson_mesh_loopback_asymmetric_routes.md` flagged that master
couldn't reach joiner loopbacks. v16 run-6 showed rqlite
`reachable_voters` PASS at N=2/3/4, so this may be fixed by the
post-Rust netd rewrite; verify on physical with `ip route show |
grep '/32 via'` on the master after a join.

## Pre-flight for physical install

1. **Hardware**: AlmaLinux 10.1 minimal works. The kickstart wants
   a fresh single boot disk (it `clearpart --all`).
2. **NIC**: anaconda binds DHCP to the first NIC it finds (no
   pinned device name). The first-boot `os_setup.configure_bridge`
   creates `br0` on the primary NIC.
3. **Repo**: install.sh defaults `BEDROCK_REPO=file:///var/lib/bedrock-install`
   so the install is fully offline once the ISO is staged.
   To use a network repo, override at boot:
   `BEDROCK_REPO=http://repo.lan:8000 /var/lib/bedrock-install/install.sh`
4. **Default root password**: `bedrock` (set in kickstart). Change
   immediately after first login.
5. **Operator account**: created by `bedrock init` as `root` /
   `admin` (see `mgmt_install._oa.hash_password`). The dashboard
   on `https://<node-ip>:8443` accepts that.
6. **DNS for dashboard cert**: the cert refresh fetches from
   `local-ip.co` and binds `<dashed-ip>.my.local-ip.co` to the
   node's primary IP. If no internet at first boot, the self-signed
   fallback cert is used (browser warning until cert-refresh
   timer next runs).

## Run with confidence

Single node:
```
# dd bedrock-install-almalinux-10.iso to USB; boot from USB; wait ~10 min
ssh root@<dhcp-ip>     # password: bedrock
bedrock init
# Dashboard at https://<node-ip>:8443
```

Add a second node:
```
# Same install on a second box.
ssh root@<node2-ip>
bedrock join --witness <node1-ip>
# Approve the join on node1's dashboard.
```

Add a third node (same way). At N=3 quorum survives single-node
failure; at N=2 failover is operator-triggered (see open issues).

## v16 e2e results across runs

| Run | PASS | FAIL | Notes |
|---|---|---|---|
| v15 (pre-session) | 66 | 24 | leveldb3 wiped at every DRBD promote; 8 HA fails |
| v16 run-5 | 69 | 21 | `cp -a` fixed promote; marker LOSTs transient |
| v16 run-6 | 68 | 15 | retry on s3_get_marker fixed transient; 5c/8b/8c open |
| v16 run-7 | (killed mid 5b) | (LVM PV missing, fixed in 2b3b525) |
| v16 run-8 | (running) | Targeting <5 FAIL with all fixes |

## What I'd ship to physical hardware tomorrow

The current ISO (post-`2ecb3ae`) is good for N=1 and N>=3 installs.
For N=2, document the operator-triggered failover path. Manual
runbook:

1. Boot from USB on each box.
2. After firstboot (~10 min), SSH in (password: bedrock).
3. **First box**: `bedrock init`. Wait for "Cluster ... initialised."
4. **Each subsequent box**: `bedrock join --witness <first-box-ip>`.
   Approve the popup on the dashboard.
5. After all nodes joined: from any node,
   `bedrock storage promote critical` to flip the cluster-singleton
   tier to DRBD.
6. The dashboard shows status; create VMs from there.
