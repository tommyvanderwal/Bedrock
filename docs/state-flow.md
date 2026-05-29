# Bedrock state flow

> Audience: operator/reviewer who wants to understand what each node
> does in each cluster state, what triggers transitions, and what the
> failure modes look like — in one read.

Each node runs one daemon — **`bedrock-d`** — plus a per-node rqlite
voter. `bedrock-d` has three responsibilities:

1. **netd thread** — mesh routing, witness heartbeat, weighted-vote
   election, NoQuorum self-demote, `.254` arbiter actuation. Reads
   rqlite (`cluster_state.load_cluster()`, level `none`) + `state.json`;
   writes routes, `/run/bedrock-no-quorum`, and (on a Leader outcome)
   drives `cluster_arbiter.promote_to_arbiter_host()`.
2. **`bedrock-rqlited`** — per-node rqlite voter holding the cluster's
   source-of-truth tables (`nodes`, `vms`, `drbd_resources`,
   `cluster_info`, `tiers`, `paths`, `witnesses`, `operators`,
   `backup_targets`, …), mTLS HTTPS on 4001/Raft 4002. On the master,
   `bedrock-rqlited-arbiter` runs additionally on the `.254` VIP
   (4011/4012) as a cluster-level Raft voter that survives master death.
3. **mgmt/orchestrator asyncio** (inside `bedrock-d`) — FastAPI +
   orchestrator. Subscribes to rqlite revisions, projects this node's
   role to `state.json`, drives `cluster_arbiter.converge()`, hosts the
   dashboard, serves `/api/*` (8443 LAN, 8001 loopback).

Cluster topology and state live only in rqlite. The one local
cluster-related file is `/etc/bedrock/state.json` (this node's identity
+ derived role + cold-boot recovery fields). A second local file,
`/etc/bedrock/cluster.json`, is a bootstrap-only peer list: it holds the
rqlite peer addresses so `rqlite_setup --render-env` can configure
rqlited at boot before rqlite is up to report its own peers. It is not a
runtime state projection.

Two storage layers:

- **DRBD** on the `cluster` singleton resource — mounted at
  `/var/lib/bedrock/cluster` on the master, holds the arbiter rqlite
  data, the SeaweedFS filer's leveldb3 metadata, and the S3 IAM db.
  Synchronous (protocol C). Moves with the master role.
- **SeaweedFS** — `weed-volume` + `weed-s3` run on every node;
  `weed-master` runs on a deterministic odd subset (Raft needs an odd
  peer count: 3 lowest-octet nodes at N>=3, 1 at N<=2). The filer + its
  s3 run only on the master, on the DRBD-replicated
  `/var/lib/bedrock/cluster/seaweedfs`.

The rest of this doc walks each cluster state.

---

## State N=1 — single-node cluster after `bedrock init`

```
+------------------------------------- node1 (mgmt+compute) -----------------------------------+
|                                                                                              |
|  bedrock-d                                                                                   |
|   netd thread          bedrock-rqlited (4001/4002)         mgmt asyncio (8443 + 8001)        |
|   - mesh probes        - sole Raft voter, self-elected      - rqlite_subscriber             |
|   - no peers           - cluster_info.mgmt_master = node1   - converge_retry (5s)           |
|   - election -> Leader - schema seeded                      - boot_orchestrator one-shot    |
|                                                                                              |
|  SeaweedFS:             weed-master       weed-volume       weed-filer       weed-s3         |
|                         (single Raft)     (local LV)         (leveldb3)       (.254)         |
|                                                                                              |
|  Per-resource storage (one thinpool per node):                                               |
|    weed-volume LV    ->  SeaweedFS volume dir              (local thin LV, no DRBD)          |
|    per-VM disks      ->  cattle local LV / pet 2-way / vipet 3-way DRBD (owned by sagas)     |
|                                                                                              |
|  Cluster singleton:     /var/lib/bedrock/cluster          (regular dir on root FS)          |
|                            +-- seaweedfs/      filer leveldb3 + S3 IAM                       |
|                            +-- rqlite/         (created; arbiter rqlite only used at N>=2)   |
+----------------------------------------------------------------------------------------------+
```

**Election outcome:** Leader, `should_set_mgmt_master=False` (already
master).

**N=1 specifics:**

