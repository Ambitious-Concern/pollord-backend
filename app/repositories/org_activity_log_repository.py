from typing import List, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.org_activity_log import OrgActivityLog
from app.repositories.base import BaseRepository


class OrgActivityLogRepository(BaseRepository[OrgActivityLog]):
    """Writes and queries OrgActivityLog rows — the org-scoped team feed,
    distinct from AuditLogRepository's platform-wide admin log."""

    def __init__(self, model, session: AsyncSession):
        super().__init__(model, session)

    async def log_activity(
        self,
        org_id: UUID,
        category: str,
        verb: str,
        target_name: str,
        user_id: Optional[UUID] = None,
        detail: Optional[str] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> OrgActivityLog:
        entry = OrgActivityLog(
            org_id=org_id,
            user_id=user_id,
            category=category,
            verb=verb,
            target_name=target_name,
            detail=detail,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        self.session.add(entry)
        await self.session.flush()
        return entry

    async def get_by_org(
        self,
        org_id: UUID,
        *,
        category: Optional[str] = None,
        skip: int = 0,
        limit: int = 50,
    ) -> List[OrgActivityLog]:
        query = (
            select(OrgActivityLog)
            .options(selectinload(OrgActivityLog.user))
            .where(OrgActivityLog.org_id == org_id)
        )
        if category:
            query = query.where(OrgActivityLog.category == category)
        query = query.order_by(OrgActivityLog.created_at.desc()).offset(skip).limit(limit)
        result = await self.session.execute(query)
        return list(result.scalars().all())
