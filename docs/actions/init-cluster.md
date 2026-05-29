# Start a new cluster (`bedrock init`)

Turns a bootstrapped node into the founding node of a new cluster at N=1.
This is the saga that brings up the cluster's control plane: it allocates
the cluster identity, provisions local (N=1) storage, brings up rqlite (the
Raft-replicated SQLite that holds all cluster state), seeds the schema, starts
the unified daemon `bedrock-d`, starts SeaweedFS, and prints the dashboard URL.

**Triggered by:** operator on a bootstrapped node:

```bash
bedrock init [--name CLUSTER_NAME]
```

The CLI (`installer/bedrock`) is a thin client: `cmd_init` calls
`mgmt_install.install_full()`, which runs `run_cluster_init()` directly —
there is no HTTP submission, because this saga is what brings rqlite (and the
mgmt API) up. Witnesses are not configured here; they are added later via the
dashboard / API as rows in the rqlite `witnesses` table.

**Source:** `installer/bedrock:cmd_init`,
`installer/lib/mgmt_install.py:install_full`,
`bedrock_d/install/cluster_init.py` (the `ClusterInit` saga — see
[`docs/sagas/cluster_init.md`](../sagas/cluster_init.md)).

## Preconditions

- `bedrock bootstrap` ran successfully (`/etc/bedrock/state.json` has
  `bootstrap_done: true`).
- This node is **not** already a member of a cluster.
  `/var/lib/bedrock/init-progress.json` is the source of truth for "is init
  done?": a completed `cluster_init` op short-circuits; a failed or in-flight
  one resumes from the first incomplete step.
- The install repo (`/etc/bedrock/installer.env`) is reachable — init fetches
  binaries and the dashboard build.

## Sequence

`run_cluster_init()` runs an ordered list of idempotent `@step`s via the saga
executor. The backend is the file-based `FileSagaBackend` at
`/var/lib/bedrock/init-progress.json` — rqlite isn't up until step 12, so init
progress cannot live in rqlite. Top-to-bottom:

```
  T=0    bedrock init --name <cluster_name>
         │  cmd_init guards → run_cluster_init(cluster_name, repo)
         │
   1. prepare_dirs              /etc/bedrock, /var/lib/bedrock, /opt/bedrock
   2. allocate_identity         cluster_uuid (uuid4), node_name, mgmt_ip,
                                loopback_ip 100.<X>.<Y>.1/32 (master = octet 1;
                                /24 prefix from sha256(cluster_uuid) inside
                                RFC 6598 100.64.0.0/10). Persisted to state.json.
   3. write_cluster_key         atomic-write /etc/bedrock/cluster.key (32 bytes,
                                0600) — the shared key for witness AEAD slots +
                                inter-node peer auth.
   4. write_bootstrap_cluster_json
                                minimal /etc/bedrock/cluster.json (rqlite peer
                                list + this node). Read by rqlite_setup
                                --render-env at every boot; rqlite can't report
                                its own peers before it starts.
   5. install_obs_binaries      curl <repo>/binaries/{victoria-metrics,
                                victoria-logs} → /opt/bedrock/bin (the VM/VL
                                backends); skips files already present.
   6. install_exporters         exporters.install(): node_exporter + vm_exporter
                                + the OBS_BINS agents (vmagent, vlagent, vmbackup,
                                vmrestore, …), and enables node-exporter +
                                vm-exporter now.
   7. write_obs_services        write bedrock-vm + bedrock-vl units + scrape.yml
                                + stage the dashboard build.
   8. start_obs_services        enable --now bedrock-vm + bedrock-vl (the
                                VictoriaMetrics/VictoriaLogs backends).
   9. provision_storage_n1      tier_storage.setup_n1(): thinpool + the SeaweedFS
                                volume LV (bedrock-weed-volume, XFS, no DRBD) +
                                the `cluster` singleton as a plain dir at
                                /var/lib/bedrock/cluster. At N=1 the singleton
                                (arbiter rqlite + filer data + CA) is a directory
                                on root FS; it promotes to a DRBD primary at N=2.
  10. bootstrap_cluster_ca      cluster CA + this node's cert + arbiter cert for
                                rqlite mTLS.
  11. render_rqlited_env        /etc/bedrock/rqlited.env (rqlite node_id =
                                loopback last octet, stable across reboots/joins).
  12. start_rqlited             enable --now bedrock-rqlited (single-node Raft);
                                polls /status until raft=Leader (30s, fails loud).
  13. apply_schema              bedrock_schema.sql (CREATE TABLE IF NOT EXISTS).
  14. seed_cluster_state        INSERT cluster_info, this node into `nodes`,
                                node loopback, set mgmt_master=self, seed the
                                default `root` operator + obs_backends=self.
  15. mirror_tier_state         push the `cluster` singleton's tier_state into
                                rqlite `tiers` (mode=local).
  16. start_bedrock_d           enable --now bedrock-d — netd thread
                                (mesh/election/witness/.254) + asyncio
                                orchestrator + mgmt API (8443 HTTPS, 8001
                                loopback). rqlite_subscriber starts here.
  17. seaweedfs_install         confirm /usr/local/bin/weed is staged.
  18. seaweedfs_configs         render master.toml + filer.toml + s3.json +
                                seaweedfs.env.
  19. seaweedfs_start_local     enable --now weed-master (lowest-octet Raft-3
                                set) + weed-volume + weed-s3 on this node.
  20. seaweedfs_start_filer     start the filer singleton on the .254 VIP
                                (owned by cluster_arbiter), wait for s3 to bind.
  21. seaweedfs_init_collections
                                fs.configure path policies: scratch=000,
                                iso/templates/snapshots=001-or-000, backups=
                                critical (N-dependent). Buckets auto-create via
                                the filer's /buckets IAM path.
  22. seed_iso_library          copy any ISOs staged in /opt/bedrock/iso into the
                                filer at /mnt/bedrock/iso (best-effort; never
                                fails init).
         │
  T+~30s print "Dashboard: https://<ip>:8443"
```

