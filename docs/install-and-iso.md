# Install and ISO design

How a Bedrock node gets onto a physical box, and the constraints
that shape every choice in that process.

## Design principles

### 1. Limit the action set to the basic minimum

Bedrock is an appliance, not a configuration kit. Every prompt,
flag, and option the operator sees during install is justified by
something that cannot be reasonably inferred from the environment.
Defaults must be the right answer for >99% of installs; the rest
of the surface area is a downstream maintenance cost.

Concrete consequences:
- The happy path on the ISO asks zero questions on a clean
  single-disk box. Hostname is derived from MAC; network is DHCP;
  partitioning is fixed.
- A multi-disk box gets exactly one question (which disk), via
  Anaconda's normal disk-selection screen — already familiar to
  any operator who has installed Linux.
- A disk with existing partitions gets exactly one question
  (wipe? type 'yes'), preceded by a full readout of what's on it.
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

`bedrock-installer-<version>.iso`, ~1 GB.

Contains the Alma 10 netinstall kernel + Anaconda + a Bedrock
kickstart. Pulls Bedrock packages and Alma RPMs from upstream at
install time. First-boot hook fetches `install.sh` from the S3
prefix matching the ISO's version (an ISO named `…-v0.8.iso`
fetches from `/v0.8/`, never from `/dev/`).

When to recommend: anyone with internet at the install site.

### Path B — offline / airgap ISO (secondary download)

`bedrock-installer-<version>-offline.iso`, ~4-8 GB.

Same kickstart layout as Path A, plus every package and Bedrock
artifact baked into the ISO. No network needed during install.
Bedrock can be brought up, joined, and operated entirely offline.

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

Version is baked into the filename so the ISO is self-describing
once downloaded — and so the embedded kickstart can dispatch to
the correct S3 prefix without configuration.

| Filename | S3 prefix | Notes |
|---|---|---|
| `bedrock-installer-dev.iso` | `/dev/` | Rolling dev build. Daily-ish updates. |
| `bedrock-installer-dev-offline.iso` | `/dev/` | Same, full offline variant. |
| `bedrock-installer-v0.8.iso` | `/v0.8/` | Frozen release. Stable. |
| `bedrock-installer-v0.8-offline.iso` | `/v0.8/` | Same, full offline. |

**Rule**: a downloaded ISO must always install the version named
in its filename. The kickstart fetches from the matching S3 prefix
and never falls back to another. If `/v0.8/install.sh` is
unreachable, the install fails — it does not silently use `/dev/`.

## First-boot UX on the ISO

Happy path on a clean single-disk box: zero prompts. Anaconda
runs from kickstart, partitions per the Bedrock layout, installs
packages, reboots into a Bedrock-ready node.

- Hostname: `bedrock-<6-hex>` where the 6 hex chars are the last
  three octets of the primary wired NIC's MAC. Already the
  convention in the existing daemon.
- Network: DHCP on the primary NIC.
- Disk: the single disk, no question asked.
- End state: `bedrock` CLI installed, daemons not started. Login
  banner instructs operator to run `bedrock init` (new cluster)
  or `bedrock join <master>` (joiner).

### Multi-disk prompt

When 2+ disks are present, Anaconda's normal disk-selection
screen appears. No Bedrock-custom UI; the standard one already
shows model, size, and partition state, and operators recognize
it from every other Linux install they've done.

### Existing-partitions prompt

When the selected disk has any existing partitions, the install
halts before touching anything and shows a single screen:

```
Disk /dev/sda — 465 GB Samsung SSD 980

  /dev/sda1     600 MB   EFI System Partition (vfat)
  /dev/sda2       1 GB   /boot (xfs)
  /dev/sda3     464 GB   LVM physical volume — VG "almalinux"
                           ├─ root   70 GB   xfs
                           ├─ swap    4 GB   swap
                           └─ home  390 GB   xfs

This disk has existing partitions. Installing Bedrock will erase
all of them. There is no undo.

Type 'yes' to wipe and proceed, anything else to cancel:
```

One screen, full readout, one prompt. No colors louder than the
default terminal palette; no all-caps warnings.

## Partition layout the kickstart produces

