# Storage architecture

Canonical reference for Bedrock's on-disk layout, DRBD topology, and
SeaweedFS deployment. Pair with `docs/cluster-quorum-spec.md` for the
witness/quorum side.

## TL;DR

- **One thinpool per node** (`thinpool` in `bedrock-vg`)
  holds everything: the DRBD-replicated cluster singleton LV pair,
  per-VM DRBD LV pairs, and the local SeaweedFS volume-server LV.
  Operator can reallocate freely between consumers.
- **One thin meta LV per DRBD resource.** No shared indexed metadata
  pool. Resource creation = `lvcreate data` + `lvcreate meta` +
  `drbdadm create-md --max-peers=7`. Online growth = `lvextend meta`
  (if needed) + `lvextend data` + `drbdadm resize`. No downtime.
- **`.254/32` cluster-singleton VIP** on `lo`, hosted on whichever
  node is the current arbiter-host. Bound on every cluster size
  including N=1. Hosts: rqlite arbiter, SeaweedFS filer:8888, mgmt
  HTTPS:8443.
- **SeaweedFS topology**: filer singleton on `.254:8888`, master is a
  Raft-3 group across regular nodes (NOT on `.254`), volume + S3 on
  every node. Every node FUSE-mounts the filer at `/mnt/bedrock`
  pointing at `.254:8888`. Replication policy `001` by default; three
  collections — `scratch` (000), `standard` (001), `critical` (002).
- **S3 IAM identities live inside the filer DB** via
  `weed s3 -iam.filerBucketsPath=/buckets`. No sidecar
  `identities.json`. One DRBD-replicated leveldb3 = whole namespace +
  bucket policies + access keys.

## LVM layout (per node)

```
VG bedrock-vg
└── thinpool thinpool          (one per node)
    ├── LV bedrock-data-cluster        ┐  DRBD-replicated to ≤3 peers
    ├── LV bedrock-meta-cluster        ┘  (rqlite + filer + S3 IAM)
    │
    ├── LV bedrock-data-vm-<name>      ┐  DRBD-replicated per VM
    ├── LV bedrock-meta-vm-<name>      ┘  (one pair per VM disk)
    │
    └── LV bedrock-weed-volume          local; NOT DRBD
                                        SeaweedFS handles file-level
                                        replication across nodes
```

One disk → one VG → one thinpool. The thinpool starts small and
`lvextend` grows it as the underlying VG fills. Operators can reallocate
space between cluster singletons, VM disks, and the weed volume
without repartitioning.

**Why not multiple thinpools per tier?** SeaweedFS already provides
the tier semantics at the collection level (different replication
policies per collection). LVM thinpools just give us
thin-provisioning + discard passdown; there's no operational reason
to slice them by tier. One pool, three SeaweedFS collections.

## DRBD per-resource layout

Each DRBD resource owns ONE data LV and ONE meta LV in the same VG.
There is no shared "metadata pool" with indexed offsets; the
operational pattern is simpler:

| Operation         | Steps                                                  |
|-------------------|--------------------------------------------------------|
| Create resource   | `lvcreate data` + `lvcreate meta` + `drbdadm create-md --max-peers=7` |
| Grow data volume  | `lvextend meta` (if needed) + `lvextend data` + `drbdadm resize` (online) |
| Add replica peer  | `drbdadm new-peer` (online; bitmap pre-baked at create-md) |
| Destroy resource  | `drbdadm down` + `lvremove data` + `lvremove meta`     |

`--max-peers=7` is baked in at create-md once; this reserves bitmap
space for up to 7 replicas. Actual peer count = current cluster
membership (1 / 2 / 3 / N). Bitmap stays thin-provisioned until a peer
disconnects and writes start dirtying bits, so the pre-baking is free
in steady state. Raising max-peers above 7 later is the only operation
that needs downtime; 7 is the design ceiling for v1.0.

### Cluster-singleton DRBD resource is capped at 3 peers

The cluster-singleton DRBD resource (`bedrock-data-cluster` — the
LV pair holding rqlite + filer DB + S3 IAM) is **capped at exactly
3 peers** once cluster size N ≥ 3, regardless of how big the cluster
gets. The standard 1-primary + 2-mirrors pattern is enough
redundancy for the arbiter; replicating to 7+ nodes synchronously
would slow every rqlite/filer write for no operational gain.

At cluster sizes:

