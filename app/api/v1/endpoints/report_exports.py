"""Backend-generated CSV/Excel/PDF report exports, run as a background
Celery job — see ReportExportService (create/poll) and
report_export_runner.py (the actual generation) for the real logic."""
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import require_roles
from app.db.tenant import get_tenant_db
from app.models.user import User
from app.schemas.report_export import ReportExportJobResponse, ReportExportRequest
from app.services.report_export_service import ReportExportService

router = APIRouter(prefix="/reports/exports", tags=["Report Exports"])

ORGANIZER_ROLES = ("System Administrator", "Election Administrator", "Event Organizer")


@router.post("", response_model=ReportExportJobResponse, status_code=202)
async def create_export(
    data: ReportExportRequest,
    current_user: User = Depends(require_roles(*ORGANIZER_ROLES)),
    db: AsyncSession = Depends(get_tenant_db),
):
    """Enqueues the export and returns immediately with a pending/queued
    job — idempotent: a duplicate submit (same idempotency_key, or
    identical report_type+format+params if none is given) returns the
    existing job instead of starting a second render."""
    return await ReportExportService(db).create_or_get_job(current_user, data)


@router.get("/{job_id}", response_model=ReportExportJobResponse)
async def get_export(
    job_id: UUID,
    current_user: User = Depends(require_roles(*ORGANIZER_ROLES)),
    db: AsyncSession = Depends(get_tenant_db),
):
    return await ReportExportService(db).get_job(job_id, current_user)
