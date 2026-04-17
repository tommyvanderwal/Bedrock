"""WebSocket hub — broadcasts cluster state to all connected clients."""

import asyncio
import json
import logging

from fastapi import WebSocket, WebSocketDisconnect

log = logging.getLogger("bedrock.ws")


class WSHub:
    """Manages WebSocket connections and broadcasts."""

    def __init__(self):
        self.clients: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.clients.append(ws)
        log.info("Client connected (%d total)", len(self.clients))

    def disconnect(self, ws: WebSocket):
        if ws in self.clients:
            self.clients.remove(ws)
        log.info("Client disconnected (%d total)", len(self.clients))

    async def broadcast(self, channel: str, data: dict):
        msg = json.dumps({"channel": channel, **data})
        dead = []
        for ws in self.clients:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    async def send_to(self, ws: WebSocket, channel: str, data: dict):
        try:
            await ws.send_text(json.dumps({"channel": channel, **data}))
        except Exception:
            self.disconnect(ws)


hub = WSHub()