- The per-node rqlite on 4001/4002 is the only voter, self-elected.
  `bedrock-rqlited-arbiter` on 4011/4012 floats with the master VIP and
  is only meaningful at N>=2; at N=1 there is no `cluster` DRBD resource
  so the arbiter rqlite is not started — singletons run on the local FS.
- The cluster `/24` prefix is derived from `cluster_uuid` (see
  `cluster_addr.py`). The node's loopback `.1/32` is on `lo`; the master
  VIP `.254/32` is bound on `lo` at every N (including N=1) so client
  config — filer URL, mgmt URL — is identical from day one.

---

## Transition N=1 -> N=2 — `bedrock join` (node_join saga)

The joiner runs the `node_join` saga (`bedrock_d/install/node_join.py`),
crash-resumable, every step idempotent. The load-bearing ordering:

1. **request_join_approval** — POST the master's mgmt API; the master
   returns a per-join challenge, the joiner signs it with its Ed25519
   key, the operator approves (dashboard `/api/join/approve` or
   `bedrock node approve <id>`). The approval response carries the
   cluster key sealed to the joiner; the joiner decrypts it to
   `/etc/bedrock/cluster.key` (0600) and writes node/CA certs for mTLS.
2. **write_state_json** — cluster_uuid + node_name + loopback_ip + role.
3. **write_bootstrap_cluster_json** — the rqlite peer list bootstrap
   file.
4. **provision_storage_n1** — `tier_storage.setup_n1()`: thinpool,
   weed-volume LV, local cluster-singleton dir.
5. **start_bedrock_d** — netd claims loopback `/32` on `lo` and begins
   probing the mesh + mgmt interfaces. (bedrock-d starts first; rqlited
   `Requires=bedrock-d.service`.)
6. **start_rqlited_joiner** — `bedrock-rqlited` with `-join` at the
   master; rqlite Raft adds the joiner as a Voter.
7. **install_dashboard** — joiners also serve the dashboard.
8. **seaweedfs** install/configs/start — `weed-master` (only if in the
   odd Raft subset; at N=2 only the lowest-octet node runs it),
   `weed-volume` + `weed-s3` on this node.
9. **cluster_tier_join_peer** — at N>=2, waits for the master to flip
   `tiers.cluster.mode` to `drbd` in rqlite, then joins the singleton
   DRBD as Secondary (see next section). No-op at N=1.

**Election once the joiner has probed back:** the master sees the joiner
in `ever_seen_peers` and its election heartbeat -> `n_nodes=2`,
`total_votes=200` (no witness), `majority=101`. Steady-state quorum uses
reachability, so the master keeps Leader at 200 votes; the joiner is a
stable Follower.

**Why a mid-join node can't tip the master to NoQuorum:** the election
denominator counts only nodes whose rqlite `nodes` row is `active`. A
joiner is in the `joining` lifecycle state until its saga calls
`node_set_active`, so it is excluded from `n_nodes` entirely — the
master never sees `n_nodes=2, votes=100 < 101` mid-join. (Liveness is
never used for the denominator, only for reachability and the ack
tally.)

---

## Transition N=2 (and N=3,4,…) -> cluster-singleton DRBD

Operator runs `bedrock storage promote` on the master. The CLI:

1. Calls `tier_storage.transition_to_n2_master(self_lo, peer)`, which
   runs `promote_local_to_drbd_master` on the master:
   - Stop `bedrock-weed-s3`, `bedrock-weed-filer`,
     `bedrock-rqlited-arbiter` so the singleton dir is quiescent.
   - Create the data + thin external-meta LV pair
     (`bedrock-data-cluster` / `bedrock-meta-cluster`, `--max-peers=7`).
   - `cp -a` snapshot `/var/lib/bedrock/cluster/` to
     `/var/lib/bedrock-promote-snapshot/` so the filer leveldb3 +
     arbiter rqlite data survive the DRBD device replacing the directory.
   - `drbdadm create-md` + `up` + `primary --force`. DRBD tuning:
     `resync-rate 100M`, `c-min-rate 0`, `c-plan-ahead 0` so initial
     sync isn't throttled under app I/O.
   - mkfs.xfs + mount `/dev/drbd1101` at `/var/lib/bedrock/cluster`.
   - `cp -a` restore the snapshot into the mounted DRBD volume; `sync`.
   - fstab line; restart the three singleton units.
   - Write `cluster-info` tier state `mode=drbd` and the local
     `/etc/bedrock/cluster-drbd-ready` marker (releases the
     election-driven arbiter promote to take over).
