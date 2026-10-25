from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_current_active_user
from app.db.base import get_db
from app.models.audit_log import AuditLog
from app.models.user import User
from app.repositories.audit_log_repository import AuditLogRepository
from app.repositories.user_repository import UserRepository
from app.schemas.two_factor import (
    RegenerateRecoveryCodesRequest,
    TwoFactorCodesResponse,
    TwoFactorDisableRequest,
    TwoFactorEnableRequest,
    TwoFactorSetupResponse,
)
from app.services.two_factor_service import TwoFactorService

router = APIRouter(prefix="/users/me/2fa", tags=["Two-Factor Authentication"])


def _get_service(db: AsyncSession) -> TwoFactorService:
    return TwoFactorService(
        user_repo=UserRepository(User, db),
        audit_repo=AuditLogRepository(AuditLog, db),
    )


@router.post("/setup", response_model=TwoFactorSetupResponse)
async def setup_two_factor(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Generate a new (not-yet-enabled) TOTP secret and return its QR
    payload. Call /enable with a code from the app to activate it."""
    secret, otpauth_uri = await _get_service(db).start_setup(current_user)
    return TwoFactorSetupResponse(secret=secret, otpauth_uri=otpauth_uri)


@router.post("/enable", response_model=TwoFactorCodesResponse)
async def enable_two_factor(
    data: TwoFactorEnableRequest,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Verify the code from the authenticator app and turn 2FA on. Returns
    a fresh set of recovery codes — shown here once, never retrievable
    again."""
    codes = await _get_service(db).verify_and_enable(current_user, data.code)
    return TwoFactorCodesResponse(recovery_codes=codes)


@router.post("/disable", status_code=204)
async def disable_two_factor(
    data: TwoFactorDisableRequest,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Turn 2FA off. Requires the account password to confirm."""
    await _get_service(db).disable(current_user, data.password)


@router.post("/recovery-codes/regenerate", response_model=TwoFactorCodesResponse)
async def regenerate_recovery_codes(
    data: RegenerateRecoveryCodesRequest,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Invalidate all existing recovery codes and issue a new set. Requires
    the account password to confirm."""
    codes = await _get_service(db).regenerate_recovery_codes(current_user, data.password)
    return TwoFactorCodesResponse(recovery_codes=codes)
