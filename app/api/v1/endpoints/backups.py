"""Admin backup management endpoints.

All endpoints require System Administrator — backups are a platform-level
concern, not per-organisation.
"""
import uuid
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import require_roles
from app.db.base import get_db
from app.models.user import User
from app.schemas.backup import (
    BackupRecordResponse,
    BackupStatsResponse,
    RestoreRequest,
    TriggerBackupRequest,
)
from app.services.backup_service import BackupService

router = APIRouter(prefix="/admin/backups", tags=["Backups"])

ADMIN_ONLY = ("System Administrator",)


@router.get("/stats", response_model=BackupStatsResponse)
async def get_backup_stats(
    current_user: User = Depends(require_roles(*ADMIN_ONLY)),
    db: AsyncSession = Depends(get_db),
) -> Any:
    return await BackupService(db).stats()


@router.get("", response_model=list[BackupRecordResponse])
async def list_backups(
    limit: int = 50,
    offset: int = 0,
    current_user: User = Depends(require_roles(*ADMIN_ONLY)),
    db: AsyncSession = Depends(get_db),
) -> Any:
    return await BackupService(db).list_backups(limit=limit, offset=offset)


@router.post("", response_model=BackupRecordResponse, status_code=202)
async def trigger_backup(
    data: TriggerBackupRequest,
    current_user: User = Depends(require_roles(*ADMIN_ONLY)),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Enqueue a manual backup and return the pending record immediately."""
    return await BackupService(db).trigger_backup(
        triggered_by_email=current_user.email,
        notes=data.notes,
    )


@router.get("/{backup_id}", response_model=BackupRecordResponse)
async def get_backup(
    backup_id: uuid.UUID,
    current_user: User = Depends(require_roles(*ADMIN_ONLY)),
    db: AsyncSession = Depends(get_db),
) -> Any:
    return await BackupService(db).get_backup(backup_id)


@router.get("/{backup_id}/download")
async def download_backup(
    backup_id: uuid.UUID,
    current_user: User = Depends(require_roles(*ADMIN_ONLY)),
    db: AsyncSession = Depends(get_db),
):
    """Stream the backup file as an attachment."""
    svc = BackupService(db)
    record = await svc.get_backup(backup_id)
    path = svc.get_download_path(record)
    return FileResponse(
        path=str(path),
        media_type="application/octet-stream",
        filename=record.filename or path.name,
        headers={"Content-Disposition": f'attachment; filename="{record.filename or path.name}"'},
    )


@router.post("/{backup_id}/restore", response_model=None)
async def restore_backup(
    backup_id: uuid.UUID,
    data: RestoreRequest,
    current_user: User = Depends(require_roles(*ADMIN_ONLY)),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Restore the database from a completed backup.

    Requires `confirm_phrase: 'RESTORE'` in the request body as a
    deliberate friction point — this operation is irreversible without a
    subsequent backup.
    """
    return await BackupService(db).restore_backup(backup_id, data.confirm_phrase)


@router.delete("/{backup_id}", status_code=204)
async def delete_backup(
    backup_id: uuid.UUID,
    current_user: User = Depends(require_roles(*ADMIN_ONLY)),
    db: AsyncSession = Depends(get_db),
) -> None:
    await BackupService(db).delete_backup(backup_id)
