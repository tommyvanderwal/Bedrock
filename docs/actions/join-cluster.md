# Add a node to a cluster (`bedrock join`)

Adds a fresh bootstrapped node to an existing cluster. Discovers the
cluster, asks for operator approval, provisions local storage, starts the
unified daemon `bedrock-d`, joins rqlite as a Raft voter, joins SeaweedFS
as a volume server, and (when N>=2 and it lands in the `min(3,N)`
lowest-octet replica set) joins the `cluster` singleton DRBD as Secondary.

**Triggered by:** operator on a bootstrapped node:

```bash
bedrock join [<node-ip>] [--yes]
```

`<node-ip>` is the IP or hostname of *any current cluster node* (not
necessarily the master). Omit it to auto-discover via mDNS on the LAN —
the joiner lists the answering candidates in a numbered menu (with an
`[m]` manual-entry option) and falls back to a subnet scan if mDNS is
blocked. `--yes` skips the menu and the confirmation prompt and auto-picks
the first candidate reachable on `:8443`.

The CLI (`installer/bedrock`) is a thin client: `cmd_join` fetches cluster
info (`discovery.query_cluster` → `/cluster-info`), prints this node's
bedrock pubkey + fingerprint for operator approval, then hands off to
`agent_install.install()` → `run_node_join()`.

**Source:** `installer/bedrock:cmd_join`, `installer/lib/discovery.py`,
`installer/lib/agent_install.py` (`_request_join` / `_poll_status`),
`bedrock_d/install/node_join.py` (the `NodeJoin` saga — see
[`docs/sagas/node_join.md`](../sagas/node_join.md)).

## Preconditions

- `bedrock bootstrap` completed on this node.
- A current cluster node is reachable on the LAN (mgmt HTTPS `:8443`, or
  discoverable via mDNS).
- The saga backend (`/var/lib/bedrock/init-progress.json`) is the source
  of truth for "is join done?" — a `node_join` op whose every step is
  `completed` short-circuits; a failed/in-flight one resumes.

## Sequence

`run_node_join()` runs 22 idempotent `@step`s via the saga executor. It
uses the file-based `FileSagaBackend` because this node's rqlite isn't up
until `start_rqlited_joiner`. Top-to-bottom:

```
  T=0    bedrock join [<node-ip>]
         │  cmd_join: discover (mDNS) or use positional IP
         │  discovery.query_cluster(node_ip) → cluster_uuid, mgmt_url, nodes
         │  print this node's fingerprint; await operator approval
         │  run_node_join(witness=node_ip, cluster_info, repo)
         │
   1. prepare_dirs              /mnt/bedrock, /opt/bedrock, /root/.ssh
   2. detect_mgmt_ip            br0 IP, else first non-link-local UP NIC
   3. derive_identity           peer_auth Ed25519 keypair + node_name
   4. install_exporters         node_exporter + vm_exporter
   5. request_join_approval     ephemeral X25519 + POST /api/join/request;
                                BLOCKS until an operator approves on the
                                dashboard (polls 2 s, up to 10 min). The
                                sealed (ECDH) reply carries cluster.key,
                                this node's allocated loopback /32, the
                                peer set, and a CA-signed TLS cert.
   6. write_state_json          persist node_name, cluster_uuid, loopback_ip
   7. write_bootstrap_cluster_json
                                minimal cluster.json so rqlite_setup can
                                render its env; the mgmt rqlite_subscriber
                                keeps it in sync from rqlite once joined
   8. install_peer_pubkeys      fetch peers' SSH pubkeys → authorized_keys
   9. prescan_peer_hostkeys     ssh-keyscan peers → /root/.ssh/known_hosts
  10. provision_storage_n1      tier_storage.setup_n1() — local thinpool +
                                tier LVs + XFS + mounts (write_rqlite=False)
  11. pre_extract_mgmt          stage mgmt.tar.gz into /opt/bedrock
  12. start_bedrock_d           enable --now bedrock-d (unified daemon);
                                set rp_filter=2 + ip_forward=1 for the mesh
  13. wait_master_reachable     ping master loopback over the mesh (15 s)
  14. render_rqlited_env        rqlited.env with -join to every peer
                                loopback :4002 (rqlite_setup.render_env_file)
  15. start_rqlited_joiner      enable --now bedrock-rqlited as a Raft
                                follower; polls /status (mTLS :4001) until
                                raft state is Leader or Follower (a voter)
  16. install_dashboard         stage the Svelte build under /opt/bedrock/mgmt/ui
  17. seaweedfs_install         confirm /usr/local/bin/weed present
  18. seaweedfs_configs         render master/filer/s3 configs + env
  19. seaweedfs_start_local     start weed master (if in Raft-3 set),
                                volume + s3 on this node
  20. fuse_mount                mount /mnt/bedrock from .254:8888 (best-effort)
  21. cluster_tier_join_peer    at N>=2: poll rqlite until the master flips
                                the `cluster` singleton to DRBD, then
                                transition_to_n2_peer() — allocate the peer
                                LV pair, write the .res, drbdadm up as
                                Secondary (initial sync runs in background).
                                No-op at N=1, or if this node falls outside
                                the min(3,N) lowest-octet replica set.
  22. activate_node             flip this node's rqlite `nodes.state` from
                                'joining' to 'active'. It joins as 'joining'
                                at approval so it stays out of the master's
                                election denominator mid-join; as a full
                                Raft voter, this write now commits.
         │
  T+~Ns  print "Joined. Check status with: bedrock status"
```

