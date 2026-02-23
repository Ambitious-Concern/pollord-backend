import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task
def check_expired_tickets():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.core.config import settings
    from app.models.event import Event
    from app.models.ticket import Ticket

    sync_url = settings.DATABASE_URL.replace("+asyncpg", "")
    engine = create_engine(sync_url)

    with Session(engine) as session:
        today = datetime.now(timezone.utc).date()
        # Find tickets for past events that are still valid
        result = session.execute(
            select(Ticket)
            .join(Event, Ticket.event_id == Event.event_id)
            .where(
                Ticket.ticket_status == "valid",
                Event.event_date < today,
            )
        )
        tickets = result.scalars().all()

        for ticket in tickets:
            ticket.ticket_status = "expired"

        session.commit()
        if tickets:
            logger.info(f"Expired {len(tickets)} ticket(s) for past events")

    engine.dispose()
