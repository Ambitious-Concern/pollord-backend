from typing import List, Optional

from fastapi import HTTPException, status

from app.core.security import hash_password, verify_password
from app.core.totp import (
    build_totp_uri,
    generate_recovery_codes,
    generate_totp_secret,
    verify_totp_code,
)
from app.models.audit_log import AuditLog
from app.models.user import User
from app.repositories.audit_log_repository import AuditLogRepository
from app.repositories.user_repository import UserRepository


class TwoFactorService:
    """TOTP setup/enable/disable and recovery-code lifecycle for a user's
    own account. Deliberately separate from AuthService: this owns the
    2FA credential itself, while AuthService owns the login flow that
    challenges against it (see AuthService.verify_2fa_and_login)."""

    def __init__(self, user_repo: UserRepository, audit_repo: AuditLogRepository):
        self.user_repo = user_repo
        self.audit_repo = audit_repo

    async def start_setup(self, user: User) -> tuple[str, str]:
        """Generates and stores a new (not-yet-enabled) secret. Safe to call
        again before verify_and_enable — a re-scanned QR from an abandoned
        attempt just replaces the pending secret."""
        secret = generate_totp_secret()
        user.totp_secret = secret
        await self.user_repo.session.flush()
        return secret, build_totp_uri(secret, user.email)

    async def verify_and_enable(
        self, user: User, code: str, ip: Optional[str] = None, user_agent: Optional[str] = None
    ) -> List[str]:
        if not user.totp_secret:
            raise HTTPException(status_code=400, detail="Start setup first")
        if not verify_totp_code(user.totp_secret, code):
            raise HTTPException(status_code=400, detail="Invalid code")

        codes = generate_recovery_codes()
        user.totp_enabled = True
        user.recovery_codes_hashed = [hash_password(c) for c in codes]
        await self.user_repo.session.flush()

        await self.audit_repo.log_action(
            action_type="2FA_ENABLED",
            entity_type="User",
            entity_id=user.user_id,
            user_id=user.user_id,
            ip_address=ip,
            user_agent=user_agent,
        )
        return codes

    async def disable(
        self, user: User, password: str, ip: Optional[str] = None, user_agent: Optional[str] = None
    ) -> None:
        if not verify_password(password, user.password_hash):
            raise HTTPException(status_code=400, detail="Incorrect password")

        user.totp_enabled = False
        user.totp_secret_encrypted = None
        user.recovery_codes_hashed = None
        await self.user_repo.session.flush()

        await self.audit_repo.log_action(
            action_type="2FA_DISABLED",
            entity_type="User",
            entity_id=user.user_id,
            user_id=user.user_id,
            ip_address=ip,
            user_agent=user_agent,
        )

    async def regenerate_recovery_codes(self, user: User, password: str) -> List[str]:
        if not user.totp_enabled:
            raise HTTPException(status_code=400, detail="Two-factor authentication is not enabled")
        if not verify_password(password, user.password_hash):
            raise HTTPException(status_code=400, detail="Incorrect password")

        codes = generate_recovery_codes()
        user.recovery_codes_hashed = [hash_password(c) for c in codes]
        await self.user_repo.session.flush()
        return codes

    async def verify_login_code(self, user: User, code: str) -> bool:
        """Accepts either a live TOTP code or a one-time recovery code
        (consumed on use — removed from the stored list so it can't be
        replayed)."""
        if user.totp_secret and verify_totp_code(user.totp_secret, code):
            return True

        for hashed in user.recovery_codes_hashed or []:
            if verify_password(code, hashed):
                user.recovery_codes_hashed = [h for h in user.recovery_codes_hashed if h != hashed]
                await self.user_repo.session.flush()
                return True

        return False
