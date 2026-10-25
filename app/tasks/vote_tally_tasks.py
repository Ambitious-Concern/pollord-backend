import logging
from datetime import datetime, timezone

from sqlalchemy import select, update

from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


def _notify_org_members(session, org_id, pref_key, title, description, link):
    """Same fan-out as OrgNotificationService.notify (async), just via a
    plain sync session — beat jobs don't have an event loop to await one
    from."""
    from app.core.notification_prefs import wants_email
    from app.models.notification import Notification
    from app.models.organization import OrganizationMember
    from app.models.user import User
    from app.services.email_service import org_notification_email
    from app.tasks.email_tasks import send_email_task

    member_ids = [
        row[0] for row in session.execute(
            select(OrganizationMember.user_id).where(OrganizationMember.org_id == org_id)
        ).all()
    ]
    if not member_ids:
        return

    session.add_all([
        Notification(user_id=member_id, org_id=org_id, type="vote", title=title,
                      description=description, link=link)
        for member_id in member_ids
    ])
    subject, html = org_notification_email(title, description, link)
    members = session.execute(select(User).where(User.user_id.in_(member_ids))).scalars().all()
    for member in members:
        if wants_email(member.notification_prefs, pref_key):
            send_email_task.delay(member.email, subject, html)


@celery_app.task
def check_election_status():
    """Celery beat job (every 60s): auto-close any active election whose
    end_datetime has passed. Without this, elections never expire on
    their own — someone would have to notice and close them by hand."""
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
            if election.org_id is not None:
                _notify_org_members(
                    session, election.org_id, "election_ended",
                    title=f"{election.title} has ended",
                    description="Voting has closed and results are ready.",
                    link=f"/dashboard/elections?highlight={election.election_id}",
                )

        session.commit()
        if elections:
            logger.info(f"Closed {len(elections)} expired election(s)")

    engine.dispose()


@celery_app.task
def activate_scheduled_elections():
    """Celery beat job (every 60s): auto-activate any scheduled election
    whose start_datetime has arrived. Schedule only records intent at
    creation time — this is what actually acts on it, mirroring
    check_election_status on the other end of the lifecycle.

    An election that reaches its start time without at least one category
    and every category having a candidate is skipped (not force-activated)
    and the organizer is notified to fix it and activate manually — same
    validation /publish already enforces for a manual click."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.core.config import settings
    from app.models.election import Category, Election

    sync_url = settings.DATABASE_URL.replace("+asyncpg", "")
    engine = create_engine(sync_url)

    with Session(engine) as session:
        now = datetime.now(timezone.utc)
        result = session.execute(
            select(Election).where(
                Election.status == "scheduled",
                Election.start_datetime <= now,
            )
        )
        elections = result.scalars().all()

        activated = 0
        for election in elections:
            if election.end_datetime < now:
                continue  # its window already closed before it ever started — leave it for a human to sort out

            categories = session.execute(
                select(Category).where(Category.election_id == election.election_id)
            ).scalars().all()
            ready = bool(categories) and all(category.candidates for category in categories)

            if not ready:
                logger.warning(
                    f"Election '{election.title}' (ID: {election.election_id}) reached its "
                    f"scheduled start but is missing categories/candidates — skipped"
                )
                if election.org_id is not None:
                    _notify_org_members(
                        session, election.org_id, "election_started",
                        title=f"{election.title} couldn't start on schedule",
                        description="It's missing categories or candidates — add them and activate it manually.",
                        link=f"/dashboard/elections?highlight={election.election_id}",
                    )
                continue

            election.status = "active"
            activated += 1
            logger.info(
                f"Election '{election.title}' (ID: {election.election_id}) auto-activated on schedule"
            )
            if election.org_id is not None:
                _notify_org_members(
                    session, election.org_id, "election_started",
                    title=f"{election.title} is now live",
                    description="Voting has started.",
                    link=f"/dashboard/elections?highlight={election.election_id}",
                )

        session.commit()
        if activated:
            logger.info(f"Auto-activated {activated} scheduled election(s)")

    engine.dispose()
