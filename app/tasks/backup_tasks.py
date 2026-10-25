"""Daily database backup task.

Runs pg_dump inside the pollord-db container (via the Docker socket) and
writes a timestamped, gzip-compressed dump to /opt/pollord-backend/backups/
on the host. Keeps the last 14 daily dumps; anything older is deleted.

The worker container does not need pg_dump installed — the command runs
inside the database container itself via ``docker exec``.
"""

import logging
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)

BACKUP_DIR = Path("/opt/pollord-backend/backups")
KEEP_DAYS = 14
DB_CONTAINER = "pollord-db"


@celery_app.task(name="app.tasks.backup_tasks.backup_database")
def backup_database():
    """Dump the Postgres database to a compressed file on the host.

    Runs inside the Celery worker container, which has the Docker socket
    bind-mounted from the host so it can exec into the DB container.
    Logs success/failure; does NOT raise on failure so a bad backup run
    never kills the beat schedule.
    """
    from app.core.config import settings

    # Parse credentials from the DATABASE_URL so we don't duplicate them here.
    # URL shape: postgresql+asyncpg://user:password@host:port/dbname
    url = settings.DATABASE_URL
    try:
        creds = _parse_db_url(url)
    except Exception as exc:
        logger.error("backup_database: could not parse DATABASE_URL: %s", exc)
        return {"status": "error", "reason": "bad DATABASE_URL"}

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    dest = BACKUP_DIR / f"pollard_{stamp}.sql.gz"

    cmd = [
        "docker", "exec", DB_CONTAINER,
        "pg_dump",
        "-U", creds["user"],
        "-d", creds["dbname"],
        "--no-password",
        "--format=custom",
        "--compress=9",
    ]

    try:
        with dest.open("wb") as fh:
            result = subprocess.run(
                cmd,
                stdout=fh,
                stderr=subprocess.PIPE,
                timeout=600,
                env={"PGPASSWORD": creds["password"]},
            )
        if result.returncode != 0:
            dest.unlink(missing_ok=True)
            err = result.stderr.decode(errors="replace").strip()
            logger.error("backup_database: pg_dump failed (rc=%d): %s", result.returncode, err)
            return {"status": "error", "reason": err}

        size_mb = dest.stat().st_size / (1024 * 1024)
        logger.info("backup_database: wrote %s (%.1f MB)", dest.name, size_mb)
        _prune_old_backups()
        return {"status": "ok", "file": dest.name, "size_mb": round(size_mb, 2)}

    except subprocess.TimeoutExpired:
        dest.unlink(missing_ok=True)
        logger.error("backup_database: pg_dump timed out after 600 s")
        return {"status": "error", "reason": "timeout"}
    except Exception as exc:
        dest.unlink(missing_ok=True)
        logger.exception("backup_database: unexpected error: %s", exc)
        return {"status": "error", "reason": str(exc)}


def _prune_old_backups():
    """Delete dump files older than KEEP_DAYS days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)
    removed = 0
    for path in BACKUP_DIR.glob("pollard_*.sql.gz"):
        if datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc) < cutoff:
            path.unlink()
            removed += 1
    if removed:
        logger.info("backup_database: pruned %d backup(s) older than %d days", removed, KEEP_DAYS)


def _parse_db_url(url: str) -> dict:
    """Extract user/password/host/port/dbname from a SQLAlchemy DB URL."""
    import re
    # Strip driver prefix: postgresql+asyncpg:// → postgresql://
    url = re.sub(r"^[^:]+\+[^:]+://", "postgresql://", url)
    from urllib.parse import urlparse
    p = urlparse(url)
    return {
        "user": p.username or "",
        "password": p.password or "",
        "host": p.hostname or "db",
        "port": str(p.port or 5432),
        "dbname": p.path.lstrip("/"),
    }
