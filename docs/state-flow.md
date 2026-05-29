# Bedrock state flow

> Audience: operator/reviewer who wants to understand what each node
> does in each cluster state, what triggers transitions, and what
> the failure modes look like — in one read, ~20 minutes.

Everything runs inside one unified daemon — **`bedrock-d`** — plus a
per-node rqlite voter. Logically there are three responsibilities:

1. **netd thread** (inside `bedrock-d`) — owns mesh routing,
   witness heartbeat, weighted-vote election, NoQuorum self-demote,
   and the `.254` arbiter actuation. Reads rqlite (via
   `cluster_state.load_cluster()`) + `state.json`; writes routes,
   `/run/bedrock-no-quorum`, and (on election win)
   `cluster_info.mgmt_master` in rqlite.
2. **`bedrock-rqlited`** — per-node rqlite voter holding the
   cluster's source-of-truth tables (nodes, vms, drbd_resources,
   cluster_info, operators, witnesses, backup_targets…). On the
   master, `bedrock-rqlited-arbiter` runs additionally on the .254
   VIP for the cluster-level Raft term that survives master death.
3. **mgmt/orchestrator asyncio** (inside `bedrock-d`) — FastAPI +
   orchestrator. Subscribes to rqlite revisions, projects this
   node's role to `state.json`, drives `cluster_arbiter.converge()`,
   hosts the dashboard, serves `/api/*`.

(There is no `bedrock-rust`, no per-daemon split, and no
`cluster.json` — cluster topology lives only in rqlite, read via
`cluster_state.load_cluster()` at level `none` so it works even
without quorum.)

Two storage layers under the hood:

- **DRBD** on the `cluster` singleton resource — mounted at
  `/var/lib/bedrock/cluster` on the master, holds the filer's
  leveldb metadata + the arbiter's rqlite data. Synchronous
  replication. Moves with the master role.
- **SeaweedFS** — master + volume run on an odd subset of nodes
  (Raft requires odd peer count). Filer + S3 gateway run only on
  the master, on the DRBD-replicated `/var/lib/bedrock/cluster/seaweedfs`.

The rest of this doc walks through every state the cluster can be
in and how each piece behaves.

---

## State N=1 — single-node cluster after `bedrock init`

```
┌─────────────────────────────────── node1 (mgmt+compute) ──────────────────────────────────┐
│                                                                                           │
│  bedrock-d                                                                                 │
│   netd thread          bedrock-rqlited (4001/4002)         mgmt asyncio (8443 + 8001)     │
│   - mesh probes        - sole Raft voter, self-elected      - rqlite_subscriber          │
│   - no peers           - cluster_info.mgmt_master = node1   - converge_retry (5s)        │
│   - election → Leader  - schema seeded                      - boot_orchestrator one-shot │
│                                                                                           │
│  SeaweedFS:             weed-master       weed-volume       weed-filer       weed-s3      │
│                         (single Raft)     (local LV)         (leveldb)        (anonymous) │
│                                                                                           │
│  Per-resource storage (one thinpool per node):                                            │
│    weed-volume LV    →  SeaweedFS volume dir              (local thin LV, no DRBD)         │
│    per-VM disks      →  cattle local LV / pet 2-way / vipet 3-way DRBD (owned by sagas)    │
│                                                                                           │
│  Cluster singleton:     /var/lib/bedrock/cluster          (regular dir on root FS)        │
│                            ├── seaweedfs/      filer leveldb                               │
│                            └── rqlite-arbiter/ (created but unused at N=1 — no DRBD       │
│                                                 means no arbiter rqlite)                  │
└───────────────────────────────────────────────────────────────────────────────────────────┘
```

**Election state:** Leader, `should_set_mgmt_master=False` (already master).

**What's notable about N=1:**

- There is no separate "arbiter rqlite" at N=1. The per-node rqlite
  on 4001/4002 IS the only voter, and it's self-elected. The
  arbiter unit on 4011/4012 is only useful at N≥2 where it floats
  with the master VIP.
