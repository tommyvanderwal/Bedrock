# RustFS source patches for Bedrock

Local copies of the RustFS source modifications that Bedrock needs.
Each patch is also published as a branch on the fork at
<https://github.com/tommyvanderwal/rustfs> for easy rebase/upstream when
RustFS releases new versions.

## Current patch set (against `rustfs/rustfs@main` ≥ 1.0.0-alpha.99)

Two patches, both on the same branch and applied in order:

| # | file | rationale |
|---|---|---|
| 0001 | `0001-relax-read-quorum-for-small-clusters.patch` | dsync read-quorum collapses to majority at N≤3, blocking 1-node-loss reads. Lower it to 1 for shared/read locks when `clients.len() <= 3`. Writes unchanged. |
| 0002 | `0002-shared-lock-bypass-stale-writers-waiting.patch` | Shared-lock fast path was blocking on `WRITERS_WAITING_MASK`, which leaks when a peer dies mid-acquire (slow-path waiter task cancelled before `dec_writers_waiting()`). Make shared locks only block on actual exclusive holder. **This is an upstream bug, not a 3-node-specific issue** — see `docs/scenarios/rustfs-shared-lock-leak-2026-04-27.md` for the full bug analysis + safety audit. |

Branch: [`fix/dsync-read-quorum-3node`](https://github.com/tommyvanderwal/rustfs/tree/fix/dsync-read-quorum-3node)

Combined effect on the 3-node EC:1 trial: 70-93 % → **100 % (370/370)**
read+write success across all victim/endpoint permutations under
1-node-loss. See `docs/scenarios/rustfs-3node-trial-2026-04-27.md` for
the layered validation.

## How to rebase onto a new RustFS release

```bash
cd /tmp/rustfs-src/rustfs   # or your local clone

# Update remotes
git remote update --prune

# Rebase the patch branch onto the new tag
git checkout fix/dsync-read-quorum-3node
git rebase v1.0.0-<new-tag>

# Conflicts are most likely in crates/lock/src/distributed_lock.rs around
# the read_quorum function. Re-apply the relaxed `client_count <= 3 -> 1`
# branch and keep the upstream majority for `client_count >= 4`.

git push --force-with-lease origin fix/dsync-read-quorum-3node
```

## Building the patched container image

```bash
cd /tmp/rustfs-src/rustfs

# Strip BuildKit cache mounts if your Docker doesn't have buildx
sed -i '/--mount=type=cache/d' Dockerfile.source

# Use a runtime base with matching glibc to the builder (trixie -> glibc 2.41)
sed -i 's|FROM ubuntu:22.04|FROM ubuntu:24.04|' Dockerfile.source

docker build -f Dockerfile.source -t rustfs:patched-3node-readq .
```

Expected build time on a modest dev box: 10–15 minutes (cargo cache hits
on second run).

## Loading into the sim cluster

```bash
docker save rustfs:patched-3node-readq -o /tmp/rustfs-patched.tar
for ip in 183 184 185; do
  scp /tmp/rustfs-patched.tar root@192.168.2.$ip:/tmp/
  ssh root@192.168.2.$ip 'podman load -i /tmp/rustfs-patched.tar'
  ssh root@192.168.2.$ip 'sed -i "s|docker.io/rustfs/rustfs:1.0.0-alpha.99|docker.io/library/rustfs:patched-3node-readq|" /etc/systemd/system/rustfs.service'
  ssh root@192.168.2.$ip 'systemctl daemon-reload && systemctl restart rustfs.service'
done
```

## When to drop these patches

- Once `rustfs/rustfs` upstream merges a fix for the N=3 dsync quorum
  case (track the issue equivalent of #2269 for N=3).
- Or once Bedrock standardizes on 4+ node deployments and the patch is
  no longer needed (the patch is a no-op when `clients.len() >= 4`).
