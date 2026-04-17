"""Cattle/Pet/ViPet workload type abstraction. Stub — Phase E."""


WORKLOAD_TYPES = {
    "cattle": {"replicas": 1, "min_nodes": 1, "description": "Stateless, local storage"},
    "pet":    {"replicas": 2, "min_nodes": 2, "description": "DRBD 2-way replicated"},
    "vipet":  {"replicas": 3, "min_nodes": 3, "description": "DRBD 3-way replicated (VIP Pet)"},
}


def validate_type(vm_type: str, cluster_node_count: int) -> tuple[bool, str]:
    if vm_type not in WORKLOAD_TYPES:
        return False, f"Unknown type: {vm_type}"
    cfg = WORKLOAD_TYPES[vm_type]
    if cluster_node_count < cfg["min_nodes"]:
        return False, f"{vm_type} requires ≥{cfg['min_nodes']} nodes (have {cluster_node_count})"
    return True, ""
