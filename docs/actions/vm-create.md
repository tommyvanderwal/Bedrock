# Create a VM (`bedrock vm create`)

Provisions a new VM of type `cattle`, `pet`, or `vipet`. Cattle: local thin
LV + Alpine image. Pet: same, plus a peer LV + 2-way DRBD. ViPet: three
peers + 3-way DRBD mesh.

**Triggered by:** operator on any cluster node:

```bash
bedrock vm create NAME --type {cattle|pet|vipet} --ram MB --disk GB
```

**Source:** `installer/bedrock:cmd_vm`, `installer/lib/vm.py`,
`installer/lib/workload.py`.

## Preconditions

- Cluster has ≥ `min_nodes` for the chosen type (cattle=1, pet=2, vipet=3).
- `VG_NAME=almalinux` exists on the home node (auto-created as loop-backed
  VG with a `thinpool` if it doesn't, via `_ensure_thin_pool()` — used by
  the testbed; real hardware has the VG from kickstart).
- Alpine cloud image reachable at `ALPINE_URL` or cached at
  `/var/lib/bedrock/alpine.qcow2`.

## Sequence — cattle

```
  T=0    bedrock vm create NAME --type cattle --ram 512 --disk 5
         │
         │ workload.validate_type("cattle", N)  →  ok if N >= 1
         │
  T+0.1  _ensure_thin_pool(home_host)
         │   lvs | grep thinpool? else lvcreate 95%FREE thinpool
         │
  T+0.5  _create_cattle(home_host, name, ram, disk):
         │
         │  a. ssh home: lvcreate -V {disk}G --thin -n vm-NAME-disk0
         │               almalinux/thinpool
         │
         │  b. ssh home: curl $ALPINE_URL → /var/lib/bedrock/alpine.qcow2
         │               (cached; ~50 MB download, once per node)
         │
         │  c. ssh home: qemu-img convert -f qcow2 -O raw
         │               /var/lib/bedrock/alpine.qcow2
         │               /dev/almalinux/vm-NAME-disk0
         │
         │  d. ssh home: virt-install
         │               --name NAME  --ram 512 --vcpus 1
         │               --disk /dev/.../vm-NAME-disk0,format=raw,bus=virtio
         │               --network bridge=br0,model=virtio
         │               --graphics vnc,listen=0.0.0.0
         │               --import --noautoconsole
         │
  T+~20s print "VM NAME created. Status: bedrock vm list"
```

Result: local LV on home node, VM defined (and started by virt-install's
`--import` unless `--noautoboot` specified), KVM + Alpine running.
The dashboard's state push picks this up in ≤ 3 s.

## Sequence — pet (2-way DRBD)

```
  T=0    bedrock vm create NAME --type pet --ram 1024 --disk 10
         │
         │ workload.validate: ok if cluster has >= 2 nodes
         │
         │ pick home=this-node, peer = first other cluster node
         │ minor = _next_drbd_minor(home)   # scans /dev/drbd* + /etc/drbd.d
         │ port  = 7789 + minor             # historical; convert uses 7000+minor
         │
  T+0.5  for h in (home, peer):
         │   _ensure_thin_pool(h)
         │   ssh h: lvcreate -V {disk}G --thin -n vm-NAME-disk0
         │          almalinux/thinpool
         │
  T+3s   generate DRBD 2-way resource text  (internal meta-disk today —
         │   NOTE: the vm-convert path uses external meta; see the
         │   lessons in docs/components/drbd.md)
         │
         │ for h in (home, peer):
         │   ssh h: cat > /etc/drbd.d/vm-NAME-disk0.res
         │   ssh h: drbdadm create-md --force --max-peers=7 vm-NAME-disk0
         │   ssh h: drbdadm up vm-NAME-disk0
         │
  T+4s   ssh home: drbdadm primary --force vm-NAME-disk0
         │
  T+5s   ssh home: curl alpine → /var/lib/bedrock/alpine.qcow2 (cache miss once)
         │ ssh home: qemu-img convert alpine.qcow2 → /dev/drbd{minor}
         │   (writes propagate to peer over DRBD ring)
         │
  T+~15s for h in (home, peer):
         │   ssh h: cat > /tmp/NAME.xml     (VM XML using /dev/drbd{minor})
         │   ssh h: virsh define /tmp/NAME.xml
         │
  T+~16s print "VM NAME created. Status: bedrock vm list"
         │
  (async) DRBD sync continues in background on the ring for a few seconds
         until peer is UpToDate.
```

## Sequence — vipet (3-way DRBD mesh)

Same as pet, but:

- Home + **two** peers all get a thin LV.
- DRBD config has three `on <node>` blocks (node-ids 0/1/2) and three
  explicit `connection { path { host A; host B; } }` pairs (0-1, 0-2, 1-2)
  — see `_drbd_3way_conf()`.
- Initial `--force primary` is the home node; the two peers start as
  Secondary/Inconsistent and sync.
- VM is defined on all three nodes so any can be the primary later.

## Log lines

Stdout of the CLI:

```
Creating {type} VM 'NAME' (RAM=512MB, disk=5GB, replicas={1|2|3}) on <home>
  Creating thin LV vm-NAME-disk0 ...
  Writing DRBD resource (minor=N, port=P)...
  Loading Alpine image on primary...
  Defining VM on {1|both|all 3} nodes...
  VM NAME created.
```

`bedrock vm create` itself does **not** push to VictoriaLogs today — it runs
on the node, not in the mgmt process. The **dashboard state push** picks up
the new VM on the next 3 s tick and emits it on the WS `cluster` channel.
Any `push_log()` calls surrounding this action (e.g. if the operator used
the mgmt API instead) would stream instantly on the `event` channel.

## Why this order

- **LVs before DRBD config**: `drbdadm create-md` reads the underlying
  block device to size the bitmap; missing LV = unhelpful error.
- **`--max-peers=7` at create-md time**: reserves bitmap slots so a future
  pet → vipet conversion doesn't hit "not enough free bitmap slots".
- **DRBD up before primary**: you cannot promote a disconnected resource.
- **Alpine write while primary**: DRBD synchronously replicates primary
  writes, so the initial image lands on the peer "for free".
- **`virsh define` last**: defining earlier would make the VM appear in
  `virsh list --all` before its disk is usable, confusing the dashboard.

## Failure modes

| Symptom | Cause | Recovery |
|---|---|---|
| `{tgt} requires ≥ N nodes` | Wrong type for cluster size | Use `--type cattle` and convert later. |
| `lvcreate … Insufficient free extents` | Thin pool full (or loop-backed VG too small) | Extend the loop file: `truncate -s 40G /var/lib/bedrock-vg.img; losetup -c; vgextend; lvextend`. |
| `drbdadm up vm-X-disk0` → "Exclusive open failed" | Previous run left the LV busy | `lsof | grep /dev/drbd`; `drbdadm down X`; retry. |
| peer LV created but DRBD handshake stuck | SSH key mesh or known_hosts not established between nodes yet | See [`join-cluster.md`](join-cluster.md) — ensure `ssh root@peer` works from home without prompt. |

## Related

- To later increase HA: [`vm-convert.md`](vm-convert.md) (cattle → pet → ViPet).
- To move: [`vm-migrate.md`](vm-migrate.md).
- To remove: [`vm-lifecycle.md`](vm-lifecycle.md).
