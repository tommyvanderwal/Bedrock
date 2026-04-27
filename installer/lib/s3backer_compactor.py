#!/usr/bin/env python3
"""
s3backer trim compactor + fill alarm.

Discard inside a VM does not always reach s3backer as a deletion (qemu may
not pass virtio-blk DISCARD through; it depends on the disk XML and the
backend file driver). Writing zeros to a block, however, DOES — s3backer
detects all-zero blocks and DELETEs the corresponding object.

This compactor walks the bucket on a schedule, reads each block, and
DELETEs blocks that read as all-zero. Run from cron or a systemd timer.

Also emits a fill-percentage warning when the underlying RustFS is
above the configured threshold.
"""

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import boto3


def list_blocks(s3, bucket):
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket):
        for obj in page.get("Contents", []) or []:
            key = obj["Key"]
            # s3backer block keys are 8 hex chars (e.g. "00000042")
            if len(key) == 8 and all(c in "0123456789abcdef" for c in key):
                yield key, obj["Size"]


def is_all_zero(s3, bucket, key):
    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    return body == b"\x00" * len(body)


def compact(s3, bucket, dry_run=False, threads=8):
    deleted = 0
    scanned = 0
    bytes_freed = 0

    def check(item):
        nonlocal deleted, bytes_freed
        key, size = item
        try:
            if is_all_zero(s3, bucket, key):
                if not dry_run:
                    s3.delete_object(Bucket=bucket, Key=key)
                deleted += 1
                bytes_freed += size
                return f"deleted {key} ({size}B)"
        except Exception as exc:
            return f"error {key}: {exc}"
        return None

    with ThreadPoolExecutor(max_workers=threads) as pool:
        for result in pool.map(check, list_blocks(s3, bucket)):
            scanned += 1
            if result and "deleted" in result:
                print(result)

    return scanned, deleted, bytes_freed


def fill_percent(s3_admin_url, bucket):
    # RustFS doesn't yet expose admin metrics reliably. Approximation:
    # compute total bytes used by listing all objects, vs known cluster cap.
    # For the trial, the caller passes total cluster cap as an arg.
    pass


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--endpoint", required=True)
    p.add_argument("--bucket", required=True)
    p.add_argument("--access-key", default=os.environ.get("AWS_ACCESS_KEY_ID"))
    p.add_argument("--secret-key", default=os.environ.get("AWS_SECRET_ACCESS_KEY"))
    p.add_argument("--region", default="us-east-1")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--threads", type=int, default=8)
    p.add_argument("--fill-warn-pct", type=float, default=80.0,
                   help="Warn if cluster fill percent exceeds this")
    p.add_argument("--fill-error-pct", type=float, default=90.0,
                   help="Error if cluster fill percent exceeds this")
    p.add_argument("--total-cap-gb", type=float,
                   help="Cluster total capacity in GiB (for fill alarm)")
    args = p.parse_args()

    s3 = boto3.client(
        "s3",
        endpoint_url=args.endpoint,
        aws_access_key_id=args.access_key,
        aws_secret_access_key=args.secret_key,
        region_name=args.region,
        config=boto3.session.Config(s3={"addressing_style": "path"},
                                    signature_version="s3v4"),
    )

    t0 = time.time()
    scanned, deleted, freed = compact(s3, args.bucket,
                                       dry_run=args.dry_run,
                                       threads=args.threads)
    dt = time.time() - t0
    print(f"compactor: scanned={scanned} deleted={deleted} "
          f"freed={freed/1e6:.1f}MB elapsed={dt:.1f}s "
          f"{'[dry-run]' if args.dry_run else ''}", flush=True)

    if args.total_cap_gb:
        # Sum remaining bucket bytes
        used_bytes = sum(size for _, size in list_blocks(s3, args.bucket))
        used_gb = used_bytes / (1 << 30)
        pct = 100.0 * used_gb / args.total_cap_gb
        msg = f"fill: {used_gb:.2f}GiB / {args.total_cap_gb:.0f}GiB ({pct:.1f}%)"
        if pct >= args.fill_error_pct:
            print(f"ERROR {msg} >= {args.fill_error_pct}%", file=sys.stderr)
            sys.exit(2)
        elif pct >= args.fill_warn_pct:
            print(f"WARN  {msg} >= {args.fill_warn_pct}%", file=sys.stderr)
            sys.exit(1)
        else:
            print(msg)


if __name__ == "__main__":
    main()
