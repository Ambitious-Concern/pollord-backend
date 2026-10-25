from typing import List
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.event_broadcast import EventBroadcast
from app.repositories.base import BaseRepository


class EventBroadcastRepository(BaseRepository[EventBroadcast]):
    def __init__(self, model, session: AsyncSession):
        super().__init__(model, session)

    async def get_by_event(self, event_id: UUID) -> List[EventBroadcast]:
        result = await self.session.execute(
            select(EventBroadcast)
            .where(EventBroadcast.event_id == event_id)
            .order_by(EventBroadcast.created_at.desc())
        )
        return list(result.scalars().all())
