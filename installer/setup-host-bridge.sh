#!/bin/bash
# Bridge enx0050b6924411 into a new br0 so libvirt VMs can sit on the LAN.
#
# Designed to be one shot — the brief network blip happens between
# `connection up br0` and `connection down netplan-...`.
set -euo pipefail

NIC=enx0050b6924411
OLD_CONN=netplan-${NIC}
BRIDGE=br0

echo "=== Pre-state ==="
ip -br addr show "$NIC" || true
nmcli -t -f NAME,DEVICE connection show --active | grep -E "^${OLD_CONN}|^${BRIDGE}"

# 1. Create the bridge connection (no autoconnect yet — we'll bring it up explicitly).
nmcli connection add type bridge con-name "$BRIDGE" ifname "$BRIDGE" \
    bridge.stp no \
    ipv4.method auto ipv6.method auto \
    connection.autoconnect yes

# 2. Add the bridge-slave pointing at our NIC.
nmcli connection add type ethernet con-name "${BRIDGE}-slave-${NIC}" \
    ifname "$NIC" master "$BRIDGE" slave-type bridge \
    connection.autoconnect yes

# 3. Atomic switch: deactivate old NIC connection + bring bridge up. Bridge will
#    pull DHCP via its slave port. Linux's bridge driver forwards immediately,
#    so the gap is the DHCP renewal time only (sub-second on the home router).
nmcli connection down "$OLD_CONN" || true
nmcli connection up "$BRIDGE"

# 4. Disable autoconnect on the old connection so it doesn't fight us at boot.
nmcli connection modify "$OLD_CONN" connection.autoconnect no

echo
echo "=== Post-state ==="
ip -br addr show "$BRIDGE" || true
ip -br addr show "$NIC" || true
ip route | grep default || true
echo
echo "Active connections involving the new bridge:"
nmcli -t -f NAME,DEVICE,STATE connection show --active | grep -E "^${BRIDGE}"
