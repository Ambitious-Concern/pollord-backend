from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Dict, List, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.financial_breakdown import (
    extract_channel,
    processing_fee_ghs,
    ticket_platform_fee_ghs,
    vote_platform_fee_ghs,
)
from app.models.election import Category, Election
from app.models.event import Event, TicketType
from app.models.organization import Organization
from app.models.transaction import Transaction
from app.models.ticket_transaction import TicketTransaction
from app.repositories.organization_repository import OrganizationRepository
from app.schemas.financial_stats import (
    DateRange,
    FinancialBreakdownItem,
    FinancialChannelBreakdown,
    FinancialSourceBreakdown,
    FinancialStatsResponse,
    FinancialSummary,
    FinancialTimeBucket,
    FinancialTransactionResponse,
)

_STATUS_MAP = {"success": "success", "pending": "pending", "failed": "failed", "abandoned": "failed", "needs_refund": "failed"}


def _q(amount: Decimal) -> Decimal:
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@dataclass
class _Row:
    id: str
    type: str
    source_id: UUID
    source_name: str
    source_kind: str
    detail: str
    amount_ghs: Decimal
    platform_fee_ghs: Decimal
    processing_fee_ghs: Decimal
    channel: str
    status: str
    reference: str
    occurred_at: datetime
    category_id: Optional[UUID] = None
    category_name: Optional[str] = None

    @property
    def net_ghs(self) -> Decimal:
        return self.amount_ghs - self.platform_fee_ghs - self.processing_fee_ghs


def _range_days(range_id: DateRange) -> Optional[int]:
    if range_id == "this-month":
        return date.today().day
    if range_id == "last-30":
        return 30
    if range_id == "last-90":
        return 90
    return None


