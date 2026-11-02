"""Queue management admin endpoints."""
from typing import Any

from fastapi import APIRouter, Depends

from app.core.dependencies import CONSOLE_READ_ROLES, require_roles
from app.models.user import User
from app.services import queue_service

router = APIRouter(prefix="/admin/queues", tags=["Queue Management"])


@router.get("/stats", response_model=None)
async def get_queue_stats(
    current_user: User = Depends(require_roles(*CONSOLE_READ_ROLES)),
) -> list[dict[str, Any]]:
    return await queue_service.queue_cards()


@router.get("/jobs", response_model=None)
async def get_jobs(
    state: str = "FAILURE",
    queue: str | None = None,
    limit: int = 50,
    current_user: User = Depends(require_roles(*CONSOLE_READ_ROLES)),
) -> list[dict[str, Any]]:
    return await queue_service.list_jobs(state=state, queue=queue, limit=limit)


@router.post("/jobs/{task_id}/retry", response_model=None)
async def retry_job(
    task_id: str,
    current_user: User = Depends(require_roles(*CONSOLE_READ_ROLES)),
) -> dict[str, Any]:
    return await queue_service.retry_job(task_id)


@router.delete("/{queue}/failed", response_model=None)
async def purge_failed_jobs(
    queue: str,
    current_user: User = Depends(require_roles(*CONSOLE_READ_ROLES)),
) -> dict[str, Any]:
    """Remove all tracked failed-job records for one queue (pass 'all' for every queue)."""
    q = None if queue == "all" else queue
    return await queue_service.purge_failed(q)


@router.post("/{queue}/pause", response_model=None)
async def pause_queue(
    queue: str,
    current_user: User = Depends(require_roles(*CONSOLE_READ_ROLES)),
) -> dict[str, Any]:
    return await queue_service.pause_queue(queue)


@router.post("/{queue}/resume", response_model=None)
async def resume_queue(
    queue: str,
    current_user: User = Depends(require_roles(*CONSOLE_READ_ROLES)),
) -> dict[str, Any]:
    return await queue_service.resume_queue(queue)
