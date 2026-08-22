"""Platform-admin ticket support endpoints.

Kept out of admin.py, which is already long, and separate from tickets.py,
whose organizer routes are all scoped to `Event.created_by`. These are
deliberately unscoped: a platform admin handling "I paid and never got my
ticket" needs to reach any purchase on the platform.
"""
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import require_roles
from app.db.base import get_db
from app.models.audit_log import AuditLog
from app.models.event import Event, TicketType
from app.models.ticket import Ticket, TicketPurchase
from app.models.user import User
from app.repositories.audit_log_repository import AuditLogRepository
from app.repositories.event_repository import EventRepository, TicketTypeRepository
from app.repositories.ticket_repository import TicketPurchaseRepository, TicketRepository
from app.repositories.user_repository import UserRepository
from app.schemas.ticket import (
    AdminTicketPurchaseListResponse,
    ResendTicketEmailRequest,
    ResendTicketEmailResponse,
)
from app.services.ticketing_service import TicketingService

router = APIRouter(prefix="/admin", tags=["Admin"])

ADMIN_ROLE = "System Administrator"


def _ticketing_service(db: AsyncSession) -> TicketingService:
    return TicketingService(
        event_repo=EventRepository(Event, db),
        ticket_type_repo=TicketTypeRepository(TicketType, db),
        ticket_repo=TicketRepository(Ticket, db),
        purchase_repo=TicketPurchaseRepository(TicketPurchase, db),
        audit_repo=AuditLogRepository(AuditLog, db),
        user_repo=UserRepository(User, db),
    )


@router.get("/ticket-purchases", response_model=AdminTicketPurchaseListResponse)
async def list_ticket_purchases(
    current_user: User = Depends(require_roles(ADMIN_ROLE)),
    db: AsyncSession = Depends(get_db),
    skip: int = 0,
    limit: int = 50,
    search: Optional[str] = None,
    event_id: Optional[UUID] = None,
    payment_status: Optional[str] = None,
    email_status: Optional[str] = None,
):
    """Every ticket purchase on the platform, newest first.

    `search` matches buyer email, buyer name, or Paystack reference.
    `email_status` is one of sent / failed / unknown.
    """
    return await _ticketing_service(db).list_purchases_for_admin(
        search=search,
        event_id=event_id,
        payment_status=payment_status,
        email_status=email_status,
        skip=skip,
        limit=limit,
    )


@router.post(
    "/ticket-purchases/{purchase_id}/resend-email",
    response_model=ResendTicketEmailResponse,
)
async def resend_ticket_email(
    purchase_id: UUID,
    data: ResendTicketEmailRequest,
    request: Request,
    current_user: User = Depends(require_roles(ADMIN_ROLE)),
    db: AsyncSession = Depends(get_db),
):
    """Re-send a purchase's ticket email, optionally to a corrected address.

    Returns whether the send actually succeeded rather than just accepting
    the request — the admin is usually on the phone to the buyer.
    """
    return await _ticketing_service(db).resend_purchase_confirmation(
        purchase_id=purchase_id,
        actor_user_id=current_user.user_id,
        override_email=data.email,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
