from typing import List, Optional
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.election import Category
from app.models.event import Event, TicketType
from app.repositories.base import BaseRepository


class EventRepository(BaseRepository[Event]):
    def __init__(self, model, session: AsyncSession):
        super().__init__(model, session)

    async def get_with_ticket_types(self, event_id: UUID) -> Optional[Event]:
        result = await self.session.execute(
            select(Event)
            .options(selectinload(Event.ticket_types))
            .where(Event.event_id == event_id)
        )
        return result.scalar_one_or_none()

    async def get_by_slug(self, slug: str) -> Optional[Event]:
        result = await self.session.execute(
            select(Event)
            .options(selectinload(Event.ticket_types))
            .where(Event.slug == slug)
        )
        return result.scalar_one_or_none()

    async def get_with_categories(self, event_id: UUID) -> Optional[Event]:
        result = await self.session.execute(
            select(Event)
            .options(selectinload(Event.categories).selectinload(Category.candidates))
            .where(Event.event_id == event_id)
        )
        return result.scalar_one_or_none()

    async def get_published_events(
        self,
        skip: int = 0,
        limit: int = 20,
        category: Optional[str] = None,
    ) -> List[Event]:
        query = select(Event).where(Event.status == "published")
        if category:
            query = query.where(Event.category == category)
        query = query.order_by(Event.event_date.asc()).offset(skip).limit(limit)
        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def get_events_by_creator(
        self,
        user_id: UUID,
        skip: int = 0,
        limit: int = 20,
    ) -> List[Event]:
        result = await self.session.execute(
            select(Event)
            .where(Event.created_by == user_id)
            .order_by(Event.created_at.desc())
            .offset(skip)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def get_events_by_creators(
        self,
        user_ids: List[UUID],
        skip: int = 0,
        limit: int = 20,
    ) -> List[Event]:
        """Events created by any of the given users — i.e. by anyone on the
        caller's team. Separate from get_events_by_creator, which the public
        per-user listing still needs."""
        if not user_ids:
            return []
        result = await self.session.execute(
            select(Event)
            .where(Event.created_by.in_(user_ids))
            .order_by(Event.created_at.desc())
            .offset(skip)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def update_status(self, event_id: UUID, status: str) -> Optional[Event]:
        event = await self.get_by_id(event_id, id_field="event_id")
        if event:
            event.status = status
            await self.session.flush()
            await self.session.refresh(event)
        return event


class TicketTypeRepository(BaseRepository[TicketType]):
    def __init__(self, model, session: AsyncSession):
        super().__init__(model, session)

    async def get_by_event(self, event_id: UUID) -> List[TicketType]:
        result = await self.session.execute(
            select(TicketType)
            .where(TicketType.event_id == event_id)
            .order_by(TicketType.price.asc())
        )
        return list(result.scalars().all())

    async def decrement_available(
        self, ticket_type_id: UUID, quantity: int
    ) -> bool:
        result = await self.session.execute(
            update(TicketType)
            .where(
                TicketType.ticket_type_id == ticket_type_id,
                TicketType.quantity_available >= quantity,
                TicketType.status == "active",
            )
            .values(
                quantity_available=TicketType.quantity_available - quantity,
                quantity_sold=TicketType.quantity_sold + quantity,
            )
            .returning(TicketType.ticket_type_id)
        )
        row = result.first()
        await self.session.flush()
        return row is not None

    async def increment_available(
        self, ticket_type_id: UUID, quantity: int
    ) -> None:
        """Compensating write for decrement_available — used to undo an
        earlier item's successful stock decrement within the same
        multi-item purchase when a later item can't be fulfilled, so the
        whole purchase's stock effect is all-or-nothing."""
        await self.session.execute(
            update(TicketType)
            .where(TicketType.ticket_type_id == ticket_type_id)
            .values(
                quantity_available=TicketType.quantity_available + quantity,
                quantity_sold=TicketType.quantity_sold - quantity,
            )
        )
        await self.session.flush()
