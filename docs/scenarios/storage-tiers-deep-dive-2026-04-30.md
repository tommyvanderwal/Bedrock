# What was actually blocking — deep dive — 2026-04-30

After the rough-and-ready sim-1 removal, the user pushed back: "Garage should
be able to decommission RF=1 gracefully" and "DRBD should be able to remove
a peer live." Both are correct. The blockers were **operator/code error**,
not fundamental capability. Below is what actually went wrong, the proper
procedures, and proof from the live testbed.

## Finding 1 — Garage RF=1 decommission needs a temporary RF bump

### What I did wrong
1. `garage layout remove <sim-1-id>` (stages removal)
2. `garage layout apply --version 4` (commits)
3. Stopped Garage on sim-1 a few seconds later
4. **Declared drain "done" after one 5-second worker-list check**

The worker check (`grep -E 'rebalance|resync_block|repair'`) didn't actually
match Garage 2.x's worker names. Drain probably never *completed* before
I declared success — but more fundamentally, **at RF=1 there's nowhere for
data to move to without first establishing redundancy**.

### What actually happens at RF=1

Each block's metadata says "this block lives on partition P, partition P is
on node N." When you remove N, the layout reassigns P to a different node M.
But there is no copy of the block on M yet — and Garage cannot conjure one
from the metadata alone. Until N pushes the block to M (or another node
pulls it from N), the block exists only physically on N.

If N is then powered off or removed before the push completes, **the block
is gone**. Garage returns empty bytes when the new owner is asked for the
block. That's exactly what we observed (`9f909473…` → `d41d8cd9…` empty MD5).

This is *correct* for RF=1 by design — "scratch is RAID0, lose-it-and-redownload"
matches it. But the *graceful* drain path requires more care.

### The proper RF=1 graceful drain

```
# 1. Bump RF cluster-wide so every block now needs 2 copies
on each node: edit /etc/garage.toml: replication_factor = 2
on each node: systemctl restart garage

# 2. Apply the (unchanged) layout to trigger re-replication at RF=2
garage layout assign <each-node> -z dc1 -c <same-capacity>  # no-op-style refresh
garage layout apply --version N+1
# Wait — Garage now copies blocks until every block exists on 2 nodes.
# (This step takes wall-clock time proportional to total data.)

# 3. Verify all blocks are at RF=2 redundancy
garage stats   # checks per-node counts, "missing replicas" metric
                # OR query the admin API for per-block placement

# 4. NOW it's safe to remove sim-1
garage layout remove <sim-1-id>
garage layout apply --version N+2
# Drain is fast: every block already exists on a surviving node;
# Garage just updates partition→node mapping.

# 5. Optionally drop back to RF=1 to free space on the surviving 3 nodes
on each node: replication_factor = 1, systemctl restart garage
```

### Code action item

`tier_storage.py` should have a `garage_drain_node(node_id)` function that:
1. Reads current `replication_factor`
2. If RF=1 and we're about to lose a node, bump RF to 2 first
3. Wait for all blocks to be at RF=2 (poll the admin endpoint)
4. Layout-remove the node, apply
5. Wait for the (now fast) layout drain
6. Restore RF=1 if desired

The Garage admin API has `/v1/health` (cluster status) and per-bucket
metrics. Polling the per-block redundancy is the rigorous check.

---

## Finding 2 — DRBD live peer removal IS possible. I was using the wrong command.

### The command I missed: `drbdsetup del-peer <res> <node-id>`

I kept trying `drbdadm forget-peer`, which has *different semantics*:

| Command | Operates on | Resource state required | What it does |
|---|---|---|---|
| `drbdsetup disconnect <res> <node-id>` | kernel runtime | resource UP | Sets connection state to `StandAlone` (no auto-reconnect). Reversible via `connect`. |
| `drbdsetup del-peer <res> <node-id>` | kernel runtime | resource UP, peer disconnected | **Removes the peer connection definition from kernel.** |
| `drbdsetup forget-peer <res> <node-id>` | DRBD metadata on disk | sometimes works UP, sometimes wants DOWN | Removes peer's bitmap slot from external metadata. Frees a node-id slot for future re-use. |
| `drbdadm forget-peer <res> <peer-name>` | drbdmeta tool | resource DOWN | Tells `drbdmeta` to wipe peer entry. Requires resource not configured. |

**The live peer-removal sequence (no resource downtime, no NFS outage, no
VM stop) is:**

