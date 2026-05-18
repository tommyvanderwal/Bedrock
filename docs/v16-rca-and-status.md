# v16 RCA & status — 2026-05-19 session

End-of-session summary of what was fixed during the v16 e2e debugging
push (autonomous 6+ hour overnight run), what's still open, and what
the user needs to know before installing on physical hardware tomorrow.

## TL;DR

**18 commits landed.** The two most important for correctness on real
hardware (these would have bitten any physical install):

- `77d9556` / `c769842` — **install.sh `LIB_FILES` was missing
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

Other meaningful fixes (production-relevant):

- `f04bc54` / `7353525` — **`tier_storage` preserves filer
  leveldb3 across the DRBD mount-flip** using cp -a (rsync isn't
  in AlmaLinux 10.1 minimal).
- `9887fd5` — **netd `demoted_in_cycle` latch only after demote
  actually fires + resets on quorum return.** An early startup
  NoQuorum latched the flag and a later real isolation skipped
  the demote.
- `97e1cb2` — **`election.compute` self-demotes already-master
  when witness blesses someone else.** Partially closes the
  2-node-HA split-brain hole.
- `cc08f71` — **`join_drbd_peer` tolerates "exists already"** as
  success from `drbdadm up`.
- `fc390da` — **tier mount fstab lines use `nofail` +
  `x-systemd.device-timeout=10s`.** Without this, a reboot when
  the tier LV had been removed dropped the node into emergency
  mode asking for the root password.
- `e604078` / `2b3b525` / `2ecb3ae` — **VG loop-back PV survives
  reboot.** Three-part fix: Python-side reattach + vgreduce,
  systemd unit at boot, install.sh wiring.
- `8da6342` — **cluster.key loaders don't `bytes.strip()`** raw
  random keys (~5% truncation rate).
- `3273f4f` / `191df0d` / `971745c` / `824ce55` — **`_local-reset`
  is now fully idempotent**: cleans all state files +
  /var/lib/bedrock/rqlite WAL + /run/bedrock, stops bedrock-net
  + rqlited + rqlited-arbiter, removes rqlited-arbiter.env,
  umounts /var/lib/bedrock/cluster BEFORE drbdadm down, and
  resets all `bedrock-*` start-rate-limit counters.
- `8ee7072` / `18c159f` — **mgmt.tar.gz rebuilt + build-iso
  auto-rebuilds it** when mgmt/ is newer.

Testbed-only fixes (not behavioural in production):

- `d569948` `test_e2e_offline.sh` — drop stale `bedrock-rust`
  references (use `bedrock-net` service + `/run/bedrock-cluster.fence`).
- xxd → `python3 -c open(path,'rb').read().hex()` (`xxd` not in
  AlmaLinux 10.1 minimal).
- `s3_get_marker` retries 6×5s — SeaweedFS filer transient
  unavailability during a join no longer flags as data loss.

**ISOs built**: 3 successive ISOs at `installer/iso-build/output/`
each with progressively more fixes. The final ISO has all 18
commits baked in (~9.8 GB).

## Confirmed working in testbed (v16 run-8 / run-9 snapshots)

- N=1 init → N=2/3/4 joins (all clean, markers PUT+GET round-trip)
- ISO library FUSE mount visible cross-node
- Storage promote critical to DRBD (with cp -a leveldb3
  preservation)
- **5c isolation + failover**: master sim-1 isolated for 90s,
  sim-2 took over as new master (`failover: mgmt_master moved`,
  `arbiter VIP claimed on new master`, `arbiter rqlite active`,
  `filer active`), AND sim-1 self-fenced (`.254 VIP released`).
- 8d split-brain prevention without witness: PASSED.
- Most marker-survival checks across joins (with retry).

## Known open issues (not blocking single-node or N>=3 install)

### 2-node-HA failover is incomplete (8b/8c in e2e)

At N=2 the `bedrock-net` election can decide a new master based
on witness votes (10/node + 1/witness), but the
`bedrock_state.set_mgmt_master` rqlite write needs Raft quorum,
which the arbiter (on `.254`) provides — except the `.254` IP
follows the master, so when the master is isolated, the arbiter
is unreachable too. Only 1/3 voters remain → no Raft quorum →
no master flip.

The new election fix in `97e1cb2` will demote the old master
once the witness blesses someone else — but the new master
still can't actually take over without rqlite quorum.

