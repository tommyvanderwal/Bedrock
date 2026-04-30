# Clean run plan — N=0 → N=1 → … → N=4 → N=3 → … → N=1 (sim-4 alone)

**Status:** PLAN. Not yet executed. Once executed, this document will be
appended with results section and any deviations encountered.

## Goal

Validate the full lifecycle of the Bedrock 3-tier storage architecture
on a clean testbed, using the helpers we just landed
(`drbd_remove_peer`, `garage_drain_node`, `transfer_mgmt_role`,
persistent DRBD node-ids, s3fs-to-localhost). Forward path proves growth
works as designed; reverse path proves the helpers do what their docs
say without partial-state leftovers.

The reverse direction is *intentionally* "shrink from the OTHER side"
— removing sim-1 first, then sim-2, then sim-3 — so the mgmt + NFS-server
+ DRBD-primary role transfers between nodes at every shrink step. This
exercises `transfer_mgmt_role` three times instead of once, and ensures
no path through the codebase is "always sim-1 is master."

End state: only `sim-4` (the LAST node added) remains. Garage is
decommissioned entirely. bulk + critical demoted from DRBD to local LV.
Cluster is functionally identical to a fresh single-node N=1 install.

## Why a clean run instead of cleaning up the existing testbed

The current testbed has accumulated state from many trial-and-error
sessions: DRBD metadata regenerated multiple times, kernel state
diverged from on-disk config (the node-id renumbering bug), NFS mount
leftovers, possibly orphan LVs. Any "fix in place" approach would mix
operator cleanup work with actual code-validation, making it hard to
say "the helpers worked" vs "we manually patched around a stuck state."

A clean run starting from `spawn.py reset` produces unambiguous
evidence about whether the code-as-shipped does what its docs claim.

## Pre-flight code fix (still missing from branch)

L6 in the lessons-log calls for s3fs to point at the local Garage
daemon (`url=http://127.0.0.1:3900`). The fix is documented in
`tier_storage.md` invariant #6 but the **code still passes a remote
DRBD IP** in `s3fs_mount_scratch()` callers. Apply this BEFORE the
testbed reset so the install repo serves the correct version.

Specifically:

1. `tier_storage.s3fs_mount_scratch()` — drop the `endpoint_drbd_ip`
   parameter; hardcode `url=http://127.0.0.1:3900` in the fstab line.
2. `installer/bedrock` — drop the `--endpoint-drbd` flag from
   `_peer-s3fs`; drop the `self_drbd` argument from the `promote`
   subcommand's `s3fs_mount_scratch()` call.
3. `tier_storage.finalize_n2_garage()` — accepts but ignores the
   endpoint argument (kept for backward-compat with bedrock CLI; the
   value is no longer threaded through to s3fs).

Compile-check after edits.

## Tier mode and DRBD topology at each cluster size (target state)

| N | scratch    | bulk            | critical        | mgmt+NFS  | web1 (DRBD)   | DRBD config notes                |
|---|------------|-----------------|-----------------|-----------|---------------|----------------------------------|
| 1 | local LV   | local LV        | local LV        | sim-1     | cattle (local LV) | no DRBD                          |
| 2 | Garage RF=1 (2 vol) | DRBD 2-way + NFS | DRBD 2-way + NFS | sim-1     | pet (2-way: sim-1+sim-2) | meta-LV `--max-peers=7`         |
| 3 | Garage RF=1 (3 vol) | DRBD 2-way + NFS | DRBD 3-way + NFS | sim-1     | pet (2-way: sim-1+sim-2) | sim-3 joins critical; bulk stays |
| 4 | Garage RF=1 (4 vol) | DRBD 2-way + NFS | DRBD 3-way + NFS | sim-1     | pet (2-way: sim-1+sim-2) | sim-4 only joins as Garage+NFS-client |
| 3 (post-sim-1-removal) | Garage RF=1 (3 vol) | DRBD 2-way + NFS | DRBD 3-way + NFS | **sim-2** | pet (2-way: sim-2+sim-3) | mgmt failed over to sim-2; sim-4 joined critical |
| 2 (post-sim-2-removal) | Garage RF=1 (2 vol) | DRBD 2-way + NFS | DRBD 2-way + NFS | **sim-3** | pet (2-way: sim-3+sim-4) | mgmt failed over to sim-3; sim-4 in bulk now |
| 1 (post-sim-3-removal) | local LV (Garage gone) | local LV (DRBD gone) | local LV (DRBD gone) | **sim-4** | cattle | DRBD demoted; full local-only mode |

