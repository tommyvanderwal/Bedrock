# VM lifecycle — start, shutdown, poweroff, delete

Smaller actions on existing VMs. All dispatched by the mgmt node over SSH
to the target node's libvirtd.

## Start

**Trigger:** `Start` button (UI) or `POST /api/vms/{name}/start`.
**Source:** `mgmt/app.py:_vm_start`.

```
  T=0    POST /api/vms/NAME/start
         │
         │ build_cluster_state → find defined_on[]
         │
         │ pick target = first defined_on node (mgmt node first if it's in)
         │
  T+0.1  ssh target: virsh start NAME
         │ push_log "VM NAME started on <target>"
```

For a **pet/ViPet** VM the target must hold the DRBD Primary (the convert
path defines the VM on all peers, but only the Primary can boot it). If
none is Primary (e.g., after a clean shutdown that left both Secondary),
the `_vm_start` logic promotes the target first via `drbdadm primary`.

## Shutdown (graceful)

**Trigger:** `Shutdown` button, `POST /api/vms/{name}/shutdown`.
**Source:** `mgmt/app.py:_vm_shutdown`.

```
  T=0    POST /api/vms/NAME/shutdown
         │
         │ ssh vm.running_on: virsh shutdown NAME
         │   → ACPI signal to the guest; guest runs init shutdown
         │
         │ push_log "VM NAME shutdown requested on <host>"
  T+~20s (guest responsible for its own timing; KVM returns immediately)
```

A subsequent state_push_loop tick notices `virsh list --state-running` no
longer contains NAME and updates the dashboard tile to `shut off`.

## Power Off (force)

**Trigger:** `Power Off` button, `POST /api/vms/{name}/poweroff`.
**Source:** `mgmt/app.py:_vm_poweroff`.

```
  T=0    POST /api/vms/NAME/poweroff
         │
         │ ssh vm.running_on: virsh destroy NAME
         │   → equivalent to yanking power; no guest OS notification
         │
         │ push_log "VM NAME powered off on <host>" (level=warn)
```

Safe for stuck VMs. Data inside the guest is subject to normal
power-loss semantics (fsck on next boot, unflushed buffers lost).
DRBD state is untouched — the resource stays Primary on that node.

## Delete

**Trigger:**

- Dashboard: `Delete VM` button on the VM detail page → double-confirm
  (browser `confirm()` + `prompt()` requiring the VM name to be typed).
  On success, redirects to `/vms`.
- HTTP: `DELETE /api/vms/{name}` — no body, idempotent-ish (a repeat
  call on a non-existent VM returns 404).
- CLI: `bedrock vm delete NAME` — same effect, different code path.

**Source:** `mgmt/app.py:_vm_delete` (dashboard + HTTP) and
`installer/lib/vm.py:delete_vm` (CLI).

## Delete — dashboard / HTTP flow

```
  DELETE /api/vms/NAME   → 202 Accepted + {task_id}
  │
  │ build_cluster_state → vm  (includes vm.disks[] — multi-disk aware)
  │
  │ if vm.state == running:
  │    ssh host: virsh destroy NAME   (force-kill)
  │
  │ for node in defined_on:
  │    ssh host: virsh undefine NAME --nvram
  │    for disk in vm.disks:
  │       resource = disk.drbd_resource     (or "" if cattle)
  │       if resource:
  │          lv_by_node, meta_by_node = _parse_drbd_res(host, resource)
  │          ssh host: drbdadm down RES
  │          ssh host: drbdadm wipe-md --force RES
  │          ssh host: rm -f /etc/drbd.d/RES.res
  │       else:
  │          lv_by_node = { every node: disk.backing_lv }
  │          meta_by_node = {}
  │       ssh host: lvremove -f {lv_by_node[node]} {meta_by_node[node]}
  │
  │ load_inventory(); inv.pop(NAME); save_inventory(inv)
  │
  │ push_log "Deleted VM NAME (was on <nodes>)"   level=warn
  │ task.succeed()
```

Multi-disk: every disk on the VM (vda, vdb, …) is torn down in order.
Each one gets its own task step (`disk0 teardown on <node>`,
`disk1 teardown on <node>`, …) so the drawer shows where cleanup is.

## Delete — CLI flow (legacy)

```
  T=0    bedrock vm delete NAME
         │
         │ GET /api/cluster   →  vm.running_on, defined_on
         │
  T+0.1  if state == running:
         │   POST /api/vms/NAME/poweroff
         │   sleep 2
         │
  T+2s   for host in defined_on:
         │   ssh host: virsh undefine NAME --remove-all-storage
         │   ssh host: drbdadm down vm-NAME-disk0 (ignore errors)
         │   ssh host: rm -f /etc/drbd.d/vm-NAME-disk0.res
         │   ssh host: lvremove -f almalinux/vm-NAME-disk0
         │
  T+~5s  print "VM NAME deleted."
```

Note on the two delete paths:

| Path | Meta-LV cleanup | Import reset | Live log |
|---|---|---|---|
| **HTTP `DELETE /api/vms/{name}`** (dashboard) | yes — `_parse_drbd_res` finds `meta_path`, `lvremove` hits both data + meta | yes — flips the import that spawned this VM back to `status:ready` | yes, push_log streams |
| **CLI `bedrock vm delete NAME`** | only data LV; `vm-NAME-disk0-meta` stays behind | no | no stream |

Prefer the HTTP path. If you must use the CLI on a converter-managed VM,
manually: `lvremove -f almalinux/vm-NAME-disk0-meta` on every host that
had the VM.

## Log lines

```
VM NAME started on <host>                              level=info
VM NAME shutdown requested on <host>                   level=info
VM NAME powered off on <host>                          level=warn
Deleted VM NAME (was on <node1>,<node2>,...)           level=warn
```

The dashboard DELETE path emits a `push_log` that streams live through
Recent Logs. The CLI `bedrock vm delete` runs on a node, not in the
mgmt process, so it does not yet stream. Running delete via the HTTP
API is the stream-friendly path.

## Why

- **Shutdown vs. poweroff**: same distinction as any VM platform — give
  the guest a chance to flush before yanking.
- **Delete sweeps all `defined_on` nodes**: a pet/ViPet VM lives in
  libvirt XML on multiple nodes and has LVs everywhere. Missing one
  leaves orphaned LVs that silently consume thin-pool capacity.
- **`drbdadm down` before `lvremove`**: the DRBD kernel module holds an
  exclusive open on the underlying LV; lvremove otherwise fails with
  "LV in use".
