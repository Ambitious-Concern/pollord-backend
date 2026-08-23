"""Platform-admin drill-in detail for a single event or election.

The org analytics endpoint returns only summary rows per event/election.
These two endpoints back the detail pages you reach by clicking one of those
rows: the full record, its ticket types / candidates, and the money behind it.

Read-only. Response models are declared inline, matching admin.py.
"""
from datetime import date, datetime, time
from decimal import Decimal
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func as sqlfunc
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.endpoints.admin import ADMIN_ROLE, _get_platform_setting
from app.core.dependencies import require_roles
from app.db.base import get_db
from app.models.audit_log import AuditLog
from app.models.election import Candidate, Election
from app.models.event import Event, TicketType
from app.models.ticket import Ticket, TicketPurchase
from app.models.user import User
from app.models.vote import Vote, VoteReceipt
from app.repositories.audit_log_repository import AuditLogRepository
from app.repositories.election_repository import CandidateRepository, ElectionRepository
from app.repositories.vote_repository import VoteReceiptRepository, VoteRepository
from app.services.cryptography_service import CryptographyService
from app.services.voting_service import VotingService

router = APIRouter(prefix="/admin", tags=["Admin"])


# ── Event detail ─────────────────────────────────────────────────────────────


class AdminTicketTypeBreakdown(BaseModel):
    ticket_type_id: UUID
    type_name: str
    description: Optional[str] = None
    price: Decimal
    quantity_available: int
    quantity_sold: int
    status: str
    sales_start_datetime: Optional[datetime] = None
    sales_end_datetime: Optional[datetime] = None
    max_per_user: int
    # Actual non-cancelled Ticket rows of this type. Can drift from
    # quantity_sold, which tracks stock rather than issuance.
    tickets_issued: int
    tickets_used: int
    revenue: Decimal


class AdminEventDetailResponse(BaseModel):
    event_id: UUID
    title: str
    description: Optional[str] = None
    event_date: date
    event_time: time
    location: str
    category: Optional[str] = None
    capacity: Optional[int] = None
    banner_image_url: Optional[str] = None
    status: str
    created_at: datetime
    updated_at: Optional[datetime] = None

    organizer_id: UUID
    organizer_name: str = ""
    organizer_email: str = ""

    total_tickets_issued: int
    total_tickets_used: int
    total_purchases: int
    # Authoritative money figure: completed purchases, matching how the org
    # analytics endpoint reports revenue.
    total_revenue: Decimal
    # Purchases whose ticket email failed or was never recorded — the number
    # worth acting on from this page.
    purchases_email_failed: int
    purchases_email_unknown: int

    ticket_types: List[AdminTicketTypeBreakdown]


