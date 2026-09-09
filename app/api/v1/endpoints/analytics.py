from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import require_roles
from app.db.base import get_db
from app.models.user import User
from app.services.analytics_service import AnalyticsService

router = APIRouter(prefix="/analytics", tags=["Analytics"])


class SystemAnalyticsResponse(BaseModel):
    """Platform-wide dashboard totals.

    Both revenue streams are reported in pesewas: vote payments are already
    stored that way, ticket purchases are converted from cedis.
    """

    total_users: int
    total_elections: int
    active_elections: int
    total_events: int
    active_events: int
    total_votes_cast: int
    total_tickets_sold: int
    total_election_revenue_pesewas: int
    total_event_revenue_pesewas: int
    total_revenue_pesewas: int


@router.get("/elections/{election_id}")
async def election_analytics(
    election_id: UUID,
    current_user: User = Depends(
        require_roles("System Administrator", "Election Administrator")
    ),
    db: AsyncSession = Depends(get_db),
):
    """Turnout, revenue, and voting timeline for one election."""
    service = AnalyticsService(db)
    return await service.get_election_stats(election_id)


@router.get("/events/{event_id}")
async def event_analytics(
    event_id: UUID,
    current_user: User = Depends(
        require_roles("System Administrator", "Event Organizer")
    ),
    db: AsyncSession = Depends(get_db),
):
    """Ticket sales, attendance, and revenue for one event."""
    service = AnalyticsService(db)
    return await service.get_event_stats(event_id)


@router.get("/system", response_model=SystemAnalyticsResponse)
async def system_analytics(
    current_user: User = Depends(require_roles("System Administrator")),
    db: AsyncSession = Depends(get_db),
):
    """Platform-wide totals for the admin dashboard."""
    service = AnalyticsService(db)
    return await service.get_system_stats()
