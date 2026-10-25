import logging

from app.core.redis import get_redis

logger = logging.getLogger(__name__)

_KEY_PREFIX = "revoked_session:"


async def revoke_session(jti: str, ttl_seconds: int) -> None:
    """Mark one session's access tokens as immediately invalid, for up to
    ttl_seconds (the longest-lived access token that could still carry this
    jti). Complements the DB-backed refresh_tokens.revoked flag, which only
    stops that session getting a *new* access token — this stops the one it
    already has from working right away, instead of waiting out its natural
    expiry (ACCESS_TOKEN_EXPIRE_MINUTES)."""
    try:
        redis = await get_redis()
        await redis.set(f"{_KEY_PREFIX}{jti}", "1", ex=ttl_seconds)
    except Exception:
        logger.exception("Failed to record session revocation in Redis for jti=%s", jti)


async def is_session_revoked(jti: str) -> bool:
    """Fails open — a Redis outage falls back to the slower path (the
    token_version check in get_current_user still catches a full logout;
    only an in-flight single-device revoke would go uncaught until Redis is
    back), rather than locking every authenticated user out."""
    try:
        redis = await get_redis()
        return bool(await redis.exists(f"{_KEY_PREFIX}{jti}"))
    except Exception:
        logger.exception("Failed to check session revocation in Redis for jti=%s", jti)
        return False
