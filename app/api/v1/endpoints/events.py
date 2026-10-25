from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.dependencies import get_current_active_user, require_roles
from app.core.ownership import owns_event as _owns_event
from app.core.ownership import require_event_ownership as _require_event_ownership
from app.core.request_meta import client_ip_and_agent
from app.core.security import create_ticket_scan_token
from app.core.slug import generate_slug
from app.core.ussd import generate_unique_ussd_code
from app.db.base import get_db
from app.db.tenant import get_tenant_db
from app.models.audit_log import AuditLog
from app.models.election import Candidate, Category
from app.models.event import Event, TicketType
from app.models.org_activity_log import OrgActivityLog
from app.models.organization import Organization
from app.models.platform_setting import PlatformSetting
from app.models.user import User
from app.repositories.audit_log_repository import AuditLogRepository
from app.repositories.election_repository import CandidateRepository, CategoryRepository
from app.repositories.event_repository import EventRepository, TicketTypeRepository
from app.repositories.org_activity_log_repository import OrgActivityLogRepository
from app.repositories.organization_repository import OrganizationRepository
from app.services.file_storage_service import file_storage_service
from app.services.org_notification_service import OrgNotificationService
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
    EventSummaryResponse,
    EventUpdate,
    EventWithTicketTypes,
    TicketTypeCreate,
    TicketTypeResponse,
    TicketTypeUpdate,
)
from app.schemas.ticket import TicketScanTokenResponse
from app.services.event_summary_service import EventSummaryService

router = APIRouter(prefix="/events", tags=["Events"])

ORGANIZER_ROLES = ("System Administrator", "Event Organizer")

ALLOWED_IMAGE_TYPES = {".jpg", ".jpeg", ".png", ".webp"}
MAX_IMAGE_SIZE = 100 * 1024 * 1024  # 100 MB

# The check-in link used to die at midnight UTC on the event's own date, which
# broke it in two ways: an event running past midnight lost its scanner
# mid-event (Ghana is UTC+0, so local midnight is the cutoff), and the link for
# any past event was permanently dead — no late check-ins, no fixing the
# register the morning after. The grace window covers both.
SCAN_LINK_GRACE_DAYS = 7


def scan_link_expiry(event_date: date) -> datetime:
    """End of the day `SCAN_LINK_GRACE_DAYS` after the event."""
    return datetime.combine(
        event_date + timedelta(days=SCAN_LINK_GRACE_DAYS + 1), time.min
    ).replace(tzinfo=timezone.utc)




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
    request: Request,
    data: EventCreate,
    current_user: User = Depends(require_roles(*ORGANIZER_ROLES)),
    db: AsyncSession = Depends(get_tenant_db),
):
    """Create an event, auto-generating a slug and USSD code."""
    org_repo = OrganizationRepository(Organization, db)
    if not await org_repo.can_create(current_user.user_id):
        raise HTTPException(
            status_code=403,
            detail=(
                "Your organization role is view-only. Ask an owner or admin "
                "to give you edit access."
            ),
        )

    event_repo = EventRepository(Event, db)
    audit_repo = AuditLogRepository(AuditLog, db)

    create_data = data.model_dump()
    create_data["slug"] = create_data.get("slug") or generate_slug(data.title)
    create_data["ussd_code"] = await generate_unique_ussd_code(db)
    event = await event_repo.create(
        {
            **create_data,
            "created_by": current_user.user_id,
            "org_id": await org_repo.get_primary_org_id_for_user(current_user.user_id),
        }
    )

    await audit_repo.log_action(
        action_type="CREATE",
        entity_type="Event",
        entity_id=event.event_id,
        user_id=current_user.user_id,
    )
    if event.org_id is not None:
        ip, agent = client_ip_and_agent(request)
        await OrgActivityLogRepository(OrgActivityLog, db).log_activity(
            org_id=event.org_id,
            category="event",
            verb="created event",
            target_name=event.title,
            user_id=current_user.user_id,
            ip_address=ip,
            user_agent=agent,
        )

    return EventResponse.model_validate(event)


