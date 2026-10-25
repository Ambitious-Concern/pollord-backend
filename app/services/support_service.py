from typing import List, Optional
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization import Organization
from app.models.support_ticket import SupportTicket
from app.repositories.organization_repository import OrganizationRepository
from app.repositories.support_ticket_repository import SupportTicketRepository


class SupportService:
    """Ticket lifecycle: creation, replies, status. Priority support is open
    to every plan, so every ticket queues as priority — see the matching
    note in LiveChatService.start_chat."""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.tickets = SupportTicketRepository(SupportTicket, db)
        self.orgs = OrganizationRepository(Organization, db)

    async def create_ticket(
        self, user_id: UUID, category: str, subject: str, message: str
    ) -> SupportTicket:
        org_id = await self.orgs.get_primary_org_id_for_user(user_id)

        ticket = await self.tickets.create(
            {
                "user_id": user_id,
                "org_id": org_id,
                "subject": subject,
                "category": category,
                "priority": True,
            }
        )
        await self.tickets.add_message(ticket.ticket_id, "customer", user_id, message)
        return await self.tickets.get_with_messages(ticket.ticket_id)

    async def list_my_tickets(self, user_id: UUID) -> List[SupportTicket]:
        return await self.tickets.get_by_user(user_id)

    async def get_my_ticket(self, ticket_id: UUID, user_id: UUID) -> SupportTicket:
        ticket = await self.tickets.get_with_messages(ticket_id)
        if ticket is None or ticket.user_id != user_id:
            raise HTTPException(status_code=404, detail="Ticket not found")
        return ticket

    async def reply_as_customer(self, ticket_id: UUID, user_id: UUID, text: str) -> SupportTicket:
        ticket = await self.get_my_ticket(ticket_id, user_id)
        await self.tickets.add_message(ticket_id, "customer", user_id, text)
        # A customer reply always reopens the conversation, even a resolved one.
        ticket.status = "Open"
        await self.db.flush()
        self.db.expire(ticket, ["messages"])
        return await self.tickets.get_with_messages(ticket_id)

    async def list_all_tickets(self, status: Optional[str] = None) -> List[SupportTicket]:
        return await self.tickets.get_all_tickets(status=status)

    async def get_ticket(self, ticket_id: UUID) -> SupportTicket:
        ticket = await self.tickets.get_with_messages(ticket_id)
        if ticket is None:
            raise HTTPException(status_code=404, detail="Ticket not found")
        return ticket

    async def reply_as_agent(self, ticket_id: UUID, agent_user_id: UUID, text: str) -> SupportTicket:
        ticket = await self.get_ticket(ticket_id)
        await self.tickets.add_message(ticket_id, "agent", agent_user_id, text)
        ticket.status = "Replied"
        await self.db.flush()
        self.db.expire(ticket, ["messages"])
        return await self.tickets.get_with_messages(ticket_id)

    async def update_status(self, ticket_id: UUID, status: str) -> SupportTicket:
        ticket = await self.get_ticket(ticket_id)
        ticket.status = status
        await self.db.flush()
        await self.db.refresh(ticket)
        return ticket
