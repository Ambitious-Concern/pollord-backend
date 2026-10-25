from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Dict, List, Optional
from uuid import UUID

from sqlalchemy import Integer, and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.election import Category
from app.models.event import TicketType
from app.models.ticket_transaction import TicketTransaction
from app.models.transaction import Transaction

_CENTS = Decimal("100")


class EventSummaryStats:
    __slots__ = (
        "tickets_sold", "capacity", "lowest_ticket_price", "has_voting",
        "revenue_ghs", "revenue_growth_pct",
    )

    def __init__(self) -> None:
        self.tickets_sold = 0
        self.capacity = 0
        self.lowest_ticket_price: Optional[Decimal] = None
        self.has_voting = False
        self.revenue_ghs = "0.00"
        self.revenue_growth_pct = 0.0


class EventSummaryService:
    """Bulk stats for the organizer dashboard's events grid — mirrors
    ElectionSummaryService. Revenue combines two payment-record tables with
    different units: Transaction.amount (hybrid-event voting) is pesewas,
    TicketTransaction.amount (ticket sales) is already GHS decimal — see
    each model's own docstring."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_summaries(self, event_ids: List[UUID]) -> Dict[UUID, EventSummaryStats]:
        stats: Dict[UUID, EventSummaryStats] = {eid: EventSummaryStats() for eid in event_ids}
        if not event_ids:
            return stats

        ticket_type_rows = (
            await self.db.execute(
                select(
                    TicketType.event_id,
                    func.sum(TicketType.quantity_sold),
                    func.sum(TicketType.quantity_available + TicketType.quantity_sold),
                    func.min(TicketType.price),
                )
                .where(TicketType.event_id.in_(event_ids))
                .group_by(TicketType.event_id)
            )
        ).all()
        for event_id, sold, capacity, lowest_price in ticket_type_rows:
            s = stats[event_id]
            s.tickets_sold = sold or 0
            s.capacity = capacity or 0
            s.lowest_ticket_price = lowest_price

        category_rows = (
            await self.db.execute(
                select(Category.event_id, func.count(Category.category_id))
                .where(Category.event_id.in_(event_ids))
                .group_by(Category.event_id)
            )
        ).all()
        for event_id, count in category_rows:
            stats[event_id].has_voting = count > 0

        now = datetime.now(timezone.utc)
        week_ago = now - timedelta(days=7)
        two_weeks_ago = now - timedelta(days=14)

        ticket_revenue_rows = (
            await self.db.execute(
                select(
                    TicketTransaction.event_id,
                    func.sum(TicketTransaction.amount),
                    func.sum(
                        case((TicketTransaction.created_at >= week_ago, TicketTransaction.amount), else_=0)
                    ),
                    func.sum(
                        case(
                            (
                                and_(
                                    TicketTransaction.created_at >= two_weeks_ago,
                                    TicketTransaction.created_at < week_ago,
                                ),
                                TicketTransaction.amount,
                            ),
                            else_=0,
                        )
                    ),
                )
                .where(TicketTransaction.event_id.in_(event_ids), TicketTransaction.status == "success")
                .group_by(TicketTransaction.event_id)
            )
        ).all()
        totals: Dict[UUID, Decimal] = {}
        recents: Dict[UUID, Decimal] = {}
        priors: Dict[UUID, Decimal] = {}
        for event_id, total, recent, prior in ticket_revenue_rows:
            totals[event_id] = Decimal(total or 0)
            recents[event_id] = Decimal(recent or 0)
            priors[event_id] = Decimal(prior or 0)

        # Hybrid events also collect paid votes — Transaction.amount is
        # pesewas, so it's divided by 100 before joining the GHS totals above.
        vote_revenue_rows = (
            await self.db.execute(
                select(
                    Transaction.event_id,
                    func.sum(Transaction.amount),
                    func.sum(
                        case((Transaction.created_at >= week_ago, Transaction.amount), else_=0)
                    ).cast(Integer),
                    func.sum(
                        case(
                            (
                                and_(
                                    Transaction.created_at >= two_weeks_ago,
                                    Transaction.created_at < week_ago,
                                ),
                                Transaction.amount,
                            ),
                            else_=0,
                        )
                    ).cast(Integer),
                )
                .where(Transaction.event_id.in_(event_ids), Transaction.status == "success")
                .group_by(Transaction.event_id)
            )
        ).all()
        for event_id, total_pesewas, recent_pesewas, prior_pesewas in vote_revenue_rows:
            totals[event_id] = totals.get(event_id, Decimal(0)) + Decimal(total_pesewas or 0) / _CENTS
            recents[event_id] = recents.get(event_id, Decimal(0)) + Decimal(recent_pesewas or 0) / _CENTS
            priors[event_id] = priors.get(event_id, Decimal(0)) + Decimal(prior_pesewas or 0) / _CENTS

        for event_id, total in totals.items():
            s = stats[event_id]
            s.revenue_ghs = str(total.quantize(Decimal("0.01")))
            recent = recents.get(event_id, Decimal(0))
            prior = priors.get(event_id, Decimal(0))
            if prior > 0:
                s.revenue_growth_pct = round(float((recent - prior) / prior * 100), 1)
            elif recent > 0:
                s.revenue_growth_pct = 100.0
            else:
                s.revenue_growth_pct = 0.0

        return stats
