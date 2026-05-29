# Start a new cluster (`bedrock init`)

Turns a bootstrapped node into the founding node of a new cluster at N=1.
This is the saga that *brings up* the cluster's control plane: it allocates
the cluster identity, provisions local (N=1) storage, starts the single
unified daemon `bedrock-d`, brings up rqlite (the Raft-replicated SQLite
that holds all cluster state), seeds the schema, starts SeaweedFS, and
prints the dashboard URL.

**Triggered by:** operator on a bootstrapped node:

```bash
bedrock init [--name CLUSTER_NAME]
```

The CLI (`installer/bedrock`) is a thin client: `cmd_init` calls
`mgmt_install.install_full()`, which delegates straight to
`run_cluster_init()` (there is no HTTP submission — this saga is what
brings rqlite up). Witnesses are NOT configured here; they are added
later via the dashboard / API by writing a row into the rqlite `witnesses`
table.

**Source:** `installer/bedrock:cmd_init`,
`installer/lib/mgmt_install.py:install_full` (delegates to the saga),
`bedrock_d/install/cluster_init.py` (the `ClusterInit` saga — see
[`docs/sagas/cluster_init.md`](../sagas/cluster_init.md)).

## Preconditions

- `bedrock bootstrap` ran successfully (`/etc/bedrock/state.json` has
  `bootstrap_done: true`).
- This node is **not** already a member of a cluster. The saga backend
  (`/var/lib/bedrock/init-progress.json`) is the source of truth for
  "is init done?"; a completed `cluster_init` op short-circuits, while a
  failed/in-flight one resumes from the first incomplete step.
- Repo (the one in `/etc/bedrock/installer.env`) is still reachable — init
  fetches binaries and the dashboard build.

## Sequence

