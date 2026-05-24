import hashlib
import secrets
import uuid as _uuid
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.platform_setting import PlatformSetting
from app.core.dependencies import get_current_active_user
from app.core.security import generate_anonymous_voter_hash
from app.db.base import get_db
from app.models.audit_log import AuditLog
from app.models.election import Candidate, Election
from app.models.transaction import Transaction
from app.models.user import User
from app.models.vote import Vote, VoteReceipt
from app.repositories.audit_log_repository import AuditLogRepository
from app.repositories.election_repository import CandidateRepository, ElectionRepository
from app.repositories.transaction_repository import TransactionRepository
from app.repositories.vote_repository import VoteReceiptRepository, VoteRepository
from app.schemas.election import ElectionWithCandidates
from app.schemas.payment import (
    InitiateVotePaymentRequest,
    VerifyAndCastRequest,
    VotePaymentInitResponse,
)
from app.schemas.vote import CastVote, ElectionResults, VoteReceiptResponse
from app.services.cryptography_service import CryptographyService
from app.services.paystack_service import PaystackService
from app.services.voting_service import VotingService

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


async def _get_effective_vote_price(election, db: AsyncSession) -> int:
    """Resolve the vote price for an election: per-election override → global → env fallback."""
    if election.vote_price is not None:
        return election.vote_price
    return await _get_global_vote_price(db)


def _get_voting_service(db: AsyncSession) -> VotingService:
    return VotingService(
        election_repo=ElectionRepository(Election, db),
        candidate_repo=CandidateRepository(Candidate, db),
        vote_repo=VoteRepository(Vote, db),
        receipt_repo=VoteReceiptRepository(VoteReceipt, db),
        audit_repo=AuditLogRepository(AuditLog, db),
        crypto_service=CryptographyService(),
    )


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


@router.get("/ballot/{election_id}", response_model=ElectionWithCandidates)
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
# Public voting endpoints — no authentication required
# Only valid for elections with visibility=public AND require_verification=False
# =========================================================================


