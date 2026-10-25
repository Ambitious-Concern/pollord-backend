from datetime import datetime, timedelta, timezone
from typing import List
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.ownership import require_event_ownership
from app.models.event import Event
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
)
from app.services import email_service
from app.services.ticketing_service import TicketingService
from app.tasks.email_tasks import send_email_task


class EventInvitationService:
    """Token-based RSVP invites for an event — no account required to
    respond, since an RSVP doesn't need to bind to a user identity the way
    org membership does."""

    def __init__(
        self,
        db: AsyncSession,
        invite_repo: EventInvitationRepository,
        event_repo: EventRepository,
        ticket_type_repo: TicketTypeRepository,
        ticket_repo: TicketRepository,
        purchase_repo: TicketPurchaseRepository,
        audit_repo: AuditLogRepository,
        user_repo: UserRepository,
    ):
        self.db = db
        self.invite_repo = invite_repo
        self.event_repo = event_repo
        self.ticket_type_repo = ticket_type_repo
        self.ticket_repo = ticket_repo
        self.purchase_repo = purchase_repo
        self.audit_repo = audit_repo
        self.user_repo = user_repo

    async def _get_event_or_404(self, event_id: UUID) -> Event:
        event = await self.event_repo.get_by_id(event_id, id_field="event_id")
        if not event:
            raise HTTPException(status_code=404, detail="Event not found")
        return event

    async def list_for_event(self, event_id: UUID, user: User) -> List[EventInvitationResponse]:
        event = await self._get_event_or_404(event_id)
        await require_event_ownership(event, user, self.db)
        invites = await self.invite_repo.get_by_event(event_id)
        return [EventInvitationResponse.model_validate(inv) for inv in invites]

    async def invite(
        self, event_id: UUID, user: User, data: EventInvitationCreate
    ) -> EventInvitationResponse:
        event = await self._get_event_or_404(event_id)
        await require_event_ownership(event, user, self.db)

        existing = await self.invite_repo.get_pending_invitation(event_id, data.email)
        if existing:
            raise HTTPException(
                status_code=409,
                detail="An invitation has already been sent to that email address",
            )

        # An RSVP link is only useful until the event actually happens.
        event_starts_at = datetime.combine(event.event_date, event.event_time, tzinfo=timezone.utc)
        expires_at = max(event_starts_at, datetime.now(timezone.utc) + timedelta(hours=1))

        invitation = await self.invite_repo.create_invitation(
            event_id=event_id,
            email=data.email,
            name=data.name,
            invited_by=user.user_id,
            expires_at=expires_at,
        )

        rsvp_url = f"{settings.FRONTEND_URL}/invite/rsvp?token={invitation.token}"
        subject, html_body = email_service.event_invite_email(
            guest_name=data.name,
            event_title=event.title,
            event_date=event.event_date.strftime("%B %d, %Y"),
            location=event.location,
            rsvp_url=rsvp_url,
        )
        send_email_task.delay(data.email, subject, html_body)

        return EventInvitationResponse.model_validate(invitation)

    async def get_by_token(self, token: str) -> EventInvitationPublicResponse:
        inv = await self.invite_repo.get_by_token(token)
        if not inv:
            raise HTTPException(status_code=404, detail="Invitation not found")
        if inv.status == "pending" and inv.expires_at < datetime.now(timezone.utc):
            await self.invite_repo.set_status(inv.invitation_id, "expired")
            inv.status = "expired"

        event = inv.event
        return EventInvitationPublicResponse(
            invitation_id=inv.invitation_id,
            email=inv.email,
            name=inv.name,
            status=inv.status,
            expires_at=inv.expires_at,
            event_id=event.event_id,
            event_title=event.title,
            event_date=event.event_date,
            event_time=event.event_time,
            location=event.location,
            banner_image_url=event.banner_image_url,
        )

    async def rsvp(self, token: str, response: str) -> EventInvitationPublicResponse:
        inv = await self.invite_repo.get_by_token(token)
        if not inv:
            raise HTTPException(status_code=404, detail="Invitation not found")
        if inv.status != "pending":
            raise HTTPException(status_code=410, detail="This invitation has already been responded to or has expired")
        if inv.expires_at < datetime.now(timezone.utc):
            await self.invite_repo.set_status(inv.invitation_id, "expired")
            raise HTTPException(status_code=410, detail="This invitation has expired")

        await self.invite_repo.set_status(inv.invitation_id, response)
        inv.status = response

        # Accepting reserves a real spot — a free ticket under the event's
        # "Free Registration" type, same as a Front Desk walk-up — so the
        # guest shows up in Attendees/check-in and (for Phase 3b) in
        # ticket-holder broadcasts, instead of the RSVP being a dead-end
        # yes/no with no effect on the door.
        if response == "accepted" and inv.invited_by is not None:
            ticketing = TicketingService(
                event_repo=self.event_repo,
                ticket_type_repo=self.ticket_type_repo,
                ticket_repo=self.ticket_repo,
                purchase_repo=self.purchase_repo,
                audit_repo=self.audit_repo,
                user_repo=self.user_repo,
            )
            await ticketing.front_desk_register(
                event=inv.event,
                name=inv.name or inv.email,
                phone="",
                staff_user_id=inv.invited_by,
                origin="rsvp_invite",
                email=inv.email,
                send_confirmation_email=True,
            )

        return await self.get_by_token(token)
