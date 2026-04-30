# Clean-rerun — full N=1 → N=3 → remove-peer ×2 → collapse-to-n1 lifecycle
## 2026-04-30

Goal: tear down all sim nodes from the previous run, recreate the testbed
from zero, and exercise the full Bedrock storage lifecycle on a fresh
cluster — including the new CLI verbs from commit `1b217fe`
(`promote-critical-3way`, `transfer-mgmt`, `remove-peer`,
`collapse-to-n1`).

Prior testbed state at start: 4 sim VMs (sim-1 through sim-4), all
either shut off or in mid-test. Fully wiped via `spawn.py reset`.

## Inputs at Phase 1 (sentinels for end-to-end verification)

Dropped on sim-1 right after `bedrock init`:

| Path | Content | MD5 |
|---|---|---|
| `/bedrock/bulk/PAYLOAD` | 8 MB random | `abbd50460820518ee999f52fd02c3f11` |
| `/bedrock/bulk/SENTINEL` | text marker | (text) |
| `/bedrock/critical/PAYLOAD` | 2 MB random | `93e0535d51693762eb8aae5f6a41c8be` |
| `/bedrock/critical/SENTINEL` | text marker | (text) |
| `/bedrock/scratch/SENTINEL` | `SCRATCH-SENTINEL-N1-1777570142` | (text) |

These survived **every** transition byte-for-byte.

## Phases run

### Phase 1 — N=1 single-node setup (sim-1)

`spawn.py up 1` → cloud-init → `install.sh` → `bedrock bootstrap` →
`bedrock init --name bedrock-rerun`.

Result: clean. mgmt URL up at `http://192.168.100.162:8080`. Storage
tiers at local-LV mode. Sentinels written.

### Phase 2 — N=1 → N=2 promote (sim-1, sim-2)

`spawn.py up 2` → bootstrap + `bedrock join` on sim-2 → `bedrock storage
promote` on sim-1.

**Bug surfaced (L26):** `migrate_scratch_into_garage()` failed with
rsync exit 23 — `failed to set times on "/var/lib/bedrock/mounts/
scratch-s3fs/.": Input/output error`. s3fs returns EIO when rsync's
`-a` (which implies `-t`) tries to set mtime on the destination root
(S3 has no native dir mtime). Fix: pass `--omit-dir-times`. File
mtimes still preserved.

This was the *first* time L15's INTO direction ran on a fresh testbed
(it was added in commit `1cdcba4` as a fix; the prior clean-run
exercised only the OUT direction).

After fix and resume: bulk + critical synced cleanly, scratch sentinel
preserved through the local→Garage migration, both sims see all 3 tiers
correctly.

### Phase 3 — N=2 → N=3 critical promote (add sim-3)

`spawn.py up 3` → bootstrap + `bedrock join` → `bedrock storage
promote-critical-3way bedrock-sim-3.bedrock.local` on sim-1.

**Bug surfaced (L27):** the new CLI verb's wiring (commit `1b217fe`)
SSH'd to sim-3 with `drbdadm create-md && drbdadm up`, but:
- sim-3 had no `/etc/drbd.d/tier-critical.res` (master-side
  `promote_critical_to_3way` only distributes to *existing* peers).
- sim-3's local `tier-critical` LV was still mounted at
  `/var/lib/bedrock/local/critical` from `setup_n1`. DRBD `attach`
  fails with "Can not open backing device (104)".

Fix: added hidden `bedrock storage _peer-join-tier --tier <t>
--peers <json>` subcommand that unmounts + drops fstab line + calls
`join_drbd_peer()`. The cluster-wide verb now SSH-fans-out to it.

After fix: sim-3 joined as Secondary, initial sync to UpToDate, all
3-way replication healthy.

### Phase 4 — transfer-mgmt sim-1 → sim-2

`bedrock storage transfer-mgmt bedrock-sim-2.bedrock.local` from sim-1.

DRBD-NFS role move worked: sim-2 became Primary on bulk + critical,
NFS exports flipped, sim-3's NFS clients re-pointed, sentinels intact.

**Bug surfaced (L28):** Two parts:
1. `transfer_mgmt_role` rsynced `/opt/bedrock/...` but missed
   `/etc/bedrock/cluster.json`. The new master ended up with only
   tier-state in its cluster.json (no `cluster_name`, no `nodes` map),
   so `bedrock storage status` reported "Cluster: <none>" and
   downstream verbs would have broken.
2. `transfer_mgmt_role` updated tier.master but not
   nodes[*].role — so the OLD master still appeared as
   `role: "mgmt+compute"` in cluster.json, and `remove-peer` was
   correctly refusing to remove a "mgmt master" that wasn't actually
   the master anymore.

Fix: added step 5b (rsync `/etc/bedrock/cluster.json`) and extended
step 11 to rewrite both old and new master's `role` fields.

### Phase 5 — remove-peer sim-1

