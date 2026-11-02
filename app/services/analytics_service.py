from decimal import ROUND_HALF_UP, Decimal
from typing import Optional
from uuid import UUID

from sqlalchemy import func, select, text
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

    async def get_listing_activity_timeline(
        self, listing_type: str, listing_id: UUID, granularity: str
    ) -> dict:
        """Zero-filled rolling timeline ending now: last 24 hourly buckets or
        last 14 daily buckets. Votes for an election, tickets sold for an
        event — the two "how much traffic is this listing getting" signals
        the dashboard's trend card needs. generate_series supplies every
        bucket up front so a quiet hour/day shows as zero instead of just
        not appearing in the result.
        """
        is_hourly = granularity == "hour"
        unit = "hour" if is_hourly else "day"
        bucket_count = 24 if is_hourly else 14

        if listing_type == "election":
            source_sql = """
                SELECT date_trunc(:unit, cast_at) AS bucket, COUNT(*) AS count
                FROM votes
                WHERE election_id = :listing_id
                GROUP BY 1
            """
        else:
            source_sql = """
                SELECT date_trunc(:unit, purchase_date) AS bucket, COUNT(*) AS count
                FROM tickets
                WHERE event_id = :listing_id AND ticket_status != 'cancelled'
                GROUP BY 1
            """

        sql = text(f"""
            SELECT bucket, COALESCE(activity.count, 0) AS count
            FROM generate_series(
                date_trunc(:unit, now()) - ((:bucket_count - 1) * interval '1 {unit}'),
                date_trunc(:unit, now()),
                interval '1 {unit}'
            ) AS bucket
            LEFT JOIN ({source_sql}) activity USING (bucket)
            ORDER BY bucket
        """)

        result = await self.session.execute(
            sql, {"unit": unit, "bucket_count": bucket_count, "listing_id": listing_id}
        )
        return {
            "granularity": granularity,
            "points": [
                {"bucket": row.bucket.isoformat(), "count": row.count}
                for row in result.all()
            ],
        }

    async def get_platform_timeline(
        self,
        days: int = 30,
        year: int | None = None,
        content_id: str | None = None,
        content_type: str | None = None,
    ) -> dict:
        """Zero-filled daily activity for the admin dashboard chart.

        When *year* is provided the series runs Jan 1 – Dec 31 of that year
        (capped at today for the current year). Otherwise the last *days* days
        are returned as a rolling window ending today.

        When *content_id* + *content_type* ('election'|'event') are given the
        four sub-queries are scoped to that single election or event.
        """
        bind: dict = {}

        if content_id and content_type == "election":
            bind["content_id"] = content_id
            _joins = """
                LEFT JOIN (
                    SELECT date_trunc('day', cast_at)::date AS d, SUM(count) AS votes
                    FROM votes
                    WHERE election_id = :content_id::uuid
                    GROUP BY 1
                ) v ON v.d = bucket
                LEFT JOIN (SELECT NULL::date AS d, 0 AS tickets WHERE false) t ON t.d = bucket
                LEFT JOIN (
                    SELECT date_trunc('day', created_at)::date AS d,
                           SUM(amount) / 100.0 AS vote_rev
                    FROM transactions
                    WHERE status = 'success' AND election_id = :content_id::uuid
                    GROUP BY 1
                ) vr ON vr.d = bucket
                LEFT JOIN (SELECT NULL::date AS d, 0.0 AS ticket_rev WHERE false) tr ON tr.d = bucket
                ORDER BY bucket
            """
        elif content_id and content_type == "event":
            bind["content_id"] = content_id
            _joins = """
                LEFT JOIN (
                    SELECT date_trunc('day', cast_at)::date AS d, SUM(count) AS votes
                    FROM votes
                    WHERE event_id = :content_id::uuid
                    GROUP BY 1
                ) v ON v.d = bucket
                LEFT JOIN (
                    SELECT date_trunc('day', purchase_date)::date AS d, COUNT(*) AS tickets
                    FROM tickets
                    WHERE ticket_status != 'cancelled' AND event_id = :content_id::uuid
                    GROUP BY 1
                ) t ON t.d = bucket
                LEFT JOIN (
                    SELECT date_trunc('day', created_at)::date AS d,
                           SUM(amount) / 100.0 AS vote_rev
                    FROM transactions
                    WHERE status = 'success' AND event_id = :content_id::uuid
                    GROUP BY 1
                ) vr ON vr.d = bucket
                LEFT JOIN (
                    SELECT date_trunc('day', purchased_at)::date AS d,
                           SUM(total_amount) AS ticket_rev
                    FROM ticket_purchases
                    WHERE payment_status = 'completed' AND event_id = :content_id::uuid
                    GROUP BY 1
                ) tr ON tr.d = bucket
                ORDER BY bucket
            """
        else:
            _joins = """
                LEFT JOIN (
                    SELECT date_trunc('day', cast_at)::date AS d, SUM(count) AS votes
                    FROM votes
                    GROUP BY 1
                ) v ON v.d = bucket
                LEFT JOIN (
                    SELECT date_trunc('day', purchase_date)::date AS d, COUNT(*) AS tickets
                    FROM tickets
                    WHERE ticket_status != 'cancelled'
                    GROUP BY 1
                ) t ON t.d = bucket
                LEFT JOIN (
                    SELECT date_trunc('day', created_at)::date AS d,
                           SUM(amount) / 100.0 AS vote_rev
                    FROM transactions
                    WHERE status = 'success'
                    GROUP BY 1
                ) vr ON vr.d = bucket
                LEFT JOIN (
                    SELECT date_trunc('day', purchased_at)::date AS d,
                           SUM(total_amount) AS ticket_rev
                    FROM ticket_purchases
                    WHERE payment_status = 'completed'
                    GROUP BY 1
                ) tr ON tr.d = bucket
                ORDER BY bucket
            """

        if year is not None:
            from datetime import date as _date
            today = _date.today()
            start = _date(year, 1, 1).isoformat()
            end = min(_date(year, 12, 31), today).isoformat()
            sql = text(f"""
                SELECT
                    bucket::date AS date,
                    COALESCE(v.votes, 0)       AS votes,
                    COALESCE(t.tickets, 0)     AS tickets,
                    COALESCE(vr.vote_rev, 0) + COALESCE(tr.ticket_rev, 0) AS revenue_ghs
                FROM generate_series(
                    '{start}'::date,
                    '{end}'::date,
                    INTERVAL '1 day'
                ) AS bucket
                {_joins}
            """)
            result = await self.session.execute(sql, bind)
        else:
            bind["days"] = days
            sql = text(f"""
                SELECT
                    bucket::date AS date,
                    COALESCE(v.votes, 0)       AS votes,
                    COALESCE(t.tickets, 0)     AS tickets,
                    COALESCE(vr.vote_rev, 0) + COALESCE(tr.ticket_rev, 0) AS revenue_ghs
                FROM generate_series(
                    (NOW() - ((:days - 1) * INTERVAL '1 day'))::date,
                    NOW()::date,
                    INTERVAL '1 day'
                ) AS bucket
                {_joins}
            """)
            result = await self.session.execute(sql, bind)
        return {
            "days": days,
            "points": [
                {
                    "date": str(row.date),
                    "votes": int(row.votes),
                    "tickets": int(row.tickets),
                    "revenue_ghs": float(row.revenue_ghs),
                }
                for row in result.all()
            ],
        }

    async def get_content_list(self, search: str = "") -> list[dict]:
        """Lightweight election+event list for the admin chart's content picker."""
        like = f"%{search}%"
        sql = text("""
            SELECT id, title, 'election' AS type, status, org_name
            FROM (
                SELECT e.election_id::text AS id, e.title,
                       e.status, o.name AS org_name
                FROM elections e
                JOIN organizations o ON o.org_id = e.org_id
            ) el
            WHERE (:search = '' OR LOWER(title) LIKE LOWER(:like) OR LOWER(org_name) LIKE LOWER(:like))
            UNION ALL
            SELECT id, title, 'event' AS type, status, org_name
            FROM (
                SELECT ev.event_id::text AS id, ev.title,
                       ev.status, o.name AS org_name
                FROM events ev
                JOIN organizations o ON o.org_id = ev.org_id
            ) evs
            WHERE (:search = '' OR LOWER(title) LIKE LOWER(:like) OR LOWER(org_name) LIKE LOWER(:like))
            ORDER BY title
            LIMIT 100
        """)
        result = await self.session.execute(sql, {"search": search, "like": like})
        return [
            {"id": row.id, "title": row.title, "type": row.type, "status": row.status, "org_name": row.org_name}
            for row in result.all()
        ]

    async def get_top_organizations(self, sort_by: str = "votes", limit: int = 10) -> list[dict]:
        """Top organisations ranked by votes, tickets, or revenue.

        Uses org_id from org-owned elections/events (same attribution rule as
        the org drill-in) so figures reconcile with the detail page.
        """
        col_map = {"votes": "votes", "tickets": "tickets", "revenue": "revenue_ghs"}
        col = col_map.get(sort_by, "votes")
        sql = text(f"""
            SELECT
                o.org_id::text  AS org_id,
                o.name          AS org_name,
                o.is_verified   AS verified,
                COALESCE(v.votes, 0)           AS votes,
                COALESCE(t.tickets, 0)         AS tickets,
                COALESCE(r.revenue_ghs, 0)     AS revenue_ghs
            FROM organizations o
            LEFT JOIN (
                SELECT e.org_id, SUM(vo.count) AS votes
                FROM votes vo
                JOIN elections e ON e.election_id = vo.election_id
                GROUP BY e.org_id
            ) v ON v.org_id = o.org_id
            LEFT JOIN (
                SELECT ev.org_id, COUNT(*) AS tickets
                FROM tickets tk
                JOIN events ev ON ev.event_id = tk.event_id
                WHERE tk.ticket_status != 'cancelled'
                GROUP BY ev.org_id
            ) t ON t.org_id = o.org_id
            LEFT JOIN (
                SELECT COALESCE(e.org_id, ev.org_id) AS org_id,
                       SUM(tx.amount) / 100.0 AS revenue_ghs
                FROM transactions tx
                LEFT JOIN elections e  ON e.election_id  = tx.election_id
                LEFT JOIN events    ev ON ev.event_id    = tx.event_id
                WHERE tx.status = 'success'
                  AND COALESCE(e.org_id, ev.org_id) IS NOT NULL
                GROUP BY 1
            ) r ON r.org_id = o.org_id
            WHERE (v.votes > 0 OR t.tickets > 0 OR r.revenue_ghs > 0)
            ORDER BY {col} DESC
            LIMIT :limit
        """)
        result = await self.session.execute(sql, {"limit": limit})
        return [
            {
                "org_id": row.org_id,
                "org_name": row.org_name,
                "verified": row.verified,
                "votes": int(row.votes),
                "tickets": int(row.tickets),
                "revenue_ghs": float(row.revenue_ghs),
            }
            for row in result.all()
        ]

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
