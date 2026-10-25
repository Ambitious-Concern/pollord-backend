import hashlib
import json
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ownership import require_election_ownership, require_event_ownership
from app.models.election import Election
from app.models.event import Event
from app.models.report_export_job import ReportExportJob
from app.models.user import User
from app.repositories.election_repository import ElectionRepository
from app.repositories.event_repository import EventRepository
from app.repositories.report_export_job_repository import ReportExportJobRepository
from app.schemas.report_export import ReportExportJobResponse, ReportExportRequest
from app.tasks.report_export_tasks import generate_report_export


class ReportExportService:
    """Create/poll side — validates access, applies idempotency, and
    enqueues the Celery task. The actual data assembly + file rendering
    happens in report_export_runner.run_export_job, inside that task."""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.jobs = ReportExportJobRepository(ReportExportJob, db)
        self.election_repo = ElectionRepository(Election, db)
        self.event_repo = EventRepository(Event, db)

    async def create_or_get_job(self, user: User, data: ReportExportRequest) -> ReportExportJobResponse:
        await self._validate_access(user, data)
        key = data.idempotency_key or _derive_key(data)

        existing = await self.jobs.get_reusable(user.user_id, key)
        if existing:
            return _to_response(existing)

        # Past the reuse window a prior non-failed job is stale, but the
        # partial unique index still reserves its key — so recycle that row
        # instead of inserting a duplicate, which would raise IntegrityError.
        stale = await self.jobs.get_recyclable(user.user_id, key)
        if stale:
            job = await self.jobs.restart(stale.job_id)
        else:
            job = await self.jobs.create(
                {
                    "requested_by": user.user_id,
                    "report_type": data.report_type,
                    "format": data.format,
                    "params": data.params,
                    "idempotency_key": key,
                }
            )
        generate_report_export.delay(str(job.job_id))
        return _to_response(job)

    async def get_job(self, job_id: UUID, user: User) -> ReportExportJobResponse:
        job = await self.jobs.get_by_id(job_id, id_field="job_id")
        if not job or job.requested_by != user.user_id:
            raise HTTPException(status_code=404, detail="Export job not found")
        return _to_response(job)

    async def _validate_access(self, user: User, data: ReportExportRequest) -> None:
        """Fail fast at request time rather than deep inside the background
        task — an organizer who can't access a source shouldn't get a
        'failed' job later, they should get a 403/404 now."""
        if data.report_type == "voting":
            source_kind = data.params.get("source_kind")
            source_id = data.params.get("source_id")
            if source_kind not in ("election", "event") or not source_id:
                raise HTTPException(status_code=400, detail="voting exports need source_kind and source_id")
            if source_kind == "election":
                election = await self.election_repo.get_by_id(UUID(str(source_id)), id_field="election_id")
                if not election:
                    raise HTTPException(status_code=404, detail="Election not found")
                await require_election_ownership(election, user, self.db)
            else:
                event = await self.event_repo.get_by_id(UUID(str(source_id)), id_field="event_id")
                if not event:
                    raise HTTPException(status_code=404, detail="Event not found")
                await require_event_ownership(event, user, self.db)
        elif data.report_type == "attendance":
            event_id = data.params.get("event_id")
            if not event_id:
                raise HTTPException(status_code=400, detail="attendance exports need event_id")
            event = await self.event_repo.get_by_id(UUID(str(event_id)), id_field="event_id")
            if not event:
                raise HTTPException(status_code=404, detail="Event not found")
            await require_event_ownership(event, user, self.db)
        # financial: no per-source ownership check — org-scoped like the
        # live GET /analytics/financial endpoint it mirrors.


def _derive_key(data: ReportExportRequest) -> str:
    """Deterministic fallback so an accidental duplicate submit collapses
    to the same job even when the caller doesn't send an explicit key."""
    payload = json.dumps(
        {"report_type": data.report_type, "format": data.format, "params": data.params},
        sort_keys=True, default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _to_response(job: ReportExportJob) -> ReportExportJobResponse:
    return ReportExportJobResponse(
        job_id=job.job_id,
        report_type=job.report_type,
        format=job.format,
        status=job.status,
        download_url=job.download_url,
        error=job.error,
        created_at=job.created_at,
        completed_at=job.completed_at,
    )