- The `cluster` singleton DRBD resource doesn't exist yet. The
  singleton lives on the local FS at `/var/lib/bedrock/cluster`.
- The mesh CGNAT /24 prefix is derived from `cluster_uuid` (see
  `installer/lib/cluster_addr.py`). Even at N=1, the node's
  loopback `.1/32` is on `lo` — the master VIP `.254/32` is also
  on `lo` once `cluster_arbiter.promote_to_arbiter_host()` runs.

---

## Transition N=1 → N=2 — `bedrock join`

The joiner's `bedrock join` calls the master's mgmt API
`/api/join/request`. The master returns a per-join challenge; the
joiner signs it with its Ed25519 key. The operator approves via the
dashboard (`/api/join/approve`) or `bedrock node approve <id>` CLI.
On approval, the master writes:

- `nodes` row for the joiner (host, role=compute, loopback_ip)
- `cluster_key_hex` in the approval response

The joiner then runs `agent_install.install()`:

1. Writes `/etc/bedrock/cluster.key` from the master's key.
2. Writes `/etc/bedrock/state.json` with cluster_uuid + node_name + loopback_ip.
3. `tier_storage.setup_n1()` — ensures the thinpool, the weed-volume
   LV, and the local cluster-singleton dir.
4. Starts `bedrock-rqlited` with `-join` pointing at the master.
   rqlite Raft adds the joiner as a Voter.
5. Starts `bedrock-d` — the netd thread claims loopback `/32` on lo
   and begins probing mesh + mgmt interfaces.
6. SeaweedFS install: master + volume start on this node (master
   subset rule means at N=2 only the lowest-octet node actually
   runs `weed-master`; the higher-octet one disables it).
7. `dashboard_install` enables `bedrock-d.service`.

**Election state once the joiner's netd thread has probed back:**

- Master sees the joiner in `ever_seen_peers` → `n_nodes=2`,
  total_votes=200 (no witness assumed), majority=101. Master has
  200 votes (self + reachable peer). Stable Leader.
- Joiner sees the master in `ever_seen_peers` → same math. Stable
  Follower.

**Joiner-grace window:** for the first 1–5 s after the master
writes the joiner's row, the joiner's netd thread hasn't probed
back yet. Without the joiner-grace rule in
`installer/lib/election.py` (`members = {n for n in node_loopbacks
if n in peer_liveness}`) the master would see `n_nodes=2, votes=100
< majority=101` → NoQuorum → demote the singleton mid-join. The rule
keeps the master stable: count only peers we've heard from.

---

## Transition N=2 (and later N=3,4,…) → cluster-singleton DRBD

Operator runs `bedrock storage promote` on the master. This is the
N=1 → N=2 singleton promotion in
`installer/lib/tier_storage.transition_to_n2_master`:

1. **Snapshot** `/var/lib/bedrock/cluster/` to `/var/lib/bedrock-promote-snapshot/`
   so the filer's leveldb + arbiter rqlite data isn't lost when
   the empty DRBD device replaces the directory.
2. Stop `bedrock-weed-{s3,filer}` and `bedrock-rqlited-arbiter` so
   the singleton data is quiescent.
3. Create the per-resource external-metadata thin LV (`bedrock-meta-cluster`)
   alongside the data LV (`bedrock-data-cluster`).
4. `drbdadm create-md cluster` + `up` + `primary --force` on
   the master. DRBD tuning: `resync-rate 100M`, `c-min-rate 0`,
   `c-plan-ahead 0` so initial sync isn't throttled under app I/O.
5. Mount the DRBD device (`/dev/drbd1101`) at `/var/lib/bedrock/cluster`.
6. **Restore** the snapshot into the freshly-mounted DRBD volume.
7. Update fstab + atomic symlink swap.
8. Restart the singleton services.
9. Drive the peer side via SSH: `transition_to_n2_peer` creates
   the peer's local LV, joins as DRBD Secondary; initial sync
   starts.
