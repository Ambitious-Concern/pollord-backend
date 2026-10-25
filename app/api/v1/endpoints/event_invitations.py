"""Organizer-sent RSVP invites for an event, plus the public token-based
lookup/RSVP flow. See EventInvitationService for the actual logic — this
file is just request wiring."""
from typing import List
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import require_roles
from app.db.base import get_db
from app.db.tenant import get_tenant_db
from app.models.audit_log import AuditLog
from app.models.event import Event, TicketType
from app.models.event_invitation import EventInvitation
from app.models.ticket import Ticket, TicketPurchase
from app.models.user import User
from app.repositories.audit_log_repository import AuditLogRepository
from app.repositories.event_invitation_repository import EventInvitationRepository
from app.repositories.event_repository import EventRepository, TicketTypeRepository
from app.repositories.ticket_repository import TicketPurchaseRepository, TicketRepository
from app.repositories.user_repository import UserRepository
from app.schemas.event_invitation import (
    EventInvitationCreate,
    EventInvitationPublicResponse,
    EventInvitationResponse,
    EventRSVPRequest,
)
from app.services.event_invitation_service import EventInvitationService

router = APIRouter(tags=["Event Invitations"])

ORGANIZER_ROLES = ("System Administrator", "Election Administrator", "Event Organizer")


def _get_service(db: AsyncSession) -> EventInvitationService:
    return EventInvitationService(
        db=db,
        invite_repo=EventInvitationRepository(EventInvitation, db),
        event_repo=EventRepository(Event, db),
        ticket_type_repo=TicketTypeRepository(TicketType, db),
        ticket_repo=TicketRepository(Ticket, db),
        purchase_repo=TicketPurchaseRepository(TicketPurchase, db),
        audit_repo=AuditLogRepository(AuditLog, db),
        user_repo=UserRepository(User, db),
    )


@router.post(
    "/events/{event_id}/invitations",
    response_model=EventInvitationResponse,
    status_code=201,
)
async def create_event_invitation(
    event_id: UUID,
    data: EventInvitationCreate,
    current_user: User = Depends(require_roles(*ORGANIZER_ROLES)),
    db: AsyncSession = Depends(get_tenant_db),
):
    return await _get_service(db).invite(event_id, current_user, data)


@router.get(
    "/events/{event_id}/invitations",
    response_model=List[EventInvitationResponse],
)
async def list_event_invitations(
    event_id: UUID,
    current_user: User = Depends(require_roles(*ORGANIZER_ROLES)),
    db: AsyncSession = Depends(get_tenant_db),
):
    return await _get_service(db).list_for_event(event_id, current_user)


@router.get("/invitations/{token}", response_model=EventInvitationPublicResponse)
async def get_event_invitation(token: str, db: AsyncSession = Depends(get_db)):
    return await _get_service(db).get_by_token(token)


@router.post("/invitations/{token}/rsvp", response_model=EventInvitationPublicResponse)
async def rsvp_event_invitation(token: str, data: EventRSVPRequest, db: AsyncSession = Depends(get_db)):
    return await _get_service(db).rsvp(token, data.response)