2. SSH the peer: `bedrock storage _peer-promote` ->
   `transition_to_n2_peer` -> `join_drbd_peer` creates the peer's LV
   pair and brings it up as Secondary; initial sync starts.
3. Wait up to **180 s** for `drbdadm status cluster` to show two
   `disk:UpToDate` and no `Sync` activity, then `sync` to flush filer
   leveldb3 buffers.

On the next subscriber tick `cluster_arbiter.converge()` sees the
`cluster-drbd-ready` marker and runs the N>=2 path (DRBD primary +
arbiter rqlite on `.254` + filer + s3). Further joins (N=3, N=4…) join
the singleton via `cluster_tier_join_peer` on the joiner side, capped at
3-way by `cap_singleton_peers` (lowest-octet nodes).

---

## State N>=2 healthy (after promote)

```
+-------------------------- node1 (current master) ---------------------------+
|                                                                            |
|  bedrock-d                                                                 |
|   netd thread: Leader  bedrock-rqlited (per-node, 4001/4002)              |
|                        bedrock-rqlited-arbiter (4011/4012 on .254/32)     |
|                                                                            |
|   mgmt asyncio         8443 HTTPS + 8001 loopback, subscribed to rqlite   |
|                                                                            |
|  cluster_arbiter:      drbdadm primary cluster                            |
|                        mount /dev/drbd1101 -> /var/lib/bedrock/cluster     |
|                        ip addr add 100.X.Y.254/32 dev lo                  |
|                        bedrock-weed-filer + bedrock-weed-s3 (.254)        |
|                                                                            |
|  routes (kernel):      100.X.Y.{2,3,4}/32 metric 10, multipath via mesh   |
+----------------------------------------------------------------------------+
                                  ^   ^   ^
         3-way DRBD on the `cluster` singleton (master + 2 secondaries)
         per-node rqlite Raft (master + N-1 voters)
                                  |   |   |
+--- node2 (follower) ------+  +--- node3 (follower) ------+  +--- node4 (follower) -----+
|  netd thread: Follower    |  |  netd thread: Follower     |  |  netd thread: Follower   |
|  per-node rqlite voter    |  |  per-node rqlite voter     |  |  per-node rqlite voter   |
|  DRBD Secondary cluster   |  |  DRBD Secondary cluster    |  |  weed-volume + weed-s3   |
|  weed-master (Raft-3 set) |  |  weed-master (Raft-3 set)  |  |  (singleton capped 3-way |
|  weed-volume + weed-s3    |  |  weed-volume + weed-s3     |  |   + master subset 3-of-4)|
+---------------------------+  +----------------------------+  +--------------------------+
```

**Election math (no witness):** `total = 100*N_active`,
`majority = total//2 + 1`. A master keeps Leader while the nodes it can
reach total `>= majority`.
- N=2: total 200, majority 101 -> master (self + peer = 200) Leader.
- N=4: total 400, majority 201 -> master (self + 3 = 400) Leader.

**With a witness:** each valid+confirmed witness adds 1 to the tally and
each configured witness adds 1 to `total`. N=2 + 1 witness: total 201,
majority 101 — same healthy outcome.

---

## Transition: isolation of the master

The master is cut off from the cluster mesh (iptables DROP, mesh NIC
down, cable pull). Constants below are from `netd.py` /
`election.py` / `witness.py`.

### On the isolated master

1. Probes go unanswered; each neighbour's `last_seen` ages.
2. After `DOWN_HYSTERESIS_S = 10` s of silence, `sweep_hysteresis`
   emits a `down` event and drops the neighbour from `d.neighbours`.
3. The peer is still in `ever_seen_peers` (so it counts in `n_nodes`)
   but `peer_liveness[peer]` is now False, and its election heartbeat
   has gone stale.
4. The master's reachable-vote tally drops below `majority` ->
   `Outcome.NO_QUORUM`.
5. A single self-demote counter (`noquorum_master_ticks`) rides out the
   ~5 s fresh-daemon startup window where neighbours=0 looks like
   NoQuorum.
