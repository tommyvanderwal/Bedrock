# `hardware.py`

**Module purpose.** One-shot inventory of the local hardware at
`bedrock bootstrap` time. Written into `state.json["hardware"]`
and posted to the master as part of the join request so the
operator can see what they're approving.

## Functions

- `detect() -> dict` — returns
  `{hostname, cpu_model, vcpus, ram_mb, nics, root_disk_gb}`.
  Pure read; no writes.
- `_read_cpu_model() -> str` — first `model name` line from
  `/proc/cpuinfo`.
- `_vcpus() -> int` — `nproc`.
- `_ram_mb() -> int` — from `/proc/meminfo` `MemTotal`.
- `_nics() -> list[dict]` — `ip -j addr show`, collects every
  IPv4 per NIC. Skips loopback. Returns
  `[{name, state, mac, ipv4_addrs:[...]}, …]`. Preferred
  non-link-local IPv4 if any.
- `_root_disk_gb() -> int` — `lsblk -bn -d -o NAME,SIZE | head -1`
  / 1e9.

Used by `bedrock bootstrap` (mgmt_install + agent_install both
call `hardware.detect()` and store it in state.json before any
network ops).
