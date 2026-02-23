from typing import Optional
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.election import Election, EligibleVoter
from app.models.event import Event, TicketType
from app.models.ticket import Ticket, TicketPurchase
from app.models.user import User
from app.models.vote import Vote
from app.repositories.election_repository import ElectionRepository
from app.repositories.event_repository import EventRepository
from app.repositories.ticket_repository import TicketPurchaseRepository, TicketRepository
from app.repositories.vote_repository import VoteRepository


class AnalyticsService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_election_stats(self, election_id: UUID) -> dict:
        # Total eligible voters
        eligible_count = await self.session.execute(
            select(func.count()).select_from(EligibleVoter).where(
                EligibleVoter.election_id == election_id
            )
        )
        total_eligible = eligible_count.scalar_one()

        # Total votes cast
        vote_count = await self.session.execute(
            select(func.count()).select_from(Vote).where(
                Vote.election_id == election_id
            )
        )
        total_votes = vote_count.scalar_one()

        # Turnout
        turnout = (total_votes / total_eligible * 100) if total_eligible > 0 else 0

        # Votes timeline
        timeline = await self.session.execute(
            select(
                func.date_trunc("hour", Vote.cast_at).label("hour"),
                func.count().label("count"),
            )
            .where(Vote.election_id == election_id)
            .group_by("hour")
            .order_by("hour")
        )

        return {
            "election_id": str(election_id),
            "total_eligible_voters": total_eligible,
            "total_votes_cast": total_votes,
            "turnout_percentage": round(turnout, 2),
            "voting_timeline": [
                {"hour": str(row.hour), "count": row.count}
                for row in timeline.all()
            ],
        }

    async def get_event_stats(self, event_id: UUID) -> dict:
        # Total tickets sold
        tickets_sold = await self.session.execute(
            select(func.count()).select_from(Ticket).where(
                Ticket.event_id == event_id,
                Ticket.ticket_status != "cancelled",
            )
        )
        total_sold = tickets_sold.scalar_one()

        # Tickets used (attended)
        tickets_used = await self.session.execute(
            select(func.count()).select_from(Ticket).where(
                Ticket.event_id == event_id,
                Ticket.ticket_status == "used",
            )
        )
        total_used = tickets_used.scalar_one()

        # Revenue
        revenue = await self.session.execute(
            select(func.sum(TicketPurchase.total_amount)).where(
                TicketPurchase.event_id == event_id,
                TicketPurchase.payment_status == "completed",
            )
        )
        total_revenue = float(revenue.scalar_one() or 0)

        # Sales by type
        type_stats = await self.session.execute(
            select(
                TicketType.type_name,
                TicketType.quantity_sold,
                TicketType.quantity_available,
                TicketType.price,
            ).where(TicketType.event_id == event_id)
        )

        # Event capacity
        event = await self.session.execute(
            select(Event.capacity).where(Event.event_id == event_id)
        )
        capacity = event.scalar_one_or_none()

        attendance_rate = (total_used / total_sold * 100) if total_sold > 0 else 0

        return {
            "event_id": str(event_id),
            "total_tickets_sold": total_sold,
            "total_attended": total_used,
            "attendance_rate": round(attendance_rate, 2),
            "total_revenue": total_revenue,
            "capacity": capacity,
            "remaining_capacity": (capacity - total_sold) if capacity else None,
            "sales_by_type": [
                {
                    "type_name": row.type_name,
                    "sold": row.quantity_sold,
                    "available": row.quantity_available,
                    "price": float(row.price),
                }
                for row in type_stats.all()
            ],
        }

    async def get_system_stats(self) -> dict:
        total_users = await self.session.execute(
            select(func.count()).select_from(User)
        )
        active_elections = await self.session.execute(
            select(func.count()).select_from(Election).where(
                Election.status == "active"
            )
        )
        active_events = await self.session.execute(
            select(func.count()).select_from(Event).where(
                Event.status == "published"
            )
        )
        total_votes = await self.session.execute(
            select(func.count()).select_from(Vote)
        )
        total_tickets = await self.session.execute(
            select(func.count()).select_from(Ticket)
        )

        return {
            "total_users": total_users.scalar_one(),
            "active_elections": active_elections.scalar_one(),
            "active_events": active_events.scalar_one(),
            "total_votes_cast": total_votes.scalar_one(),
            "total_tickets_issued": total_tickets.scalar_one(),
        }
