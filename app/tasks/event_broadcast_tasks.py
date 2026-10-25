import logging
from typing import List

from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, max_retries=2, default_retry_delay=60)
def send_broadcast_chunk(self, broadcast_id: str, recipient_emails: List[str], subject: str, html_body: str):
    """Send one chunk of a ticket-holder broadcast and atomically fold the
    outcome into the parent EventBroadcast's running counts.

    The service fans out one call per chunk (see EventBroadcastService.send)
    rather than a single task over the whole recipient list, so no one task
    risks tripping celery_app's global task_time_limit on a large event. The
    counts UPDATE is a single column-relative statement so concurrent chunks
    finishing at the same time can't clobber each other."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from app.core.config import settings
    from app.services.email_service import send_email

    sent = 0
    failed = 0
    for email in recipient_emails:
        try:
            ok = send_email(email, subject, html_body)
        except Exception:
            ok = False
        if ok:
            sent += 1
        else:
            failed += 1

    sync_url = settings.DATABASE_URL.replace("+asyncpg", "")
    engine = create_engine(sync_url)
    with Session(engine) as session:
        session.execute(
            text(
                """
                UPDATE event_broadcasts
                SET sent_count = sent_count + :sent,
                    failed_count = failed_count + :failed,
                    status = CASE
                        WHEN sent_count + failed_count + :sent + :failed >= recipient_count THEN 'completed'
                        ELSE status
                    END
                WHERE broadcast_id = :broadcast_id
                """
            ),
            {"sent": sent, "failed": failed, "broadcast_id": broadcast_id},
        )
        session.commit()
    engine.dispose()

    logger.info(f"Broadcast {broadcast_id} chunk: {sent} sent, {failed} failed")
