import redis.asyncio as aioredis

from app.core.config import settings

_client: aioredis.Redis | None = None


async def get_redis() -> aioredis.Redis:
    """Lazily create and return the single shared Redis client for the process."""
    global _client
    if _client is None:
        _client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    return _client


async def close_redis() -> None:
    """Called on app shutdown to release the connection cleanly."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
