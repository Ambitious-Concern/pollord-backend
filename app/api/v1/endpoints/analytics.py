from typing import List, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import require_roles
from app.core.ownership import require_election_ownership, require_event_ownership
from app.db.base import get_db
from app.db.tenant import get_tenant_db
from app.models.election import Election
from app.models.event import Event
from app.models.user import User
from app.repositories.election_repository import ElectionRepository
from app.repositories.event_repository import EventRepository
from app.schemas.financial_stats import DateRange, FinancialStatsResponse
from app.services.analytics_service import AnalyticsService
from app.services.financial_stats_service import FinancialStatsService

router = APIRouter(prefix="/analytics", tags=["Analytics"])


class OrganizationRevenue(BaseModel):
    """What one organisation has earned, in cedis."""

    org_id: UUID
    name: str
    # Paid votes, whether cast in an election or an event's category voting.
    vote_revenue_ghs: float
    ticket_revenue_ghs: float
    total_revenue_ghs: float


class SystemAnalyticsResponse(BaseModel):
    """Platform-wide dashboard totals.

    Both revenue streams are reported in cedis: ticket purchases are already
    stored that way, vote payments are converted from pesewas.
    """

    total_users: int
    total_elections: int
    active_elections: int
    total_events: int
    active_events: int
    total_votes_cast: int
    total_tickets_sold: int
    total_election_revenue_ghs: float
    total_event_revenue_ghs: float
    total_revenue_ghs: float
    # Every organisation, highest earner first; zero-revenue orgs included.
    organizations: List[OrganizationRevenue] = []


@router.get("/elections/{election_id}")
async def election_analytics(
    election_id: UUID,
    current_user: User = Depends(
        require_roles("System Administrator", "Election Administrator")
    ),
    db: AsyncSession = Depends(get_tenant_db),
):
    """Turnout, revenue, and voting timeline for one election."""
    election = await ElectionRepository(Election, db).get_by_id(
        election_id, id_field="election_id"
    )
    if not election:
        raise HTTPException(status_code=404, detail="Election not found")
    await require_election_ownership(election, current_user, db)

    service = AnalyticsService(db)
    return await service.get_election_stats(election_id)


@router.get("/events/{event_id}")
async def event_analytics(
    event_id: UUID,
    current_user: User = Depends(
        require_roles("System Administrator", "Event Organizer")
    ),
    db: AsyncSession = Depends(get_tenant_db),
):
    """Ticket sales, attendance, and revenue for one event."""
    event = await EventRepository(Event, db).get_by_id(event_id, id_field="event_id")
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    await require_event_ownership(event, current_user, db)

    service = AnalyticsService(db)
    return await service.get_event_stats(event_id)


@router.get("/timeline")
async def listing_activity_timeline(
    listing_type: Literal["election", "event"],
    listing_id: UUID,
    granularity: Literal["day", "hour"] = "day",
    current_user: User = Depends(
        require_roles("System Administrator", "Election Administrator", "Event Organizer")
    ),
    db: AsyncSession = Depends(get_tenant_db),
):
    """Rolling votes/tickets timeline for one listing, for the dashboard's
    trend card — 24 hourly buckets or 14 daily buckets, ending now."""
    if listing_type == "election":
        election = await ElectionRepository(Election, db).get_by_id(
            listing_id, id_field="election_id"
        )
        if not election:
            raise HTTPException(status_code=404, detail="Election not found")
        await require_election_ownership(election, current_user, db)
    else:
        event = await EventRepository(Event, db).get_by_id(listing_id, id_field="event_id")
        if not event:
            raise HTTPException(status_code=404, detail="Event not found")
        await require_event_ownership(event, current_user, db)

    service = AnalyticsService(db)
    return await service.get_listing_activity_timeline(listing_type, listing_id, granularity)


@router.get("/financial", response_model=FinancialStatsResponse)
async def financial_stats(
    range: DateRange = "all-time",
    source_id: Optional[UUID] = None,
    current_user: User = Depends(
        require_roles("System Administrator", "Election Administrator", "Event Organizer")
    ),
    db: AsyncSession = Depends(get_tenant_db),
):
    """Gross/commission/processing/net across every paid vote and paid
    ticket the caller's team runs — the real backend for both the
    standalone Financial Stats page and Reports' Financial tab."""
    return await FinancialStatsService(db).get_stats(
        current_user.user_id, range_id=range, source_id=source_id
    )


@router.get("/system", response_model=SystemAnalyticsResponse)
async def system_analytics(
    current_user: User = Depends(require_roles("System Administrator")),
    db: AsyncSession = Depends(get_db),
):
    """Platform-wide totals for the admin dashboard."""
    service = AnalyticsService(db)
    return await service.get_system_stats()
