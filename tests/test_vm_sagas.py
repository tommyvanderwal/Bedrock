"""Tests for the four VM lifecycle sagas + LVM/DRBD helpers.

Two layers:
- **Helper math** — meta_size_mb_for, drbd_port_for, render_config.
  Pure functions; no shell-out; cheap.
- **Saga contracts** — step list shape + architectural invariants.
  Step bodies aren't executed (they shell out); they're audited
  by listing the step set.

EXPECTED_STEPS lists are the load-bearing contract per saga —
any change there means a change to the VM lifecycle, which wants
deliberate review.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

# Import side-effect registers all four sagas.
from bedrock_d.vm import create as _vm_create  # noqa: F401
from bedrock_d.vm import destroy as _vm_destroy  # noqa: F401
from bedrock_d.vm import grow as _vm_grow  # noqa: F401
from bedrock_d.vm import migrate as _vm_migrate  # noqa: F401
from bedrock_d.vm import drbd_config as _cfg
from bedrock_d.vm import lvm as _lvm
from bedrock_d.orchestrator.sagas import SAGAS
from bedrock_d.orchestrator.sagas.executor import _ordered_steps


# ───────────────────────────────────────────────────────────────────
# LVM helper math
# ───────────────────────────────────────────────────────────────────


def test_meta_size_baseline_for_small_disk():
    """A 20 GiB disk with max_peers=7:
      header (4) + AL (32) + 7 peers × 20 GiB × 32 KiB/GiB/peer
      = 36 + 4480 KiB
      = 36 + 4.375 MiB → 41 MiB → round up to 44.

    The 4-MiB rounding gives extent-aligned LVM allocations + a
    touch of safety margin for DRBD format drift."""
    assert _lvm.meta_size_mb_for(20) == 44


def test_meta_size_scales_with_data():
    """Bitmap grows linearly with data_gb. Header + AL are
    constant (36 MiB)."""
    small = _lvm.meta_size_mb_for(10)
    big = _lvm.meta_size_mb_for(100)
    # Big should be ~10x bigger bitmap component, but with the
    # fixed 36 MiB floor. Just check monotonic growth.
    assert big > small


def test_meta_size_for_terabyte_disk():
    """A 1 TiB VM disk (1024 GiB) with max_peers=7:
      header (4) + AL (32) + 7 × 1024 × 32 KiB
      = 36 + 229_376 KiB
      = 36 + 224 MiB
      = 260 MiB → round up to 260 (already a multiple of 4)."""
    assert _lvm.meta_size_mb_for(1024) == 260


def test_meta_size_rejects_zero_data():
    with pytest.raises(ValueError):
        _lvm.meta_size_mb_for(0)


def test_meta_size_rejects_too_many_peers():
    with pytest.raises(ValueError):
        _lvm.meta_size_mb_for(100, max_peers=16)


def test_meta_size_scales_with_peer_count():
    """More peers = more bitmap. 1 peer vs 7 peers on same disk."""
    one = _lvm.meta_size_mb_for(100, max_peers=1)
    seven = _lvm.meta_size_mb_for(100, max_peers=7)
    assert seven > one


def test_lv_names_canonical_form():
    pair = _lvm.lv_names_for("vm-foo-disk0")
    assert pair.data_lv == "bedrock-data-vm-foo-disk0"
    assert pair.meta_lv == "bedrock-meta-vm-foo-disk0"


def test_lv_names_rejects_empty_resource():
    with pytest.raises(ValueError):
        _lvm.lv_names_for("")


# ───────────────────────────────────────────────────────────────────
# DRBD config rendering
# ───────────────────────────────────────────────────────────────────


def test_drbd_port_offset():
    """Port = 7700 + (minor - 1100), keeping every DRBD resource inside
    the documented 7700-7799 band (SG-03). The cluster singleton and
    every per-VM disk share this one formula. Minors are laid out so
    the band is honoured: cluster=1101→7701, VM disks 1102..1189→
    7702..7789 (clear of 9333/8333/8080/8443/4001/4002/4011/4012)."""
    assert _cfg.drbd_port_for(1100) == 7700   # band base
    assert _cfg.drbd_port_for(1101) == 7701   # cluster-singleton
    assert _cfg.drbd_port_for(1102) == 7702   # first VM-disk slot
    assert _cfg.drbd_port_for(1189) == 7789   # last VM-disk slot in band


def test_render_drbd_two_peers():
    peers = [
        _cfg.Peer(node_name="n1", host="192.168.2.1",
                   loopback_ip="100.5.5.1", node_id=0),
        _cfg.Peer(node_name="n2", host="192.168.2.2",
                   loopback_ip="100.5.5.2", node_id=1),
    ]
    text = _cfg.render("vm-test-disk0", minor=1102, peers=peers)
    # On-blocks present for both peers
    assert "on n1" in text
    assert "on n2" in text
    # External meta — points at the meta LV, not "internal"
    assert "meta-disk /dev/bedrock/bedrock-meta-vm-test-disk0" in text
    # Connection block for the n1-n2 pair — port 7702 (minor 1102, in band)
    assert "100.5.5.1:7702" in text
    assert "100.5.5.2:7702" in text
    # protocol C is non-negotiable for VMs (synchronous write ack)
    assert "protocol C" in text


def test_render_drbd_three_peers_full_mesh():
    """vipet = 3 peers = 3 pair connection blocks (n1-n2, n1-n3, n2-n3)."""
    peers = [
        _cfg.Peer(node_name="n1", host="h1", loopback_ip="10.0.0.1", node_id=0),
        _cfg.Peer(node_name="n2", host="h2", loopback_ip="10.0.0.2", node_id=1),
        _cfg.Peer(node_name="n3", host="h3", loopback_ip="10.0.0.3", node_id=2),
    ]
    text = _cfg.render("vm-vipet-disk0", minor=1103, peers=peers)
    # Three connection blocks (count occurrences)
    assert text.count("connection {") == 3
    # Every peer's loopback appears at least once
    for p in peers:
        assert p.loopback_ip in text


def test_render_drbd_rejects_duplicate_node_id():
    peers = [
        _cfg.Peer(node_name="a", host="h", loopback_ip="1.1.1.1", node_id=0),
        _cfg.Peer(node_name="b", host="h", loopback_ip="2.2.2.2", node_id=0),
    ]
    with pytest.raises(ValueError):
        _cfg.render("vm-test-disk0", minor=1200, peers=peers)


def test_render_drbd_rejects_no_peers():
    with pytest.raises(ValueError):
        _cfg.render("vm-test-disk0", minor=1200, peers=[])


def test_render_drbd_rejects_too_many_peers():
    peers = [
        _cfg.Peer(node_name=f"n{i}", host=f"h{i}",
                   loopback_ip=f"10.0.0.{i+1}", node_id=i)
        for i in range(8)
    ]
    with pytest.raises(ValueError):
        _cfg.render("vm-test-disk0", minor=1200, peers=peers, max_peers=7)


# ───────────────────────────────────────────────────────────────────
# Saga contracts — step lists are the lifecycle docs
# ───────────────────────────────────────────────────────────────────


# Phase 5 cutover: the create saga is now the SINGLE live path for
# every type (cattle / pet / vipet) and is multi-disk aware. The step
# list reflects that:
#   - allocate_minors / register_drbd_resources are plural — they loop
#     over every disk of the VM, not just disk0 (VM-04).
#   - virsh_install replaces the old static-XML branch (which referenced
#     a nonexistent _vm_xml_cattle): it runs virt-install --import on the
#     home node, which both defines AND starts the domain for all types.
#   - record_disk_uuids writes each disk's post-promote DRBD UUID to
#     rqlite so the FIRST failover has a quorum-confirmed baseline
#     (INV-5). Without it the first host-death failover is refused.
VM_CREATE_STEPS = [
    "validate_request",
    "allocate_minors",
    "register_drbd_resources",
    "lvcreate_on_peers",
    "write_drbd_config",
    "drbd_create_md",
    "drbd_up",
    "drbd_primary",
    "write_boot_image",
    "virsh_install",
    "virsh_define_on_peers",
    "record_disk_uuids",
    "register_vm",
]


def test_vm_create_saga_registered():
    assert "vm_create" in SAGAS


def test_vm_create_steps_match_documented_flow():
    declared = [name for (name, _fn) in _ordered_steps(
        SAGAS["vm_create"])]
    assert declared == VM_CREATE_STEPS


def test_vm_create_register_resource_before_lvcreate():
    """The drbd_resources rows are written BEFORE any storage is
    provisioned. Lets a crash-mid-lvcreate resume cleanly: rows
    exist → resume picks same minors + names + peers."""
    declared = [name for (name, _fn) in _ordered_steps(
        SAGAS["vm_create"])]
    assert declared.index("register_drbd_resources") < declared.index(
        "lvcreate_on_peers")


def test_vm_create_drbd_up_before_primary():
    """drbdadm primary --force requires the resource to be up."""
    declared = [name for (name, _fn) in _ordered_steps(
        SAGAS["vm_create"])]
    assert declared.index("drbd_up") < declared.index("drbd_primary")


def test_vm_create_image_write_before_install():
    """Image bytes need to be on the device before libvirt is
    asked to boot from it."""
    declared = [name for (name, _fn) in _ordered_steps(
        SAGAS["vm_create"])]
    assert declared.index("write_boot_image") < declared.index(
        "virsh_install")


def test_vm_create_records_uuid_after_promote():
    """The first failover's INV-5 exact-equality check needs a
    quorum-confirmed UUID baseline; the create saga records it after
    drbd_primary on the home node (VM-02 — same fix the migrate +
    takeover paths apply)."""
    declared = [name for (name, _fn) in _ordered_steps(
        SAGAS["vm_create"])]
    assert declared.index("drbd_primary") < declared.index(
        "record_disk_uuids")


# ─── vm_destroy ────────────────────────────────────────────────────


VM_DESTROY_STEPS = [
    "load_resource_metadata",
    "virsh_destroy_running",
    "virsh_undefine",
    "drbd_down",
    "drbd_wipe_md",
    "remove_drbd_res_file",
    "lvremove_pair",
    "delete_rqlite_rows",
]


def test_vm_destroy_steps_match():
    declared = [name for (name, _fn) in _ordered_steps(
        SAGAS["vm_destroy"])]
    assert declared == VM_DESTROY_STEPS


def test_vm_destroy_destroy_before_undefine():
    """Can't undefine a running VM. Kill it first."""
    declared = [name for (name, _fn) in _ordered_steps(
        SAGAS["vm_destroy"])]
    assert declared.index("virsh_destroy_running") < declared.index(
        "virsh_undefine")


