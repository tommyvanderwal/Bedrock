# bedrock_d/vm/lvm.py

LV-pair sizing and lifecycle helpers for per-resource DRBD storage. Every DRBD
resource (a per-VM `vm-<name>-disk<N>` or the `cluster` singleton) is backed by
two thin LVs in the node's single `thinpool`: a data LV and an external-metadata
LV. This module owns the canonical LV naming, the meta-LV sizing math, and the
host-aware shell-out that creates / removes / grows the pair. The storage sagas
call these helpers from their step bodies; keeping the math here makes it
unit-testable and keeps the step bodies short.

## Functions / Classes

### `LvPair`
Frozen dataclass holding the two LV names backing one DRBD resource.
- Fields: `data_lv` (e.g. `bedrock-data-vm-foo-disk0`), `meta_lv`
  (e.g. `bedrock-meta-vm-foo-disk0`).

### `lv_names_for(resource) -> LvPair`
Canonical LV names for a DRBD resource — the single source of truth, so no
string-concat scatter.
- **In:** `resource` — resource name (`vm-<name>-disk<N>` or `cluster`).
- **Out:** `LvPair(data_lv=f"bedrock-data-{resource}", meta_lv=f"bedrock-meta-{resource}")`.
  Raises `ValueError` if `resource` is empty. No side effects.

### `meta_size_mb_for(data_gb, max_peers=7) -> int`
Pure function: MiB of meta LV to provision for `data_gb` of data and `max_peers`
future peers.
- **In:** `data_gb` — data size in GiB; `max_peers` — DRBD peer count to reserve
  bitmap for (1..15).
- **Out:** integer MiB = `header(4) + AL(32) + ceil(max_peers*data_gb*32 KiB)`,
  rounded up to the next 4 MiB. Raises `ValueError` if `data_gb < 1` or
  `max_peers` outside 1..15. No side effects.

### `lv_path(lv, vg=VG_NAME) -> str`
Device-mapper path for a thin LV.
- **In:** `lv` — LV name; `vg` — volume group (defaults to the resolved VG).
- **Out:** `f"/dev/{vg}/{lv}"`. No side effects.

### `lv_exists(host, lv, vg=VG_NAME) -> bool`
Existence check used as the idempotency seam for the create / remove sagas.
- **In:** `host` — target node (empty/local → run locally, else SSH); `lv` — LV
  name; `vg` — volume group.
- **Out:** `True` if `lvs --noheadings -o lv_name {vg}/{lv}` returns rc 0. Runs
  one `lvs` subprocess (10 s timeout, non-checking).

### `lvcreate_pair(host, resource, data_gb, *, max_peers=7, vg=VG_NAME, thinpool=THINPOOL) -> LvPair`
Create the data + meta LV pair for a resource. Idempotent.
- **In:** `host` — target node; `resource` — DRBD resource name; `data_gb` —
  data LV virtual size in GiB; `max_peers` — peers to size meta for; `vg`,
  `thinpool` — volume group and thinpool names.
- **Out:** the `LvPair`. Side effects: runs `lvcreate -y -V {data_gb}G --thin`
  for the data LV and `lvcreate -y -V {meta_mb}M --thin` for the meta LV, each
  skipped if that LV already exists. Both are checked subprocess calls.

### `lvremove_pair(host, resource, *, vg=VG_NAME) -> None`
Remove both LVs. Idempotent — a missing LV is success.
- **In:** `host` — target node; `resource` — DRBD resource name; `vg` — volume
  group.
- **Out:** `None`. Side effects: for each existing LV runs `lvremove -fy {vg}/{lv}`
  (non-checking).

### `lvextend_data(host, resource, new_gb, *, vg=VG_NAME) -> None`
Online-grow the data LV. Idempotent (LVM no-ops if target <= current).
- **In:** `host` — target node; `resource` — DRBD resource name; `new_gb` —
  new data size in GiB; `vg` — volume group.
- **Out:** `None`. Side effects: runs
  `lvextend -L {new_gb}G --no-resize-fs {vg}/{data_lv} 2>&1 || true`
  (non-checking).

### `lvextend_meta(host, resource, data_gb, *, max_peers=7, vg=VG_NAME) -> None`
Grow the meta LV to match `meta_size_mb_for(data_gb)`. Idempotent.
- **In:** `host` — target node; `resource` — DRBD resource name; `data_gb` —
  the data size to size meta against; `max_peers`; `vg`.