10. `bedrock storage promote` waits up to 180 s for both sides to
    report `disk:UpToDate` (no `Sync` activity) before returning.
    Then `sync` flushes filer's leveldb buffers.

After promote, `cluster_arbiter.converge()` notices the `cluster`
resource exists and runs the N≥2 mode (DRBD primary + arbiter rqlite
on .254 + filer + s3). Subsequent peer-joins (N=3, N=4…) get added
as DRBD peers via `transition_to_n2_peer` on the joiner side, capped
at 3-way (`cap_singleton_peers`, lowest-octet nodes).

---

## State N≥2 healthy (after promote)

```
┌────────────────────────── node1 (current master) ──────────────────────────┐
│                                                                            │
│  bedrock-d                                                                  │
│   netd thread: Leader  bedrock-rqlited (per-node, 4001/4002)               │
│                        bedrock-rqlited-arbiter (4011/4012 on .254/32)      │
│                                                                            │
│   mgmt asyncio         8443 HTTPS + 8001 loopback, subscribed to rqlite    │
│                                                                            │
│  cluster_arbiter:      drbdadm primary cluster                             │
│                        mount /dev/drbd1101 → /var/lib/bedrock/cluster      │
│                        ip addr add 100.X.Y.254/32 dev lo                   │
│                        bedrock-weed-filer + bedrock-weed-s3 running        │
│                                                                            │
│  routes (kernel):                                                          │
│    100.X.Y.2/32 metric 10 nexthop via 192.168.2.…/br0 nexthop via 169…/enp2s0 …│
│    100.X.Y.3/32 metric 10 …                                                │
│    100.X.Y.4/32 metric 10 …                                                │
└────────────────────────────────────────────────────────────────────────────┘
                                  ▲   ▲   ▲
         3-way DRBD on the `cluster` singleton volume (master + 2 secondaries)
         per-node rqlite Raft (master + N-1 voters)
                                  │   │   │
┌─── node2 (follower) ─────┐   ┌─── node3 (follower) ─────┐   ┌─── node4 (follower) ────┐
│  netd thread: Follower   │   │  netd thread: Follower    │   │  netd thread: Follower  │
│  per-node rqlite voter   │   │  per-node rqlite voter    │   │  per-node rqlite voter  │
│  bedrock-d running       │   │  bedrock-d running        │   │  bedrock-d running      │
│  DRBD Secondary cluster  │   │  DRBD Secondary cluster   │   │  weed-volume only       │
│  weed-master (in subset) │   │  weed-master (in subset)  │   │  (singleton capped 3-way│
│  weed-volume always      │   │  weed-volume always       │   │   + master subset 3-of-4)│
└──────────────────────────┘   └───────────────────────────┘   └─────────────────────────┘
```

**Election math at N=4, no witness:** total_votes = 400, majority =
201. Master sees 4 peers (self + 3 alive) = 400 ≥ 201 → Leader.

**Election math at N=2 healthy:** total_votes = 200, majority = 101.
Master sees self + 1 peer = 200 ≥ 101 → Leader.

**Election math at N=2 with witness:** total_votes = 201 (200 + 1
witness vote), majority = 101. Same outcome under healthy state.

---

## Transition: isolation of the master

Operator (or fault) cuts the master off from the cluster mesh —
iptables DROP, mesh NIC down, or physical cable pull. Within
seconds:

### On the isolated master

1. The netd thread's probes go un-answered. Each
   `(peer_node, peer_nic, my_nic)` Neighbour's `last_seen` ages.
2. After `DOWN_HYSTERESIS_S=10` s of silence, `sweep_hysteresis`
   emits a `down` event and drops the Neighbour from `d.neighbours`.
3. The election layer sees the peer in `ever_seen_peers` but
   missing from live `d.neighbours` → `peer_liveness[peer] = False`.