Two key transitions on the way down:

1. **Pre-shrink capacity additions.** Before removing any node, we add
   it as a peer somewhere else first so quorum + redundancy survive
   the removal. e.g. before removing sim-1 from the cluster, add
   sim-3 to web1's DRBD (pet→vipet) and add sim-4 to critical's DRBD
   (3-way → 4-way temporarily). After sim-1 leaves, the resource
   has its desired peer count (web1: 2-way, critical: 3-way).

2. **Final demote at N=2→N=1.** `drbd_remove_peer` leaves a single-
   peer DRBD resource on the survivor — functional but pointless.
   We then bring DRBD down on the survivor and mount the underlying
   LV directly. External metadata makes this byte-for-byte preserving
   (same mechanism that made local→DRBD promotion zero-copy).

## Step-by-step plan

Each phase has: (a) what to do, (b) verification, (c) what to record.

---

### Phase 0 — pre-flight code fix + commit + reset

```
0.1  Edit tier_storage.s3fs_mount_scratch — drop endpoint param
0.2  Edit installer/bedrock — drop endpoint plumbing for s3fs
0.3  python3 -m py_compile installer/lib/tier_storage.py
0.4  git commit + push (so review can see the diff)
0.5  ./testbed/spawn.py reset
0.6  verify: virsh list --all shows no bedrock-sim-*
0.7  verify: testbed/state/ has only state.json (or empty)
```

Record: commit hash of the s3fs fix.

---

### Phase 1 — N=1 fresh install

```
1.1   ./testbed/spawn.py up 1
1.2   wait for sim-1 to boot + ssh
1.3   ssh sim-1 'curl -sSL http://192.168.100.1:8000/install.sh | bash'
1.4   ssh sim-1 'bedrock init --name bedrock-test'
1.5   ssh sim-1 'bedrock storage status'    → expect 3 tiers all "local"
1.6   write SENTINEL files:
        for tier in scratch bulk critical:
          ssh sim-1 "echo 'sentinel-$tier-$(date -Iseconds)' > /bedrock/$tier/SENTINEL.txt"
        record MD5s
1.7   prepare ISO library:
        ssh sim-1 'mkdir -p /bedrock/bulk/iso /bedrock/bulk/templates'
        scp /tmp/alpine-virt-3.20.iso  sim-1:/bedrock/bulk/iso/
        scp /tmp/cirros.qcow2          sim-1:/bedrock/bulk/templates/
1.8   create cattle VM:
        ssh sim-1 'bedrock vm create web1 --type cattle --ram 256 --disk 2'
1.9   verify VM:
        ssh sim-1 'virsh list --all'   → web1 running
1.10  write VM-data sentinel inside web1 if accessible (Alpine cloud-init may
      need ssh; if not, skip and use VM uptime as the proof of life)
```

**Verification gate:** `bedrock storage status` shows 3 tiers in
`local` mode; web1 in `virsh list` is running; `md5sum
/bedrock/*/SENTINEL.txt` returns 3 distinct hashes that we record for
later comparison.

**What to record:**
- 3 SENTINEL MD5s (will compare across all subsequent transitions)
- web1's qcow2 location + cluster.json snapshot
- Any boot-time warnings or unexpected service states

---

### Phase 2 — N=1 → N=2 (sim-2 joins)

```
2.1   ./testbed/spawn.py up 2
2.2   wait sim-2 boot + ssh
2.3   ssh sim-2 'curl -sSL http://192.168.100.1:8000/install.sh | bash'
2.4   ssh sim-2 'bedrock join --witness <sim-1> --yes'
2.5   verify sim-2 has its own /bedrock/{scratch,bulk,critical} as local LVs
2.6   SSH key mesh between sim-1 ↔ sim-2 (root):
        ssh-key-exchange both directions; ssh-keyscan into known_hosts
2.7   ssh sim-1 'bedrock storage promote'
        → installs Garage on both, sets up DRBD bulk+critical 2-way,
          NFS export from sim-1, NFS mount on sim-2, Garage cluster
          formed, scratch bucket created, s3fs mounts (LOCAL Garage
          per the L6 fix) on both
2.8   wait for DRBD initial syncs to complete (poll drbdadm status)
2.9   verify all 3 tiers from BOTH nodes:
        for node in sim-1 sim-2:
          ssh $node 'md5sum /bedrock/{scratch,bulk,critical}/SENTINEL.txt'
        all hashes must match phase 1.6 record
2.10  verify cattle web1 still running on sim-1 (was running before promote,
      promote should not have affected it)
2.11  convert cattle → pet:
        ssh sim-1 'curl -X POST -d "{\"target_type\":\"pet\"}" \
          -H "Content-Type: application/json" \
          http://localhost:8080/api/vms/web1/convert'
        poll task → "succeeded"
2.12  live migrate sim-1 → sim-2:
        curl -X POST -d "{\"target_node\":\"<sim-2-fqdn>\"}" \
          http://sim-1:8080/api/vms/web1/migrate
        record duration_s (expect ~1 s)
2.13  live migrate back sim-2 → sim-1
```