def test_vm_destroy_drbd_down_before_lvremove():
    """Can't lvremove a device that DRBD is actively using."""
    declared = [name for (name, _fn) in _ordered_steps(
        SAGAS["vm_destroy"])]
    assert declared.index("drbd_down") < declared.index("lvremove_pair")


def test_vm_destroy_rqlite_delete_is_last():
    """Operator-visible 'gone' state comes last — so a half-
    destroyed VM still shows up in the dashboard with whatever
    state we last wrote."""
    declared = [name for (name, _fn) in _ordered_steps(
        SAGAS["vm_destroy"])]
    assert declared[-1] == "delete_rqlite_rows"


# ─── vm_grow ───────────────────────────────────────────────────────


VM_GROW_STEPS = [
    "load_current_size",
    "validate_new_size",
    "lvextend_meta_on_peers",
    "lvextend_data_on_peers",
    "drbd_resize",
    "update_drbd_resources_row",
]


def test_vm_grow_steps_match():
    declared = [name for (name, _fn) in _ordered_steps(SAGAS["vm_grow"])]
    assert declared == VM_GROW_STEPS


def test_vm_grow_meta_before_data():
    """The architectural invariant for online grow: extend meta
    first (so bitmap has room for the bigger data device), THEN
    extend data, THEN drbdadm resize. Reversing risks 'meta LV
    out of space' when DRBD recalculates the bitmap."""
    declared = [name for (name, _fn) in _ordered_steps(SAGAS["vm_grow"])]
    assert declared.index("lvextend_meta_on_peers") < declared.index(
        "lvextend_data_on_peers")
    assert declared.index("lvextend_data_on_peers") < declared.index(
        "drbd_resize")


