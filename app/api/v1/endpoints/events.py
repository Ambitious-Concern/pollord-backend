from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_current_active_user, require_roles
from app.db.base import get_db
from app.models.audit_log import AuditLog
from app.models.event import Event, TicketType
from app.models.user import User
from app.repositories.audit_log_repository import AuditLogRepository
from app.repositories.event_repository import EventRepository, TicketTypeRepository
from app.schemas.event import (
    EventCreate,
    EventResponse,
    EventUpdate,
    EventWithTicketTypes,
    TicketTypeCreate,
    TicketTypeResponse,
    TicketTypeUpdate,
)

router = APIRouter(prefix="/events", tags=["Events"])

ORGANIZER_ROLES = ("System Administrator", "Event Organizer")


@router.post("/", response_model=EventResponse, status_code=201)
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


@router.get("/", response_model=List[EventResponse])
async def list_events(
    db: AsyncSession = Depends(get_db),
    skip: int = 0,
    limit: int = 20,
    category: Optional[str] = None,
    current_user: User = Depends(get_current_active_user),
):
    event_repo = EventRepository(Event, db)
    user_roles = [ur.role.role_name for ur in current_user.user_roles]

    if any(r in ORGANIZER_ROLES for r in user_roles):
        events = await event_repo.get_all(skip=skip, limit=limit)
    else:
        events = await event_repo.get_published_events(skip, limit, category)

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

    update_data = data.model_dump(exclude_unset=True)
    updated = await event_repo.update(event_id, update_data, id_field="event_id")
    return EventResponse.model_validate(updated)


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

    await event_repo.update_status(event_id, "cancelled")

    await AuditLogRepository(AuditLog, db).log_action(
        action_type="CANCEL",
        entity_type="Event",
        entity_id=event_id,
        user_id=current_user.user_id,
    )

    return {"message": "Event cancelled"}


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
    tt_repo = TicketTypeRepository(TicketType, db)
    tt = await tt_repo.get_by_id(type_id, id_field="ticket_type_id")
    if not tt or tt.event_id != event_id:
        raise HTTPException(status_code=404, detail="Ticket type not found")

    update_data = data.model_dump(exclude_unset=True)
    updated = await tt_repo.update(type_id, update_data, id_field="ticket_type_id")
    return TicketTypeResponse.model_validate(updated)
