from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import require_roles
from app.db.base import get_db
from app.models.user import User
from app.services.analytics_service import AnalyticsService

router = APIRouter(prefix="/analytics", tags=["Analytics"])


@router.get("/elections/{election_id}")
async def election_analytics(
    election_id: UUID,
    current_user: User = Depends(
        require_roles("System Administrator", "Election Administrator")
    ),
    db: AsyncSession = Depends(get_db),
):
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
    service = AnalyticsService(db)
    return await service.get_event_stats(event_id)


@router.get("/system")
async def system_analytics(
    current_user: User = Depends(require_roles("System Administrator")),
    db: AsyncSession = Depends(get_db),
):
    service = AnalyticsService(db)
    return await service.get_system_stats()
