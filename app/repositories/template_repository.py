from typing import List
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.template import Template
from app.repositories.base import BaseRepository


class TemplateRepository(BaseRepository[Template]):
    """Template CRUD. get_visible_to is the one non-obvious query — templates
    are the app's only cross-tenant-visible resource (see the migration's RLS
    docstring), so "visible to this user" is org-or-personal PLUS anything
    marked public, not just org-or-personal like everywhere else."""

    def __init__(self, model, session: AsyncSession):
        super().__init__(model, session)

    async def get_visible_to(self, user_id: UUID, org_id: UUID | None) -> List[Template]:
        own_scope = Template.org_id == org_id if org_id else Template.created_by == user_id
        result = await self.session.execute(
            select(Template)
            .where(or_(Template.visibility == "public", own_scope))
            .order_by(Template.created_at.desc())
        )
        return list(result.scalars().all())
