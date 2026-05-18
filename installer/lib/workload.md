# `workload.py`

**Module purpose.** Workload-type taxonomy. Single-source-of-truth
for the cattle/pet/vipet replica counts + min-node requirements.

## Functions

- `WORKLOAD_TYPES` — module-level dict:
  - `cattle`: replicas=1, min_nodes=1, "Stateless, local storage"
  - `pet`: replicas=2, min_nodes=2, "DRBD 2-way replicated"
  - `vipet`: replicas=3, min_nodes=3, "DRBD 3-way replicated"
- `validate_type(vm_type, cluster_node_count) -> (ok, reason)` —
  used by `bedrock vm create` to refuse a vipet on a 2-node
  cluster.

Phase E stub — full workload abstraction (placement policies,
anti-affinity, etc.) is v1.x.
