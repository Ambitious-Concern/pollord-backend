import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete

from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task
def purge_closed_chats():
    """Live chat transcripts aren't kept indefinitely like email support
    tickets — deletes chat-channel tickets (cascading to their encrypted
    messages) once they've been closed for SUPPORT_CHAT_RETENTION_DAYS.
    A closed ticket can still be reopened by the customer well within that
    window; this only reclaims genuinely old, resolved conversations."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.core.config import settings
    from app.models.support_ticket import SupportTicket

    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.SUPPORT_CHAT_RETENTION_DAYS)
    sync_url = settings.DATABASE_URL.replace("+asyncpg", "")
    engine = create_engine(sync_url)

    with Session(engine) as session:
        result = session.execute(
            delete(SupportTicket).where(
                SupportTicket.channel == "chat",
                SupportTicket.status == "Closed",
                SupportTicket.updated_at < cutoff,
            )
        )
        session.commit()
        if result.rowcount:
            logger.info(f"Purged {result.rowcount} closed live chat(s) past retention")

    engine.dispose()
