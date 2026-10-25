from datetime import datetime, timezone
from typing import List, Optional

from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.dependencies import get_current_active_user, get_current_session_id
from app.core.notification_prefs import effective_prefs
from app.core.session_revocation import revoke_session as revoke_session_in_redis
from app.db.base import get_db
from app.services.file_storage_service import file_storage_service
from app.models.audit_log import AuditLog
from app.models.organization import Organization
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.repositories.audit_log_repository import AuditLogRepository
from app.repositories.organization_repository import OrganizationRepository
from app.repositories.refresh_token_repository import RefreshTokenRepository
from app.repositories.user_repository import UserRepository
from pydantic import BaseModel
from app.schemas.notification_prefs import NotificationPrefsResponse, NotificationPrefsUpdate
from app.schemas.user import (
    AuditLogResponse,
    ChangePassword,
    SessionResponse,
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
        avatar_url=user.avatar_url,
        totp_enabled=user.totp_enabled,
        onboarding_completed_at=user.onboarding_completed_at,
        created_at=user.created_at,
        roles=roles,
    )


@router.get("/me", response_model=UserResponse)
async def get_profile(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """The signed-in user's own profile."""
    return await _user_response(current_user, db)


@router.put("/me", response_model=UserResponse)
async def update_profile(
    data: UserUpdate,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Partial update of the signed-in user's own profile fields."""
    user_repo = UserRepository(User, db)
    update_data = data.model_dump(exclude_unset=True)
    if update_data:
        for key, value in update_data.items():
            setattr(current_user, key, value)
        await db.flush()
        await db.refresh(current_user)

    user = await user_repo.get_with_roles(current_user.user_id)
    return await _user_response(user, db)


MAX_AVATAR_SIZE = 10 * 1024 * 1024  # 10 MB
ALLOWED_AVATAR_TYPES = {".jpg", ".jpeg", ".png", ".webp"}


@router.post("/me/avatar", response_model=UserResponse)
async def upload_avatar(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Upload the signed-in user's profile picture.

    Goes through the ordinary public file route, not the private one: an
    avatar is rendered in <img> tags across the dashboard, and unlike an
    identity document it isn't sensitive.
    """
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_AVATAR_TYPES:
        raise HTTPException(
            status_code=400,
            detail="Unsupported file type. Allowed: JPG, PNG, WEBP",
        )

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(content) > MAX_AVATAR_SIZE:
        raise HTTPException(status_code=400, detail="Image must be under 10 MB")

    current_user.avatar_url = await file_storage_service.upload(
        content=content,
        filename=file.filename or f"avatar{ext}",
        content_type=file.content_type,
    )
    await db.commit()
    await db.refresh(current_user)
    return await _user_response(current_user, db)


@router.delete("/me/avatar", response_model=UserResponse)
async def remove_avatar(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Clear the profile picture, falling back to the initials monogram."""
    if current_user.avatar_url is not None:
        current_user.avatar_url = None
        await db.commit()
        await db.refresh(current_user)
    return await _user_response(current_user, db)


@router.post("/me/onboarding-complete", response_model=UserResponse)
async def complete_onboarding(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Mark the welcome tour as seen, whether it was finished or skipped.

    Idempotent — the first timestamp wins, so a double-submit (or a second
    tab) can't reopen the tour or move the date.
    """
    if current_user.onboarding_completed_at is None:
        current_user.onboarding_completed_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(current_user)
    return await _user_response(current_user, db)


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
    return [
        UserSearchResult(user_id=str(u.user_id), full_name=u.full_name, email=u.email)
        for u in users
        if u.user_id != current_user.user_id
    ]


@router.get("/me/sessions", response_model=List[SessionResponse])
async def list_sessions(
    current_user: User = Depends(get_current_active_user),
    current_session_id: Optional[str] = Depends(get_current_session_id),
    db: AsyncSession = Depends(get_db),
):
    """Every device with a currently-usable refresh token — the real
    "active sessions" list. See RefreshTokenRepository.get_active_for_user."""
    repo = RefreshTokenRepository(RefreshToken, db)
    tokens = await repo.get_active_for_user(current_user.user_id)
    return [
        SessionResponse(
            jti=t.jti,
            ip_address=t.ip_address,
            user_agent=t.user_agent,
            created_at=t.created_at,
            expires_at=t.expires_at,
            is_current=(t.jti == current_session_id),
        )
        for t in tokens
    ]


@router.delete("/me/sessions/{jti}", status_code=204)
async def revoke_session(
    jti: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Sign out one device — its refresh token can no longer be redeemed, so
    its next silent refresh attempt fails and it's kicked back to login.
    Also revoked in Redis so the access token it's currently holding stops
    working immediately, rather than staying valid until it naturally
    expires (up to ACCESS_TOKEN_EXPIRE_MINUTES later)."""
    repo = RefreshTokenRepository(RefreshToken, db)
    token = await repo.get_by_jti(jti)
    if not token or token.user_id != current_user.user_id:
        raise HTTPException(status_code=404, detail="Session not found")
    await repo.revoke(jti)
    await revoke_session_in_redis(jti, ttl_seconds=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60)


@router.get("/me/activity", response_model=List[AuditLogResponse])
async def get_activity(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
    skip: int = 0,
    limit: int = 20,
    action_type: Optional[str] = None,
):
    """The signed-in user's own audit trail (logins, votes cast, etc.).

    `action_type` narrows to one action (e.g. "login" for the Security
    settings page's Login Activity card, which has no interest in vote-cast
    or admin-change entries) — matched case-insensitively against the
    stored LOGIN/LOGIN_FAILED-style uppercase action_type values.
    """
    audit_repo = AuditLogRepository(AuditLog, db)
    action_types = [action_type.upper()] if action_type else None
    logs = await audit_repo.get_by_user(current_user.user_id, skip, limit, action_types=action_types)
    return [AuditLogResponse.model_validate(log) for log in logs]


@router.get("/me/notification-prefs", response_model=NotificationPrefsResponse)
async def get_notification_prefs(
    current_user: User = Depends(get_current_active_user),
):
    """Which events on the Settings > Notifications tab email the signed-in
    user — merged over the defaults, since a user who's never touched a
    toggle has no stored row for it."""
    return NotificationPrefsResponse(prefs=effective_prefs(current_user.notification_prefs))


@router.put("/me/notification-prefs", response_model=NotificationPrefsResponse)
async def update_notification_prefs(
    data: NotificationPrefsUpdate,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Partial update — merges into whatever's already stored rather than
    replacing it, so toggling one preference doesn't reset the others back
    to default."""
    merged = {**(current_user.notification_prefs or {}), **data.prefs}
    current_user.notification_prefs = merged
    await db.flush()
    return NotificationPrefsResponse(prefs=effective_prefs(merged))