For a single-disk Bedrock node:

| Partition | Size | FS | Purpose |
|---|---|---|---|
| `sdX1` | 600 MB | vfat | EFI System Partition |
| `sdX2` | 1 GB | xfs | `/boot` |
| `sdX3` | rest | LVM PV | VG `bedrock` |

Inside VG `bedrock`:

| LV | Size | FS / use |
|---|---|---|
| `root` | 30 GB | xfs `/` |
| `swap` | 4 GB | swap |
| `thinpool` | rest minus margin | LVM thin pool, holds Bedrock storage tiers |

Bedrock's `tier-critical`, `tier-bulk`, `tier-scratch`, and per-VM
disks all live as thin LVs inside `bedrock/thinpool`, created at
`bedrock init` time and on demand thereafter.

Why VG name `bedrock` (not `almalinux`): Bedrock owns this box.
The VG name should reflect that. Custom-install workflows on
default-Alma VGs (the script's `/home` reclaim path) keep the
existing `almalinux` VG name; only the ISO path uses `bedrock`.

## `install.sh` `/home` reclaim path (Path C recovery)

When `install.sh` runs on an already-installed default Alma box
and finds zero free PE in the VG, it offers to reclaim `/home`.
This is the **only** XFS-can't-shrink recovery path the script
supports — it is intentionally narrow.

### Why this exists

AlmaLinux 10's default Anaconda partitioning fills the disk:
~70 GB root LV, ~4 GB swap LV, all remaining space as `/home`
LV. All LVs are XFS. XFS cannot be shrunk online or offline.
Therefore the only way to make VG space available for Bedrock
without reinstalling is to remove the `/home` LV entirely.

This is acceptable when the box is freshly installed and `/home`
has no real user data. It is unacceptable otherwise. The script
errs aggressively on the side of refusing.

### Safety checks (all must pass)

The script refuses on any of:

- VG name is not the Alma default (custom layouts are not in
  scope for this recovery path).
- `/home` is not on its own LV.
- `/home` contains anything outside the allow-list:
  - Empty XDG dirs (`Desktop`, `Documents`, `Downloads`, `Music`,
    `Pictures`, `Public`, `Templates`, `Videos`) — OK.
  - Default shell dotfiles (`.bashrc`, `.bash_logout`,
    `.bash_profile`) — OK.
  - `.bash_history` only if smaller than 4 KB.
  - Empty `.cache`, `.config`, `.local` — OK.
  - `.ssh/` only if it contains no `authorized_keys`.
  - Anything else — refuse.
- More than one user directory under `/home/`.
- Bedrock-managed directories already exist anywhere on the box
  (means a prior install ran; different recovery path needed).

### When refused

Refuse messages tell the operator exactly what was found and what
to do. Tone is informational, not alarmist:

```
/home/tommy/ contains files outside the default install set:
  Downloads/firefox-latest.tar.gz (78 MB)
  project-notes.md (12 KB)
  .ssh/authorized_keys (2 entries)

Cannot safely reclaim /home automatically. Options:
  - Back up these files elsewhere, remove them from /home, and
    re-run this installer.
  - Reinstall from the Bedrock ISO. Recommended for new deploys.
```

### When proceeding

Single-screen readout of what will happen, one consent prompt:

```
VG 'almalinux' has no free space. Bedrock needs ~50 GB.

Plan: remove the /home LV (almalinux-home, 390 GB, xfs) to
reclaim VG space. /home is currently empty save for default
folders, so no data will be lost.

This is needed because AlmaLinux's default installer uses XFS
for /home, which cannot be shrunk. For new deploys the Bedrock
ISO does the right thing automatically — this path is the
recovery option for already-installed boxes.

Type 'almalinux-home' to confirm the LV to remove, or anything
else to cancel:
```

Operator types the LV name verbatim. Script unmounts, lvremoves,
removes the `/etc/fstab` entry, and continues with the rest of
the install.

### Code structure

This whole path is one well-marked section in `install.sh` with a
header comment block explaining it exists because of XFS+Alma
defaults, and pointing at this doc. It can be deleted as one
unit if upstream defaults ever change.

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