`bedrock storage remove-peer bedrock-sim-1.bedrock.local` from sim-2
(the new master).

**Bug surfaced (mode-string mismatch):** the CLI verb checked
`scratch.mode == "garage-s3fs"` to decide whether to run
`garage_drain_node`, but the actual mode set elsewhere is `"garage"`.
The check failed silently and the Garage drain step was skipped — a
data-loss risk on RF=1 if any partitions had landed on sim-1's shard.

Fix: changed both `remove-peer` and `collapse-to-n1` to check
`mode == "garage"`.

After fix: DRBD-side removal worked (bulk had only sim-2; critical had
sim-2 + sim-3). The Garage drain step would have run on a re-run
because the mode check is now correct.

### Phase 6 — remove-peer sim-3

`bedrock storage remove-peer bedrock-sim-3.bedrock.local` from sim-2.

Clean. Garage drain correctly skipped (sim-3 was never in the Garage
cluster; agent_install only sets up local-LV scratch on a joining
node). DRBD removed sim-3 from tier-critical.

### Phase 7 — collapse-to-n1 on sim-2

`bedrock storage collapse-to-n1 --skip-md5` on sim-2 (now the only
node).

`drbd_demote_to_local(bulk)` + `drbd_demote_to_local(critical)` +
`migrate_scratch_out_of_garage()` ran cleanly. `garage` service
stopped + disabled. All three `/bedrock/<tier>` symlinks now point at
local LVs.

`bedrock storage status` confirms: 1 node, all tiers in local mode.

## Final verification

| | Phase 1 | After collapse |
|---|---|---|
| `/bedrock/bulk/PAYLOAD` MD5 | `abbd50460820518ee999f52fd02c3f11` | `abbd50460820518ee999f52fd02c3f11` ✓ |
| `/bedrock/critical/PAYLOAD` MD5 | `93e0535d51693762eb8aae5f6a41c8be` | `93e0535d51693762eb8aae5f6a41c8be` ✓ |
| `/bedrock/scratch/SENTINEL` | `SCRATCH-SENTINEL-N1-1777570142` | `SCRATCH-SENTINEL-N1-1777570142` ✓ |

Every transition's data preservation is verified.

## Lessons surfaced

| # | Title | Where it bit | Fix shipped |
|---|---|---|---|
| L25 | Testbed SSH key lives in `/root/.ssh`, not `~tommy/.ssh` | First fresh ssh-into-sim from a non-sudo shell | Documented as project rule; use `sudo ssh` or `spawn.py exec` |
| L26 | rsync into s3fs needs `--omit-dir-times` | First INTO migration on fresh testbed | tier_storage.py rsync flag |
| L27 | promote-critical-3way wiring needs unmount + .res + create-md on the new peer | First test of new CLI verb | bedrock CLI hidden `_peer-join-tier` subcommand |
| L28 | transfer_mgmt_role must rsync cluster.json + update role fields | First test of new CLI verb | tier_storage.py step 5b + extended step 11 |
| (no L) | bedrock CLI checked wrong scratch.mode string | First test of remove-peer / collapse-to-n1 | `"garage-s3fs"` → `"garage"` |

## Things deliberately NOT exercised

- **N=3 → N=4:** no CLI verb yet for extending the Garage cluster to a
  fourth node (`agent_install` only sets up local-LV scratch on
  joiners). `spawn.py up 4` was skipped — sim-4 wouldn't have
  exercised any new code paths.
- **Live VM migration during tier transitions:** out of scope for
  this rerun; covered by `test_e2e.sh` separately.
- **Crash recovery:** every helper has a documented crash-safety
  table; this run didn't simulate power loss mid-transition.

## Commits during this rerun

| Commit | Subject |
|---|---|
| `295aec5` | Fix L26: rsync into s3fs needs --omit-dir-times |
| `43b4ac4` | Fix L27: promote-critical-3way wiring needs unmount + .res + create-md on the new peer |
| `1fa36ea` | Fix L28: transfer_mgmt_role propagates cluster.json + role updates |

Branch: `storage-tiers-1to4`.

## Follow-ups (backlog)

1. CLI verb to add a 4th+ node to the Garage cluster (extend Garage
   layout for sim-4 etc.).
2. CLI verb to add a peer to tier-bulk (mirroring the existing
   `promote-critical-3way` for critical). Currently bulk stays at the
   peer count from the original N=2 promote.
3. L27 hardening: master pushes its `drbd_node_ids` map to the new
   peer's cluster.json before `_peer-join-tier` runs. Today the
   peer's get_drbd_node_id allocates fresh and matches by accident
   when the cluster has only ever grown monotonically.
4. Dashboard / `bedrock storage status` needs to surface Garage drain
   state cluster-wide so the operator can see WHICH nodes have data
   when shrinking.
5. Re-test the same lifecycle on a fresh testbed to confirm the
   commits in this run leave no remaining surprises.
