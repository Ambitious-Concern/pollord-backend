from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import require_roles
from app.db.tenant import get_tenant_db
from app.models.organization import Organization
from app.models.user import User
from app.repositories.organization_repository import OrganizationRepository
from app.schemas.search import SearchResponse
from app.services.search_service import SearchService

router = APIRouter(prefix="/search", tags=["Search"])

ADMIN_ROLES = ("System Administrator", "Election Administrator")


@router.get("", response_model=SearchResponse)
async def search(
    q: str = Query(..., min_length=1),
    db: AsyncSession = Depends(get_tenant_db),
    current_user: User = Depends(require_roles(*ADMIN_ROLES)),
):
    """One endpoint for the dashboard's command palette — elections, events,
    nominees, ticket types — scoped to the caller's team."""
    teammate_ids = await OrganizationRepository(Organization, db).get_teammate_ids(current_user.user_id)
    items = await SearchService(db).search(teammate_ids, q)
    return SearchResponse(items=items)
