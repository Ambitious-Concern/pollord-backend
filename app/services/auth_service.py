import logging
from typing import Optional
from uuid import UUID

from fastapi import HTTPException, status

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
from app.models.user import User
from app.repositories.audit_log_repository import AuditLogRepository
from app.repositories.user_repository import UserRepository
from app.schemas.user import (
    TokenResponse,
    UserCreate,
    UserLogin,
    UserResponse,
)

logger = logging.getLogger(__name__)


class AuthService:
    def __init__(
        self,
        user_repo: UserRepository,
        audit_repo: AuditLogRepository,
    ):
        self.user_repo = user_repo
        self.audit_repo = audit_repo

    async def register(
        self,
        data: UserCreate,
        ip: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> UserResponse:
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

        # Assign default Voter role
        voter_role = await self.user_repo.get_role_by_name("Voter")
        if voter_role:
            await self.user_repo.assign_role(user.user_id, voter_role.role_id)

        # Audit log
        await self.audit_repo.log_action(
            action_type="REGISTER",
            entity_type="User",
            entity_id=user.user_id,
            user_id=user.user_id,
            ip_address=ip,
            user_agent=user_agent,
        )

        # Reload user with roles
        user = await self.user_repo.get_with_roles(user.user_id)
        return self._to_response(user)

    async def login(
        self,
        data: UserLogin,
        ip: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> TokenResponse:
        user = await self.user_repo.get_by_email(data.email)
        if not user or not verify_password(data.password, user.password_hash):
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

        if user.account_status != "active":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Account is not active",
            )

        access_token = create_access_token(str(user.user_id))
        refresh_token = create_refresh_token(str(user.user_id))

        await self.user_repo.update_last_login(user.user_id)

        await self.audit_repo.log_action(
            action_type="LOGIN",
            entity_type="User",
            entity_id=user.user_id,
            user_id=user.user_id,
            ip_address=ip,
            user_agent=user_agent,
        )

        return TokenResponse(
            access_token=access_token,
            refresh_token=refresh_token,
        )

    async def refresh_token(self, refresh_token: str) -> TokenResponse:
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

        access_token = create_access_token(str(user.user_id))
        new_refresh_token = create_refresh_token(str(user.user_id))

        return TokenResponse(
            access_token=access_token,
            refresh_token=new_refresh_token,
        )

    async def verify_email(self, token: str) -> None:
        payload = decode_token(token)
        if payload is None or payload.get("type") != "email_verify":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid verification token",
            )

        user_id = payload.get("sub")
        user = await self.user_repo.get_by_id(UUID(user_id), id_field="user_id")
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found",
            )

        user.email_verified = True
        await self.user_repo.session.flush()

    async def forgot_password(self, email: str) -> None:
        user = await self.user_repo.get_by_email(email)
        if user:
            token = create_password_reset_token(str(user.user_id))
            # In production, queue email task here
            logger.info(f"Password reset token generated for {email}: {token}")

    async def reset_password(self, token: str, new_password: str) -> None:
        payload = decode_token(token)
        if payload is None or payload.get("type") != "password_reset":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid reset token",
            )

        user_id = payload.get("sub")
        user = await self.user_repo.get_by_id(UUID(user_id), id_field="user_id")
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found",
            )

        user.password_hash = hash_password(new_password)
        await self.user_repo.session.flush()

    async def change_password(
        self, user: User, current_password: str, new_password: str
    ) -> None:
        if not verify_password(current_password, user.password_hash):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Current password is incorrect",
            )
        user.password_hash = hash_password(new_password)
        await self.user_repo.session.flush()

    @staticmethod
    def _to_response(user: User) -> UserResponse:
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