@router.get("/events/{event_id}", response_model=AdminEventDetailResponse)
async def get_event_detail(
    event_id: UUID,
    current_user: User = Depends(require_roles(ADMIN_ROLE)),
    db: AsyncSession = Depends(get_db),
):
    """Everything about one event: its record, per-ticket-type sales, and
    the state of its ticket-email delivery."""
    result = await db.execute(select(Event).where(Event.event_id == event_id))
    event = result.scalar_one_or_none()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    organizer = await db.get(User, event.created_by)

    # Issued/used counts per ticket type, in one grouped pass rather than a
    # query per type.
    issued_rows = await db.execute(
        select(
            Ticket.ticket_type_id,
            sqlfunc.count(Ticket.ticket_id),
            sqlfunc.count(Ticket.ticket_id).filter(Ticket.ticket_status == "used"),
        )
        .where(Ticket.event_id == event_id, Ticket.ticket_status != "cancelled")
        .group_by(Ticket.ticket_type_id)
    )
    issued_by_type: dict[UUID, tuple[int, int]] = {
        row[0]: (int(row[1]), int(row[2])) for row in issued_rows.all()
    }

    types_result = await db.execute(
        select(TicketType)
        .where(TicketType.event_id == event_id)
        .order_by(TicketType.price.asc())
    )
    ticket_types = []
    for tt in types_result.scalars().all():
        issued, used = issued_by_type.get(tt.ticket_type_id, (0, 0))
        ticket_types.append(
            AdminTicketTypeBreakdown(
                ticket_type_id=tt.ticket_type_id,
                type_name=tt.type_name,
                description=tt.description,
                price=tt.price,
                quantity_available=tt.quantity_available,
                quantity_sold=tt.quantity_sold,
                status=tt.status,
                sales_start_datetime=tt.sales_start_datetime,
                sales_end_datetime=tt.sales_end_datetime,
                max_per_user=tt.max_per_user,
                tickets_issued=issued,
                tickets_used=used,
                revenue=Decimal(str(tt.price)) * issued,
            )
        )

    purchase_row = await db.execute(
        select(
            sqlfunc.count(TicketPurchase.purchase_id),
            sqlfunc.coalesce(sqlfunc.sum(TicketPurchase.total_amount), 0),
            sqlfunc.count(TicketPurchase.purchase_id).filter(
                TicketPurchase.confirmation_email_status == "failed"
            ),
            sqlfunc.count(TicketPurchase.purchase_id).filter(
                TicketPurchase.confirmation_email_status.is_(None)
            ),
        ).where(
            TicketPurchase.event_id == event_id,
            TicketPurchase.payment_status == "completed",
        )
    )
    purchases, revenue, email_failed, email_unknown = purchase_row.one()

    return AdminEventDetailResponse(
        event_id=event.event_id,
        title=event.title,
        description=event.description,
        event_date=event.event_date,
        event_time=event.event_time,
        location=event.location,
        category=event.category,
        capacity=event.capacity,
        banner_image_url=event.banner_image_url,
        status=event.status,
        created_at=event.created_at,
        updated_at=event.updated_at,
        organizer_id=event.created_by,
        organizer_name=organizer.full_name if organizer else "",
        organizer_email=organizer.email if organizer else "",
        total_tickets_issued=sum(t.tickets_issued for t in ticket_types),
        total_tickets_used=sum(t.tickets_used for t in ticket_types),
        total_purchases=int(purchases),
        total_revenue=Decimal(str(revenue)),
        purchases_email_failed=int(email_failed),
        purchases_email_unknown=int(email_unknown),
        ticket_types=ticket_types,
    )


# ── Election detail ──────────────────────────────────────────────────────────


class AdminCandidateResult(BaseModel):
    candidate_id: UUID
    name: str
    short_code: Optional[str] = None
    description: Optional[str] = None
    image_url: Optional[str] = None
    display_order: int
    vote_count: int
    percentage: float
    rank: int


class AdminVoteTransaction(BaseModel):
    """Payment facts only.

    Deliberately omits `candidate_ids` and `voter_hash`: pairing those with
    `email` would de-anonymise who voted for whom, and elections default to
    anonymous_results=true. This is enough to reconcile money or chase a
    failed payment, and no more.
    """

    transaction_id: UUID
    reference: str
    email: Optional[str] = None
    amount_pesewas: int
    currency: str
    status: str
    created_at: datetime
    # Derived from amount / effective vote price, since it isn't stored.
    vote_count: int


class AdminElectionDetailResponse(BaseModel):
    election_id: UUID
    title: str
    description: Optional[str] = None
    election_type: str
    start_datetime: datetime
    end_datetime: datetime
    status: str
    created_at: datetime
    updated_at: Optional[datetime] = None
    banner_image_url: Optional[str] = None
    visibility: str
    allow_result_viewing: str
    anonymous_results: bool
    allow_revoting: bool
    max_selections: Optional[int] = None

    organizer_id: UUID
    organizer_name: str = ""
    organizer_email: str = ""

    vote_price: Optional[int] = None
    effective_vote_price: int

    total_votes: int
    total_candidates: int
    total_eligible_voters: int
    turnout_percentage: float
    total_receipts: int

    total_revenue_pesewas: int
    transactions_successful: int
    transactions_failed: int
    transactions_pending: int

    candidates: List[AdminCandidateResult]
    transactions: List[AdminVoteTransaction]


