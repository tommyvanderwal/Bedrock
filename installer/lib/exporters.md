# `exporters.py`

**Module purpose.** Helpers for installing node_exporter +
vm_exporter (Bedrock's per-VM metrics scraper). Called by
`observability.bootstrap_*` paths.

## Functions

- `install_node_exporter()` — copy binary from
  `/var/lib/bedrock-install/binaries/node_exporter` to
  `/opt/bedrock/bin/`, write the systemd unit listening on
  `:9100`, enable + start.
- `install_vm_exporter()` — copy
  `vm_exporter.py` (small Python script that runs `virsh list`
  + queries libvirt for per-VM CPU/RAM/disk stats), write
  systemd unit on `:9177`, enable + start.

Both are idempotent (re-install just overwrites the binary +
restarts the unit if checksums differ).
