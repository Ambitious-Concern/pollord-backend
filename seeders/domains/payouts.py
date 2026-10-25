"""Payout request history — deliberately curated per-source scenarios
(not random cyclic amounts) so the demo data always includes: one source
fully paid out, two sources blocked by a request pending review, and two
genuinely "ready to request" sources with real remaining balance and no
blocker. Random amounts were tried first and keep producing already-
requested totals that exceed real revenue (an artifact of picking a random
GH₵50-5000 figure with no regard for what a source actually earned) —
every source ended up either fully consumed or pending-blocked, leaving
nothing available to demo the Request Payout flow itself."""
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.plans import TICKET_COMMISSION_RATE_BPS, commission_rate_bps
from app.models.election import Election
from app.models.event import Event
from app.models.payout_request import PayoutRequest
from app.models.ticket import TicketPurchase
from app.models.transaction import Transaction
from seeders import config
from seeders.config import rng

_MOBILE_NETWORKS = ["MTN", "Vodafone", "AirtelTigo"]

# (status, fraction of available balance this entry requests, days ago) —
# rejected entries don't count against already_requested, matching
# PayoutRequestRepository.get_total_requested's real paid+pending-only sum.
_ELECTION_PLANS = [
    [("paid", Decimal("0.60"), 60), ("rejected", Decimal("0.15"), 40)],  # ready to request (40% left)
    [("paid", Decimal("1.00"), 45)],                                     # fully paid out
    [("rejected", Decimal("0.35"), 20)],                                 # ready to request (100% left)
]
_EVENT_PLANS = [
    [("pending", Decimal("0.50"), 5), ("paid", Decimal("0.10"), 70)],   # pending review
    [("paid", Decimal("0.70"), 55), ("pending", Decimal("0.20"), 3)],   # pending review
]


async def _election_available(db: AsyncSession, election: Election) -> Decimal:
    result = await db.execute(
        select(func.sum(Transaction.amount)).where(
            Transaction.election_id == election.election_id, Transaction.status == "success",
        )
    )
    gross = Decimal(result.scalar_one() or 0) / 100
    bps = commission_rate_bps(election.plan, election.commission_rate_override_bps)
    fee = (gross * bps / Decimal(10000)).quantize(Decimal("0.01"))
    return gross - fee


async def _event_available(db: AsyncSession, event: Event) -> Decimal:
    result = await db.execute(
        select(func.sum(TicketPurchase.total_amount)).where(TicketPurchase.event_id == event.event_id)
    )
    gross = Decimal(result.scalar_one() or 0)
    bps = commission_rate_bps(event.plan, event.commission_rate_override_bps, rate_table=TICKET_COMMISSION_RATE_BPS)
    fee = (gross * bps / Decimal(10000)).quantize(Decimal("0.01"))
    return gross - fee


def _build_request(kind: str, source, amount: Decimal, status: str, days_ago: int) -> PayoutRequest:
    now = datetime.now(timezone.utc)
    requested_at = now - timedelta(days=days_ago)
    reviewed_at = requested_at + timedelta(days=rng.randint(1, 3)) if status != "pending" else None
    return PayoutRequest(
        election_id=source.election_id if kind == "election" else None,
        event_id=source.event_id if kind == "event" else None,
        organizer_id=source.created_by,
        amount=amount.quantize(Decimal("0.01")),
        status=status,
        express=True,
        admin_notes=(
            "Paid via mobile money." if status == "paid"
            else "Destination details couldn't be verified." if status == "rejected"
            else None
        ),
        payout_method="mobile_money",
        recipient_name=f"{config.ORG_NAME} Account",
        destination_code=rng.choice(_MOBILE_NETWORKS),
        destination_number=f"024{rng.randint(1000000, 9999999)}",
        transfer_reference=f"seed_payout_{secrets.token_hex(6)}" if status == "paid" else None,
        transfer_status="success" if status == "paid" else None,
        requested_at=requested_at,
        reviewed_at=reviewed_at,
        reviewed_by=source.created_by if reviewed_at else None,
    )


async def seed_payout_history(
    db: AsyncSession, revenue_elections: list[Election], revenue_events: list[Event],
) -> None:
    existing_count = (await db.execute(select(func.count()).select_from(PayoutRequest))).scalar_one()
    if existing_count >= config.N_PAYOUT_HISTORY:
        return

    elections = sorted(revenue_elections, key=lambda e: e.title)
    events = sorted(revenue_events, key=lambda e: e.title)

    for election, plan in zip(elections, _ELECTION_PLANS):
        available = await _election_available(db, election)
        for status, fraction, days_ago in plan:
            db.add(_build_request("election", election, available * fraction, status, days_ago))

    for event, plan in zip(events, _EVENT_PLANS):
        available = await _event_available(db, event)
        for status, fraction, days_ago in plan:
            db.add(_build_request("event", event, available * fraction, status, days_ago))

    await db.flush()