4. Vote tally drops below `majority` → `Outcome.NO_QUORUM`.
5. A single self-demote counter rides out false positives (the ~5 s
   fresh-daemon startup window where neighbours=0 looks like NoQuorum).
6. On the `SELF_DEMOTE_MISSES=9`th consecutive NoQuorum tick (~9 s —
   1 s before a survivor promotes at `MASTER_LOSS_MISSES=10`, so .254
   is never on two nodes at once):
   - `lib/election.set_no_quorum_marker()` writes
     `/run/bedrock-no-quorum`
   - `cluster_arbiter.demote_arbiter_host()` runs directly from the
     netd thread (can't rely on the orchestrator's converge — rqlite
     is by definition unreachable in NoQuorum):
     - Stop `bedrock-weed-{s3,filer}`
     - Stop `bedrock-rqlited-arbiter`
     - `ip addr del 100.X.Y.254/32 dev lo` (release the master VIP)
     - `umount /var/lib/bedrock/cluster`
     - `drbdadm secondary cluster`
7. The mgmt asyncio's `no_quorum_responder` notices the marker, pauses
   running VMs (so qemu doesn't keep writing to a now-secondary
   DRBD device), and waits for `_wait_for_role` to return a settled
   role before clearing the marker.

### On the surviving partition (N-1 nodes)

1. The netd thread on every surviving node sees the master in
   `ever_seen_peers` AND its `peer_liveness[master] = False` →
   election sees `current_mgmt_master` is gone.
2. After `MASTER_LOSS_MISSES=10` missed master beats (~10 s) the
   election promoter rule fires:
   `should_set_mgmt_master = (my_votes ≥ majority)
     AND (current_master is gone) AND (I have the lowest loopback
     octet among reachable peers)`.
3. The lowest-octet survivor writes `bs.set_mgmt_master(self)` to
   rqlite. Rqlite Raft replicates (3 of 4 voters is quorate).
4. Every node's `orchestrator.rqlite_subscriber` sees the
   revision advance, rebuilds the snapshot, projects to
   `state.json` (the new master's `role` flips to `mgmt+compute`).
5. New master's `cluster_arbiter.converge()` reads
   `i_should_host_arbiter()=True` and calls
   `promote_to_arbiter_host()`:
   - `drbdadm primary cluster` — fails with "Need access to
     UpToDate data" because the old master is unreachable
   - Retry with `drbdadm -- --force primary cluster` —
     succeeds (election authority + witness UUID claim already
     decided this side wins; --force is correct)
   - Mount `/var/lib/bedrock/cluster`
   - `ip addr add 100.X.Y.254/32 dev lo`
   - Start `bedrock-rqlited-arbiter`
   - `seaweedfs.promote_to_filer_host()` — starts filer + s3
6. The new master reads its `cluster` DRBD current-UUID via
   `drbdadm dump-md` and updates its own witness slot —
   `witness.set_own_slot(marker=uuid, tag=TAG_LMS iff alone)`.
   The next `heartbeat_all` packet publishes the AEAD-sealed slot
   (ChaCha20-Poly1305 over msgpack, UDP 12321) to the LAN's BedRock
   Echo, which stores it verbatim.
7. The witness is a passive per-node K/V slot store with NO concept
   of "blessed master" — each node owns one slot (keyed by its
   loopback last octet) and writes only its own. Other nodes verify
   `slot[M].marker == drbdadm current-uuid` (exact equality) locally
   before any takeover attempt. See `docs/cluster-quorum-spec.md` for
   the protocol.

**End state during isolation:** the isolated old master holds none
of the singletons; the new master serves filer/s3 at .254. From
the operator's workstation (which is on a different bridge than
the cluster mesh), `https://<new-master-mgmt-ip>:8443` works
normally. End-to-end this lands at roughly: isolated master suspends
its pet VMs at ~T+20-30 s, the survivor takes over at ~T+35 s, and
no split-brain occurs on rejoin.

---

