"""The nomination pipeline: organizer form builder + review queue, and the
public form view/submit endpoints. See NominationService for the actual
logic — this file is just request wiring."""
from typing import List
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.dependencies import get_current_active_user, require_roles
from app.core.request_meta import client_ip, client_ip_and_agent
from app.db.base import get_db
from app.db.tenant import get_tenant_db
from app.middleware.rate_limit import limiter
from app.models.election import Candidate, Category, Election
from app.models.nomination import NominationForm, NominationSubmission
from app.models.organization import Organization
from app.models.user import User
from app.repositories.election_repository import CandidateRepository, CategoryRepository, ElectionRepository
from app.repositories.nomination_repository import NominationFormRepository, NominationSubmissionRepository
from app.repositories.organization_repository import OrganizationRepository
from app.schemas.nomination import (
    ManualNominationCreate,
    NominationFormCreate,
    NominationFormResponse,
    NominationFormUpdate,
    NominationGroupResponse,
    NominationGroupReview,
    NominationSubmissionCreate,
    NominationSubmissionResponse,
    PublicNominationFormResponse,
)
from app.services.nomination_service import NominationService

router = APIRouter(tags=["Nominations"])

ADMIN_ROLES = ("System Administrator", "Election Administrator")


def _get_service(db: AsyncSession) -> NominationService:
    return NominationService(
        db=db,
        form_repo=NominationFormRepository(NominationForm, db),
        submission_repo=NominationSubmissionRepository(NominationSubmission, db),
        election_repo=ElectionRepository(Election, db),
        category_repo=CategoryRepository(Category, db),
        candidate_repo=CandidateRepository(Candidate, db),
    )


# --- Organizer: form builder ---------------------------------------------


@router.post(
    "/elections/{election_id}/nomination-forms",
    response_model=NominationFormResponse,
    status_code=201,
)
async def create_nomination_form(
    election_id: UUID,
    data: NominationFormCreate,
    request: Request,
    current_user: User = Depends(require_roles(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_tenant_db),
):
    return await _get_service(db).create_form(election_id, current_user, data, str(request.base_url))


@router.get(
    "/elections/{election_id}/nomination-forms",
    response_model=List[NominationFormResponse],
)
async def list_nomination_forms(
    election_id: UUID,
    request: Request,
    current_user: User = Depends(require_roles(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_tenant_db),
):
    return await _get_service(db).list_forms(election_id, current_user, str(request.base_url))


@router.get("/nomination-forms/mine", response_model=List[NominationFormResponse])
async def list_all_nomination_forms(
    request: Request,
    current_user: User = Depends(require_roles(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_tenant_db),
):
    """Every nomination form across every election the caller's team owns
    — the Nominations page's cross-election "Forms" panel. Declared before
    the /{form_id} route below so "mine" is never swallowed as a form_id."""
    teammate_ids = await OrganizationRepository(Organization, db).get_teammate_ids(current_user.user_id)
    return await _get_service(db).list_forms_for_teammates(teammate_ids, str(request.base_url))


@router.get("/nomination-forms/{form_id}", response_model=NominationFormResponse)
async def get_nomination_form(
    form_id: UUID,
    request: Request,
    current_user: User = Depends(require_roles(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_tenant_db),
):
    return await _get_service(db).get_form(form_id, current_user, str(request.base_url))


@router.put("/nomination-forms/{form_id}", response_model=NominationFormResponse)
async def update_nomination_form(
    form_id: UUID,
    data: NominationFormUpdate,
    request: Request,
    current_user: User = Depends(require_roles(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_tenant_db),
):
    return await _get_service(db).update_form(form_id, current_user, data, str(request.base_url))


@router.delete("/nomination-forms/{form_id}", status_code=204)
async def delete_nomination_form(
    form_id: UUID,
    current_user: User = Depends(require_roles(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_tenant_db),
):
    await _get_service(db).delete_form(form_id, current_user)


# --- Public: view + submit -------------------------------------------------


@router.get("/public/nomination-forms/{slug}", response_model=PublicNominationFormResponse)
async def get_public_nomination_form(
    slug: str,
    db: AsyncSession = Depends(get_db),
):
    return await _get_service(db).get_public_form(slug)


@router.post("/public/nomination-forms/{slug}/submit", status_code=201)
@limiter.limit(settings.RATE_LIMIT_VOTING)
async def submit_nomination(
    slug: str,
    data: NominationSubmissionCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    submission = await _get_service(db).submit_public(
        slug,
        data,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    return {"message": "Thanks — your nomination has been submitted for review.", "submission_id": str(submission.submission_id)}


# --- Organizer: cross-election review queue --------------------------------


@router.get("/nomination-groups", response_model=List[NominationGroupResponse])
async def list_all_nomination_groups(
    current_user: User = Depends(require_roles(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_tenant_db),
):
    """Every nomination group across every election the caller's team owns
    — the review queue's cross-election view (like /nominees for candidates)."""
    teammate_ids = await OrganizationRepository(Organization, db).get_teammate_ids(current_user.user_id)
    return await _get_service(db).list_groups_for_teammates(teammate_ids)


# --- Organizer: manual add + review queue ----------------------------------


@router.post(
    "/elections/{election_id}/nomination-groups/manual",
    status_code=201,
)
async def add_manual_nomination(
    election_id: UUID,
    data: ManualNominationCreate,
    current_user: User = Depends(require_roles(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_tenant_db),
):
    submission = await _get_service(db).add_manual(election_id, current_user, data)
    return {"submission_id": str(submission.submission_id)}


@router.get(
    "/elections/{election_id}/nomination-groups",
    response_model=List[NominationGroupResponse],
)
async def list_nomination_groups(
    election_id: UUID,
    current_user: User = Depends(require_roles(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_tenant_db),
):
    return await _get_service(db).list_groups(election_id, current_user)


@router.get(
    "/elections/{election_id}/nomination-groups/submissions",
    response_model=List[NominationSubmissionResponse],
)
async def list_nomination_group_submissions(
    election_id: UUID,
    category_id: UUID,
    nominee_name: str,
    current_user: User = Depends(require_roles(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_tenant_db),
):
    """The individual submissions behind one review-queue row — each
    nominator's full answers against the real form they were given."""
    return await _get_service(db).list_submissions_for_group(
        election_id, current_user, category_id, nominee_name
    )


@router.post("/elections/{election_id}/nomination-groups/review")
async def review_nomination_group(
    request: Request,
    election_id: UUID,
    data: NominationGroupReview,
    current_user: User = Depends(require_roles(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_tenant_db),
):
    ip, agent = client_ip_and_agent(request)
    return await _get_service(db).review_group(election_id, current_user, data, ip=ip, user_agent=agent)