## How the joiner reaches the cluster

The join is rqlite-first. Once `start_rqlited_joiner` lands, this node is
a full Raft voter and its `rqlite_subscriber` projects the same cluster
view every other node holds. The master allocates this node's loopback
/32 in the approval reply (`request_join_approval`); the final
`activate_node` step flips this node's rqlite row from `joining` to
`active` once it is a voter.

`install_peer_pubkeys` + `prescan_peer_hostkeys` set up the SSH mesh
(authorized_keys + known_hosts) so DRBD replication and `virsh migrate`
work on first try, without the operator wiring keys by hand.

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
Approve this fingerprint on the cluster dashboard
  (<mgmt_url> → Cluster → pending joins).

  ... (per-step progress)
Joined. Check status with: bedrock status
```

**master node — VictoriaLogs (also broadcast on the WS `event` channel):**

```
join request: <new-hostname> (<new-ip>) fp=<fingerprint>
operator <user> approved join <new-hostname> (<new-ip>)
  node=mgmt  app=bedrock-mgmt  level=info
```

`app=bedrock-mgmt` is the push_log tag for mgmt-plane events; the service
unit is `bedrock-d`. After the next rqlite revision tick the dashboard's
host list shows the new node as **Online** and its memory/load tiles
populate.

## Why this order

1. **Approval gates state.** `request_join_approval` blocks until an
   operator approves the fingerprint — no loopback /32, no cluster.key, no
   TLS cert, no rqlite join before that. The operator's click is the trust
   anchor: an unapproved node never receives the cluster key.
2. **bedrock-d + mesh before rqlited.** `wait_master_reachable` needs
   netd's mesh routing up (the multi-NIC `arp_ignore` / `arp_announce` /
   `rp_filter` sysctls in `installer/lib/netd.py`); rqlited then `-join`s
   the peer loopbacks `:4002`.
3. **rqlited bootstraps as a follower** and becomes a full voter, so this
   node counts in the Raft group and can commit its own `activate_node`
   write.
4. **cluster_tier_join_peer last.** DRBD secondary-join only makes sense
   once the master has promoted the `cluster` singleton to DRBD; the step
   polls rqlite for that and no-ops at N=1.

## Failure modes

| Symptom | Cause | Recovery |
|---|---|---|
| `No cluster found` | No mDNS answer and the subnet-scan fallback found nothing | Pass the IP positionally: `bedrock join 192.168.x.y`. |
| Join hangs at `request_join_approval` | No operator has approved the fingerprint | Approve on the dashboard (`GET /api/join/pending` → `POST /api/join/approve`). Times out after 10 min; re-running issues a fresh handshake. |
| `wait_master_reachable` times out | Mesh routing not up / master loopback unreachable | Check `bedrock-d` is running and the mesh NICs are up; verify the routing sysctls applied (`installer/lib/netd.py`). |
| Joined but dashboard shows **Offline** | Exporters didn't bind | `systemctl status node-exporter vm-exporter`; `ss -tlnp \| grep 9100`. |
| live-migrate fails: `Host key verification failed` | `prescan_peer_hostkeys` missed a peer (IP changed since) | `ssh-keyscan -H <peer> >> /root/.ssh/known_hosts`. |
| `Already joined cluster <x> (saga completed)` | Every `node_join` step is already `completed` | Intended guard. Re-running resumes only a failed/in-flight join; a completed-but-broken node is taken out with `bedrock node leave`, then re-joined. |

## Post-join state

- **On this node:** `bedrock-d` + `bedrock-rqlited` running (full Raft
  voter); exporters scraped; `state.json` has `cluster_uuid`,
  `loopback_ip`, `mgmt_url`; `authorized_keys` + `known_hosts` hold every
  peer; SeaweedFS volume serving; `/mnt/bedrock` mounted.
- **Cluster-wide:** the new node is an `active` row in rqlite `nodes`,
  visible to every node's subscriber projection; at N>=2 the `cluster`
  singleton is DRBD-replicated with this node as Secondary (when it is in
  the min(3,N) lowest-octet replica set).

## What's next

- With ≥ 2 nodes, the `PET` checkbox on any cattle VM unlocks — see
  [`vm-convert.md`](vm-convert.md).
- Live migrate also unlocks for existing pet/ViPet VMs.
