# Install and ISO design

How a Bedrock node gets onto a physical box, and the constraints
that shape every choice in that process.

## Design principles

### 1. Limit the action set to the basic minimum

Bedrock is an appliance, not a configuration kit. Every prompt,
flag, and option the operator sees during install is justified by
something that cannot be reasonably inferred from the environment.
Defaults are the right answer for >99% of installs; the rest of
the surface area is a downstream maintenance cost.

Concrete consequences:
- The happy path on the ISO asks zero questions on a clean
  single-disk box. Hostname derives from MAC; network is DHCP;
  partitioning is fixed.
- A multi-disk box gets exactly one question (which disk), at the
  install console: a numbered list of candidate disks (size +
  model), smallest first — Bedrock convention is OS on the
  smallest disk, data tiers on the larger ones.
- A disk with existing partitions gets exactly one question
  (wipe? type 'yes'), preceded by a full `lsblk` readout.
- The install script (running on already-installed Alma) asks
  exactly one question if it needs to reclaim `/home` — preceded
  by a readout of what it found and why this is needed.
- Nothing else is configurable at install time. Cluster identity,
  witnesses, network roles, etc. are decided by `bedrock init` /
  `bedrock join` *after* the OS is installed.

### 2. The install never decides cluster role

The ISO and the install script bring a node to "Bedrock installed,
services prepared, awaiting operator decision." They never run
`bedrock init` automatically — the operator must explicitly choose
between *create new cluster* and *join existing cluster*. Joiners
greatly outnumber first-node installs at any operating MSP, and
the ISO has no way to know which case the operator is in.

### 3. The operator is always allowed to fully control Bedrock

Two consequences flow from this:

- **The offline / airgap ISO is a primary deliverable, not an
  afterthought.** Every release ships both a net-install ISO and
  a full offline ISO. Airgapped sites, sensitive customer
  environments, and MSPs shipping hardware to remote locations
  must be able to install Bedrock with no upstream dependency
  beyond the ISO itself.
- **The install script's recovery paths are documented and
  predictable.** When the operator already has a default Alma
  install and runs `install.sh`, the `/home` reclaim path exists
  and works, but only when the box is in a truly pristine state.
  Operator data is never silently destroyed.

## Install paths

Three supported paths, in order of preference for new deploys:

### Path A — net-install ISO (default visible download)

`bedrock-installer-<version>.iso`, ~1 GB. Built from AlmaLinux's
`boot.iso`.

Contains the Alma 10 netinstall kernel + Anaconda + the Bedrock
kickstart. Pulls Alma RPMs from Alma's mirrors during install
(`inst.repo=…` on the kernel cmdline). The first-boot service has
`BEDROCK_REPO=https://bedrock.fsn1.your-objectstorage.com/<version>`
baked in at build time (`build-iso.sh --version <version>`), then
`curl ${BEDROCK_REPO}/install.sh` and runs it. The version in the
S3 URL is fixed when the ISO is built, so a given ISO always
pulls from its own prefix — `/dev/` for the dev ISO, `/v0.8/` for
the v0.8 ISO.

When to recommend: anyone with internet at the install site.

### Path B — offline / airgap ISO (secondary download)

`bedrock-installer-<version>-offline.iso`, ~4-8 GB. Built from
AlmaLinux's `dvd.iso`.

Same kickstart as Path A, plus the full Bedrock payload (RPMs,
wheels, binaries, VM images) staged onto the ISO under `/bedrock`.
The kickstart copies it to `/var/lib/bedrock-install`, and the
first-boot service runs `install.sh` from there with
`BEDROCK_REPO=file:///var/lib/bedrock-install`. No network needed
during install or first boot. Bedrock can be brought up, joined,
and operated entirely offline.

When to recommend: airgapped environments, MSPs shipping
preconfigured hardware to remote sites, anyone who values full
operator control over the install supply chain.

### Path C — install script on running AlmaLinux 10

```
curl -fsSL https://bedrock.fsn1.your-objectstorage.com/<prefix>/install.sh | sudo bash
```

For operators who already have an Alma install and don't want to
reimage. This path includes the `/home` reclaim recovery and is
documented as the recovery option, not the default.

When to recommend: existing Alma installs only.

## ISO naming convention

Both variants of a version come from one kickstart
(`installer/iso-build/bedrock-almalinux-10.ks`); `build-iso.sh`
sed-substitutes per-variant tokens (install method, `BEDROCK_REPO`,
first-boot ExecStart). Version is baked into the filename and into
the net ISO's `BEDROCK_REPO`, so an ISO is self-describing once
downloaded and pulls from its own S3 prefix without configuration.

