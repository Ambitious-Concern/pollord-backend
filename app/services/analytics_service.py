from decimal import ROUND_HALF_UP, Decimal
from typing import Optional
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.election import Election, EligibleVoter
from app.models.event import Event, TicketType
from app.models.organization import Organization, OrganizationMember
from app.models.ticket import Ticket, TicketPurchase
from app.models.transaction import Transaction
from app.models.user import User
from app.models.vote import Vote
from app.repositories.election_repository import ElectionRepository
from app.repositories.event_repository import EventRepository
from app.repositories.ticket_repository import TicketPurchaseRepository, TicketRepository
from app.repositories.transaction_repository import TransactionRepository
from app.repositories.vote_repository import VoteRepository


def _to_cedis(amount: Decimal) -> Decimal:
    """Quantize a cedi amount to whole pesewas.

    ROUND_HALF_UP rather than Python's default banker's rounding, so a
    displayed total matches what an accountant would write down.
    """
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class AnalyticsService:
    """Read-only aggregate stats for elections, events, and the platform as
    a whole — used by the organizer dashboard and admin analytics views."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_election_stats(self, election_id: UUID) -> dict:
        """Turnout, revenue, and an hourly voting timeline for one election."""
        eligible_count = await self.session.execute(
            select(func.count()).select_from(EligibleVoter).where(
                EligibleVoter.election_id == election_id
            )
        )
        total_eligible = eligible_count.scalar_one()

        vote_count = await self.session.execute(
            select(func.count()).select_from(Vote).where(
                Vote.election_id == election_id
            )
        )
        total_votes = vote_count.scalar_one()

        turnout = (total_votes / total_eligible * 100) if total_eligible > 0 else 0

        timeline = await self.session.execute(
            select(
                func.date_trunc("hour", Vote.cast_at).label("hour"),
                func.count().label("count"),
            )
            .where(Vote.election_id == election_id)
            .group_by("hour")
            .order_by("hour")
        )

        # Revenue from paid votes — same "sales" concept as get_event_stats'
        # total_revenue, sourced from Transaction instead of TicketPurchase.
        transaction_repo = TransactionRepository(self.session)
        total_revenue = await transaction_repo.get_revenue_by_election(election_id)
        total_paid_votes = await transaction_repo.count_paid_votes_by_election(election_id)

        return {
            "election_id": str(election_id),
            "total_eligible_voters": total_eligible,
            "total_votes_cast": total_votes,
            "turnout_percentage": round(turnout, 2),
            "total_revenue": total_revenue,
            "total_paid_votes": total_paid_votes,
            "voting_timeline": [
                {"hour": str(row.hour), "count": row.count}
                for row in timeline.all()
            ],
        }

    async def get_event_stats(self, event_id: UUID) -> dict:
        """Ticket sales, attendance rate, and revenue for one event."""
        tickets_sold = await self.session.execute(
            select(func.count()).select_from(Ticket).where(
                Ticket.event_id == event_id,
                Ticket.ticket_status != "cancelled",
            )
        )
        total_sold = tickets_sold.scalar_one()

        tickets_used = await self.session.execute(
            select(func.count()).select_from(Ticket).where(
                Ticket.event_id == event_id,
                Ticket.ticket_status == "used",
            )
        )
        total_used = tickets_used.scalar_one()

        revenue = await self.session.execute(
            select(func.sum(TicketPurchase.total_amount)).where(
                TicketPurchase.event_id == event_id,
                TicketPurchase.payment_status == "completed",
            )
        )
        total_revenue = float(revenue.scalar_one() or 0)

        type_stats = await self.session.execute(
            select(
                TicketType.type_name,
                TicketType.quantity_sold,
                TicketType.quantity_available,
                TicketType.price,
            ).where(TicketType.event_id == event_id)
        )

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

    async def _revenue_by_organization(self) -> list[dict]:
        """Revenue per organisation, mirroring how the org drill-in attributes it.

        An organisation owns the elections and events created by any of its
        members (OrganizationMember.user_id), which is the same rule
        /admin/organizations/{id}/analytics uses — so the figures reconcile
        with the drill-in an admin has already seen.

        Two caveats inherited from that rule: a user who belongs to more than
        one organisation has their revenue counted under each, and activity by
        a creator who belongs to no organisation is in the platform total but
        under no organisation here. So these rows sum to the platform total
        only when neither case applies.
        """
        # Vote revenue reaches an org two ways, because a paid vote can belong
        # to an election or to an event's category voting. Sum both streams
        # into one figure per org.
        vote_by_org: dict[UUID, int] = {}

        election_votes = await self.session.execute(
            select(OrganizationMember.org_id, func.sum(Transaction.amount))
            .join(Election, Election.created_by == OrganizationMember.user_id)
            .join(Transaction, Transaction.election_id == Election.election_id)
            .where(Transaction.status == "success")
            .group_by(OrganizationMember.org_id)
        )
        event_votes = await self.session.execute(
            select(OrganizationMember.org_id, func.sum(Transaction.amount))
            .join(Event, Event.created_by == OrganizationMember.user_id)
            .join(Transaction, Transaction.event_id == Event.event_id)
            .where(Transaction.status == "success")
            .group_by(OrganizationMember.org_id)
        )
        for rows in (election_votes, event_votes):
            for org_id, amount in rows.all():
                vote_by_org[org_id] = vote_by_org.get(org_id, 0) + int(amount or 0)

        ticket_rows = await self.session.execute(
            select(
                OrganizationMember.org_id,
                func.sum(TicketPurchase.total_amount),
            )
            .join(Event, Event.created_by == OrganizationMember.user_id)
            .join(TicketPurchase, TicketPurchase.event_id == Event.event_id)
            .where(TicketPurchase.payment_status == "completed")
            .group_by(OrganizationMember.org_id)
        )
        ticket_by_org = {
            org_id: Decimal(str(amount or 0)) for org_id, amount in ticket_rows.all()
        }

        # Every organisation appears, including those earning nothing yet —
        # a zero row is information, not noise.
        orgs = await self.session.execute(
            select(Organization.org_id, Organization.name)
        )

        breakdown = []
        for org_id, name in orgs.all():
            votes = _to_cedis(Decimal(vote_by_org.get(org_id, 0)) / 100)
            tickets = _to_cedis(ticket_by_org.get(org_id, Decimal(0)))
            breakdown.append({
                "org_id": org_id,
                "name": name,
                "vote_revenue_ghs": float(votes),
                "ticket_revenue_ghs": float(tickets),
                "total_revenue_ghs": float(votes + tickets),
            })

        # Highest earner first; name breaks ties so the order is stable.
        breakdown.sort(key=lambda o: (-o["total_revenue_ghs"], o["name"]))
        return breakdown

    async def get_system_stats(self) -> dict:
        """Platform-wide totals for the admin console dashboard.

        Revenue is stored in two different units: votes are paid in pesewas
        (Transaction.amount, an int) while tickets are priced in cedis
        (TicketPurchase.total_amount, Numeric(10, 2)). Both are reported in
        cedis so the caller never has to know which stream a figure came
        from, and never has to add two different currencies itself.
        """
        total_users = await self.session.execute(
            select(func.count()).select_from(User)
        )
        total_elections = await self.session.execute(
            select(func.count()).select_from(Election)
        )
        active_elections = await self.session.execute(
            select(func.count()).select_from(Election).where(
                Election.status == "active"
            )
        )
        total_events = await self.session.execute(
            select(func.count()).select_from(Event)
        )
        active_events = await self.session.execute(
            select(func.count()).select_from(Event).where(
                Event.status == "published"
            )
        )
        # Votes are weighted (one paid row can carry several votes), so sum
        # the count column rather than counting rows.
        total_votes = await self.session.execute(select(func.sum(Vote.count)))
        tickets_sold = await self.session.execute(
            select(func.count()).select_from(Ticket).where(
                Ticket.ticket_status != "cancelled"
            )
        )
        election_revenue = await self.session.execute(
            select(func.sum(Transaction.amount)).where(
                Transaction.status == "success"
            )
        )
        event_revenue = await self.session.execute(
            select(func.sum(TicketPurchase.total_amount)).where(
                TicketPurchase.payment_status == "completed"
            )
        )

        # Both streams are reported in cedis. Vote payments are stored in
        # pesewas so they divide down; ticket sales are already in cedis.
        # The arithmetic stays in Decimal and only rounds once, at the end,
        # so a half-pesewa can't drift into the total.
        election_revenue_ghs = _to_cedis(
            Decimal(int(election_revenue.scalar_one() or 0)) / 100
        )
        event_revenue_ghs = _to_cedis(
            Decimal(str(event_revenue.scalar_one() or 0))
        )

        return {
            "total_users": total_users.scalar_one(),
            "total_elections": total_elections.scalar_one(),
            "active_elections": active_elections.scalar_one(),
            "total_events": total_events.scalar_one(),
            "active_events": active_events.scalar_one(),
            "total_votes_cast": int(total_votes.scalar_one() or 0),
            "total_tickets_sold": tickets_sold.scalar_one(),
            "total_election_revenue_ghs": float(election_revenue_ghs),
            "total_event_revenue_ghs": float(event_revenue_ghs),
            "total_revenue_ghs": float(election_revenue_ghs + event_revenue_ghs),
            "organizations": await self._revenue_by_organization(),
        }
