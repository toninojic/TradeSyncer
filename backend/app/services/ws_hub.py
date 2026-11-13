"""Fan-out of live messages to connected browser WebSockets.

Each client has its own bounded queue and sender task, so a slow browser tab
can never slow down the copy engine.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from starlette.websockets import WebSocket

log = logging.getLogger(__name__)


class WsClient:
    def __init__(self, ws: WebSocket, max_queue: int = 500) -> None:
        self.ws = ws
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=max_queue)
        self.dropped = 0

    def offer(self, message: dict[str, Any]) -> None:
        try:
            self.queue.put_nowait(message)
        except asyncio.QueueFull:
            self.dropped += 1  # the client will resync from the next snapshot

    async def sender(self) -> None:
        while True:
            message = await self.queue.get()
            await self.ws.send_json(message)


class WsHub:
    def __init__(self) -> None:
        self.clients: set[WsClient] = set()

    @property
    def has_clients(self) -> bool:
        return bool(self.clients)

    def add(self, ws: WebSocket) -> WsClient:
        client = WsClient(ws)
        self.clients.add(client)
        return client

    def remove(self, client: WsClient) -> None:
        self.clients.discard(client)

    def publish(self, message: dict[str, Any]) -> None:
        for client in list(self.clients):
            client.offer(message)
