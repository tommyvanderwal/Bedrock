#!/bin/bash
# Build the patched RustFS container image.
#
# Prerequisites:
#   - Docker CLI (no buildx required — we strip BuildKit-only directives).
#   - The patch branch fix/shared-lock-stale-writers-waiting checked out in $RUSTFS_SRC.
#
# Usage:
#   RUSTFS_SRC=/path/to/rustfs ./build-patched.sh [tag]

set -euo pipefail

RUSTFS_SRC="${RUSTFS_SRC:-/tmp/rustfs-src/rustfs}"
TAG="${1:-rustfs:patched-3node-readq}"

if [ ! -d "$RUSTFS_SRC" ]; then
    echo "RUSTFS_SRC=$RUSTFS_SRC does not exist. Clone https://github.com/tommyvanderwal/rustfs first." >&2
    exit 1
fi

cd "$RUSTFS_SRC"

# Reset Dockerfile.source so we always start from a clean slate
git checkout HEAD -- Dockerfile.source

# 1) Strip --mount=type=cache directives. These need BuildKit; the dev box's
#    Docker doesn't have buildx. perl -0pe handles the multi-line RUN form.
perl -i -0pe 's/RUN\s+(--mount=type=cache,target=\S+\s*\\\s*)+/RUN /g' Dockerfile.source
perl -i -pe 's/^\s+--mount=type=cache,target=\S+\s*\\?\n//g' Dockerfile.source

# 2) Switch the runtime base from ubuntu:22.04 (glibc 2.35) to ubuntu:24.04
#    (glibc 2.39+) so the trixie-built binary's GLIBC_2.39 symbol resolves.
sed -i 's|FROM ubuntu:22.04|FROM ubuntu:24.04|' Dockerfile.source

# 3) Build
docker build -f Dockerfile.source -t "$TAG" .

# Show the built image
docker images "$TAG"
