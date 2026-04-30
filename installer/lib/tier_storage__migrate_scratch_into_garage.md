# `migrate_scratch_into_garage()` — operational spec

Companion document for the `migrate_scratch_into_garage()` function in
[`tier_storage.py`](tier_storage.py). The reverse-direction
counterpart is documented inline in [`tier_storage.md`](tier_storage.md)
under "migrate_scratch_out_of_garage".

For the journey of why this function exists at all (it was missing
from the original design), see lessons-log entry
[L15](../../docs/lessons-log.md).

---

## Top-of-section summary

When a Bedrock cluster grows from N=1 → N=2, scratch goes from a
local thin LV (mounted at `/var/lib/bedrock/local/scratch`) to a
Garage S3 bucket (FUSE-mounted at `/var/lib/bedrock/mounts/scratch-s3fs`).
This function copies the contents of the local LV into the Garage
bucket BEFORE the symlink swap, so the operator's existing scratch
data isn't lost mid-promotion.

The original design treated scratch as "RAID0, lose-it-and-redownload"
and the promote helper unmounted the local LV without copying. That's
correct semantics for *node loss* — but a default operator-driven
N=1 → N=2 promote is not a node loss; it's a planned topology
change, and data loss there is unacceptable.

This is the symmetric counterpart of `migrate_scratch_out_of_garage()`
which runs at N=2+ → N=1 collapse. Both functions use the same
playbook (rsync via the FUSE/local mount, MD5 verify, atomic symlink
swap, lsof drain, umount source, drop fstab line) — only the
direction reversed.

## Pre-conditions

- The local scratch LV is mounted at `/var/lib/bedrock/local/scratch`
  (set up by `setup_n1()` at install time).
- The s3fs mount is already up at `/var/lib/bedrock/mounts/scratch-s3fs`
  (caller is `s3fs_mount_scratch()`, which mounts s3fs first then
  calls this function).
