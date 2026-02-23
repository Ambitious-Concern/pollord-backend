from uuid import UUID

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_current_active_user
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


@router.get("/results/{election_id}", response_model=ElectionResults)
async def get_results(
    election_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    service = _get_voting_service(db)
    return await service.get_results(election_id)
