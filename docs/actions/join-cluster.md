# Add a node to a cluster (`bedrock join`)

Registers a fresh bootstrapped node with an existing cluster's mgmt API.
Installs exporters and pre-populates SSH known_hosts so live migration
works on first try.

**Triggered by:** operator on a bootstrapped node:

```bash
bedrock join --witness <mgmt-host> [--yes]
```

**Source:** `installer/bedrock:cmd_join`, `installer/lib/agent_install.py`,
`installer/lib/discovery.py`, `installer/lib/exporters.py`.

## Preconditions

- `bedrock bootstrap` completed on this node.
- The target cluster's mgmt node is reachable on port 8080.
- This node's DRBD NIC (eth1 / `bedrock-drbd`) has a 10.99.0.X address
  (done by cloud-init in the testbed, by the operator on physical hw).

## Sequence

```
  T=0    bedrock join --witness <mgmt_host>
         │
         │ if --witness not given:
         │   discovery.find_witness()
         │     try common IPs (.253 .252 .254) + first 50 of local /24
         │     probe :9443/health (external witness) then :8080/cluster-info
         │
  T+0.5s discovery.query_cluster(witness)
         │   GET http://<mgmt>:8080/cluster-info
         │   → { cluster_name, cluster_uuid, nodes: [...], mgmt_url }
         │
         │ if not --yes:
         │   prompt "Join this cluster? [Y/n]"
         │
  T+1s   agent_install.install(witness, cluster_info, repo)
         │
         │  1. pick mgmt_ip (br0 IP) and drbd_ip (10.99.0.X) from hardware
         │
         │  2. state.save({ cluster_name, cluster_uuid, role=compute,
         │                 node_id, node_name, witness_host, mgmt_url,
         │                 mgmt_ip, drbd_ip })
         │
         │  3. exporters.install(repo)  ────────────────────┐
         │       curl <repo>/binaries/node_exporter          │
         │       curl <repo>/binaries/vm_exporter.py         │
         │       write systemd units + daemon-reload         │
         │       systemctl enable --now node-exporter vm-exporter
         │                                                    │
         │  4. POST <mgmt>/api/nodes/register                 │
         │       { name, host, drbd_ip, role=compute }        │
         │     mgmt side (register_node):                     │
         │       cluster.json += this node                    │
         │       save_cluster()  →  write_scrape_config()     │
         │                           →  POST VM /-/reload     │
         │       push_log("Node X (ip) registered with cluster")
         │       return { nodes: [...], peer_ips: [...] }     │
         │                                                    │
         │  5. for ip in peer_ips:                            │
         │       ssh-keyscan -H -T 3 $ip >> /root/.ssh/known_hosts
         │     sort -u                                        │
         │
         │  6. print "Joined cluster X as node N"             │
  T+~5s  │  print "Dashboard: http://<mgmt>:8080"             │
```

## What the mgmt node does on register

```python
# mgmt/app.py:register_node
cluster = load_cluster()
cluster["nodes"][req.name] = {
    "host": req.host,                    # mgmt LAN IP (for SSH, cockpit)
    "drbd_ip": req.drbd_ip or "",        # 10.99.0.X (for DRBD, migrate URI)
    "tb_ip":   req.drbd_ip or "",        # testbed migration URI = drbd_ip
    "eno_ip":  req.drbd_ip or "",        # physical-lab direct-eth fallback
    "role":    req.role,
    "cockpit": f"https://{req.host}:9090",
}
save_cluster(cluster)                    # atomic write + write_scrape_config()
push_log(f"Node {req.name} ({req.host}) registered with cluster",
         node="mgmt", app="bedrock-mgmt", level="info")
return { "status": "registered",
         "cluster": cluster_name,
         "nodes": [...],
         "peer_ips": sorted({all host and drbd_ip fields}) }
```

**`save_cluster()`** triggers `write_scrape_config()` which:

