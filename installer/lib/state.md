# `state.py`

**Module purpose.** Read/write the per-node
`/etc/bedrock/state.json` file. This file is BOTH a cache of the
projected view of cluster state (refreshed by
`mgmt/orchestrator.rqlite_subscriber` on every revision tick)
AND the place where bootstrap-time facts go before rqlite is up:
`bootstrap_done`, `hardware`, `cluster_uuid`, `cluster_name`,
`node_name`, `loopback_ip`, `mgmt_ip`.

The bedrock CLI (`/usr/local/bin/bedrock`) reads this to know
who it is in the cluster. `bedrock-net` reads it for
cluster_uuid + node_name + loopback_ip. `cluster_arbiter` reads
the projected `role` field to decide should-host-arbiter.

## Functions

- `load() -> dict` — read `/etc/bedrock/state.json`, return as
  dict. Returns `{}` if the file doesn't exist (pre-bootstrap).
- `save(state)` — `json.dumps` with 2-space indent, atomic
  write via tmp+rename. Mode 0o644.
- `set_bootstrap_done(value=True)` — convenience: load, set
  `state["bootstrap_done"] = value`, save. Called by
  `cmd_bootstrap` at the end of OS prep so `bedrock init/join`
  can gate on `state["bootstrap_done"]`.
- `get(key, default=None)` — short read-and-extract.
- `update(**kv)` — short read-merge-save.

## Schema (informal)

```json
{
  "bootstrap_done": true,
  "hardware": {
    "hostname": "bedrock-ccd477",
    "cpu_model": "AMD RYZEN AI MAX+ PRO 395 …",
    "vcpus": 4,
    "ram_mb": 11698,
    "nics": [{"name":"enp1s0","state":"UP",...}],
    "root_disk_gb": 130
  },
  "cluster_uuid": "27d5edb1-…",
  "cluster_name": "test-fresh",
  "node_name": "bedrock-ccd477",
  "node_id": 1,
  "mgmt_ip": "192.168.2.38",
  "loopback_ip": "100.117.97.1",
  "role": "mgmt+compute",
  "mgmt_url": "https://192.168.2.38:8443",
  "witness_host": "192.168.2.38"
}
```

- `bootstrap_done` is set by `bedrock bootstrap` and remains
  True across re-runs.
- `hardware` is written once at bootstrap by
  `hardware.detect()`.
- `cluster_uuid`, `cluster_name`, `node_name`, `node_id`,
  `loopback_ip`, `mgmt_ip` are written at `bedrock init` /
  `bedrock join` time and never change after that (loopback is
  permanent — `lesson_rqlite_node_id_stability`).
- `role`, `mgmt_url`, `witness_host` are PROJECTED — overwritten
  on every rqlite revision tick by
  `view_builder._state_view(snapshot, node_name)`.

Joiner + master never share an `id`; each is `node_index` from
`cluster_addr` (the last octet of the loopback /32).
