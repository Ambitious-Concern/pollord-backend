from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_current_active_user
from app.db.base import get_db
from app.models.audit_log import AuditLog
from app.models.user import User
from app.repositories.audit_log_repository import AuditLogRepository
from app.repositories.user_repository import UserRepository
from app.schemas.user import (
    AuditLogResponse,
    ChangePassword,
    UserResponse,
    UserUpdate,
)
from app.services.auth_service import AuthService

router = APIRouter(prefix="/users", tags=["Users"])


@router.get("/me", response_model=UserResponse)
async def get_profile(
    current_user: User = Depends(get_current_active_user),
):
    roles = [ur.role.role_name for ur in current_user.user_roles] if current_user.user_roles else []
    return UserResponse(
        user_id=current_user.user_id,
        email=current_user.email,
        full_name=current_user.full_name,
        phone_number=current_user.phone_number,
        email_verified=current_user.email_verified,
        account_status=current_user.account_status,
        created_at=current_user.created_at,
        roles=roles,
    )


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
    roles = [ur.role.role_name for ur in user.user_roles] if user.user_roles else []
    return UserResponse(
        user_id=user.user_id,
        email=user.email,
        full_name=user.full_name,
        phone_number=user.phone_number,
        email_verified=user.email_verified,
        account_status=user.account_status,
        created_at=user.created_at,
        roles=roles,
    )


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