1. Rewrites `/opt/bedrock/scrape.yml` with every node's IP for both
   `:9100` (node) and `:9177` (libvirt).
2. POSTs to `http://127.0.0.1:8428/-/reload` so VictoriaMetrics picks up
   the new targets without a restart.

**`push_log()`** broadcasts on the WebSocket `event` channel first (so any
open browser sees it in ~ms), then inserts into VictoriaLogs for history.

## Log lines

**joining node — stdout:**

```
=== Bedrock Join (existing cluster) ===

Cluster: <name> (N existing nodes)
  Installing exporters...
  Fetching node_exporter...
  Fetching vm_exporter...
  Registering with mgmt at http://<ip>:8080...
  Registered. Cluster now has N+1 nodes.
  Pre-scanned N peer host keys.

  Joined cluster <name> as node <id>.
  Dashboard: http://<ip>:8080
```

**mgmt node — VictoriaLogs (also broadcast on WS `event`):**

```
Node <new-hostname> (<new-ip>) registered with cluster
  hostname=mgmt  app=bedrock-mgmt  level=info
```

**mgmt node — VictoriaMetrics journal:**

```
info ... reading scrape configs from "/opt/bedrock/scrape.yml"
info ... static_configs: added targets: 2, removed targets: 0; total targets: (2N)
```

After the next scrape tick (≤ 10 s) the dashboard's host list shows the
new node as **Online** and its memory/load tiles populate.

## Why this order

1. **state.json before register**: we need `node_name` and IPs before
   we can populate the cluster.json entry.
2. **exporters.install before register**: if mgmt-side scrape reload
   races ahead of exporters being up, the first scrapes of the new
   node return `up=0`; harmless but noisy in graphs.
3. **ssh-keyscan after register**: the register response delivers the
   peer list. Pre-scanning every peer's `ssh-ed25519` hostkey into our
   `known_hosts` means the **first** `virsh migrate qemu+ssh://...` after
   a future convert doesn't hit a cold-cache race. Combined with the
   `accept-new` in `/root/.ssh/config` (written at bootstrap), this is
   belt-and-braces.

## Failure modes

| Symptom | Cause | Recovery |
|---|---|---|
| `No cluster found` | `--witness` not given and discovery failed | Give `--witness <ip>` explicitly. |
| `registration failed: HTTP 500` | mgmt unreachable / cluster.json corrupt | `curl -v <mgmt>/api/cluster` to diagnose; restart `bedrock-mgmt`. |
| registered but dashboard shows **Offline** | exporters didn't bind (firewall?) | `systemctl status node-exporter vm-exporter`; `ss -tlnp \| grep 9100`. |
| live-migrate fails: `Host key verification failed` | ssh-keyscan didn't cover a peer (IP changed since) | Re-run `ssh-keyscan -H <peer> >> /root/.ssh/known_hosts` manually, or `bedrock status` which future-me can extend to re-sync. |
| `Already a member of cluster` | This node already joined once | See `/etc/bedrock/state.json`; delete + rejoin only if safe. |

## Post-join state

- **On this node:** exporters running, scraped by mgmt; `state.json` has
  `cluster_uuid` and `mgmt_url`; `known_hosts` has every peer.
- **On mgmt node:** `/etc/bedrock/cluster.json` contains this node;
  `/opt/bedrock/scrape.yml` has added this node's `:9100` + `:9177`;
  VictoriaMetrics reloaded; dashboard sidebar shows the new host.
- **Still not wired** (explicit follow-ups): SSH pubkey exchange between
  peers (needed for DRBD replication + `virsh migrate`). Today the
  operator sets up the mesh manually; a future iteration will push pub-
  keys through the register endpoint.

## What's next

- If the cluster now has ≥ 2 nodes: `PET` checkbox on any cattle VM
  unlocks — see [`vm-convert.md`](vm-convert.md).
- Live migrate also unlocks for existing pet/ViPet VMs.
