# bedrock_d/__init__.py

Package marker for `bedrock_d`, the unified Bedrock daemon. The package holds the daemon's subsystems: `cluster/` (election, witness, mesh, quorum role, resource rename), `install/` (crash-resumable provisioning sagas: cluster init, node join/leave, tiers), `orchestrator/` (saga executor, reactor, `vm_failover`, `self_heal`, `replica_repair`, plus `sagas/`), `vm/` (VM lifecycle: create, destroy, grow, migrate, DRBD config, LVM, failover), and `state.py`.

`__init__.py` contains only the package docstring — no symbols are re-exported; importers reach into the submodules directly (e.g. `bedrock_d.orchestrator`, `bedrock_d.vm`).
