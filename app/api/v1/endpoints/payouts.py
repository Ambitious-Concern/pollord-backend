from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_current_active_user, require_roles
from app.db.base import get_db
from app.models.event import Event
from app.models.payout_request import PayoutRequest
from app.models.ticket import TicketPurchase
from app.models.user import User
from app.repositories.event_repository import EventRepository
from app.repositories.payout_request_repository import PayoutRequestRepository
from app.repositories.ticket_repository import TicketPurchaseRepository
from app.repositories.user_repository import UserRepository
from app.schemas.payout import PayoutAvailableResponse, PayoutRequestResponse, PayoutReviewRequest
from app.services.payout_service import PayoutService

router = APIRouter(prefix="/payouts", tags=["Payouts"])

ORGANIZER_ROLES = ("System Administrator", "Event Organizer")
ADMIN_ROLE = "System Administrator"


def _get_service(db: AsyncSession) -> PayoutService:
    return PayoutService(
        payout_repo=PayoutRequestRepository(PayoutRequest, db),
        event_repo=EventRepository(Event, db),
        purchase_repo=TicketPurchaseRepository(TicketPurchase, db),
        user_repo=UserRepository(User, db),
    )


@router.get("/events/{event_id}/available", response_model=PayoutAvailableResponse)
async def get_available_payout(
    event_id: UUID,
    current_user: User = Depends(require_roles(*ORGANIZER_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """How much revenue for this event hasn't been requested (or paid) yet."""
    return await _get_service(db).get_available(event_id, current_user)


@router.post("/events/{event_id}", response_model=PayoutRequestResponse, status_code=201)
async def request_payout(
    event_id: UUID,
    current_user: User = Depends(require_roles(*ORGANIZER_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """Request payout of an event's outstanding revenue. This does not move
    money — it creates a request for a platform admin to review and pay out
    manually, then mark as paid."""
    return await _get_service(db).request_payout(event_id, current_user)


@router.get("/events/{event_id}", response_model=List[PayoutRequestResponse])
async def list_event_payout_requests(
    event_id: UUID,
    current_user: User = Depends(require_roles(*ORGANIZER_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    return await _get_service(db).list_for_event(event_id, current_user)


@router.get("/mine", response_model=List[PayoutRequestResponse])
async def list_my_payout_requests(
    current_user: User = Depends(require_roles(*ORGANIZER_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    return await _get_service(db).list_mine(current_user.user_id)


@router.get("/admin/all", response_model=List[PayoutRequestResponse])
async def list_all_payout_requests(
    status: Optional[str] = None,
    current_user: User = Depends(require_roles(ADMIN_ROLE)),
    db: AsyncSession = Depends(get_db),
):
    """Every payout request on the platform, for a System Administrator to review."""
    return await _get_service(db).list_all(status)


@router.put("/admin/{payout_request_id}", response_model=PayoutRequestResponse)
async def review_payout_request(
    payout_request_id: UUID,
    data: PayoutReviewRequest,
    current_user: User = Depends(require_roles(ADMIN_ROLE)),
    db: AsyncSession = Depends(get_db),
):
    """Mark a payout request as paid (after paying the organizer manually
    outside the app) or rejected."""
    return await _get_service(db).review(
        payout_request_id, data.status, data.admin_notes, current_user
    )
