from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_current_active_user, require_roles
from app.core.security import create_ticket_scan_token
from app.core.slug import generate_slug
from app.db.base import get_db
from app.models.audit_log import AuditLog
from app.models.election import Candidate, Category
from app.models.event import Event, TicketType
from app.models.user import User
from app.repositories.audit_log_repository import AuditLogRepository
from app.repositories.election_repository import CandidateRepository, CategoryRepository
from app.repositories.event_repository import EventRepository, TicketTypeRepository
from app.services.file_storage_service import file_storage_service
from app.schemas.election import (
    CandidateCreate,
    CandidateResponse,
    CandidateUpdate,
    CategoryCreate,
    CategoryResponse,
    CategoryUpdate,
    CategoryWithCandidates,
)
from app.schemas.event import (
    EventCreate,
    EventResponse,
    EventUpdate,
    EventWithTicketTypes,
    TicketTypeCreate,
    TicketTypeResponse,
    TicketTypeUpdate,
)
from app.schemas.ticket import TicketScanTokenResponse

router = APIRouter(prefix="/events", tags=["Events"])

ORGANIZER_ROLES = ("System Administrator", "Event Organizer")
SYSTEM_ADMIN = "System Administrator"

ALLOWED_IMAGE_TYPES = {".jpg", ".jpeg", ".png", ".webp"}
MAX_IMAGE_SIZE = 100 * 1024 * 1024  # 100 MB


def _owns_event(event, current_user: User) -> bool:
    user_roles = [ur.role.role_name for ur in current_user.user_roles]
    return event.created_by == current_user.user_id or SYSTEM_ADMIN in user_roles


def _require_event_ownership(event, current_user: User) -> None:
    if not _owns_event(event, current_user):
        raise HTTPException(status_code=403, detail="You do not have access to this event")


@router.get("/user/{user_id}", response_model=List[EventResponse])
async def list_user_public_events(
    user_id: UUID,
    db: AsyncSession = Depends(get_db),
    skip: int = 0,
    limit: int = 50,
):
    """List published events by a specific user. No auth required."""
    event_repo = EventRepository(Event, db)
    events = await event_repo.get_events_by_creator(user_id, skip=skip, limit=limit)
    public = [e for e in events if e.status == "published"]
    return [EventResponse.model_validate(e) for e in public]


