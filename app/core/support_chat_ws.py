"""WebSocket push helpers for live chat — mirrors support_chat_stream.py but
uses WebSocket.send_text instead of SSE yield. Both share the same Redis
pub/sub channels so notify_new_message / notify_queue_changed wake either."""
import asyncio
import json
from typing import Awaitable, Callable, TypeVar
from uuid import UUID

from fastapi import WebSocket
from pydantic import BaseModel

from app.core.redis import get_redis

ModelT = TypeVar("ModelT", bound=BaseModel)

_QUEUE_CHANNEL = "support_chat_updates:queue"


def _ticket_channel(ticket_id: UUID) -> str:
    return f"support_chat_updates:ticket:{ticket_id}"


async def handle_queue_ws(
    websocket: WebSocket,
    compute: Callable[[], Awaitable[list]],
) -> None:
    await websocket.accept()
    redis = await get_redis()
    pubsub = redis.pubsub(ignore_subscribe_messages=True)
    await pubsub.subscribe(_QUEUE_CHANNEL)

    async def _listen() -> None:
        result = await compute()
        await websocket.send_text(_dump_list(result))
        async for msg in pubsub.listen():
            if msg["type"] != "message":
                continue
            result = await compute()
            await websocket.send_text(_dump_list(result))

    async def _watch_close() -> None:
        while True:
            frame = await websocket.receive()
            if frame.get("type") == "websocket.disconnect":
                return

    listen_task = asyncio.create_task(_listen())
    close_task = asyncio.create_task(_watch_close())
    try:
        done, pending = await asyncio.wait(
            [listen_task, close_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
    except Exception:
        listen_task.cancel()
        close_task.cancel()
    finally:
        await pubsub.unsubscribe(_QUEUE_CHANNEL)
        await pubsub.aclose()


async def handle_ticket_ws(
    ticket_id: UUID,
    websocket: WebSocket,
    compute: Callable[[], Awaitable[ModelT]],
) -> None:
    await websocket.accept()
    redis = await get_redis()
    pubsub = redis.pubsub(ignore_subscribe_messages=True)
    channel = _ticket_channel(ticket_id)
    await pubsub.subscribe(channel)

    async def _listen() -> None:
        result = await compute()
        await websocket.send_text(result.model_dump_json())
        async for msg in pubsub.listen():
            if msg["type"] != "message":
                continue
            result = await compute()
            await websocket.send_text(result.model_dump_json())

    async def _watch_close() -> None:
        while True:
            frame = await websocket.receive()
            if frame.get("type") == "websocket.disconnect":
                return

    listen_task = asyncio.create_task(_listen())
    close_task = asyncio.create_task(_watch_close())
    try:
        done, pending = await asyncio.wait(
            [listen_task, close_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
    except Exception:
        listen_task.cancel()
        close_task.cancel()
    finally:
        await pubsub.unsubscribe(channel)
        await pubsub.aclose()


def _dump_list(items: list) -> str:
    return json.dumps([item.model_dump(mode="json") for item in items])
