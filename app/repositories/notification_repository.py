from typing import List, Optional
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import Notification
from app.repositories.base import BaseRepository


class NotificationRepository(BaseRepository[Notification]):
    """Writes and queries Notification rows. notify_org() is the write-side
    helper other services call directly (mirrors OrgActivityLogRepository's
    log_activity()) — there's no separate write-service layer for the same
    reason OrgActivityLog doesn't have one."""

    def __init__(self, model, session: AsyncSession):
        super().__init__(model, session)

    async def notify_org(
        self,
        org_id: UUID,
        member_ids: List[UUID],
        type: str,
        title: str,
        description: str,
        link: Optional[str] = None,
    ) -> None:
        if not member_ids:
            return
        self.session.add_all([
            Notification(
                user_id=member_id, org_id=org_id, type=type,
                title=title, description=description, link=link,
            )
            for member_id in member_ids
        ])
        await self.session.flush()

    async def list_for_user(
        self, user_id: UUID, *, skip: int = 0, limit: int = 20
    ) -> List[Notification]:
        result = await self.session.execute(
            select(Notification)
            .where(Notification.user_id == user_id)
            .order_by(Notification.created_at.desc())
            .offset(skip)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def count_unread(self, user_id: UUID) -> int:
        result = await self.session.execute(
            select(func.count()).select_from(Notification).where(
                Notification.user_id == user_id, Notification.is_read.is_(False)
            )
        )
        return result.scalar_one()

    async def mark_read(self, notification_id: UUID, user_id: UUID) -> bool:
        result = await self.session.execute(
            update(Notification)
            .where(Notification.notification_id == notification_id, Notification.user_id == user_id)
            .values(is_read=True)
            .returning(Notification.notification_id)
        )
        await self.session.flush()
        return result.first() is not None

    async def mark_all_read(self, user_id: UUID) -> int:
        result = await self.session.execute(
            update(Notification)
            .where(Notification.user_id == user_id, Notification.is_read.is_(False))
            .values(is_read=True)
            .returning(Notification.notification_id)
        )
        await self.session.flush()
        return len(result.all())
