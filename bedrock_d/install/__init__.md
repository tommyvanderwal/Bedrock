# bedrock_d/install/__init__.py

Package marker for bedrock-d's install sagas — the ordered, idempotent step lists
that build and reshape a cluster. Each saga is one readable list of steps that
choreographs lower-level helpers (storage, net, seaweed, …) rather than doing the
work itself; the step list is the flow chart, and no step writes to rqlite before
rqlited is started.

## Contents

- `cluster_init.py` — bootstrap the first node into a single-node cluster.
- `node_join.py` — add a node to an existing cluster.
- `node_leave.py` — remove a node from the cluster.
- `cluster_tier.py` — tier/storage steps used by the cluster sagas.

The package file itself holds only the package docstring; it defines no symbols
and re-exports nothing.
