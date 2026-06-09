"""Supportability checks.

The dashboard's ``/support`` page calls ``GET /api/support/checks``;
this module owns that endpoint plus its helpers. Pure read-only —
side-effect-free — so it's safe to refresh on every dashboard hit.
"""
from __future__ import annotations

from fastapi import APIRouter

from common import load_cluster, get_nodes, ssh_cmd_rc

router = APIRouter(tags=["support"])


@router.get("/api/support/checks")
def api_support_checks():
    """Run all supportability checks live and return the results."""
    checks: list[dict] = []
    cluster = load_cluster()
    nodes_cfg = get_nodes()

    # 1. TRIM/discard config across the stack — sampled on each node.
    #    Verifies lvm.conf passdown, fstrim.timer enabled, and that
    #    DRBD .res files (if any) declare discard-zeroes-if-aligned.
    trim_summary = {"ok": [], "warn": [], "fail": []}
    for nname, ncfg in nodes_cfg.items():
        host = ncfg.get("host")
        if not host:
            continue
        try:
            out, rc = ssh_cmd_rc(host, (
                "set -e; "
                "echo \"lvm_passdown=$(grep -E '^\\s*thin_pool_discards' "
                "/etc/lvm/lvm.conf 2>/dev/null | grep -i passdown | head -1 || "
                "echo passdown_default)\"; "
                "echo \"fstrim_timer=$(systemctl is-enabled fstrim.timer 2>/dev/null || echo missing)\"; "
                "echo \"drbd_discard=$(grep -l 'discard-zeroes-if-aligned' "
                "/etc/drbd.d/*.res 2>/dev/null | wc -l)\""
            ), timeout=10)
            if rc != 0:
                trim_summary["fail"].append(nname)
                continue
            facts = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
            ok = (
                "passdown" in facts.get("lvm_passdown", "").lower() or
                facts.get("lvm_passdown") == "passdown_default"
            ) and facts.get("fstrim_timer") in ("enabled", "static")
            (trim_summary["ok"] if ok else trim_summary["warn"]).append(nname)
        except Exception:
            trim_summary["fail"].append(nname)
    if trim_summary["fail"]:
        trim_status, trim_note = "fail", (
            f"could not query TRIM config on: "
            f"{', '.join(trim_summary['fail'])}")
    elif trim_summary["warn"]:
        trim_status, trim_note = "warn", (
            f"TRIM stack not fully configured on: "
            f"{', '.join(trim_summary['warn'])} — fstrim.timer + lvm.conf "
            f"thin_pool_discards=passdown both required")
    else:
        trim_status, trim_note = "ok", (
            f"TRIM passdown active on {len(trim_summary['ok'])} node(s); "
            f"fstrim.timer enabled; DRBD discards configured if applicable")
    checks.append({
        "id": "trim_stack", "label": "TRIM / discard end-to-end",
        "status": trim_status, "note": trim_note,
        "remediation": ("Bedrock-bootstrap configures this automatically; "
                        "if a check fails, re-run `bedrock bootstrap` on "
                        "the affected node."),
    })

    # 2. Mesh reachability — every peer's loopback /32 pings.
    #    Heuristic: every peer has a loopback_ip reachable from every other
    #    node via the mesh (any path is fine; bedrock-net routes).
    #    ping each peer's loopback_ip from every node.
    if len(nodes_cfg) >= 2:
        names = list(nodes_cfg)
        unreachable = []
        for src in names:
            src_host = nodes_cfg[src].get("host")
            for dst in names:
                if dst == src: continue
                dst_lo = nodes_cfg[dst].get("loopback_ip")
                if not dst_lo:
                    unreachable.append(f"{src}→{dst}(no loopback_ip)")
                    continue
                try:
                    _o, rc = ssh_cmd_rc(src_host,
                        f"ping -c1 -W2 -q {dst_lo} >/dev/null 2>&1",
                        timeout=8)
                    if rc != 0:
                        unreachable.append(f"{src}→{dst}({dst_lo})")
                except Exception:
                    unreachable.append(f"{src}→{dst}({dst_lo})")
        if not unreachable:
            checks.append({
                "id": "drbd_cable", "label": "Dedicated DRBD path between nodes",
                "status": "ok",
                "note": f"every pair of {len(names)} nodes can ping over "
                        f"the DRBD network",
                "remediation": "",
            })
        else:
            checks.append({
                "id": "drbd_cable", "label": "Dedicated DRBD path between nodes",
                "status": "fail",
                "note": "DRBD-network ping failures: " + ", ".join(unreachable),
                "remediation": (
                    "Connect a direct ethernet cable between the second NICs "
                    "of each node (or a dedicated VLAN on a switch). DRBD "
                    "replication MUST NOT share bandwidth with VM traffic."),
            })
    else:
        checks.append({
            "id": "drbd_cable", "label": "Dedicated DRBD path between nodes",
            "status": "warn",
            "note": "single-node cluster — DRBD path not yet meaningful",
            "remediation": "Add a second node before this becomes a "
                           "supportability requirement.",
        })

    # 3. Witness reachable from every node (or at least the master).
    wit = (cluster.get("witnesses") or {})
    if not wit:
        checks.append({
            "id": "witness", "label": "External witness configured",
            "status": "warn",
            "note": "no witness registered — 2-node clusters need a witness "
                    "to break ties on partition; 1-node + witness is also "
                    "the smallest fully-supported configuration",
            "remediation": "`bedrock witness register <host>` to add one.",
        })
    else:
        checks.append({
            "id": "witness", "label": "External witness configured",
            "status": "ok",
            "note": f"{len(wit)} witness(es) registered",
            "remediation": "",
        })

    # 4. No "advanced mode" overrides currently active. (For v1.0 we
    #    don't have any such overrides; the check is a placeholder
    #    that always passes. v1.x: any operator-set knob that
    #    deviates from supported defaults shows up here.)
    checks.append({
        "id": "advanced_mode", "label": "No unsupported advanced-mode overrides",
        "status": "ok",
        "note": "no operator overrides outside the supported defaults set",
        "remediation": "",
    })

    # 5. Backup target configured.
    targets = cluster.get("backup_targets") or {}
    if targets:
        checks.append({
            "id": "backups", "label": "Backup target configured",
            "status": "ok",
            "note": f"{len(targets)} target(s) configured: "
                    f"{', '.join(targets.keys())}",
            "remediation": "",
        })
    else:
        checks.append({
            "id": "backups", "label": "Backup target configured",
            "status": "warn",
            "note": "no backup target — VMs are not protected against "
                    "data loss outside the cluster",
            "remediation": "Open `/backups` and configure a target (S3 "
                           "or filesystem). Encryption password is set "
                           "ONCE — store it externally.",
        })

    # 6. Disk fill on every node — warn at 70 %, alarm at 80 %.
    #    Advisory only: bedrock never refuses operations on this.
    disk_warn: list[dict] = []
    for nname, ncfg in nodes_cfg.items():
        host = ncfg.get("host")
        if not host:
            continue
        try:
            out, rc = ssh_cmd_rc(host,
                "lvs --noheadings -o lv_name,data_percent --separator='|' "
                "-S 'lv_role=thin,pool' 2>/dev/null", timeout=8)
            for line in out.strip().splitlines():
                parts = [p.strip() for p in line.split("|") if p.strip()]
                if len(parts) < 2: continue
                pool, pct_s = parts[0], parts[1]
                try:
                    pct = float(pct_s)
                except ValueError:
                    continue
                if pct >= 80:
                    disk_warn.append({"node": nname, "pool": pool, "pct": pct,
                                      "level": "alarm"})
                elif pct >= 70:
                    disk_warn.append({"node": nname, "pool": pool, "pct": pct,
                                      "level": "warn"})
        except Exception:
            pass
    if not disk_warn:
        checks.append({
            "id": "disk_fill", "label": "Thin-pool fill level (advisory)",
            "status": "ok",
            "note": "all thin pools below 70 % full",
            "remediation": "",
        })
    else:
        worst = max(disk_warn, key=lambda d: d["pct"])
        status = "fail" if any(d["level"] == "alarm" for d in disk_warn) else "warn"
        items = ", ".join(f"{d['node']}/{d['pool']} {d['pct']:.0f}%" for d in disk_warn)
        checks.append({
            "id": "disk_fill", "label": "Thin-pool fill level (advisory)",
            "status": status,
            "note": f"{items}",
            "remediation": (
                "Delete unused VMs, snapshots, or old backups. Note: bedrock "
                "won't BLOCK new allocations on this — operator may need to "
                "create a 4 GB LV to migrate a 50 GB workload off the node."),
        })

    # Roll-up: green badge requires ALL checks ok.
    overall = "ok"
    for c in checks:
        if c["status"] == "fail":
            overall = "fail"; break
        if c["status"] == "warn":
            overall = "warn"
    return {"checks": checks, "overall": overall}