## Transition: master rejoins after isolation

1. Isolation is lifted (iptables flush, NICs back up).
2. The old master's netd thread re-discovers peers within 1–2 probe
   intervals (≤2 s).
3. Election sees `current_mgmt_master = <new master>` AND the new
   master is alive → outcome = `Follower`. `should_set_mgmt_master
   = False`. `demoted_in_cycle` resets so the next NoQuorum event
   can act.
4. The witness still holds the new master's slot (fresh
   ts_writer, `tag.lms` if applicable, current DRBD-UUID marker).
   If the old master tried to take over, it would inspect the new
   master's slot, see it's fresh (not stale ≥ `SLOT_STALE_MS` = 10 s)
   and either `tag.lms == 1` (legitimate solo) or the master is
   mesh-reachable anyway → step 2 of takeover refuses. Old master
   stays Follower per `cluster-quorum-spec.md` Scenario B.
5. The old master's per-node rqlite re-joins Raft (already a Voter,
   just catches up the log).
6. `orchestrator.no_quorum_responder` waits for `_wait_for_role` to
   return `follower`. Once it does, it clears the no-quorum marker and
   runs `_reconcile_paused_vms`: for each paused VM, if rqlite
   (`cluster_state.load_cluster()`) says it's now on another host,
   `virsh destroy` the local stale copy + `drbdadm secondary` the
   per-VM DRBD resource; if it still says it's ours, `virsh resume`.