**Verification gate:** all 3 SENTINELs MD5-match across both nodes;
`drbdadm status` shows tier-bulk + tier-critical as 2-way UpToDate;
Garage cluster shows 2 healthy nodes; web1 successfully migrated both
directions.

**What to record:**
- DRBD initial-sync duration for bulk + critical
- Garage cluster status output
- Both live-migration duration_s values
- Final state of web1 (which node, DRBD roles)
- any warnings from `bedrock storage promote`

---

### Phase 3 — N=2 → N=3 (sim-3 joins; critical to 3-way)

```
3.1   ./testbed/spawn.py up 3
3.2   sim-3 install + bedrock join
3.3   SSH key mesh sim-3 ↔ sim-1 ↔ sim-2 (full mesh)
3.4   extend Garage to 3 nodes:
        get sim-3 garage node id
        garage node connect (from any existing member)
        layout assign sim-3 -z dc1 -c 16G
        layout apply --version <next>
        wait for resync workers Idle on all nodes
3.5   promote tier-critical 2-way → 3-way:
        sim-3 already has tier-critical LV (from setup_n1 on join)
        write 3-peer .res file using render_drbd_res (persistent node-ids!)
        distribute to all 3 nodes
        on sim-3: ensure_meta_lv, drbdadm create-md --max-peers=7, drbdadm up
        on sim-1 + sim-2: drbdadm adjust (no-op for them; just picks up sim-3)
        wait for sim-3 sync to UpToDate
3.6   sim-3 NFS-mount + s3fs-mount:
        nfs_mount_drbd_tiers(<sim-1's drbd ip>) — bulk + critical
        s3fs_mount_scratch(...) — scratch via local Garage
3.7   verify all 3 tiers from sim-3:
        md5sum /bedrock/{scratch,bulk,critical}/SENTINEL.txt
        all hashes must match phase 1.6 record
3.8   live migrate web1 sim-1 ↔ sim-2 ↔ ?  
      web1 is still 2-way pet (sim-1 + sim-2). Cannot migrate to sim-3
      yet. Keep it as-is for phase 3; phase 5 will upgrade it.
```

**Verification gate:** bulk DRBD still 2-way; critical DRBD now 3-way
all UpToDate; Garage layout shows 3 healthy nodes with rebalanced
partitions; sim-3 sees all 3 SENTINELs with matching MD5.

**What to record:**
- critical 3-way sync duration
- Garage layout rebalance partition count + duration
- whether `drbdadm adjust` succeeded cleanly (this is the test of
  the persistent-node-id fix; with the fix, adjust should be a clean
  no-op on sim-1 and sim-2)

---

### Phase 4 — N=3 → N=4 (sim-4 joins as Garage volume + NFS client)

```
4.1   ./testbed/spawn.py up 4
4.2   sim-4 install + bedrock join
4.3   SSH key mesh sim-4 ↔ {sim-1, sim-2, sim-3} (full mesh)
4.4   extend Garage to 4 nodes (same pattern as 3.4)
4.5   sim-4 NFS-mount + s3fs-mount (NO new DRBD peer for bulk/critical)
4.6   verify all 3 tiers from sim-4
4.7   live migrate web1 sim-1 ↔ sim-2 once more for confidence
```

**Verification gate:** Garage 4 healthy nodes, balanced; sim-4 sees
all 3 SENTINELs with matching MD5; web1 migrates ~1 s.

**What to record:**
- Garage 3→4-node rebalance behaviour
- sim-4's view confirms cluster-wide read availability
- snapshot of `bedrock storage status` — this is "peak cluster" baseline

---

### Phase 5 — N=4 → N=3 (REMOVE sim-1; mgmt → sim-2)

This is the first exercise of the new helpers in this clean run.

