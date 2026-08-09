"""
Integration tests for ticket payment (Paystack) integration.

Covers:
  1. initiate-payment: rejects free-total requests, creates pending
     TicketTransaction, returns Paystack init data (Paystack mocked)
  2. verify-and-purchase: happy path fulfills the purchase, idempotency,
     failed/underpaid payment rejected, sold-out-during-payment marks
     needs_refund
  3. Existing POST /tickets/purchase rejects priced items
  4. Webhook charge.success/failed routes ticket_ references correctly
"""
from datetime import date, time
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.event import Event, TicketType


@pytest.fixture
async def paid_event_with_tickets(db_session: AsyncSession, admin_user):
    """Published event with one paid ticket type (small stock, for sold-out testing)."""
    event = Event(
        title="Paid Ticket Event",
        description="An event with a paid ticket type",
        event_date=date(2026, 9, 1),
        event_time=time(19, 0),
        location="Test Arena",
        category="Concert",
        capacity=50,
        status="published",
        created_by=admin_user["user"].user_id,
    )
    db_session.add(event)
    await db_session.flush()

    vip = TicketType(
        event_id=event.event_id,
        type_name="VIP",
        price=100.00,
        quantity_available=2,
        max_per_user=5,
    )
    db_session.add(vip)
    await db_session.flush()

    return {"event": event, "vip": vip}


FAKE_ACCESS_CODE = "acc_test_ticket123"
FAKE_REFERENCE = "ticket_testref123"

PAYSTACK_INIT_PATH = "app.api.v1.endpoints.tickets.PaystackService.initialize_transaction"
PAYSTACK_VERIFY_PATH = "app.api.v1.endpoints.tickets.PaystackService.verify_transaction"


def _init_mock(amount: int) -> AsyncMock:
    return AsyncMock(return_value={
        "access_code": FAKE_ACCESS_CODE,
        "reference": FAKE_REFERENCE,
        "authorization_url": "https://checkout.paystack.com/test",
    })


@pytest.mark.asyncio
class TestInitiateTicketPayment:
    async def test_initiate_payment_for_priced_ticket(
        self, client: AsyncClient, test_user, paid_event_with_tickets
    ):
        evt = paid_event_with_tickets
        with patch(PAYSTACK_INIT_PATH, new=_init_mock(200_00)):
            resp = await client.post(
                "/api/v1/tickets/initiate-payment",
                json={
                    "event_id": str(evt["event"].event_id),
                    "items": [{"ticket_type_id": str(evt["vip"].ticket_type_id), "quantity": 2}],
                },
                headers=test_user["headers"],
            )
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["amount"] == 200.0
        assert data["currency"] == "GHS"
        assert "reference" in data
        assert data["access_code"] == FAKE_ACCESS_CODE
        assert "public_key" in data

    async def test_initiate_payment_rejects_zero_total(
        self, client: AsyncClient, test_user, db_session: AsyncSession, admin_user
    ):
        free_event = Event(
            title="Free Event",
            event_date=date(2026, 9, 2),
            event_time=time(10, 0),
            location="Community Hall",
            status="published",
            created_by=admin_user["user"].user_id,
        )
        db_session.add(free_event)
        await db_session.flush()
        free_type = TicketType(
            event_id=free_event.event_id,
            type_name="Free Entry",
            price=0,
            quantity_available=10,
            max_per_user=5,
        )
        db_session.add(free_type)
        await db_session.flush()

        resp = await client.post(
            "/api/v1/tickets/initiate-payment",
            json={
                "event_id": str(free_event.event_id),
                "items": [{"ticket_type_id": str(free_type.ticket_type_id), "quantity": 1}],
            },
            headers=test_user["headers"],
        )
        assert resp.status_code == 400
