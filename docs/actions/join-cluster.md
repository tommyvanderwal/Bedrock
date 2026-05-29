# Add a node to a cluster (`bedrock join`)

Adds a fresh bootstrapped node to an existing cluster. Discovers the
master, asks for operator approval, provisions local storage, starts the
unified daemon `bedrock-d`, joins rqlite as a Raft voter, joins SeaweedFS
as a volume server, and (at N=2) joins the `cluster` singleton DRBD as
Secondary.

**Triggered by:** operator on a bootstrapped node:

```bash
bedrock join [<node-ip>] [--yes]
```

`<node-ip>` is the IP or hostname of *any current cluster node* (not
necessarily the master). Omit it to auto-discover via mDNS on the LAN —
the joiner presents a numbered list if more than one cluster answers.
`--yes` skips the confirmation prompt and auto-picks the first candidate
reachable on `:8443`.

The CLI (`installer/bedrock`) is a thin client: `cmd_join` fetches cluster
info, prints this node's bedrock pubkey + fingerprint for operator
approval, then calls `run_node_join()`.

**Source:** `installer/bedrock:cmd_join`, `installer/lib/discovery.py`,
`installer/lib/agent_install.py:install` (delegates to the saga),
`bedrock_d/install/node_join.py` (the `NodeJoin` saga — see
[`docs/sagas/node_join.md`](../sagas/node_join.md)).

## Preconditions

- `bedrock bootstrap` completed on this node.
- A current cluster node is reachable on the LAN (mgmt HTTPS `:8443`, or
  discoverable via mDNS).
- Saga backend (`/var/lib/bedrock/init-progress.json`) is the source of
  truth for "is join done?" — a completed `node_join` op short-circuits;
  a failed/in-flight one resumes.

## Sequence

`run_node_join()` runs an ordered list of idempotent `@step`s via the saga
executor. Like `cluster_init`, it uses the file-based `FileSagaBackend`
(this node's rqlite isn't up until `start_rqlited_joiner`). Top-to-bottom:

```
  T=0    bedrock join [<node-ip>]
         │  cmd_join: discover (mDNS) or use positional IP
         │  discovery.query_cluster(node_ip) → cluster_uuid, mgmt_url, nodes
         │  print this node's fingerprint; await operator approval
         │  run_node_join(witness=node_ip, cluster_info, repo)
         │
   1. prepare_dirs              /etc/bedrock, /var/lib/bedrock, /opt/bedrock
   2. detect_mgmt_ip            first non-link-local IPv4 on an UP NIC
   3. derive_identity           peer_auth Ed25519 keypair + node_name
   4. install_exporters         node_exporter + vm_exporter
   5. request_join_approval     ephemeral X25519 + POST /api/join/request;
                                BLOCKS until an operator approves on the
                                dashboard (polls 2 s, up to 10 min). The
                                sealed (ECDH) reply carries cluster_key +
                                this node's allocated loopback_ip /32.
   6. write_state_json          persist node_name, cluster_uuid, loopback_ip
   7. write_bootstrap_cluster_json
                                minimal cluster.json so netd can start;
                                OVERWRITTEN by the rqlite projection once
                                start_rqlited_joiner completes
   8. install_peer_pubkeys      fetch peers' SSH pubkeys → authorized_keys
   9. prescan_peer_hostkeys     ssh-keyscan peers → /root/.ssh/known_hosts
  10. provision_storage_n1      tier_storage.setup_n1() — local thinpool +
                                tier LVs + XFS + mounts
  11. pre_extract_mgmt          stage mgmt code into /opt/bedrock/mgmt
  12. start_bedrock_d           enable --now bedrock-d (unified daemon)
  13. wait_master_reachable     ping master loopback over the mesh (15 s)
  14. render_rqlited_env        rqlited.env with -join <master_lo>:4002
  15. start_rqlited_joiner      enable --now bedrock-rqlited as a Raft
                                follower; polls until a leader is visible
  16. install_dashboard         stage Svelte build under /opt/bedrock/mgmt/ui
  17-19. seaweedfs_*            install/configs + start weed master/volume/s3
  20. fuse_mount                mount /mnt/bedrock from .254:8888 (best-effort)
  21. cluster_tier_join_peer    at N=2: wait for the master's
                                cluster_tier_promote_master to flip the
                                `cluster` singleton to DRBD, then
                                transition_to_n2_peer() — allocate the peer
                                LV pair, write the .res, drbdadm up as
                                Secondary; initial sync runs in background.
                                No-op if still N=1.
  22. activate_node             flip this node's rqlite `nodes.state` from
                                'joining' to 'active' (it joined as
                                'joining' at approval so it stayed out of
                                the master's election denominator mid-join).
                                Now a full Raft voter, this write commits.
         │
  T+~Ns  print "Joined. Check status with: bedrock status"
```

