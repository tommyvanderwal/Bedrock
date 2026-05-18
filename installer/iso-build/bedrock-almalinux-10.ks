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
rootpw --plaintext --allow-ssh bedrock
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
# directly readable by firmware/grub). biosboot is a 1MiB unformatted
# slot grub-bios uses to embed core.img on a GPT-labeled disk; ESP is
# the EFI partition used in UEFI boot. We allocate BOTH so the same
# install image works on legacy-BIOS and UEFI machines without an
# operator-time choice. Cost: ~501 MiB on the boot disk, trivial.
part biosboot  --fstype=biosboot --size=1
part /boot/efi --fstype=efi --size=500 --asprimary
part /boot     --fstype=xfs --size=1024 --asprimary

# The rest of the disk is one PV → one VG `bedrock`.
part pv.bedrock --size=1 --grow --asprimary

volgroup bedrock pv.bedrock

# Single thin pool taking MOST of the VG space, leaving headroom for
# thick LVs that live OUTSIDE the pool (DRBD external metadata
# volumes: tier-critical-meta etc.). Anaconda's `--grow` here would
# eat 100% of VG → ensure_meta_lv would fail with "VG has insufficient
# free space" at storage-promote time. Hold back 1 GiB.
# metadatasize=512 MB gives generous pool-meta headroom for the
# operations bedrock generates (per-VM thin LVs, snapshots, restores).
logvol none --thinpool --name=thinpool --vgname=bedrock --size=1 --grow --maxsize=130048 --metadatasize=512

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

# ── Post-install (stage A — outside chroot): stage the bedrock
#    payload from the ISO into the target system. This MUST run
#    with `--nochroot` because the install media is only mounted in
#    the installer environment at /run/install/repo, not inside the
#    chroot the default %post enters. The chrooted second %post
#    below then arms the first-boot service against the staged
#    payload.
%post --nochroot --interpreter=/bin/bash --erroronfail --log=/mnt/sysroot/var/log/bedrock-postinstall-stage-a.log

set -euo pipefail

# Anaconda mounts the ISO at /run/install/repo for %post. Target
# system root is at /mnt/sysroot. Copy /bedrock from the ISO to
# /var/lib/bedrock-install on the new system so first-boot can read
# the payload after the ISO is ejected.
SRC=/run/install/repo/bedrock
DST=/mnt/sysroot/var/lib/bedrock-install
[ -d "$SRC" ] || { echo "ERROR: $SRC missing on install media"; ls -la /run/install/repo/; exit 1; }
mkdir -p "$DST"
cp -r "$SRC"/. "$DST/"
chmod +x "$DST"/install.sh "$DST"/bedrock "$DST"/binaries/bedrock-rust 2>/dev/null || true
echo "[stage-a] payload copied: $(du -sh "$DST" | cut -f1)"
%end

# ── Post-install (stage B — inside chroot): arm the first-boot
#    bootstrap service + write the operator MOTD. Runs against the
#    payload staged in stage A.
%post --interpreter=/bin/bash --erroronfail --log=/var/log/bedrock-postinstall.log

set -euo pipefail

# Testbed convenience: drop the dev-box operator's public key into
# root's authorized_keys so test scripts can SSH key-based. Real
# deployments leave authorized_keys empty (passwd auth only until
# operator wires their own key).
mkdir -p /root/.ssh
chmod 700 /root/.ssh
cat > /root/.ssh/authorized_keys <<'PUBKEY_EOF'
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHXS8J+TpzUuO2WDCeSxV9baR5p7p14ZtaXWRvVlZgqp tommy@HP-G1a
PUBKEY_EOF
chmod 600 /root/.ssh/authorized_keys

# OpenSSH 9.8+ (AlmaLinux 10) ships with PerSourcePenalties enabled
# by default. A burst of failed auths from one source IP (e.g.
# paramiko probes from the master during cluster install) locks
# that source out for up to 10 min. Bedrock's intra-cluster SSH
# pool can't tolerate this — disable globally on every cluster
# node. See memory/lesson_persourcepenalty_flap.md.
mkdir -p /etc/ssh/sshd_config.d
cat > /etc/ssh/sshd_config.d/99-bedrock-no-persource.conf <<'EOF'
PerSourcePenalties no
# Bedrock's intra-cluster tooling fans out many short-lived SSH
# sessions from the same source (test harness, mgmt master driving
# storage promote, paramiko-based status probes). The OpenSSH
# default of MaxStartups 10:30:100 randomly rejects connections
# at 10 concurrent unauthenticated sockets, which manifests as
# "kex_exchange_identification: read: Connection reset by peer"
# in the test logs. Bump it well past the worst expected burst.
MaxStartups 100:30:200
EOF
chmod 644 /etc/ssh/sshd_config.d/99-bedrock-no-persource.conf

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