# ─── vm_migrate ────────────────────────────────────────────────────


# Phase 5 cutover: the saga is the SINGLE migrate path (the mgmt
# _vm_migrate and lib.vm.migrate_vm are gone). Two contract changes:
#   - record_uuids_after_migrate writes the new primary's post-promote
#     DRBD UUID to rqlite, so a later host-death failover passes the
#     INV-5 exact-equality gate. Without this, every migrate silently
#     broke HA (VM-02). It runs after the migrate (the promote bumped
#     the UUID) and before the source is demoted.
#   - the saga does NOT pass --undefinesource (see migrate.py): the
#     domain stays defined on the source so it remains a failover
#     target for the migrated VM (VM-03).
VM_MIGRATE_STEPS = [
    "validate_request",
    "enable_dual_primary",
    "drbd_primary_on_target",
    "virsh_migrate_live",
    "record_uuids_after_migrate",
    "drbd_secondary_on_source",
    "disable_dual_primary",
    "update_vms_host",
]


def test_vm_migrate_steps_match():
    declared = [name for (name, _fn) in _ordered_steps(
        SAGAS["vm_migrate"])]
    assert declared == VM_MIGRATE_STEPS


def test_vm_migrate_dual_primary_window_is_bounded():
    """Architectural invariant: dual-primary is enabled BEFORE
    target is promoted, and disabled AFTER source is demoted.
    The window covers the migration + the post-promote UUID record."""
    declared = [name for (name, _fn) in _ordered_steps(
        SAGAS["vm_migrate"])]
    enable_idx  = declared.index("enable_dual_primary")
    promote_idx = declared.index("drbd_primary_on_target")
    migrate_idx = declared.index("virsh_migrate_live")
    demote_idx  = declared.index("drbd_secondary_on_source")
    disable_idx = declared.index("disable_dual_primary")
    assert enable_idx < promote_idx < migrate_idx < demote_idx < disable_idx


