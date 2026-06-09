# installer/lib/http_redirect.py

A tiny HTTP server bound to port 80 whose only job is to bounce every request to this node's own HTTPS dashboard with a `302`. It runs as the `bedrock-redirect` service (`:80 → :8443`) on every node, so any node can be the browser's entry point. It pairs with the mDNS responder: a browser hits `http://bedrock.local`, mDNS resolves that name to one node's LAN IP, and this redirector on that node forwards the browser to the same node's `:8443` dashboard.

## Functions / Classes

### `own_lan_ip() -> str`
Discover this host's primary LAN IPv4 address.
- **In:** none.
- **Out:** the dotted-quad source IP the kernel would use to reach the public internet. Opens a UDP socket, `connect()`s it to `1.1.1.1:1` (no packet is actually sent), reads `getsockname()[0]`, then closes the socket in a `finally`.

### `class Redirect(BaseHTTPRequestHandler)`
Request handler that turns any `GET`/`HEAD` into a `302` redirect.
- **In:** standard handler interface; reads `self.path` (the original request path + query).
- **Out:** `do_GET` (aliased by `do_HEAD`) builds the target from the node's current LAN IP and the incoming path, then sends a `302` with `Location: https://<lan-ip-dashed>.my.local-ip.co:8443<original-path>`, `Content-Length: 0`, and `Cache-Control: no-store`. `log_message` is overridden to a no-op (no per-request access lines).

### `run() -> int`
Start the redirector and serve forever.
- **In:** none.
- **Out:** returns `0` (only on shutdown). Binds a `ThreadingHTTPServer` on `("", 80)` with the `Redirect` handler, prints one startup line to stderr, then blocks in `serve_forever()`. This is the service entrypoint (`__main__` runs `sys.exit(run())`).

Module constants: `PORT = 80`, `HTTPS_PORT = 8443`, `LOCAL_IP_HOSTNAME_SUFFIX = ".my.local-ip.co"`.

## How it works

The redirect target is computed per request, not cached, so it always reflects the node's current LAN IP.

```
browser            mDNS responder          this node :80              this node :8443
  | http://bedrock.local   |                    |                          |
  |----------------------->| resolve -> LAN IP   |                          |
  | GET / on <lan-ip>:80 ---------------------->|                          |
  |                        |   own_lan_ip() -> 192.168.1.50               |
  |                        |   dash + suffix -> 192-168-1-50.my.local-ip.co
  |        302 Location: https://192-168-1-50.my.local-ip.co:8443/        |
  |<-------------------------------------------|                          |
  | GET https://192-168-1-50.my.local-ip.co:8443/ -------------------------->|
```

Each request resolves the host fresh via `own_lan_ip()`, replaces the dots with dashes, and appends `.my.local-ip.co` to form the TLS hostname. `self.path` (the original path + query) is carried straight through onto the HTTPS URL, so deep links survive the redirect. `Cache-Control: no-store` stops the browser caching the redirect, which matters because the target IP is node-specific and the entry node can change between visits.

`do_HEAD` is bound to `do_GET` because browsers sometimes probe with `HEAD` first; both get the identical `302`. The server is threading, so concurrent probes are handled independently. There is no error handling beyond what `BaseHTTPRequestHandler` / `ThreadingHTTPServer` provide; the process runs under systemd and is restarted by it.

## Why

The dashed-IP form under `.my.local-ip.co` is public wildcard DNS that resolves back to the same LAN IP, so the browser lands on a hostname that matches the dashboard's TLS certificate; redirecting to a bare `https://<ip>:8443` would trip a cert-name mismatch. Recomputing the target on every request (rather than at startup) keeps the redirect correct if the node's LAN address changes without a restart.
