#version=RHEL10
# Bedrock unattended installation kickstart for AlmaLinux 10.1
#
# Layout: single boot disk → small EFI + /boot + LVM PV → bedrock VG →
# thinpool → root (thin LV) inside thinpool. Everything dynamic lives
# in the one thin pool, matching docs/01-storage-stack.md.
#
# After install, a one-shot systemd unit (bedrock-firstboot.service)
# runs install.sh from the bundled payload, completing bedrock
# bootstrap before handing off to the operator. No network required
# during install — every package and every bedrock asset comes from
# the ISO itself.

# ── Install method: pull packages from the bundled DVD repo on the ISO
cdrom

# ── Localization (override in your kickstart fork if non-US/UTC)
lang en_US.UTF-8
keyboard us
timezone UTC --utc

# ── Authentication
# Default root password is `bedrock`. **Change at first login** —
# operator policy. We could randomise via %post but visibility
# during initial bring-up matters for testbed + lab.
rootpw --plaintext bedrock
selinux --permissive
firewall --disabled

# ── Networking — DHCP on the first NIC anaconda finds; bedrock's
#    network helper rewrites this to a static br0 + bedrock-drbd
#    setup at first `bedrock init` / `bedrock join`. We don't pin
#    a device name here because real-hardware NIC names vary
#    (enp1s0, eno1, eth0…) and anaconda's `link` autodetect
#    isn't supported in 10.1's anaconda.
network --bootproto=dhcp --activate

# ── Reboot when done
reboot --eject

# ── Disk: wipe everything, build the bedrock layout
zerombr
clearpart --all --initlabel

# Boot partitions — outside the LVM (bootloaders + kernel must be
# directly readable by firmware/grub).
part /boot/efi --fstype=efi --size=500 --asprimary
part /boot     --fstype=xfs --size=1024 --asprimary

# The rest of the disk is one PV → one VG `bedrock`.
part pv.bedrock --size=1 --grow --asprimary

volgroup bedrock pv.bedrock

# Single thin pool taking ~all VG space. metadatasize=512 MB gives
# generous headroom for the operations bedrock generates (per-VM
# thin LVs, snapshots, restores).
logvol none --thinpool --name=thinpool --vgname=bedrock --size=1 --grow --metadatasize=512

# Root as a thin LV inside the pool. 16 GB virtual covers any
# reasonable AlmaLinux install footprint; xfs supports online
# `xfs_growfs` so the operator can grow later if needed.
# (Single line — anaconda 10.1 doesn't reliably parse the `\`
# continuation in kickstart files.)
logvol / --name=root --vgname=bedrock --thin --poolname=thinpool --size=16384 --fstype=xfs

# No swap by default. Swap-on-thin can panic the kernel when the pool
# fills, and a hypervisor with VMs has no business swapping. Operator
# can opt in with `bedrock storage swap-set <gb>` post-install.

bootloader --location=mbr --append="console=tty0 console=ttyS0,115200n8"

# ── Minimal package set. The bedrock payload's bootstrap step layers
#    everything else (kvm, libvirt, drbd, mgmt deps) on top.
#    --excludedocs only (no --excludeenvs in 10.1 anaconda).
%packages --excludedocs
@core
xfsprogs
chrony
curl
tar
%end

# ── Post-install: stage the bedrock payload from the ISO into the
#    new system + arm a first-boot service that runs install.sh.
#    --interpreter=/bin/bash keeps the script readable; --erroronfail
#    aborts the whole install if the payload is missing (better than
#    a silently broken first boot).
%post --interpreter=/bin/bash --erroronfail --log=/var/log/bedrock-postinstall.log

set -euo pipefail

# Anaconda mounts the ISO's payload at /run/install/repo for the
# duration of %post. We copy bedrock/ into the target system so
# first-boot can read it from disk after the ISO is ejected.
SRC=/run/install/repo/bedrock
DST=/var/lib/bedrock-install
mkdir -p "$DST"
cp -r "$SRC"/. "$DST/"
chmod +x "$DST"/install.sh "$DST"/bedrock "$DST"/bedrock-rust 2>/dev/null || true

# First-boot one-shot service: runs install.sh against the local
# payload. Self-disables after success so it never re-runs.
cat > /etc/systemd/system/bedrock-firstboot.service <<'EOF'
[Unit]
Description=Bedrock first-boot bootstrap (offline install)
After=network-online.target local-fs.target
Wants=network-online.target
ConditionPathExists=/var/lib/bedrock-install/install.sh
ConditionPathExists=!/var/lib/bedrock-install/.bootstrap-done

[Service]
Type=oneshot
RemainAfterExit=yes
StandardOutput=journal+console
Environment=BEDROCK_REPO=file:///var/lib/bedrock-install
ExecStart=/bin/bash /var/lib/bedrock-install/install.sh
ExecStartPost=/bin/bash -c 'touch /var/lib/bedrock-install/.bootstrap-done; \
  systemctl disable bedrock-firstboot.service'

[Install]
WantedBy=multi-user.target
EOF

systemctl enable bedrock-firstboot.service

# Operator-facing first-login banner: explain what just happened +
# the next step (`bedrock init` or `bedrock join`).
cat > /etc/motd <<'EOF'

  ╔══════════════════════════════════════════════════════════════════╗
  ║                  Bedrock node — fresh install                    ║
  ╠══════════════════════════════════════════════════════════════════╣
  ║                                                                  ║
  ║  AlmaLinux 10 + bedrock storage layout installed offline from    ║
  ║  the install ISO. First-boot bootstrap finished at:              ║
  ║      /var/lib/bedrock-install/.bootstrap-done                    ║
  ║                                                                  ║
  ║  Next step:                                                      ║
  ║      bedrock init           — start a new cluster                ║
  ║      bedrock join HOST      — join an existing one               ║
  ║      bedrock storage status — show the LVM thin-pool layout     ║
  ║                                                                  ║
  ║  Default root password is `bedrock`. Change it now.              ║
  ║                                                                  ║
  ╚══════════════════════════════════════════════════════════════════╝

EOF

echo "[bedrock-postinstall] payload staged; firstboot service armed"
%end
