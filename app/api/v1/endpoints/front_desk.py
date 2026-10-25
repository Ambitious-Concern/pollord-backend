"""Front Desk: staff-issued walk-up tickets (paid door sale or free
registration) that bypass the normal Paystack checkout entirely — open to
every plan. Still routed through require_event_plan (at the lowest tier)
rather than a bare ownership check, so the 404-if-missing/403-if-not-owned
behavior stays identical to every other plan-gated endpoint. See
TicketingService.front_desk_sell/front_desk_register for the actual logic
(including org activity log writes)."""
from typing import List

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import require_event_plan, require_roles
from app.core.plans import PLAN_STANDARD
from app.core.request_meta import client_ip_and_agent
from app.db.tenant import get_tenant_db
from app.models.audit_log import AuditLog
from app.models.event import Event, TicketType
from app.models.ticket import Ticket, TicketPurchase
from app.models.user import User
from app.repositories.audit_log_repository import AuditLogRepository
from app.repositories.event_repository import EventRepository, TicketTypeRepository
from app.repositories.ticket_repository import TicketPurchaseRepository, TicketRepository
from app.repositories.user_repository import UserRepository
from app.schemas.ticket import FrontDeskRegistrationRequest, FrontDeskSaleRequest, TicketResponse
from app.services.ticketing_service import TicketingService

router = APIRouter(prefix="/events", tags=["Front Desk"])

ORGANIZER_ROLES = ("System Administrator", "Event Organizer")


def _get_service(db: AsyncSession) -> TicketingService:
    return TicketingService(
        event_repo=EventRepository(Event, db),
        ticket_type_repo=TicketTypeRepository(TicketType, db),
        ticket_repo=TicketRepository(Ticket, db),
        purchase_repo=TicketPurchaseRepository(TicketPurchase, db),
        audit_repo=AuditLogRepository(AuditLog, db),
        user_repo=UserRepository(User, db),
    )


@router.post(
    "/{event_id}/front-desk/sell",
    response_model=List[TicketResponse],
    status_code=201,
)
async def front_desk_sell(
    request: Request,
    data: FrontDeskSaleRequest,
    event: Event = Depends(require_event_plan(PLAN_STANDARD)),
    current_user: User = Depends(require_roles(*ORGANIZER_ROLES)),
    db: AsyncSession = Depends(get_tenant_db),
):
    """Walk-up paid door sale, auto-checked-in on the spot."""
    ip, agent = client_ip_and_agent(request)
    return await _get_service(db).front_desk_sell(
        event=event,
        ticket_type_id=data.ticket_type_id,
        quantity=data.quantity,
        name=data.name,
        phone=data.phone,
        payment_method=data.payment_method,
        staff_user_id=current_user.user_id,
        origin=data.origin,
        ip=ip,
        user_agent=agent,
    )


@router.post(
    "/{event_id}/front-desk/register",
    response_model=TicketResponse,
    status_code=201,
)
async def front_desk_register(
    request: Request,
    data: FrontDeskRegistrationRequest,
    event: Event = Depends(require_event_plan(PLAN_STANDARD)),
    current_user: User = Depends(require_roles(*ORGANIZER_ROLES)),
    db: AsyncSession = Depends(get_tenant_db),
):
    """Walk-up free registration — generates a QR the attendee shows at the
    actual door to be scanned in; not auto-checked-in."""
    ip, agent = client_ip_and_agent(request)
    return await _get_service(db).front_desk_register(
        event=event,
        name=data.name,
        phone=data.phone,
        staff_user_id=current_user.user_id,
        origin=data.origin,
        ip=ip,
        user_agent=agent,
    )
