"""Database backup Celery tasks.

run_backup_task(backup_id) is the single entry-point used by both the
scheduled beat job and manual admin triggers. It owns the full lifecycle:
  pending → running → completed/failed

The task writes status updates directly via a synchronous SQLAlchemy session
(using the asyncio.run / run_in_executor bridge pattern used elsewhere in
this project) so the admin UI can poll the record without scraping logs.
"""
import asyncio
import hashlib
import logging
import os
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)

BACKUP_DIR = Path("/opt/pollord-backend/backups")
KEEP_SCHEDULED = 14
KEEP_MANUAL = 30


# ── Helpers ────────────────────────────────────────────────────────────────────

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _parse_db_url(url: str) -> dict:
    import re
    from urllib.parse import urlparse
    url = re.sub(r"^[^:]+\+[^:]+://", "postgresql://", url)
    p = urlparse(url)
    return {
        "user": p.username or "",
        "password": p.password or "",
        "host": p.hostname or "db",
        "port": str(p.port or 5432),
        "dbname": p.path.lstrip("/"),
    }


async def _update_record(backup_id: uuid.UUID, **fields) -> None:
    """Write status fields to BackupRecord inside a short async session."""
    from app.db.base import async_session_maker
    from app.repositories.backup_repository import BackupRepository
    async with async_session_maker() as db:
        async with db.begin():
            await BackupRepository(db).update(backup_id, **fields)


async def _prune_old_backups_async(backup_type: str) -> None:
    """Keep only the last KEEP_* completed backups of a given type; delete older ones."""
    keep = KEEP_SCHEDULED if backup_type == "scheduled" else KEEP_MANUAL
    from app.db.base import async_session_maker
    from app.repositories.backup_repository import BackupRepository
    async with async_session_maker() as db:
        async with db.begin():
            repo = BackupRepository(db)
            records = await repo.list_completed_oldest_first(type_=backup_type)
            to_remove = records[:-keep] if len(records) > keep else []
            for rec in to_remove:
                if rec.storage_path:
                    try:
                        Path(rec.storage_path).unlink(missing_ok=True)
                    except Exception:
                        pass
                # Delete the already-loaded object directly to avoid a re-fetch
                await db.delete(rec)
            if to_remove:
                await db.flush()
    if to_remove:
        logger.info("backup prune: removed %d old %s backup(s)", len(to_remove), backup_type)


# ── Celery tasks ───────────────────────────────────────────────────────────────

@celery_app.task(name="app.tasks.backup_tasks.run_backup_task", bind=True)
def run_backup_task(self, backup_id_str: str) -> dict:
    """Run a single pg_dump and update the BackupRecord through its lifecycle.

    Accepts backup_id as a string because Celery JSON serialisation does not
    know about UUID types.
    """
    from app.core.config import settings

    backup_id = uuid.UUID(backup_id_str)

    # Mark running
    asyncio.run(_update_record(backup_id, status="running"))

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    try:
        creds = _parse_db_url(settings.DATABASE_URL)
    except Exception as exc:
        asyncio.run(_update_record(backup_id, status="failed", error=f"bad DATABASE_URL: {exc}"))
        return {"status": "error", "reason": str(exc)}

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"pollard_{stamp}.dump"
    dest = BACKUP_DIR / filename

    cmd = [
        "pg_dump",
        "-h", creds["host"],
        "-p", creds["port"],
        "-U", creds["user"],
        "-d", creds["dbname"],
        "--no-password",
        "--format=custom",
        "--compress=9",
    ]
    env = {**os.environ, "PGPASSWORD": creds["password"]}

    try:
        with dest.open("wb") as fh:
            result = subprocess.run(
                cmd, stdout=fh, stderr=subprocess.PIPE, timeout=600, env=env
            )

        if result.returncode != 0:
            dest.unlink(missing_ok=True)
            err = result.stderr.decode(errors="replace").strip()
            asyncio.run(_update_record(backup_id, status="failed", error=err))
            logger.error("run_backup_task: pg_dump failed (rc=%d): %s", result.returncode, err)
            return {"status": "error", "reason": err}

        size_bytes = dest.stat().st_size
        checksum = _sha256(dest)

        asyncio.run(_update_record(
            backup_id,
            status="completed",
            filename=filename,
            storage_path=str(dest),
            size_bytes=size_bytes,
            checksum_sha256=checksum,
            completed_at=datetime.now(timezone.utc),
            error=None,
        ))

        # Determine type from the record to apply the correct retention window
        try:
            from app.db.base import async_session_maker as _sm
            from app.repositories.backup_repository import BackupRepository

            async def _get_type() -> str:
                async with _sm() as db:
                    rec = await BackupRepository(db).get(backup_id)
                    return rec.type if rec else "scheduled"

            backup_type = asyncio.run(_get_type())
            asyncio.run(_prune_old_backups_async(backup_type))
        except Exception as exc:
            logger.warning("run_backup_task: prune failed: %s", exc)

        size_mb = round(size_bytes / 1024 / 1024, 2)
        logger.info("run_backup_task: wrote %s (%.1f MB, sha256=%s…)", filename, size_mb, checksum[:8])
        return {"status": "ok", "file": filename, "size_mb": size_mb}

    except subprocess.TimeoutExpired:
        dest.unlink(missing_ok=True)
        asyncio.run(_update_record(backup_id, status="failed", error="pg_dump timed out after 600s"))
        logger.error("run_backup_task: pg_dump timed out")
        return {"status": "error", "reason": "timeout"}
    except Exception as exc:
        dest.unlink(missing_ok=True)
        asyncio.run(_update_record(backup_id, status="failed", error=str(exc)))
        logger.exception("run_backup_task: unexpected error: %s", exc)
        return {"status": "error", "reason": str(exc)}


@celery_app.task(name="app.tasks.backup_tasks.reap_stale_backups")
def reap_stale_backups() -> dict:
    """Mark any backup stuck in pending/running for >30 min as failed.

    Covers the case where the Celery worker died mid-task and never had a
    chance to write the terminal status itself."""
    from datetime import timedelta

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)

    async def _reap() -> int:
        from app.db.base import async_session_maker
        from app.repositories.backup_repository import BackupRepository
        async with async_session_maker() as db:
            async with db.begin():
                repo = BackupRepository(db)
                stale = await repo.list_stale(older_than=cutoff)
                for rec in stale:
                    # Mutate the already-loaded objects directly; repo.update()
                    # would re-fetch each one (N+1 SELECTs) which trips asyncpg's
                    # "another operation is in progress" guard inside a transaction.
                    rec.status = "failed"
                    rec.error = "Timed out — worker may have crashed"
                if stale:
                    await db.flush()
        return len(stale)

    count = asyncio.run(_reap())
    if count:
        logger.warning("reap_stale_backups: marked %d stale backup(s) as failed", count)
    return {"reaped": count}


@celery_app.task(name="app.tasks.backup_tasks.backup_database")
def backup_database() -> dict:
    """Beat-scheduled entry point — creates a 'scheduled' BackupRecord then
    delegates to run_backup_task so both paths share the same lifecycle code."""
    from datetime import datetime, timezone

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    label = f"scheduled_{stamp}"

    async def _create() -> str:
        from app.db.base import async_session_maker
        from app.repositories.backup_repository import BackupRepository
        async with async_session_maker() as db:
            async with db.begin():
                rec = await BackupRepository(db).create(
                    label=label,
                    type="scheduled",
                    status="pending",
                )
                return str(rec.backup_id)

    backup_id_str = asyncio.run(_create())
    return run_backup_task(backup_id_str)
