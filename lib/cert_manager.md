# installer/lib/cert_manager.py

Keeps the dashboard's TLS material fresh. Each node serves its dashboard at
`https://<lan-ip-dashed>.my.local-ip.co:8443/`, and local-ip.co publishes a free
publicly-trusted wildcard cert + key for `*.my.local-ip.co`. This module downloads
that cert+key into `/etc/bedrock/tls/`, gives the browser a green padlock that
matches the hostname, and restarts the mgmt service to pick up the new files. It
runs as a standalone script from a daily systemd timer (`main()` is the entry
point) and self-skips when the existing cert still has runway.

## Functions

### `needs_refresh() -> bool`
True when the cert is missing or close to expiry.
- **In:** none (reads `CERT_PATH`, `KEY_PATH`, `RENEW_DAYS`).
- **Out:** `True` if `cert.pem` or `key.pem` is absent, or if
  `openssl x509 -checkend (RENEW_DAYS·86400)` returns non-zero (cert not valid at
  least `RENEW_DAYS` = 30 days out). Else `False`. Side effect: one `openssl`
  subprocess (output captured).

### `download(url: str) -> bytes`
Fetch a URL over HTTPS with a 20 s timeout.
- **In:** `url` — one of the three local-ip.co endpoints.
- **Out:** response body as bytes. Side effect: outbound HTTPS GET; raises on
  network/HTTP error.

### `write_atomic(path: Path, data: bytes, mode: int) -> None`
Write a file atomically with a target permission mode.
- **In:** `path` destination; `data` bytes; `mode` octal perms.
- **Out:** none. Side effects: creates the parent dir; writes a `<path>.tmp`
  sibling, chmods it, then `replace()`s it onto `path` (no torn reads).

### `derive_hostname() -> str`
Compute the dashboard hostname from the node's LAN-facing IP.
- **In:** none.
- **Out:** `<ip-with-dots-as-dashes>.my.local-ip.co`. Side effect: opens a UDP
  socket and `connect()`s to `1.1.1.1:1` (no packet sent) to read the kernel's
  chosen source IP via `getsockname()`, then closes it.

### `refresh() -> None`
Download the cert, optional chain, and key, then write them.
- **In:** none.
- **Out:** none. Side effects: downloads `server.pem`, `server.key`, and
  best-effort `chain.pem`; writes `cert.pem` (0644, = server cert + `\n` + chain
  when present) and `key.pem` (0600); logs to stderr.

### `restart_mgmt() -> None`
Bounce the mgmt service so it reloads the TLS files.
- **In:** none.
- **Out:** none. Side effect: `systemctl restart bedrock-mgmt.service`
  (`check=False`, so a failure is ignored).

### `main() -> int`
Script entry point.
- **In:** none.
- **Out:** always `0`. If `needs_refresh()` is false, logs and returns. Otherwise
  calls `refresh()`, logs the dashboard URL via `derive_hostname()`, and
  `restart_mgmt()`.

## How it works

`main()` is a guarded refresh:

```
main()
  └─ needs_refresh()? ──no──▶ log "good for > 30 days" ─▶ return 0
        │ yes
        ▼
     refresh()           download cert + key (+ chain best-effort)
        │                cert.pem = server.pem [+ "\n" + chain.pem]
        │                write_atomic cert.pem 0644 / key.pem 0600
        ▼
     log dashboard URL   derive_hostname() → <ip-dashed>.my.local-ip.co
        ▼
     restart_mgmt()      systemctl restart bedrock-mgmt.service
        ▼
     return 0
```

Idempotency is the whole point: `openssl x509 -checkend N` exits 0 only if the cert
is still valid `N` seconds from now, so the daily timer is a no-op until the cert
falls inside the 30-day renewal window. The chain download is wrapped in
`try/except` — `cert.pem` alone is valid, so a missing chain does not fail the run.
File writes go through `write_atomic` (tmp + chmod + rename) so a half-written cert
is never read by the live server; the mgmt restart happens only after both files
land.

## Why

A publicly-trusted cert matching the hostname gives the green padlock by default, so
operators are not trained to click through browser warnings — the real security win
on a small LAN where an active MITM could serve a self-signed cert anyway. The LAN
IP is found via an outbound-route socket lookup (not interface enumeration) so the
correct NIC is chosen regardless of how many interfaces the node has.