@router.post("", response_model=EventResponse, status_code=201)
async def create_event(
    data: EventCreate,
    current_user: User = Depends(require_roles(*ORGANIZER_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    event_repo = EventRepository(Event, db)
    audit_repo = AuditLogRepository(AuditLog, db)

    create_data = data.model_dump()
    create_data["slug"] = create_data.get("slug") or generate_slug(data.title)
    event = await event_repo.create(
        {**create_data, "created_by": current_user.user_id}
    )

    await audit_repo.log_action(
        action_type="CREATE",
        entity_type="Event",
        entity_id=event.event_id,
        user_id=current_user.user_id,
    )

    return EventResponse.model_validate(event)


@router.get("", response_model=List[EventResponse])
async def list_events(
    db: AsyncSession = Depends(get_db),
    skip: int = 0,
    limit: int = 20,
    category: Optional[str] = None,
    current_user: User = Depends(require_roles(*ORGANIZER_ROLES)),
):
    """Return only the events created by the current user."""
    event_repo = EventRepository(Event, db)
    events = await event_repo.get_events_by_creator(
        current_user.user_id, skip=skip, limit=limit
    )
    return [EventResponse.model_validate(e) for e in events]


@router.get("/public", response_model=List[EventResponse])
async def list_public_events(
    db: AsyncSession = Depends(get_db),
    skip: int = 0,
    limit: int = 20,
    category: Optional[str] = None,
):
    """List all published events. No authentication required.

    Must stay registered before GET /{event_id} — otherwise FastAPI matches
    this path against that route first and tries (and fails) to parse
    "public" as a UUID.
    """
    event_repo = EventRepository(Event, db)
    events = await event_repo.get_published_events(skip=skip, limit=limit, category=category)
    return [EventResponse.model_validate(e) for e in events]


def _build_event_with_ticket_types(event) -> EventWithTicketTypes:
    return EventWithTicketTypes(
        event_id=event.event_id,
        title=event.title,
        slug=event.slug,
        description=event.description,
        event_date=event.event_date,
        event_time=event.event_time,
        location=event.location,
        category=event.category,
        latitude=event.latitude,
        longitude=event.longitude,
        capacity=event.capacity,
        banner_image_url=event.banner_image_url,
        status=event.status,
        show_ticket_counts=event.show_ticket_counts,
        vote_price=event.vote_price,
        allow_revoting=event.allow_revoting,
        created_by=event.created_by,
        created_at=event.created_at,
        updated_at=event.updated_at,
        ticket_types=[TicketTypeResponse.model_validate(tt) for tt in event.ticket_types],
    )


@router.get("/{event_id}", response_model=EventWithTicketTypes)
async def get_event(
    event_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    event_repo = EventRepository(Event, db)
    event = await event_repo.get_with_ticket_types(event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    return _build_event_with_ticket_types(event)


@router.get("/public/by-slug/{slug}", response_model=EventWithTicketTypes)
async def get_public_event_by_slug(
    slug: str,
    db: AsyncSession = Depends(get_db),
):
    """Same as get_event, keyed by slug — for slug-based routing (v2)."""
    event_repo = EventRepository(Event, db)
    event = await event_repo.get_by_slug(slug)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    return _build_event_with_ticket_types(event)


@router.put("/{event_id}", response_model=EventResponse)
async def update_event(
    event_id: UUID,
    data: EventUpdate,
    current_user: User = Depends(require_roles(*ORGANIZER_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    event_repo = EventRepository(Event, db)
    event = await event_repo.get_by_id(event_id, id_field="event_id")
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    _require_event_ownership(event, current_user)

    update_data = data.model_dump(exclude_unset=True)
    updated = await event_repo.update(event_id, update_data, id_field="event_id")
    return EventResponse.model_validate(updated)


@router.delete("/{event_id}", status_code=204)
async def delete_event(
    event_id: UUID,
    current_user: User = Depends(require_roles(*ORGANIZER_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    event_repo = EventRepository(Event, db)
    event = await event_repo.get_by_id(event_id, id_field="event_id")
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    _require_event_ownership(event, current_user)

    if event.status != "draft":
        raise HTTPException(
            status_code=400, detail="Only draft events can be deleted"
        )

    await event_repo.delete(event_id, id_field="event_id")


@router.post("/{event_id}/banner/upload")
async def upload_event_banner(
    event_id: UUID,
    file: UploadFile = File(...),
    current_user: User = Depends(require_roles(*ORGANIZER_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """Upload an event banner. Returns { banner_image_url } for use in create/update."""
    event_repo = EventRepository(Event, db)
    event = await event_repo.get_by_id(event_id, id_field="event_id")
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    _require_event_ownership(event, current_user)

    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(
            status_code=400,
            detail="Unsupported file type. Allowed: JPG, PNG, WEBP",
        )

    content = await file.read()
    if len(content) > MAX_IMAGE_SIZE:
        raise HTTPException(status_code=400, detail="Image must be under 100 MB")

    banner_image_url = await file_storage_service.upload(
        content=content,
        filename=file.filename or f"banner{ext}",
        content_type=file.content_type,
    )

    return {"banner_image_url": banner_image_url}


@router.post("/{event_id}/publish")
async def publish_event(
    event_id: UUID,
    current_user: User = Depends(require_roles(*ORGANIZER_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    event_repo = EventRepository(Event, db)
    event = await event_repo.get_by_id(event_id, id_field="event_id")
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    _require_event_ownership(event, current_user)
    if event.status != "draft":
        raise HTTPException(status_code=400, detail="Only draft events can be published")

    # Unlike elections, categories are optional for events (many events are
    # pure ticketing, no voting at all) — but any category that does exist
    # must have candidates before it can go live.
    event_with_categories = await event_repo.get_with_categories(event_id)
    for category in event_with_categories.categories:
        if not category.candidates:
            raise HTTPException(
                status_code=400,
                detail=f"Category '{category.name}' has no candidates",
            )

    await event_repo.update_status(event_id, "published")
    return {"message": "Event published successfully"}


@router.post("/{event_id}/cancel")
async def cancel_event(
    event_id: UUID,
    current_user: User = Depends(require_roles(*ORGANIZER_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    event_repo = EventRepository(Event, db)
    event = await event_repo.get_by_id(event_id, id_field="event_id")
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    _require_event_ownership(event, current_user)

    await event_repo.update_status(event_id, "cancelled")

    await AuditLogRepository(AuditLog, db).log_action(
        action_type="CANCEL",
        entity_type="Event",
        entity_id=event_id,
        user_id=current_user.user_id,
    )

    return {"message": "Event cancelled"}


@router.get("/{event_id}/scan-token", response_model=TicketScanTokenResponse)
async def get_event_scan_token(
    event_id: UUID,
    current_user: User = Depends(require_roles(*ORGANIZER_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """Mint (or re-mint — it's deterministic per event/day) a no-auth
    check-in link the organizer can hand to volunteers who have no
    account. Valid until the end of the event's calendar day."""
    event_repo = EventRepository(Event, db)
    event = await event_repo.get_by_id(event_id, id_field="event_id")
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    _require_event_ownership(event, current_user)

    expires_at = datetime.combine(
        event.event_date + timedelta(days=1), time.min
    ).replace(tzinfo=timezone.utc)
    scan_token = create_ticket_scan_token(str(event.event_id), expires_at)
    return TicketScanTokenResponse(scan_token=scan_token, expires_at=expires_at)


# --- Ticket Types ---


@router.post(
    "/{event_id}/ticket-types",
    response_model=TicketTypeResponse,
    status_code=201,
)
async def create_ticket_type(
    event_id: UUID,
    data: TicketTypeCreate,
    current_user: User = Depends(require_roles(*ORGANIZER_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    event_repo = EventRepository(Event, db)
    event = await event_repo.get_by_id(event_id, id_field="event_id")
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    _require_event_ownership(event, current_user)

    tt_repo = TicketTypeRepository(TicketType, db)
    ticket_type = await tt_repo.create(
        {**data.model_dump(), "event_id": event_id}
    )
    return TicketTypeResponse.model_validate(ticket_type)


@router.get(
    "/{event_id}/ticket-types", response_model=List[TicketTypeResponse]
)
async def list_ticket_types(
    event_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    tt_repo = TicketTypeRepository(TicketType, db)
    types = await tt_repo.get_by_event(event_id)
    return [TicketTypeResponse.model_validate(tt) for tt in types]


@router.put(
    "/{event_id}/ticket-types/{type_id}",
    response_model=TicketTypeResponse,
)
async def update_ticket_type(
    event_id: UUID,
    type_id: UUID,
    data: TicketTypeUpdate,
    current_user: User = Depends(require_roles(*ORGANIZER_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    event_repo = EventRepository(Event, db)
    event = await event_repo.get_by_id(event_id, id_field="event_id")
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    _require_event_ownership(event, current_user)

    tt_repo = TicketTypeRepository(TicketType, db)
    tt = await tt_repo.get_by_id(type_id, id_field="ticket_type_id")
    if not tt or tt.event_id != event_id:
        raise HTTPException(status_code=404, detail="Ticket type not found")

    update_data = data.model_dump(exclude_unset=True)
    updated = await tt_repo.update(type_id, update_data, id_field="ticket_type_id")
    return TicketTypeResponse.model_validate(updated)


@router.delete(
    "/{event_id}/ticket-types/{type_id}",
    status_code=204,
)
async def delete_ticket_type(
    event_id: UUID,
    type_id: UUID,
    current_user: User = Depends(require_roles(*ORGANIZER_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    event_repo = EventRepository(Event, db)
    event = await event_repo.get_by_id(event_id, id_field="event_id")
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    _require_event_ownership(event, current_user)

    tt_repo = TicketTypeRepository(TicketType, db)
    tt = await tt_repo.get_by_id(type_id, id_field="ticket_type_id")
    if not tt or tt.event_id != event_id:
        raise HTTPException(status_code=404, detail="Ticket type not found")

    if tt.quantity_sold > 0:
        raise HTTPException(
            status_code=400,
            detail="Cannot delete a ticket type that already has sales",
        )

    await tt_repo.delete(type_id, id_field="ticket_type_id")


# =========================================================================
# Categories — voting categories/prizes for the event (e.g. "Best Dressed"),
# independent of ticket sales. Mirrors the /elections/{id}/categories block.
# =========================================================================


@router.post("/{event_id}/categories", response_model=CategoryResponse, status_code=201)
async def add_event_category(
    event_id: UUID,
    data: CategoryCreate,
    current_user: User = Depends(require_roles(*ORGANIZER_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    event_repo = EventRepository(Event, db)
    event = await event_repo.get_by_id(event_id, id_field="event_id")
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    _require_event_ownership(event, current_user)

    if event.status != "draft":
        raise HTTPException(
            status_code=400, detail="Cannot add categories after the event is published"
        )

    category_repo = CategoryRepository(Category, db)
    category = await category_repo.create({**data.model_dump(), "event_id": event_id})
    return CategoryResponse.model_validate(category)


@router.get("/{event_id}/categories", response_model=List[CategoryWithCandidates])
async def list_event_categories(
    event_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """No auth required — event categories/candidates are always public."""
    category_repo = CategoryRepository(Category, db)
    categories = await category_repo.get_by_event(event_id)
    return [CategoryWithCandidates.model_validate(c) for c in categories]


@router.get("/{event_id}/categories/{category_id}", response_model=CategoryWithCandidates)
async def get_event_category(
    event_id: UUID,
    category_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    category_repo = CategoryRepository(Category, db)
    category = await category_repo.get_with_candidates(category_id)
    if not category or category.event_id != event_id:
        raise HTTPException(status_code=404, detail="Category not found")
    return CategoryWithCandidates.model_validate(category)


@router.put("/{event_id}/categories/{category_id}", response_model=CategoryResponse)
async def update_event_category(
    event_id: UUID,
    category_id: UUID,
    data: CategoryUpdate,
    current_user: User = Depends(require_roles(*ORGANIZER_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    event_repo = EventRepository(Event, db)
    event = await event_repo.get_by_id(event_id, id_field="event_id")
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    _require_event_ownership(event, current_user)

    if event.status != "draft":
        raise HTTPException(
            status_code=400, detail="Cannot edit categories after the event is published"
        )

    category_repo = CategoryRepository(Category, db)
    category = await category_repo.get_by_id(category_id, id_field="category_id")
    if not category or category.event_id != event_id:
        raise HTTPException(status_code=404, detail="Category not found")

    update_data = data.model_dump(exclude_unset=True)
    updated = await category_repo.update(category_id, update_data, id_field="category_id")
    return CategoryResponse.model_validate(updated)


@router.delete("/{event_id}/categories/{category_id}", status_code=204)
async def delete_event_category(
    event_id: UUID,
    category_id: UUID,
    current_user: User = Depends(require_roles(*ORGANIZER_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    event_repo = EventRepository(Event, db)
    event = await event_repo.get_by_id(event_id, id_field="event_id")
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    _require_event_ownership(event, current_user)

    if event.status != "draft":
        raise HTTPException(
            status_code=400, detail="Cannot delete categories after the event is published"
        )

    category_repo = CategoryRepository(Category, db)
    category = await category_repo.get_by_id(category_id, id_field="category_id")
    if not category or category.event_id != event_id:
        raise HTTPException(status_code=404, detail="Category not found")

    await category_repo.delete(category_id, id_field="category_id")


# =========================================================================
# Candidates — nominees within one of the event's categories.
# =========================================================================


@router.post("/{event_id}/candidates", response_model=CandidateResponse, status_code=201)
async def add_event_candidate(
    event_id: UUID,
    data: CandidateCreate,
    current_user: User = Depends(require_roles(*ORGANIZER_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    event_repo = EventRepository(Event, db)
    event = await event_repo.get_by_id(event_id, id_field="event_id")
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    _require_event_ownership(event, current_user)

    if event.status != "draft":
        raise HTTPException(
            status_code=400, detail="Cannot add candidates after the event is published"
        )

    category_repo = CategoryRepository(Category, db)
    category = await category_repo.get_by_id(data.category_id, id_field="category_id")
    if not category or category.event_id != event_id:
        raise HTTPException(status_code=400, detail="Invalid category for this event")

    candidate_repo = CandidateRepository(Candidate, db)
    import uuid as uuid_lib
    candidate_id = uuid_lib.uuid4()
    short_code = str(candidate_id).replace("-", "")[-4:].upper()
    candidate = await candidate_repo.create(
        {
            **data.model_dump(),
            "candidate_id": candidate_id,
            "short_code": short_code,
            "event_id": event_id,
        }
    )
    return CandidateResponse.model_validate(candidate)


@router.get("/{event_id}/candidates", response_model=List[CandidateResponse])
async def list_event_candidates(
    event_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    candidate_repo = CandidateRepository(Candidate, db)
    candidates = await candidate_repo.get_by_event(event_id)
    return [CandidateResponse.model_validate(c) for c in candidates]


@router.put("/{event_id}/candidates/{candidate_id}", response_model=CandidateResponse)
async def update_event_candidate(
    event_id: UUID,
    candidate_id: UUID,
    data: CandidateUpdate,
    current_user: User = Depends(require_roles(*ORGANIZER_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    event_repo = EventRepository(Event, db)
    event = await event_repo.get_by_id(event_id, id_field="event_id")
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    _require_event_ownership(event, current_user)

    if event.status != "draft":
        raise HTTPException(
            status_code=400, detail="Cannot edit candidates after the event is published"
        )

    candidate_repo = CandidateRepository(Candidate, db)
    candidate = await candidate_repo.get_by_id(candidate_id, id_field="candidate_id")
    if not candidate or candidate.event_id != event_id:
        raise HTTPException(status_code=404, detail="Candidate not found")

    update_data = data.model_dump(exclude_unset=True)
    updated = await candidate_repo.update(candidate_id, update_data, id_field="candidate_id")
    return CandidateResponse.model_validate(updated)


@router.delete("/{event_id}/candidates/{candidate_id}", status_code=204)
async def remove_event_candidate(
    event_id: UUID,
    candidate_id: UUID,
    current_user: User = Depends(require_roles(*ORGANIZER_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    event_repo = EventRepository(Event, db)
    event = await event_repo.get_by_id(event_id, id_field="event_id")
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    _require_event_ownership(event, current_user)

    candidate_repo = CandidateRepository(Candidate, db)
    candidate = await candidate_repo.get_by_id(candidate_id, id_field="candidate_id")
    if not candidate or candidate.event_id != event_id:
        raise HTTPException(status_code=404, detail="Candidate not found")

    await candidate_repo.delete(candidate_id, id_field="candidate_id")
