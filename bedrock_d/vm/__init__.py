"""bedrock-d VM lifecycle sagas.

One saga per CLI verb:

- ``vm_create``  — bedrock vm create  ← saga in create.py
- ``vm_destroy`` — bedrock vm delete  ← saga in destroy.py
- ``vm_grow``    — bedrock vm grow    ← saga in grow.py  (online)
- ``vm_migrate`` — bedrock vm migrate ← saga in migrate.py (live)

Helpers shared across sagas:

- ``lvm``         — per-resource thin data + meta LV sizing
- ``drbd_config`` — .res file templates (external meta, max-peers=7
                    per docs/storage-architecture.md)
- ``failover``    — pet/vipet failover building blocks (DRBD-UUID
                    read/record, next-in-line arithmetic, pre-start
                    safety check)

VM libvirt XML is produced inline by ``create.py`` via
``virt-install`` + ``virsh dumpxml`` (cattle / pet / vipet shapes),
not by a separate helper module.

All four sagas use ``RqliteSagaBackend`` (rqlite is up by VM-op
time) and persist progress in the operations + operation_steps
tables. Submit a saga via ``SagaExecutor.submit``, poll
``GET /api/operations/{id}`` for the current step; final state
is ``completed`` or ``failed`` with the failed step name.
"""
