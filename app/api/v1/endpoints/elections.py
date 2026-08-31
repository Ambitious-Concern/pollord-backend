import uuid as uuid_lib
from pathlib import Path
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from datetime import datetime, timedelta, timezone

from app.core.config import settings
from app.core.dependencies import get_current_active_user, require_roles
from app.core.security import create_candidate_result_token, decode_token
from app.core.slug import generate_slug
from app.db.base import get_db
from app.models.audit_log import AuditLog
from app.models.election import Candidate, CandidateAccessOTP, Category, Election, EligibleVoter
from app.models.organization import Organization
from app.models.platform_setting import PlatformSetting
from app.models.user import User
from app.repositories.audit_log_repository import AuditLogRepository
from app.repositories.election_repository import CandidateRepository, CategoryRepository, ElectionRepository
from app.repositories.organization_repository import OrganizationRepository
from app.services import email_service
from app.services.file_storage_service import file_storage_service
from app.services.voting_service import VotingService
from app.schemas.election import (
    CandidateCreate,
    CandidateResponse,
    CandidateUpdate,
    CategoryCreate,
    CategoryResponse,
    CategoryUpdate,
    CategoryWithCandidates,
    ElectionCreate,
    ElectionPublicResponse,
    ElectionResponse,
    ElectionUpdate,
    ElectionWithCategories,
    EligibleVoterAdd,
    EligibleVoterResponse,
)

router = APIRouter(prefix="/elections", tags=["Elections"])

ADMIN_ROLES = ("System Administrator", "Election Administrator")
SYSTEM_ADMIN = "System Administrator"


def _owns_election(election, current_user: User) -> bool:
    """True if the user created the election OR is a System Administrator."""
    user_roles = [ur.role.role_name for ur in current_user.user_roles]
    return election.created_by == current_user.user_id or SYSTEM_ADMIN in user_roles


def _require_ownership(election, current_user: User) -> None:
    if not _owns_election(election, current_user):
        raise HTTPException(status_code=403, detail="You do not have access to this election")

SETTINGS_FIELDS = (
    "visibility",
    "access_code",
    "allow_result_viewing",
    "require_verification",
    "anonymous_results",
    "show_candidate_count",
    "randomize_candidate_order",
    "enable_notifications",
    "allow_revoting",
    "vote_price",
)

# Once an election is active, its ballot (dates, categories, voting rules,
# access) is locked so results stay trustworthy — but purely cosmetic/info
# fields are still safe to fix up (a typo in the title, a wrong venue, etc).
SAFE_FIELDS_WHILE_ACTIVE = {"title", "description", "venue", "latitude", "longitude", "tag"}
SAFE_SETTINGS_WHILE_ACTIVE = {"enable_notifications"}


def _extract_settings(data) -> dict:
    """Extract settings fields from an ElectionCreate/Update payload."""
    result = {}
    s = getattr(data, "settings", None)
    if s:
        for field in SETTINGS_FIELDS:
            val = getattr(s, field, None)
            if val is not None:
                result[field] = val
    return result


async def _get_global_vote_price(db: AsyncSession) -> int:
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


MIN_VOTE_PRICE = 50  # pesewas (₵0.50); custom prices must be a multiple of this


async def _validate_vote_price(data, db: AsyncSession) -> None:
    """Raise if a per-election vote_price is below the ₵0.50 minimum or not a ₵0.50 multiple."""
    s = getattr(data, "settings", None)
    if not s:
        return
    price = getattr(s, "vote_price", None)
    if price is None:
        return
    if price < MIN_VOTE_PRICE or price % MIN_VOTE_PRICE != 0:
        raise HTTPException(
            status_code=400,
            detail="Vote price must be at least ₵0.50 and a multiple of ₵0.50",
        )


# =========================================================================
# Public endpoints (no auth required)
# =========================================================================


async def _get_global_vote_price(db: AsyncSession) -> int:
    """Read the global vote price from platform_settings, falling back to the env-var constant."""
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


