from typing import List
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.support_chat_stream import notify_new_message, notify_queue_changed
from app.models.organization import Organization
from app.models.support_ticket import SupportTicket
from app.repositories.organization_repository import OrganizationRepository
from app.repositories.support_ticket_repository import SupportTicketRepository


class LiveChatService:
    """The queue/claim workflow on top of SupportTicket/SupportMessage —
    email tickets (SupportService) and chat share the same tables, this
    just owns the parts unique to a live conversation: waiting queue,
    first-to-claim, and pushing an SSE event on every mutation."""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.tickets = SupportTicketRepository(SupportTicket, db)
        self.orgs = OrganizationRepository(Organization, db)

    async def start_chat(self, user_id: UUID, message: str) -> SupportTicket:
        org_id = await self.orgs.get_primary_org_id_for_user(user_id)
        # Priority support is open to every plan now — every chat queues as
        # priority, which in practice just means the queue is FIFO again
        # (the sort in get_waiting_chats() is a no-op once priority never
        # varies, kept as-is rather than removed for the day a real
        # non-plan priority signal shows up).
        ticket = await self.tickets.create(
            {
                "user_id": user_id,
                "org_id": org_id,
                "subject": "Live chat",
                "category": "Live Chat",
                "channel": "chat",
                "status": "Waiting",
                "priority": True,
            }
        )
        await self.tickets.add_message(ticket.ticket_id, "customer", user_id, message)
        await notify_queue_changed()
        return await self.tickets.get_with_messages(ticket.ticket_id)

    async def send_customer_message(self, ticket_id: UUID, user_id: UUID, text: str) -> SupportTicket:
        ticket = await self._get_owned_chat(ticket_id, user_id)
        await self.tickets.add_message(ticket_id, "customer", user_id, text)
        await notify_new_message(ticket_id)
        return await self.tickets.get_with_messages(ticket_id)

    async def get_chat_for_customer(self, ticket_id: UUID, user_id: UUID) -> SupportTicket:
        return await self._get_owned_chat(ticket_id, user_id)

    async def list_waiting(self) -> List[SupportTicket]:
        return await self.tickets.get_waiting_chats()

    async def claim(self, ticket_id: UUID, agent_user_id: UUID) -> SupportTicket:
        claimed = await self.tickets.claim_chat(ticket_id, agent_user_id)
        if not claimed:
            raise HTTPException(status_code=409, detail="This chat has already been claimed")
        await notify_queue_changed()
        await notify_new_message(ticket_id)
        return await self.tickets.get_with_messages(ticket_id)

    async def send_agent_message(self, ticket_id: UUID, agent_user_id: UUID, text: str) -> SupportTicket:
        ticket = await self._get_chat(ticket_id)
        await self.tickets.add_message(ticket_id, "agent", agent_user_id, text)
        await notify_new_message(ticket_id)
        return await self.tickets.get_with_messages(ticket_id)

    async def close(self, ticket_id: UUID) -> SupportTicket:
        ticket = await self._get_chat(ticket_id)
        ticket.status = "Closed"
        await self.db.flush()
        await notify_queue_changed()
        await notify_new_message(ticket_id)
        return await self.tickets.get_with_messages(ticket_id)

    async def get_for_stream(self, ticket_id: UUID) -> SupportTicket:
        return await self._get_chat(ticket_id)

    async def _get_chat(self, ticket_id: UUID) -> SupportTicket:
        ticket = await self.tickets.get_with_messages(ticket_id)
        if not ticket or ticket.channel != "chat":
            raise HTTPException(status_code=404, detail="Chat not found")
        return ticket

    async def _get_owned_chat(self, ticket_id: UUID, user_id: UUID) -> SupportTicket:
        ticket = await self._get_chat(ticket_id)
        if ticket.user_id != user_id:
            raise HTTPException(status_code=404, detail="Chat not found")
        return ticket