6. On the `SELF_DEMOTE_MISSES = 9`th consecutive NoQuorum tick (~9 s,
   one second before a survivor promotes at `MASTER_LOSS_MISSES = 10`,
   so `.254` is never on two nodes at once):
   - `election.set_no_quorum_marker()` writes `/run/bedrock-no-quorum`.
   - netd calls `cluster_arbiter.demote_arbiter_host()` directly (it
     can't rely on the orchestrator's converge — rqlite is unreachable
     in NoQuorum):
     stop `bedrock-weed-s3` + filer, release `.254/32` from `lo`, stop
     `bedrock-rqlited-arbiter`, `umount /var/lib/bedrock/cluster`,
     `drbdadm secondary cluster`, and clear our witness LMS bit.
     A `demoted_in_cycle` latch fires this once per NoQuorum episode.
7. The mgmt asyncio's `no_quorum_responder` sees the marker and pauses
   running pet/vipet VMs (so qemu stops writing to a now-secondary DRBD
   device), then waits for `_wait_for_role` to settle before clearing
   the marker.

### On the surviving partition (N-1 nodes)

1. Each survivor's netd sees the master's heartbeat go stale; after
   `MASTER_LOSS_MISSES = 10` missed beats (~10 s) it treats the master
   as gone.
2. Election then runs the failover branch: node-votes = self + each peer
   that has **acked** us (an ack is active — a peer only acks once it
   too has lost the master and found our advertised arbiter-DRBD UUID
   eligible against its own 7-day UUID history). The lowest-octet
   eligible contender proposes; peers defer one tick and ack it.
3. The winner's outcome is Leader with `should_set_mgmt_master=True`.
   netd drives `cluster_arbiter.promote_to_arbiter_host()`:
   - Run the witness takeover protocol (steps 1-5) — no rqlite on this
     path; rqlite is the service being recovered.
   - `drbdadm primary cluster`; if the old master is unreachable DRBD
     refuses ("Need access to UpToDate data"), so retry with
     `drbdadm -- --force primary` (election authority + witness UUID
     claim already decided this side wins).
   - mount `/var/lib/bedrock/cluster`, `ip addr add .254/32 dev lo`,
     render arbiter env, start `bedrock-rqlited-arbiter`,
     `seaweedfs.promote_to_filer_host()` (filer + s3).
   - `_set_mgmt_master_after_promote` writes `cluster_info.mgmt_master`
     to rqlite as a RESULT, only after `arbiter_status()` confirms DRBD
     Primary + `.254` + arbiter service are up.
4. Every node's `rqlite_subscriber` sees the revision advance and
   projects the new master's role to `state.json`.

### The witness, briefly

BedRock Echo is a passive per-node K/V slot store (UDP 12321,
ChaCha20-Poly1305 over msgpack). Each node owns exactly one slot (key =
loopback last octet; 254 reserved) and writes only its own. The slot
marker is the node's `cluster` DRBD current-UUID; a `tag.lms` bit marks
last-man-standing and never times out. The witness has no concept of a
"blessed master": before a takeover, the survivor reads the old master's
slot and checks it locally — stale (`SLOT_STALE_MS = 10000`, ~10 s),
`tag.lms=0`, and its marker exactly equals the survivor's local DRBD
UUID. Reachability for the +1 vote uses `WITNESS_FRESHNESS_S = 12` s.
The DRBD UUID is read from DRBD9 debugfs `data_gen_id` (with `dump-md`
fallback for a detached resource). Full protocol:
`docs/cluster-quorum-spec.md`.

**End state during isolation:** the isolated old master holds none of
the singletons; the new master serves filer/s3 at `.254`. From the
operator's workstation (on a different bridge than the cluster mesh),
`https://<new-master-mgmt-ip>:8443` works normally. End-to-end:
pet/vipet VMs on the isolated side suspend ~T+20 s, the survivor takes
over ~T+35 s, no split-brain on rejoin.

---

## Transition: master rejoins after isolation

1. Isolation is lifted (iptables flush, NICs back up).
2. The old master's netd re-discovers peers within 1-2 probe intervals.
3. Election sees `current_mgmt_master = <new master>` and that master is
   reachable -> Follower. `should_set_mgmt_master=False`.
   `noquorum_master_ticks` and `demoted_in_cycle` reset so a future
   NoQuorum can act again.
