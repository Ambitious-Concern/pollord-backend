from typing import List
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization import OrgRole
from app.repositories.base import BaseRepository


class OrgRoleRepository(BaseRepository[OrgRole]):
    def __init__(self, model, session: AsyncSession):
        super().__init__(model, session)

    async def get_by_org(self, org_id: UUID) -> List[OrgRole]:
        result = await self.session.execute(
            select(OrgRole).where(OrgRole.org_id == org_id).order_by(OrgRole.name)
        )
        return list(result.scalars().all())

    async def get_by_name(self, org_id: UUID, name: str) -> OrgRole | None:
        result = await self.session.execute(
            select(OrgRole).where(OrgRole.org_id == org_id, OrgRole.name == name)
        )
        return result.scalar_one_or_none()
