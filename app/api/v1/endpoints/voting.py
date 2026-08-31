import hashlib
import secrets
import uuid as _uuid
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.platform_setting import PlatformSetting
from app.core.dependencies import get_current_active_user
from app.core.security import generate_anonymous_voter_hash, generate_email_voter_hash
from app.db.base import get_db
from app.models.audit_log import AuditLog
from app.models.election import Candidate, Category, Election, VoterOTP
from app.models.event import Event
from app.models.transaction import Transaction
from app.models.user import User
from app.models.vote import Vote, VoteReceipt
from app.repositories.audit_log_repository import AuditLogRepository
from app.repositories.election_repository import CandidateRepository, CategoryRepository, ElectionRepository
from app.repositories.event_repository import EventRepository
from app.repositories.transaction_repository import TransactionRepository
from app.repositories.vote_repository import VoteReceiptRepository, VoteRepository
from app.schemas.election import ElectionWithCategories
from app.schemas.event import EventWithCategories
from app.schemas.payment import (
    InitiateVotePaymentRequest,
    VerifyAndCastRequest,
    VotePaymentInitResponse,
)
from app.schemas.vote import (
    CastVote,
    ElectionResults,
    EventResults,
    RequestVoteOTP,
    VerifyVoteOTPAndCast,
    VoteReceiptResponse,
)
from app.services import email_service
from app.services.cryptography_service import CryptographyService
from app.services.paystack_service import PaystackService
from app.services.voting_service import VotingService, resolve_candidate_selection

router = APIRouter(prefix="/voting", tags=["Voting"])


async def _get_global_vote_price(db: AsyncSession) -> int:
    """Read the global vote price from platform_settings, falling back to the env-var constant."""
    from sqlalchemy import select
    result = await db.execute(
        select(PlatformSetting).where(PlatformSetting.key == "vote_price")
    )
    row = result.scalar_one_or_none()
    if row:
        try:
            return int(row.value)
        except (ValueError, TypeError):
            pass
    return settings.VOTE_PRICE


async def _get_effective_vote_price(parent, db: AsyncSession) -> int:
    """Resolve the vote price for an Election or Event: per-parent override → global → env fallback."""
    if parent.vote_price is not None:
        return parent.vote_price
    return await _get_global_vote_price(db)


def _get_voting_service(db: AsyncSession) -> VotingService:
    return VotingService(
        election_repo=ElectionRepository(Election, db),
        event_repo=EventRepository(Event, db),
        category_repo=CategoryRepository(Category, db),
        candidate_repo=CandidateRepository(Candidate, db),
        vote_repo=VoteRepository(Vote, db),
        receipt_repo=VoteReceiptRepository(VoteReceipt, db),
        audit_repo=AuditLogRepository(AuditLog, db),
        crypto_service=CryptographyService(),
    )


async def _resolve_category_and_parent(db: AsyncSession, category_id: UUID):
    """Resolve a Category and whichever parent (Election or Event) it belongs
    to. Category-driven and parent-agnostic — the same lookup powers both
    election and event public voting."""
    category_repo = CategoryRepository(Category, db)
    category = await category_repo.get_with_candidates(category_id)
    if not category:
        raise HTTPException(status_code=404, detail="Category not found")

    if category.election_id is not None:
        parent = await ElectionRepository(Election, db).get_by_id(
            category.election_id, id_field="election_id"
        )
        parent_kind = "election"
    else:
        parent = await EventRepository(Event, db).get_by_id(
            category.event_id, id_field="event_id"
        )
        parent_kind = "event"

    if not parent:
        raise HTTPException(status_code=404, detail=f"{parent_kind.title()} not found")
    return category, parent, parent_kind


def _assert_open_for_voting(parent, parent_kind: str) -> None:
    now = datetime.now(timezone.utc)
    if parent_kind == "election":
        if not (parent.visibility == "public" and not parent.require_verification):
            raise HTTPException(
                status_code=403, detail="This election requires authentication to vote"
            )
        if parent.status != "active":
            raise HTTPException(status_code=400, detail="Election is not active")
        if now < parent.start_datetime or now > parent.end_datetime:
            raise HTTPException(status_code=400, detail="Election is not within voting period")
    else:
        # Events have no eligibility/visibility gating — always open while published.
        if parent.status != "published":
            raise HTTPException(status_code=400, detail="Event is not open for voting")


