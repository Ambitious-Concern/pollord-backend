from typing import List, Optional
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.notification_prefs import wants_email
from app.core.redis import get_redis
from app.models.election import Election
from app.models.event import Event
from app.models.notification import Notification
from app.models.organization import Organization
from app.models.user import User
from app.repositories.election_repository import ElectionRepository
from app.repositories.event_repository import EventRepository
from app.repositories.notification_repository import NotificationRepository
from app.repositories.organization_repository import OrganizationRepository
from app.repositories.user_repository import UserRepository
from app.services import email_service
from app.tasks.email_tasks import send_email_task

# A busy election/event can produce a vote or a ticket sale every few
# seconds — without a cooldown, "vote cast"/"ticket purchased" would mean
# one email (and one bell entry) per transaction. This caps it to at most
# one notification per parent per window; see OrgNotificationService.notify.
_HIGH_FREQUENCY_COOLDOWN_SECONDS = 300


class OrgNotificationService:
    """Fans an event out to every member of an org: an in-app bell entry for
    all of them (unchanged from NotificationRepository.notify_org's original
    behavior), plus an email for whichever members have that event's
    Settings > Notifications preference turned on.

    `cooldown_key`/`cooldown_seconds` exist for high-frequency events (a
    vote landing, a ticket selling) where a busy election/event would
    otherwise fire this once per transaction — when a cooldown is active,
    the whole notify (bell + email) is skipped, not just the email, since a
    bell entry per vote is just as useless as an email per vote. Low
    frequency lifecycle events (payouts, election/event publish, team
    changes) pass no cooldown and always fire.
    """

    def __init__(self, db: AsyncSession):
        self.db = db

    async def notify(
        self,
        *,
        org_id: UUID,
        member_ids: List[UUID],
        pref_key: str,
        notif_type: str,
        title: str,
        description: str,
        link: Optional[str] = None,
        cooldown_key: Optional[str] = None,
        cooldown_seconds: Optional[int] = None,
    ) -> None:
        if not member_ids:
            return

        if cooldown_key and cooldown_seconds:
            redis = await get_redis()
            # NX: only the first caller within the window wins the "send"
            # decision, so concurrent votes on the same election don't all
            # slip through before the key is set.
            acquired = await redis.set(cooldown_key, "1", ex=cooldown_seconds, nx=True)
            if not acquired:
                return

        await NotificationRepository(Notification, self.db).notify_org(
            org_id=org_id, member_ids=member_ids, type=notif_type,
            title=title, description=description, link=link,
        )

        user_repo = UserRepository(User, self.db)
        subject, html = email_service.org_notification_email(title, description, link)
        for member_id in member_ids:
            member = await user_repo.get_by_id(member_id, id_field="user_id")
            if member and wants_email(member.notification_prefs, pref_key):
                send_email_task.delay(member.email, subject, html)


async def notify_vote_cast_if_due(
    db: AsyncSession, election_id: Optional[UUID], event_id: Optional[UUID]
) -> None:
    """Org-facing "votes are coming in" notification, cooldown-gated. Call
    alongside (not instead of) notify_vote_cast — that one wakes SSE
    streams unconditionally on every vote; this one tells the organizer at
    most once per cooldown window."""
    if election_id:
        election = await ElectionRepository(Election, db).get_by_id(election_id, id_field="election_id")
        if not election or election.org_id is None:
            return
        org_id, title, path = election.org_id, election.title, f"/dashboard/elections?highlight={election_id}"
        cooldown_key = f"notif_cooldown:vote_cast:election:{election_id}"
    elif event_id:
        event = await EventRepository(Event, db).get_by_id(event_id, id_field="event_id")
        if not event or event.org_id is None:
            return
        org_id, title, path = event.org_id, event.title, f"/dashboard/events?highlight={event_id}"
        cooldown_key = f"notif_cooldown:vote_cast:event:{event_id}"
    else:
        return

    member_ids = await OrganizationRepository(Organization, db).get_member_ids(org_id)
    await OrgNotificationService(db).notify(
        org_id=org_id,
        member_ids=member_ids,
        pref_key="vote_cast",
        notif_type="vote",
        title=f"New votes in {title}",
        description="Votes are coming in.",
        link=path,
        cooldown_key=cooldown_key,
        cooldown_seconds=_HIGH_FREQUENCY_COOLDOWN_SECONDS,
    )


async def notify_ticket_purchased_if_due(db: AsyncSession, event: Event) -> None:
    """Org-facing "tickets are selling" notification, cooldown-gated — the
    ticketing-service counterpart to notify_vote_cast_if_due. Called from
    the same two call sites already proven correct for the low-stock
    check (_maybe_notify_low_stock), right after a real stock decrement."""
    if event.org_id is None:
        return
    member_ids = await OrganizationRepository(Organization, db).get_member_ids(event.org_id)
    await OrgNotificationService(db).notify(
        org_id=event.org_id,
        member_ids=member_ids,
        pref_key="ticket_purchased",
        notif_type="ticket",
        title=f"Tickets selling for {event.title}",
        description="New ticket purchases are coming in.",
        link="/dashboard/events/attendees",
        cooldown_key=f"notif_cooldown:ticket_purchased:event:{event.event_id}",
        cooldown_seconds=_HIGH_FREQUENCY_COOLDOWN_SECONDS,
    )
