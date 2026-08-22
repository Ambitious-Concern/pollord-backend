from datetime import datetime, timezone
from typing import List, Optional, Tuple
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.event import Event
from app.models.ticket import Ticket, TicketPurchase, owned_by
from app.models.user import User
from app.repositories.base import BaseRepository


class TicketRepository(BaseRepository[Ticket]):
    def __init__(self, model, session: AsyncSession):
        super().__init__(model, session)

    async def get_by_ticket_code(self, code: str) -> Optional[Ticket]:
        result = await self.session.execute(
            select(Ticket)
            .where(Ticket.ticket_code == code)
            .options(selectinload(Ticket.event), selectinload(Ticket.user))
        )
        return result.scalar_one_or_none()

    async def get_user_tickets(
        self,
        user_id: UUID,
        email: Optional[str] = None,
        skip: int = 0,
        limit: int = 20,
    ) -> List[Ticket]:
        result = await self.session.execute(
            select(Ticket)
            .where(owned_by(user_id, email))
            .options(
                selectinload(Ticket.event),
                selectinload(Ticket.ticket_type),
                selectinload(Ticket.user),
            )
            .order_by(Ticket.purchase_date.desc())
            .offset(skip)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def get_organizer_tickets(
        self,
        organizer_ids: List[UUID],
        event_id: Optional[UUID] = None,
        skip: int = 0,
        limit: int = 50,
    ) -> List[Ticket]:
        """Every ticket issued for an event created by any of the given users —
        the caller and their organization teammates — optionally narrowed to a
        single event."""
        if not organizer_ids:
            return []
        conditions = [Event.created_by.in_(organizer_ids)]
        if event_id:
            conditions.append(Event.event_id == event_id)
        result = await self.session.execute(
            select(Ticket)
            .join(Event, Event.event_id == Ticket.event_id)
            .where(*conditions)
            .options(
                selectinload(Ticket.event),
                selectinload(Ticket.ticket_type),
                selectinload(Ticket.user),
            )
            .order_by(Ticket.purchase_date.desc())
            .offset(skip)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def get_event_tickets(self, event_id: UUID) -> List[Ticket]:
        result = await self.session.execute(
            select(Ticket).where(Ticket.event_id == event_id)
        )
        return list(result.scalars().all())

    async def mark_as_used(
        self, ticket_id: UUID, scanned_by: Optional[UUID]
    ) -> Optional[Ticket]:
        result = await self.session.execute(
            select(Ticket)
            .where(Ticket.ticket_id == ticket_id)
            .options(selectinload(Ticket.event), selectinload(Ticket.user))
        )
        ticket = result.scalar_one_or_none()
        if ticket and ticket.ticket_status == "valid":
            ticket.ticket_status = "used"
            ticket.used_at = datetime.now(timezone.utc)
            ticket.scanned_by = scanned_by
            await self.session.flush()
            await self.session.refresh(ticket)
            return ticket
        return None

    async def count_by_event(self, event_id: UUID) -> int:
        result = await self.session.execute(
            select(func.count()).select_from(Ticket).where(
                Ticket.event_id == event_id
            )
        )
        return result.scalar_one()

    async def count_used_by_event(self, event_id: UUID) -> int:
        result = await self.session.execute(
            select(func.count()).select_from(Ticket).where(
                Ticket.event_id == event_id,
                Ticket.ticket_status == "used",
            )
        )
        return result.scalar_one()

    async def count_user_tickets_for_type(
        self, user_id: UUID, ticket_type_id: UUID
    ) -> int:
        result = await self.session.execute(
            select(func.count()).select_from(Ticket).where(
                Ticket.user_id == user_id,
                Ticket.ticket_type_id == ticket_type_id,
                Ticket.ticket_status != "cancelled",
            )
        )
        return result.scalar_one()

    async def count_guest_tickets_for_type(
        self, guest_email: str, ticket_type_id: UUID
    ) -> int:
        result = await self.session.execute(
            select(func.count()).select_from(Ticket).where(
                Ticket.guest_email == guest_email,
                Ticket.ticket_type_id == ticket_type_id,
                Ticket.ticket_status != "cancelled",
            )
        )
        return result.scalar_one()


class TicketPurchaseRepository(BaseRepository[TicketPurchase]):
    def __init__(self, model, session: AsyncSession):
        super().__init__(model, session)

    async def get_by_user(
        self, user_id: UUID, skip: int = 0, limit: int = 20
    ) -> List[TicketPurchase]:
        result = await self.session.execute(
            select(TicketPurchase)
            .where(TicketPurchase.user_id == user_id)
            .order_by(TicketPurchase.purchased_at.desc())
            .offset(skip)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def get_by_event(self, event_id: UUID) -> List[TicketPurchase]:
        result = await self.session.execute(
            select(TicketPurchase)
            .where(TicketPurchase.event_id == event_id)
            .order_by(TicketPurchase.purchased_at.desc())
        )
        return list(result.scalars().all())

    async def get_with_details(self, purchase_id: UUID) -> Optional[TicketPurchase]:
        """A purchase with everything needed to rebuild its confirmation
        email: the buyer, the event title, and the issued tickets."""
        result = await self.session.execute(
            select(TicketPurchase)
            .where(TicketPurchase.purchase_id == purchase_id)
            .options(
                selectinload(TicketPurchase.user),
                selectinload(TicketPurchase.event),
                selectinload(TicketPurchase.tickets),
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    def _admin_search_conditions(
        search: Optional[str],
        event_id: Optional[UUID],
        payment_status: Optional[str],
        email_status: Optional[str],
    ) -> list:
        conditions = []
        if event_id:
            conditions.append(TicketPurchase.event_id == event_id)
        if payment_status:
            conditions.append(TicketPurchase.payment_status == payment_status)
        if email_status == "unknown":
            conditions.append(TicketPurchase.confirmation_email_status.is_(None))
        elif email_status:
            conditions.append(
                TicketPurchase.confirmation_email_status == email_status
            )
        if search:
            term = f"%{search.lower()}%"
            # Buyers arrive at support with any one of these — a guest email,
            # an account email, a name, or a Paystack reference off a receipt.
            conditions.append(
                or_(
                    func.lower(TicketPurchase.guest_email).like(term),
                    func.lower(TicketPurchase.guest_name).like(term),
                    func.lower(TicketPurchase.payment_reference).like(term),
                    func.lower(User.email).like(term),
                    func.lower(User.full_name).like(term),
                )
            )
        return conditions

    async def search_for_admin(
        self,
        search: Optional[str] = None,
        event_id: Optional[UUID] = None,
        payment_status: Optional[str] = None,
        email_status: Optional[str] = None,
        skip: int = 0,
        limit: int = 50,
    ) -> Tuple[List[TicketPurchase], int]:
        """Purchases across every event, for platform-admin support work.

        Unlike the organizer-facing queries this is deliberately unscoped by
        owner. Returns (page, total_matching) so the caller can paginate.
        """
        conditions = self._admin_search_conditions(
            search, event_id, payment_status, email_status
        )

        # outerjoin, not join — guest purchases have no user row and must
        # still appear.
        rows = await self.session.execute(
            select(TicketPurchase)
            .outerjoin(User, User.user_id == TicketPurchase.user_id)
            .where(*conditions)
            .options(
                selectinload(TicketPurchase.user),
                selectinload(TicketPurchase.event),
                selectinload(TicketPurchase.tickets),
            )
            .order_by(TicketPurchase.purchased_at.desc())
            .offset(skip)
            .limit(limit)
        )
        total = await self.session.execute(
            select(func.count())
            .select_from(TicketPurchase)
            .outerjoin(User, User.user_id == TicketPurchase.user_id)
            .where(*conditions)
        )
        return list(rows.scalars().all()), total.scalar_one()

    async def get_revenue_by_event(self, event_id: UUID) -> float:
        result = await self.session.execute(
            select(func.sum(TicketPurchase.total_amount)).where(
                TicketPurchase.event_id == event_id,
                TicketPurchase.payment_status == "completed",
            )
        )
        return float(result.scalar_one() or 0)