```
5.1   PRE-CAPACITY: live-migrate web1 to sim-2 if currently on sim-1
5.2   PRE-CAPACITY: convert web1 pet → vipet
        adds sim-3 as 3rd DRBD peer; web1 now sim-1+sim-2+sim-3 3-way
        wait for sim-3 sync
5.3   PRE-CAPACITY: add sim-3 to tier-bulk (2-way → 3-way temporarily)
        write 3-peer .res, distribute, on sim-3 ensure_meta_lv +
        create-md --max-peers=7 + up; drbdadm adjust on sim-1, sim-2
        wait for sim-3 to reach UpToDate
5.4   PRE-CAPACITY: add sim-4 to tier-critical (3-way → 4-way temporarily)
        same pattern as 5.3 but for sim-4 + critical
        wait for sim-4 to reach UpToDate
5.5   ROLE TRANSFER: tier_storage.transfer_mgmt_role(
            old_master_host=sim-1,
            new_master_host=sim-2,
            new_master_drbd_ip=10.99.0.11,
            other_peer_hosts=[sim-3, sim-4],
        )
      verifies UpToDate, stops services on sim-1, demotes DRBD,
      promotes on sim-2, mounts, rsyncs /opt/bedrock, copies
      systemd units, sets up NFS exports, starts services on sim-2,
      re-points NFS clients on sim-3+sim-4, swaps symlinks,
      updates cluster.json everywhere
5.6   GARAGE DRAIN: tier_storage.garage_drain_node(
            departing_node_id_short=<sim-1's 16-char id>,
            surviving_admin_host=sim-2,
            departing_node_admin_host=sim-1,
        )
      stages layout-remove, applies, watches workers idle on sim-1,
      verifies block list-errors empty, runs cluster-wide repair,
      stops garage on sim-1
5.7   DRBD PEER REMOVAL: for resource in (vm-web1-disk0, tier-bulk, tier-critical):
        compute surviving_peers (everyone except sim-1)
        compute surviving_hosts (everyone except sim-1)
        tier_storage.drbd_remove_peer(
            resource, leaving_peer_name="<sim-1-fqdn>",
            surviving_peers, surviving_hosts,
        )
5.8   POWER OFF sim-1:
        sudo virsh shutdown bedrock-sim-1
5.9   VERIFY:
        bedrock storage status (from sim-2)  → 3 nodes, master=sim-2
        drbdadm status (each remaining)       → no sim-1 ghosts
        garage status (from sim-2)            → 3 healthy nodes
        md5sum SENTINELs from sim-2/sim-3/sim-4 → all match phase 1.6
        live migrate web1 sim-2 ↔ sim-3       → ~1 s
```

**Verification gate:**
- 3 SENTINEL MD5s still match the original (preserved through phase 5)
- web1 still running, can live-migrate between sim-2 ↔ sim-3
- `drbdadm status` on every remaining node shows no `sim-1` references
  (this is the test that drbd_remove_peer cleanly removed peer state)
- `garage status` shows exactly 3 healthy nodes
- mgmt API accessible at sim-2:8080 (this is the test that
  transfer_mgmt_role succeeded)
- `cluster.json.tiers.{bulk,critical}.master` = sim-2's name
  everywhere

**What to record:**
- Full timeline of phase 5 (per-step duration)
- Any errors from any of the three helpers (log to scenario doc)
- Final state diff vs phase 4 baseline

---

### Phase 6 — N=3 → N=2 (REMOVE sim-2; mgmt → sim-3)

Same shape as phase 5, but the master moves sim-2 → sim-3 and the
peer set shrinks more.

```
6.1   PRE-CAPACITY: live-migrate web1 to sim-3 (off sim-2)
6.2   PRE-CAPACITY: web1 currently 2-way pet (sim-2+sim-3) — add sim-4
        as 3rd peer (pet → vipet): sim-2+sim-3+sim-4
        wait for sim-4 sync
6.3   PRE-CAPACITY: tier-bulk currently 2-way (sim-2+sim-3) — add sim-4
        as 3rd peer
        wait for sim-4 sync
6.4   PRE-CAPACITY: tier-critical currently 3-way (sim-2+sim-3+sim-4)
        — already has all peers; nothing to add
6.5   transfer_mgmt_role(sim-2 → sim-3, other_peer_hosts=[sim-4])
6.6   garage_drain_node(sim-2 → surviving sim-3, sim-4)
6.7   drbd_remove_peer(vm-web1-disk0, sim-2, [sim-3, sim-4])
6.8   drbd_remove_peer(tier-bulk, sim-2, [sim-3, sim-4])
6.9   drbd_remove_peer(tier-critical, sim-2, [sim-3, sim-4])
6.10  power off sim-2
6.11  verify: bulk 2-way (sim-3+sim-4), critical 2-way (sim-3+sim-4),
              web1 2-way pet (sim-3+sim-4), Garage 2 nodes,
              SENTINELs preserved
```

