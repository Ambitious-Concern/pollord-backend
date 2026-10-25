from typing import Awaitable, Callable, Optional, Type, TypeVar
from uuid import UUID

from pydantic import BaseModel

from app.core.redis import get_redis
from app.core.results_cache import set_cached

ModelT = TypeVar("ModelT", bound=BaseModel)


def _channel(parent_kind: str, parent_id: UUID) -> str:
    return f"live_results_updates:{parent_kind}:{parent_id}"


def _cache_key(parent_kind: str, parent_id: UUID) -> str:
    return f"live_results:{parent_kind}:{parent_id}"


async def notify_vote_cast(election_id: Optional[UUID], event_id: Optional[UUID]) -> None:
    """Call once after any successful vote insert — wakes every open SSE
    stream for that election/event so it pushes a fresh tally, without
    those streams having to poll Redis themselves."""
    parent_kind, parent_id = ("election", election_id) if election_id else ("event", event_id)
    redis = await get_redis()
    await redis.publish(_channel(parent_kind, parent_id), "updated")


async def stream_results(
    parent_kind: str,
    parent_id: UUID,
    model: Type[ModelT],
    compute: Callable[[], Awaitable[ModelT]],
):
    """SSE generator: pushes the current tally immediately on connect, then
    again every time notify_vote_cast fires for this election/event. Also
    re-primes the REST poll path's cache with each fresh value, so a client
    still polling GET /live-results sees the same number right away instead
    of waiting out the TTL."""
    redis = await get_redis()
    pubsub = redis.pubsub(ignore_subscribe_messages=True)
    channel = _channel(parent_kind, parent_id)
    cache_key = _cache_key(parent_kind, parent_id)
    await pubsub.subscribe(channel)
    try:
        result = await compute()
        await set_cached(cache_key, result)
        yield f"data: {result.model_dump_json()}\n\n"

        async for message in pubsub.listen():
            if message["type"] != "message":
                continue
            result = await compute()
            await set_cached(cache_key, result)
            yield f"data: {result.model_dump_json()}\n\n"
    finally:
        await pubsub.unsubscribe(channel)
        await pubsub.aclose()
