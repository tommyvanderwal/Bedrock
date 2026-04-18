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

**Trigger:** `bedrock vm delete NAME` (CLI only today; no UI button yet).
**Source:** `installer/lib/vm.py:delete_vm`.

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

Note: `delete_vm` does **not** yet remove the external meta-disk LV
(`vm-NAME-disk0-meta`). If the VM was converter-managed, clean it up
manually: `lvremove -f almalinux/vm-NAME-disk0-meta` on every host that
had the VM. A follow-up is to teach `delete_vm` to call `_parse_drbd_res`
first (like the convert downgrade path does) and remove both LVs.

## Log lines

```
VM NAME started on <host>                              level=info
VM NAME shutdown requested on <host>                   level=info
VM NAME powered off on <host>                          level=warn
```

`delete_vm` currently runs client-side (CLI) and doesn't `push_log` —
history of deletion only shows up in bash / ssh session logs unless run
via a future `/api/vms/{name}` DELETE endpoint.

## Why

- **Shutdown vs. poweroff**: same distinction as any VM platform — give
  the guest a chance to flush before yanking.
- **Delete sweeps all `defined_on` nodes**: a pet/ViPet VM lives in
  libvirt XML on multiple nodes and has LVs everywhere. Missing one
  leaves orphaned LVs that silently consume thin-pool capacity.
- **`drbdadm down` before `lvremove`**: the DRBD kernel module holds an
  exclusive open on the underlying LV; lvremove otherwise fails with
  "LV in use".
