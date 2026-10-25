import asyncio
import logging
from uuid import UUID

from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task
def generate_report_export(job_id: str):
    """Runs the actual report assembly + file rendering + upload for one
    ReportExportJob. The job itself (async, SQLAlchemy async engine) is
    bridged into this plain sync Celery task via asyncio.run — every other
    task in this codebase uses a sync engine directly instead, but the
    report services this reuses (FinancialStatsService, VotingService,
    TicketingService) are all async, and re-implementing their queries
    synchronously here would just be a second copy to keep in sync.

    The import itself is inside the try — a missing dependency or any other
    import-time error used to escape uncaught here, leaving the job stuck
    in "pending" forever with no error recorded (run_export_job's own
    try/except never even got a chance to run)."""
    try:
        from app.services.report_export_runner import run_export_job

        _run(run_export_job(UUID(job_id)))
    except Exception as exc:
        logger.exception(f"Report export job {job_id} failed outside run_export_job's own handling")
        _run(_mark_job_failed(job_id, str(exc)))


def _run(coro) -> None:
    """Run one async job in its own event loop, then drop the connection pool.

    asyncio.run() builds a fresh loop per call, but async_session_maker draws
    from a module-level engine whose pooled asyncpg connections stay bound to
    whichever loop first opened them. Without disposing, the second export in a
    worker process borrows a connection from the previous (now-closed) loop and
    dies with 'another operation is in progress' — so exports worked exactly
    once per worker restart and then failed silently, leaving jobs pending.
    """

    async def _with_fresh_pool():
        from app.db.base import engine

        try:
            return await coro
        finally:
            await engine.dispose()

    asyncio.run(_with_fresh_pool())


async def _mark_job_failed(job_id: str, error: str) -> None:
    from app.db.base import async_session_maker
    from app.models.report_export_job import ReportExportJob
    from app.repositories.report_export_job_repository import ReportExportJobRepository

    async with async_session_maker() as db:
        jobs = ReportExportJobRepository(ReportExportJob, db)
        await jobs.mark_failed(UUID(job_id), error)
        await db.commit()