`run_cluster_init()` runs an ordered list of idempotent `@step`s via the
saga executor. The backend is the **file-based** `FileSagaBackend` (rqlite
isn't up yet — steps 1–11 run before `start_rqlited`), switching to the
rqlite-backed executor afterwards. Top-to-bottom:

```
  T=0    bedrock init --name <cluster_name>
         │  cmd_init guards → run_cluster_init(cluster_name, repo)
         │
   1. prepare_dirs              /etc/bedrock, /var/lib/bedrock, /opt/bedrock
   2. allocate_identity         cluster_uuid (uuid4), node_name, loopback_ip
                                100.<X>.<Y>.1/32 (master = octet 1; prefix
                                derived from sha256(cluster_uuid), RFC 6598),
                                + 32-byte cluster_key (AEAD witness slots +
                                peer-auth gate). All persisted to state.json.
   3. write_cluster_key         atomic-write /etc/bedrock/cluster.key (0600)
   4. write_bootstrap_cluster_json
                                minimal /etc/bedrock/cluster.json so netd
                                can start; OVERWRITTEN by the rqlite
                                projection after seed_cluster_state
   5. install_obs_binaries      curl <repo>/binaries/{victoria-metrics,
                                victoria-logs} → /opt/bedrock/bin
                                (the VM/VL backends); skips present files
   6. install_exporters         exporters.install(): node_exporter +
                                vm_exporter, plus the OBS_BINS agents
                                (vmagent, vlagent, vmbackup, vmrestore, …)
   7. write_obs_services        write bedrock-vm + bedrock-vl units +
                                scrape.yml + stage the dashboard build
   8. start_obs_services        enable --now bedrock-vm + bedrock-vl
                                (the VictoriaMetrics/VictoriaLogs backends;
                                 the exporters were already enabled in
                                 step 6 by exporters.install())
   9. provision_storage_n1      tier_storage.setup_n1(): thinpool + tier LVs
                                + XFS + mounts. At N=1 the `cluster`
                                singleton resource lives on a local thin LV;
                                it flips to DRBD when the cluster grows.
  10. bootstrap_cluster_ca      cluster CA + node cert for rqlite mTLS
  11. render_rqlited_env        /etc/bedrock/rqlited.env (node_id = loopback
                                last octet, stable across reboots/joins)
  12. start_rqlited             enable --now bedrock-rqlited (single-node
                                Raft); polls until it reports Leader (itself)
  13. apply_schema              bedrock_schema.sql (CREATE TABLE IF NOT EXISTS)
  14. seed_cluster_state        INSERT cluster_info, this node into `nodes`,
                                set mgmt_master=self, seed default operator
  15. mirror_tier_state         push local tier_state rows into rqlite `tiers`
  16. start_bedrock_d           enable --now bedrock-d — the unified daemon:
                                netd thread (mesh/election/witness/.254) +
                                asyncio orchestrator + mgmt API (8443 HTTPS,
                                8001 loopback). rqlite_subscriber starts here.
  17-22. seaweedfs_*            install/configs/start master+volume+s3, start
                                filer on .254, init collections + buckets,
                                seed the bundled Alpine image into /mnt/bedrock/iso
         │
  T+~30s print "Dashboard: https://<ip>:8443"
```

(The orchestrator's `rqlite_subscriber` then polls `bedrock_meta.revision`
and projects state to disk on each advance. There is no separate
`bedrock-mgmt` service — the dashboard + mgmt API live inside `bedrock-d`.
VictoriaMetrics and VictoriaLogs DO run as their own units —
`bedrock-vm.service` (:8428) and `bedrock-vl.service` (:9428/:5140),
written by `write_obs_services` and started by `start_obs_services` — and
the exporters are `node-exporter.service` / `vm-exporter.service`.)

## Log lines emitted

**stdout during init** (one line per step plus the final summary):

```
=== Bedrock Init (new cluster) ===

Creating cluster: <name>
  ... (per-step progress)
  Setting up storage tiers (N=1: local LV thin)...

Cluster <name> initialised.
Dashboard: https://<ip>:8443
```

**Systemd journals (`journalctl -u <service>`):**

- `bedrock-rqlited`: Raft bootstrap + "Leader" once consensus is reached
- `bedrock-d`: orchestrator startup + `rqlite_subscriber: starting`
- `node-exporter`, `vm-exporter`: listening on 9100 / 9177

**VictoriaLogs:** no entries yet — nothing pushes during init. First entry
arrives when the first node joins (see [`join-cluster.md`](join-cluster.md))
or a VM action fires.

## Why this order

1. **Identity before everything**: `allocate_identity` fixes the
   cluster_uuid, loopback /32, and cluster_key that every later step (and
   every peer) depends on. Persisted to state.json so resumes read them back.
2. **Binaries before systemd units**: the units `ExecStart=` the binary
   paths; a missing binary = unit fails on first start.
3. **Storage + CA before rqlite**: rqlited needs its node cert (mTLS on
   4001) and its data directory; `provision_storage_n1` + `bootstrap_cluster_ca`
   run first.
4. **rqlite before schema/seed**: `start_rqlited` must report Leader before
   `apply_schema` / `seed_cluster_state` can write. The saga polls Raft
   state, not just HTTP-up.
5. **bedrock-d after the seed**: the daemon's `rqlite_subscriber` expects a
   schema'd, seeded store; starting it earlier would just spin on an empty DB.

## Failure modes

| Symptom | Cause | Recovery |
|---|---|---|
| `Cluster <x> already initialised (saga completed)` | `init-progress.json` has a completed `cluster_init` op | Intended guard. To truly start over, run `bedrock node reset` (tears down DRBD/LVs, wipes `/etc/bedrock`) — only safe if no VMs exist. |
| `curl <repo>/binaries/victoria-metrics` 404 | Repo missing artefacts | `ls installer/binaries/` on repo host; rebuild if empty. |
| `rqlited didn't reach Leader within 30s` | rqlited can't bootstrap (cert/env/port) | `journalctl -u bedrock-rqlited`; check `/etc/bedrock/rqlited.env` + node cert under `/etc/bedrock`. |
| `bedrock-d` won't start | mgmt deps missing (pip in air-gap) | Install manually: `pip3 install fastapi uvicorn paramiko websockets pydantic`, then `systemctl restart bedrock-d`. |
| Dashboard 200s but `/api/cluster` returns `{"nodes": {}}` | rqlite seed didn't land, or subscriber hasn't projected yet | Wait a revision tick; check `journalctl -u bedrock-d | grep rqlite_subscriber` and `bedrock status` (which dials `127.0.0.1:8001`). |

## Post-init state

```
  Node state:
    /etc/bedrock/state.json   cluster_uuid, role, node identity, mgmt_url
    rqlite (bedrock_meta + cluster_info + nodes + tiers + …)
      — authoritative cluster state, read via cluster_state.load_cluster()

  Services running:
    bedrock-rqlited   per-node Raft store   :4001 (HTTPS mTLS) / :4002 (Raft)
    bedrock-d         unified daemon        :8443 (HTTPS dashboard+API),
                                            :8001 (loopback CLI/API)
    bedrock-vm        VictoriaMetrics       :8428
    bedrock-vl        VictoriaLogs          :9428 / :5140 (syslog)
    bedrock-weed-*    SeaweedFS master/volume/filer/s3
    node-exporter                           :9100
    vm-exporter                             :9177
    libvirtd                                (local socket)

  Dashboard URL:
    https://<node-ip>:8443
    → sidebar shows 1 host, 0 VMs
```

## What's next

- Create a cattle VM (`bedrock vm create foo --type cattle`) — works on 1 node.
- Or add a second node with [`join-cluster.md`](join-cluster.md) to unlock pet
  (the master's `cluster_tier_promote_master` flips the `cluster` singleton to
  DRBD at N=2).