| Filename | Source ISO | net `BEDROCK_REPO` | Notes |
|---|---|---|---|
| `bedrock-installer-dev.iso` | Alma boot.iso | `…/dev` | Rolling dev build. |
| `bedrock-installer-dev-offline.iso` | Alma dvd.iso | `file://…` | Full offline variant. |
| `bedrock-installer-v0.8.iso` | Alma boot.iso | `…/v0.8` | Frozen release. |
| `bedrock-installer-v0.8-offline.iso` | Alma dvd.iso | `file://…` | Full offline. |

**Rule**: a net ISO installs the version named in its filename.
Its `BEDROCK_REPO` is fixed to the matching S3 prefix at build
time; it never falls back to another. If `/v0.8/install.sh` is
unreachable, the install fails — it does not silently use `/dev/`.

## First-boot UX on the ISO

Happy path on a clean single-disk box: zero prompts. Anaconda
runs from kickstart, partitions per the Bedrock layout, installs
the base OS, reboots. At `multi-user.target` two things happen in
parallel: the login prompt comes up, and a one-shot
`bedrock-firstboot.service` runs `install.sh` (~3-7 min on a net
install: pip wheels, payload stage-out, `bedrock bootstrap`
dnf-installing libvirt/qemu/drbd-kmod).

- Hostname: `install.sh` rewrites the default
  `localhost.localdomain` to `bedrock-<6-hex>`, the last three
  octets of the primary NIC's MAC.
- Network: DHCP on the primary NIC (`network --bootproto=dhcp`).
- Disk: the single disk, no question asked.
- The MOTD reads "installation in progress" while
  `bedrock-firstboot` runs; `install.sh` swaps it to the "ready"
  banner only on a clean `bedrock bootstrap`, so a stale "run
  bedrock init" message never shows mid-install. On bootstrap
  failure the "in progress" MOTD stays and points at
  `journalctl -u bedrock-firstboot`.
- End state: `bedrock` CLI + daemons installed, not started. The
  "ready" banner instructs `bedrock init` (new cluster) or
  `bedrock join` (joiner — mDNS-discovers the cluster on the LAN;
  pass a node IP positionally if discovery can't reach it).

### Multi-disk prompt

When 2+ disks are present, the kickstart's `%pre` shows a numbered
list at the install console — index, `/dev/name`, size, model,
smallest first — and reads one number. No Anaconda GUI; this runs
on `/dev/tty1` before partitioning.

### Existing-partitions prompt

When the selected disk has existing partitions, `%pre` halts
before touching anything, prints the disk's `lsblk` tree, and
reads one confirmation:

```
Disk /dev/sda has existing partitions:

    sda     465G
    ├─sda1  600M  /boot/efi
    ├─sda2    1G  /boot
    └─sda3  464G  almalinux  (root/swap/home)

Installing Bedrock will erase all of them. There is no undo.

Type 'yes' to wipe and proceed, anything else to cancel:
```

One screen, full readout, one prompt. No all-caps warnings.

## Partition layout the kickstart produces

For a single-disk Bedrock node (`clearpart --all`):

| Partition | Size | FS | Purpose |
|---|---|---|---|
| biosboot | 1 MiB | — | GPT BIOS-boot slot (legacy-BIOS grub core.img) |
| `/boot/efi` | 500 MB | efi | EFI System Partition (UEFI boot) |
| `/boot` | 1024 MB | xfs | `/boot` |
| `pv.bedrock` | rest | LVM PV | VG `bedrock` |

Both biosboot and ESP are allocated so one image boots on
legacy-BIOS and UEFI without an operator choice.

Inside VG `bedrock`:

| LV | Size | FS / use |
|---|---|---|
| `thinpool` | grows, ≤130 GiB, holds back 1 GiB; 512 MB pool-meta | LVM thin pool — all dynamic Bedrock storage |
| `root` | 16 GB thin LV in `thinpool` | xfs `/` (grow online with `xfs_growfs`) |

No swap LV. Swap-on-thin can panic the kernel when the pool fills,
and a hypervisor running VMs should not swap. Opt in post-install
with `bedrock storage swap-set <gb>`. The 1 GiB held back outside
the pool is room for thick LVs (the DRBD external-metadata volumes).

Bedrock's `cluster` singleton (`bedrock-data-cluster` /
`bedrock-meta-cluster`), the SeaweedFS volume store
(`bedrock-weed-volume`, a local thin LV), and per-VM disks
(`bedrock-data-vm-<name>-disk0` / `bedrock-meta-vm-<name>-disk0`) are
thin LVs in `bedrock/thinpool`, created at `bedrock init` and on
demand. One LVM thin pool per node; each DRBD resource gets a data
LV plus an external-metadata LV. The `cluster` singleton holds the
arbiter rqlite + SeaweedFS filer data and replicates `min(3, N)`-way
across the lowest-octet nodes; per-VM disks are local (cattle),
2-way (pet), or 3-way (vipet).

