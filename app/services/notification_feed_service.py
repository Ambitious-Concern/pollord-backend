from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import Notification
from app.repositories.notification_repository import NotificationRepository
from app.schemas.notification import NotificationListResponse, NotificationResponse

# Named distinctly from app.services.notification_service (the email-sending
# wrapper) — this is the dashboard's in-app notification feed, unrelated.


class NotificationFeedService:
    def __init__(self, db: AsyncSession):
        self.repo = NotificationRepository(Notification, db)

    async def list_for_user(self, user_id: UUID, *, skip: int = 0, limit: int = 20) -> NotificationListResponse:
        rows = await self.repo.list_for_user(user_id, skip=skip, limit=limit)
        unread_count = await self.repo.count_unread(user_id)
        return NotificationListResponse(
            items=[NotificationResponse.model_validate(r) for r in rows],
            unread_count=unread_count,
        )

    async def mark_read(self, notification_id: UUID, user_id: UUID) -> bool:
        return await self.repo.mark_read(notification_id, user_id)

    async def mark_all_read(self, user_id: UUID) -> int:
        return await self.repo.mark_all_read(user_id)
