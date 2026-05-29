# bedrock_d/orchestrator/__init__.py

Package marker for bedrock-d's orchestration loop — the asyncio task that runs
inside bedrock-d alongside the netd thread and the FastAPI app. The package owns
the rqlite revision watcher, saga executor, service reconciler, and membership
rebalancer.

The module body is the package docstring only: no functions, classes, or
re-exports. The logic lives in siblings imported by their own paths: `sagas/`
(crash-resumable step sequences), `vm_failover.py`, `self_heal.py`, and
`replica_repair.py`.