| Cluster N | Cluster-DRBD peers        | Notes |
|-----------|---------------------------|-------|
| 1         | 1 (just this node)        | Single-instance; no DRBD attachment yet. |
| 2         | 2 (both nodes)            | Standard DRBD pair. |
| 3+        | 3 (lowest-octet 3 nodes)  | Nodes 4..N do NOT carry the cluster-DRBD resource. |

When one of the 3 arbiter-bearing nodes leaves, the cluster keeps
running with 2 arbiter peers (still HA, just below design
redundancy). At some later point the **calm orchestration loop**
promotes a non-arbiter node into the arbiter set: allocate its
cluster-DRBD LV pair, `drbdadm new-peer`, initial sync, mark it as
a candidate for `.254` ownership. Tracked in rqlite table
`cluster_drbd_membership(node_name, joined_at, updated_at)`.

**This promotion is NOT on the critical-failover path.** See
[Two-loop split](#two-loop-split-critical-vs-calm) below — picking
which node to promote requires resource-size evaluation (free space
in the local thinpool, current arbiter-set load, peer mesh health)
and lives in the mgmt orchestrator's slower deliberate loop, not
in netd's 1-Hz election tick.

## Everything goes through rqlite (except arbiter recovery)

**Design rule:** every long-running cluster operation — VM create,
disk grow, DRBD attach/detach, node join, cluster-DRBD
membership change, weed-master reshuffle, SeaweedFS replica
fix-up after a node returns, backup target rotation — is a
**saga in rqlite**:

1. The caller writes an `operations` row with `state='pending'`
   and the parameters as JSON.
2. The target node's calm orchestrator picks it up, transitions
   to `state='in_progress'`, and starts executing.
3. Each step writes its own `operation_steps` row to mark
   completion before moving on.
4. On the final step, the orchestrator writes
   `state='completed'`.

If a node loses power mid-saga, on next boot it queries rqlite for
`operations WHERE state IN ('pending','in_progress') AND target_node = self`,
inspects the `operation_steps` rows to find the last completed
step, and resumes from the next idempotent step. Every step is
designed to be either a no-op-if-already-done or a clean retry.

**The ONE exception is arbiter recovery.** rqlite itself is what's
being recovered when `.254` needs to fail over — so the takeover
protocol can't write to rqlite. It uses witness slots + local
commands only. See `cluster-quorum-spec.md`. Once the arbiter is
back up and rqlite has quorum, all other in-flight sagas resume
through the normal rqlite-driven path.

This boundary keeps the critical path tiny (witness + local
commands, deterministic in seconds) while everything else gets
crash-safe orchestration for free.

### Saga schema (rqlite)

```sql
CREATE TABLE IF NOT EXISTS operations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT NOT NULL,     -- e.g. "vm_create", "disk_grow",
                                     --      "cluster_tier_promote",
                                     --      "weed_master_reshuffle"
    target_node   TEXT,              -- node that runs this; NULL = any
    params        TEXT NOT NULL,     -- JSON payload
    state         TEXT NOT NULL,     -- 'pending'|'in_progress'|
                                     -- 'completed'|'failed'
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL,
    error         TEXT
);

CREATE TABLE IF NOT EXISTS operation_steps (
    op_id        INTEGER NOT NULL,
    step_name    TEXT NOT NULL,      -- e.g. "lvcreate_data", "drbd_create_md"
    state        TEXT NOT NULL,      -- 'done'|'failed'
    started_at   INTEGER,
    finished_at  INTEGER,
    PRIMARY KEY (op_id, step_name)
);
```

The orchestrator owns these tables. Per-resource state tables
(`drbd_resources`, `cluster_drbd_membership`,
`seaweed_master_membership`) are the *outcomes* a saga writes;
in-flight progress lives in `operations` + `operation_steps`.

## Two-loop split: critical vs calm

Bedrock has two control loops with very different latency and
complexity budgets. Storage lifecycle work is partitioned between
them on purpose:

| Loop                  | Where                              | Cadence | Job                                                        | What it CAN'T do                                                |
|-----------------------|------------------------------------|---------|------------------------------------------------------------|------------------------------------------------------------------|
| **Critical / netd**   | `installer/lib/netd.py` (thread in bedrock-d) | 1 s     | Mesh probes, election, takeover, self-demote, route emission. Must be deterministic and complete in seconds. | Complex resource decisions; reading multiple rqlite tables; sizing logic. |
| **Calm / orchestrator** | `mgmt/orchestrator.py` (asyncio task in bedrock-d) | 10–30 s reconcile + revision-driven | Capacity planning; arbiter-set membership; weed-master Raft membership; thinpool growth; drbd_resources bookkeeping; VM placement decisions. Can read multiple rqlite tables, plan, choose, then act. | Anything on the failover-decision critical path — would risk blocking on resource enumeration when the cluster needs to self-demote on NoQuorum in seconds. |

The **takeover protocol** in `cluster-quorum-spec.md` runs entirely
in the critical loop: witness + local commands, no rqlite, no
sizing, no "pick the best candidate". The calm loop is what makes
the *next* takeover possible — it ensures a healthy arbiter set is
in place before the next failure.

Storage-lifecycle ownership by loop:

| Operation                                       | Loop      | Why                                                                                   |
|-------------------------------------------------|-----------|---------------------------------------------------------------------------------------|
| Election outcome (Leader / Follower / NoQuorum) | critical  | 1-Hz deterministic, no rqlite reads on the hot path.                                  |
| Takeover (drbdadm primary, mount, bind .254)    | critical  | Must complete in seconds with witness + local commands only.                          |
| Self-demote on NoQuorum                         | critical  | Same; stops services before any cluster-visible state change.                         |
| Set `mgmt_master` in rqlite (post-takeover bookkeeping) | critical | Single write after the local promote already succeeded; not a gate.                  |
| Pick replacement for departed arbiter peer      | **calm**  | Needs free-space scan, peer-mesh-health check, deliberate. Cluster runs degraded until done. |
| Promote replacement into arbiter set            | **calm**  | LV allocate + `drbdadm new-peer` + initial sync — minutes-long, not seconds.          |
| Re-shuffle weed-master Raft membership          | **calm**  | Same — deliberate placement decision, not time-critical.                              |
| Grow a thinpool                                 | **calm**  | Operator-triggered or capacity-watcher-triggered; never urgent.                        |
| Grow a VM disk (`bedrock vm grow`)              | **calm**  | Operator-driven; runs `lvextend` + `drbdadm resize` deliberately.                     |

The contract: the critical loop never blocks on a query that could
take longer than a few hundred ms, and never makes a sizing
decision. The calm loop never claims responsibility for keeping the
cluster up during a failure — it's reconciling toward a healthy
*future* state.

## `.254/32` cluster-singleton

One VIP, one set of singleton services, one current host:

```
.254/32 on lo (on whichever node is current arbiter-host)
├── rqlited-arbiter      :4011/:4012   (the 3rd-voter Raft member)
├── seaweedfs-filer      :8888         (POSIX namespace + S3 IAM in leveldb3)
└── mgmt HTTPS           :8443         (single-address operator/CLI entry)
```

`/var/lib/bedrock/cluster/` is the on-disk root; it's the mount of
`/dev/drbd1101` (the `cluster` singleton DRBD resource, minor 1101).
Filer's `leveldb3` dir, rqlite's data dir, and S3 IAM bucket files
all live under here, so one DRBD failover hands off everything
atomically.

Locked at all N including N=1. Even a single-node bootstrap binds
`.254`; this keeps client config (the FUSE mount target, the mgmt URL)
identical across cluster sizes — there's nothing to reconfigure when
the cluster grows from 1 to 2.

## SeaweedFS topology

```
.254  (cluster-singleton)
└── weed-filer    :8888    leveldb3 on the cluster singleton DRBD volume
                            (POSIX namespace + S3 IAM identities)

every regular node (NOT .254)
├── weed-master   :9333    Raft-3 across 3 deterministically-chosen nodes
│                          (lowest-octet 3 by default; tracked in rqlite
│                          table seaweed_master_membership)
├── weed-volume   :8080    bytes; one daemon per node bound 0.0.0.0
├── weed-s3       :8333    S3 API; one daemon per node bound 0.0.0.0
└── /mnt/bedrock           FUSE mount of .254:8888, uniform on every node
```

- **Filer** singleton on `.254`. Failover = DRBD primary handoff +
  filer restart on the new arbiter-host. Brief stall (~5 s);
  in-flight S3 requests retry.
- **Master** is a fixed Raft-3 group on three regular cluster nodes
  (NOT bound to `.254`). This keeps weed-master quorum independent
  of `.254` arbiter handovers — moving `.254` for maintenance
  doesn't affect master availability. Membership stored in rqlite;
  re-shuffled when a master-bearing node leaves.
- **Volume** + **S3** on every node. Volume servers bind
  `0.0.0.0:8080`, S3 binds `0.0.0.0:8333`. mDNS resolves
  `bedrock.local` to any UP node so external backup clients have a
  stable DNS name without a LAN VIP.

### Three collections

Different storage classes are expressed as SeaweedFS *collections*
on a single weed-volume per node — NOT separate thinpools, NOT
separate clusters:

| Collection | Replication code | Copies | Behaviour at smaller N |
|------------|------------------|--------|-------------------------|
| `scratch`  | `000`            | 1 total | Always single-copy (no redundancy). Lost on the hosting node's failure. |
| `standard` | `001`            | 2 (different server) | N=1: collapses to 1 copy on the only node. N≥2: 2 copies on different nodes. |
| `critical` | `002`            | 3 (different servers) | N=1: 1 copy. N=2: 2 copies. N≥3: 3 copies. |

**Single-node behaviour** is intentional: a 1-node cluster keeps
working with whatever copies the topology allows; a write that
requests "2 extra copies" on a 1-node cluster lands on the one
available volume server. SeaweedFS doesn't refuse the write; it
just under-replicates. When more nodes join, the calm orchestrator
runs `volume.fix.replication` in `weed shell` to bring the
extra copies up to policy.

Replication code `XYZ` reads as:
`X` extra copies in other datacenters · `Y` extra copies in other
racks · `Z` extra copies on other servers (same rack). v1.0 uses no
rack tag — every node is in the default rack — so the `Z` component
is what does the work. Operators with multi-rack sites can add rack
tags later and bump to `010` for cross-rack diversity without changing
the spec.

### FUSE mount layout (every node)

```
/mnt/bedrock
├── scratch/      → bucket scratch     (collection: scratch)
├── iso/          → bucket iso         (collection: standard)
├── templates/    → bucket templates   (collection: standard)
├── snapshots/    → bucket snapshots   (collection: standard)
└── backups/      → bucket backups     (collection: critical)
```

A single `weed mount -filer=<vip>:8888 -dir=/mnt/bedrock` systemd unit
on every node. The mount target is identical everywhere because the
filer IS at `.254` everywhere.

### S3 IAM identities

Stored INSIDE the filer DB via `weed s3 -iam.filerBucketsPath=/buckets`.
That means bucket policies, access keys, and per-bucket ACLs all live
in the same leveldb3 that holds the POSIX namespace, on the same
DRBD-replicated `/var/lib/bedrock/cluster/seaweedfs/`. One backup
target captures the entire S3 identity surface. No sidecar JSON.

### `bedrock.local` external endpoint

External systems (e.g. customer kopia targets, backup appliances on
the LAN) use `bedrock.local` as their S3 endpoint hostname.
Resolution: mDNS responder on every node advertises the hostname; any
UP node answers. Internal callers on a Bedrock node itself can use
`127.0.0.1:8333` (the local S3 daemon). Off-site backup targeting
configures a separate kopia/restic repo against an external endpoint
— that's a per-customer policy, not a Bedrock concern.

## Port map (one place)

| Service           | Port | Bind          | Notes                                   |
|-------------------|------|---------------|-----------------------------------------|
| mgmt HTTPS        | 8443 | `0.0.0.0`     | Operator UI + LAN API (operator-authed); reached at `.254` |
| mgmt local HTTP   | 8001 | `127.0.0.1`   | local CLI / intra-process (auth-exempt) |
| rqlited per-node  | 4001 / 4002 | node loopback | HTTP API (4001, HTTPS mTLS) + Raft (4002) |
| rqlited arbiter   | 4011 / 4012 | `.254`  | 3rd-voter Raft member (HTTPS mTLS + Raft) |
| weed-master       | 9333 | node loopback | Raft-3 across selected nodes            |
| weed-volume       | 8080 | `0.0.0.0`     | bytes; every node                       |
| weed-filer        | 8888 | `.254`        | namespace + IAM; singleton              |
| weed-s3           | 8333 | `0.0.0.0`     | S3 API; every node                      |
| bedrock-echo      | 12321 (UDP) | LAN  | witness K/V (passive; ChaCha20-Poly1305 AEAD) |
| netd probes       | 7732 (UDP/mcast) | 239.7.7.7 | mesh discovery (HMAC-SHA256)    |
| DRBD              | 7700-7799 | per-link IP | per-resource ports (7700 + minor − 1100) |

The mgmt dashboard/API is **HTTPS on 8443** (plus the loopback-only
HTTP CLI listener on 8001). The earlier 8080 collision (mgmt vs
weed-volume) is closed: weed-volume owns 8080.

## rqlite schema (storage-related tables)

Abridged; the source of truth is
[`installer/lib/bedrock_schema.sql`](../installer/lib/bedrock_schema.sql).

```sql
-- One row per DRBD resource (cluster singleton + per-VM)
CREATE TABLE IF NOT EXISTS drbd_resources (
    name              TEXT PRIMARY KEY,    -- e.g. "cluster", "vm-foo-disk0"
    minor             INTEGER NOT NULL,    -- DRBD minor number (cluster = 1101)
    data_lv           TEXT NOT NULL,       -- e.g. "bedrock-data-vm-foo-disk0"
    meta_lv           TEXT NOT NULL,       -- e.g. "bedrock-meta-vm-foo-disk0"
    thinpool          TEXT NOT NULL DEFAULT 'thinpool',  -- one pool per node
    data_size_bytes   INTEGER NOT NULL,
    meta_size_bytes   INTEGER NOT NULL,
    max_peers         INTEGER NOT NULL DEFAULT 7,
    peers             TEXT NOT NULL DEFAULT '[]',  -- JSON node_name set
    current_uuid      TEXT NOT NULL DEFAULT '',    -- last Primary DRBD UUID
    created_at        INTEGER NOT NULL,
    updated_at        INTEGER NOT NULL
);

-- Which nodes carry the cluster-singleton DRBD resource (capped at 3)
CREATE TABLE IF NOT EXISTS cluster_drbd_membership (
    node_name         TEXT PRIMARY KEY,
    joined_at         INTEGER NOT NULL,
    updated_at        INTEGER NOT NULL
);

-- Membership of the Raft-3 weed-master group
CREATE TABLE IF NOT EXISTS seaweed_master_membership (
    node_name         TEXT PRIMARY KEY,
    joined_at         INTEGER NOT NULL,
    updated_at        INTEGER NOT NULL
);
```

Both membership tables are managed by the orchestrator; nodes don't
write them directly.

## Lifecycle interactions (summary)

- **Cluster init (N=1)**: create the node thinpool; create the local
  weed-volume LV; the `cluster` singleton is just a directory on the
  root FS at `/var/lib/bedrock/cluster` (mode `local`, **no DRBD
  yet**); bind `.254`; start rqlite (single-node) + filer + S3 + mgmt;
  start weed-volume + weed-s3 on local; weed FUSE-mount
  `127.0.0.1:8888` → `/mnt/bedrock` (only N=1 special case for the
  mount target; flips to `.254:8888` once N≥2).
- **Cluster grow (N=2)**: the existing node promotes its local
  `cluster` directory onto DRBD — allocate `bedrock-data-cluster` +
  `bedrock-meta-cluster`, `drbdadm create-md --max-peers=7`,
  snapshot+restore the N=1 contents onto the DRBD volume. The new node
  creates its thinpool and `cluster` LV pair; `drbdadm new-peer`;
  initial sync; joins weed-volume + weed-s3 + weed-mount. The
  `cluster` singleton now has 2 peers.
- **Cluster grow (N=3)**: new node allocates the `cluster` LV pair;
  becomes the 3rd peer. weed-master Raft-3 group now full. rqlite
  promotes to 3-voter Raft.
- **Cluster grow (N>3)**: new node creates the thinpool; joins
  weed-volume + weed-s3 + weed-mount; does **NOT** get a `cluster`
  LV pair — the 3-peer cap (`cap_singleton_peers`) holds. Node is
  eligible for promotion into the arbiter set only if a current
  arbiter-bearer leaves.
- **Node leave (arbiter-bearer)**: orchestrator picks a replacement
  from the non-arbiter pool; new node allocates the LV pair, joins
  the `cluster` singleton, initial-sync. Leave is held until the new
  peer is UpToDate.
- **VM disk grow** (`bedrock vm grow`): `lvextend
  bedrock-meta-vm-foo` if needed, `lvextend bedrock-data-vm-foo`,
  `drbdadm resize` on all peers. Online, ~seconds.
- **VM disk destroy**: `drbdadm down`, `drbdadm wipe-md`, `lvremove
  data`, `lvremove meta`. One row deleted from `drbd_resources`.

## Out of scope (post-v1.0)

- Multi-witness quorum reads/writes (see
  `cluster-quorum-spec.md#out-of-scope`).
- Erasure coding for SeaweedFS bulk tier (today: replication-2 or
  replication-3 only).
- Cross-datacenter replication (the `X` digit of the replication
  code). Today every cluster is a single DC.
- Per-VM-disk DRBD-on-DRBD nesting (some XXL workloads might want
  data on fast tier + write-cache on faster tier; not v1.0).
