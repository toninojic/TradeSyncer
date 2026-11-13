"""Live updates for the admin panel.

Messages (JSON):
  {"type": "snapshot", "data": {...}}        dashboard state (same as GET /api/status)
  {"type": "leader_event", "data": {...}}    new leader event (trade log)
  {"type": "follower_order", "data": {...}}  new/updated follower order (trade log)
  {"type": "log", "data": {...}}             system log entry
"""

from __future__ import annotations

import asyncio
import contextlib

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.api.deps import session_user

router = APIRouter()


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    runtime = ws.app.state.runtime
    origin = ws.headers.get("origin")
    host = ws.headers.get("host", "")
    same_origin = origin is not None and origin.split("://", 1)[-1] == host
    if origin is not None and not same_origin and origin not in runtime.settings.allowed_origins:
        await ws.close(code=4403)
        return
    if session_user(ws.session, runtime) is None:
        await ws.close(code=4401)
        return

    await ws.accept()
    client = runtime.hub.add(ws)
    client.offer({"type": "snapshot", "data": runtime.snapshot()})
    sender = asyncio.create_task(client.sender())
    try:
        while True:
            # The browser may send pings; we only need to notice disconnects.
            receive = asyncio.create_task(ws.receive_text())
            done, _ = await asyncio.wait({receive, sender}, return_when=asyncio.FIRST_COMPLETED)
            if sender in done:
                receive.cancel()
                break
            text = receive.result()
            if text == "ping":
                client.offer({"type": "pong"})
    except WebSocketDisconnect:
        pass
    finally:
        runtime.hub.remove(client)
        sender.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await sender
