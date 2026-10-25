from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import require_roles
from app.db.tenant import get_tenant_db
from app.models.organization import Organization
from app.models.user import User
from app.repositories.organization_repository import OrganizationRepository
from app.schemas.nominee import NomineeListResponse
from app.services.nominee_service import NomineeService

router = APIRouter(prefix="/nominees", tags=["Nominees"])

ADMIN_ROLES = ("System Administrator", "Election Administrator", "Event Organizer")


@router.get("", response_model=NomineeListResponse)
async def list_nominees(
    db: AsyncSession = Depends(get_tenant_db),
    current_user: User = Depends(require_roles(*ADMIN_ROLES)),
):
    """Every candidate across the caller's elections and events, with real
    vote counts and category rank — the dashboard's cross-source Nominees
    page. Organizer-only, same sensitivity as live-results."""
    teammate_ids = await OrganizationRepository(Organization, db).get_teammate_ids(current_user.user_id)
    items = await NomineeService(db).list_nominees(teammate_ids)
    return NomineeListResponse(items=items)