@router.get("", response_model=List[EventSummaryResponse])
async def list_events(
    db: AsyncSession = Depends(get_tenant_db),
    skip: int = 0,
    limit: int = 20,
    category: Optional[str] = None,
    current_user: User = Depends(require_roles(*ORGANIZER_ROLES)),
):
    """Events belonging to the caller's organization, with the grid's
    aggregate stats (tickets sold/capacity/revenue) bulk-computed alongside.

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
    summaries = await EventSummaryService(db).get_summaries([e.event_id for e in events])

    responses = []
    for e in events:
        resp = EventSummaryResponse.model_validate(e)
        stats = summaries[e.event_id]
        resp.tickets_sold = stats.tickets_sold
        resp.ticket_capacity = stats.capacity
        resp.lowest_ticket_price = stats.lowest_ticket_price
        resp.has_voting = stats.has_voting
        resp.revenue_ghs = stats.revenue_ghs
        resp.revenue_growth_pct = stats.revenue_growth_pct
        responses.append(resp)
    return responses


@router.get("/public", response_model=List[EventResponse])
async def list_public_events(
    db: AsyncSession = Depends(get_db),
    skip: int = 0,
    limit: int = 20,
    category: Optional[str] = None,
    search: Optional[str] = None,
):
    """List all published (and cancelled — see get_published_events) events. No authentication required.

    Must stay registered before GET /{event_id} — otherwise FastAPI matches
    this path against that route first and tries (and fails) to parse
    "public" as a UUID.
    """
    event_repo = EventRepository(Event, db)
    events = await event_repo.get_published_events(skip=skip, limit=limit, category=category, search=search)
    return [EventResponse.model_validate(e) for e in events]


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


async def _build_event_with_ticket_types(event, db: AsyncSession) -> EventWithTicketTypes:
    """Shared response builder for get_event and get_public_event_by_slug."""
    global_vote_price = await _get_global_vote_price(db)
    return EventWithTicketTypes(
        event_id=event.event_id,
        title=event.title,
        slug=event.slug,
        ussd_code=event.ussd_code,
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
        voting_starts_at=event.voting_starts_at,
        voting_ends_at=event.voting_ends_at,
        scan_enabled=event.scan_enabled,
        created_by=event.created_by,
        created_at=event.created_at,
        updated_at=event.updated_at,
        ticket_types=[TicketTypeResponse.model_validate(tt) for tt in event.ticket_types],
        effective_vote_price=event.vote_price if event.vote_price is not None else global_vote_price,
    )


@router.get("/{event_id}", response_model=EventWithTicketTypes)
async def get_event(
    event_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Full event detail including ticket types. No auth required."""
    event_repo = EventRepository(Event, db)
    event = await event_repo.get_with_ticket_types(event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    return await _build_event_with_ticket_types(event, db)


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
    return await _build_event_with_ticket_types(event, db)


@router.put("/{event_id}", response_model=EventResponse)
async def update_event(
    request: Request,
    event_id: UUID,
    data: EventUpdate,
    current_user: User = Depends(require_roles(*ORGANIZER_ROLES)),
    db: AsyncSession = Depends(get_tenant_db),
):
    """Partial update of an event's own fields."""
    event_repo = EventRepository(Event, db)
    event = await event_repo.get_by_id(event_id, id_field="event_id")
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    await _require_event_ownership(event, current_user, db)

    update_data = data.model_dump(exclude_unset=True)
    updated = await event_repo.update(event_id, update_data, id_field="event_id")
    if updated.org_id is not None and update_data:
        ip, agent = client_ip_and_agent(request)
        await OrgActivityLogRepository(OrgActivityLog, db).log_activity(
            org_id=updated.org_id,
            category="event",
            verb="updated details for",
            target_name=updated.title,
            user_id=current_user.user_id,
            detail=f"Updated: {', '.join(sorted(update_data.keys()))}",
            ip_address=ip,
            user_agent=agent,
        )
    return EventResponse.model_validate(updated)


@router.delete("/{event_id}", status_code=204)
async def delete_event(
    event_id: UUID,
    current_user: User = Depends(require_roles(*ORGANIZER_ROLES)),
    db: AsyncSession = Depends(get_tenant_db),
):
    """Delete a draft event (published events must be cancelled instead)."""
    event_repo = EventRepository(Event, db)
    event = await event_repo.get_by_id(event_id, id_field="event_id")
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    await _require_event_ownership(event, current_user, db)

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
    db: AsyncSession = Depends(get_tenant_db),
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


@router.post("/{event_id}/candidates/upload-image")
async def upload_event_candidate_image(
    event_id: UUID,
    file: UploadFile = File(...),
    current_user: User = Depends(require_roles(*ORGANIZER_ROLES)),
    db: AsyncSession = Depends(get_tenant_db),
):
    """Upload a nominee photo. Returns { image_url } for use in add_candidate."""
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

    image_url = await file_storage_service.upload(
        content=content,
        filename=file.filename or f"candidate{ext}",
        content_type=file.content_type,
    )

    return {"image_url": image_url}


@router.post("/{event_id}/publish")
async def publish_event(
    event_id: UUID,
    current_user: User = Depends(require_roles(*ORGANIZER_ROLES)),
    db: AsyncSession = Depends(get_tenant_db),
):
    """Publish a draft/cancelled event, after checking every category (if
    any) has at least one candidate."""
    event_repo = EventRepository(Event, db)
    event = await event_repo.get_by_id(event_id, id_field="event_id")
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    await _require_event_ownership(event, current_user, db)
    if event.status not in ("draft", "cancelled"):
        raise HTTPException(
            status_code=400,
            detail="Only draft or cancelled events can be published",
        )

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

    if event.org_id is not None:
        member_ids = await OrganizationRepository(Organization, db).get_member_ids(event.org_id)
        await OrgNotificationService(db).notify(
            org_id=event.org_id,
            member_ids=member_ids,
            pref_key="event_published",
            notif_type="alert",
            title=f"{event.title} is now live",
            description="Your event has been published.",
            link=f"/dashboard/events?highlight={event_id}",
        )

    return {"message": "Event published successfully"}


@router.post("/{event_id}/unpublish")
async def unpublish_event(
    event_id: UUID,
    current_user: User = Depends(require_roles(*ORGANIZER_ROLES)),
    db: AsyncSession = Depends(get_tenant_db),
):
    """Revert a published event back to draft. Any tickets already sold stay
    valid — this only hides the event from public listings and re-locks it
    for editing; it isn't a cancellation."""
    event_repo = EventRepository(Event, db)
    event = await event_repo.get_by_id(event_id, id_field="event_id")
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    await _require_event_ownership(event, current_user, db)
    if event.status != "published":
        raise HTTPException(status_code=400, detail="Only published events can be reverted to draft")

    await event_repo.update_status(event_id, "draft")
    return {"message": "Event reverted to draft"}


@router.post("/{event_id}/cancel")
async def cancel_event(
    event_id: UUID,
    current_user: User = Depends(require_roles(*ORGANIZER_ROLES)),
    db: AsyncSession = Depends(get_tenant_db),
):
    """Cancel an event (distinct from unpublish — see get_event_scan_token's
    neighbor unpublish_event for the reversible, no-audit-trail variant)."""
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
    db: AsyncSession = Depends(get_tenant_db),
):
    """Mint (or re-mint — it's deterministic per event/day) a no-auth
    check-in link the organizer can hand to volunteers who have no
    account. Valid until the end of the event's calendar day."""
    event_repo = EventRepository(Event, db)
    event = await event_repo.get_by_id(event_id, id_field="event_id")
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    await _require_event_ownership(event, current_user, db)

    expires_at = scan_link_expiry(event.event_date)
    scan_token = create_ticket_scan_token(str(event.event_id), expires_at)
    return TicketScanTokenResponse(scan_token=scan_token, expires_at=expires_at)


