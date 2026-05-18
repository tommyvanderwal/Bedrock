# `os_setup.py`

**Module purpose.** OS-level prep at `bedrock bootstrap` time:
SELinux, firewalld, hostname, the `br0` bridge over the
operator's mgmt NIC, hostsfile + chrony, sysctls. Idempotent;
safe to re-run after a wipe.

## Functions

- `configure_base(hw)` — SELinux to permissive (DRBD's labelling
  isn't perfect), `firewalld` disabled, set hostname from
  `hw["hostname"]`, ensure `/root/.ssh/authorized_keys` mode
  0600.
- `configure_bridge(hw)` — picks the first non-loopback NIC with
  a UP-state DHCP lease, and converts it into `br0` (Linux
  bridge) so libvirt can attach VMs to the operator's LAN. Uses
  `nmcli` to flip the connection profile. No-op if `br0` already
  exists. Survives NetworkManager re-renders.
- `configure_chrony(witness_host=None)` — ensure chrony is
  installed + enabled; if `witness_host` is given, add it as a
  preferred time source. Otherwise leave default pool.
- `configure_sysctls()` — `net.ipv4.conf.all.rp_filter=2`,
  `net.ipv4.ip_forward=1`,
  `net.ipv4.fib_multipath_hash_policy=1`. Persists via
  `/etc/sysctl.d/99-bedrock.conf`. (bedrock-net's run_daemon
  also applies these at runtime as a safety net.)
- `ensure_hostsfile()` — make sure `127.0.0.1 localhost` exists;
  add `<mgmt_ip> <hostname>` so hostname-based local lookups
  resolve without DNS.