The dashboard + mgmt API live inside `bedrock-d`. Its `rqlite_subscriber` polls
`bedrock_meta.revision` and, on each advance, projects this node's role/URL into
`state.json` and runs the reactor. `cluster.json` is the boot-only peer-list
file, not a runtime projection. VictoriaMetrics and VictoriaLogs run as their
own units — `bedrock-vm.service` (:8428) and `bedrock-vl.service`
(:9428/:5140); the exporters are `node-exporter.service` / `vm-exporter.service`.

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

- `bedrock-rqlited`: Raft bootstrap + "Leader" once consensus is reached.
- `bedrock-d`: orchestrator startup + `rqlite_subscriber: starting`.
- `node-exporter`, `vm-exporter`: listening on 9100 / 9177.

**VictoriaLogs:** empty during init — nothing pushes yet. The first entry
arrives when the first node joins (see [`join-cluster.md`](join-cluster.md)) or
a VM action fires.

## Why this order

1. **Identity first:** `allocate_identity` fixes the cluster_uuid, loopback
   /32, and cluster_key that every later step and every peer depends on.
   Persisted to state.json so resumes read them back.
2. **Binaries before their units:** units `ExecStart=` the binary paths; a
   missing binary makes the unit fail on first start.
3. **Storage + CA before rqlite:** rqlited needs its node cert (mTLS on 4001)
   and its data directory; `provision_storage_n1` + `bootstrap_cluster_ca` run
   first.
4. **rqlite before schema/seed:** `start_rqlited` must report Leader (it polls
   Raft state, not just HTTP-up) before `apply_schema` / `seed_cluster_state`
   can write.
5. **bedrock-d after the seed:** its `rqlite_subscriber` expects a schema'd,
   seeded store; starting it earlier just spins on an empty DB.

## Failure modes

| Symptom | Cause | Recovery |
|---|---|---|
| `Cluster <x> already initialised (saga completed)` | `init-progress.json` has a completed `cluster_init` op | Intended guard. To start over, run `bedrock node reset` (tears down DRBD/LVs, wipes `/etc/bedrock`) — only safe if no VMs exist. |
| `curl <repo>/binaries/victoria-metrics` 404 | Repo missing artefacts | `ls installer/binaries/` on the repo host; rebuild if empty. |
| `rqlited didn't reach Leader within 30s` | rqlited can't bootstrap (cert/env/port) | `journalctl -u bedrock-rqlited`; check `/etc/bedrock/rqlited.env` + the node cert under `/etc/bedrock`. |
| `bedrock-d` won't start | mgmt deps missing (pip in air-gap) | `pip3 install fastapi uvicorn paramiko websockets pydantic`, then `systemctl restart bedrock-d`. |
| Dashboard 200s but `/api/cluster` returns `{"nodes": {}}` | seed didn't land, or subscriber hasn't projected yet | Wait a revision tick; check `journalctl -u bedrock-d \| grep rqlite_subscriber` and `bedrock status` (dials `127.0.0.1:8001`). |

## Post-init state

```
  Node state:
    /etc/bedrock/state.json   cluster_uuid, role, node identity, mgmt_url
    rqlite (cluster_info + nodes + tiers + bedrock_meta + …)
      — authoritative cluster state, read via cluster_state.load_cluster()

  Services running:
    bedrock-rqlited   per-node Raft store   :4001 (HTTPS mTLS) / :4002 (Raft)
    bedrock-d         unified daemon        :8443 (HTTPS dashboard+API),
                                            :8001 (loopback CLI/API)
    bedrock-mdns      bedrock.local responder
    bedrock-redirect  :80 → :8443 HTTPS redirect
    bedrock-vm        VictoriaMetrics       :8428
    bedrock-vl        VictoriaLogs          :9428 / :5140 (syslog)
    bedrock-weed-*    SeaweedFS master :9333 / volume :8080 / filer :8888 / s3 :8333
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
  VMs: the master's `cluster_tier_promote_master` saga flips the `cluster`
  singleton to DRBD at N=2.
```
