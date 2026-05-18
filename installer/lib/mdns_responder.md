# `mdns_responder.py`

**Module purpose.** Service advertisement for the cluster — runs
on every node, multicasts `_bedrock._tcp` records over mDNS so a
new joiner's `bedrock join` (with no explicit hint) can discover
existing clusters on the LAN.

Currently advertise-only. The discovery side (`discovery.py`)
has a placeholder mDNS lookup that isn't wired in for v1.0.

## Functions

- `main()` — entry point of `bedrock-mdns.service`. Reads
  cluster.json for cluster_uuid + cluster_name + mgmt_master,
  publishes a continuous mDNS announcement.
- `_build_record(cluster_uuid, cluster_name, mgmt_url) -> bytes`
  — DNS SD record format: PTR for `_bedrock._tcp.local`, SRV
  pointing at the mgmt URL, TXT with cluster_uuid +
  cluster_name.

Idempotent: re-announces every 60 s; clients that already cache
the record skip the re-render.