@router.get("/elections/{election_id}", response_model=AdminElectionDetailResponse)
async def get_election_detail(
    election_id: UUID,
    current_user: User = Depends(require_roles(ADMIN_ROLE)),
    db: AsyncSession = Depends(get_db),
    transaction_limit: int = 100,
):
    """Everything about one election: its record, the candidate vote
    breakdown, and the payments behind the revenue figure."""
    election_repo = ElectionRepository(Election, db)
    election = await election_repo.get_with_candidates(election_id)
    if not election:
        raise HTTPException(status_code=404, detail="Election not found")

    organizer = await db.get(User, election.created_by)

    global_price = int(await _get_platform_setting(db, "vote_price"))
    effective_price = (
        election.vote_price if election.vote_price is not None else global_price
    )

    # Reuse the one correct, weight-aware tally rather than writing another.
    # Candidate ids live inside the encrypted vote payload, so there is no
    # SQL path to a per-candidate breakdown.
    voting_service = VotingService(
        election_repo=election_repo,
        candidate_repo=CandidateRepository(Candidate, db),
        vote_repo=VoteRepository(Vote, db),
        receipt_repo=VoteReceiptRepository(VoteReceipt, db),
        audit_repo=AuditLogRepository(AuditLog, db),
        crypto_service=CryptographyService(),
    )
    results = await voting_service.get_live_results(election_id)

    by_id = {c.candidate_id: c for c in election.candidates}
    candidates = [
        AdminCandidateResult(
            candidate_id=r.candidate_id,
            name=r.name,
            short_code=by_id[r.candidate_id].short_code if r.candidate_id in by_id else None,
            description=by_id[r.candidate_id].description if r.candidate_id in by_id else None,
            image_url=by_id[r.candidate_id].image_url if r.candidate_id in by_id else None,
            display_order=by_id[r.candidate_id].display_order if r.candidate_id in by_id else 0,
            vote_count=r.vote_count,
            percentage=r.percentage,
            rank=index + 1,
        )
        # get_live_results already sorts descending by vote count.
        for index, r in enumerate(results.results)
    ]

    # Transaction.election_id has no FK and no relationship — filter manually.
    from app.models.transaction import Transaction

    status_rows = await db.execute(
        select(
            Transaction.status,
            sqlfunc.count(Transaction.transaction_id),
            sqlfunc.coalesce(sqlfunc.sum(Transaction.amount), 0),
        )
        .where(Transaction.election_id == election_id)
        .group_by(Transaction.status)
    )
    counts: dict[str, int] = {}
    revenue_pesewas = 0
    for status_value, count, amount in status_rows.all():
        counts[status_value] = int(count)
        if status_value == "success":
            revenue_pesewas = int(amount or 0)

    txn_result = await db.execute(
        select(Transaction)
        .where(Transaction.election_id == election_id)
        .order_by(Transaction.created_at.desc())
        .limit(transaction_limit)
    )
    transactions = [
        AdminVoteTransaction(
            transaction_id=t.transaction_id,
            reference=t.reference,
            email=t.email,
            amount_pesewas=t.amount,
            currency=t.currency,
            status=t.status,
            created_at=t.created_at,
            vote_count=max(1, t.amount // effective_price) if effective_price else 1,
        )
        for t in txn_result.scalars().all()
    ]

    receipts = await db.execute(
        select(sqlfunc.count(VoteReceipt.receipt_id)).where(
            VoteReceipt.election_id == election_id
        )
    )

    return AdminElectionDetailResponse(
        election_id=election.election_id,
        title=election.title,
        description=election.description,
        election_type=election.election_type,
        start_datetime=election.start_datetime,
        end_datetime=election.end_datetime,
        status=election.status,
        created_at=election.created_at,
        updated_at=election.updated_at,
        banner_image_url=election.banner_image_url,
        visibility=election.visibility,
        allow_result_viewing=election.allow_result_viewing,
        anonymous_results=election.anonymous_results,
        allow_revoting=election.allow_revoting,
        max_selections=election.max_selections,
        organizer_id=election.created_by,
        organizer_name=organizer.full_name if organizer else "",
        organizer_email=organizer.email if organizer else "",
        vote_price=election.vote_price,
        effective_vote_price=effective_price,
        total_votes=results.total_votes,
        total_candidates=len(election.candidates),
        total_eligible_voters=results.total_eligible_voters,
        turnout_percentage=results.turnout_percentage,
        total_receipts=int(receipts.scalar_one()),
        total_revenue_pesewas=revenue_pesewas,
        transactions_successful=counts.get("success", 0),
        transactions_failed=counts.get("failed", 0),
        transactions_pending=counts.get("pending", 0),
        candidates=candidates,
        transactions=transactions,
    )
