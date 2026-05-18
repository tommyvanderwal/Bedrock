# `cert_manager.py`

**Module purpose.** TLS cert lifecycle for the mgmt dashboard
(`https://<mgmt-ip>:8443`). Self-signed CA + per-node leaf
cert. Renewed by `bedrock-cert-refresh.timer` (daily).

## Functions

- `ensure_ca() -> tuple[Path, Path]` — generate
  `/etc/bedrock/tls/ca.{key,crt}` if missing. 10-year self-
  signed CA.
- `issue_leaf(hostname, ip_list) -> tuple[Path, Path]` —
  generate a per-node leaf cert signed by the CA. SAN includes
  every IP in `ip_list` + the hostname. Written to
  `/etc/bedrock/tls/server.{key,crt}`.
- `refresh_if_due()` — re-issue if leaf is within 30 days of
  expiry. Called by the systemd timer's exec script.
- `read_ca_pem() -> bytes` — for inclusion in the dashboard's
  "download CA" button.
- `restart_mgmt_after_renewal()` — `systemctl restart
  bedrock-mgmt` so uvicorn picks up the new cert.

After renewal, joiners that pre-scanned the old cert into their
`~/.ssh/known_hosts` need to re-scan — but the mgmt API is
HTTPS not SSH, and joiners use the CA cert (which doesn't
rotate) to verify, so renewal is transparent.
