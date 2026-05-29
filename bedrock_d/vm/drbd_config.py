"""DRBD .res config templates — external metadata, max-peers=7.

Per docs/storage-architecture.md every DRBD resource uses **external
metadata** on a separate thin LV per resource. The .res file points
the data disk at ``/dev/bedrock/bedrock-data-<r>`` and the meta disk
at ``/dev/bedrock/bedrock-meta-<r>``.

This module is the single source of DRBD .res text: every per-VM DRBD
resource is rendered here. It emits the config text; saga steps write
it to disk on each peer + run ``drbdadm create-md --max-peers=7``.
"""
from __future__ import annotations

from dataclasses import dataclass

from .lvm import data_lv_for, meta_lv_for, VG_NAME


# All DRBD resource ports live in the documented 7700-7799 band
# (docs/storage-architecture.md port map). The cluster singleton and
# every per-VM disk use the SAME formula: port = 7700 + (minor - 1100).
# DRBD minors are laid out so this maps cleanly into the band:
#   - cluster singleton: minor 1101            → port 7701
#   - per-VM disks:       minors 1102..1189     → ports 7702..7789
# That keeps ~87 VM-disk resources per node in-band and clear of every
# reserved port (9333 weed-master, 8333 weed-s3, 8080 weed-volume,
# 8443 mgmt, 4001/4002/4011/4012 rqlite — none of which fall in the
# band) and clear of the netd mesh UDP ports 7732 (probe), 7733
# (advert) and 7734 (node-to-node election heartbeat, netd.HB_PORT)
# — those minors, 1132/1133/1134, are skipped by the VM minor allocator.
DRBD_PORT_BASE   = 7700
DRBD_MINOR_BASE  = 1100   # minor 1100 would map to the band base, 7700


def drbd_port_for(minor: int) -> int:
    """Map a DRBD minor to its port in the 7700-7799 band. Same formula
    for the cluster singleton (minor 1101) and per-VM disks (1102+)."""
    return DRBD_PORT_BASE + (minor - DRBD_MINOR_BASE)


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