**Why the witness DRBD-UUID claim matters:** without it, a flapping
link could let the OLD master re-attach with stale leveldb data and
re-claim primacy. The new master's witness slot records its current
DRBD UUID — if the OLD master tries to claim with an older UUID (the
markers don't match by exact equality), takeover refuses while the
slot is fresh.

---

## State: 2-node HA with witness

Half-quorum on either side — needs the witness to break a tie.

### Healthy

- election: `total_votes = 200 + 1 = 201`, `majority = 101`
- Master sees self + peer + witness alive = 200 + 1 = 201 ≥ 101 → Leader
- Follower sees self + peer + witness alive = 200 + 1 = 201 ≥ 101 → Follower

### Master isolated, witness on the follower's side

- Master sees: self (100) only. Witness unreachable too (the
  partition cut the master off the LAN entirely). Total = 100/101 →
  NoQuorum → self-demote .254/arbiter/filer (at `SELF_DEMOTE_MISSES`
  ~9 s).
- Follower sees: self (100) + witness vote (1) = 101/101 → Leader.
  Calls `set_mgmt_master(self)`. Promotes the singleton. Writes its
  witness slot. Cluster now runs on the follower.

### Both sides see witness (impossible in real partition)

- Both halves think they have 101/101 → both try to promote.
- Each node writes only its own witness slot, so neither can "steal"
  the other's; rqlite's own Raft refuses the 1-of-2 write anyway —
  the lone voter has no quorum, so only one side's `mgmt_master`
  actually lands.

### Witness absent

- Both sides see 100/101 → both stay NoQuorum, both self-demote.
- No master, no singleton. Service down, data safe. Operator
  manually intervenes once the partition is fixed.

---

## Scale-down — `bedrock node leave <target>`

Issued from a node that is NOT the target. Steps in the CLI:

1. Master appends `node_unregister(target)` to rqlite. Replicates.
2. Master calls rqlite's `/remove` to drop the target's Voter slot
   so future leaves don't brick quorum at N/2 voters.
3. Master regenerates its own state from the new snapshot.
4. Master SSHes to the leaving node: `systemctl stop bedrock-d
   bedrock-rqlited bedrock-rqlited-arbiter; rm -f /run/bedrock-no-quorum`.
   The node stops broadcasting probes.
5. Other peers' netd thread detects the down after `DOWN_HYSTERESIS_S`
   and `sweep_hysteresis` drops the Neighbour. The node is no
   longer in the rqlite `nodes` table, so `ever_seen_peers` minus
   the unregistered set keeps the quorum math correct.

---

## Boot recovery — node reboots while cluster is up

1. Anaconda kickstart + bedrock-firstboot.service + install.sh +
   `bedrock bootstrap` are all idempotent. After firstboot, on
   each subsequent boot `bedrock-d` starts via systemd.
2. The netd thread reads `/etc/bedrock/state.json` for cluster_uuid +
   node_name + loopback_ip. Claims `.<i>/32` on lo (idempotent).
3. `bedrock-rqlited` (per-node) starts via systemd; rejoins the
   existing Raft (its node-id is the last octet of its loopback,
   permanent across restarts per `lesson_rqlite_node_id_stability`).
4. `bedrock-d`'s orchestrator subscribes to rqlite and projects this
   node's role to `state.json`. If `state.json["role"]` says
   mgmt+compute, `cluster_arbiter.converge()` triggers
   `promote_to_arbiter_host()`. Otherwise demote (no-op if
   already not hosting).
5. `no_quorum_responder` is idle (no marker present on a normal boot).
6. `_reconcile_paused_vms` runs once `_wait_for_role` returns a
   settled role.

---

## What can go wrong (and where to look)

| Symptom                                 | Likely cause                                  | Where to look                                |
|-----------------------------------------|-----------------------------------------------|----------------------------------------------|
| `rqlite POST … Connection refused`      | per-node rqlited not started                  | `systemctl status bedrock-rqlited`           |
| Master sees `n_nodes=N` but `votes=100` | joiner not yet probed back                    | `journalctl -u bedrock-d | grep ever_seen`   |
| Filer not active on master post-promote | leveldb hidden under DRBD mount               | `ls -la /var/lib/bedrock/cluster/seaweedfs/` |
| S3 marker LOST after failover           | leveldb didn't sync to new master             | check snapshot restored in transition_to_n2_master |
| `.254` not released within ~9 s         | `DOWN_HYSTERESIS_S`/`SELF_DEMOTE_MISSES` bug  | netd log: `election leader → noquorum`       |
| Master flaps leader↔noquorum            | self-demote counter + demoted_in_cycle bug    | netd log: noquorum_master_ticks, demoted_in_cycle |
| `drbdadm primary` fails post-failover   | DRBD Secondary thinks data not UpToDate       | cluster_arbiter retries with `--force`       |
| `Only odd number of masters supported`  | SeaweedFS Raft refused even master subset     | check `seaweedfs.write_master_config` rule   |
| Joiner sees stale IP on master          | `get_mgmt_ip` ARP-cache miss + bad fallback   | spawn.py ARP sweep + ip neigh                |
| `bedrock storage promote` exits early   | DRBD initial sync ≥180 s                      | extend timeout in `bedrock` action="promote" |

---

## Key file paths

- `/etc/bedrock/state.json` — this node's identity + derived role +
  master URL, projected from rqlite (the only local cluster-related
  file; survives cold-boot without rqlite). Cluster topology itself
  lives only in rqlite, read via `cluster_state.load_cluster()`.
- `/etc/bedrock/cluster.key` — 32-byte cluster key shared across the
  cluster; keys the mesh HMAC-SHA256 (probes/adverts) AND the witness
  ChaCha20-Poly1305 AEAD slots.
- `/etc/bedrock/installer.env` — `BEDROCK_REPO=file:///…` for
  offline-install re-runs.
- `/run/bedrock-no-quorum` — no-quorum marker (NoQuorum-triggered).
- `/var/lib/bedrock/seaweedfs/volumes` — local weed-volume LV mount.
- `/var/lib/bedrock/cluster` — cluster-singleton root. At N=1
  this is a regular dir; at N≥2 (post-promote) it's the DRBD-
  replicated XFS mount.
- `/var/lib/bedrock/rqlite` — per-node rqlite data dir.
- `/var/lib/bedrock-install` — install.sh staging area copied
  from the ISO firstboot payload.