def test_vm_migrate_records_uuid_on_new_primary():
    """VM-02: the migrate records the target's post-promote DRBD UUID
    in rqlite (after the live migrate, before demoting the source) so a
    subsequent failover isn't refused by the exact-equality check."""
    declared = [name for (name, _fn) in _ordered_steps(
        SAGAS["vm_migrate"])]
    assert declared.index("virsh_migrate_live") < declared.index(
        "record_uuids_after_migrate")
    assert declared.index("record_uuids_after_migrate") < declared.index(
        "drbd_secondary_on_source")


def test_vm_migrate_host_update_is_last():
    """rqlite reflects the move only after everything actually
    happened; a failed migrate leaves vms.host unchanged so the
    dashboard doesn't lie."""
    declared = [name for (name, _fn) in _ordered_steps(
        SAGAS["vm_migrate"])]
    assert declared[-1] == "update_vms_host"


# ─── No duplicates in any saga ──────────────────────────────────────


@pytest.mark.parametrize("kind",
                          ["vm_create", "vm_destroy", "vm_grow", "vm_migrate"])
def test_no_duplicate_step_names(kind):
    declared = [name for (name, _fn) in _ordered_steps(SAGAS[kind])]
    assert len(declared) == len(set(declared)), (
        f"{kind}: duplicates {[n for n in declared if declared.count(n) > 1]}"
    )