4. The takeover steal-back guard (`_peer_claims_master_now`) refuses any
   promote while a peer's fresh heartbeat advertises itself as master.
   Even on the witness path, the new master's slot is fresh (not stale)
   and may carry `tag.lms=1` -> takeover refuses. Old master stays
   Follower (`cluster-quorum-spec.md` Scenario B).
5. The old master's per-node rqlite rejoins Raft (already a Voter; it
   catches up the log).
6. `no_quorum_responder` waits for `_wait_for_role` to return
   `follower`, then clears the marker and runs `_reconcile_paused_vms`:
   per paused VM, if rqlite now says it lives on another host
   `virsh destroy` the stale local copy + `drbdadm secondary` its per-VM
   resource; if it still says ours, `virsh resume`.

**Why the witness DRBD-UUID claim matters:** without it a flapping link
could let the old master re-attach with stale leveldb3 and re-claim
primacy. The new master's slot records its current DRBD UUID; an old
master claiming with an older UUID fails the exact-equality check while
the slot is fresh, so takeover refuses.

---

## State: 2-node HA with a witness

Half-quorum on either side needs the witness to break the tie.

### Healthy

- total = 200 + 1 = 201, majority = 101.
- Each node sees self (100) + peer (100) + witness (1) = 201 >= 101.
  Master Leader, peer Follower.

### Master isolated, witness on the follower's side

- Master sees self (100); the witness is unreachable too (the partition
  cut it off the LAN). 100 < 101 -> NoQuorum -> self-demote `.254` /
  arbiter / filer at `SELF_DEMOTE_MISSES` (~9 s).
- Follower sees self (100) + witness (1) = 101 >= 101 -> Leader.
  Promotes the singleton, writes its witness slot. Cluster runs on the
  follower.

### Witness absent

- Both sides see 100/101 -> both NoQuorum, both self-demote. No master,
  no singleton: service down, data safe. Operator intervenes once the
  partition is fixed.

A configured-but-unreachable/invalid witness still counts in `total`
(raising `majority`) but adds 0 to `my_votes`, biasing the cluster
toward "do not fail over" — safety over availability.

---

## Scale-down — `bedrock node leave <target>` (node_leave saga)

Run from a node that is NOT the target; it executes on the master as the
crash-resumable `node_leave` saga (`bedrock_d/install/node_leave.py`):

1. **validate_target** — look up target in the snapshot; refuse
   leave-from-self; treat already-gone as a no-op.
2. **rqlite_node_unregister** — master appends `node_unregister(target)`;
   Raft replicates.
3. **rqlite_voter_remove** — `DELETE /remove` on rqlite (id = target's
   loopback last octet) to drop its Voter slot, so consecutive leaves
   don't brick quorum at N/2 voters.
4. **propagate_daemon_config** — bump the rqlite revision; each node's
   subscriber regenerates its own daemon config.
5. **stop_remote_services** — best-effort SSH to the target:
   `systemctl stop bedrock-d bedrock-rqlited bedrock-rqlited-arbiter;
   rm -f /run/bedrock-no-quorum`. Failure is non-fatal — the witness
   slot ages out and the node stops heartbeating.
6. **verify_membership_drop** — read back the snapshot (`level=strong`)
   until the target is gone.

Once the target is out of the rqlite `nodes` table it leaves
`ever_seen_peers` math too, so survivors' quorum stays correct.
Re-shuffling the `cluster` DRBD 3-peer set when the leaver carried it is
left to the orchestrator's reconcile, not this saga.

---

## Boot recovery — node reboots while the cluster is up

1. `bedrock-d`, `bedrock-rqlited`, `bedrock-mdns`, `bedrock-redirect`
   auto-start at `multi-user.target`. (The `weed-*` and
   `bedrock-rqlited-arbiter` units are role-aware: started by the
   orchestrator / `cluster_arbiter`, not by a systemd target.)
2. netd reads `/etc/bedrock/state.json` for cluster_uuid + node_name +
   loopback_ip; claims `.<i>/32` on `lo` (idempotent).
3. `bedrock-rqlited` rejoins the existing Raft. Its node-id is the
   loopback's last octet — permanent across restarts.
