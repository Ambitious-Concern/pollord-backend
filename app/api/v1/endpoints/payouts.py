from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.dependencies import get_current_active_user, require_roles
from app.db.base import get_db
from app.models.election import Election
from app.models.event import Event
from app.models.payout_request import PayoutRequest
from app.models.ticket import TicketPurchase
from app.models.user import User
from app.repositories.election_repository import ElectionRepository
from app.repositories.event_repository import EventRepository
from app.repositories.payout_request_repository import PayoutRequestRepository
from app.repositories.ticket_repository import TicketPurchaseRepository
from app.repositories.transaction_repository import TransactionRepository
from app.repositories.user_repository import UserRepository
from app.schemas.payout import (
    MobileMoneyNetwork,
    PayoutAvailableResponse,
    PayoutRequestCreate,
    PayoutRequestResponse,
    PayoutReviewRequest,
)
from app.services.payout_service import PayoutService
from app.services.paystack_service import PaystackService

router = APIRouter(prefix="/payouts", tags=["Payouts"])

ORGANIZER_ROLES = ("System Administrator", "Event Organizer")
ELECTION_ROLES = ("System Administrator", "Election Administrator")
ADMIN_ROLE = "System Administrator"


def _get_service(db: AsyncSession) -> PayoutService:
    return PayoutService(
        payout_repo=PayoutRequestRepository(PayoutRequest, db),
        event_repo=EventRepository(Event, db),
        purchase_repo=TicketPurchaseRepository(TicketPurchase, db),
        user_repo=UserRepository(User, db),
        election_repo=ElectionRepository(Election, db),
        transaction_repo=TransactionRepository(db),
        paystack=PaystackService(settings.PAYSTACK_SECRET_KEY),
    )


@router.get("/events/{event_id}/available", response_model=PayoutAvailableResponse)
async def get_available_payout(
    event_id: UUID,
    current_user: User = Depends(require_roles(*ORGANIZER_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """How much revenue for this event hasn't been requested (or paid) yet."""
    return await _get_service(db).get_available(event_id, current_user)


@router.get("/mobile-money-networks", response_model=List[MobileMoneyNetwork])
async def list_mobile_money_networks(
    current_user: User = Depends(
        require_roles("System Administrator", "Event Organizer", "Election Administrator")
    ),
    db: AsyncSession = Depends(get_db),
):
    """Paystack's own list of supported GHS mobile money networks + their
    codes, for the payout-destination dropdown."""
    return await _get_service(db).list_mobile_money_networks()


@router.post("/events/{event_id}", response_model=PayoutRequestResponse, status_code=201)
async def request_payout(
    event_id: UUID,
    data: PayoutRequestCreate,
    current_user: User = Depends(require_roles(*ORGANIZER_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """Request payout of an event's outstanding revenue, to the given mobile
    money destination. An admin can then pay this out via Paystack Transfer
    from the admin console, or pay outside the app and mark it paid."""
    return await _get_service(db).request_payout(event_id, current_user, data)


@router.get("/events/{event_id}", response_model=List[PayoutRequestResponse])
async def list_event_payout_requests(
    event_id: UUID,
    current_user: User = Depends(require_roles(*ORGANIZER_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    return await _get_service(db).list_for_event(event_id, current_user)


@router.get("/elections/{election_id}/available", response_model=PayoutAvailableResponse)
async def get_available_election_payout(
    election_id: UUID,
    current_user: User = Depends(require_roles(*ELECTION_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """How much revenue for this election's paid votes hasn't been requested
    (or paid) yet."""
    return await _get_service(db).get_available_for_election(election_id, current_user)


@router.post("/elections/{election_id}", response_model=PayoutRequestResponse, status_code=201)
async def request_election_payout(
    election_id: UUID,
    data: PayoutRequestCreate,
    current_user: User = Depends(require_roles(*ELECTION_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """Request payout of an election's outstanding paid-vote revenue, to the
    given mobile money destination. Mirrors the event payout flow."""
    return await _get_service(db).request_payout_for_election(election_id, current_user, data)


@router.get("/elections/{election_id}", response_model=List[PayoutRequestResponse])
async def list_election_payout_requests(
    election_id: UUID,
    current_user: User = Depends(require_roles(*ELECTION_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    return await _get_service(db).list_for_election(election_id, current_user)


@router.get("/mine", response_model=List[PayoutRequestResponse])
async def list_my_payout_requests(
    current_user: User = Depends(
        require_roles("System Administrator", "Event Organizer", "Election Administrator")
    ),
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


@router.post("/admin/{payout_request_id}/pay", response_model=PayoutRequestResponse)
async def pay_via_paystack(
    payout_request_id: UUID,
    current_user: User = Depends(require_roles(ADMIN_ROLE)),
    db: AsyncSession = Depends(get_db),
):
    """Pays a pending request out via Paystack Transfer, to the mobile money
    destination the organizer provided when requesting it. Moves real money —
    check `transfer_status` in the response: "success" means it's done and
    `status` is now "paid"; "pending"/"otp" means Paystack queued it or needs
    OTP finalization on Paystack's own dashboard and the request stays
    pending; anything else means it failed and can be retried or paid
    manually instead."""
    return await _get_service(db).initiate_transfer(payout_request_id, current_user)
