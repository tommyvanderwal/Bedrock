# `mgmt/ws.py`

**Module purpose.** WebSocket helpers for the dashboard:

- **Live cluster events** — orchestrator pushes "VM started",
  "node joined", "failover happened" events to a shared
  asyncio queue; this module fans them out to connected
  browsers.
- **Log streams** — tail VictoriaLogs query results in
  near-real-time for the dashboard's logs panel.
- **VNC consoles** — proxies to per-VM novnc (TCP to libvirt's
  VNC port).

## Functions

- `register_subscriber(queue) -> int` — add a queue to the fan-out
  set; returns a subscriber id for cleanup.
- `unregister_subscriber(subscriber_id)`.
- `publish(event_dict)` — push to every subscriber's queue.
- `vnc_proxy(websocket, host, port) -> None` — bridge a browser
  WS to libvirt's TCP VNC on `host:port`.
