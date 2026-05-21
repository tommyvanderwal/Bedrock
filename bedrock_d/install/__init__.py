"""bedrock-d install sagas — cluster_init, node_join, node_leave.

Each saga is a single, readable, ordered list of idempotent steps.
The step list IS the flow chart. Each step:
- has a 1-line docstring explaining its idempotency check
- delegates implementation to lower-level helpers (storage, net,
  seaweed, etc.) — the saga is the choreography, not the work
- writes nothing to rqlite before the start_rqlited step

See docs/codebase-rewrite-plan.md §3.2 for the saga model.
"""
