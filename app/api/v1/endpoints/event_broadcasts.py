"""Organizer-composed messages to every ticket-holder of an event. See
EventBroadcastService for the actual logic — this file is just request
wiring. Deliberately not plan-gated, unlike Front Desk."""
from typing import List
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import require_roles
from app.db.tenant import get_tenant_db
from app.models.event import Event
from app.models.event_broadcast import EventBroadcast
from app.models.ticket import Ticket
from app.models.user import User
from app.repositories.event_broadcast_repository import EventBroadcastRepository
from app.repositories.event_repository import EventRepository
from app.repositories.ticket_repository import TicketRepository
from app.schemas.event_broadcast import EventBroadcastCreate, EventBroadcastResponse
from app.services.event_broadcast_service import EventBroadcastService

router = APIRouter(prefix="/events", tags=["Event Broadcasts"])

ORGANIZER_ROLES = ("System Administrator", "Election Administrator", "Event Organizer")


def _get_service(db: AsyncSession) -> EventBroadcastService:
    return EventBroadcastService(
        db=db,
        broadcast_repo=EventBroadcastRepository(EventBroadcast, db),
        event_repo=EventRepository(Event, db),
        ticket_repo=TicketRepository(Ticket, db),
    )


@router.post(
    "/{event_id}/broadcast",
    response_model=EventBroadcastResponse,
    status_code=201,
)
async def send_event_broadcast(
    event_id: UUID,
    data: EventBroadcastCreate,
    current_user: User = Depends(require_roles(*ORGANIZER_ROLES)),
    db: AsyncSession = Depends(get_tenant_db),
):
    return await _get_service(db).send(event_id, current_user, data)


@router.get(
    "/{event_id}/broadcasts",
    response_model=List[EventBroadcastResponse],
)
async def list_event_broadcasts(
    event_id: UUID,
    current_user: User = Depends(require_roles(*ORGANIZER_ROLES)),
    db: AsyncSession = Depends(get_tenant_db),
):
    return await _get_service(db).list_for_event(event_id, current_user)
