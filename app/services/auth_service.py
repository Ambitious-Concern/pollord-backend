import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select

from app.core.config import settings
from app.core.security import (
    create_access_token,
    create_email_verification_token,
    create_password_reset_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.models.audit_log import AuditLog
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.repositories.audit_log_repository import AuditLogRepository
from app.repositories.user_repository import UserRepository
from app.schemas.user import (
    TokenResponse,
    UserCreate,
    UserLogin,
    UserResponse,
)
from app.services.email_service import welcome_email, otp_email
from app.tasks.email_tasks import send_email_task, send_password_reset_email

logger = logging.getLogger(__name__)

MAX_FAILED_LOGIN_ATTEMPTS = 5
LOCKOUT_DURATION = timedelta(minutes=15)


def _generate_otp(length: int = 6) -> str:
    return "".join(secrets.choice("0123456789") for _ in range(length))


class AuthService:
    """Registration, login/logout, password reset, and OTP email
    verification — everything behind /api/v1/auth."""

    def __init__(
        self,
        user_repo: UserRepository,
        audit_repo: AuditLogRepository,
    ):
        self.user_repo = user_repo
        self.audit_repo = audit_repo

    async def _issue_tokens(self, user: User) -> TokenResponse:
        """Creates a fresh access+refresh pair and records the refresh
        token's jti so refresh_token() can enforce single-use rotation."""
        access_token = create_access_token(str(user.user_id), user.token_version)
        refresh_token = create_refresh_token(str(user.user_id), user.token_version)
        payload = decode_token(refresh_token)
        self.user_repo.session.add(
            RefreshToken(
                jti=payload["jti"],
                user_id=user.user_id,
                expires_at=datetime.fromtimestamp(payload["exp"], tz=timezone.utc),
            )
        )
        await self.user_repo.session.flush()
        return TokenResponse(access_token=access_token, refresh_token=refresh_token)

    async def register(
        self,
        data: UserCreate,
        ip: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> UserResponse:
        """Create an account, send a verification OTP, and return the new
        user (email_verified=False until verify_otp succeeds)."""
        existing = await self.user_repo.get_by_email(data.email)
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Email already registered",
            )

        user = await self.user_repo.create(
            {
                "email": data.email,
                "password_hash": hash_password(data.password),
                "full_name": data.full_name,
                "phone_number": data.phone_number,
            }
        )

        # Assign default roles: Voter and Event Attendee
        # Election Administrator and Event Organizer are granted when the user creates an organization
        await self.user_repo.grant_roles_by_name(user.user_id, ["Voter", "Event Attendee"])

        otp = _generate_otp()
        user.otp_code = otp
        user.otp_expires_at = datetime.now(timezone.utc) + timedelta(
            minutes=settings.OTP_EXPIRE_MINUTES
        )
        await self.user_repo.session.flush()

        # Async via Celery so registration doesn't block on SMTP.
        subject, html = welcome_email(data.full_name)
        send_email_task.delay(data.email, subject, html)

        otp_subject, otp_html = otp_email(data.full_name, otp)
        send_email_task.delay(data.email, otp_subject, otp_html)

        await self.audit_repo.log_action(
            action_type="REGISTER",
            entity_type="User",
            entity_id=user.user_id,
            user_id=user.user_id,
            ip_address=ip,
            user_agent=user_agent,
        )

        user = await self.user_repo.get_with_roles(user.user_id)
        return self._to_response(user)

    async def verify_otp(self, email: str, otp_code: str) -> dict:
        """Verify an OTP code and mark email as verified."""
        user = await self.user_repo.get_by_email(email)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        if user.email_verified:
            return {"message": "Email already verified"}

        if not user.otp_code or user.otp_code != otp_code:
            raise HTTPException(status_code=400, detail="Invalid OTP code")

        if user.otp_expires_at and user.otp_expires_at < datetime.now(timezone.utc):
            raise HTTPException(status_code=400, detail="OTP has expired")

        user.email_verified = True
        user.otp_code = None
        user.otp_expires_at = None
        await self.user_repo.session.flush()

        return {"message": "Email verified successfully"}

    async def resend_otp(self, email: str) -> dict:
        """Generate and send a new OTP."""
        user = await self.user_repo.get_by_email(email)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        if user.email_verified:
            return {"message": "Email already verified"}

        otp = _generate_otp()
        user.otp_code = otp
        user.otp_expires_at = datetime.now(timezone.utc) + timedelta(
            minutes=settings.OTP_EXPIRE_MINUTES
        )
        await self.user_repo.session.flush()

        subject, html = otp_email(user.full_name, otp)
        send_email_task.delay(email, subject, html)

        return {"message": "OTP sent"}

    async def login(
        self,
        data: UserLogin,
        ip: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> TokenResponse:
        """Verify credentials and issue an access+refresh pair. Locks the
        account for LOCKOUT_DURATION after MAX_FAILED_LOGIN_ATTEMPTS
        consecutive failures."""
        user = await self.user_repo.get_by_email(data.email)

        if user and user.locked_until and user.locked_until > datetime.now(timezone.utc):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Account temporarily locked due to repeated failed logins. Try again later.",
            )

        if not user or not verify_password(data.password, user.password_hash):
            if user:
                user.failed_login_attempts += 1
                if user.failed_login_attempts >= MAX_FAILED_LOGIN_ATTEMPTS:
                    user.locked_until = datetime.now(timezone.utc) + LOCKOUT_DURATION
                await self.user_repo.session.flush()
            await self.audit_repo.log_action(
                action_type="LOGIN_FAILED",
                entity_type="User",
                changes={"email": data.email},
                ip_address=ip,
                user_agent=user_agent,
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password",
            )

        if not user.email_verified:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Email not verified. Please verify your email first.",
            )

        if user.account_status != "active":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Account is not active",
            )

        user.failed_login_attempts = 0
        user.locked_until = None
        await self.user_repo.session.flush()

        tokens = await self._issue_tokens(user)

        await self.user_repo.update_last_login(user.user_id)

        await self.audit_repo.log_action(
            action_type="LOGIN",
            entity_type="User",
            entity_id=user.user_id,
            user_id=user.user_id,
            ip_address=ip,
            user_agent=user_agent,
        )

        return tokens

    async def refresh_token(self, refresh_token: str) -> TokenResponse:
        """Redeem a refresh token for a new access+refresh pair. Single-use:
        the presented token is revoked here regardless of outcome, so
        replaying it (e.g. a stolen copy used after the legitimate client
        already rotated) is rejected and treated as a signal to kill every
        session for the user."""
        payload = decode_token(refresh_token)
        if payload is None or payload.get("type") != "refresh":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid refresh token",
            )

        user_id = payload.get("sub")
        user = await self.user_repo.get_by_id(UUID(user_id), id_field="user_id")
        if not user or user.account_status != "active":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid refresh token",
            )

        if payload.get("ver", 0) != user.token_version:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid refresh token",
            )

        jti = payload.get("jti")
        if jti:
            result = await self.user_repo.session.execute(
                select(RefreshToken).where(RefreshToken.jti == jti)
            )
            token_row = result.scalar_one_or_none()

            if token_row is None or token_row.revoked:
                # Signature is valid but the token was never issued via this
                # flow (pre-rotation token) or has already been rotated once
                # before — a second use of an already-rotated token is a
                # strong signal of theft/replay. Kill every session for this
                # user rather than just denying this one request.
                if token_row is not None:
                    user.token_version += 1
                    await self.user_repo.session.flush()
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid refresh token",
                )

            if token_row.expires_at < datetime.now(timezone.utc):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid refresh token",
                )

            token_row.revoked = True
            await self.user_repo.session.flush()

        return await self._issue_tokens(user)

    async def logout(self, refresh_token: str) -> None:
        """Bumps token_version so every access/refresh token issued so far —
        across every device, not just the caller's — stops validating.
        There's no per-session table, so this is deliberately all-or-nothing."""
        payload = decode_token(refresh_token)
        if payload is None or payload.get("type") != "refresh":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid refresh token",
            )

        user_id = payload.get("sub")
        user = await self.user_repo.get_by_id(UUID(user_id), id_field="user_id")
        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid refresh token",
            )

        jti = payload.get("jti")
        if jti:
            result = await self.user_repo.session.execute(
                select(RefreshToken).where(RefreshToken.jti == jti)
            )
            token_row = result.scalar_one_or_none()
            if token_row:
                token_row.revoked = True

        user.token_version += 1
        await self.user_repo.session.flush()

    async def verify_email(self, token: str) -> None:
        """Consume a create_email_verification_token link."""
        payload = decode_token(token)
        if payload is None or payload.get("type") != "email_verify":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid verification token",
            )

        user_id = payload.get("sub")
        user = await self.user_repo.get_by_id(UUID(user_id), id_field="user_id")
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        user.email_verified = True
        await self.user_repo.session.flush()

    async def forgot_password(self, email: str) -> None:
        """Silently no-ops for unknown emails (anti-enumeration) — the
        caller always returns the same generic response either way."""
        user = await self.user_repo.get_by_email(email)
        if user:
            token = create_password_reset_token(str(user.user_id))
            send_password_reset_email.delay(email, token)

    async def reset_password(self, token: str, new_password: str) -> None:
        """Consume a create_password_reset_token link. Bumps token_version
        to kill every existing session, since a leaked old password is the
        usual reason someone resets it."""
        payload = decode_token(token)
        if payload is None or payload.get("type") != "password_reset":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid reset token",
            )

        user_id = payload.get("sub")
        user = await self.user_repo.get_by_id(UUID(user_id), id_field="user_id")
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        user.password_hash = hash_password(new_password)
        user.token_version += 1
        await self.user_repo.session.flush()

    async def change_password(
        self, user: User, current_password: str, new_password: str
    ) -> None:
        """Authenticated password change; also bumps token_version (see
        reset_password)."""
        if not verify_password(current_password, user.password_hash):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Current password is incorrect",
            )
        user.password_hash = hash_password(new_password)
        user.token_version += 1
        await self.user_repo.session.flush()

    @staticmethod
    def _to_response(
        user: User,
        has_organization: bool = False,
        organization_verified: bool = False,
    ) -> UserResponse:
        roles = [ur.role.role_name for ur in user.user_roles] if user.user_roles else []
        return UserResponse(
            user_id=user.user_id,
            email=user.email,
            full_name=user.full_name,
            phone_number=user.phone_number,
            email_verified=user.email_verified,
            account_status=user.account_status,
            has_organization=has_organization,
            organization_verified=organization_verified,
            created_at=user.created_at,
            roles=roles,
        )
