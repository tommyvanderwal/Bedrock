# `s3backer_compactor.py`

**Module purpose.** Standalone script (run from cron or a systemd
timer) that walks an s3backer-backed bucket, finds blocks that
read as all-zero, and DELETEs them. Reclaims space that virtio-blk
DISCARD didn't manage to pass through to s3backer.

**This script is legacy** — Bedrock no longer uses s3backer in
the bulk-tier path (SeaweedFS replaced it for cluster-wide
replication). Kept in-tree because some experimental setups
still mount s3backer on top of Bedrock's S3 endpoint for
operator-VM disk images.

## Functions

- `list_blocks(s3, bucket)` — paginate S3 `ListObjectsV2`.
- `is_all_zero(s3, bucket, key, block_size)` — read the object,
  check every byte == 0.
- `compact(s3, bucket, block_size, parallel=8)` — main loop:
  for each block, if all-zero, DELETE.
- `check_fill(s3, bucket, threshold_pct=85)` — emit a warning
  log line if backing store is above the threshold.
- `main()` — CLI: `--bucket`, `--block-size`, `--threshold`.
