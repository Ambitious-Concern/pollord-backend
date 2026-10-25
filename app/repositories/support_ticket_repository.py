from typing import List, Optional
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.support_ticket import SupportMessage, SupportTicket
from app.repositories.base import BaseRepository


class SupportTicketRepository(BaseRepository[SupportTicket]):
    """Ticket queries: creator-scoped listing, admin listing, message appends."""

    def __init__(self, model, session: AsyncSession):
        super().__init__(model, session)

    async def get_with_messages(self, ticket_id: UUID) -> Optional[SupportTicket]:
        result = await self.session.execute(
            select(SupportTicket)
            .options(selectinload(SupportTicket.messages))
            .where(SupportTicket.ticket_id == ticket_id)
        )
        return result.scalar_one_or_none()

    async def get_by_user(self, user_id: UUID) -> List[SupportTicket]:
        result = await self.session.execute(
            select(SupportTicket)
            .options(selectinload(SupportTicket.messages))
            .where(SupportTicket.user_id == user_id)
            .order_by(SupportTicket.created_at.desc())
        )
        return list(result.scalars().all())

    async def get_all_tickets(
        self, *, skip: int = 0, limit: int = 50, status: Optional[str] = None
    ) -> List[SupportTicket]:
        query = select(SupportTicket).options(selectinload(SupportTicket.messages))
        if status:
            query = query.where(SupportTicket.status == status)
        query = query.order_by(
            SupportTicket.priority.desc(), SupportTicket.created_at.desc()
        ).offset(skip).limit(limit)
        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def add_message(
        self, ticket_id: UUID, sender_type: str, sender_user_id: UUID, text: str
    ) -> SupportMessage:
        message = SupportMessage(
            ticket_id=ticket_id,
            sender_type=sender_type,
            sender_user_id=sender_user_id,
            text=text,
        )
        self.session.add(message)
        await self.session.flush()
        await self.session.refresh(message)

        # If the parent ticket is already in the identity map with .messages
        # eager-loaded (e.g. an earlier get_with_messages this request),
        # selectinload would otherwise reuse that stale, pre-insert
        # collection on the next fetch instead of re-querying it — same bug
        # class as NominationFormRepository.replace_fields.
        ticket = await self.session.get(SupportTicket, ticket_id)
        if ticket:
            self.session.expire(ticket, ["messages"])
        return message

    async def get_waiting_chats(self) -> List[SupportTicket]:
        """Priority (Premium+) chats first, oldest-first within each tier so
        a steady stream of priority chats can't starve a standard one
        forever — same precedence rule as get_all_tickets's email queue."""
        result = await self.session.execute(
            select(SupportTicket)
            .options(selectinload(SupportTicket.messages))
            .where(SupportTicket.channel == "chat", SupportTicket.status == "Waiting")
            .order_by(SupportTicket.priority.desc(), SupportTicket.created_at.asc())
        )
        return list(result.scalars().all())

    async def claim_chat(self, ticket_id: UUID, agent_user_id: UUID) -> bool:
        """Atomic first-to-claim: only succeeds if the chat is still waiting,
        so two agents opening the same queue entry at once can't both win it
        (same WHERE-guarded UPDATE pattern as ticket stock decrements)."""
        result = await self.session.execute(
            update(SupportTicket)
            .where(SupportTicket.ticket_id == ticket_id, SupportTicket.status == "Waiting")
            .values(status="Active", assigned_agent_id=agent_user_id)
            .returning(SupportTicket.ticket_id)
        )
        return result.first() is not None