- **Out:** `None`. Side effects: runs `lvextend -L {new_mb}M {vg}/{meta_lv} 2>&1 || true`
  (non-checking).

### Module-level names
- `VG_NAME` — the resolved volume-group name, computed once at import via
  `_resolved_vg()`.
- `THINPOOL = "thinpool"`, `DEFAULT_MAX_PEERS = 7`.
- DRBD layout constants: `DRBD_HEADER_MB = 4`, `DRBD_AL_MB = 32`,
  `DRBD_BITMAP_KIB_PER_GB_PER_PEER = 32`.
- `data_lv_for(r)` / `meta_lv_for(r)` — aliases returning `lv_names_for(r).data_lv`
  / `.meta_lv`; the saga steps call these.

### Private helpers
- `_resolved_vg() -> str` — imports `lib.tier_storage` (from
  `/usr/local/lib/bedrock`) and returns `detect_vg()` (reads
  `/etc/bedrock/storage.json`); falls back to `"bedrock"` on any exception.
- `_run_on(host, cmd, *, check=True, timeout=60) -> (rc, stdout, stderr)` —
  runs `cmd` locally if `host` is empty / `localhost` / the local hostname,
  else over SSH to `root@{host}`; raises `CalledProcessError` when `check` and
  rc != 0.
- `_local_hostname() -> str` — `socket.gethostname()`, `""` on `OSError`.

## How it works

VG resolution happens once, at import:

```
import → _resolved_vg()
           ├─ tier_storage.detect_vg()  → reads /etc/bedrock/storage.json  (on a node)
           └─ except → "bedrock"                                            (off-node / tests)
         → VG_NAME
```

The resolved VG is never hardcoded: a fresh install makes `bedrock-vg`, while an
install over existing AlmaLinux adopts whatever VG the OS installer created
(often `almalinux`). Off-node (e.g. tests) the fallback `bedrock` keeps the
`/dev/bedrock/...` LV-name contract intact.

Each DRBD resource maps to exactly two thin LVs in one thinpool:

```
  thinpool (VG_NAME)
  ├─ bedrock-data-<resource>   data_gb (virtual) — the disk
  └─ bedrock-meta-<resource>   meta_mb (virtual) — DRBD9 external metadata
```

Meta sizing (`meta_size_mb_for`) reserves bitmap room for `max_peers` up front,
so adding peers later never needs downtime:

```
  meta_mb = 4 MiB header
          + 32 MiB activity log
          + ceil(max_peers × data_gb × 32 KiB / 1024) MiB bitmap
  then round up to the next 4 MiB (extent alignment + DRBD format headroom)
```

The bitmap term is `1 bit per 4 KiB of data, per peer` (= 32 KiB of bitmap per
GiB of data per peer). Both LVs are thin, so their large virtual sizes cost
almost nothing until peers actually dirty bitmap bits.

Every lifecycle op routes through `_run_on(host, cmd)`. When `host` is local the
command runs directly; for a remote `host` the entire `cmd` is wrapped with
`shlex.quote` before being handed to `ssh root@{host}`:

```
  _run_on(host, cmd):
    local host  → subprocess.run(cmd, shell=True)
    remote host → subprocess.run("ssh ... root@{host} " + shlex.quote(cmd), shell=True)
                  (shlex.quote survives the local shell=True layer; the remote
                   shell still interprets pipes/redirects/heredocs inside cmd,
                   and cmd's own single quotes — e.g. libvirt XML type='kvm' —
                   pass through intact)
```

The remote SSH form uses `StrictHostKeyChecking=no`, `BatchMode=yes`, and
`ConnectTimeout=5`.

Idempotency is uniform across the lifecycle helpers, which is what lets the
storage sagas re-run their steps safely on resume:

- `lvcreate_pair` checks `lv_exists` per LV and skips creation when present;
- `lvremove_pair` only removes an LV that exists (missing == success);
- `lvextend_data` / `lvextend_meta` lean on LVM no-op'ing when the target size
  is at or below current, and both swallow failures via `2>&1 || true` +
  `check=False`.

## Why

The sizing and naming live as pure, importable helpers (not inline in saga
steps) so the DRBD-metadata math is unit-testable on its own and the saga step
bodies stay short. Pre-reserving bitmap space for `max_peers` at create-md time
trades a little thin virtual size for the ability to add DRBD peers later
without taking the resource down.
