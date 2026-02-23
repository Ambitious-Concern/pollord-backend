from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_current_active_user, require_roles
from app.db.base import get_db
from app.models.audit_log import AuditLog
from app.models.election import Candidate, Election, EligibleVoter
from app.models.user import User
from app.repositories.audit_log_repository import AuditLogRepository
from app.repositories.election_repository import CandidateRepository, ElectionRepository
from app.schemas.election import (
    CandidateCreate,
    CandidateResponse,
    ElectionCreate,
    ElectionResponse,
    ElectionUpdate,
    ElectionWithCandidates,
    EligibleVoterAdd,
    EligibleVoterResponse,
)

router = APIRouter(prefix="/elections", tags=["Elections"])

ADMIN_ROLES = ("System Administrator", "Election Administrator")


@router.post("/", response_model=ElectionResponse, status_code=201)
async def create_election(
    data: ElectionCreate,
    current_user: User = Depends(require_roles(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    election_repo = ElectionRepository(Election, db)
    audit_repo = AuditLogRepository(AuditLog, db)

    election = await election_repo.create(
        {
            **data.model_dump(),
            "created_by": current_user.user_id,
        }
    )

    await audit_repo.log_action(
        action_type="CREATE",
        entity_type="Election",
        entity_id=election.election_id,
        user_id=current_user.user_id,
    )

    return ElectionResponse.model_validate(election)


@router.get("/", response_model=List[ElectionResponse])
async def list_elections(
    db: AsyncSession = Depends(get_db),
    skip: int = 0,
    limit: int = 20,
    status_filter: Optional[str] = None,
    current_user: User = Depends(get_current_active_user),
):
    election_repo = ElectionRepository(Election, db)
    user_roles = [ur.role.role_name for ur in current_user.user_roles]

    if any(r in ADMIN_ROLES for r in user_roles):
        if status_filter:
            elections = await election_repo.get_elections_by_status(
                status_filter, skip, limit
            )
        else:
            elections = await election_repo.get_all(skip=skip, limit=limit)
    else:
        elections = await election_repo.get_active_elections()

    return [ElectionResponse.model_validate(e) for e in elections]


@router.get("/{election_id}", response_model=ElectionWithCandidates)
async def get_election(
    election_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    election_repo = ElectionRepository(Election, db)
    election = await election_repo.get_with_candidates(election_id)
    if not election:
        raise HTTPException(status_code=404, detail="Election not found")

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
        candidates=[CandidateResponse.model_validate(c) for c in election.candidates],
    )


@router.put("/{election_id}", response_model=ElectionResponse)
async def update_election(
    election_id: UUID,
    data: ElectionUpdate,
    current_user: User = Depends(require_roles(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    election_repo = ElectionRepository(Election, db)
    election = await election_repo.get_by_id(election_id, id_field="election_id")
    if not election:
        raise HTTPException(status_code=404, detail="Election not found")

    if election.status not in ("draft", "scheduled"):
        raise HTTPException(
            status_code=400,
            detail="Cannot modify election after voting has started",
        )

    update_data = data.model_dump(exclude_unset=True)
    updated = await election_repo.update(
        election_id, update_data, id_field="election_id"
    )
    return ElectionResponse.model_validate(updated)


@router.delete("/{election_id}", status_code=204)
async def delete_election(
    election_id: UUID,
    current_user: User = Depends(require_roles(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    election_repo = ElectionRepository(Election, db)
    election = await election_repo.get_by_id(election_id, id_field="election_id")
    if not election:
        raise HTTPException(status_code=404, detail="Election not found")

    if election.status != "draft":
        raise HTTPException(
            status_code=400, detail="Only draft elections can be deleted"
        )

    await election_repo.delete(election_id, id_field="election_id")


# --- Candidates ---


@router.post(
    "/{election_id}/candidates",
    response_model=CandidateResponse,
    status_code=201,
)
async def add_candidate(
    election_id: UUID,
    data: CandidateCreate,
    current_user: User = Depends(require_roles(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    election_repo = ElectionRepository(Election, db)
    election = await election_repo.get_by_id(election_id, id_field="election_id")
    if not election:
        raise HTTPException(status_code=404, detail="Election not found")

    if election.status not in ("draft", "scheduled"):
        raise HTTPException(
            status_code=400,
            detail="Cannot add candidates after voting has started",
        )

    candidate_repo = CandidateRepository(Candidate, db)
    candidate = await candidate_repo.create(
        {**data.model_dump(), "election_id": election_id}
    )
    return CandidateResponse.model_validate(candidate)


@router.get(
    "/{election_id}/candidates", response_model=List[CandidateResponse]
)
async def list_candidates(
    election_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    candidate_repo = CandidateRepository(Candidate, db)
    candidates = await candidate_repo.get_by_election(election_id)
    return [CandidateResponse.model_validate(c) for c in candidates]


@router.delete("/{election_id}/candidates/{candidate_id}", status_code=204)
async def remove_candidate(
    election_id: UUID,
    candidate_id: UUID,
    current_user: User = Depends(require_roles(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    candidate_repo = CandidateRepository(Candidate, db)
    candidate = await candidate_repo.get_by_id(candidate_id, id_field="candidate_id")
    if not candidate or candidate.election_id != election_id:
        raise HTTPException(status_code=404, detail="Candidate not found")

    await candidate_repo.delete(candidate_id, id_field="candidate_id")


# --- Eligible Voters ---


@router.post("/{election_id}/eligible-voters", status_code=201)
async def add_eligible_voters(
    election_id: UUID,
    data: EligibleVoterAdd,
    current_user: User = Depends(require_roles(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    election_repo = ElectionRepository(Election, db)
    election = await election_repo.get_by_id(election_id, id_field="election_id")
    if not election:
        raise HTTPException(status_code=404, detail="Election not found")

    voters = await election_repo.add_eligible_voters(election_id, data.user_ids)
    return {"message": f"Added {len(voters)} eligible voter(s)"}


@router.get(
    "/{election_id}/eligible-voters",
    response_model=List[EligibleVoterResponse],
)
async def list_eligible_voters(
    election_id: UUID,
    current_user: User = Depends(require_roles(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    election_repo = ElectionRepository(Election, db)
    voters = await election_repo.get_eligible_voters(election_id)
    return [EligibleVoterResponse.model_validate(v) for v in voters]