@router.post("/cast", response_model=VoteReceiptResponse, status_code=201)
async def cast_vote(
    data: CastVote,
    request: Request,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    service = _get_voting_service(db)
    return await service.cast_vote(
        user_id=current_user.user_id,
        data=data,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )


@router.get("/ballot/{election_id}", response_model=ElectionWithCategories)
async def get_ballot(
    election_id: UUID,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    service = _get_voting_service(db)
    return await service.get_ballot(current_user.user_id, election_id)


@router.get("/receipt/{election_id}", response_model=VoteReceiptResponse)
async def get_receipt(
    election_id: UUID,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    service = _get_voting_service(db)
    return await service.get_receipt(current_user.user_id, election_id)


@router.get("/live-results/{election_id}", response_model=ElectionResults)
async def get_live_results(
    election_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    service = _get_voting_service(db)
    return await service.get_live_results(election_id)


@router.get("/results/{election_id}", response_model=ElectionResults)
async def get_results(
    election_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    service = _get_voting_service(db)
    return await service.get_results(election_id)


# =========================================================================
# Public voting endpoints — no authentication required.
#
# Elections: only valid for visibility=public AND require_verification=False.
# Events: always open (no eligibility concept) while the event is published.
#
# Casting/payment endpoints below are category_id-driven and parent-agnostic —
# the same three endpoints serve both election-owned and event-owned
# categories, since a category_id alone already implies its parent.
# =========================================================================


@router.get("/public/ballot/{election_id}", response_model=ElectionWithCategories)
async def get_public_ballot(
    election_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Return ballot for a public open election. No auth required."""
    election_repo = ElectionRepository(Election, db)
    election = await election_repo.get_with_categories(election_id)
    if not election:
        raise HTTPException(status_code=404, detail="Election not found")
    if not (election.visibility == "public" and not election.require_verification):
        raise HTTPException(status_code=403, detail="This election requires authentication to vote")

    service = _get_voting_service(db)
    return service.build_election_with_categories(election)


@router.get("/public/events/{event_id}/ballot", response_model=EventWithCategories)
async def get_public_event_ballot(
    event_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Return ballot for an event's categories. No auth required — events have
    no eligibility concept, anyone can view and vote while it's published."""
    service = _get_voting_service(db)
    return await service.get_event_ballot(event_id)


@router.post("/public/cast", response_model=VoteReceiptResponse, status_code=201)
async def cast_public_vote(
    data: CastVote,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Cast a free vote into an election- or event-owned category without authentication."""
    category, parent, parent_kind = await _resolve_category_and_parent(db, data.category_id)
    _assert_open_for_voting(parent, parent_kind)

    candidate_ids = resolve_candidate_selection(
        category, data.candidate_ids, data.candidate_short_codes
    )

    ip = request.client.host if request.client else "unknown"
    ua = request.headers.get("user-agent", "unknown")
    base_hash = generate_anonymous_voter_hash(ip, ua, data.category_id)

    vote_repo = VoteRepository(Vote, db)
    allow_revoting = getattr(parent, "allow_revoting", False)
    if allow_revoting:
        voter_hash = hashlib.sha256(f"{base_hash}:{_uuid.uuid4().hex}".encode()).hexdigest()
    else:
        voter_hash = base_hash
        if await vote_repo.has_voted(voter_hash, data.category_id):
            raise HTTPException(status_code=409, detail="You have already voted in this category")

    crypto = CryptographyService()
    encrypted = crypto.encrypt_vote_data([str(cid) for cid in candidate_ids])
    now = datetime.now(timezone.utc)
    cast_at = now.isoformat()
    signature = crypto.sign_vote(encrypted, cast_at)

    await vote_repo.create({
        "category_id": category.category_id,
        "election_id": category.election_id,
        "event_id": category.event_id,
        "voter_hash": voter_hash,
        "vote_data": encrypted,
        "vote_signature": signature,
        "count": 1,
    })

    # Anonymous voters get a receipt code without a persisted VoteReceipt row
    # (that table is user-account scoped).
    receipt_code = crypto.generate_receipt_code()
    return VoteReceiptResponse(
        receipt_code=receipt_code,
        election_id=category.election_id,
        event_id=category.event_id,
        issued_at=now,
    )


@router.post("/public/request-vote-otp", status_code=200)
async def request_vote_otp(
    data: RequestVoteOTP,
    db: AsyncSession = Depends(get_db),
):
    """
    Step 1 of email-OTP-verified free voting — a stronger identity check than
    the plain IP+user-agent hash `cast_public_vote` uses (that path is
    untouched; this is additive). Sends a 6-digit code to the voter's email
    (free to send, unlike SMS) which must be passed to verify-vote-otp-and-cast.
    """
    category, parent, parent_kind = await _resolve_category_and_parent(db, data.category_id)
    _assert_open_for_voting(parent, parent_kind)

    email = data.email.lower()
    voter_hash = generate_email_voter_hash(email, data.category_id)
    vote_repo = VoteRepository(Vote, db)
    allow_revoting = getattr(parent, "allow_revoting", False)
    if not allow_revoting and await vote_repo.has_voted(voter_hash, data.category_id):
        raise HTTPException(status_code=409, detail="You have already voted in this category")

    # Invalidate any previous unused OTPs for this email+category
    prev = await db.execute(
        select(VoterOTP).where(
            VoterOTP.category_id == data.category_id,
            VoterOTP.email == email,
            VoterOTP.used == False,
        )
    )
    for old in prev.scalars().all():
        old.used = True
    await db.flush()

    otp_record = VoterOTP(
        category_id=data.category_id,
        email=email,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    db.add(otp_record)
    await db.flush()
    await db.refresh(otp_record)

    subject, html_body = email_service.voter_otp_email(
        otp_code=otp_record.otp_code,
        category_name=category.name,
        parent_title=getattr(parent, "title", "the election"),
    )
    email_service.send_email(email, subject, html_body)

    return {"message": "Code sent"}


@router.post("/public/verify-vote-otp-and-cast", response_model=VoteReceiptResponse, status_code=201)
async def verify_vote_otp_and_cast(
    data: VerifyVoteOTPAndCast,
    db: AsyncSession = Depends(get_db),
):
    """Step 2 — verify the emailed code and cast the vote, hashed on the
    verified email instead of IP+user-agent."""
    category, parent, parent_kind = await _resolve_category_and_parent(db, data.category_id)
    _assert_open_for_voting(parent, parent_kind)

    email = data.email.lower()
    result = await db.execute(
        select(VoterOTP).where(
            VoterOTP.category_id == data.category_id,
            VoterOTP.email == email,
            VoterOTP.otp_code == data.otp.strip(),
            VoterOTP.used == False,
        )
    )
    otp_record = result.scalar_one_or_none()
    if not otp_record:
        raise HTTPException(status_code=400, detail="Invalid or expired code")
    if otp_record.expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="Code has expired, please request a new one")

    otp_record.used = True
    await db.flush()

    candidate_ids = resolve_candidate_selection(
        category, data.candidate_ids, data.candidate_short_codes
    )

    voter_hash = generate_email_voter_hash(email, data.category_id)
    vote_repo = VoteRepository(Vote, db)
    allow_revoting = getattr(parent, "allow_revoting", False)
    if allow_revoting:
        voter_hash = hashlib.sha256(f"{voter_hash}:{_uuid.uuid4().hex}".encode()).hexdigest()
    elif await vote_repo.has_voted(voter_hash, data.category_id):
        raise HTTPException(status_code=409, detail="You have already voted in this category")

    crypto = CryptographyService()
    encrypted = crypto.encrypt_vote_data([str(cid) for cid in candidate_ids])
    now = datetime.now(timezone.utc)
    cast_at = now.isoformat()
    signature = crypto.sign_vote(encrypted, cast_at)

    await vote_repo.create({
        "category_id": category.category_id,
        "election_id": category.election_id,
        "event_id": category.event_id,
        "voter_hash": voter_hash,
        "vote_data": encrypted,
        "vote_signature": signature,
        "count": 1,
    })

    receipt_code = crypto.generate_receipt_code()
    return VoteReceiptResponse(
        receipt_code=receipt_code,
        election_id=category.election_id,
        event_id=category.event_id,
        issued_at=now,
    )


@router.post("/public/initiate-payment", response_model=VotePaymentInitResponse, status_code=201)
async def initiate_public_vote_payment(
    data: InitiateVotePaymentRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Step 1 of paid public voting.
    Validates the category, checks for duplicate vote (if revoting disabled),
    creates a pending Transaction, and initialises a Paystack payment.
    Returns the Paystack access_code + public_key for the inline popup.

    Bulk vote-buying (a custom amount for many votes) is governed solely by
    the parent's allow_revoting flag — independent of the category's ballot
    type (single_choice vs ranked).
    """
    category, parent, parent_kind = await _resolve_category_and_parent(db, data.category_id)
    _assert_open_for_voting(parent, parent_kind)

    valid_ids = {c.candidate_id for c in category.candidates}
    for cid in data.candidate_ids:
        if cid not in valid_ids:
            raise HTTPException(status_code=400, detail=f"Invalid candidate: {cid}")
    if category.election_type == "single_choice" and len(data.candidate_ids) != 1:
        raise HTTPException(status_code=400, detail="Single choice requires exactly one candidate")

    allow_revoting = getattr(parent, "allow_revoting", False)
    is_multi_vote = allow_revoting

    vote_price = await _get_effective_vote_price(parent, db)

    if is_multi_vote and data.amount_pesewas is not None:
        charge_pesewas = data.amount_pesewas
    else:
        charge_pesewas = vote_price

    # Must be an exact multiple of the effective vote_price
    if charge_pesewas % vote_price != 0:
        charge_pesewas = (charge_pesewas // vote_price) * vote_price
    vote_count = charge_pesewas // vote_price

    ip = request.client.host if request.client else "unknown"
    ua = request.headers.get("user-agent", "unknown")
    base_hash = generate_anonymous_voter_hash(ip, ua, data.category_id)

    if is_multi_vote:
        # Unique base hash so multiple transactions from same IP don't collide
        voter_hash_base = hashlib.sha256(f"{base_hash}:{_uuid.uuid4().hex}".encode()).hexdigest()
    else:
        voter_hash_base = base_hash
        vote_repo = VoteRepository(Vote, db)
        if await vote_repo.has_voted(voter_hash_base, data.category_id):
            raise HTTPException(status_code=409, detail="You have already voted in this category")

    reference = f"vote_{secrets.token_urlsafe(16)}"

    txn_repo = TransactionRepository(db)
    await txn_repo.create({
        "reference": reference,
        "election_id": category.election_id,
        "event_id": category.event_id,
        "category_id": category.category_id,
        "voter_hash": voter_hash_base,
        "email": data.email,
        "candidate_ids": [str(cid) for cid in data.candidate_ids],
        "amount": charge_pesewas,
        "currency": settings.VOTE_CURRENCY,
        "status": "pending",
    })

    paystack = PaystackService(settings.PAYSTACK_SECRET_KEY)
    ps_data = await paystack.initialize_transaction(
        email=data.email,
        amount=charge_pesewas,
        reference=reference,
        currency=settings.VOTE_CURRENCY,
        metadata={
            "category_id": str(data.category_id),
            "parent_kind": parent_kind,
            "title": getattr(parent, "title", None),
            "vote_count": vote_count,
        },
    )

    return VotePaymentInitResponse(
        reference=reference,
        access_code=ps_data["access_code"],
        public_key=settings.PAYSTACK_PUBLIC_KEY,
        amount=charge_pesewas,
        currency=settings.VOTE_CURRENCY,
        vote_count=vote_count,
    )


@router.post("/public/verify-and-cast", response_model=VoteReceiptResponse, status_code=201)
async def verify_and_cast_public_vote(
    data: VerifyAndCastRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Step 2 of paid public voting.
    Verifies the Paystack payment, then casts the vote and issues a receipt.
    """
    txn_repo = TransactionRepository(db)
    vote_repo = VoteRepository(Vote, db)
    crypto = CryptographyService()
    paystack = PaystackService(settings.PAYSTACK_SECRET_KEY)

    txn = await txn_repo.get_by_reference(data.reference)
    if not txn:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if txn.status == "success":
        raise HTTPException(status_code=409, detail="This payment has already been used to cast a vote")
    if txn.status == "failed":
        raise HTTPException(status_code=400, detail="Payment failed, please try again")

    ps_data = await paystack.verify_transaction(data.reference)

    if ps_data.get("status") != "success":
        await txn_repo.update_status(data.reference, "failed", ps_data)
        raise HTTPException(status_code=402, detail="Payment was not successful")

    if ps_data.get("amount", 0) < txn.amount:
        await txn_repo.update_status(data.reference, "failed", ps_data)
        raise HTTPException(status_code=402, detail="Payment amount does not match the required vote fee")

    if txn.election_id is not None:
        parent = await ElectionRepository(Election, db).get_by_id(
            txn.election_id, id_field="election_id"
        )
    else:
        parent = await EventRepository(Event, db).get_by_id(
            txn.event_id, id_field="event_id"
        )
    vote_price = await _get_effective_vote_price(parent, db) if parent else await _get_global_vote_price(db)
    vote_count = max(1, txn.amount // vote_price)

    now = datetime.now(timezone.utc)
    encrypted = crypto.encrypt_vote_data(txn.candidate_ids)
    cast_at = now.isoformat()
    signature = crypto.sign_vote(encrypted, cast_at)

    await vote_repo.create({
        "category_id": txn.category_id,
        "election_id": txn.election_id,
        "event_id": txn.event_id,
        "voter_hash": txn.voter_hash,
        "vote_data": encrypted,
        "vote_signature": signature,
        "count": vote_count,
    })

    await txn_repo.update_status(data.reference, "success", ps_data)

    receipt_code = crypto.generate_receipt_code()
    return VoteReceiptResponse(
        receipt_code=receipt_code,
        election_id=txn.election_id,
        event_id=txn.event_id,
        issued_at=now,
    )


@router.get("/public/live-results/{election_id}", response_model=ElectionResults)
async def get_public_live_results(
    election_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Live results for a public election. No auth required."""
    service = _get_voting_service(db)
    return await service.get_live_results(election_id)


@router.get("/public/events/{event_id}/live-results", response_model=EventResults)
async def get_public_event_live_results(
    event_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Live results for an event's categories. No auth required."""
    service = _get_voting_service(db)
    return await service.get_event_live_results(event_id)
