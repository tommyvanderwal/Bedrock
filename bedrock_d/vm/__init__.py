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
- ``libvirt_xml`` — VM XML generation (cattle / pet / vipet shapes)

All four sagas use ``RqliteSagaBackend`` (rqlite is up by VM-op
time) and persist progress in the operations + operation_steps
tables. Submit a saga via ``SagaExecutor.submit``, poll
``GET /api/operations/{id}`` for the current step; final state
is ``completed`` or ``failed`` with the failed step name.
"""
