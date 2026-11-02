"""Lightweight per-route performance tracker.

Intercepts every response and stores call count, cumulative latency,
max latency, error count, and last HTTP status in a Redis hash per
normalised route (method + path with IDs stripped).  TTL is 7 days.
"""
import re
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I
)
_INT_SEG = re.compile(r"/\d+(?=/|$)")

_SKIP_PREFIXES = ("/uploads/", "/docs", "/redoc", "/openapi", "/health", "/static")


def _normalize(path: str) -> str:
    path = _UUID_RE.sub("{id}", path)
    path = _INT_SEG.sub("/{id}", path)
    return path


class RouteTrackerMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        t0 = time.perf_counter()
        response = await call_next(request)
        ms = round((time.perf_counter() - t0) * 1000, 2)

        path = request.url.path
        if any(path.startswith(p) for p in _SKIP_PREFIXES):
            return response

        method = request.method
        norm = _normalize(path)
        key = f"health:route:{method}:{norm}"

        try:
            from app.core.redis import get_redis

            r = await get_redis()
            status = response.status_code
            is_error = 1 if status >= 400 else 0

            pipe = r.pipeline()
            pipe.hincrby(key, "calls", 1)
            pipe.hincrbyfloat(key, "total_ms", ms)
            pipe.hincrby(key, "errors", is_error)
            pipe.hset(key, "last_status", status)
            pipe.expire(key, 86400 * 7)
            await pipe.execute()

            # Non-atomic max update — fine for monitoring purposes
            cur = await r.hget(key, "max_ms")
            if cur is None or ms > float(cur):
                await r.hset(key, "max_ms", ms)
        except Exception:
            pass

        return response