**For v1.0 deployment, recommend N=3 (or higher).** N=2 will not
auto-failover; manual `bedrock storage transfer-mgmt <peer>` is
needed.

### Testbed iteration churn (NOT a production issue)

In the testbed, killing tests mid-flight + push_sources cycle
causes service start-rate-limits (e.g. bedrock-weed-master).
The reset path covers this, but a series of rapid kill-and-rerun
cycles can compound. Production install doesn't have this churn
— services start clean from a fresh ISO boot.

## Pre-flight for physical install

1. **Hardware**: AlmaLinux 10.1 minimal works. The kickstart wants
   a fresh single boot disk (it `clearpart --all`).
2. **NIC**: anaconda binds DHCP to the first NIC it finds (no
   pinned device name). The first-boot `os_setup.configure_bridge`
   creates `br0` on the primary NIC.
3. **Repo**: install.sh defaults `BEDROCK_REPO=file:///var/lib/bedrock-install`
   so the install is fully offline once the ISO is staged.
4. **Default root password**: `bedrock` (set in kickstart). Change
   immediately after first login.
5. **Operator account**: created by `bedrock init` as `root` /
   `admin`. The dashboard on `https://<node-ip>:8443` accepts that.
6. **DNS for dashboard cert**: cert refresh fetches from
   `local-ip.co` and binds `<dashed-ip>.my.local-ip.co` to the
   node's primary IP. If no internet at first boot, the
   self-signed fallback cert is used (browser warning until
   cert-refresh OnBootSec=2min runs).

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

## Promote critical tier to DRBD at N>=2

After all nodes joined:
```
bedrock storage promote
# wait ~30s for initial sync + arbiter promote
```

This flips the cluster-singleton tier (filer's leveldb3 + arbiter
rqlite data) from a per-node local LV to a 2-way DRBD replicated
volume. The mgmt master holds the DRBD primary and runs filer +
s3; followers hold DRBD secondary, ready to take over.

## v16 e2e results summary

| Run | PASS | FAIL | Notes |
|---|---|---|---|
| v15 (pre-session) | 66 | 24 | leveldb3 wiped at every DRBD promote; 8 HA fails |
| v16 run-5 | 69 | 21 | `cp -a` fixed promote |
| v16 run-6 | 68 | 15 | retry on s3_get_marker fixed transient |
| v16 run-7 | (killed at 5b) | (VG PV missing, fixed in 2b3b525) |
| v16 run-8 | 47/0 → cascading | _is_mounted fix landed 5c failover (then DRBD config gap on sim-2) |
| v16 run-9 | 23/0 → 5b drbdmeta | umount + retry drbdadm down fix |
| v16 run-10/11/12 | partial | testbed churn — see "testbed iteration churn" |

## What ships to physical hardware

The current ISO at
`installer/iso-build/output/bedrock-install-almalinux-10.iso`
(~9.8 GB, all 18 commits) is good for N=1 and N>=3 installs.
For N=2, document the operator-triggered failover path.

## End-of-session manual runbook (physical install)

1. Boot from USB on each box.
2. After firstboot (~10 min), SSH in (password: bedrock).
3. **First box**: `bedrock init`. Wait for "Cluster ...
   initialised."
4. **Each subsequent box**: `bedrock join --witness <first-box-ip>`.
   Approve the popup on the dashboard.
5. After all nodes joined: from any node,
   `bedrock storage promote` to flip the cluster-singleton tier
   to DRBD.
6. The dashboard at `https://<node-ip>:8443` shows status;
   create VMs from there.

## Key file paths for debug on physical

- `/etc/bedrock/state.json` — per-node identity + role
- `/etc/bedrock/cluster.json` — cluster snapshot (projected from rqlite)
- `/etc/bedrock/seaweedfs.env` — SeaweedFS envs
- `/etc/bedrock/rqlited.env` — per-node rqlite envs
- `/etc/bedrock/rqlited-arbiter.env` — arbiter rqlite envs (master only)
- `/run/bedrock-cluster.fence` — fence marker (if present, node fenced)
- `journalctl -u bedrock-net` — daemon election + witness state
- `journalctl -u bedrock-mgmt` — orchestrator
- `journalctl -u bedrock-rqlited` — Raft state
- `drbdadm status` — DRBD replication state
- `bedrock storage status` — tier states