4. The orchestrator subscribes to rqlite and projects this node's role
   to `state.json`. `boot_orchestrator` -> `_start_local_services`
   (idempotent, role-aware) brings up per-VM DRBD, per-node SeaweedFS
   (`weed-volume` + `weed-s3`, plus `weed-master` if in the Raft-3 set),
   libvirtd, and this node's VMs. `cluster_arbiter.converge()` promotes
   the singletons if netd's election says Leader, else demotes (no-op
   when already not hosting).
5. `no_quorum_responder` is idle (no marker on a normal boot).
6. `_reconcile_paused_vms` runs once `_wait_for_role` settles.

---

## Ports

8443 HTTPS (dashboard + LAN mgmt API, operator/peer-authed) |
127.0.0.1:8001 HTTP (local CLI, auth-exempt) |
4001/4002 per-node rqlite (mTLS) | 4011/4012 arbiter rqlite |
SeaweedFS master 9333, volume 8080, filer 8888, s3 8333 |
VictoriaMetrics 8428, VictoriaLogs 9428 (+ syslog 5140) |
node-exporter 9100, vm-exporter 9177 | Cockpit 9090 |
mesh UDP probe 7732 / advert 7733 / heartbeat 7734 + ICMP (HMAC-SHA256) |
witness UDP 12321 (AEAD) | DRBD 7700-7799 | VNC 5900+ |
live-migrate 49152+. Port 80 redirects to 8443.

---

## What can go wrong (and where to look)

| Symptom                                  | Likely cause                                 | Where to look                                |
|------------------------------------------|----------------------------------------------|----------------------------------------------|
| `rqlite POST … Connection refused`       | per-node rqlited not started                 | `systemctl status bedrock-rqlited`           |
| Master sees `n_nodes=N` but `votes=100`  | peers unacked / heartbeats stale             | `journalctl -u bedrock-d | grep election`    |
| Filer not active on master post-promote  | leveldb3 hidden under DRBD mount             | `ls -la /var/lib/bedrock/cluster/seaweedfs/` |
| S3 marker lost after failover            | leveldb3 didn't sync to new master           | snapshot restore in `promote_local_to_drbd_master` |
| `.254` not released within ~9 s          | `DOWN_HYSTERESIS_S` / `SELF_DEMOTE_MISSES`   | netd log: `election leader -> noquorum`      |
| Master flaps leader<->noquorum           | `noquorum_master_ticks` / `demoted_in_cycle` | netd log: those fields                        |
| `drbdadm primary` fails post-failover    | Secondary thinks data not UpToDate           | `cluster_arbiter._drbd_promote` retries `--force` |
| `Only odd number of masters supported`   | weed Raft refused an even master subset      | `seaweedfs.write_master_config` subset rule  |
| Joiner sees stale IP on master           | `get_mgmt_ip` ARP-cache miss + bad fallback  | spawn.py ARP sweep + `ip neigh`              |
| `bedrock storage promote` exits early    | DRBD initial sync took >180 s                | promote-wait loop in `bedrock` action=promote |

---

## Key file paths

- `/etc/bedrock/state.json` — this node's identity + derived role +
  master URL. The only local runtime-state file; survives cold boot
  without rqlite.
- `/etc/bedrock/cluster.json` — bootstrap-only rqlite peer list, read by
  `rqlite_setup --render-env` at boot.
- `/etc/bedrock/cluster.key` — 32 raw bytes shared cluster-wide; keys
  the mesh HMAC-SHA256 (probes/adverts) AND the witness
  ChaCha20-Poly1305 AEAD slots.
- `/etc/bedrock/cluster-drbd-ready` — marker that the N=1->N=2 singleton
  DRBD transition is complete; gates the election-driven arbiter promote.
- `/etc/bedrock/installer.env` — `BEDROCK_REPO=file:///…` for
  offline-install re-runs.
- `/run/bedrock-no-quorum` — NoQuorum marker.
- `/var/lib/bedrock/seaweedfs/volumes` — local weed-volume LV mount.
- `/var/lib/bedrock/cluster` — cluster-singleton root. A regular dir at
  N=1; the DRBD-replicated XFS mount at N>=2.
- `/var/lib/bedrock/rqlite` — per-node rqlite data dir.
- `/var/lib/bedrock-install` — install.sh staging from the ISO firstboot
  payload.
