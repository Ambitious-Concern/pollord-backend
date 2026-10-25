"""Ticket sales confined to the events designated as payout-revenue sources
(see events.py) — a paid TicketTransaction (what Financial Stats reads) per
sale attempt, plus the matching TicketPurchase/Ticket fulfillment rows on
success (what Attendees/ticket-sales listings read). A shared
config.N_ATTENDEES_TOTAL budget is spread across just those events and
fulfilled by retrying until enough *successful* purchases land — the
failed/pending attempts along the way are exactly the transaction noise
Financial Stats needs to show a realistic success rate, not a separate
step."""
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import generate_secure_token
from app.models.event import Event
from app.models.ticket import Ticket, TicketPurchase
from app.models.ticket_transaction import TicketTransaction
from app.models.user import User
from seeders import config
from seeders.config import rng
from seeders.data.copy_bank import PLACES
from seeders.data.distribute import distribute

_CHANNELS = ["mobile_money", "mobile_money", "mobile_money", "card", "bank"]
_TXN_STATUS = ["success"] * 17 + ["pending"] * 2 + ["failed"] * 1
_CHECKIN_STATUS = (
    [("used", "scan")] * 10 + [("used", "manual_code")] * 2
    + [("used", "door-sale")] * 2 + [("valid", "registration")] * 1 + [("valid", None)] * 5
)


def _random_past_datetime(now: datetime, max_days: int) -> datetime:
    return now - timedelta(days=rng.randint(0, max_days), hours=rng.randint(0, 23), minutes=rng.randint(0, 59))


async def seed_ticket_sales(
    db: AsyncSession, events: list[Event], voters: list[User], revenue_event_ids: set[UUID],
) -> None:
    now = datetime.now(timezone.utc)
    candidates = [e for e in events if e.event_id in revenue_event_ids and e.ticket_types]

    pending_events = []
    for event in candidates:
        already_sold = (
            await db.execute(select(exists().where(TicketPurchase.event_id == event.event_id)))
        ).scalar()
        if not already_sold:
            pending_events.append(event)
    if not pending_events:
        return

    attendee_targets = distribute(config.N_ATTENDEES_TOTAL, len(pending_events), minimum=1)

    for event, target in zip(pending_events, attendee_targets):
        ticket_types = event.ticket_types
        remaining = target
        attempts = 0
        while remaining > 0 and attempts < target * 20:
            attempts += 1
            ticket_type = rng.choice(ticket_types)
            quantity = rng.randint(1, min(4, ticket_type.max_per_user, remaining))
            buyer = rng.choice(voters)
            purchased_at = _random_past_datetime(now, config.DATA_SPREAD_DAYS)
            # Every ticket type reaching this loop is on a designated
            # revenue event, and TICKET_TIERS never contains a free tier —
            # so amount is always > 0, no is_free branch needed.
            amount = ticket_type.price * quantity
            status = rng.choice(_TXN_STATUS)

            paystack_response = None
            if status != "pending":
                paystack_response = {
                    "channel": rng.choice(_CHANNELS),
                    "fees": int(round(amount * Decimal("0.0195") * 100)),
                }
            db.add(TicketTransaction(
                reference=f"seed_ticket_{generate_secure_token(12)}",
                user_id=buyer.user_id,
                event_id=event.event_id,
                items=[{"ticket_type_id": str(ticket_type.ticket_type_id), "quantity": quantity}],
                amount=amount,
                status=status,
                paystack_response=paystack_response,
                created_at=purchased_at,
            ))
            if status != "success":
                continue  # unfulfilled payment — no purchase/tickets issued, remaining unchanged

            purchase = TicketPurchase(
                user_id=buyer.user_id,
                event_id=event.event_id,
                total_amount=amount,
                payment_status="completed",
                payment_method="mobile_money",
                purchased_at=purchased_at,
            )
            db.add(purchase)
            await db.flush()

            for _i in range(quantity):
                ticket_code = generate_secure_token(16)
                ticket_status, check_in_channel = rng.choice(_CHECKIN_STATUS)
                used_at = purchased_at + timedelta(hours=rng.randint(1, 72)) if ticket_status == "used" else None
                db.add(Ticket(
                    ticket_code=ticket_code,
                    event_id=event.event_id,
                    ticket_type_id=ticket_type.ticket_type_id,
                    user_id=buyer.user_id,
                    purchase_id=purchase.purchase_id,
                    qr_code_data=json.dumps({
                        "ticket_code": ticket_code, "event_id": str(event.event_id),
                        "ticket_type": ticket_type.type_name,
                    }),
                    purchase_date=purchased_at,
                    ticket_status=ticket_status,
                    used_at=used_at,
                    check_in_channel=check_in_channel,
                    origin=rng.choice(PLACES) if rng.random() < 0.4 else None,
                ))

            remaining -= quantity

        await db.flush()