**Note:** at the end of phase 6, critical has degraded from 3-way →
2-way (because there are only 2 peers now — sim-3 and sim-4). This is
expected: design says critical is RF=3 *when N≥3*; at N=2 it is
effectively 2-way with no 3rd-failure-tolerance. Bedrock semantics
match physics.

**Verification gate:** SENTINELs preserved; web1 live-migrates
sim-3 ↔ sim-4 in ~1 s.

---

### Phase 7 — N=2 → N=1 (REMOVE sim-3; sim-4 alone; Garage + DRBD demoted)

The hardest phase: transition out of cluster mode entirely.

```
7.1   live-migrate web1 to sim-4 (off sim-3)
7.2   convert web1 vipet → pet (drops one DRBD peer; web1 now sim-3+sim-4)
      OR convert web1 pet → cattle (drops DRBD entirely; web1 becomes
      a local-LV VM on sim-4 only)
      → choose: cattle, since after sim-3 leaves there's no peer for HA
        (and cattle is the design state for N=1)
7.3   transfer_mgmt_role(sim-3 → sim-4, other_peer_hosts=[])
7.4   garage_drain_node(sim-3 → sim-4) — sim-4 ends up with all data
        (single-node Garage cluster RF=1)
7.5   drbd_remove_peer for tier-bulk + tier-critical
        (sim-4 is now the only DRBD peer for each)
7.6   POWER OFF sim-3
7.7   FULL DEMOTE: on sim-4, take down DRBD and switch to local LV
      a) for tier in (bulk, critical):
           umount /var/lib/bedrock/mounts/<tier>-drbd
           drbdadm down tier-<tier>
           # underlying /dev/bedrock/tier-<tier> still has the XFS
           # because external metadata never touched the data LV
           mount /dev/bedrock/tier-<tier> /var/lib/bedrock/local/<tier>
           atomic_symlink(/var/lib/bedrock/local/<tier>, /bedrock/<tier>)
           # update fstab: drop DRBD line, add local-LV line
           # set_tier_state(<tier>, mode="local")
      b) Garage decommission:
           umount /var/lib/bedrock/mounts/scratch-s3fs
           systemctl stop garage; systemctl disable garage
           # scratch is RF=1 by design — there's no replica.
           # Choices for the migration:
           #   (i) accept loss: just unmount, swap symlink,
           #       /bedrock/scratch is empty local LV
           #   (ii) export Garage data to local: rclone or aws-cli
           #        copy s3://scratch/ → /var/lib/bedrock/local/scratch/
           #        then stop Garage
           # Default for this run: (ii) export, to verify the path works.
           atomic_symlink(/var/lib/bedrock/local/scratch, /bedrock/scratch)
7.8   verify final state:
        - drbdadm status (empty — no resources up)
        - systemctl is-active garage → inactive
        - /bedrock/{scratch,bulk,critical} all symlink to local/* mounts
        - md5sum SENTINELs (all match phase 1.6)
        - web1 still running on sim-4 (now cattle on local LV)
        - bedrock storage status → 1 node, all tiers local mode
        - bedrock vm list → web1
```

**Verification gate:** sim-4 in fully-N=1 state, indistinguishable
from a fresh `bedrock init --name bedrock-test` from a topology
perspective. SENTINELs preserved through every transition.
`drbdadm status` is empty. Garage is stopped + disabled.

---

## What we're set up to learn from this run

### Tests of code that's never been exercised end-to-end

1. **Persistent DRBD node-ids across configs.** Every `render_drbd_res`
   call in the forward path should allocate IDs once and reuse them
   forever. Bug indicator: `drbdadm adjust` errors anywhere in
   phases 5/6/7 — symptom of node-id renumbering.

2. **`drbd_remove_peer()` with `--dry-run` safety net.** The dry-run
   step catches cases where adjust would do unexpected work. We expect
   each invocation to print only `del-peer` (and possibly `disconnect`)
   lines; anything else aborts. Bug indicator: `drbd_remove_peer`
   raises during dry-run validation.

3. **`garage_drain_node()` worker-queue polling.** Block resync workers
   on the departing node should go Busy → Idle within seconds for
   our small dataset; the function waits for that. Bug indicator:
   `garage_drain_node` times out, OR drains too quickly because the
   worker-list parser misreads the format.