class FinancialStatsService:
    """Org-scoped financial reporting: combines paid-vote (Transaction) and
    paid-ticket (TicketTransaction) payment records — the actual payment
    gateway records, not the post-fulfillment Election/TicketPurchase rows —
    since only these carry the Paystack response Financial Stats reports on
    (channel, real processing fee)."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_stats(
        self, user_id: UUID, *, range_id: DateRange, source_id: Optional[UUID] = None
    ) -> FinancialStatsResponse:
        teammate_ids = await OrganizationRepository(Organization, self.session).get_teammate_ids(user_id)
        rows = await self._fetch_rows(teammate_ids)
        scoped_kind: Optional[str] = None
        if source_id is not None:
            rows = [r for r in rows if r.source_id == source_id]
            if rows:
                scoped_kind = rows[0].source_kind

        days = _range_days(range_id)
        now = datetime.now(timezone.utc)
        window_start = now - timedelta(days=days) if days else None
        current_rows = self._in_window(rows, start=window_start, end=now)
        previous_rows = (
            self._in_window(rows, start=now - timedelta(days=days * 2), end=now - timedelta(days=days))
            if days
            else []
        )

        scoped_breakdown: List[FinancialBreakdownItem] = []
        scoped_breakdown_kind: Optional[str] = None
        if scoped_kind == "election":
            scoped_breakdown = self._category_breakdown(current_rows)
            scoped_breakdown_kind = "categories"
        elif scoped_kind == "event":
            scoped_breakdown = await self._ticket_type_breakdown(source_id, window_start, now)
            scoped_breakdown_kind = "ticket_types"

        return FinancialStatsResponse(
            range=range_id,
            summary=self._summarize(current_rows),
            previous_summary=self._summarize(previous_rows),
            by_channel=self._by_channel(current_rows),
            top_sources=self._top_sources(current_rows),
            time_series=self._time_series(current_rows, range_id),
            transactions=[self._to_response(r) for r in sorted(current_rows, key=lambda r: r.occurred_at, reverse=True)],
            scoped_breakdown=scoped_breakdown,
            scoped_breakdown_kind=scoped_breakdown_kind,
        )

    @staticmethod
    def _in_window(rows: List[_Row], *, start: Optional[datetime], end: datetime) -> List[_Row]:
        return [r for r in rows if (start is None or r.occurred_at >= start) and r.occurred_at <= end]

    @staticmethod
    def _summarize(rows: List[_Row]) -> FinancialSummary:
        successful = [r for r in rows if r.status == "success"]
        return FinancialSummary(
            gross_ghs=_q(sum((r.amount_ghs for r in successful), Decimal("0"))),
            platform_fee_ghs=_q(sum((r.platform_fee_ghs for r in successful), Decimal("0"))),
            processing_fee_ghs=_q(sum((r.processing_fee_ghs for r in successful), Decimal("0"))),
            net_ghs=_q(sum((r.net_ghs for r in successful), Decimal("0"))),
            count=len(successful),
        )

    @staticmethod
    def _by_channel(rows: List[_Row]) -> List[FinancialChannelBreakdown]:
        totals: Dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        for r in rows:
            if r.status == "success":
                totals[r.channel] += r.amount_ghs
        return [FinancialChannelBreakdown(channel=c, amount_ghs=_q(a)) for c, a in totals.items()]

    @staticmethod
    def _top_sources(rows: List[_Row], limit: int = 5) -> List[FinancialSourceBreakdown]:
        totals: Dict[UUID, dict] = {}
        for r in rows:
            if r.status != "success":
                continue
            entry = totals.setdefault(r.source_id, {"name": r.source_name, "gross": Decimal("0"), "net": Decimal("0")})
            entry["gross"] += r.amount_ghs
            entry["net"] += r.net_ghs
        ranked = sorted(totals.items(), key=lambda kv: kv[1]["gross"], reverse=True)[:limit]
        return [
            FinancialSourceBreakdown(source_id=sid, source_name=v["name"], gross_ghs=_q(v["gross"]), net_ghs=_q(v["net"]))
            for sid, v in ranked
        ]

    @staticmethod
    def _category_breakdown(rows: List[_Row], limit: int = 5) -> List[FinancialBreakdownItem]:
        totals: Dict[UUID, dict] = {}
        for r in rows:
            if r.status != "success" or r.type != "vote" or r.category_id is None:
                continue
            entry = totals.setdefault(r.category_id, {"name": r.category_name or "", "amount": Decimal("0")})
            entry["amount"] += r.amount_ghs
        ranked = sorted(totals.items(), key=lambda kv: kv[1]["amount"], reverse=True)[:limit]
        return [FinancialBreakdownItem(key=str(cid), label=v["name"], value=_q(v["amount"])) for cid, v in ranked]

    async def _ticket_type_breakdown(
        self, event_id: UUID, start: Optional[datetime], end: datetime, limit: int = 5
    ) -> List[FinancialBreakdownItem]:
        query = select(TicketTransaction).where(
            TicketTransaction.event_id == event_id,
            TicketTransaction.status == "success",
            TicketTransaction.created_at <= end,
        )
        if start is not None:
            query = query.where(TicketTransaction.created_at >= start)
        result = await self.session.execute(query)

        totals: Dict[UUID, int] = defaultdict(int)
        for txn in result.scalars().all():
            for item in txn.items:
                totals[UUID(item["ticket_type_id"])] += item["quantity"]
        if not totals:
            return []

        names = await self._ticket_type_names(set(totals.keys()))
        ranked = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[:limit]
        return [FinancialBreakdownItem(key=str(tid), label=names.get(tid, ""), value=Decimal(qty)) for tid, qty in ranked]

    @staticmethod
    def _time_series(rows: List[_Row], range_id: DateRange) -> List[FinancialTimeBucket]:
        granularity = "day" if range_id in ("this-month", "last-30") else "week" if range_id == "last-90" else "month"
        now = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

        # "%-d" (no leading zero) is a glibc/macOS strftime extension that
        # raises on Windows — build the label without relying on it.
        def _label(d: datetime) -> str:
            return f"{d.strftime('%b')} {d.day}"

        if granularity == "day":
            count = date.today().day if range_id == "this-month" else 30
            starts = [now - timedelta(days=count - 1 - i) for i in range(count)]
            labels = [_label(d) for d in starts]
            bucket_of = lambda occurred: (occurred - starts[0]).days
        elif granularity == "week":
            count = 13
            starts = [now - timedelta(days=(count - 1 - i) * 7) for i in range(count)]
            labels = [_label(d) for d in starts]
            bucket_of = lambda occurred: (occurred - starts[0]).days // 7
        else:
            count = 6
            month_starts = [
                (now.year, now.month - (count - 1 - i)) for i in range(count)
            ]
            month_starts = [(y + (m - 1) // 12, (m - 1) % 12 + 1) for y, m in month_starts]
            labels = [date(y, m, 1).strftime("%b") for y, m in month_starts]
            index_of_month = {(y, m): i for i, (y, m) in enumerate(month_starts)}
            bucket_of = lambda occurred: index_of_month.get((occurred.year, occurred.month), -1)

        votes = [Decimal("0")] * count
        tickets = [Decimal("0")] * count
        for r in rows:
            if r.status != "success":
                continue
            idx = bucket_of(r.occurred_at)
            if idx is None or idx < 0 or idx >= count:
                continue
            if r.type == "vote":
                votes[idx] += r.amount_ghs
            else:
                tickets[idx] += r.amount_ghs

        return [
            FinancialTimeBucket(label=labels[i], votes_ghs=_q(votes[i]), tickets_ghs=_q(tickets[i]))
            for i in range(count)
        ]

    @staticmethod
    def _to_response(r: _Row) -> FinancialTransactionResponse:
        return FinancialTransactionResponse(
            id=r.id,
            type=r.type,
            source_id=r.source_id,
            source_name=r.source_name,
            source_kind=r.source_kind,
            detail=r.detail,
            amount_ghs=r.amount_ghs,
            platform_fee_ghs=r.platform_fee_ghs,
            processing_fee_ghs=r.processing_fee_ghs,
            net_ghs=r.net_ghs,
            channel=r.channel,
            status=r.status,
            reference=r.reference,
            occurred_at=r.occurred_at,
        )

    async def _fetch_rows(self, teammate_ids: List[UUID]) -> List[_Row]:
        rows: List[_Row] = []
        rows += await self._fetch_vote_rows_via(Election, "election_id", teammate_ids)
        rows += await self._fetch_vote_rows_via(Event, "event_id", teammate_ids)
        rows += await self._fetch_ticket_rows(teammate_ids)
        return rows

    async def _fetch_vote_rows_via(self, ParentModel, fk_name: str, teammate_ids: List[UUID]) -> List[_Row]:
        fk_col = getattr(Transaction, fk_name)
        parent_pk = ParentModel.election_id if ParentModel is Election else ParentModel.event_id
        result = await self.session.execute(
            select(Transaction, ParentModel.title, ParentModel.plan, ParentModel.commission_rate_override_bps)
            .join(ParentModel, fk_col == parent_pk)
            .where(ParentModel.created_by.in_(teammate_ids))
        )
        txn_rows = result.all()

        category_ids = {txn.category_id for txn, *_ in txn_rows}
        category_names = await self._category_names(category_ids)

        out: List[_Row] = []
        for txn, title, plan, override_bps in txn_rows:
            amount_ghs = Decimal(txn.amount) / 100
            platform_fee = vote_platform_fee_ghs(amount_ghs, plan, override_bps, txn.fee_payer)
            processing_fee = processing_fee_ghs(txn.paystack_response, amount_ghs)
            vote_count = txn.vote_count if txn.vote_count is not None else 1
            category_name = category_names.get(txn.category_id, "")
            out.append(
                _Row(
                    id=f"vote-{txn.transaction_id}",
                    type="vote",
                    source_id=getattr(txn, fk_name),
                    source_name=title,
                    source_kind="election" if ParentModel is Election else "event",
                    detail=f"{vote_count} vote{'s' if vote_count != 1 else ''} · {category_name}".strip(" ·"),
                    amount_ghs=amount_ghs,
                    platform_fee_ghs=platform_fee,
                    processing_fee_ghs=processing_fee,
                    channel=extract_channel(txn.paystack_response),
                    status=_STATUS_MAP.get(txn.status, "failed"),
                    reference=txn.reference,
                    occurred_at=txn.created_at,
                    category_id=txn.category_id,
                    category_name=category_name,
                )
            )
        return out

    async def _fetch_ticket_rows(self, teammate_ids: List[UUID]) -> List[_Row]:
        result = await self.session.execute(
            select(TicketTransaction, Event.title, Event.plan, Event.commission_rate_override_bps)
            .join(Event, TicketTransaction.event_id == Event.event_id)
            .where(Event.created_by.in_(teammate_ids))
        )
        txn_rows = result.all()

        ticket_type_ids = {UUID(i["ticket_type_id"]) for txn, *_ in txn_rows for i in txn.items}
        type_names = await self._ticket_type_names(ticket_type_ids)

        out: List[_Row] = []
        for txn, title, plan, override_bps in txn_rows:
            ticket_count = sum(i["quantity"] for i in txn.items)
            names = ", ".join(dict.fromkeys(type_names.get(UUID(i["ticket_type_id"]), "") for i in txn.items))
            platform_fee = ticket_platform_fee_ghs(txn.amount, plan, override_bps, txn.fee_payer)
            processing_fee = processing_fee_ghs(txn.paystack_response, txn.amount)
            out.append(
                _Row(
                    id=f"ticket-{txn.transaction_id}",
                    type="ticket",
                    source_id=txn.event_id,
                    source_name=title,
                    source_kind="event",
                    detail=f"{ticket_count} × {names}".strip(" ×"),
                    amount_ghs=txn.amount,
                    platform_fee_ghs=platform_fee,
                    processing_fee_ghs=processing_fee,
                    channel=extract_channel(txn.paystack_response),
                    status=_STATUS_MAP.get(txn.status, "failed"),
                    reference=txn.reference,
                    occurred_at=txn.created_at,
                )
            )
        return out

    async def _category_names(self, category_ids: set) -> Dict[UUID, str]:
        if not category_ids:
            return {}
        result = await self.session.execute(
            select(Category.category_id, Category.name).where(Category.category_id.in_(category_ids))
        )
        return dict(result.all())

    async def _ticket_type_names(self, ticket_type_ids: set) -> Dict[UUID, str]:
        if not ticket_type_ids:
            return {}
        result = await self.session.execute(
            select(TicketType.ticket_type_id, TicketType.type_name).where(
                TicketType.ticket_type_id.in_(ticket_type_ids)
            )
        )
        return dict(result.all())