# --- Ticket Types ---


@router.post(
    "/{event_id}/ticket-types",
    response_model=TicketTypeResponse,
    status_code=201,
)
async def create_ticket_type(
    request: Request,
    event_id: UUID,
    data: TicketTypeCreate,
    current_user: User = Depends(require_roles(*ORGANIZER_ROLES)),
    db: AsyncSession = Depends(get_tenant_db),
):
    """Add a ticket type (price tier) to an event."""
    event_repo = EventRepository(Event, db)
    event = await event_repo.get_by_id(event_id, id_field="event_id")
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    await _require_event_ownership(event, current_user, db)

    tt_repo = TicketTypeRepository(TicketType, db)

    if event.capacity is not None:
        existing = await tt_repo.get_by_event(event_id)
        allocated = sum(tt.quantity_available + tt.quantity_sold for tt in existing)
        if allocated + data.quantity_available > event.capacity:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Ticket quantities would exceed the event capacity of {event.capacity}. "
                    f"{allocated} already allocated; adding {data.quantity_available} would "
                    f"reach {allocated + data.quantity_available}."
                ),
            )

    ticket_type = await tt_repo.create(
        {**data.model_dump(), "event_id": event_id}
    )
    if event.org_id is not None:
        ip, agent = client_ip_and_agent(request)
        await OrgActivityLogRepository(OrgActivityLog, db).log_activity(
            org_id=event.org_id,
            category="ticketing",
            verb="added ticket type",
            target_name=f"{ticket_type.type_name} · {event.title}",
            user_id=current_user.user_id,
            detail=f"GH₵{ticket_type.price:.2f}",
            ip_address=ip,
            user_agent=agent,
        )
    return TicketTypeResponse.model_validate(ticket_type)


