import asyncio
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import require_roles
from app.db.base import get_db
from app.schemas.app_log import (
    AppLogListResponse,
    AppLogResponse,
    AppLogStats,
    AppLogTrendResponse,
    ResolveRequest,
)
from app.services.app_log_service import AppLogService, _enrich
from app.repositories.app_log_repository import AppLogRepository
from app.db.base import async_session_maker

router = APIRouter(prefix="/admin/app-logs", tags=["app-logs"])

REQUIRED_ROLES = ("System Administrator",)

_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def _svc(db: AsyncSession = Depends(get_db)) -> AppLogService:
    return AppLogService(db)


@router.get("/stats", response_model=AppLogStats)
async def get_stats(
    _admin=Depends(require_roles(*REQUIRED_ROLES)),
    svc: AppLogService = Depends(_svc),
):
    return await svc.get_stats()


@router.get("/trend", response_model=AppLogTrendResponse)
async def get_trend(
    hours: int = Query(24, ge=1, le=168),
    _admin=Depends(require_roles(*REQUIRED_ROLES)),
    svc: AppLogService = Depends(_svc),
):
    return await svc.trend(hours)


@router.get("/issues", response_model=AppLogListResponse)
async def list_issues(
    level: Optional[str] = Query(None),
    resolved: Optional[bool] = Query(None),
    search: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    logger_name: Optional[str] = Query(None),
    path_prefix: Optional[str] = Query(None),
    release: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    _admin=Depends(require_roles(*REQUIRED_ROLES)),
    svc: AppLogService = Depends(_svc),
):
    return await svc.list_issues(
        level=level, resolved=resolved, search=search,
        source=source, logger_name=logger_name,
        path_prefix=path_prefix, release=release,
        limit=limit, offset=offset,
    )


@router.get("/stream")
async def stream_logs(
    since: Optional[str] = Query(None),
    _admin=Depends(require_roles(*REQUIRED_ROLES)),
):
    """SSE endpoint — streams new AppLog rows as they arrive (polling every 3s)."""
    cursor_dt = datetime.now(timezone.utc)
    if since:
        try:
            cursor_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError:
            pass

    async def generator():
        nonlocal cursor_dt
        try:
            while True:
                async with async_session_maker() as db:
                    repo = AppLogRepository(db)
                    rows = await repo.list_since(cursor_dt, limit=50)
                if rows:
                    for row in rows:
                        enriched = _enrich(row)
                        yield f"data: {enriched.model_dump_json()}\n\n"
                    cursor_dt = rows[-1].created_at
                else:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(3)
        except asyncio.CancelledError:
            return

    return StreamingResponse(generator(), media_type="text/event-stream", headers=_SSE_HEADERS)


@router.get("/", response_model=AppLogListResponse)
async def list_logs(
    level: Optional[str] = Query(None),
    resolved: Optional[bool] = Query(None),
    search: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    logger_name: Optional[str] = Query(None),
    path_prefix: Optional[str] = Query(None),
    release: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    _admin=Depends(require_roles(*REQUIRED_ROLES)),
    svc: AppLogService = Depends(_svc),
):
    return await svc.list_logs(
        level=level, resolved=resolved, search=search,
        source=source, logger_name=logger_name,
        path_prefix=path_prefix, release=release,
        limit=limit, offset=offset,
    )


@router.get("/{log_id}", response_model=AppLogResponse)
async def get_log(
    log_id: uuid.UUID,
    _admin=Depends(require_roles(*REQUIRED_ROLES)),
    svc: AppLogService = Depends(_svc),
):
    log = await svc.get_log(log_id)
    if not log:
        raise HTTPException(404, "Log not found")
    return log


@router.patch("/{log_id}/resolve", response_model=AppLogResponse)
async def resolve_log(
    log_id: uuid.UUID,
    body: ResolveRequest,
    _admin=Depends(require_roles(*REQUIRED_ROLES)),
    svc: AppLogService = Depends(_svc),
):
    log = await svc.resolve(log_id, body.resolved_by_email)
    if not log:
        raise HTTPException(404, "Log not found")
    return log


@router.post("/resolve-issue/{fingerprint}")
async def resolve_issue(
    fingerprint: str,
    body: ResolveRequest,
    _admin=Depends(require_roles(*REQUIRED_ROLES)),
    svc: AppLogService = Depends(_svc),
):
    count = await svc.resolve_issue(fingerprint, body.resolved_by_email)
    return {"resolved": count}


@router.delete("/{log_id}", status_code=204)
async def delete_log(
    log_id: uuid.UUID,
    _admin=Depends(require_roles(*REQUIRED_ROLES)),
    svc: AppLogService = Depends(_svc),
):
    ok = await svc.delete(log_id)
    if not ok:
        raise HTTPException(404, "Log not found")
