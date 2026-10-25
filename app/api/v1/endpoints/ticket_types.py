"""Cross-event view over ticket types — per-event CRUD lives under
/events/{event_id}/ticket-types (events.py); this is just the dashboard's
"every price tier across your events" list, mirroring nominees.py."""
from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import require_roles
from app.db.tenant import get_tenant_db
from app.models.event import Event, TicketType
from app.models.organization import Organization
from app.models.user import User
from app.repositories.event_repository import EventRepository, TicketTypeRepository
from app.repositories.organization_repository import OrganizationRepository
from app.schemas.event import TicketTypeResponse, TicketTypeWithEventResponse

router = APIRouter(prefix="/ticket-types", tags=["Ticket Types"])

ORGANIZER_ROLES = ("System Administrator", "Event Organizer")


@router.get("", response_model=List[TicketTypeWithEventResponse])
async def list_all_ticket_types(
    current_user: User = Depends(require_roles(*ORGANIZER_ROLES)),
    db: AsyncSession = Depends(get_tenant_db),
):
    """Every ticket type across every event the caller's team owns."""
    teammate_ids = await OrganizationRepository(Organization, db).get_teammate_ids(current_user.user_id)
    types = await TicketTypeRepository(TicketType, db).get_by_creators(teammate_ids)
    if not types:
        return []

    event_ids = {tt.event_id for tt in types}
    events = await EventRepository(Event, db).get_by_ids(list(event_ids))
    titles = {e.event_id: e.title for e in events}

    return [
        TicketTypeWithEventResponse(
            **TicketTypeResponse.model_validate(tt).model_dump(),
            event_title=titles.get(tt.event_id, ""),
        )
        for tt in types
    ]
