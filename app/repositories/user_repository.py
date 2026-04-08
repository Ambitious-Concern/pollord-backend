from datetime import datetime, timezone
from typing import List, Optional
from uuid import UUID

from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.user import User, Role, UserRole
from app.repositories.base import BaseRepository


class UserRepository(BaseRepository[User]):
    def __init__(self, model, session: AsyncSession):
        super().__init__(model, session)

    async def get_by_email(self, email: str) -> Optional[User]:
        result = await self.session.execute(
            select(User).where(User.email == email)
        )
        return result.scalar_one_or_none()

    async def get_with_roles(self, user_id: UUID) -> Optional[User]:
        result = await self.session.execute(
            select(User)
            .options(selectinload(User.user_roles).selectinload(UserRole.role))
            .where(User.user_id == user_id)
        )
        return result.scalar_one_or_none()

    async def update_last_login(self, user_id: UUID) -> None:
        user = await self.get_by_id(user_id, id_field="user_id")
        if user:
            user.last_login_at = datetime.now(timezone.utc)
            await self.session.flush()

    async def search_users(
        self, query: str, skip: int = 0, limit: int = 20
    ) -> List[User]:
        search = f"%{query}%"
        result = await self.session.execute(
            select(User)
            .where(
                or_(
                    User.email.ilike(search),
                    User.full_name.ilike(search),
                )
            )
            .offset(skip)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def update_account_status(self, user_id: UUID, status: str) -> Optional[User]:
        user = await self.get_by_id(user_id, id_field="user_id")
        if user:
            user.account_status = status
            await self.session.flush()
            await self.session.refresh(user)
        return user

    async def assign_role(
        self, user_id: UUID, role_id: UUID, assigned_by: Optional[UUID] = None
    ) -> UserRole:
        user_role = UserRole(
            user_id=user_id, role_id=role_id, assigned_by=assigned_by
        )
        self.session.add(user_role)
        await self.session.flush()
        return user_role

    async def remove_user_roles(self, user_id: UUID) -> None:
        result = await self.session.execute(
            select(UserRole).where(UserRole.user_id == user_id)
        )
        for ur in result.scalars().all():
            await self.session.delete(ur)
        await self.session.flush()

    async def get_role_by_name(self, role_name: str) -> Optional[Role]:
        result = await self.session.execute(
            select(Role).where(Role.role_name == role_name)
        )
        return result.scalar_one_or_none()

    async def get_all_roles(self) -> List[Role]:
        result = await self.session.execute(select(Role))
        return list(result.scalars().all())

    async def has_role(self, user_id: UUID, role_name: str) -> bool:
        result = await self.session.execute(
            select(UserRole)
            .join(Role, UserRole.role_id == Role.role_id)
            .where(UserRole.user_id == user_id, Role.role_name == role_name)
        )
        return result.scalar_one_or_none() is not None

    async def grant_roles_by_name(
        self, user_id: UUID, role_names: List[str], granted_by: Optional[UUID] = None
    ) -> None:
        """Assign system roles by name if not already assigned."""
        for name in role_names:
            role = await self.get_role_by_name(name)
            if role and not await self.has_role(user_id, name):
                await self.assign_role(user_id, role.role_id, assigned_by=granted_by)

    async def revoke_roles_by_name(self, user_id: UUID, role_names: List[str]) -> None:
        """Remove specific system roles from a user."""
        for name in role_names:
            result = await self.session.execute(
                select(UserRole)
                .join(Role, UserRole.role_id == Role.role_id)
                .where(UserRole.user_id == user_id, Role.role_name == name)
            )
            ur = result.scalar_one_or_none()
            if ur:
                await self.session.delete(ur)
        await self.session.flush()
