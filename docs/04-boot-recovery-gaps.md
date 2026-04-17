# Boot Recovery Gaps — What Doesn't Auto-Recover Yet

## After a full power outage (all nodes + switch off simultaneously)

### What WORKS after boot

```
  ✅  AlmaLinux boots
  ✅  Networking (br0 bridge, eno1 direct link) — via NetworkManager
  ✅  DRBD module loads — via /etc/modules-load.d/drbd.conf
  ✅  DRBD resources come up — via drbd-resources.service
  ✅  DRBD reconnects and resyncs between nodes automatically
  ✅  Failover orchestrator starts — via bedrock-failover.service
  ✅  MikroTik switch boots with config intact
  ✅  Witness container image persists on MikroTik flash
```

### What FAILS after boot — needs manual fix

```
  ❌  libvirtd does not auto-start
      Fix: systemctl enable libvirtd
      Root cause: libvirtd was started but never "enabled" for boot
      One-time fix: run on both nodes

  ❌  VMs do not auto-start
      Fix: needs orchestrator or virsh autostart logic
      Design decision: VMs should NOT blindly autostart — the
      orchestrator should decide which node starts which VM
      based on DRBD roles and quorum state

  ❌  Witness container root filesystem lost on MikroTik reboot
      Fix: re-import container from tar.gz + start
      Root cause: MikroTik extracts container rootfs to RAM/tmpfs,
      not persistent flash. The tar.gz image persists but the
      extracted rootfs does not survive reboot.
      Status: start-on-boot=true should handle re-extraction,
      but it failed this time. Needs investigation.

  ❌  DRBD roles are both Secondary after simultaneous boot
      Fix: orchestrator should auto-promote based on last known state
      Current: orchestrator only promotes on failover, not on cold boot
      Need: startup logic that checks "am I the expected primary for
      this resource?" and promotes accordingly
```

## Required fixes for production

### 1. Enable libvirtd on boot (one-time, both nodes)

```bash
systemctl enable libvirtd    # already done during this recovery
```

### 2. Add cold-boot VM startup to orchestrator

The orchestrator needs a startup phase:
- On boot, check DRBD state for each resource
- If both nodes are Secondary (cold boot), one must promote
- Use a deterministic rule: node with lower node-id promotes its
  "home" resources (node1 → vm-test, node2 → vm-win)
- Or: read last-known ownership from a state file
- After promoting, start the VMs

### 3. Fix witness container persistence on MikroTik

Options:
- Investigate why start-on-boot didn't re-extract the rootfs
- Add a MikroTik scheduler script that checks container state
  and re-imports if needed
- Store the container image on flash and auto-import on boot

### 4. Orchestrator should handle "both nodes booting simultaneously"

Current gap: if both nodes boot at the same time, both see
"peer alive" and neither takes action. Both DRBD resources
stay Secondary, no VMs start.

Fix: add a startup negotiation phase where nodes agree who
owns which resource via the witness as tiebreaker.

## Recovery timeline from this outage

```
  T+0s    Power returns, all devices boot
  T+30s   Both nodes up, DRBD connected, both Secondary
  T+30s   Failover orchestrator running, peer alive, no action
  T+30s   MikroTik up, witness container NOT running

  Manual steps needed:
  1. systemctl start libvirtd (both nodes)         ~5s
  2. Re-import witness container on MikroTik        ~10s
  3. drbdadm primary + virsh start (each node)      ~10s

  Total manual recovery time: ~2 minutes
  Target for v0.5: fully automatic, 0 manual steps
```
