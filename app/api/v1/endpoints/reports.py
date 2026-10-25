import json
from typing import List
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import require_election_plan, require_event_plan
from app.core.plans import PLAN_STANDARD
from app.db.base import get_db
from app.db.tenant import get_tenant_db
from app.models.audit_log import AuditLog
from app.models.election import Candidate, Category, Election
from app.models.event import Event
from app.models.vote import Vote, VoteReceipt
from app.repositories.audit_log_repository import AuditLogRepository
from app.repositories.election_repository import CandidateRepository, CategoryRepository, ElectionRepository
from app.repositories.event_repository import EventRepository
from app.repositories.vote_repository import VoteReceiptRepository, VoteRepository
from app.schemas.vote import (
    CategoryResults,
    ElectionResults,
    EventResults,
    ResultCertificate,
    VerifyCertificateResponse,
)
from app.services.cryptography_service import CryptographyService
from app.services.voting_service import VotingService

router = APIRouter(prefix="/reports", tags=["Reports"])


def _voting_service(db: AsyncSession) -> VotingService:
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


def _build_certificate(
    parent_type: str,
    parent_id: UUID,
    title: str,
    total_votes: int,
    categories: List[CategoryResults],
) -> ResultCertificate:
    payload = json.dumps(
        {
            "parent_type": parent_type,
            "parent_id": str(parent_id),
            "title": title,
            "total_votes": total_votes,
            "categories": [c.model_dump(mode="json") for c in categories],
        },
        sort_keys=True,
    ).encode()
    signature = CryptographyService().sign_certificate(payload)
    return ResultCertificate(
        verification_code=f"POL-{signature[:8].upper()}",
        parent_type=parent_type,
        parent_id=parent_id,
        title=title,
        total_votes=total_votes,
        categories=categories,
        signature=signature,
    )


@router.get("/elections/{election_id}/voting-results", response_model=ElectionResults)
async def election_voting_report(
    election_id: UUID,
    _election=Depends(require_election_plan(PLAN_STANDARD)),
    db: AsyncSession = Depends(get_tenant_db),
):
    """The exportable voting-results report — open to every plan."""
    return await _voting_service(db).get_live_results(election_id)


@router.get("/events/{event_id}/voting-results", response_model=EventResults)
async def event_voting_report(
    event_id: UUID,
    _event=Depends(require_event_plan(PLAN_STANDARD)),
    db: AsyncSession = Depends(get_tenant_db),
):
    """The exportable voting-results report — open to every plan."""
    return await _voting_service(db).get_event_live_results(event_id)


@router.get("/elections/{election_id}/certificate", response_model=ResultCertificate)
async def election_certificate(
    election_id: UUID,
    _election=Depends(require_election_plan(PLAN_STANDARD)),
    db: AsyncSession = Depends(get_tenant_db),
):
    """Signed audit certificate — only once the election has ended."""
    results = await _voting_service(db).get_results(election_id)
    return _build_certificate(
        "election", results.election_id, results.title, results.total_votes, results.categories
    )


@router.get("/events/{event_id}/certificate", response_model=ResultCertificate)
async def event_certificate(
    event_id: UUID,
    _event=Depends(require_event_plan(PLAN_STANDARD)),
    db: AsyncSession = Depends(get_tenant_db),
):
    """Signed audit certificate — only once voting has ended."""
    results = await _voting_service(db).get_event_results(event_id)
    return _build_certificate(
        "event", results.event_id, results.title, results.total_votes, results.categories
    )


@router.get("/certificates/verify", response_model=VerifyCertificateResponse)
async def verify_certificate(
    parent_type: str = Query(..., pattern="^(election|event)$"),
    parent_id: UUID = Query(...),
    verification_code: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """Public, no auth — recomputes the tally independently and checks
    whether the code matches, so anyone holding a code can confirm it's
    real without having to trust whoever shared it with them."""
    service = _voting_service(db)
    try:
        if parent_type == "election":
            results = await service.get_results(parent_id)
            certificate = _build_certificate(
                "election", results.election_id, results.title, results.total_votes, results.categories
            )
        else:
            results = await service.get_event_results(parent_id)
            certificate = _build_certificate(
                "event", results.event_id, results.title, results.total_votes, results.categories
            )
    except HTTPException:
        return VerifyCertificateResponse(valid=False)

    if certificate.verification_code != verification_code.strip().upper():
        return VerifyCertificateResponse(valid=False)
    return VerifyCertificateResponse(valid=True, certificate=certificate)
