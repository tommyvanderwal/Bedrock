"""VmCreate saga — `bedrock vm create` flow (single live path).

Steps cover, per disk: allocate DRBD minor, lvcreate data + meta on
every peer, write .res config, drbdadm create-md / up / primary; then
image-fill disk0, virt-install on the home node, propagate the domain
definition to every peer, register in rqlite.

Cattle VMs have no DRBD: their disks are plain local thin LVs and the
DRBD steps no-op. Pet/vipet disks are external-meta DRBD resources
(per-disk) on every peer so any peer can take over on host death.

ctx inputs (from caller / mgmt /api/vms endpoint):
  - vm_name:   str
  - vcpus:     int
  - ram_mb:    int
  - disk_gb:   int            (size of the boot disk, disk0)
  - extra_disks: list[int]    (extra data-disk sizes in GiB; vdb, vdc…)
  - vm_type:   "cattle" | "pet" | "vipet"
  - priority:  "low" | "normal" | "high"
  - iso:       Optional[str]   (filename in /mnt/bedrock/iso/)
  - peers:     list[node_name] (1 for cattle, 2 for pet, 3 for vipet)
  - home:      node_name where the VM runs (== peers[0])

ctx outputs (filled as steps run):
  - disks:     list[{"index", "resource", "size_gb", "minor", "port",
                     "data_lv", "meta_lv"}]
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

# DRBD minors are laid out so every resource's port lands in the
# 7700-7799 band (drbd_port_for = 7700 + minor - 1100): the cluster
# singleton is minor 1101, per-VM disks live at 1102..1189. Minors
# 1132/1133/1134 are skipped (they map to the netd mesh-probe UDP
# port 7732, advert port 7733, and the node-to-node election heartbeat
# port 7734 = netd.HB_PORT). ~87 VM-disk resources per node fit in the band.
VM_MINOR_BASE = 1102
VM_MINOR_MAX  = 1189
_RESERVED_MINORS = {1132, 1133, 1134}

ALPINE_URL = (
    "https://dl-cdn.alpinelinux.org/alpine/v3.21/releases/cloud/"
    "nocloud_alpine-3.21.0-x86_64-bios-cloudinit-r0.qcow2"
)
ISO_MOUNT_DIR = "/mnt/bedrock/iso"


@saga("vm_create")
class VmCreate:
    """Create a VM (cattle / pet / vipet) as a saga."""

    # ─── Pre-flight ──────────────────────────────────────────────────

    @step("validate_request")
    def step_validate(self, ctx):
        """Sanity-check the params + materialise the per-disk plan.
        Cheap; runs first so we fail before allocating anything if the
        request is malformed."""
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
        # disk0 (boot) + any extra data disks. Each entry tracks its
        # index, resource name and size; minors/LVs get filled in by
        # allocate_minors / register_drbd_resources.
        sizes = [int(ctx["disk_gb"])] + [int(s) for s in
                                         (ctx.get("extra_disks") or [])]
        ctx["disks"] = [
            {"index": i, "resource": f"vm-{ctx['vm_name']}-disk{i}",
             "size_gb": sz}
            for i, sz in enumerate(sizes)
        ]
        ctx["is_replicated"] = ctx["vm_type"] in ("pet", "vipet")

    @step("allocate_minors")
    def step_allocate_minors(self, ctx):
        """Pick a free DRBD minor in 1102..1189 for each replicated
        disk by reading ``drbd_resources`` in rqlite. Idempotent —
        reuse the row's minor on resume. Cattle disks get no minor."""
        if not ctx["is_replicated"]:
            return
        from bedrock_d import state as _st
        with _st.RqliteClient() as client:
            existing = {
                r["name"]: int(r["minor"]) for r in client.query(
                    "SELECT name, minor FROM drbd_resources WHERE minor "
                    "BETWEEN ? AND ?",
                    params=[VM_MINOR_BASE, VM_MINOR_MAX],
                )
            }
        used = set(existing.values())
        free = (m for m in range(VM_MINOR_BASE, VM_MINOR_MAX + 1)
                if m not in used and m not in _RESERVED_MINORS)
        for d in ctx["disks"]:
            minor = existing.get(d["resource"])
            if minor is None:
                minor = next(free, None)
                if minor is None:
                    raise RuntimeError(
                        f"no free DRBD minor in "
                        f"{VM_MINOR_BASE}..{VM_MINOR_MAX}")
                used.add(minor)
            d["minor"] = minor
            d["port"] = _cfg.drbd_port_for(minor)
            log.info("vm_create: disk%d=%s minor=%d",
                     d["index"], d["resource"], minor)

    @step("register_drbd_resources")
    def step_register_resources(self, ctx):
        """Pre-record each replicated disk's resource row so a crash
        mid-saga is recoverable: on resume we already have minor +
        intended peers. INSERT OR REPLACE so retry is fine. Cattle
        disks have no DRBD row."""
        for d in ctx["disks"]:
            pair = _lvm.lv_names_for(d["resource"])
            d["data_lv"] = pair.data_lv
            d["meta_lv"] = pair.meta_lv
        if not ctx["is_replicated"]:
            return
        from bedrock_d import state as _st
        import time as _t
        peers_json = json.dumps(ctx["peers"])
        now = int(_t.time())
        with _st.RqliteClient() as client:
            for d in ctx["disks"]:
                data_bytes = d["size_gb"] * 1024 * 1024 * 1024
                meta_bytes = (_lvm.meta_size_mb_for(d["size_gb"])
                              * 1024 * 1024)
                client.execute(
                    "INSERT OR REPLACE INTO drbd_resources "
                    "(name, minor, data_lv, meta_lv, thinpool, "
                    " data_size_bytes, meta_size_bytes, max_peers, "
                    " peers, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, 'thinpool', ?, ?, 7, ?, ?, ?)",
                    params=[d["resource"], d["minor"], d["data_lv"],
                            d["meta_lv"], data_bytes, meta_bytes,
                            peers_json, now, now],
                )

    # ─── Storage provisioning (data + meta LV pair on every peer) ───

    @step("lvcreate_on_peers")
    def step_lvcreate(self, ctx):
        """Create the disk LVs on every peer. Replicated disks get a
        data + meta pair via lvcreate_pair; cattle disks get a single
        local data LV. Idempotent — both check ``lvs`` first."""
        peer_hosts = _peer_hosts(ctx["peers"])
        for host in peer_hosts:
            for d in ctx["disks"]:
                if ctx["is_replicated"]:
                    _lvm.lvcreate_pair(host, d["resource"], d["size_gb"])
                elif not _lvm.lv_exists(host, d["data_lv"]):
                    _lvm._run_on(
                        host,
                        f"lvcreate -y -V {d['size_gb']}G --thin "
                        f"-n {d['data_lv']} {_lvm.VG_NAME}/{_lvm.THINPOOL}")

    @step("write_drbd_config")
    def step_write_config(self, ctx):
        """Write /etc/drbd.d/<resource>.res on every peer for each
        replicated disk. The config is content-deterministic given
        (peers, minor) so re-running overwrites with the same bytes."""
        if not ctx["is_replicated"]:
            return
        peers_meta = _peer_metadata(ctx["peers"])
        for d in ctx["disks"]:
            config = _cfg.render(
                d["resource"], minor=d["minor"], peers=peers_meta)
            res_path = _cfg.res_file_path(d["resource"])
            for p in peers_meta:
                _lvm._run_on(
                    p.host,
                    f"cat > {res_path} << 'EOF'\n{config}EOF")

    @step("drbd_create_md")
    def step_create_md(self, ctx):
        """drbdadm create-md --max-peers=7 on every peer, per disk.
        Idempotent — if metadata already exists drbdadm exits non-
        zero, which we tolerate (later steps catch real problems)."""
        if not ctx["is_replicated"]:
            return
        for host in _peer_hosts(ctx["peers"]):
            for d in ctx["disks"]:
                _lvm._run_on(
                    host,
                    f"drbdadm create-md --force --max-peers=7 "
                    f"{d['resource']} 2>&1 | tail -2",
                    check=False)

    @step("drbd_up")
    def step_drbd_up(self, ctx):
        """drbdadm up on every peer, per disk. Idempotent."""
        if not ctx["is_replicated"]:
            return
        for host in _peer_hosts(ctx["peers"]):
            for d in ctx["disks"]:
                _lvm._run_on(host, f"drbdadm up {d['resource']}",
                             check=False)

    @step("drbd_primary")
    def step_drbd_primary(self, ctx):
        """drbdadm primary --force on the home node, per disk. Required
        so the image step can write disk0 and so libvirt can boot.
        Idempotent — promoting an already-Primary resource is a no-op."""
        if not ctx["is_replicated"]:
            return
        home_host = _peer_hosts([ctx["home"]])[0]
        for d in ctx["disks"]:
            _lvm._run_on(
                home_host,
                f"drbdadm primary --force {d['resource']}",
                check=False)

    # ─── Image fill ──────────────────────────────────────────────────

    @step("write_boot_image")
    def step_write_image(self, ctx):
        """When no ISO is given, write the cached Alpine image onto
        disk0's block device on the home node so the VM has something
        to boot. Idempotent — skips if the device already carries a
        filesystem signature. ISO-booted VMs install from CDROM."""
        if ctx.get("iso"):
            return
        home_host = _peer_hosts([ctx["home"]])[0]
        d0 = ctx["disks"][0]
        device = (f"/dev/drbd{d0['minor']}" if ctx["is_replicated"]
                  else _lvm.lv_path(d0["data_lv"]))
        rc, _, _ = _lvm._run_on(
            home_host, f"blkid -s TYPE -o value {device} 2>/dev/null",
            check=False, timeout=5)
        if rc == 0:
            log.info("vm_create: %s already has data; skipping image fill",
                     device)
            return
        _lvm._run_on(
            home_host,
            "mkdir -p /var/lib/bedrock; "
            "test -f /var/lib/bedrock/alpine.qcow2 || "
            f"  curl -sfL -o /var/lib/bedrock/alpine.qcow2 '{ALPINE_URL}'",
            timeout=300)
        _lvm._run_on(
            home_host,
            f"qemu-img convert -f qcow2 -O raw "
            f"/var/lib/bedrock/alpine.qcow2 {device}")

    # ─── libvirt define + register ───────────────────────────────────

    @step("virsh_install")
    def step_install(self, ctx):
        """virt-install --import (or --cdrom) on the home node to
        define + register the domain, then dump its XML into ctx for
        propagation to peers. Idempotent — if the domain is already
        defined here, skip straight to the XML dump."""
        home_host = _peer_hosts([ctx["home"]])[0]
        rc, _, _ = _lvm._run_on(
            home_host, f"virsh dominfo {ctx['vm_name']}",
            check=False, timeout=10)
        if rc != 0:
            disk_args = " ".join(
                f"--disk path="
                f"{(f'/dev/drbd' + str(d['minor'])) if ctx['is_replicated'] else _lvm.lv_path(d['data_lv'])}"
                ",format=raw,bus=virtio,cache=none,discard=unmap"
                for d in ctx["disks"])
            if ctx.get("iso"):
                iso_path = f"{ISO_MOUNT_DIR}/{Path(ctx['iso']).name}"
                media = f"--cdrom {iso_path}"
                boot = "--boot cdrom,hd"
            else:
                media = "--import"
                boot = "--boot hd"
            _lvm._run_on(
                home_host,
                f"virt-install --name {ctx['vm_name']} "
                f"--vcpus {int(ctx['vcpus'])} --ram {int(ctx['ram_mb'])} "
                f"{disk_args} "
                f"--network bridge=br0,model=virtio "
                f"--graphics vnc,listen=0.0.0.0 "
                f"--channel unix,target_type=virtio,"
                f"name=org.qemu.guest_agent.0 "
                f"--os-variant detect=on,name=generic "
                f"--noautoconsole {media} {boot} 2>&1 | tail -5")
        _, xml, _ = _lvm._run_on(
            home_host, f"virsh dumpxml {ctx['vm_name']}")
        ctx["libvirt_xml"] = xml

    @step("virsh_define_on_peers")
    def step_define_peers(self, ctx):
        """Propagate the domain definition to every other peer so any
        of them can run the VM on failover. Cattle (single peer == home)
        no-ops. Idempotent — virsh define updates in place."""
        if not ctx["is_replicated"]:
            return
        xml = ctx["libvirt_xml"]
        home = ctx["home"]
        for name, host in zip(ctx["peers"], _peer_hosts(ctx["peers"])):
            if name == home:
                continue
            _lvm._run_on(
                host,
                f"cat > /tmp/{ctx['vm_name']}.xml << 'EOF'\n{xml}\nEOF")
            rc, _, err = _lvm._run_on(
                host, f"virsh define /tmp/{ctx['vm_name']}.xml",
                check=False)
            if rc != 0:
                log.warning("vm_create: virsh define on peer %s FAILED "
                            "rc=%d: %s — VM cannot fail over to this peer",
                            host, rc, (err or "").strip()[:200])

    @step("record_disk_uuids")
    def step_record_uuids(self, ctx):
        """Record each replicated disk's post-promote DRBD current-UUID
        in rqlite so the first failover's exact-equality safety check
        (INV-5) has a quorum-confirmed baseline. Cattle no-ops."""
        if not ctx["is_replicated"]:
            return
        from bedrock_d.vm.failover import record_uuid_after_promote
        for d in ctx["disks"]:
            try:
                record_uuid_after_promote(d["resource"])
            except Exception as e:
                log.warning("vm_create: record UUID for %s skipped: %s",
                            d["resource"], e)

    @step("register_vm")
    def step_register_vm(self, ctx):
        """Write the vms row. Records failover_order — the predetermined
        primary/secondary/tertiary sequence the failover orchestrator
        consults on a surviving node to decide whether it is next in
        line after a dead primary. Cattle gets '[]' (no failover);
        pet/vipet get ctx["peers"] verbatim (peers[0]=home=primary).
        State = 'running' — virt-install started the domain."""
        from bedrock_d import state as _st
        import time as _t
        failover_order = [] if ctx["vm_type"] == "cattle" else list(ctx["peers"])
        # HA-importance drives self-heal replica-restore ordering.
        priority = (ctx.get("priority") or "normal")
        if priority not in ("low", "normal", "high"):
            priority = "normal"
        with _st.RqliteClient() as client:
            client.execute(
                "INSERT OR REPLACE INTO vms "
                "(vm_name, vm_type, host, ram_mb, disk_gb, state, "
                " failover_order, priority, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?)",
                params=[ctx["vm_name"], ctx["vm_type"],
                        ctx["home"], int(ctx["ram_mb"]),
                        int(ctx["disk_gb"]),
                        json.dumps(failover_order),
                        priority,
                        int(_t.time())],
            )

    @step("store_libvirt_xml")
    def step_store_libvirt_xml(self, ctx):
        """Persist the domain XML in cluster state so a node that JOINED
        LATER (and so isn't in the create-time virsh-define-on-peers) can
        re-`virsh define` + take over the VM on failover. MUST run AFTER
        register_vm: register_vm's `INSERT OR REPLACE INTO vms` rewrites the
        whole row and would otherwise clobber libvirt_xml back to '' (steps
        run in source order; the executor re-runs this after any register_vm
        retry too). Replicated VMs only — cattle don't fail over."""
        if not ctx.get("is_replicated"):
            return
        xml = ctx.get("libvirt_xml") or ""
        if not xml.strip():
            log.warning("vm_create: no libvirt_xml captured for %s — failover "
                        "to a later-joining node may need a manual define",
                        ctx["vm_name"])
            return
        from lib import bedrock_state as _bs  # type: ignore
        _bs.vm_set_libvirt_xml(ctx["vm_name"], xml)


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
                f"missing from rqlite nodes table")
        out.append(_cfg.Peer(node_name=n, host=host, loopback_ip=lo,
                              node_id=i))
    return out
