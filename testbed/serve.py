#!/usr/bin/env python3
"""Serve the Bedrock install repository over HTTP.

Usage:
  serve.py [--port 8000] [--bind 0.0.0.0]

Serves /home/tommy/pythonprojects/bedrock/installer/ at:
  http://<host>:8000/

Accessible from:
  - Dev box LAN: http://192.168.2.108:8000/
  - Sim nodes (bedrock-mgmt): http://192.168.100.1:8000/
"""

import argparse
import http.server
import os
import socketserver
from pathlib import Path

INSTALLER_DIR = Path(__file__).parent.parent / "installer"


class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        # Quieter logging — one line per request, no date
        print(f"{self.address_string()} {format % args}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--bind", default="0.0.0.0")
    args = p.parse_args()

    os.chdir(INSTALLER_DIR)
    print(f"Serving {INSTALLER_DIR} at http://{args.bind}:{args.port}/")
    print(f"  Install URL: http://192.168.100.1:{args.port}/install.sh")
    print(f"  Dev box URL: http://192.168.2.108:{args.port}/install.sh")
    print()
    with socketserver.TCPServer((args.bind, args.port), Handler) as httpd:
        httpd.allow_reuse_address = True
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down.")


if __name__ == "__main__":
    main()