## How the joiner reaches the cluster

The join is **rqlite-first**: once `start_rqlited_joiner` lands, this node
is a full Raft voter and its `rqlite_subscriber` projects the same cluster
view every other node has — there is no `POST /api/nodes/register` writing
into a shared `cluster.json` anymore. The master allocated this node's
loopback /32 in the approval reply (`request_join_approval`); the joiner's
own `activate_node` step (the last step of the saga) flips this node's
rqlite row from `joining` to `active` once it is a full Raft voter.

`install_peer_pubkeys` + `prescan_peer_hostkeys` set up the SSH mesh
(authorized_keys + known_hosts) so DRBD replication and `virsh migrate`
work on first try, rather than the operator wiring keys by hand.

## Log lines

**joining node — stdout:**

```
=== Bedrock Join (existing cluster) ===

Discovering Bedrock clusters on the LAN (mDNS)...
─── This node ─────────────────────────────────────────
  node name:      <hostname>
  bedrock pubkey: <hex>
  fingerprint:    <fp>
─── Joining cluster ───────────────────────────────────
  name:           <name>
  uuid:           <uuid>
  master:         <mgmt_url>
  existing nodes: N
Approve this fingerprint on the cluster dashboard ...

  ... (per-step progress)
Joined. Check status with: bedrock status
```

**master node — VictoriaLogs (also broadcast on the WS `event` channel):**

```
join request: <new-hostname> (<new-ip>) fp=<fingerprint>
operator <user> approved join <new-hostname> (<new-ip>)
  node=mgmt  app=bedrock-mgmt  level=info
```

(The `app=bedrock-mgmt` label is the push_log tag for mgmt-plane events;
the service unit is `bedrock-d`.) After the next rqlite revision tick the
dashboard's host list shows the new node as **Online** and its memory/load
tiles populate.

## Why this order

1. **approval before state**: `request_join_approval` gates everything — no
   loopback /32, no cluster_key, no rqlite join until an operator approves
   the fingerprint. This is join-approval (piece 3 of the inter-node auth
   design).
2. **bedrock-d + mesh before rqlited**: `wait_master_reachable` needs the
   netd mesh routing up (the multi-NIC `arp_ignore`/`arp_announce`/`rp_filter`
   sysctls in `installer/lib/netd.py`); rqlited then `-join`s the master's
   loopback `:4002`.
3. **rqlited join order matters**: rqlited bootstraps as a follower joining
   the existing Raft cluster, becoming a full voter. (A joiner that never
   starts rqlited would leave the cluster at N=1 voters — that was a real
   bug, fixed.)
4. **cluster_tier_join_peer last**: DRBD secondary-join only makes sense
   once the master has promoted the `cluster` singleton to DRBD; the step
   polls for that and no-ops if still N=1.

## Failure modes

| Symptom | Cause | Recovery |
|---|---|---|
| `No cluster found` | No mDNS answer and subnet-scan fallback found nothing | Pass the IP positionally: `bedrock join 192.168.x.y`. |
| Join hangs at `request_join_approval` | No operator has approved the fingerprint | Approve on the dashboard (`/api/join/pending` → `POST /api/join/approve`). Times out after 10 min; re-run issues a fresh handshake. |
| `wait_master_reachable` times out | Mesh routing not up / master loopback unreachable | Check `bedrock-d` is running and the mesh NICs are up; verify the routing sysctls applied (`installer/lib/netd.py`). |
| registered but dashboard shows **Offline** | exporters didn't bind | `systemctl status node-exporter vm-exporter`; `ss -tlnp \| grep 9100`. |
| live-migrate fails: `Host key verification failed` | `prescan_peer_hostkeys` missed a peer (IP changed since) | Re-run `ssh-keyscan -H <peer> >> /root/.ssh/known_hosts` manually. |
| `Already joined cluster <x> (saga completed)` | This node already joined | Intended guard. `bedrock node reset` to wipe local state and rejoin/init. |

## Post-join state

- **On this node:** `bedrock-d` + `bedrock-rqlited` running (full Raft
  voter); exporters scraped; `state.json` has `cluster_uuid`, `loopback_ip`,
  `mgmt_url`; `authorized_keys` + `known_hosts` have every peer; SeaweedFS
  volume serving; `/mnt/bedrock` mounted.
- **Cluster-wide:** the new node is a row in rqlite `nodes` (active), visible
  to every node's subscriber projection; at N=2 the `cluster` singleton is
  DRBD-replicated with this node as Secondary.

## What's next

- If the cluster now has ≥ 2 nodes: the `PET` checkbox on any cattle VM
  unlocks — see [`vm-convert.md`](vm-convert.md).
- Live migrate also unlocks for existing pet/ViPet VMs.
