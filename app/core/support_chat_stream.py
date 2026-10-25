from typing import Awaitable, Callable, TypeVar
from uuid import UUID

from pydantic import BaseModel

from app.core.redis import get_redis

ModelT = TypeVar("ModelT", bound=BaseModel)


def _ticket_channel(ticket_id: UUID) -> str:
    return f"support_chat_updates:ticket:{ticket_id}"


def _queue_channel() -> str:
    return "support_chat_updates:queue"


async def notify_new_message(ticket_id: UUID) -> None:
    """Call after every chat message insert — wakes the one open SSE stream
    for that conversation (customer or agent side, whichever is listening)."""
    redis = await get_redis()
    await redis.publish(_ticket_channel(ticket_id), "updated")


async def notify_queue_changed() -> None:
    """Call whenever a chat starts waiting or stops waiting (claimed/closed)
    — wakes every agent's queue view so a new arrival or a claim by a
    teammate shows up without polling."""
    redis = await get_redis()
    await redis.publish(_queue_channel(), "updated")


async def stream_ticket(ticket_id: UUID, model: type, compute: Callable[[], Awaitable[ModelT]]):
    """SSE generator for one conversation: pushes the current ticket+messages
    immediately on connect, then again every time notify_new_message fires."""
    redis = await get_redis()
    pubsub = redis.pubsub(ignore_subscribe_messages=True)
    channel = _ticket_channel(ticket_id)
    await pubsub.subscribe(channel)
    try:
        result = await compute()
        yield f"data: {result.model_dump_json()}\n\n"

        async for message in pubsub.listen():
            if message["type"] != "message":
                continue
            result = await compute()
            yield f"data: {result.model_dump_json()}\n\n"
    finally:
        await pubsub.unsubscribe(channel)
        await pubsub.aclose()


async def stream_queue(compute: Callable[[], Awaitable[list]]):
    """SSE generator for the agent-side waiting queue."""
    redis = await get_redis()
    pubsub = redis.pubsub(ignore_subscribe_messages=True)
    channel = _queue_channel()
    await pubsub.subscribe(channel)
    try:
        result = await compute()
        yield f"data: {_dump_list(result)}\n\n"

        async for message in pubsub.listen():
            if message["type"] != "message":
                continue
            result = await compute()
            yield f"data: {_dump_list(result)}\n\n"
    finally:
        await pubsub.unsubscribe(channel)
        await pubsub.aclose()


def _dump_list(items: list) -> str:
    import json

    return json.dumps([item.model_dump(mode="json") for item in items])
