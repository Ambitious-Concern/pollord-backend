import logging
from datetime import datetime, timezone

from sqlalchemy import select, update

from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task
def check_election_status():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.core.config import settings
    from app.models.election import Election

    # Use sync engine for Celery tasks
    sync_url = settings.DATABASE_URL.replace("+asyncpg", "")
    engine = create_engine(sync_url)

    with Session(engine) as session:
        now = datetime.now(timezone.utc)
        result = session.execute(
            select(Election).where(
                Election.status == "active",
                Election.end_datetime < now,
            )
        )
        elections = result.scalars().all()

        for election in elections:
            election.status = "completed"
            logger.info(
                f"Election '{election.title}' (ID: {election.election_id}) "
                f"automatically closed"
            )

        session.commit()
        if elections:
            logger.info(f"Closed {len(elections)} expired election(s)")

    engine.dispose()
