"""HTTP → HTTPS redirector on port 80.

Pairs with the mDNS responder: a browser navigates to
`http://bedrock.local`, mDNS resolves that to one of the cluster
nodes' LAN IPs, this redirector on that node sends a 302 to the
node's own dashboard URL —

    https://<that-node-ip-dashed>.my.local-ip.co:8443/<original-path>

Every node runs an instance, so any node can be the entry point.
"""

from __future__ import annotations

import socket
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


PORT       = 80
HTTPS_PORT = 8443
LOCAL_IP_HOSTNAME_SUFFIX = ".my.local-ip.co"


def own_lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("1.1.1.1", 1))
        return s.getsockname()[0]
    finally:
        s.close()


class Redirect(BaseHTTPRequestHandler):
    def do_GET(self):
        host = own_lan_ip().replace(".", "-") + LOCAL_IP_HOSTNAME_SUFFIX
        target = f"https://{host}:{HTTPS_PORT}{self.path}"
        self.send_response(302)
        self.send_header("Location", target)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
    # Browsers send HEAD probes sometimes; same answer.
    do_HEAD = do_GET

    def log_message(self, fmt: str, *args) -> None:
        # journal already timestamps every line; the BaseHTTPRequestHandler
        # default doubles up. Quiet by default; uncomment for debugging.
        return


def run() -> int:
    srv = ThreadingHTTPServer(("", PORT), Redirect)
    print(f"bedrock-redirect: 302 -> https://<lan-ip-dashed>"
          f"{LOCAL_IP_HOSTNAME_SUFFIX}:{HTTPS_PORT} on :{PORT}",
          file=sys.stderr, flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(run())
