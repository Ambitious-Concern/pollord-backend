from typing import Awaitable, Callable, Type, TypeVar

from pydantic import BaseModel

from app.core.config import settings
from app.core.redis import get_redis

ModelT = TypeVar("ModelT", bound=BaseModel)


async def cached_or_compute(
    cache_key: str, model: Type[ModelT], compute: Callable[[], Awaitable[ModelT]]
) -> ModelT:
    """Cache-aside for a Pydantic response behind a short TTL. Absorbs
    refresh-spam on hot "live results" reads without breaking their
    liveness — still recomputed every LIVE_RESULTS_CACHE_TTL_SECONDS, never
    invalidated early, since a new vote landing mid-TTL is an acceptable
    delay for a live (not push-streamed) endpoint."""
    redis = await get_redis()
    cached = await redis.get(cache_key)
    if cached is not None:
        return model.model_validate_json(cached)

    result = await compute()
    await set_cached(cache_key, result)
    return result


async def set_cached(cache_key: str, result: BaseModel) -> None:
    """Exposed separately so results_stream.py's SSE push path can re-prime
    the same cache a fresh push computes, keeping REST polling and the SSE
    stream consistent instead of polling seeing stale data until the TTL
    expires."""
    redis = await get_redis()
    await redis.set(cache_key, result.model_dump_json(), ex=settings.LIVE_RESULTS_CACHE_TTL_SECONDS)
