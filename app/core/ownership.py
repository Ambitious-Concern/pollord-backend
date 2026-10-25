"""Ownership checks for elections/events, shared across endpoint files and
dependencies.py — kept out of elections.py/events.py to avoid a circular
import (dependencies.py needs these too, and elections.py/events.py already
import from dependencies.py)."""
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization import Organization
from app.models.user import User
from app.repositories.organization_repository import OrganizationRepository

SYSTEM_ADMIN = "System Administrator"


async def _owns_resource(resource, current_user: User, db: AsyncSession) -> bool:
    """The creator, a System Administrator, or an organization teammate
    holding a managing role."""
    user_roles = [ur.role.role_name for ur in current_user.user_roles]
    if resource.created_by == current_user.user_id or SYSTEM_ADMIN in user_roles:
        return True
    return await OrganizationRepository(Organization, db).can_manage_with(
        current_user.user_id, resource.created_by
    )


async def owns_election(election, current_user: User, db: AsyncSession) -> bool:
    return await _owns_resource(election, current_user, db)


async def require_election_ownership(election, current_user: User, db: AsyncSession) -> None:
    if not await owns_election(election, current_user, db):
        raise HTTPException(status_code=403, detail="You do not have access to this election")


async def owns_event(event, current_user: User, db: AsyncSession) -> bool:
    return await _owns_resource(event, current_user, db)


async def require_event_ownership(event, current_user: User, db: AsyncSession) -> None:
    if not await owns_event(event, current_user, db):
        raise HTTPException(status_code=403, detail="You do not have access to this event")


async def require_promo_code_ownership(promo_code, current_user: User, db: AsyncSession) -> None:
    if not await _owns_resource(promo_code, current_user, db):
        raise HTTPException(status_code=403, detail="You do not have access to this promo code")


async def require_template_ownership(template, current_user: User, db: AsyncSession) -> None:
    if not await _owns_resource(template, current_user, db):
        raise HTTPException(status_code=403, detail="You do not have access to this template")
