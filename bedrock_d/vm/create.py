"""VmCreate saga — `bedrock vm create` flow.

Steps cover: allocate DRBD minor, lvcreate data + meta on every
peer, write .res config, drbdadm create-md / up / primary, image
fill (Alpine for cattle / pet / vipet base), libvirt define on
every peer, register in rqlite.

ctx inputs (from caller / mgmt /api/vms/create endpoint):
  - vm_name:   str
  - vcpus:     int
  - ram_mb:    int
  - disk_gb:   int
  - vm_type:   "cattle" | "pet" | "vipet"
  - priority:  "low" | "normal" | "high"
  - iso:       Optional[str]   (filename in /mnt/bedrock/iso/)
  - peers:     list[node_name] (1 for cattle, 2 for pet, 3 for vipet)
  - home:      node_name where the VM runs (== peers[0])

ctx outputs (filled as steps run):
  - minor:     int (DRBD minor allocated for this VM's disk)
  - port:      int (DRBD port = 7700 + minor)
  - data_lv:   str
  - meta_lv:   str
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "installer"))

from bedrock_d.orchestrator.sagas import saga, step  # noqa: E402
from . import drbd_config as _cfg
from . import lvm as _lvm

log = logging.getLogger(__name__)

# Per docs/storage-architecture.md cluster-singleton DRBD lives at
# minors 1100-1199; VM disks live at 1200+ to keep them clearly
# distinct in `drbdadm status` output.
VM_MINOR_BASE = 1200
VM_MINOR_MAX  = 1899


@saga("vm_create")
class VmCreate:
    """Create a VM (cattle / pet / vipet) as a saga."""

    # ─── Pre-flight ──────────────────────────────────────────────────

    @step("validate_request")
    def step_validate(self, ctx):
        """Sanity-check the params. Cheap; runs first so we fail
        before allocating anything if the request is malformed."""
        for k in ("vm_name", "vcpus", "ram_mb", "disk_gb",
                  "vm_type", "peers", "home"):
            if ctx.get(k) in (None, ""):
                raise ValueError(f"missing required ctx field: {k}")
        expected_peers = {"cattle": 1, "pet": 2, "vipet": 3}
        want = expected_peers.get(ctx["vm_type"])
        if want is None:
            raise ValueError(f"unknown vm_type: {ctx['vm_type']!r}")
        if len(ctx["peers"]) != want:
            raise ValueError(
                f"vm_type={ctx['vm_type']} needs {want} peer(s), "
                f"got {len(ctx['peers'])}")
        if ctx["home"] not in ctx["peers"]:
            raise ValueError("home must be in peers")

    @step("allocate_minor")
    def step_allocate_minor(self, ctx):
        """Pick the next free DRBD minor in 1200..1899 by reading
        ``drbd_resources`` in rqlite. Idempotent — if a row for
        this vm_name already exists (resume), re-use its minor."""
        from bedrock_d import state as _st
        resource = f"vm-{ctx['vm_name']}-disk0"
        with _st.RqliteClient() as client:
            rows = client.query(
                "SELECT name, minor FROM drbd_resources WHERE name = ?",
                params=[resource],
            )
            if rows:
                ctx["minor"] = int(rows[0]["minor"])
                ctx["port"] = _cfg.drbd_port_for(ctx["minor"])
                log.info("vm_create: reusing minor=%d for %s",
                         ctx["minor"], resource)
                return
            used = {
                int(r["minor"]) for r in client.query(
                    "SELECT minor FROM drbd_resources "
                    "WHERE minor BETWEEN ? AND ?",
                    params=[VM_MINOR_BASE, VM_MINOR_MAX],
                )
            }
        for m in range(VM_MINOR_BASE, VM_MINOR_MAX + 1):
            if m not in used:
                ctx["minor"] = m
                ctx["port"] = _cfg.drbd_port_for(m)
                log.info("vm_create: allocated minor=%d for %s",
                         m, resource)
                return
        raise RuntimeError(f"no free DRBD minor in "
                           f"{VM_MINOR_BASE}..{VM_MINOR_MAX}")

    @step("register_drbd_resource")
    def step_register_resource(self, ctx):
        """Pre-record the resource row so a crash mid-saga is
        recoverable: on resume we already have minor + intended
        peers. Uses INSERT OR REPLACE so retry is fine."""
        from bedrock_d import state as _st
        import time as _t
        resource = f"vm-{ctx['vm_name']}-disk0"
        pair = _lvm.lv_names_for(resource)
        data_bytes = int(ctx["disk_gb"]) * 1024 * 1024 * 1024
        meta_bytes = _lvm.meta_size_mb_for(int(ctx["disk_gb"])) * 1024 * 1024
        peers_json = json.dumps(ctx["peers"])
        now = int(_t.time())
        with _st.RqliteClient() as client:
            client.execute(
                "INSERT OR REPLACE INTO drbd_resources "
                "(name, minor, data_lv, meta_lv, thinpool, "
                " data_size_bytes, meta_size_bytes, max_peers, "
                " peers, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'thinpool', ?, ?, 7, ?, ?, ?)",
                params=[resource, ctx["minor"], pair.data_lv,
                        pair.meta_lv, data_bytes, meta_bytes,
                        peers_json, now, now],
            )
        ctx["resource"] = resource
        ctx["data_lv"] = pair.data_lv
        ctx["meta_lv"] = pair.meta_lv

    # ─── Storage provisioning (data + meta LV pair on every peer) ───

    @step("lvcreate_pair_on_peers")
    def step_lvcreate(self, ctx):
        """Create data + meta LVs on every peer. Idempotent —
        lvcreate_pair checks ``lvs`` first per peer."""
        peer_hosts = _peer_hosts(ctx["peers"])
        for host in peer_hosts:
            _lvm.lvcreate_pair(
                host, ctx["resource"], int(ctx["disk_gb"]),
            )

    @step("write_drbd_config")
    def step_write_config(self, ctx):
        """Write /etc/drbd.d/<resource>.res on every peer. The
        config is content-deterministic given (peers, minor) so
        re-running overwrites with the same bytes."""
        peers_meta = _peer_metadata(ctx["peers"])
        config = _cfg.render(
            ctx["resource"], minor=ctx["minor"], peers=peers_meta,
        )
        res_path = _cfg.res_file_path(ctx["resource"])
        for p in peers_meta:
            _lvm._run_on(
                p.host,
                f"cat > {res_path} << 'EOF'\n{config}EOF",
            )

    @step("drbd_create_md")
    def step_create_md(self, ctx):
        """drbdadm create-md --max-peers=7 on every peer.
        Idempotent — if metadata already exists drbdadm exits non-
        zero, which we tolerate (the next steps will catch real
        problems)."""
        peer_hosts = _peer_hosts(ctx["peers"])
        for host in peer_hosts:
            _lvm._run_on(
                host,
                f"drbdadm create-md --force --max-peers=7 "
                f"{ctx['resource']} 2>&1 | tail -2",
                check=False,
            )

    @step("drbd_up")
    def step_drbd_up(self, ctx):
        """drbdadm up on every peer. Idempotent."""
        peer_hosts = _peer_hosts(ctx["peers"])
        for host in peer_hosts:
            _lvm._run_on(
                host, f"drbdadm up {ctx['resource']}",
                check=False,
            )

    @step("drbd_primary")
    def step_drbd_primary(self, ctx):
        """drbdadm primary --force on the home node. Required so
        the next step can write the base image. Idempotent — a
        promote of an already-Primary resource is a no-op."""
        home_host = _peer_hosts([ctx["home"]])[0]
        _lvm._run_on(
            home_host,
            f"drbdadm primary --force {ctx['resource']}",
            check=False,
        )

    # ─── Image fill ──────────────────────────────────────────────────

    @step("fetch_base_image")
    def step_fetch_image(self, ctx):
        """Ensure /var/lib/bedrock/alpine.qcow2 exists on home.
        Used when no ISO is specified — gives the VM a bootable
        base. Idempotent — skips if file present."""
        if ctx.get("iso"):
            return  # ISO-booted; no base image needed
        home_host = _peer_hosts([ctx["home"]])[0]
        rc, _, _ = _lvm._run_on(
            home_host, "test -f /var/lib/bedrock/alpine.qcow2",
            check=False, timeout=5,
        )
        if rc == 0:
            return
        # Fetch via the legacy helper for now; future PR replaces
        # with a saga step that pulls from /mnt/bedrock/templates.
        sys.path.insert(0, "/usr/local/lib/bedrock")
        from lib import vm as _legacy_vm
        _legacy_vm._download_alpine_on_node(home_host)

    @step("write_image_to_drbd")
    def step_write_image(self, ctx):
        """qemu-img convert the base image onto /dev/drbdN on the
        home node. Idempotent IN INTENT (re-writing the same image
        is benign for a fresh VM) but EXPENSIVE — we skip if the
        drbd device already has a valid filesystem signature."""
        if ctx.get("iso"):
            return  # ISO-booted; install runs from CDROM
        home_host = _peer_hosts([ctx["home"]])[0]
        device = f"/dev/drbd{ctx['minor']}"
        # Lightweight idempotency: skip if the disk already has any
        # filesystem signature (i.e. blkid returns something).
        rc, _, _ = _lvm._run_on(
            home_host, f"blkid -s TYPE -o value {device} 2>/dev/null",
            check=False, timeout=5,
        )
        if rc == 0:
            log.info("vm_create: %s already has data; skipping image fill",
                     device)
            return
        _lvm._run_on(
            home_host,
            f"qemu-img convert -f qcow2 -O raw "
            f"/var/lib/bedrock/alpine.qcow2 {device}",
        )

    # ─── libvirt define + register ───────────────────────────────────

    @step("write_libvirt_xml")
    def step_write_xml(self, ctx):
        """Generate the libvirt XML + write to /tmp on every peer.
        Uses the legacy XML helpers for now (cattle / pet / vipet
        templates are short). Stage 8.1 replaces with a clean
        bedrock_d/vm/libvirt_xml.py."""
        sys.path.insert(0, "/usr/local/lib/bedrock")
        from lib import vm as _legacy_vm
        if ctx["vm_type"] == "cattle":
            xml = _legacy_vm._vm_xml_cattle(
                ctx["vm_name"], int(ctx["ram_mb"]),
                _lvm.lv_path(ctx["data_lv"]),
            )
        else:
            xml = _legacy_vm._vm_xml_pet(
                ctx["vm_name"], int(ctx["ram_mb"]), ctx["minor"],
            )
        ctx["libvirt_xml"] = xml
        peer_hosts = _peer_hosts(ctx["peers"])
        for host in peer_hosts:
            _lvm._run_on(
                host,
                f"cat > /tmp/{ctx['vm_name']}.xml << 'EOF'\n{xml}\nEOF",
            )

    @step("virsh_define")
    def step_define(self, ctx):
        """virsh define on every peer. Idempotent — defining an
        already-defined VM updates the config in place."""
        peer_hosts = _peer_hosts(ctx["peers"])
        for host in peer_hosts:
            _lvm._run_on(
                host, f"virsh define /tmp/{ctx['vm_name']}.xml",
                check=False,
            )

    @step("register_vm")
    def step_register_vm(self, ctx):
        """Write the vms row (or update if exists). Mark state =
        'created' (NOT 'running' yet — start_if_requested handles
        that). Also records failover_order — the predetermined
        primary/secondary/tertiary sequence the failover orchestrator
        consults on a surviving node to decide whether it is next in
        line after a dead primary. Cattle gets '[]' (no failover);
        pet/vipet get ctx["peers"] verbatim — peers[0] is the
        primary (= ctx["home"]), peers[1] is the secondary, peers[2]
        is the tertiary for vipet."""
        from bedrock_d import state as _st
        import time as _t
        if ctx["vm_type"] == "cattle":
            failover_order = []
        else:
            failover_order = list(ctx["peers"])
        with _st.RqliteClient() as client:
            client.execute(
                "INSERT OR REPLACE INTO vms "
                "(vm_name, vm_type, host, ram_mb, disk_gb, state, "
                " failover_order, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'created', ?, ?)",
                params=[ctx["vm_name"], ctx["vm_type"],
                        ctx["home"], int(ctx["ram_mb"]),
                        int(ctx["disk_gb"]),
                        json.dumps(failover_order),
                        int(_t.time())],
            )


# ─── Helpers ─────────────────────────────────────────────────────────


def _peer_hosts(peer_names: list[str]) -> list[str]:
    """Look up LAN IPs for the given node names from the rqlite nodes
    table (level='none', works without quorum)."""
    if not peer_names:
        return []
    from lib import rqlite_client
    placeholders = ",".join("?" * len(peer_names))
    with rqlite_client.RqliteClient() as _rc:
        rows = _rc.query(
            f"SELECT node_name, host FROM nodes WHERE node_name IN ({placeholders})",
            params=peer_names, level="none",
        )
    hosts = {r["node_name"]: r["host"] for r in rows}
    out = []
    for n in peer_names:
        h = hosts.get(n)
        if not h:
            raise RuntimeError(f"node {n!r} has no host in rqlite nodes table")
        out.append(h)
    return out


def _peer_metadata(peer_names: list[str]) -> list[_cfg.Peer]:
    """Build the Peer objects (with node_id assigned by position +
    loopback_ip looked up from rqlite) for drbd_config.render."""
    if not peer_names:
        return []
    from lib import rqlite_client
    placeholders = ",".join("?" * len(peer_names))
    with rqlite_client.RqliteClient() as _rc:
        rows = _rc.query(
            f"SELECT node_name, host, loopback_ip FROM nodes WHERE node_name IN ({placeholders})",
            params=peer_names, level="none",
        )
    nodes = {r["node_name"]: {"host": r["host"], "loopback_ip": r["loopback_ip"]} for r in rows}
    out = []
    for i, n in enumerate(peer_names):
        info = nodes.get(n) or {}
        host = info.get("host")
        lo = info.get("loopback_ip", "")
        if not host or not lo:
            raise RuntimeError(
                f"node {n!r}: host={host!r} loopback={lo!r} — "
                f"cluster.json incomplete")
        out.append(_cfg.Peer(node_name=n, host=host, loopback_ip=lo,
                              node_id=i))
    return out