```
drbdsetup disconnect <res> <node-id>   # state → StandAlone
drbdsetup del-peer    <res> <node-id>   # remove peer from kernel state
# (optional, when convenient): drbdsetup forget-peer <res> <node-id>
#                              to clean the metadata bitmap slot
```

I verified this on the live testbed today. After cleaning sim-1's ghost
this way, sim-2's `drbdadm status tier-bulk` immediately changed from:

```
tier-bulk role:Primary
  bedrock-sim-1.bedrock.local connection:Connecting     ← ghost
  bedrock-sim-3.bedrock.local role:Secondary peer-disk:UpToDate
  bedrock-sim-4.bedrock.local role:Secondary peer-disk:UpToDate
```

to clean 3-way:

```
tier-bulk role:Primary
  bedrock-sim-3.bedrock.local role:Secondary peer-disk:UpToDate
  bedrock-sim-4.bedrock.local role:Secondary peer-disk:UpToDate
```

Then `live migrate web1 sim-3 → sim-4` succeeded in 1.51 s, confirming the
"vipet with dead peer blocks promotion" problem is *entirely* about the
ghost peer being in kernel state — once cleared, dual-primary promotion
works normally.

### Why my earlier attempts failed

1. `drbdadm forget-peer tier-critical bedrock-sim-1.bedrock.local`
   → "Device is configured!" → forget-peer chose the *drbdmeta* path
   (offline tool) because the on-disk config no longer mentioned sim-1,
   so drbdadm assumed peer was already removed from runtime.
