import logging
from datetime import datetime, timezone

from sqlalchemy import delete

from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task
def prune_expired_refresh_tokens():
    """refresh_tokens gets one row per login/refresh and is never cleaned up
    inline (rows are only ever marked revoked, not deleted) — this keeps it
    from growing forever."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.core.config import settings
    from app.models.refresh_token import RefreshToken

    sync_url = settings.DATABASE_URL.replace("+asyncpg", "")
    engine = create_engine(sync_url)

    with Session(engine) as session:
        result = session.execute(
            delete(RefreshToken).where(RefreshToken.expires_at < datetime.now(timezone.utc))
        )
        session.commit()
        if result.rowcount:
            logger.info(f"Pruned {result.rowcount} expired refresh token(s)")

    engine.dispose()
