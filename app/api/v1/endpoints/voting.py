from uuid import UUID

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_current_active_user
from app.core.security import generate_anonymous_voter_hash
from app.db.base import get_db
from app.models.audit_log import AuditLog
from app.models.election import Candidate, Election
from app.models.user import User
from app.models.vote import Vote, VoteReceipt
from app.repositories.audit_log_repository import AuditLogRepository
from app.repositories.election_repository import CandidateRepository, ElectionRepository
from app.repositories.vote_repository import VoteReceiptRepository, VoteRepository
from app.schemas.election import ElectionWithCandidates
from app.schemas.vote import CastVote, ElectionResults, VoteReceiptResponse
from app.services.cryptography_service import CryptographyService
from app.services.voting_service import VotingService

router = APIRouter(prefix="/voting", tags=["Voting"])


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
    })

    # Return receipt without persisting (no user account for anonymous voters)
    receipt_code = crypto.generate_receipt_code()
    return VoteReceiptResponse(
        receipt_code=receipt_code,
        election_id=data.election_id,
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
