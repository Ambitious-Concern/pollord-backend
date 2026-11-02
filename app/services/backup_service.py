"""Backup management: trigger, list, restore, delete, retention.

pg_dump/pg_restore run as a subprocess inside the app/worker container,
which requires postgresql-client (pg_dump/pg_restore binaries). PGPASSWORD
is passed via the subprocess env — never in the command string.

Retention policy:
  - scheduled: keep last KEEP_SCHEDULED (14) completed dumps
  - manual: keep last KEEP_MANUAL (30) completed dumps
  - Each prune pass runs after a successful backup.
"""
import hashlib
import logging
import os
import re
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.backup_record import BackupRecord
from app.repositories.backup_repository import BackupRepository

logger = logging.getLogger(__name__)

BACKUP_DIR = Path("/opt/pollord-backend/backups")
KEEP_SCHEDULED = 14
KEEP_MANUAL = 30
RESTORE_CONFIRM_PHRASE = "RESTORE"


def _parse_db_url(url: str) -> dict[str, str]:
    url = re.sub(r"^[^:]+\+[^:]+://", "postgresql://", url)
    p = urlparse(url)
    return {
        "user": p.username or "",
        "password": p.password or "",
        "host": p.hostname or "db",
        "port": str(p.port or 5432),
        "dbname": p.path.lstrip("/"),
    }


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def run_pg_dump(dest: Path, creds: dict[str, str]) -> None:
    """Run pg_dump into dest; raises subprocess.CalledProcessError on failure."""
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
    with dest.open("wb") as fh:
        result = subprocess.run(
            cmd, stdout=fh, stderr=subprocess.PIPE, timeout=600, env=env
        )
    if result.returncode != 0:
        dest.unlink(missing_ok=True)
        raise subprocess.CalledProcessError(
            result.returncode, cmd,
            stderr=result.stderr.decode(errors="replace").strip()
        )


def run_pg_restore(source: Path, creds: dict[str, str]) -> None:
    """Restore from source using pg_restore --clean --if-exists."""
    cmd = [
        "pg_restore",
        "-h", creds["host"],
        "-p", creds["port"],
        "-U", creds["user"],
        "-d", creds["dbname"],
        "--no-password",
        "--clean",
        "--if-exists",
        "--single-transaction",
        str(source),
    ]
    env = {**os.environ, "PGPASSWORD": creds["password"]}
    result = subprocess.run(cmd, stderr=subprocess.PIPE, timeout=1800, env=env)
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, cmd,
            stderr=result.stderr.decode(errors="replace").strip()
        )


class BackupService:
    def __init__(self, db: AsyncSession) -> None:
        self._repo = BackupRepository(db)

    async def list_backups(self, limit: int = 50, offset: int = 0) -> list[BackupRecord]:
        return await self._repo.list_recent(limit=limit, offset=offset)

    async def count(self) -> int:
        return await self._repo.count()

    async def get_backup(self, backup_id: uuid.UUID) -> BackupRecord:
        record = await self._repo.get(backup_id)
        if not record:
            raise HTTPException(status_code=404, detail="Backup not found")
        return record

    async def stats(self) -> dict[str, Any]:
        s = await self._repo.stats()
        from celery.schedules import crontab
        from app.tasks.celery_app import celery_app
        beat = celery_app.conf.beat_schedule.get("backup-database", {})
        schedule = beat.get("schedule")
        next_scheduled = None
        if isinstance(schedule, crontab):
            next_scheduled = f"Daily at {schedule.hour}:{str(schedule.minute).zfill(2)} UTC"
        s["next_scheduled"] = next_scheduled
        return s

    async def trigger_backup(
        self, triggered_by_email: Optional[str] = None, notes: Optional[str] = None
    ) -> BackupRecord:
        """Create a BackupRecord and enqueue the Celery task."""
        from app.tasks.backup_tasks import run_backup_task

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        label = f"manual_{stamp}"
        record = await self._repo.create(
            label=label,
            type="manual",
            status="pending",
            triggered_by_email=triggered_by_email,
            notes=notes,
        )
        await self._repo._db.commit()
        run_backup_task.delay(str(record.backup_id))
        return record

    async def delete_backup(self, backup_id: uuid.UUID) -> None:
        record = await self.get_backup(backup_id)
        if record.status in ("running", "pending"):
            raise HTTPException(
                status_code=409, detail="Cannot delete a backup that is in progress"
            )
        # Remove file from disk (best-effort)
        if record.storage_path:
            try:
                Path(record.storage_path).unlink(missing_ok=True)
            except Exception as exc:
                logger.warning("delete_backup: could not remove file %s: %s", record.storage_path, exc)
        await self._repo.delete(backup_id)

    def get_download_path(self, record: BackupRecord) -> Path:
        if record.status != "completed" or not record.storage_path:
            raise HTTPException(status_code=409, detail="Backup file is not available")
        path = Path(record.storage_path)
        if not path.exists():
            raise HTTPException(status_code=404, detail="Backup file missing from disk")
        return path

    async def restore_backup(
        self, backup_id: uuid.UUID, confirm_phrase: str
    ) -> dict[str, Any]:
        """Restore the database from backup_id.

        Safety steps:
          1. Verify confirm_phrase == 'RESTORE'
          2. Verify the file exists and checksum matches
          3. Take a pre-restore snapshot (fire-and-forget, best-effort)
          4. Run pg_restore --clean --if-exists --single-transaction
        """
        if confirm_phrase != RESTORE_CONFIRM_PHRASE:
            raise HTTPException(
                status_code=422,
                detail=f"Confirmation phrase must be exactly '{RESTORE_CONFIRM_PHRASE}'"
            )

        record = await self.get_backup(backup_id)
        if record.status != "completed":
            raise HTTPException(status_code=409, detail="Only completed backups can be restored")

        path = self.get_download_path(record)

        # Verify checksum before touching the database
        if record.checksum_sha256:
            actual = _sha256(path)
            if actual != record.checksum_sha256:
                raise HTTPException(
                    status_code=409,
                    detail="Checksum mismatch — backup file may be corrupt"
                )

        # Pre-restore safety snapshot (best-effort; don't block if it fails)
        try:
            from app.tasks.backup_tasks import run_backup_task
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            pre_record = await self._repo.create(
                label=f"pre_restore_{stamp}",
                type="manual",
                status="pending",
                triggered_by_email="system",
                notes=f"Auto-snapshot before restore of {record.label}",
            )
            await self._repo._db.commit()
            run_backup_task.delay(str(pre_record.backup_id))
        except Exception as exc:
            logger.warning("restore_backup: pre-restore snapshot failed: %s", exc)

        from app.core.config import settings
        try:
            creds = _parse_db_url(settings.DATABASE_URL)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Could not parse DATABASE_URL: {exc}")

        try:
            run_pg_restore(path, creds)
        except subprocess.TimeoutExpired:
            raise HTTPException(status_code=500, detail="pg_restore timed out after 30 minutes")
        except subprocess.CalledProcessError as exc:
            raise HTTPException(status_code=500, detail=f"pg_restore failed: {exc.stderr}")

        logger.info("restore_backup: restored from %s (%s)", record.label, backup_id)
        return {
            "restored_from": record.label,
            "backup_id": str(backup_id),
            "restored_at": datetime.now(timezone.utc).isoformat(),
        }
