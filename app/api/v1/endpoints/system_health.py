"""Admin health endpoints: full snapshot + slow query analysis."""
import asyncio
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import CONSOLE_READ_ROLES, require_roles
from app.db.base import get_db
from app.models.user import User
from app.services import health_service


class ExplainRequest(BaseModel):
    query: str

router = APIRouter(prefix="/admin/health", tags=["System Health"])


def _overall(probes: list[str]) -> str:
    if any(s == "down" for s in probes):
        return "down"
    if any(s == "degraded" for s in probes):
        return "degraded"
    return "healthy"


@router.get("", response_model=None)
async def get_health_snapshot(
    current_user: User = Depends(require_roles(*CONSOLE_READ_ROLES)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Run all service probes concurrently and return a full health snapshot."""
    hit = await health_service._hget("snapshot")
    if hit:
        return hit

    db_probe, redis_probe, smtp_probe, celery_probe, server = await asyncio.gather(
        health_service.probe_database(db),
        health_service.probe_redis(),
        health_service.probe_smtp(),
        health_service.probe_celery(),
        health_service.server_metrics(),
    )

    result = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "overall_status": _overall([
            db_probe["status"],
            redis_probe["status"],
            smtp_probe["status"],
            celery_probe["status"],
        ]),
        "database": db_probe,
        "redis": redis_probe,
        "smtp": smtp_probe,
        "celery": celery_probe,
        "beat_schedule": health_service.beat_schedule(),
        "server": server,
    }
    await health_service._hset("snapshot", result, 20)
    return result


@router.get("/routes", response_model=None)
async def get_route_stats(
    limit: int = 30,
    current_user: User = Depends(require_roles(*CONSOLE_READ_ROLES)),
) -> list[dict[str, Any]]:
    """Per-route performance aggregates from the route tracker middleware."""
    return await health_service.route_stats(limit=limit)


@router.get("/slow-queries", response_model=None)
async def get_slow_queries(
    limit: int = 20,
    current_user: User = Depends(require_roles(*CONSOLE_READ_ROLES)),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """Top slow queries from pg_stat_statements (empty if extension unavailable)."""
    return await health_service.slow_queries(db, limit=limit)


@router.get("/db/tables", response_model=None)
async def get_db_table_stats(
    current_user: User = Depends(require_roles(*CONSOLE_READ_ROLES)),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    return await health_service.db_table_stats(db)


@router.get("/db/indexes", response_model=None)
async def get_db_index_stats(
    current_user: User = Depends(require_roles(*CONSOLE_READ_ROLES)),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    return await health_service.db_index_stats(db)


@router.get("/db/activity", response_model=None)
async def get_db_live_activity(
    current_user: User = Depends(require_roles(*CONSOLE_READ_ROLES)),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    return await health_service.db_live_activity(db)


@router.get("/db/cache", response_model=None)
async def get_db_cache_stats(
    current_user: User = Depends(require_roles(*CONSOLE_READ_ROLES)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    return await health_service.db_cache_stats(db)


@router.get("/db/locks", response_model=None)
async def get_db_locks(
    current_user: User = Depends(require_roles(*CONSOLE_READ_ROLES)),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    return await health_service.db_locks(db)


@router.post("/db/explain", response_model=None)
async def post_db_explain(
    payload: ExplainRequest,
    current_user: User = Depends(require_roles(*CONSOLE_READ_ROLES)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    return await health_service.explain_query(db, payload.query)


@router.post("/cache/clear", response_model=None)
async def clear_health_cache(
    current_user: User = Depends(require_roles(*CONSOLE_READ_ROLES)),
) -> dict[str, str]:
    await health_service.invalidate_health_cache()
    return {"status": "cleared"}
