from datetime import datetime
from typing import List, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.event_invitation import EventInvitation
from app.repositories.base import BaseRepository


class EventInvitationRepository(BaseRepository[EventInvitation]):
    def __init__(self, model, session: AsyncSession):
        super().__init__(model, session)

    async def create_invitation(
        self,
        event_id: UUID,
        email: str,
        name: Optional[str],
        invited_by: Optional[UUID],
        expires_at: datetime,
    ) -> EventInvitation:
        inv = EventInvitation(
            event_id=event_id,
            email=email.lower(),
            name=name,
            invited_by=invited_by,
            expires_at=expires_at,
        )
        self.session.add(inv)
        await self.session.flush()
        await self.session.refresh(inv)
        return inv

    async def get_pending_invitation(self, event_id: UUID, email: str) -> Optional[EventInvitation]:
        result = await self.session.execute(
            select(EventInvitation).where(
                EventInvitation.event_id == event_id,
                EventInvitation.email == email.lower(),
                EventInvitation.status == "pending",
            )
        )
        return result.scalar_one_or_none()

    async def get_by_token(self, token: str) -> Optional[EventInvitation]:
        result = await self.session.execute(
            select(EventInvitation).where(EventInvitation.token == token)
        )
        return result.scalar_one_or_none()

    async def get_by_event(self, event_id: UUID) -> List[EventInvitation]:
        result = await self.session.execute(
            select(EventInvitation)
            .where(EventInvitation.event_id == event_id)
            .order_by(EventInvitation.created_at.desc())
        )
        return list(result.scalars().all())

    async def set_status(self, invitation_id: UUID, status: str) -> None:
        inv = await self.get_by_id(invitation_id, id_field="invitation_id")
        if inv:
            inv.status = status
            await self.session.flush()
