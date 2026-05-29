# Storage architecture

Canonical reference for Bedrock's on-disk layout, DRBD topology, and
SeaweedFS deployment. Pair with `docs/cluster-quorum-spec.md` for the
witness/quorum side.

## TL;DR

- **One thinpool per node** (`thinpool` in the node's VG) holds
  everything: the DRBD-replicated `cluster` singleton LV pair, every
  per-VM DRBD LV pair, and the local SeaweedFS volume-server LV.
  Operators reallocate freely between consumers.
- **One external-meta LV per DRBD resource.** Resource creation =
  `lvcreate data` + `lvcreate meta` + `drbdadm create-md --max-peers=7`.
  Online growth = `lvextend meta` (if needed) + `lvextend data` +
  `drbdadm resize`. No downtime. External metadata makes local-LV →
  DRBD promotion zero-copy (the data LV's XFS is preserved
  byte-for-byte) and keeps the DRBD device the same size as the data LV.
- **`.254/32` cluster-singleton VIP** on `lo`, hosted by whichever node
  is the current arbiter. Bound at every cluster size including N=1, so
  the FUSE mount target and mgmt URL never change as the cluster grows.
  Hosts: arbiter rqlite (`:4011/:4012`), SeaweedFS filer (`:8888`), mgmt
  HTTPS (`:8443`).
- **SeaweedFS topology**: filer singleton on `.254:8888`; master is a
  Raft group on the lowest-octet odd subset of nodes (NOT on `.254`);
  volume + S3 on every node bound `0.0.0.0`. Every node FUSE-mounts the
  filer at `/mnt/bedrock` via `.254:8888`. Three collections — `scratch`
  (000), `standard` (001), `critical` (002).
- **S3 IAM identities** are an admin + anonymous identity in a per-node
  sidecar `/etc/bedrock/seaweedfs-s3.json`, derived deterministically
  from `/etc/bedrock/cluster.key` so every node renders the same admin
  credentials. The POSIX namespace + bucket configs live in the filer's
  `leveldb3` on the cluster-singleton DRBD volume.

## LVM layout (per node)

```
VG (bedrock-vg on a fresh install; the adopted OS VG otherwise)
└── thinpool thinpool          (one per node)
    ├── LV bedrock-data-cluster        ┐  DRBD-replicated to ≤3 peers
    ├── LV bedrock-meta-cluster        ┘  (rqlite + filer + S3 buckets)
    │
    ├── LV bedrock-data-vm-<name>      ┐  DRBD-replicated per VM
    ├── LV bedrock-meta-vm-<name>      ┘  (one pair per VM disk)
    │
    └── LV bedrock-weed-volume          local; NOT DRBD
                                        SeaweedFS handles file-level
                                        replication across nodes
```

One disk → one VG → one thinpool. The thinpool starts sized to fill the
VG (less a small reserve for its own metadata growth) and `lvextend`
grows it as the VG fills. Both DRBD meta LVs are thin (in-pool), so they
consume blocks only as DRBD dirties bitmap bits.

The VG name is `bedrock-vg` on a greenfield install (carve a PV from the
boot-disk tail or a separate data disk, then `vgcreate`). On an install
over an existing AlmaLinux box, Bedrock adopts whatever VG the OS
installer made (often `almalinux`) rather than renaming it — `vgrename`
would force a grub + initramfs + reboot dance for a cosmetic gain. The
resolved name is written to `/etc/bedrock/storage.json`.

**Why one thinpool, not one per tier?** SeaweedFS already expresses tier
semantics at the collection level (a replication policy per collection).
Thinpools just give thin-provisioning + discard passdown; slicing them
by tier buys nothing.

## DRBD per-resource layout

Each DRBD resource owns ONE data LV and ONE meta LV in the same VG:

| Operation         | Steps                                                  |
|-------------------|--------------------------------------------------------|
| Create resource   | `lvcreate data` + `lvcreate meta` + `drbdadm create-md --max-peers=7` |
| Grow data volume  | `lvextend meta` (if needed) + `lvextend data` + `drbdadm resize` (online) |
| Add replica peer  | `drbdadm new-peer` (online; bitmap pre-baked at create-md) |
| Destroy resource  | `drbdadm down` + `lvremove data` + `lvremove meta`     |

`--max-peers=7` is baked in at create-md, reserving bitmap space for up
to 7 replicas. Actual peer count = current cluster membership (1 / 2 / 3
/ N). The bitmap stays thin until a peer disconnects and writes start
dirtying bits, so the pre-baking is free in steady state. 7 is the v1.0
ceiling; raising it later is the only resize op that needs downtime.

Every resource is rendered with protocol C, `on-no-quorum suspend-io`,
`resync-rate 100M`, `c-min-rate 0`, `c-plan-ahead 0`, and TRIM passdown
(`rs-discard-granularity 65536`, `discard-zeroes-if-aligned yes`) so a
`fstrim` on the mounted FS reclaims pool blocks on every peer. Per-peer
node-ids are persistent for the life of the resource (allocated on first
sight, never renumbered) so adding or removing a peer never forces a
full resync of the others.

### Cluster-singleton DRBD resource is capped at 3 peers

The `cluster` DRBD resource (`bedrock-data-cluster` / `bedrock-meta-cluster`,
minor 1101) holds rqlite + filer DB + S3 buckets. It is capped at exactly
3 peers — the lowest-octet nodes — once N ≥ 3. 1 primary + 2 mirrors is
enough redundancy for the arbiter; replicating to 7+ nodes synchronously
would slow every rqlite/filer write for no gain. `cap_singleton_peers()`
enforces the cap even if a caller passes a wider peer list.

| Cluster N | Cluster-DRBD peers       | Notes |
|-----------|--------------------------|-------|
| 1         | 0 (local LV, no DRBD)    | `cluster` is a plain directory on the root FS. |
| 2         | 2 (both nodes)           | Standard DRBD pair. |
| 3+        | 3 (lowest-octet 3 nodes) | Nodes 4..N do NOT carry the cluster-DRBD resource. |

When one of the 3 arbiter-bearing nodes leaves, the cluster runs with 2
arbiter peers (still HA, below design redundancy). The calm orchestrator
later promotes a non-arbiter node into the set: allocate its cluster-DRBD
LV pair, `drbdadm new-peer`, initial sync, mark it a candidate for `.254`
ownership. Membership is tracked in rqlite (`cluster_drbd_membership`).

This promotion is NOT on the critical-failover path. Picking which node
to promote needs a free-space scan, current arbiter-set load, and
peer-mesh health, so it lives in the calm loop (see
[Two-loop split](#two-loop-split-critical-vs-calm)), not netd's 1-Hz tick.

## Everything goes through rqlite (except arbiter recovery)

Every long-running cluster operation — VM create, disk grow, DRBD
attach/detach, node join, cluster-DRBD membership change, weed-master
reshuffle, SeaweedFS replica fix-up, backup-target rotation — is a
**saga in rqlite**, run by the saga executor (`bedrock_d/orchestrator/sagas/`):

1. The caller writes an `operations` row with `state='pending'` and
   parameters as JSON.
2. The target node's executor picks it up, transitions to
   `state='in_progress'`, and starts executing.
3. Each step writes an `operation_steps` row on completion before
   moving on.
4. The final step writes `state='completed'`.

After a power loss mid-saga, on next boot the node queries rqlite for
`operations WHERE state IN ('pending','in_progress') AND target_node = self`,
finds the last completed step in `operation_steps`, and resumes from the
next idempotent step. Every step is a no-op-if-already-done or a clean
retry.

**The one exception is arbiter recovery.** rqlite is what's being
recovered when `.254` needs to fail over, so the takeover protocol can't
write to rqlite — it uses witness slots + local commands only (see
`cluster-quorum-spec.md`). Once the arbiter is back and rqlite has
quorum, all other in-flight sagas resume through the rqlite-driven path.

This boundary keeps the critical path tiny (witness + local commands,
deterministic in seconds) while everything else gets crash-safe
orchestration for free.

### Saga schema (rqlite)

Source of truth: [`installer/lib/bedrock_schema.sql`](../installer/lib/bedrock_schema.sql).

```sql
CREATE TABLE IF NOT EXISTS operations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT NOT NULL,     -- e.g. "drbd_resource_create",
                                     --      "cluster_tier_promote",
                                     --      "weed_master_reshuffle",
                                     --      "node_leave"
    target_node   TEXT,              -- node that runs this; NULL = any
    params        TEXT NOT NULL,     -- JSON payload
    state         TEXT NOT NULL DEFAULT 'pending',
                                     -- 'pending'|'in_progress'|
                                     -- 'completed'|'failed'
    requested_by  TEXT NOT NULL DEFAULT '',
    error         TEXT,
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL,
    completed_at  INTEGER
);

CREATE TABLE IF NOT EXISTS operation_steps (
    op_id        INTEGER NOT NULL,
    step_name    TEXT NOT NULL,      -- e.g. "lvcreate_data", "drbd_create_md"
    state        TEXT NOT NULL,      -- 'done'|'failed'
    error        TEXT,
    started_at   INTEGER,
    finished_at  INTEGER,
    PRIMARY KEY (op_id, step_name),
    FOREIGN KEY (op_id) REFERENCES operations(id)
);
```

Per-resource state tables (`drbd_resources`, `cluster_drbd_membership`,
`seaweed_master_membership`) hold the *outcomes* a saga writes;
in-flight progress lives in `operations` + `operation_steps`.

## Two-loop split: critical vs calm

Two control loops with different latency and complexity budgets. Storage
lifecycle work is partitioned between them on purpose:

| Loop                    | Where                                            | Cadence | Job |
|-------------------------|--------------------------------------------------|---------|-----|
| **Critical / netd**     | `installer/lib/netd.py` (thread in bedrock-d)    | 1 s     | Mesh probes, election, takeover, self-demote, route emission. Deterministic, completes in seconds. No multi-table rqlite reads, no sizing. |
| **Calm / orchestrator** | `mgmt/orchestrator.py` (asyncio task in bedrock-d) | revision-driven reconcile | Capacity planning, arbiter-set membership, weed-master Raft membership, thinpool growth, drbd_resources bookkeeping, VM placement. Reads many rqlite tables, plans, then acts. Never on the failover-decision path. |

The **takeover protocol** in `cluster-quorum-spec.md` runs entirely in
the critical loop: witness + local commands, no rqlite, no sizing, no
"pick the best candidate". The calm loop is what makes the *next*
takeover possible — it keeps a healthy arbiter set in place.

Storage-lifecycle ownership by loop:

| Operation                                       | Loop      | Why |
|-------------------------------------------------|-----------|-----|
| Election outcome (Leader / Follower / NoQuorum) | critical  | 1-Hz deterministic, no rqlite reads on the hot path. |
| Takeover (drbdadm primary, mount, bind `.254`)  | critical  | Must complete in seconds with witness + local commands only. |
| Self-demote on NoQuorum                         | critical  | Stops services before any cluster-visible state change. |
| Set `mgmt_master` in rqlite post-takeover       | critical  | Single write after the local promote succeeded; not a gate. |
| Pick replacement for departed arbiter peer      | **calm**  | Needs free-space scan + peer-mesh-health check. Cluster runs degraded until done. |
| Promote replacement into arbiter set            | **calm**  | LV allocate + `drbdadm new-peer` + initial sync — minutes, not seconds. |
| Re-shuffle weed-master Raft membership          | **calm**  | Deliberate placement decision, not time-critical. |
| Grow a thinpool                                 | **calm**  | Operator- or capacity-watcher-triggered; never urgent. |
| Grow a VM disk (`bedrock vm grow`)              | **calm**  | Operator-driven; `lvextend` + `drbdadm resize`. |

The contract: the critical loop never blocks on a query that could take
longer than a few hundred ms and never makes a sizing decision; the calm
loop never owns keeping the cluster up during a failure — it reconciles
toward a healthy *future* state.

## `.254/32` cluster-singleton

One VIP, one set of singleton services, one current host:

```
.254/32 on lo (whichever node is the current arbiter)
├── rqlited-arbiter      :4011/:4012   (the 3rd-voter Raft member)
├── seaweedfs-filer      :8888         (POSIX namespace + bucket configs in leveldb3)
└── mgmt HTTPS           :8443         (single-address operator/CLI entry)
```

`/var/lib/bedrock/cluster/` is the on-disk root — the mount of the
`cluster` singleton DRBD resource (`/dev/drbd1101`). The filer's
`leveldb3` dir and the arbiter rqlite data dir both live under it, so one
DRBD failover hands them off together. `cluster_arbiter.converge()` owns
the DRBD primary, mount, `.254` claim, arbiter rqlite, and filer/S3
promote.

Bound at all N including N=1. Even a single-node bootstrap binds `.254`,
so client config (FUSE mount target, mgmt URL) is identical across
cluster sizes — nothing to reconfigure when the cluster grows from 1 to 2.

## SeaweedFS topology

```
.254  (cluster-singleton)
└── weed-filer    :8888    leveldb3 on the cluster-singleton DRBD volume
                            (POSIX namespace + bucket configs)

every node
├── weed-master   :9333    Raft group on the lowest-octet odd subset
│                          (N=1→1, N=2→1, N≥3→3; tracked in rqlite
│                          table seaweed_master_membership)
├── weed-volume   :8080    file bytes; one daemon per node, bound 0.0.0.0
├── weed-s3       :8333    S3 API; one daemon per node, bound 0.0.0.0
└── /mnt/bedrock           FUSE mount of .254:8888, uniform on every node
```

- **Filer** is a singleton on `.254`. Failover = DRBD primary handoff +
  filer restart on the new arbiter-host; the FUSE clients auto-reconnect.
- **Master** runs on the lowest-octet odd subset (Raft needs an odd
  member count). NOT bound to `.254`, so weed-master quorum is
  independent of `.254` arbiter handovers. Membership lives in rqlite;
  the calm orchestrator reshuffles it when a master-bearing node leaves.
- **Volume** + **S3** on every node, bound `0.0.0.0`. The S3 gateway and
  volume server point at the filer VIP `.254:8888`, so all gateways share
  one namespace and one identity store. The volume server advertises its
  loopback `/32` as `publicUrl` for a stable per-node ID.

### Three collections

Storage classes are SeaweedFS *collections* on a single weed-volume per
node — NOT separate thinpools, NOT separate clusters:

| Collection | Replication | Copies | Behaviour at smaller N |
|------------|-------------|--------|-------------------------|
| `scratch`  | `000`       | 1 total | Always single-copy. Lost on the hosting node's failure. |
| `standard` | `001`       | 2 (different server) | N=1: 1 copy. N≥2: 2 copies on different nodes. |
| `critical` | `002`       | 3 (different servers) | N=1: 1 copy. N=2: 2 copies. N≥3: 3 copies. |

`init_collections()` applies these as path policies via `weed shell`:
`/scratch/`→scratch, `/iso/` `/templates/` `/snapshots/`→standard,
`/backups/`→critical. The replication code per prefix is clamped to what
the current cluster can satisfy (N=1 forces 000, N=2 caps at 001), so an
ISO upload on a 1-node box doesn't hang at volume-assign time. When more
nodes join, raising a prefix's policy to its full code is a calm-loop
re-`fs.configure` over `weed shell`; SeaweedFS's master then re-replicates
under-policy volumes onto the newly available servers in the background.

Replication code `XYZ` reads as: `X` extra copies in other datacenters ·
`Y` in other racks · `Z` on other servers (same rack). v1.0 uses no rack
tag — every node is the default rack — so `Z` does the work. Multi-rack
sites can add rack tags and bump to `010` for cross-rack diversity.

### FUSE mount layout (every node)

```
/mnt/bedrock
├── scratch/      (collection: scratch)
├── iso/          (collection: standard)
├── templates/    (collection: standard)
├── snapshots/    (collection: standard)
└── backups/      (collection: critical)
```

A single `bedrock-fuse-mount.service` runs `weed mount
-filer=.254:8888 -dir=/mnt/bedrock` on every node. The mount target is
the cluster VIP everywhere, so it doesn't change when the arbiter-host
flips; the filer moves with `.254` and the FUSE client auto-reconnects.

### S3 IAM identities

`weed s3` runs with `-config=/etc/bedrock/seaweedfs-s3.json`, a per-node
sidecar holding two identities:

- `admin` — full actions, credentials derived from
  `/etc/bedrock/cluster.key` via two domain-tagged SHA-256 hashes, so
  every node renders the same admin keypair (the same secret that
  underwrites witness AEAD also underwrites S3 admin).
- `anonymous` — Read/Write/List/Tagging, for testbed push-without-auth.
  Operators override this before any production deploy.

Bucket configs and the POSIX namespace live in the filer's `leveldb3` on
the DRBD-replicated `/var/lib/bedrock/cluster/seaweedfs/`, so a backup of
the cluster volume captures the namespace; identity creds regenerate from
`cluster.key` on any node.

### `bedrock.local` external endpoint

External systems (customer kopia targets, LAN backup appliances) use
`bedrock.local` as their S3 endpoint hostname. The mDNS responder on
every node advertises it; any UP node answers, giving a stable DNS name
without a LAN VIP. Internal callers on a Bedrock node use
`127.0.0.1:8333` (the local S3 daemon). Off-site backup targeting is a
per-customer kopia/restic policy, not a Bedrock concern.

## Port map (one place)

| Service           | Port | Bind          | Notes |
|-------------------|------|---------------|-------|
| mgmt HTTPS        | 8443 | `0.0.0.0`     | Operator UI + LAN API (operator-authed); reached at `.254` |
| mgmt local HTTP   | 8001 | `127.0.0.1`   | local CLI / intra-process (auth-exempt) |
| rqlited per-node  | 4001 / 4002 | node loopback | HTTP API (4001, HTTPS mTLS) + Raft (4002) |
| rqlited arbiter   | 4011 / 4012 | `.254`  | 3rd-voter Raft member (HTTPS mTLS + Raft) |
| weed-master       | 9333 | node loopback | Raft odd-subset |
| weed-volume       | 8080 | `0.0.0.0`     | file bytes; every node |
| weed-filer        | 8888 | `.254`        | namespace + buckets; singleton |
| weed-s3           | 8333 | `0.0.0.0`     | S3 API; every node |
| bedrock-echo      | 12321 (UDP) | LAN  | witness K/V (passive; ChaCha20-Poly1305 AEAD) |
| netd probes       | 7732 (UDP/mcast) | 239.7.7.7 | mesh discovery (HMAC-SHA256) |
| DRBD              | 7700-7799 | per-link IP | per-resource ports = 7700 + (minor − 1100); cluster = 7701 |

## rqlite schema (storage-related tables)

Abridged; source of truth is
[`installer/lib/bedrock_schema.sql`](../installer/lib/bedrock_schema.sql).

```sql
-- One row per DRBD resource (cluster singleton + per-VM)
CREATE TABLE IF NOT EXISTS drbd_resources (
    name              TEXT PRIMARY KEY,    -- e.g. "cluster", "vm-foo-disk0"
    minor             INTEGER NOT NULL,    -- DRBD minor (cluster = 1101)
    data_lv           TEXT NOT NULL,       -- e.g. "bedrock-data-vm-foo-disk0"
    meta_lv           TEXT NOT NULL,       -- e.g. "bedrock-meta-vm-foo-disk0"
    thinpool          TEXT NOT NULL DEFAULT 'thinpool',
    data_size_bytes   INTEGER NOT NULL,
    meta_size_bytes   INTEGER NOT NULL,
    max_peers         INTEGER NOT NULL DEFAULT 7,
    peers             TEXT NOT NULL DEFAULT '[]',  -- JSON node_name set
    current_uuid      TEXT NOT NULL DEFAULT '',    -- last Primary DRBD UUID
    uuid_ts_set       INTEGER NOT NULL DEFAULT 0,
    created_at        INTEGER NOT NULL,
    updated_at        INTEGER NOT NULL
);

-- Which nodes carry the cluster-singleton DRBD resource (capped at 3).
-- This is the set the .254 arbiter VIP can migrate to.
CREATE TABLE IF NOT EXISTS cluster_drbd_membership (
    node_name         TEXT PRIMARY KEY,
    joined_at         INTEGER NOT NULL,
    updated_at        INTEGER NOT NULL
);

-- Membership of the Raft weed-master set.
CREATE TABLE IF NOT EXISTS seaweed_master_membership (
    node_name         TEXT PRIMARY KEY,
    joined_at         INTEGER NOT NULL,
    updated_at        INTEGER NOT NULL
);
```

The calm orchestrator owns both membership tables; nodes don't write
them directly. `current_uuid` is written via a strong (quorum-confirmed)
rqlite update on every successful `drbdadm primary` and read back as the
pre-start safety check before a VM is started on a new primary.

## Lifecycle interactions (summary)

- **Cluster init (N=1)**: create the thinpool; create + mount the local
  `bedrock-weed-volume` LV; the `cluster` singleton is a directory on the
  root FS at `/var/lib/bedrock/cluster` (mode `local`, no DRBD); bind
  `.254`; start arbiter rqlite (single-node) + filer + S3 + mgmt; start
  weed-volume + weed-s3; FUSE-mount `.254:8888` → `/mnt/bedrock`.
- **Cluster grow (N=2)**: the existing node promotes its local `cluster`
  directory onto DRBD — stop the singletons, snapshot the dir, allocate
  `bedrock-data-cluster` + `bedrock-meta-cluster`, `create-md
  --max-peers=7`, mount the DRBD device, restore the snapshot, restart
  the singletons. The new node creates its thinpool + `cluster` LV pair,
  `drbdadm new-peer`, initial sync, joins weed-volume + weed-s3 +
  weed-mount. The singleton now has 2 peers.
- **Cluster grow (N=3)**: the new node allocates the `cluster` LV pair
  and becomes the 3rd peer; the weed-master Raft group reaches 3; rqlite
  promotes to a 3-voter Raft.
- **Cluster grow (N>3)**: the new node creates its thinpool; joins
  weed-volume + weed-s3 + weed-mount; does NOT get a `cluster` LV pair —
  the 3-peer cap (`cap_singleton_peers`) holds. It is eligible for
  arbiter-set promotion only if a current arbiter-bearer leaves.
- **Node leave (arbiter-bearer)**: the calm orchestrator picks a
  replacement from the non-arbiter pool; the new node allocates the LV
  pair, joins the singleton, initial-syncs. The leave is held until the
  new peer is UpToDate.
- **VM disk grow** (`bedrock vm grow`): `lvextend` the meta LV if needed,
  `lvextend` the data LV, `drbdadm resize` on all peers. Online, seconds.
- **VM disk destroy**: `drbdadm down`, `wipe-md`, `lvremove` data + meta,
  one row deleted from `drbd_resources`.

Per-VM disks themselves are owned by the VM-lifecycle sagas
(`bedrock_d/vm/*`), not `tier_storage`: **cattle** = one local thin LV
(no DRBD, no migrate); **pet** = 2-way DRBD; **vipet** = 3-way DRBD.
Per-VM resource = `vm-<name>-disk0`, minor 1102+.

## Out of scope (post-v1.0)

- Multi-witness quorum reads/writes (see `cluster-quorum-spec.md`).
- Erasure coding for the SeaweedFS bulk tier (today: replication-2 or -3).
- Cross-datacenter replication (the `X` digit of the replication code).
- Per-VM-disk DRBD-on-DRBD nesting (fast-tier data + faster-tier
  write-cache).
- IAM identities moving from the sidecar JSON into the filer DB via
  `weed s3 -iam.filerBucketsPath`.