4. **`transfer_mgmt_role()` orchestration.** Ten coordinated steps;
   any single failure leaves the cluster in a partial state — but the
   docstring claims idempotency. Bug indicator: re-running after a
   failed step doesn't recover.

5. **s3fs-via-localhost across node failures.** Each node's s3fs
   should keep working as long as that node's local Garage daemon
   does. When a peer node is removed, s3fs on surviving nodes should
   keep returning correct data via Garage's internal cluster-wide
   routing. Bug indicator: hangs / empty bytes on surviving s3fs
   after a peer is removed.

### Lessons we expect to capture

- Empirical timing for sync / drain / repair / migrate at each phase
- Whether `drbdadm adjust` is truly a no-op for surviving peers (the
  whole point of persistent node-ids)
- Whether `transfer_mgmt_role` outage is really 5–10 s or longer
- What single-node operation looks like after Garage decommission +
  DRBD demote (the "full N=1 collapse" path)
- Surprises (always have surprises)

## Risks / known sharp edges

- **Phase 5.5–5.7 has many SSH operations against sim-1 just before
  power-off.** If sim-1 becomes unreachable mid-step, the helper may
  fail. Mitigation: helpers use `check=False` for old-master ssh
  steps where appropriate, but operator may need to re-run.
- **Phase 7.7's "DRBD → local LV" demote is currently manual** —
  there's no `drbd_demote_to_local()` helper. If the manual steps
  miss something, the result is a stuck mount or a missing tier
  symlink. We'll write the helper if time permits, otherwise script
  inline.
- **Garage data export at phase 7.7b option (ii)** requires `aws`
  CLI or `rclone` installed on sim-4. Not currently in our package
  list. Either add it for this run or fall back to option (i)
  (accept scratch loss; it's RF=1 by design).

## Plan execution mode

Suggest serial execution with explicit verification at each phase
gate. After each phase, append a short "actual" section to this doc
recording duration, surprises, deviations. After all phases, write
a separate "results" doc summarizing.

If a phase fails its verification gate, STOP and analyze before
proceeding — the whole point is that bad states should be diagnosed,
not papered over.

## Sign-off question for the operator

Before execution: any changes to the plan? Specific things to
record beyond what's listed? Any phase you want to skip or defer
(e.g. skip phase 7 final-demote and leave at single-peer DRBD)?

Once approved, this doc gets a "## Actual run results" section
appended as we go, and at the end a final "## Summary" section.

## Post-run follow-up items added during execution

- **L15 fix — implement `migrate_scratch_into_garage()`**: symmetric
  counterpart to `migrate_scratch_out_of_garage()`. Per Tommy:
  "data may be lost ONLY when losing a node, never during default/
  normal migration." Add the function + .md section. Wire into
  `transition_to_n2_master` so the N=1→N=2 promote preserves
  scratch data automatically.

- **L17 real fix — pip install moves to base**: Move the
  `pip install fastapi uvicorn paramiko websockets pydantic
  python-multipart` line from `mgmt_install.install_full()` into
  `packages.install_base()`. Every node then has the mgmt deps at
  bootstrap time, ready to take over the master role. Per Tommy:
  "any one could in principle become the master."

- **drbd_remove_peer generalization**: currently hardcoded to
  `tier-X` resource names (uses `write_drbd_resource` which only
  knows tier resources). VM disks (`vm-X-disk0`) follow a
  different config format (vm.py's `_drbd_2way_conf` /
  `_drbd_3way_conf`). Generalize so the same online-remove-peer
  flow works for any DRBD resource.

- **L18 follow-up — research a machine-friendly Garage API**:
  the current parser regex on `garage worker list` is fragile —
  any change in column layout breaks it. Investigate what Garage
  v2.x exposes for *machine-readable* status checks:
  - Is there a JSON output flag for `worker list` / `stats` /
    `block list-errors`?
  - The admin API (port 3903) — does it have endpoints like
    `GET /v1/health`, `GET /v1/worker?name=block_resync`, or per-
    node stats that would replace the text-table parsing?
  - Specifically: a "is the resync queue empty for THIS node"
    binary check that doesn't require splitting whitespace.
  - Replace the regex parser with the API call. Document in
    tier_storage.md "garage_drain_node" with new source citation
    to whichever Garage admin-API endpoint we end up using.

  This research item is *queued for after the clean-run completes*.
  Add the result as L19 to the lessons log.
