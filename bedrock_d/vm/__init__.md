# bedrock_d/vm/__init__.py

Package marker for the bedrock-d VM lifecycle sagas. The `bedrock_d.vm` package
holds one crash-resumable saga per VM CLI verb plus the helpers those sagas
share; the sagas are submitted to the `SagaExecutor` and tracked through rqlite.

## Contents

One saga module per CLI verb:
- `create.py` — `vm_create` saga (`bedrock vm create`)
- `destroy.py` — `vm_destroy` saga (`bedrock vm delete`)
- `grow.py` — `vm_grow` saga (`bedrock vm grow`, online)
- `migrate.py` — `vm_migrate` saga (`bedrock vm migrate`, live)

Shared helpers:
- `lvm` — per-resource thin data + meta LV sizing and LV lifecycle (create /
  remove / grow the data+meta pair)
- `drbd_config` — `.res` file templates (external meta, `max-peers=7`)
- `failover.py` — pet/vipet failover building blocks (DRBD-UUID read/record,
  next-in-line arithmetic, pre-start safety check) used outside the CLI-verb
  sagas. The VM's libvirt XML is produced inline by `create.py` via
  `virt-install` + `virsh dumpxml`, not by a separate module.

All four CLI-verb sagas use `RqliteSagaBackend` and persist progress in the
`operations` and `operation_steps` tables. A saga is run via
`SagaExecutor.submit`; callers poll `GET /api/operations/{id}` for the current
step, ending in `completed` or `failed` (with the failed step name).

The file itself is a docstring-only package marker; it defines no symbols.