@router.get("/public", response_model=List[ElectionPublicResponse])
async def list_public_elections(
    db: AsyncSession = Depends(get_db),
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    status_filter: Optional[str] = None,
):
    """List all public elections. No authentication required."""
    election_repo = ElectionRepository(Election, db)

    if status_filter:
        elections = await election_repo.get_elections_by_status(
            status_filter, skip, limit
        )
    else:
        elections = await election_repo.get_all(skip=skip, limit=limit)

    # Filter to only public elections
    public = [e for e in elections if getattr(e, "visibility", "public") == "public"]

    global_vote_price = await _get_global_vote_price(db)
    responses = []
    for e in public:
        resp = ElectionPublicResponse.model_validate(e)
        resp.effective_vote_price = e.vote_price if e.vote_price is not None else global_vote_price
        responses.append(resp)
    return responses


@router.get("/public/{election_id}", response_model=ElectionWithCategories)
async def get_public_election(
    election_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Get a single public election with its categories/candidates. No auth required."""
    election_repo = ElectionRepository(Election, db)
    election = await election_repo.get_with_categories(election_id)
    if not election:
        raise HTTPException(status_code=404, detail="Election not found")

    if getattr(election, "visibility", "public") != "public":
        raise HTTPException(status_code=404, detail="Election not found")

    response = VotingService.build_election_with_categories(election)
    response.access_code = None  # Don't expose access code publicly
    return response


@router.get("/public/by-slug/{slug}", response_model=ElectionWithCategories)
async def get_public_election_by_slug(
    slug: str,
    db: AsyncSession = Depends(get_db),
):
    """Same as get_public_election, keyed by slug — for slug-based routing (v2)."""
    election_repo = ElectionRepository(Election, db)
    election = await election_repo.get_by_slug(slug)
    if not election:
        raise HTTPException(status_code=404, detail="Election not found")

    if getattr(election, "visibility", "public") != "public":
        raise HTTPException(status_code=404, detail="Election not found")

    response = VotingService.build_election_with_categories(election)
    response.access_code = None
    return response


@router.get("/user/{user_id}", response_model=List[ElectionPublicResponse])
async def list_user_public_elections(
    user_id: UUID,
    db: AsyncSession = Depends(get_db),
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
):
    """List all public elections by a specific user. No auth required."""
    election_repo = ElectionRepository(Election, db)
    elections = await election_repo.get_elections_by_creator(user_id)

    public = [e for e in elections if getattr(e, "visibility", "public") == "public"]
    paginated = public[skip : skip + limit]
    return [ElectionPublicResponse.model_validate(e) for e in paginated]


# =========================================================================
# Authenticated endpoints
# =========================================================================


@router.post("", response_model=ElectionResponse, status_code=201)
async def create_election(
    data: ElectionCreate,
    current_user: User = Depends(require_roles(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    election_repo = ElectionRepository(Election, db)
    audit_repo = AuditLogRepository(AuditLog, db)

    await _validate_vote_price(data, db)

    create_data = {
        "title": data.title,
        "slug": data.slug or generate_slug(data.title),
        "description": data.description,
        "start_datetime": data.start_datetime,
        "end_datetime": data.end_datetime,
        "banner_image_url": data.banner_image_url,
        "venue": data.venue,
        "latitude": data.latitude,
        "longitude": data.longitude,
        "tag": data.tag,
        "created_by": current_user.user_id,
        **_extract_settings(data),
    }

    election = await election_repo.create(create_data)

    await audit_repo.log_action(
        action_type="CREATE",
        entity_type="Election",
        entity_id=election.election_id,
        user_id=current_user.user_id,
    )

    return ElectionResponse.model_validate(election)


@router.get("", response_model=List[ElectionResponse])
async def list_elections(
    db: AsyncSession = Depends(get_db),
    skip: int = 0,
    limit: int = 20,
    status_filter: Optional[str] = None,
    current_user: User = Depends(require_roles(*ADMIN_ROLES)),
):
    """Return only the elections created by the current user."""
    election_repo = ElectionRepository(Election, db)
    elections = await election_repo.get_elections_by_creator(
        current_user.user_id,
        skip=skip,
        limit=limit,
        status=status_filter,
    )
    return [ElectionResponse.model_validate(e) for e in elections]


@router.get("/{election_id}", response_model=ElectionWithCategories)
async def get_election(
    election_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(*ADMIN_ROLES)),
):
    election_repo = ElectionRepository(Election, db)
    election = await election_repo.get_with_categories(election_id)
    if not election:
        raise HTTPException(status_code=404, detail="Election not found")
    _require_ownership(election, current_user)

    return VotingService.build_election_with_categories(election)


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
    _require_ownership(election, current_user)

    update_data = data.model_dump(exclude_unset=True, exclude={"settings"})
    settings_data = _extract_settings(data)

    if election.status == "active":
        disallowed = sorted(set(update_data) - SAFE_FIELDS_WHILE_ACTIVE)
        if disallowed:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot change {', '.join(disallowed)} while the election is active",
            )
        disallowed_settings = sorted(set(settings_data) - SAFE_SETTINGS_WHILE_ACTIVE)
        if disallowed_settings:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot change {', '.join(disallowed_settings)} while the election is active",
            )
    elif election.status not in ("draft", "scheduled"):
        raise HTTPException(
            status_code=400,
            detail="Cannot modify election after voting has started",
        )

    await _validate_vote_price(data, db)

    update_data.update(settings_data)

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
    _require_ownership(election, current_user)

    if election.status != "draft":
        raise HTTPException(
            status_code=400, detail="Only draft elections can be deleted"
        )

    await election_repo.delete(election_id, id_field="election_id")


# =========================================================================
# Status transitions
# =========================================================================

VALID_TRANSITIONS = {
    "draft": ["scheduled", "active"],
    "scheduled": ["active", "draft"],
    "active": ["completed"],
}


@router.post("/{election_id}/publish", response_model=ElectionResponse)
async def publish_election(
    election_id: UUID,
    current_user: User = Depends(require_roles(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """Activate/publish an election (draft/scheduled → active)."""
    election_repo = ElectionRepository(Election, db)
    audit_repo = AuditLogRepository(AuditLog, db)
    election = await election_repo.get_by_id(election_id, id_field="election_id")
    if not election:
        raise HTTPException(status_code=404, detail="Election not found")
    _require_ownership(election, current_user)

    if "active" not in VALID_TRANSITIONS.get(election.status, []):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot activate election with status '{election.status}'",
        )

    election_with_categories = await election_repo.get_with_categories(election_id)
    if not election_with_categories.categories:
        raise HTTPException(
            status_code=400,
            detail="Election must have at least one category before it can be activated",
        )
    for category in election_with_categories.categories:
        if not category.candidates:
            raise HTTPException(
                status_code=400,
                detail=f"Category '{category.name}' has no candidates",
            )

    updated = await election_repo.update_status(election_id, "active")

    await audit_repo.log_action(
        action_type="PUBLISH",
        entity_type="Election",
        entity_id=election_id,
        user_id=current_user.user_id,
    )

    return ElectionResponse.model_validate(updated)


@router.post("/{election_id}/schedule", response_model=ElectionResponse)
async def schedule_election(
    election_id: UUID,
    current_user: User = Depends(require_roles(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """Schedule a draft election."""
    election_repo = ElectionRepository(Election, db)
    election = await election_repo.get_by_id(election_id, id_field="election_id")
    if not election:
        raise HTTPException(status_code=404, detail="Election not found")
    _require_ownership(election, current_user)

    if "scheduled" not in VALID_TRANSITIONS.get(election.status, []):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot schedule election with status '{election.status}'",
        )

    updated = await election_repo.update_status(election_id, "scheduled")
    return ElectionResponse.model_validate(updated)


@router.post("/{election_id}/close", response_model=ElectionResponse)
async def close_election(
    election_id: UUID,
    current_user: User = Depends(require_roles(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """Close/complete an active election."""
    election_repo = ElectionRepository(Election, db)
    audit_repo = AuditLogRepository(AuditLog, db)
    election = await election_repo.get_by_id(election_id, id_field="election_id")
    if not election:
        raise HTTPException(status_code=404, detail="Election not found")
    _require_ownership(election, current_user)

    if "completed" not in VALID_TRANSITIONS.get(election.status, []):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot close election with status '{election.status}'",
        )

    updated = await election_repo.update_status(election_id, "completed")

    await audit_repo.log_action(
        action_type="CLOSE",
        entity_type="Election",
        entity_id=election_id,
        user_id=current_user.user_id,
    )

    return ElectionResponse.model_validate(updated)


# =========================================================================
# Candidates
# =========================================================================


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
    _require_ownership(election, current_user)

    if election.status not in ("draft", "scheduled"):
        raise HTTPException(
            status_code=400,
            detail="Cannot add candidates after voting has started",
        )

    category_repo = CategoryRepository(Category, db)
    category = await category_repo.get_by_id(data.category_id, id_field="category_id")
    if not category or category.election_id != election_id:
        raise HTTPException(status_code=400, detail="Invalid category for this election")

    candidate_repo = CandidateRepository(Candidate, db)
    candidate_id = uuid_lib.uuid4()
    short_code = str(candidate_id).replace("-", "")[-4:].upper()
    candidate = await candidate_repo.create(
        {
            **data.model_dump(),
            "candidate_id": candidate_id,
            "short_code": short_code,
            "election_id": election_id,
        }
    )

    # Send nomination email if an address was provided
    if candidate.email:
        # Resolve org name for a nicer email
        org_repo = OrganizationRepository(Organization, db)
        org_list = await org_repo.get_by_owner(current_user.user_id)
        org_name = org_list[0].name if org_list else current_user.full_name

        start_str = election.start_datetime.strftime("%b %d, %Y %H:%M UTC")
        end_str = election.end_datetime.strftime("%b %d, %Y %H:%M UTC")

        subject, html_body = email_service.candidate_nomination_email(
            candidate_name=candidate.name,
            election_title=election.title,
            election_id=str(election_id),
            candidate_email=candidate.email,
            start_datetime=start_str,
            end_datetime=end_str,
            org_name=org_name,
        )
        email_service.send_email(candidate.email, subject, html_body)

    return CandidateResponse.model_validate(candidate)


_ALLOWED_IMAGE_TYPES = {".jpg", ".jpeg", ".png", ".webp"}
_MAX_IMAGE_SIZE = 100 * 1024 * 1024  # 100 MB


@router.post("/{election_id}/candidates/upload-image")
async def upload_candidate_image(
    election_id: UUID,
    file: UploadFile = File(...),
    current_user: User = Depends(require_roles(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """Upload a candidate photo. Returns { image_url } for use in add_candidate."""
    election_repo = ElectionRepository(Election, db)
    election = await election_repo.get_by_id(election_id, id_field="election_id")
    if not election:
        raise HTTPException(status_code=404, detail="Election not found")
    _require_ownership(election, current_user)

    ext = Path(file.filename or "").suffix.lower()
    if ext not in _ALLOWED_IMAGE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type. Allowed: JPG, PNG, WEBP",
        )

    content = await file.read()
    if len(content) > _MAX_IMAGE_SIZE:
        raise HTTPException(status_code=400, detail="Image must be under 100 MB")

    image_url = await file_storage_service.upload(
        content=content,
        filename=file.filename or f"candidate{ext}",
        content_type=file.content_type,
    )

    return {"image_url": image_url}


@router.post("/{election_id}/banner/upload")
async def upload_election_banner(
    election_id: UUID,
    file: UploadFile = File(...),
    current_user: User = Depends(require_roles(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """Upload an election banner. Returns { banner_image_url } for use in create/update."""
    election_repo = ElectionRepository(Election, db)
    election = await election_repo.get_by_id(election_id, id_field="election_id")
    if not election:
        raise HTTPException(status_code=404, detail="Election not found")
    _require_ownership(election, current_user)

    ext = Path(file.filename or "").suffix.lower()
    if ext not in _ALLOWED_IMAGE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type. Allowed: JPG, PNG, WEBP",
        )

    content = await file.read()
    if len(content) > _MAX_IMAGE_SIZE:
        raise HTTPException(status_code=400, detail="Image must be under 100 MB")

    banner_image_url = await file_storage_service.upload(
        content=content,
        filename=file.filename or f"banner{ext}",
        content_type=file.content_type,
    )

    return {"banner_image_url": banner_image_url}


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
    election_repo = ElectionRepository(Election, db)
    election = await election_repo.get_by_id(election_id, id_field="election_id")
    if not election:
        raise HTTPException(status_code=404, detail="Election not found")
    _require_ownership(election, current_user)

    candidate_repo = CandidateRepository(Candidate, db)
    candidate = await candidate_repo.get_by_id(candidate_id, id_field="candidate_id")
    if not candidate or candidate.election_id != election_id:
        raise HTTPException(status_code=404, detail="Candidate not found")

    await candidate_repo.delete(candidate_id, id_field="candidate_id")


@router.put(
    "/{election_id}/candidates/{candidate_id}",
    response_model=CandidateResponse,
)
async def update_candidate(
    election_id: UUID,
    candidate_id: UUID,
    data: CandidateUpdate,
    current_user: User = Depends(require_roles(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    election_repo = ElectionRepository(Election, db)
    election = await election_repo.get_by_id(election_id, id_field="election_id")
    if not election:
        raise HTTPException(status_code=404, detail="Election not found")
    _require_ownership(election, current_user)

    if election.status not in ("draft", "scheduled"):
        raise HTTPException(
            status_code=400,
            detail="Cannot edit candidates after voting has started",
        )

    candidate_repo = CandidateRepository(Candidate, db)
    candidate = await candidate_repo.get_by_id(candidate_id, id_field="candidate_id")
    if not candidate or candidate.election_id != election_id:
        raise HTTPException(status_code=404, detail="Candidate not found")

    update_data = data.model_dump(exclude_unset=True)
    updated = await candidate_repo.update(candidate_id, update_data, id_field="candidate_id")
    return CandidateResponse.model_validate(updated)


# =========================================================================
# Categories — a position/prize within the election (e.g. "President").
# Each category owns its own election_type/allow_abstain and candidates.
# =========================================================================


@router.post("/{election_id}/categories", response_model=CategoryResponse, status_code=201)
async def add_category(
    election_id: UUID,
    data: CategoryCreate,
    current_user: User = Depends(require_roles(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    election_repo = ElectionRepository(Election, db)
    election = await election_repo.get_by_id(election_id, id_field="election_id")
    if not election:
        raise HTTPException(status_code=404, detail="Election not found")
    _require_ownership(election, current_user)

    if election.status not in ("draft", "scheduled"):
        raise HTTPException(
            status_code=400,
            detail="Cannot add categories after voting has started",
        )

    category_repo = CategoryRepository(Category, db)
    category = await category_repo.create(
        {**data.model_dump(), "election_id": election_id}
    )
    return CategoryResponse.model_validate(category)


@router.get("/{election_id}/categories", response_model=List[CategoryWithCandidates])
async def list_categories(
    election_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    category_repo = CategoryRepository(Category, db)
    categories = await category_repo.get_by_election(election_id)
    return [CategoryWithCandidates.model_validate(c) for c in categories]


@router.get("/{election_id}/categories/{category_id}", response_model=CategoryWithCandidates)
async def get_category(
    election_id: UUID,
    category_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    category_repo = CategoryRepository(Category, db)
    category = await category_repo.get_with_candidates(category_id)
    if not category or category.election_id != election_id:
        raise HTTPException(status_code=404, detail="Category not found")
    return CategoryWithCandidates.model_validate(category)


@router.put("/{election_id}/categories/{category_id}", response_model=CategoryResponse)
async def update_category(
    election_id: UUID,
    category_id: UUID,
    data: CategoryUpdate,
    current_user: User = Depends(require_roles(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    election_repo = ElectionRepository(Election, db)
    election = await election_repo.get_by_id(election_id, id_field="election_id")
    if not election:
        raise HTTPException(status_code=404, detail="Election not found")
    _require_ownership(election, current_user)

    if election.status not in ("draft", "scheduled"):
        raise HTTPException(
            status_code=400,
            detail="Cannot edit categories after voting has started",
        )

    category_repo = CategoryRepository(Category, db)
    category = await category_repo.get_by_id(category_id, id_field="category_id")
    if not category or category.election_id != election_id:
        raise HTTPException(status_code=404, detail="Category not found")

    update_data = data.model_dump(exclude_unset=True)
    updated = await category_repo.update(category_id, update_data, id_field="category_id")
    return CategoryResponse.model_validate(updated)


@router.delete("/{election_id}/categories/{category_id}", status_code=204)
async def delete_category(
    election_id: UUID,
    category_id: UUID,
    current_user: User = Depends(require_roles(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    election_repo = ElectionRepository(Election, db)
    election = await election_repo.get_by_id(election_id, id_field="election_id")
    if not election:
        raise HTTPException(status_code=404, detail="Election not found")
    _require_ownership(election, current_user)

    if election.status not in ("draft", "scheduled"):
        raise HTTPException(
            status_code=400,
            detail="Cannot delete categories after voting has started",
        )

    category_repo = CategoryRepository(Category, db)
    category = await category_repo.get_by_id(category_id, id_field="category_id")
    if not category or category.election_id != election_id:
        raise HTTPException(status_code=404, detail="Category not found")

    await category_repo.delete(category_id, id_field="category_id")


# =========================================================================
# Eligible Voters
# =========================================================================


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
    _require_ownership(election, current_user)

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
    election = await election_repo.get_by_id(election_id, id_field="election_id")
    if not election:
        raise HTTPException(status_code=404, detail="Election not found")
    _require_ownership(election, current_user)
    voters = await election_repo.get_eligible_voters(election_id)
    return [EligibleVoterResponse.model_validate(v) for v in voters]


# =========================================================================
# Candidate Results — public OTP access (no account required)
# =========================================================================

from pydantic import BaseModel, EmailStr
from sqlalchemy import select


class CandidateOTPRequest(BaseModel):
    email: EmailStr


class CandidateOTPVerify(BaseModel):
    email: EmailStr
    otp: str


class CandidateResultResponse(BaseModel):
    candidate_id: str
    candidate_name: str
    image_url: Optional[str]
    election_id: str
    election_title: str
    election_status: str
    start_datetime: str
    end_datetime: str
    vote_count: int
    percentage: float
    rank: int
    total_candidates: int
    total_votes: int
    turnout_percentage: float
    access_token: Optional[str] = None


@router.post("/{election_id}/candidate-access/request-otp", status_code=200)
async def request_candidate_otp(
    election_id: UUID,
    data: CandidateOTPRequest,
    db: AsyncSession = Depends(get_db),
):
    """Send an OTP to the candidate's email so they can view their own results."""
    # Find the candidate in this election by email
    result = await db.execute(
        select(Candidate).where(
            Candidate.election_id == election_id,
            Candidate.email == data.email.lower(),
        )
    )
    candidate = result.scalar_one_or_none()
    if not candidate:
        # Return same message to avoid email enumeration
        return {"message": "If that email is registered as a candidate, a code has been sent."}

    # Invalidate any previous unused OTPs for this candidate+election
    prev = await db.execute(
        select(CandidateAccessOTP).where(
            CandidateAccessOTP.election_id == election_id,
            CandidateAccessOTP.candidate_id == candidate.candidate_id,
            CandidateAccessOTP.used == False,
        )
    )
    for old in prev.scalars().all():
        old.used = True
    await db.flush()

    expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
    otp_record = CandidateAccessOTP(
        election_id=election_id,
        candidate_id=candidate.candidate_id,
        email=data.email.lower(),
        expires_at=expires_at,
    )
    db.add(otp_record)
    await db.flush()
    await db.refresh(otp_record)

    subject, html_body = email_service.candidate_otp_email(
        candidate_name=candidate.name,
        otp_code=otp_record.otp_code,
        election_title=(
            await db.execute(
                select(Election.title).where(Election.election_id == election_id)
            )
        ).scalar_one_or_none() or "the election",
    )
    email_service.send_email(data.email, subject, html_body)

    return {"message": "If that email is registered as a candidate, a code has been sent."}


async def _get_candidate_standing(db: AsyncSession, election, candidate: Candidate) -> dict:
    """A candidate's rank/vote count/turnout, scoped to their OWN category —
    ranking across the whole election would be meaningless once categories
    can have entirely different candidate pools."""
    category = next(
        (c for c in election.categories if c.category_id == candidate.category_id),
        None,
    )
    if category is None:
        raise HTTPException(status_code=404, detail="Candidate's category not found")

    from app.repositories.vote_repository import VoteRepository
    from app.models.vote import Vote
    from app.services.cryptography_service import CryptographyService

    vote_repo = VoteRepository(Vote, db)
    all_votes = await vote_repo.get_votes_by_election(election.election_id)
    category_votes = [v for v in all_votes if v.category_id == category.category_id]
    crypto = CryptographyService()

    candidate_counts: dict = {}
    for vote in category_votes:
        try:
            decrypted = crypto.decrypt_vote_data(vote.vote_data)
            weight = getattr(vote, "count", 1)
            for cid_str in decrypted.get("candidate_ids", []):
                candidate_counts[cid_str] = candidate_counts.get(cid_str, 0) + weight
        except Exception:
            pass

    total_votes = sum(getattr(v, "count", 1) for v in category_votes)
    my_count = candidate_counts.get(str(candidate.candidate_id), 0)
    my_pct = round((my_count / total_votes * 100) if total_votes > 0 else 0, 2)

    all_counts = [candidate_counts.get(str(c.candidate_id), 0) for c in category.candidates]
    all_counts.sort(reverse=True)
    rank = all_counts.index(my_count) + 1

    election_repo = ElectionRepository(Election, db)
    total_eligible = await election_repo.count_eligible_voters(election.election_id)
    turnout = round((total_votes / total_eligible * 100) if total_eligible > 0 else 0, 2)

    return {
        "vote_count": my_count,
        "percentage": my_pct,
        "rank": rank,
        "total_candidates": len(category.candidates),
        "total_votes": total_votes,
        "turnout_percentage": turnout,
    }


@router.post("/{election_id}/candidate-access/verify-otp", response_model=CandidateResultResponse)
async def verify_candidate_otp(
    election_id: UUID,
    data: CandidateOTPVerify,
    db: AsyncSession = Depends(get_db),
):
    """Verify OTP and return the candidate's own performance data."""
    result = await db.execute(
        select(CandidateAccessOTP).where(
            CandidateAccessOTP.election_id == election_id,
            CandidateAccessOTP.email == data.email.lower(),
            CandidateAccessOTP.otp_code == data.otp.strip(),
            CandidateAccessOTP.used == False,
        )
    )
    otp_record = result.scalar_one_or_none()

    if not otp_record:
        raise HTTPException(status_code=400, detail="Invalid or expired code")
    if otp_record.expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="Code has expired, please request a new one")

    # Mark used
    otp_record.used = True
    await db.flush()

    # Fetch candidate + election
    cand_result = await db.execute(
        select(Candidate).where(Candidate.candidate_id == otp_record.candidate_id)
    )
    candidate = cand_result.scalar_one()

    election_repo = ElectionRepository(Election, db)
    election = await election_repo.get_with_categories(election_id)

    standing = await _get_candidate_standing(db, election, candidate)

    access_token = create_candidate_result_token(
        candidate_id=str(candidate.candidate_id),
        election_id=str(election_id),
        election_end=election.end_datetime,
    )

    results_url = (
        f"{settings.FRONTEND_URL}/candidate/results"
        f"?election_id={election_id}&token={access_token}"
    )
    subj, body = email_service.candidate_result_link_email(
        candidate_name=candidate.name,
        election_title=election.title,
        results_url=results_url,
    )
    email_service.send_email(data.email, subj, body)

    return CandidateResultResponse(
        candidate_id=str(candidate.candidate_id),
        candidate_name=candidate.name,
        image_url=candidate.image_url,
        election_id=str(election_id),
        election_title=election.title,
        election_status=election.status,
        start_datetime=election.start_datetime.isoformat(),
        end_datetime=election.end_datetime.isoformat(),
        access_token=access_token,
        **standing,
    )