- The Garage cluster is healthy and the `scratch` bucket exists.
- There is enough free space in the Garage bucket to hold the local
  scratch dataset. (Garage is RF=1 cluster-wide, so 1× the data
  size; the cluster's total capacity must accommodate it.)

## Post-conditions

- All files from `/var/lib/bedrock/local/scratch/` are now objects
  in the Garage `scratch` bucket, accessible via the s3fs mount.
- (Optional) MD5 manifests of source and destination match, byte-
  for-byte verified.
- `/bedrock/scratch` symlink → s3fs mount (atomic swap).
- Local scratch LV is unmounted.
- The local-LV line is removed from `/etc/fstab`.
- The local LV itself is *kept* (`lvremove` not run) so the operator
  can manually `lvremove` after a confidence period if disk space
  is needed.

## Visual flow

```
                   start (s3fs already mounted, local LV has data)
                                         │
                                         ▼
       ┌────────────────────────────────────────────────────────────┐
       │  0. Pre-flight                                              │
       │     - mountpoint -q LOCAL  → must be true                   │
       │     - mountpoint -q S3FS   → must be true                   │
       │     (raise if not — caller must mount s3fs first)           │
       └────────────────────────────────────────────────────────────┘
                                         │
                                         ▼
       ┌────────────────────────────────────────────────────────────┐
       │  1. rsync -aH --inplace LOCAL/ → S3FS/                     │
       │     ── note: NOT -X. s3fs reports SELinux/xattr contexts   │
       │        differently from XFS, breaks rsync mid-copy with    │
       │        "lremovexattr: Permission denied". (See L22.)       │
       │     ── --inplace: write through to dest file directly      │
       │        (no temp + rename); destination has no concurrent   │
       │        readers in this scenario.                            │
       │     ── -aH preserves perms/times/hardlinks but not xattrs  │
       └────────────────────────────────────────────────────────────┘
                                         │
                                         ▼
       ┌────────────────────────────────────────────────────────────┐
       │  2. (optional, verify_md5=True default) MD5 manifest diff  │
       │     - find both sides, sort -z, xargs md5sum               │
       │     - compare                                              │
       │     - on mismatch: dump both manifests to /tmp/scratch-    │
       │       into-md5-{src,dst}.log and raise                     │
       └────────────────────────────────────────────────────────────┘
                                         │
                                         ▼
       ┌────────────────────────────────────────────────────────────┐
       │  3. atomic_symlink /bedrock/scratch → s3fs mount           │
       │     ── COMMIT POINT. New opens of /bedrock/scratch follow  │
       │        the new target (Garage). Existing fds opened via    │
       │        the OLD local-LV path keep working until they       │
       │        close (POSIX rename(2) inode-pinning semantics).    │
       └────────────────────────────────────────────────────────────┘
                                         │
                                         ▼
       ┌────────────────────────────────────────────────────────────┐
       │  4. WAIT: poll lsof +D LOCAL until count ≤ 1 (header only) │
       │     bounded by 60s timeout                                  │
       └────────────────────────────────────────────────────────────┘
                                         │
                                         ▼
       ┌────────────────────────────────────────────────────────────┐
       │  5. umount LOCAL (lazy fallback: -l if normal umount fails)│
       │     drop the /etc/fstab line for LOCAL                     │
       └────────────────────────────────────────────────────────────┘
                                         │
                                         ▼
                                       end
```

## Step-by-step exact commands (for reviewer verification)

The function takes no positional arguments. With defaults
(`verify_md5=True`):

```bash
# 0. Pre-flight (raise if either is false):
mountpoint -q /var/lib/bedrock/local/scratch
mountpoint -q /var/lib/bedrock/mounts/scratch-s3fs

# 1. Bulk copy:
rsync -aH --inplace \
  /var/lib/bedrock/local/scratch/ \
  /var/lib/bedrock/mounts/scratch-s3fs/

# 2. (optional) MD5 verify:
cd /var/lib/bedrock/local/scratch && find . -type f -print0 | sort -z | xargs -0 md5sum > /tmp/src.md5
cd /var/lib/bedrock/mounts/scratch-s3fs && find . -type f -print0 | sort -z | xargs -0 md5sum > /tmp/dst.md5
diff /tmp/src.md5 /tmp/dst.md5 || exit 1

# 3. Atomic symlink swap (Python's os.symlink + os.replace under
#    atomic_symlink() helper):
ln -sfn /var/lib/bedrock/mounts/scratch-s3fs /bedrock/scratch.tmp
mv -T /bedrock/scratch.tmp /bedrock/scratch

# 4. Wait for fd drain (60s timeout):
while [ $(lsof +D /var/lib/bedrock/local/scratch 2>/dev/null | wc -l) -gt 1 ]; do
  sleep 2
done

# 5. Unmount + fstab cleanup:
umount /var/lib/bedrock/local/scratch \
  || umount -l /var/lib/bedrock/local/scratch
sed -i '\|/var/lib/bedrock/local/scratch|d' /etc/fstab
```

## Crash-safety analysis

| Crash point | Persistent state | Effect on next boot |
|---|---|---|
| Before step 1 | local LV mounted with data; fstab has both s3fs + local-LV lines; symlink → local-LV | Boot → both mounts come up; symlink → local-LV; cluster keeps running with scratch on local. Re-run picks up where it left off (rsync sees most data already there if a prior pass got partway). |
| Mid-rsync (step 1) | partial data in s3fs bucket; rest still local | Boot → both mounts; symlink still → local; rsync re-run is idempotent (skips files with matching size+mtime). |
| Between rsync and symlink swap (step 2-3) | data fully in both places; symlink still → local | Boot → still local-mode; re-running the function: rsync no-op, MD5 verify, swap symlink, finish. |
| Between swap and umount (step 3-5) | symlink → s3fs; local LV still mounted (orphaned); fstab has both lines | Boot → both lines mount; symlink → s3fs (correct); local LV mounts to its old path but nothing reads it. Re-run completes the umount + fstab cleanup. |
| After step 5 | symlink → s3fs; local unmounted; fstab has only s3fs line | Boot → end state directly. |

In every case, persistent state on disk encodes the operator's
intent and re-running the function converges. No hand-cleanup needed
even after a power-loss interruption.

## Failure modes

- **rsync exits with code 23** — partial transfer due to permission
  errors. The most common cause is the `-X` flag we *don't* pass; if
  someone re-adds it, mismatched SELinux xattrs on s3fs break the
  copy mid-flight. Solution: keep `-aH --inplace` only.
- **MD5 verification mismatch** — function raises with paths to
  `/tmp/scratch-into-md5-{src,dst}.log` for inspection. Possible
  causes: sparse files (s3fs may not preserve sparseness), special
  files (sockets, FIFOs — should not be in scratch). Operator
  can compare manifests and decide whether the diff is benign.
- **lsof drain timeout (60s)** — something has files open under
  `/var/lib/bedrock/local/scratch` that won't release. Function
  falls through to `umount -l` (lazy unmount) which always
  succeeds; the kernel cleans up the inode when the last fd closes.
  Acceptable side-effect: writers continue against the now-orphaned
  inode, their writes are lost when the inode is freed.

## When NOT to use this function directly

Normally this is called *only* from `s3fs_mount_scratch()`, which
runs after the Garage cluster + scratch bucket are set up. Direct
operator invocation makes sense only for:

- Re-running after a partial failure (idempotent re-run completes
  the migration).
- A "promote scratch to Garage" workflow we don't currently expose
  (e.g. operator manually sets up Garage and wants to migrate
  pre-existing scratch data without going through the full
  N=1→N=2 transition). For that case, ensure the s3fs mount is
  up *before* calling.

## Sources

### Linux semantics
- [`rename(2)` — atomic on same filesystem](https://man7.org/linux/man-pages/man2/rename.2.html) — basis for `atomic_symlink()`.
- [`open(2)` — paths resolved at open time, fds reference inodes](https://man7.org/linux/man-pages/man2/open.2.html) — gives us the "old fds keep working" property after the swap.

### rsync
- [`rsync(1)` — `--inplace`](https://manpages.debian.org/testing/rsync/rsync.1.en.html#opt--inplace) — write directly to destination, no temp+rename.
- [`rsync(1)` — `-X` extended attributes](https://manpages.debian.org/testing/rsync/rsync.1.en.html#opt--xattrs) — preserves xattrs; we deliberately omit it for s3fs source compatibility (lessons-log L22).

### s3fs / Garage
- [s3fs-fuse — POSIX semantics caveats](https://github.com/s3fs-fuse/s3fs-fuse/wiki/Limitations) — the xattr behavior that informs our `-X`-omission.
- [Garage Operations — replication factor](https://garagehq.deuxfleurs.fr/documentation/operations/replication/) — RF=1 capacity sizing for scratch.

### Bedrock project
- [`tier_storage.md` — invariant #6 (s3fs targets local Garage)](tier_storage.md)
- [`docs/lessons-log.md` — L15 (this function exists), L22 (rsync -X drop)](../../docs/lessons-log.md)
- [`tier_storage.py — migrate_scratch_into_garage`](tier_storage.py)