Why VG name `bedrock`: Bedrock owns this box, so the VG reflects
that. The `/home` reclaim path (Path C) keeps the existing
`almalinux` VG name it finds; only the ISO path creates `bedrock`.

## `install.sh` `/home` reclaim path (Path C recovery)

When `install.sh` runs on an already-installed default Alma box
and finds zero free PE in the VG, it offers to reclaim `/home`.
This is the **only** XFS-can't-shrink recovery path the script
supports — intentionally narrow.

### Why this exists

AlmaLinux 10's default Anaconda partitioning fills the disk:
~70 GB root LV, ~4 GB swap LV, all remaining space as `/home`
LV. All LVs are XFS, which cannot be shrunk online or offline.
Removing the `/home` LV entirely is the only way to free VG space
for Bedrock without reinstalling.

The whole reclaim block is one self-contained, header-commented
section in `install.sh`; it can be deleted as a unit if upstream
Alma defaults ever ship free PE.

### Safety checks (all must pass)

`home_reclaim_check()` runs first thing in `install.sh`. It does
nothing (returns 0) unless every gate matches; it refuses (exits)
on a partial match that looks like operator data:

- VG must be `almalinux` or `vg_almalinux`. Any other name → skip
  silently (custom layout, not in scope). If a `bedrock` VG with
  free PE already exists → skip (already laid out).
- VG must have zero free extents. Any free PE → no reclaim needed.
- `/home` must be its own LV mounted at `/home`. If the VG is full
  but `/home` is not a separate LV → fatal, recommend ISO reinstall.
- At most one user directory under `/home/` (excluding `lost+found`).
- That user dir's top-level entries must all be in the allow-list:
  - XDG dirs (`Desktop`, `Documents`, `Downloads`, `Music`,
    `Pictures`, `Public`, `Templates`, `Videos`) — only if empty.
  - Shell dotfiles `.bashrc`, `.bash_logout`, `.bash_profile`,
    `.bash_logout.rpmnew` — always OK.
  - `.bash_history` — only if < 4 KB.
  - `.cache`, `.config`, `.local`, `.dbus`, `.mozilla`, `.gnupg` —
    only if empty.
  - `.ssh/` — OK unless it holds a non-empty `authorized_keys`.
  - Stragglers (`.viminfo`, `.lesshst`, `.xauthority`,
    `.ICEauthority`, `.face`, `lost+found`) — OK.
  - Anything else → refuse.

### When refused

Refuse messages tell the operator exactly what was found and what
to do. Tone is informational, not alarmist:

```
/home/tommy contains files outside the default install set:
  /home/tommy/Downloads (not empty)
  /home/tommy/project-notes.md
  /home/tommy/.ssh/authorized_keys (real SSH keys)

Cannot safely reclaim /home automatically. Options:
  - Back up these files elsewhere, remove them from /home, and
    re-run this installer.
  - Reinstall from the Bedrock ISO (recommended for new deploys).
```

### When proceeding

Single-screen readout of what will happen, one consent prompt:

```
VG 'almalinux' has no free space. Bedrock needs roughly 50 GB.

Plan: remove LV 'almalinux/home' (390g) to reclaim VG space.
/home contains only the default install footprint, so no data
is lost.

This step exists because AlmaLinux's default installer puts /home
on an XFS volume, which can't be shrunk. For new deploys the
Bedrock ISO partitions the disk correctly from the start — this
path is the recovery option for already-installed boxes only.

Type the LV name to confirm, or anything else to cancel.
  expected: almalinux/home
  > 
```

The operator types the `<vg>/home` name verbatim (e.g.
`almalinux/home`). The script `umount`s `/home`, `lvremove`s the
LV, strips its `/etc/fstab` line (backing up to
`/etc/fstab.bedrock-bak`), leaves `/home` as an empty mount point,
and continues with the rest of the install. A piped (non-tty)
invocation refuses here and tells the operator to re-run
interactively or use the ISO.

## Out of scope

Things deliberately not built for v1.0:

- Hetzner `installimage` config snippets, cloud-init userdata,
  and other provider-specific install paths. The ISO and the
  install script cover 99% of the target audience; provider
  integrations are corner cases for later.
- Kickstart URLs as a public deliverable (operators pasting
  `inst.ks=https://…` at GRUB). The ISO embeds the kickstart;
  exposing it separately adds a path with no clear constituency.
- Partition resize after default Alma install (beyond the
  `/home` reclaim). Other layouts are too varied to support
  cleanly.
- Disk-encryption setup. Customers who need it can layer LUKS
  on the install disk via custom kickstart; Bedrock's own data
  tiers are not encrypted at rest in v1.0.