@router.get("/{election_id}/candidate-access/results", response_model=CandidateResultResponse)
async def get_candidate_results_by_token(
    election_id: UUID,
    token: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """Token-based access — no OTP needed (link emailed after first verification)."""
    payload = decode_token(token)
    if not payload or payload.get("type") != "candidate_result":
        raise HTTPException(status_code=401, detail="Invalid or expired results link")

    token_election_id = payload.get("election_id")
    if token_election_id != str(election_id):
        raise HTTPException(status_code=403, detail="Token does not match this election")

    candidate_id = payload.get("sub")
    cand_result = await db.execute(
        select(Candidate).where(Candidate.candidate_id == candidate_id)
    )
    candidate = cand_result.scalar_one_or_none()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")

    election_repo = ElectionRepository(Election, db)
    election = await election_repo.get_with_categories(election_id)
    if not election:
        raise HTTPException(status_code=404, detail="Election not found")

    standing = await _get_candidate_standing(db, election, candidate)

    return CandidateResultResponse(
        candidate_id=str(candidate.candidate_id),
        candidate_name=candidate.name,
        image_url=candidate.image_url,
        election_id=str(election_id),
        election_title=election.title,
        election_status=election.status,
        start_datetime=election.start_datetime.isoformat(),
        end_datetime=election.end_datetime.isoformat(),
        access_token=token,
        **standing,
    )
