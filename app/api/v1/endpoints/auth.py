from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.base import get_db
from app.middleware.rate_limit import limiter
from app.models.audit_log import AuditLog
from app.models.user import User
from app.repositories.audit_log_repository import AuditLogRepository
from app.repositories.user_repository import UserRepository
from app.schemas.user import (
    ForgotPassword,
    LogoutRequest,
    RefreshTokenRequest,
    ResendOtp,
    ResetPassword,
    TokenResponse,
    UserCreate,
    UserLogin,
    UserResponse,
    VerifyEmail,
    VerifyOtp,
)
from app.services.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["Authentication"])


def _get_auth_service(db: AsyncSession) -> AuthService:
    return AuthService(
        user_repo=UserRepository(User, db),
        audit_repo=AuditLogRepository(AuditLog, db),
    )


@router.post("/register", response_model=UserResponse, status_code=201)
@limiter.limit(settings.RATE_LIMIT_AUTH)
async def register(
    data: UserCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Create an account and send a verification OTP to the given email."""
    service = _get_auth_service(db)
    return await service.register(
        data,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )


@router.post("/login", response_model=TokenResponse)
@limiter.limit(settings.RATE_LIMIT_AUTH)
async def login(
    data: UserLogin,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Verify credentials and return an access+refresh token pair."""
    service = _get_auth_service(db)
    return await service.login(
        data,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )


@router.post("/refresh", response_model=TokenResponse)
@limiter.limit(settings.RATE_LIMIT_AUTH)
async def refresh_token(
    data: RefreshTokenRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Exchange a refresh token for a new pair (single-use — see
    AuthService.refresh_token)."""
    service = _get_auth_service(db)
    return await service.refresh_token(data.refresh_token)


@router.post("/logout", status_code=204)
@limiter.limit(settings.RATE_LIMIT_AUTH)
async def logout(
    data: LogoutRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Log out — invalidates every session for this user (see
    AuthService.logout)."""
    service = _get_auth_service(db)
    await service.logout(data.refresh_token)


@router.post("/forgot-password", status_code=202)
@limiter.limit(settings.RATE_LIMIT_AUTH)
async def forgot_password(
    data: ForgotPassword,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Always returns the same generic message, whether or not the email
    is registered (anti-enumeration)."""
    service = _get_auth_service(db)
    await service.forgot_password(data.email)
    return {"message": "If the email exists, a reset link has been sent"}


@router.post("/reset-password")
@limiter.limit(settings.RATE_LIMIT_AUTH)
async def reset_password(
    data: ResetPassword,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Consume a forgot-password token to set a new password."""
    service = _get_auth_service(db)
    await service.reset_password(data.token, data.new_password)
    return {"message": "Password reset successfully"}


@router.post("/verify-email")
@limiter.limit(settings.RATE_LIMIT_AUTH)
async def verify_email(
    data: VerifyEmail,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Consume a link-based email verification token."""
    service = _get_auth_service(db)
    await service.verify_email(data.token)
    return {"message": "Email verified successfully"}


@router.post("/verify-otp")
@limiter.limit(settings.RATE_LIMIT_AUTH)
async def verify_otp(
    data: VerifyOtp,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Verify the 6-digit code sent at registration and mark email_verified."""
    service = _get_auth_service(db)
    return await service.verify_otp(data.email, data.otp_code)


@router.post("/resend-otp")
@limiter.limit(settings.RATE_LIMIT_AUTH)
async def resend_otp(
    data: ResendOtp,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Issue a fresh verification OTP, replacing any unexpired one."""
    service = _get_auth_service(db)
    return await service.resend_otp(data.email)
