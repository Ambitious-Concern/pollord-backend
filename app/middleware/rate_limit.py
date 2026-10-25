from slowapi import Limiter
from slowapi.util import get_remote_address

from app.core.config import settings

# Redis-backed, not the default in-memory counter — with 2+ app replicas
# behind a load balancer, in-memory limits are per-process, so each replica
# would silently grant its own separate quota (an attacker landing on both
# replicas gets N times the intended rate). Redis makes the counters shared.
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[settings.RATE_LIMIT_DEFAULT],
    storage_uri=settings.REDIS_URL,
)
