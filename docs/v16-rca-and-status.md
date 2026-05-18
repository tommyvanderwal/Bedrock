# v16 RCA & status — 2026-05-19 session

End-of-session summary of what was fixed during the v16 e2e debugging
push, what's still open, and what the user needs to know before
installing on physical hardware tomorrow.

## TL;DR

Six commits landed against `master`. Two of them are critical for
correctness on real hardware:

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

Other fixes:

- `7353525` `tier_storage` — promote-snapshot uses `cp -a` (rsync
  isn't in AlmaLinux 10.1 minimal); preserves filer leveldb3
  across the DRBD mount-flip.
- `3273f4f` `_local-reset` — also stops bedrock-net + rqlited +
  rqlited-arbiter; removes /etc/bedrock/{seaweedfs.env,
  rqlited.env, storage.json, …} + /var/lib/bedrock/rqlite WAL +
  /run/bedrock. Without these, a re-init reused the previous
  cluster's loopback IPs and Raft voter set, deadlocking N=1.
- `8da6342` `cluster.key` loaders — don't `bytes.strip()` raw
  random keys (~5% of 32-byte keys start/end with a whitespace
  byte and get silently truncated; HMAC verification falls apart).
- `cc08f71` `join_drbd_peer` — tolerate "exists already" as
  success from `drbdadm up`. The kernel attach + peer connect
  succeed even when the redundant `drbdsetup new-minor` complains.

Testbed fixes (not behavioural):

- `d569948` `test_e2e_offline.sh` — drop stale `bedrock-rust`
  references (use `bedrock-net` service + `/run/bedrock-cluster.fence`).
- Test now uses `python3 -c open(path,'rb').read().hex()` instead
  of `xxd -p` (`xxd` not in AlmaLinux 10.1 minimal).
- `s3_get_marker` retries 6×5s before reporting LOST — SeaweedFS
  filer transient unavailability during a join no longer flags
  as data loss.

ISO rebuilt at `installer/iso-build/output/bedrock-install-almalinux-10.iso`
with all fixes baked in. ~9.8 GB. Tommy can `dd` it to USB for
the physical-hardware install.

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
isolated old master refuses to re-claim with a stale UUID), but
the post-isolation new master can't actually become master in
rqlite's view.

Possible fixes (all out of scope for v1.0):
- A. Make the arbiter rqlite run on the witness (BedRock Echo /
  workstation) instead of `.254/32` — permanent third voter.
- B. Have netd, after election outcome=LEADER + witness-blessed-
  self, update `state.json["role"]` locally and call
  `cluster_arbiter.converge()` — soft failover that doesn't need
  rqlite quorum. Requires careful handling at rejoin to
  reconcile divergent state.json on each side.

**For v1.0 deployment, recommend N=3 (or higher).** N=2 will not
auto-failover; manual `bedrock storage transfer-mgmt <peer>` is
needed.

### Master → joiner peer-loopback `/32` routes (lesson L from memory)

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
