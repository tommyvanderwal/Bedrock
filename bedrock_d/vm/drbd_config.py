"""DRBD .res config templates — external metadata, max-peers=7.

Per docs/storage-architecture.md the locked v0.8-alpha design uses
**external metadata** on a separate thin LV per resource (NOT
internal — that was the pre-rewrite shape). The .res file points
the data disk at ``/dev/bedrock/bedrock-data-<r>`` and the meta
disk at ``/dev/bedrock/bedrock-meta-<r>``.

This module emits the config text. Saga steps write it to disk
on each peer + run ``drbdadm create-md --max-peers=7``.

# Why a separate module

The CLI flow (legacy ``installer/lib/vm.py``) writes its own
internal-metadata configs. New code goes through this module to
guarantee the locked-design shape. Old VMs stay on their internal
meta layout until they're recreated; the two shapes coexist fine
at the kernel level (different resources just have different
.res files).
"""
from __future__ import annotations

from dataclasses import dataclass

from .lvm import data_lv_for, meta_lv_for, VG_NAME


# Per-VM DRBD ports start at 7700 + minor (per the storage doc).
# Cluster-singleton has minor 1101 → port 8801.
DRBD_PORT_BASE = 7700


def drbd_port_for(minor: int) -> int:
    """Port allocation: 7700 + minor. Saga's allocate_minor step
    picks an unused minor; the port follows."""
    return DRBD_PORT_BASE + minor


@dataclass(frozen=True)
class Peer:
    """One node hosting this DRBD resource."""
    node_name: str
    host: str             # LAN IP (operator-routable; for SSH from master)
    loopback_ip: str      # cluster identity /32 (DRBD path target)
    node_id: int          # DRBD node-id (0..max_peers-1; stable per node)


def render(resource: str, *, minor: int, peers: list[Peer],
           max_peers: int = 7, vg: str = VG_NAME) -> str:
    """Render the .res file for ``resource``. ``peers`` lists every
    node that will host this DRBD resource (1 for cattle, 2 for pet,
    3 for vipet, up to ``max_peers``)."""
    if not peers:
        raise ValueError("at least one peer required")
    if len(peers) > max_peers:
        raise ValueError(f"too many peers ({len(peers)} > "
                         f"max_peers={max_peers})")
    seen_ids = set()
    for p in peers:
        if p.node_id in seen_ids:
            raise ValueError(f"duplicate node_id {p.node_id}")
        seen_ids.add(p.node_id)
    port = drbd_port_for(minor)
    data_lv = data_lv_for(resource)
    meta_lv = meta_lv_for(resource)

    on_blocks = []
    for p in peers:
        on_blocks.append(
            f"    on {p.node_name} {{\n"
            f"        device /dev/drbd{minor} minor {minor};\n"
            f"        disk   /dev/{vg}/{data_lv};\n"
            f"        meta-disk /dev/{vg}/{meta_lv};\n"
            f"        node-id {p.node_id};\n"
            f"    }}"
        )

    # Full-mesh connection blocks — every pair gets one.
    conn_blocks = []
    for i in range(len(peers)):
        for j in range(i + 1, len(peers)):
            a, b = peers[i], peers[j]
            conn_blocks.append(
                f"    connection {{\n"
                f"        path {{\n"
                f"            host {a.node_name} address "
                f"{a.loopback_ip}:{port};\n"
                f"            host {b.node_name} address "
                f"{b.loopback_ip}:{port};\n"
                f"        }}\n"
                f"    }}"
            )

    return (
        f"resource {resource} {{\n"
        f"    protocol C;\n"
        f"    disk {{ on-io-error detach; }}\n"
        f"    net {{\n"
        f"        allow-two-primaries no;\n"
        f"        after-sb-0pri discard-zero-changes;\n"
        f"        after-sb-1pri discard-secondary;\n"
        f"        after-sb-2pri disconnect;\n"
        f"    }}\n"
        + "\n".join(on_blocks) + "\n\n"
        + "\n".join(conn_blocks) + "\n"
        f"}}\n"
    )


def res_file_path(resource: str) -> str:
    """Canonical path: ``/etc/drbd.d/<resource>.res``."""
    return f"/etc/drbd.d/{resource}.res"