@router.get("/public/ballot/{election_id}", response_model=ElectionWithCandidates)
async def get_public_ballot(
    election_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Return ballot for a public open election. No auth required."""
    from sqlalchemy.orm import selectinload
    from sqlalchemy import select as sa_select
    election_repo = ElectionRepository(Election, db)
    election = await election_repo.get_with_candidates(election_id)
    if not election:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Election not found")
    if not (election.visibility == "public" and not election.require_verification):
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="This election requires authentication to vote")
    from app.schemas.election import CandidateResponse as CandResp
    return ElectionWithCandidates(
        election_id=election.election_id,
        title=election.title,
        description=election.description,
        election_type=election.election_type,
        start_datetime=election.start_datetime,
        end_datetime=election.end_datetime,
        status=election.status,
        created_by=election.created_by,
        created_at=election.created_at,
        updated_at=election.updated_at,
        banner_image_url=getattr(election, "banner_image_url", None),
        visibility=election.visibility,
        access_code=None,
        allow_result_viewing=getattr(election, "allow_result_viewing", "after_end"),
        require_verification=election.require_verification,
        anonymous_results=getattr(election, "anonymous_results", True),
        allow_abstain=getattr(election, "allow_abstain", False),
        show_candidate_count=getattr(election, "show_candidate_count", False),
        randomize_candidate_order=getattr(election, "randomize_candidate_order", False),
        enable_notifications=getattr(election, "enable_notifications", True),
        max_selections=getattr(election, "max_selections", None),
        allow_revoting=getattr(election, "allow_revoting", False),
        vote_price=getattr(election, "vote_price", None),
        effective_vote_price=await _get_effective_vote_price(election, db),
        candidates=[
            CandResp(
                candidate_id=c.candidate_id,
                election_id=c.election_id,
                name=c.name,
                short_code=c.short_code,
                email=c.email,
                image_url=c.image_url,
                display_order=c.display_order,
            )
            for c in sorted(election.candidates, key=lambda x: x.display_order)
        ],
    )


@router.post("/public/cast", response_model=VoteReceiptResponse, status_code=201)
async def cast_public_vote(
    data: CastVote,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Cast a vote on a public open election without authentication."""
    from datetime import datetime, timezone
    from fastapi import HTTPException
    election_repo = ElectionRepository(Election, db)
    candidate_repo = CandidateRepository(Candidate, db)
    vote_repo = VoteRepository(Vote, db)
    crypto = CryptographyService()

    election = await election_repo.get_with_candidates(data.election_id)
    if not election:
        raise HTTPException(status_code=404, detail="Election not found")
    if not (election.visibility == "public" and not election.require_verification):
        raise HTTPException(status_code=403, detail="This election requires authentication to vote")
    if election.status != "active":
        raise HTTPException(status_code=400, detail="Election is not active")

    now = datetime.now(timezone.utc)
    if now < election.start_datetime or now > election.end_datetime:
        raise HTTPException(status_code=400, detail="Election is not within voting period")

    # Resolve candidates
    if data.candidate_short_codes:
        code_map = {c.short_code.upper(): c.candidate_id for c in election.candidates if c.short_code}
        candidate_ids = []
        for code in data.candidate_short_codes:
            cid = code_map.get(code.upper())
            if not cid:
                raise HTTPException(status_code=400, detail=f"Unknown candidate code: {code}")
            candidate_ids.append(cid)
    elif data.candidate_ids:
        candidate_ids = data.candidate_ids
    else:
        raise HTTPException(status_code=400, detail="Provide candidate_ids or candidate_short_codes")

    valid_ids = {c.candidate_id for c in election.candidates}
    for cid in candidate_ids:
        if cid not in valid_ids:
            raise HTTPException(status_code=400, detail=f"Invalid candidate: {cid}")

    if election.election_type == "single_choice" and len(candidate_ids) != 1:
        raise HTTPException(status_code=400, detail="Single choice requires exactly one candidate")

    # Anonymous voter hash from IP + UA
    ip = request.client.host if request.client else "unknown"
    ua = request.headers.get("user-agent", "unknown")
    base_hash = generate_anonymous_voter_hash(ip, ua, data.election_id)

    allow_revoting = getattr(election, "allow_revoting", False)
    if allow_revoting:
        import uuid as _uuid
        import hashlib as _hashlib
        voter_hash = _hashlib.sha256(f"{base_hash}:{_uuid.uuid4().hex}".encode()).hexdigest()
    else:
        voter_hash = base_hash
        if await vote_repo.has_voted(voter_hash, data.election_id):
            raise HTTPException(status_code=409, detail="You have already voted in this election")

    encrypted = crypto.encrypt_vote_data([str(cid) for cid in candidate_ids])
    cast_at = now.isoformat()
    signature = crypto.sign_vote(encrypted, cast_at)

    await vote_repo.create({
        "election_id": data.election_id,
        "voter_hash": voter_hash,
        "vote_data": encrypted,
        "vote_signature": signature,
        "count": 1,
    })

    # Return receipt without persisting (no user account for anonymous voters)
    receipt_code = crypto.generate_receipt_code()
    return VoteReceiptResponse(
        receipt_code=receipt_code,
        election_id=data.election_id,
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
    Validates the election, checks for duplicate vote (if revoting disabled),
    creates a pending Transaction, and initialises a Paystack payment.
    Returns the Paystack access_code + public_key for the inline popup.
    """
    from datetime import datetime, timezone

    election_repo = ElectionRepository(Election, db)
    txn_repo = TransactionRepository(db)
    crypto = CryptographyService()
    paystack = PaystackService(settings.PAYSTACK_SECRET_KEY)

    # 1. Load and validate the election
    election = await election_repo.get_with_candidates(data.election_id)
    if not election:
        raise HTTPException(status_code=404, detail="Election not found")
    if not (election.visibility == "public" and not election.require_verification):
        raise HTTPException(status_code=403, detail="This election requires authentication to vote")
    if election.status != "active":
        raise HTTPException(status_code=400, detail="Election is not active")

    now = datetime.now(timezone.utc)
    if now < election.start_datetime or now > election.end_datetime:
        raise HTTPException(status_code=400, detail="Election is not within voting period")

    # 2. Validate candidate IDs belong to this election
    valid_ids = {str(c.candidate_id) for c in election.candidates}
    for cid in data.candidate_ids:
        if str(cid) not in valid_ids:
            raise HTTPException(status_code=400, detail=f"Invalid candidate: {cid}")
    if election.election_type == "single_choice" and len(data.candidate_ids) != 1:
        raise HTTPException(status_code=400, detail="Single choice requires exactly one candidate")

    # 3. Determine vote count and amount
    allow_revoting = getattr(election, "allow_revoting", False)
    is_multi_vote = allow_revoting or election.election_type == "multiple_choice"

    vote_price = await _get_effective_vote_price(election, db)

    if is_multi_vote and data.amount_pesewas is not None:
        charge_pesewas = data.amount_pesewas
    else:
        charge_pesewas = vote_price

    # Must be an exact multiple of the effective vote_price
    if charge_pesewas % vote_price != 0:
        charge_pesewas = (charge_pesewas // vote_price) * vote_price
    vote_count = charge_pesewas // vote_price

    # 4. Compute voter hash; for multi-vote or revoting use unique hash per transaction
    ip = request.client.host if request.client else "unknown"
    ua = request.headers.get("user-agent", "unknown")
    base_hash = generate_anonymous_voter_hash(ip, ua, data.election_id)

    if is_multi_vote or allow_revoting:
        # Unique base hash so multiple transactions from same IP don't collide
        voter_hash_base = hashlib.sha256(f"{base_hash}:{_uuid.uuid4().hex}".encode()).hexdigest()
    else:
        voter_hash_base = base_hash
        vote_repo = VoteRepository(Vote, db)
        if await vote_repo.has_voted(voter_hash_base, data.election_id):
            raise HTTPException(status_code=409, detail="You have already voted in this election")

    # 5. Create a unique Paystack reference
    reference = f"vote_{secrets.token_urlsafe(16)}"

    # 6. Persist pending transaction (store the base hash; verify-and-cast will derive per-vote hashes)
    await txn_repo.create({
        "reference": reference,
        "election_id": data.election_id,
        "voter_hash": voter_hash_base,
        "email": data.email,
        "candidate_ids": [str(cid) for cid in data.candidate_ids],
        "amount": charge_pesewas,
        "currency": settings.VOTE_CURRENCY,
        "status": "pending",
    })

    # 7. Initialise Paystack transaction
    ps_data = await paystack.initialize_transaction(
        email=data.email,
        amount=charge_pesewas,
        reference=reference,
        currency=settings.VOTE_CURRENCY,
        metadata={
            "election_id": str(data.election_id),
            "election_title": election.title,
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
    from datetime import datetime, timezone

    txn_repo = TransactionRepository(db)
    vote_repo = VoteRepository(Vote, db)
    crypto = CryptographyService()
    paystack = PaystackService(settings.PAYSTACK_SECRET_KEY)

    # 1. Look up the pending transaction
    txn = await txn_repo.get_by_reference(data.reference)
    if not txn:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if txn.status == "success":
        raise HTTPException(status_code=409, detail="This payment has already been used to cast a vote")
    if txn.status == "failed":
        raise HTTPException(status_code=400, detail="Payment failed — please try again")

    # 2. Verify with Paystack
    ps_data = await paystack.verify_transaction(data.reference)

    if ps_data.get("status") != "success":
        await txn_repo.update_status(data.reference, "failed", ps_data)
        raise HTTPException(status_code=402, detail="Payment was not successful")

    # Amount check: paid amount must be >= what we charged
    if ps_data.get("amount", 0) < txn.amount:
        await txn_repo.update_status(data.reference, "failed", ps_data)
        raise HTTPException(status_code=402, detail="Payment amount does not match the required vote fee")

    # 3. Calculate vote count using the effective vote_price for this election
    election_repo = ElectionRepository(Election, db)
    election_for_price = await election_repo.get_by_id(txn.election_id, id_field="election_id")
    if election_for_price:
        vote_price = await _get_effective_vote_price(election_for_price, db)
    else:
        vote_price = await _get_global_vote_price(db)
    vote_count = max(1, txn.amount // vote_price)
    now = datetime.now(timezone.utc)
    encrypted = crypto.encrypt_vote_data(txn.candidate_ids)
    cast_at = now.isoformat()
    signature = crypto.sign_vote(encrypted, cast_at)

    await vote_repo.create({
        "election_id": txn.election_id,
        "voter_hash": txn.voter_hash,
        "vote_data": encrypted,
        "vote_signature": signature,
        "count": vote_count,
    })

    # 4. Mark transaction as success
    await txn_repo.update_status(data.reference, "success", ps_data)

    # 5. Return receipt (anonymous — no DB VoteReceipt record needed)
    receipt_code = crypto.generate_receipt_code()
    return VoteReceiptResponse(
        receipt_code=receipt_code,
        election_id=txn.election_id,
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
