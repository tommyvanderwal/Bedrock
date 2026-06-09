# installer/lib/workload.py

Workload-type abstraction for VM placement. Defines the three Bedrock VM types
(`cattle`, `pet`, `vipet`) with their replica counts and minimum node
requirements, and offers a single validator callers use to reject a VM type the
cluster is too small to host.

## Functions / Classes

### `WORKLOAD_TYPES`
Module-level dict mapping each type name to its `replicas`, `min_nodes`, and a
human `description`:
- `cattle` → 1 replica, 1 node min (stateless, local storage).
- `pet` → 2 replicas, 2 nodes min (DRBD 2-way replicated).
- `vipet` → 3 replicas, 3 nodes min (DRBD 3-way replicated, VIP Pet).

### `validate_type(vm_type, cluster_node_count) -> tuple[bool, str]`
Checks whether a given VM type can be placed on a cluster of the given size.
- **In:** `vm_type` — type name string; `cluster_node_count` — number of nodes
  currently in the cluster.
- **Out:** `(True, "")` when the type is known and the cluster has at least its
  `min_nodes`; otherwise `(False, message)` naming the failure (unknown type, or
  too few nodes). Pure function — no side effects.

## How it works

Two guards in order: the type-lookup runs first, so the node-count check only
ever touches a known config and never raises a `KeyError`.

```
vm_type in WORKLOAD_TYPES ?
  no  -> (False, "Unknown type: <vm_type>")
  yes -> cluster_node_count >= cfg["min_nodes"] ?
           no  -> (False, "<type> requires >=<min> nodes (have <count>)")
           yes -> (True, "")
```

Sizing each type encodes:

```
cattle  → 1 replica  → 1+ node   (local thin LV, no DRBD)
pet     → 2 replicas → 2+ nodes  (DRBD 2-way)
vipet   → 3 replicas → 3+ nodes  (DRBD 3-way)
```
