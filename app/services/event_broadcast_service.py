from typing import List
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ownership import require_event_ownership
from app.models.event import Event
from app.models.user import User
from app.repositories.event_broadcast_repository import EventBroadcastRepository
from app.repositories.event_repository import EventRepository
from app.repositories.ticket_repository import TicketRepository
from app.schemas.event_broadcast import EventBroadcastCreate, EventBroadcastResponse
from app.services import email_service
from app.tasks.event_broadcast_tasks import send_broadcast_chunk

CHUNK_SIZE = 50


class EventBroadcastService:
    """Organizer-composed messages to every ticket-holder of an event —
    not plan-gated (unlike Front Desk), open to every organizer regardless
    of plan. See TicketRepository.get_recipient_emails for how the
    recipient list is resolved."""

    def __init__(
        self,
        db: AsyncSession,
        broadcast_repo: EventBroadcastRepository,
        event_repo: EventRepository,
        ticket_repo: TicketRepository,
    ):
        self.db = db
        self.broadcast_repo = broadcast_repo
        self.event_repo = event_repo
        self.ticket_repo = ticket_repo

    async def _get_event_or_404(self, event_id: UUID) -> Event:
        event = await self.event_repo.get_by_id(event_id, id_field="event_id")
        if not event:
            raise HTTPException(status_code=404, detail="Event not found")
        return event

    async def list_for_event(self, event_id: UUID, user: User) -> List[EventBroadcastResponse]:
        event = await self._get_event_or_404(event_id)
        await require_event_ownership(event, user, self.db)
        rows = await self.broadcast_repo.get_by_event(event_id)
        return [EventBroadcastResponse.model_validate(r) for r in rows]

    async def send(self, event_id: UUID, user: User, data: EventBroadcastCreate) -> EventBroadcastResponse:
        event = await self._get_event_or_404(event_id)
        await require_event_ownership(event, user, self.db)

        recipients = await self.ticket_repo.get_recipient_emails(event_id)
        if not recipients:
            raise HTTPException(
                status_code=400, detail="This event has no ticket-holders to message yet"
            )

        broadcast = await self.broadcast_repo.create(
            {
                "event_id": event_id,
                "sent_by": user.user_id,
                "subject": data.subject,
                "body": data.body,
                "recipient_count": len(recipients),
                "status": "sending",
            }
        )

        subject, html_body = email_service.event_broadcast_email(event.title, data.subject, data.body)
        for i in range(0, len(recipients), CHUNK_SIZE):
            chunk = recipients[i : i + CHUNK_SIZE]
            send_broadcast_chunk.delay(str(broadcast.broadcast_id), chunk, subject, html_body)

        return EventBroadcastResponse.model_validate(broadcast)
