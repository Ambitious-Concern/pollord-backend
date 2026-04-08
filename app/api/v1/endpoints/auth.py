from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import get_db
from app.models.audit_log import AuditLog
from app.models.user import User
from app.repositories.audit_log_repository import AuditLogRepository
from app.repositories.user_repository import UserRepository
from app.schemas.user import (
    ForgotPassword,
    RefreshTokenRequest,
    ResetPassword,
    TokenResponse,
    UserCreate,
    UserLogin,
    UserResponse,
    VerifyEmail,
)
from app.services.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["Authentication"])


def _get_auth_service(db: AsyncSession) -> AuthService:
    return AuthService(
        user_repo=UserRepository(User, db),
        audit_repo=AuditLogRepository(AuditLog, db),
    )


@router.post("/register", response_model=UserResponse, status_code=201)
async def register(
    data: UserCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    service = _get_auth_service(db)
    return await service.register(
        data,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )


@router.post("/login", response_model=TokenResponse)
async def login(
    data: UserLogin,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    service = _get_auth_service(db)
    return await service.login(
        data,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )



@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(
    data: RefreshTokenRequest,
    db: AsyncSession = Depends(get_db),
):
    service = _get_auth_service(db)
    return await service.refresh_token(data.refresh_token)


@router.post("/forgot-password", status_code=202)
async def forgot_password(
    data: ForgotPassword,
    db: AsyncSession = Depends(get_db),
):
    service = _get_auth_service(db)
    await service.forgot_password(data.email)
    return {"message": "If the email exists, a reset link has been sent"}


@router.post("/reset-password")
async def reset_password(
    data: ResetPassword,
    db: AsyncSession = Depends(get_db),
):
    service = _get_auth_service(db)
    await service.reset_password(data.token, data.new_password)
    return {"message": "Password reset successfully"}


@router.post("/verify-email")
async def verify_email(
    data: VerifyEmail,
    db: AsyncSession = Depends(get_db),
):
    service = _get_auth_service(db)
    await service.verify_email(data.token)
    return {"message": "Email verified successfully"}


@router.post("/verify-otp")
async def verify_otp(
    data: dict,
    db: AsyncSession = Depends(get_db),
):
    """Verify OTP code sent to email. Body: { email, otp_code }"""
    service = _get_auth_service(db)
    email = data.get("email", "")
    otp_code = data.get("otp_code", "")
    return await service.verify_otp(email, otp_code)


@router.post("/resend-otp")
async def resend_otp(
    data: dict,
    db: AsyncSession = Depends(get_db),
):
    """Resend OTP code. Body: { email }"""
    service = _get_auth_service(db)
    email = data.get("email", "")
    return await service.resend_otp(email)
