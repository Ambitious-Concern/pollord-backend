from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.report_export_job import ReportExportJob
from app.repositories.base import BaseRepository


# Long enough that a legitimately slow render is never treated as stranded
# (Celery's hard task_time_limit is 300s, so this is 3x the worst case), short
# enough that a retry after a lost task doesn't leave the user stuck.
REUSE_WINDOW = timedelta(minutes=15)


class ReportExportJobRepository(BaseRepository[ReportExportJob]):
    """Job CRUD, plus the idempotent lookup that's the actual
    "prevent double clicks" mechanism (see ReportExportJob's docstring)."""

    def __init__(self, model, session: AsyncSession):
        super().__init__(model, session)

    async def get_reusable(self, user_id: UUID, idempotency_key: str) -> Optional[ReportExportJob]:
        """A *recent* prior job for this exact (user, key) that's still in
        flight or already succeeded — reused instead of enqueueing a
        duplicate. A FAILED prior job is not reused, so the caller can retry
        freely.

        The age bound is what stops a stranded job blocking exports forever: a
        task can be lost (enqueued while no worker was up, or the worker died
        mid-render) leaving its row 'pending' permanently. Unbounded, this
        lookup would hand that corpse back on every retry and the user could
        never start a new export — no error, just no file, indefinitely.

        It also stops a 'completed' job being served as a stale cache: past the
        window a re-export re-renders against current data instead of
        returning last month's file.
        """
        cutoff = datetime.now(timezone.utc) - REUSE_WINDOW
        result = await self.session.execute(
            select(ReportExportJob).where(
                ReportExportJob.requested_by == user_id,
                ReportExportJob.idempotency_key == idempotency_key,
                ReportExportJob.status.in_(("pending", "processing", "completed")),
                ReportExportJob.created_at >= cutoff,
            )
            .order_by(ReportExportJob.created_at.desc())
        )
        return result.scalars().first()

    async def get_recyclable(self, user_id: UUID, idempotency_key: str) -> Optional[ReportExportJob]:
        """The row the partial unique index would collide with, if any.

        The index is UNIQUE(requested_by, idempotency_key) WHERE status !=
        'failed', so a non-failed row of any age blocks inserting another for
        the same key. Past the reuse window that row is no longer servable
        (stale file, or a job whose task was lost) but it still occupies the
        key — so it gets reset and re-run rather than duplicated.
        """
        result = await self.session.execute(
            select(ReportExportJob)
            .where(
                ReportExportJob.requested_by == user_id,
                ReportExportJob.idempotency_key == idempotency_key,
                ReportExportJob.status != "failed",
            )
            .order_by(ReportExportJob.created_at.desc())
        )
        return result.scalars().first()

    async def restart(self, job_id: UUID) -> Optional[ReportExportJob]:
        """Hand a stale job back to the queue as a fresh pending run."""
        job = await self.get_by_id(job_id, id_field="job_id")
        if job:
            job.status = "pending"
            job.error = None
            job.download_url = None
            job.completed_at = None
            # Restarts the reuse window too, so this run is dedupable again.
            job.created_at = datetime.now(timezone.utc)
            await self.session.flush()
        return job

    async def mark_processing(self, job_id: UUID) -> None:
        job = await self.get_by_id(job_id, id_field="job_id")
        if job:
            job.status = "processing"
            await self.session.flush()

    async def mark_completed(self, job_id: UUID, download_url: str) -> None:
        job = await self.get_by_id(job_id, id_field="job_id")
        if job:
            job.status = "completed"
            job.download_url = download_url
            job.completed_at = datetime.now(timezone.utc)
            await self.session.flush()

    async def mark_failed(self, job_id: UUID, error: str) -> None:
        job = await self.get_by_id(job_id, id_field="job_id")
        if job:
            job.status = "failed"
            job.error = error[:2000]
            job.completed_at = datetime.now(timezone.utc)
            await self.session.flush()
