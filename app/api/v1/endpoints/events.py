from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_current_active_user, require_roles
from app.core.security import create_ticket_scan_token
from app.db.base import get_db
from app.models.audit_log import AuditLog
from app.models.event import Event, TicketType
from app.models.organization import Organization
from app.models.user import User
from app.repositories.audit_log_repository import AuditLogRepository
from app.repositories.event_repository import EventRepository, TicketTypeRepository
from app.repositories.organization_repository import OrganizationRepository
from app.services.file_storage_service import file_storage_service
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


async def _owns_event(event, current_user: User, db: AsyncSession) -> bool:
    """The creator, a System Administrator, or an organization teammate
    holding a managing role. Events carry no org_id, so organization access
    is resolved through shared membership with the creator."""
    user_roles = [ur.role.role_name for ur in current_user.user_roles]
    if event.created_by == current_user.user_id or SYSTEM_ADMIN in user_roles:
        return True
    return await OrganizationRepository(Organization, db).can_manage_with(
        current_user.user_id, event.created_by
    )


async def _require_event_ownership(event, current_user: User, db: AsyncSession) -> None:
    if not await _owns_event(event, current_user, db):
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

    event = await event_repo.create(
        {**data.model_dump(), "created_by": current_user.user_id}
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
    """Events belonging to the caller's organization.

    Scoped to the caller plus their teammates, not the caller alone — an
    added member created nothing, and scoping to the individual made them
    look like they were in an organization of one.
    """
    event_repo = EventRepository(Event, db)
    teammate_ids = await OrganizationRepository(Organization, db).get_teammate_ids(
        current_user.user_id
    )
    events = await event_repo.get_events_by_creators(
        teammate_ids, skip=skip, limit=limit
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


@router.get("/{event_id}", response_model=EventWithTicketTypes)
async def get_event(
    event_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    event_repo = EventRepository(Event, db)
    event = await event_repo.get_with_ticket_types(event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    return EventWithTicketTypes(
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
        show_ticket_counts=event.show_ticket_counts,
        created_by=event.created_by,
        created_at=event.created_at,
        updated_at=event.updated_at,
        ticket_types=[TicketTypeResponse.model_validate(tt) for tt in event.ticket_types],
    )


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
    await _require_event_ownership(event, current_user, db)

    update_data = data.model_dump(exclude_unset=True)
    updated = await event_repo.update(event_id, update_data, id_field="event_id")
    return EventResponse.model_validate(updated)


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
    await _require_event_ownership(event, current_user, db)

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
    await _require_event_ownership(event, current_user, db)
    if event.status != "draft":
        raise HTTPException(status_code=400, detail="Only draft events can be published")

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
    await _require_event_ownership(event, current_user, db)

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
    await _require_event_ownership(event, current_user, db)

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
    await _require_event_ownership(event, current_user, db)

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
    await _require_event_ownership(event, current_user, db)

    tt_repo = TicketTypeRepository(TicketType, db)
    tt = await tt_repo.get_by_id(type_id, id_field="ticket_type_id")
    if not tt or tt.event_id != event_id:
        raise HTTPException(status_code=404, detail="Ticket type not found")

    update_data = data.model_dump(exclude_unset=True)
    updated = await tt_repo.update(type_id, update_data, id_field="ticket_type_id")
    return TicketTypeResponse.model_validate(updated)
