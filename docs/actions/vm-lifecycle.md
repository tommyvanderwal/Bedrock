# VM lifecycle — start, shutdown, poweroff, delete

Day-to-day actions on existing VMs. The operator drives them from the dashboard
or the `bedrock` CLI; both reach the same mgmt API. The CLI (`installer/bedrock`)
is a thin HTTP client to `http://127.0.0.1:8001`; the dashboard hits the same
routes on the LAN listener (HTTPS 8443). The mgmt API runs inside `bedrock-d` on
the master. Operators never SSH a compute node for a state change — the master's
mgmt process dispatches to the target node's libvirtd over SSH (start / shutdown /
poweroff), and delete runs the `vm_destroy` saga in the saga executor.

Routes: `mgmt/app.py` (`_vm_start`, `_vm_shutdown`, `_vm_poweroff`,
`api_vm_delete`). Delete saga: `bedrock_d/vm/destroy.py`.

## Start

**Trigger:** Start button (UI) or `POST /api/vms/{name}/start`.
**Source:** `mgmt/app.py:_vm_start`.

```
  POST /api/vms/NAME/start
    │
    │ build_cluster_state → vm
    │ 400 if already running
    │
    │ pick target:
    │   1. node where DRBD resource is Primary (pet/vipet), else
    │   2. first online node in vm.defined_on
    │   503 if no online defined node
    │
    │ if resource:  ssh target: drbdadm primary RES   (promote)
    │ ssh target:   virsh start NAME                   (500 on rc!=0)
    │
    │ push_log "VM NAME started on <target>"  level=info
    └─ 200 {"status":"started","node":target}
```

A pet/vipet VM can only boot where DRBD is Primary. The convert path defines the
VM on every peer, so any peer can host it, but `_vm_start` promotes the chosen
target with `drbdadm primary` first. Cattle VMs have no DRBD resource and skip the
promote.

## Shutdown (graceful)

**Trigger:** Shutdown button, `POST /api/vms/{name}/shutdown`.
**Source:** `mgmt/app.py:_vm_shutdown`.

```
  POST /api/vms/NAME/shutdown
    │ 400 if not running
    │ ssh vm.running_on: virsh shutdown NAME     (ACPI → guest init shutdown)
    │ push_log "VM NAME shutdown requested on <host>"  level=info
    └─ 200 {"status":"shutdown sent"}
```

`virsh shutdown` returns immediately; the guest owns its own shutdown timing. The
3-second `state_push_loop` tick later sees the domain leave the running set and
flips the dashboard tile to `shut off`.

## Power Off (force)

**Trigger:** Power Off button, `POST /api/vms/{name}/poweroff`.
**Source:** `mgmt/app.py:_vm_poweroff`.

```
  POST /api/vms/NAME/poweroff
    │ 400 if not running
    │ ssh vm.running_on: virsh destroy NAME      (yank power; no guest notice)
    └─ 200 {"status":"powered off"}
```

For stuck VMs. Guest data is subject to normal power-loss semantics (fsck on next
boot, unflushed buffers lost). DRBD is untouched — the resource stays Primary on
that node. No log line is emitted.

## Delete

**Trigger:**

- Dashboard: Delete VM button on the VM detail page → a modal requires typing the
  literal word `delete` to enable the final Delete button; on success the page
  redirects to `/vms`.
- HTTP: `DELETE /api/vms/{name}` — no body. Returns `202 {"status":"accepted",
  "task_id":...}` and runs teardown in the background; a repeat on an unknown VM
  returns 404.
- CLI: `bedrock vm delete NAME` — POSTs the same `DELETE /api/vms/{name}`.

**Source:** `mgmt/app.py:api_vm_delete` → `_run_vm_saga("vm_destroy", …)` →
`bedrock_d/vm/destroy.py` (`VmDestroy` saga).

```
  DELETE /api/vms/NAME  → 202 {task_id}
    │ task "vm.delete" registered (per-disk count in title)
    │
    │ background: run vm_destroy saga ──────────────────────────────┐
    │                                                               │
    │   load_resource_metadata  read drbd_resources + vms;          │
    │                           cattle: enumerate bedrock-data LVs;  │
    │                           already_gone short-circuit          │
    │   virsh_destroy_running   virsh destroy   on every peer       │
    │   virsh_undefine          virsh undefine --nvram  per peer    │
    │   drbd_down               drbdadm down RES  per peer per disk │
    │   drbd_wipe_md            drbdadm wipe-md --force  per disk   │
    │   remove_drbd_res_file    rm /etc/drbd.d/RES.res  per disk    │
    │   lvremove_pair           drop data+meta (or cattle) LVs      │
    │   delete_rqlite_rows      DELETE drbd_resources + vms rows ───┘
    │
    │ inv.pop(NAME) from dashboard inventory breadcrumb
    └─ task.succeed()
```

The saga reverses `vm_create` in safe order: stop the domain before its DRBD goes
down, DRBD before its LVs, LVs before the rqlite rows that named them. It handles
multi-disk VMs (`vm-NAME-disk0`, `disk1`, …) and both disk shapes — pet/vipet
(DRBD data+meta pair) and cattle (single local LV); the DRBD steps no-op
harmlessly on the cattle path. Every step is idempotent, so a re-run over a
half-deleted VM converges. Each step is a task entry, so the drawer shows where
teardown is.

The teardown sweeps **every peer in the resource's `peers` list**. A pet/vipet VM
lives in libvirt XML on multiple nodes and has LVs on each; missing one leaves
orphaned LVs that silently consume thin-pool capacity. `drbd_wipe_md` zeroes the
external meta LV so a later re-create of the same VM name sees clean metadata.

## Log lines

```
VM NAME started on <host>              level=info   (start)
VM NAME shutdown requested on <host>   level=info   (shutdown)
```

Start and shutdown push a log line via `push_log` (streams live in Recent Logs).
Poweroff and delete do not push a log line; delete progress is visible through its
`vm.delete` task in the Tasks drawer.

## Why

- **Shutdown vs. poweroff:** same distinction as any VM platform — give the guest a
  chance to flush before yanking power.
- **Delete is a saga, not inline SSH:** teardown crosses libvirt, DRBD, LVM, and
  rqlite on several nodes; the saga gives crash-resumable ordering and per-step
  idempotency so a partial failure can be re-run safely.
- **`drbdadm down` before `lvremove`:** the DRBD kernel module holds an exclusive
  open on the backing LV; lvremove otherwise fails with "LV in use".