@router.get(
    "/{event_id}/ticket-types", response_model=List[TicketTypeResponse]
)
async def list_ticket_types(
    event_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """All ticket types for an event. No auth required."""
    tt_repo = TicketTypeRepository(TicketType, db)
    types = await tt_repo.get_by_event(event_id)
    return [TicketTypeResponse.model_validate(tt) for tt in types]


@router.put(
    "/{event_id}/ticket-types/{type_id}",
    response_model=TicketTypeResponse,
)
async def update_ticket_type(
    request: Request,
    event_id: UUID,
    type_id: UUID,
    data: TicketTypeUpdate,
    current_user: User = Depends(require_roles(*ORGANIZER_ROLES)),
    db: AsyncSession = Depends(get_tenant_db),
):
    """Partial update of a ticket type."""
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

    if event.capacity is not None and "quantity_available" in update_data:
        existing = await tt_repo.get_by_event(event_id)
        others_allocated = sum(
            (e.quantity_available + e.quantity_sold)
            for e in existing
            if e.ticket_type_id != type_id
        )
        new_qty = update_data["quantity_available"]
        if others_allocated + new_qty > event.capacity:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Ticket quantities would exceed the event capacity of {event.capacity}. "
                    f"Other ticket types use {others_allocated}; setting this to {new_qty} "
                    f"would reach {others_allocated + new_qty}."
                ),
            )

    updated = await tt_repo.update(type_id, update_data, id_field="ticket_type_id")
    if event.org_id is not None and "price" in update_data:
        ip, agent = client_ip_and_agent(request)
        await OrgActivityLogRepository(OrgActivityLog, db).log_activity(
            org_id=event.org_id,
            category="ticketing",
            verb="updated pricing for",
            target_name=f"{updated.type_name} · {event.title}",
            user_id=current_user.user_id,
            detail=f"Now GH₵{updated.price:.2f}",
            ip_address=ip,
            user_agent=agent,
        )
    return TicketTypeResponse.model_validate(updated)


@router.delete(
    "/{event_id}/ticket-types/{type_id}",
    status_code=204,
)
async def delete_ticket_type(
    event_id: UUID,
    type_id: UUID,
    current_user: User = Depends(require_roles(*ORGANIZER_ROLES)),
    db: AsyncSession = Depends(get_tenant_db),
):
    """Delete a ticket type that has never sold any tickets."""
    event_repo = EventRepository(Event, db)
    event = await event_repo.get_by_id(event_id, id_field="event_id")
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    await _require_event_ownership(event, current_user, db)

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
    db: AsyncSession = Depends(get_tenant_db),
):
    """Add a voting category to a draft event."""
    event_repo = EventRepository(Event, db)
    event = await event_repo.get_by_id(event_id, id_field="event_id")
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    await _require_event_ownership(event, current_user, db)

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
    """One event category with its candidates. No auth required."""
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
    db: AsyncSession = Depends(get_tenant_db),
):
    """Partial update of a draft event's category."""
    event_repo = EventRepository(Event, db)
    event = await event_repo.get_by_id(event_id, id_field="event_id")
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    await _require_event_ownership(event, current_user, db)

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
    db: AsyncSession = Depends(get_tenant_db),
):
    """Delete a draft event's category."""
    event_repo = EventRepository(Event, db)
    event = await event_repo.get_by_id(event_id, id_field="event_id")
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    await _require_event_ownership(event, current_user, db)

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
    db: AsyncSession = Depends(get_tenant_db),
):
    """Add a nominee to one of a draft event's categories."""
    event_repo = EventRepository(Event, db)
    event = await event_repo.get_by_id(event_id, id_field="event_id")
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    await _require_event_ownership(event, current_user, db)

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
    """All nominees across an event's categories. No auth required."""
    candidate_repo = CandidateRepository(Candidate, db)
    candidates = await candidate_repo.get_by_event(event_id)
    return [CandidateResponse.model_validate(c) for c in candidates]


@router.put("/{event_id}/candidates/{candidate_id}", response_model=CandidateResponse)
async def update_event_candidate(
    event_id: UUID,
    candidate_id: UUID,
    data: CandidateUpdate,
    current_user: User = Depends(require_roles(*ORGANIZER_ROLES)),
    db: AsyncSession = Depends(get_tenant_db),
):
    """Partial update of a draft event's nominee."""
    event_repo = EventRepository(Event, db)
    event = await event_repo.get_by_id(event_id, id_field="event_id")
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    await _require_event_ownership(event, current_user, db)

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
    db: AsyncSession = Depends(get_tenant_db),
):
    """Remove a nominee from an event."""
    event_repo = EventRepository(Event, db)
    event = await event_repo.get_by_id(event_id, id_field="event_id")
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    await _require_event_ownership(event, current_user, db)

    candidate_repo = CandidateRepository(Candidate, db)
    candidate = await candidate_repo.get_by_id(candidate_id, id_field="candidate_id")
    if not candidate or candidate.event_id != event_id:
        raise HTTPException(status_code=404, detail="Candidate not found")

    await candidate_repo.delete(candidate_id, id_field="candidate_id")
