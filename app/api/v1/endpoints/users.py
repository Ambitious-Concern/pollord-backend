from typing import List, Optional

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_current_active_user
from app.db.base import get_db
from app.models.audit_log import AuditLog
from app.models.organization import Organization
from app.models.user import User
from app.repositories.audit_log_repository import AuditLogRepository
from app.repositories.organization_repository import OrganizationRepository
from app.repositories.user_repository import UserRepository
from pydantic import BaseModel
from app.schemas.user import (
    AuditLogResponse,
    ChangePassword,
    UserResponse,
    UserUpdate,
)


class UserSearchResult(BaseModel):
    user_id: str
    full_name: str
    email: str

    class Config:
        from_attributes = True
from app.services.auth_service import AuthService

router = APIRouter(prefix="/users", tags=["Users"])


async def _user_response(user: User, db: AsyncSession) -> UserResponse:
    """Build UserResponse with organization info."""
    roles = [ur.role.role_name for ur in user.user_roles] if user.user_roles else []
    org_repo = OrganizationRepository(Organization, db)
    # Use get_user_organizations so invited members also get has_organization=True
    orgs = await org_repo.get_user_organizations(user.user_id)
    has_org = len(orgs) > 0
    org_verified = any(o.is_verified for o in orgs) if has_org else False

    return UserResponse(
        user_id=user.user_id,
        email=user.email,
        full_name=user.full_name,
        phone_number=user.phone_number,
        email_verified=user.email_verified,
        account_status=user.account_status,
        has_organization=has_org,
        organization_verified=org_verified,
        created_at=user.created_at,
        roles=roles,
    )


@router.get("/me", response_model=UserResponse)
async def get_profile(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    return await _user_response(current_user, db)


@router.put("/me", response_model=UserResponse)
async def update_profile(
    data: UserUpdate,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    user_repo = UserRepository(User, db)
    update_data = data.model_dump(exclude_unset=True)
    if update_data:
        for key, value in update_data.items():
            setattr(current_user, key, value)
        await db.flush()
        await db.refresh(current_user)

    user = await user_repo.get_with_roles(current_user.user_id)
    return await _user_response(user, db)


@router.post("/me/change-password")
async def change_password(
    data: ChangePassword,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    service = AuthService(
        user_repo=UserRepository(User, db),
        audit_repo=AuditLogRepository(AuditLog, db),
    )
    await service.change_password(
        current_user, data.current_password, data.new_password
    )
    return {"message": "Password changed successfully"}


@router.get("/search", response_model=List[UserSearchResult])
async def search_users(
    q: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
    limit: int = 10,
):
    """Search users by email or name. Available to any authenticated user for org invites."""
    if len(q) < 2:
        return []
    user_repo = UserRepository(User, db)
    users = await user_repo.search_users(q, skip=0, limit=limit)
    # Exclude the searching user from results
    return [
        UserSearchResult(user_id=str(u.user_id), full_name=u.full_name, email=u.email)
        for u in users
        if u.user_id != current_user.user_id
    ]


@router.get("/me/activity", response_model=List[AuditLogResponse])
async def get_activity(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
    skip: int = 0,
    limit: int = 20,
):
    audit_repo = AuditLogRepository(AuditLog, db)
    logs = await audit_repo.get_by_user(current_user.user_id, skip, limit)
    return [AuditLogResponse.model_validate(log) for log in logs]