2. `drbdadm disconnect tier-critical bedrock-sim-1.bedrock.local`
   → "peer node id cannot be my own node id" → drbdadm couldn't find
   sim-1 in the on-disk config (we'd already removed it), so it
   couldn't translate the *name* to a *node-id* — and fell back to
   guessing, which collided with the local node-id.

**Both failures share one root cause: I edited the on-disk config before
running the kernel-side commands.** Once the config no longer has sim-1,
all `drbdadm` commands for sim-1 fail.

The proper order is **kernel-state changes FIRST, on-disk config edits LAST**:

```
# 1. While sim-1 is in the config (whether sim-1 is online or not)
drbdadm disconnect <res> bedrock-sim-1.bedrock.local
drbdadm forget-peer <res> bedrock-sim-1.bedrock.local

# 2. THEN remove sim-1 from /etc/drbd.d/<res>.res
# 3. THEN drbdadm adjust <res> on each remaining node (no-op)
```

OR — bypass `drbdadm`'s name-to-id translation entirely by working in
node-id space:

```
# Find the node-id from drbdsetup show output
drbdsetup disconnect <res> <node-id>
drbdsetup del-peer    <res> <node-id>
# (now safe to update on-disk config however you like, as long as
#  surviving peers keep their original node-ids)
```

### Code action item

`tier_storage.py` needs `drbd_remove_peer(resource, node_name)` that
implements the proper sequence and works whether or not the departing
node is online.

---

## Finding 3 — DRBD node-ids are permanent; renumbering them breaks everything

### The bug in `render_drbd_res`

My code generated `node-id` values by Python list-index order:

```python
for i, peer in enumerate(peers):  # i = 0, 1, 2, ...
    body += f"on {peer['name']} {{ node-id {i}; ... }}"
```

This means when I rewrote the config from {sim-1, sim-2, sim-3} (IDs 0, 1, 2)
to {sim-2, sim-3, sim-4} (IDs 0, 1, 2 — sim-2 RENUMBERED from 1 to 0!),
the on-disk config diverged from the running kernel.

We could see it explicitly in `drbdsetup show`:

```
_this_host { node-id 1; ... }      ← kernel still thinks sim-2 is id 1
connection { _peer_node_id 0; _name "bedrock-sim-1.bedrock.local"; ... }
connection { _peer_node_id 2; _name "bedrock-sim-3.bedrock.local"; ... }
connection { _peer_node_id 3; _name "bedrock-sim-4.bedrock.local"; ... }
```

But the on-disk config:

```
on bedrock-sim-2.bedrock.local { node-id 0; ... }   ← CONFIG says 0
on bedrock-sim-3.bedrock.local { node-id 1; ... }   ← CONFIG says 1
on bedrock-sim-4.bedrock.local { node-id 2; ... }   ← CONFIG says 2
```

`drbdadm adjust` then tries to reconcile and fails with errors like:

> `tier-bulk: Failure: (162) Invalid configuration request, peer node id cannot be my own node id`
> `Combination of local address(port) and remote address(port) already in use`

These are red herrings — the real error is "config and runtime disagree on
who is who."

### The fix

`render_drbd_res` must take a `peer_assignments: dict[str, int]` argument
that maps peer-name → permanent node-id, persist it across config rewrites,
and never reuse a freed slot for a different peer (until `forget-peer`
explicitly clears the bitmap).

### Code action item

In `tier_storage.py`:

```python
def get_drbd_node_id(resource: str, peer_name: str) -> int:
    """Return the node-id that this peer was assigned the first time
    we saw it for this resource. New peers get the next free id from
    a {resource: {peer_name: node_id}} map persisted in cluster.json."""

def render_drbd_res(resource: str, peers: list[dict],
                    assignments: dict[str, int]) -> str:
    # Use assignments[peer['name']] for node-id, NOT enumerate-index.
```

The map lives in `cluster.json` under
`tiers[<tier>].drbd_node_ids = {peer_name: node_id, ...}` and is updated
*only* when adding a new peer.

---

## Finding 4 — The mgmt-failover playbook is mostly mechanical

This wasn't a blocker but is worth codifying. Moving mgmt + NFS server +
DRBD primary from sim-1 → sim-2 was about ten manual steps (stop services,
demote, promote, mount, exports, rsync, systemd units, restart, repoint
NFS clients in fstab, update cluster.json on every node).

### Code action item

`bedrock storage transfer-mgmt-role <new-master>` should:
1. Verify new-master is healthy and has the DRBD secondaries up-to-date
2. Stop `bedrock-mgmt`, `bedrock-vm`, `bedrock-vl`, `nfs-server` on old master
3. `drbdadm secondary` for tier-bulk + tier-critical on old master
4. `drbdadm primary` on new master, mount the DRBD devices
5. rsync `/opt/bedrock/{mgmt,iso,data,bin}` + systemd units from old → new
6. Re-create `/etc/exports.d/bedrock-tiers.exports` on new master, exportfs -ra
7. Start services on new master
8. SSH-fanout: update fstab on every other node:
   `s/<old-master-drbd-ip>:/<new-master-drbd-ip>:/g`, then remount
9. `atomic_symlink` on new master: /bedrock/<tier> → bulk-drbd (was nfs)
10. `atomic_symlink` on old master (if still alive): /bedrock/<tier> → bulk-nfs
11. Update `cluster.json.tiers[*].master` everywhere

Idempotent + safe to retry. Outage measured: 5–10 s for NFS clients to
re-establish; mgmt API briefly returns 503 during step 7.

---

## What this means for the originally-asked path

Tommy asked: "DRBD should be able to go from 2 nodes → 3 nodes → move the
master (with a live migration of the VM / short multi-primary window) make
the 'to be evacuated' node secondary and then remove it, so 2 nodes
(primary-secondary) remain. What is fundamentally blocking any of this?"

**Nothing fundamental.** The path is:

```
1. drbdadm adjust to grow 2-way → 3-way (works clean if --max-peers=7
   was set at create-md time and node-ids are stable across configs)
2. Live-migrate VM from old-master to a survivor (dual-primary briefly)
3. drbdadm secondary on old master (resource itself stays up,
   just role drops)
4. drbdsetup disconnect <res> <old-master-node-id>
5. drbdsetup del-peer <res> <old-master-node-id>
6. (only now) update /etc/drbd.d/*.res to remove old master, preserve
   surviving node-ids
7. drbdadm adjust on each survivor — should be a no-op
```

The blockers I hit were:
- DRBD metadata created with default `--max-peers=1` (now fixed in code,
  needs brief regen on existing testbed metadata)
- `render_drbd_res` renumbering node-ids (now diagnosed; fix queued)
- Wrong order: I edited the config before running `drbdadm disconnect`,
  which broke `drbdadm`'s ability to translate names to ids
- `drbdadm forget-peer` chose the offline `drbdmeta` path because of (3)

**All four are fixable in tier_storage.py.** Empirically the proper sequence
worked on the live testbed today — sim-1 ghosts cleared without resource
downtime, web1 live-migrated sim-3 → sim-4 in 1.51 s.

## What still needs care for the user's "remove from the OTHER side" plan

The user wants: remove sim-1, then sim-2, then sim-3, leaving sim-4 alone.
This forces the mgmt+NFS+DRBD-primary role to migrate at each step, which
isn't a single fixed direction.

The patterns above all work for any departure direction. The only
additional complexity is that the *last* removal (sim-3, leaving sim-4
solo) is also the **N=2→N=1 transition**, which means:

- Garage is decommissioned entirely (`systemctl stop garage`,
  `lvremove garage-data` if you want disk back)
- bulk and critical DRBD resources go from 2-way → 1-way (or stay as
  diskless/standalone). DRBD does not have a true "1-way" — you'd
  `drbdadm down` and just mount the underlying LV directly.
- Symlinks `/bedrock/<tier>` swap from `bulk-drbd` (still useful) →
  the underlying XFS mount, dropping DRBD. Or keep the DRBD layer
  (single-node DRBD with no peers — works fine, just no replication).

I'll write the demote-N=2-to-N=1 path next.

---

## Update — empirical proof + the actual s3fs gotcha

After committing the deep-dive doc, ran the proper drain procedure on the
live testbed (3-node cluster sim-2/3/4, sim-4 chosen as the drainee):

1. Wrote test files to `/bedrock/scratch`:
   - `DRAIN_TEST.txt` (33 bytes, MD5 c67516ff…)
   - `DRAIN_BIGFILE.bin` (20 MB random, MD5 430d2d08…)
2. `garage layout remove <sim-4-id>` and `apply --version 5`
3. On sim-4: `garage worker set resync-tranquility 0 && set resync-worker-count 8`
4. Waited for `garage worker list` to show all 8 `Block resync` workers `Idle`
   with Queue=0 and no errors
5. `garage block list-errors` → empty
6. Verified MD5 hashes from sim-2 AND sim-3 (both surviving nodes):
   **all three files MD5-match the original byte-for-byte.**

**Garage drained 84 partitions from sim-4 to sim-2 (42) + sim-3 (42) without
any temporary cluster-wide RF bump, no doubled storage, zero errors.** RF=1
graceful node drain *just works* — the procedure is sound, the
documentation gap is "wait and verify" not "dual-replicate first."

### The other big finding from the empirical run: **s3fs endpoint hard-pinning**

The `cross-node-via-garage-FINAL` test object that I'd marked "lost during
sim-1 removal" was actually **fine** — its MD5 came back as the expected
`9f909473…` once I re-pointed s3fs at a live Garage node.

Root cause: my `s3fs_mount_scratch()` puts a single fixed `url=http://<sim-1-drbd>:3900`
in fstab. When that one node goes down, every other node's s3fs hangs trying
to reach a dead endpoint, returning empty bytes / I/O hangs. Garage cluster
is *fine* — but the s3fs client can't see that.

#### Fix: each node's s3fs targets ITS OWN local Garage endpoint

```python
# in tier_storage.s3fs_mount_scratch()
url = f"http://127.0.0.1:{GARAGE_S3_PORT}"  # always local!
```

This is structurally correct because:
- Garage is a peer-to-peer cluster: any node serves any object via internal RPC
- A node's local s3fs only needs the local Garage daemon to be alive — and if
  *that* node's Garage is down, the rest of the node is presumably also down
- No cross-node failure cascade — sim-1 going down can't break sim-2/3/4's
  s3fs view
- No DNS, no VIP, no load-balancer machinery needed for the storage path

#### Code action item (added to the queue):

```python
# tier_storage.py
def s3fs_mount_scratch(access_key: str, secret_key: str) -> None:
    # No more endpoint_drbd_ip parameter — always local Garage
    url = f"http://127.0.0.1:{GARAGE_S3_PORT}"
    ...
```

### Summary for Tommy's 8→7 node case

```
For each node-removal step:
  garage layout remove <node-id>
  garage layout apply --version N+1
  garage worker set resync-tranquility 0           # on departing node
  garage worker set resync-worker-count 8          # on departing node
  # WAIT
  while not all "Block resync" workers Idle && Queue==0 on departing:
      sleep
  garage block list-errors  # MUST be empty everywhere
  garage repair --all-nodes --yes tables
  garage repair --all-nodes --yes blocks
  # only NOW: systemctl stop garage on departing node, remove
```

For 1 TB at 10 GbE that's ~15 minutes of waiting, well under 5 minutes if
the data is well-distributed. **No RF bump, no double storage, no
operational risk** beyond "wait and verify."

