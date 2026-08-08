"""WebSocket endpoint relaying alerts from Redis to connected operators.

Each connection opens its own subscription. That is fine at dashboard scale
(tens of operators, not tens of thousands) and keeps the code honest: there is
no shared broadcast registry to leak connections into, and a dropped client
cleans itself up when its task ends.
"""

from __future__ import annotations

import asyncio

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.events import ALERT_CHANNEL, get_async_redis

router = APIRouter()

log = structlog.get_logger(__name__)


@router.websocket("/ws/alerts")
async def alerts_socket(websocket: WebSocket) -> None:
    await websocket.accept()
    client = get_async_redis()
    pubsub = client.pubsub()

    try:
        await pubsub.subscribe(ALERT_CHANNEL)
        while True:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=30.0)
            if message is None:
                # No alert in the window. Ping so a half-open connection
                # through a proxy that silently dropped it is detected here
                # rather than lingering as a phantom subscriber.
                await websocket.send_text('{"event":"ping"}')
                continue
            await websocket.send_text(message["data"])
    except WebSocketDisconnect:
        pass
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("alert_socket_failed")
    finally:
        # Order matters: unsubscribe before closing the connection it uses.
        # PubSub.aclose is untyped in redis-py, hence the narrow ignore.
        try:
            await pubsub.aclose()  # type: ignore[no-untyped-call]
        finally:
            await client.aclose()
