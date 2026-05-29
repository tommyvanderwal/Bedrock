# mgmt/ws.py

WebSocket fan-out hub for the dashboard. It holds the set of connected client
sockets and pushes JSON frames to them — each frame tagged with a channel name.
Lives inside the asyncio mgmt side of `bedrock-d`; the dashboard's `/ws` route in
`mgmt/app.py` registers sockets here via `connect()` and answers each socket's
RPC calls with `send_to(...)`, while the periodic cluster-state loop and the
log-streaming path call `broadcast(...)`. A single process-wide instance, `hub`,
is the shared entry point.

## Functions / Classes

### `class WSHub`
Manages the live set of client WebSockets and broadcasts to them.
- **State:** `clients: list[WebSocket]` — currently connected sockets.

### `async WSHub.connect(ws) -> None`
Accepts a socket and registers it.
- **In:** `ws` — the incoming FastAPI `WebSocket`.
- **Out:** none; calls `ws.accept()`, appends to `clients`, logs the new total.

### `WSHub.disconnect(ws) -> None`
Removes a socket from the client set (sync, idempotent).
- **In:** `ws` — the socket to drop.
- **Out:** none; removes `ws` from `clients` if present (no-op otherwise), logs the new total.

### `async WSHub.broadcast(channel, data) -> None`
Sends one message to every connected client.
- **In:** `channel` — channel label string; `data` — dict payload, merged into the message body.
- **Out:** none; serializes `{"channel": channel, **data}` once and `send_text`s it to each client. Any socket that raises on send is collected and dropped via `disconnect()` after the loop.

### `async WSHub.send_to(ws, channel, data) -> None`
Sends one message to a single client.
- **In:** `ws` — target socket; `channel` — channel label; `data` — dict payload.
- **Out:** none; `send_text`s `{"channel": channel, **data}` to `ws`; on send failure, drops that socket via `disconnect()`.

### `hub`
Module-level `WSHub()` singleton — the shared hub the dashboard route and update producers import and use.

## How it works

Every frame is a single JSON object: the `channel` label plus the caller's
`data` fields spread in at the top level (not nested), so a client reads
`msg.channel` to route the payload.

```
{"channel": "<name>", <…data…>}
```

The client list is a plain Python list mutated from the asyncio event loop, so
there is no lock — correctness relies on all access happening on that one loop.

`broadcast` serializes the frame once, then `send_text`s the same string to each
client. It never lets one dead socket abort delivery to the rest: each send is
wrapped in `try/except`, failures are gathered into a `dead` list, and removals
happen only after the iteration finishes — so the list being iterated is not
mutated mid-loop. `send_to` is the single-recipient form and drops the socket
inline on failure. `disconnect` guards its removal with a membership check, so
it is safe to call on a socket that is already gone.

```
broadcast(channel, data)
        │  msg = json.dumps({"channel": channel, **data})   (once)
        ▼
   for ws in clients ──send_text(msg)──► ok
        │                  └─ raises ──► dead.append(ws)
        ▼
   for ws in dead: disconnect(ws)
```

Channels driven from `mgmt/app.py`: `"cluster"` (latest cluster-state snapshot —
sent to a client on connect via `send_to`, and broadcast on the periodic refresh
loop), `"event"` (log/event entries streamed as they arrive), and
`"rpc.response"` (per-client RPC replies carrying the request `id` plus either
`result` or `error`).
