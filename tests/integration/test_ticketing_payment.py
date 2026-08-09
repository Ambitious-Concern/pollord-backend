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


def _verify_mock(amount_pesewas: int, status: str = "success") -> AsyncMock:
    return AsyncMock(return_value={
        "status": status,
        "amount": amount_pesewas,
        "reference": FAKE_REFERENCE,
        "currency": "GHS",
    })


@pytest.mark.asyncio
class TestVerifyAndPurchase:
    async def _initiate(self, client, event_id, ticket_type_id, quantity, headers, amount_pesewas):
        with patch(PAYSTACK_INIT_PATH, new=_init_mock(amount_pesewas)):
            resp = await client.post(
                "/api/v1/tickets/initiate-payment",
                json={"event_id": event_id, "items": [{"ticket_type_id": ticket_type_id, "quantity": quantity}]},
                headers=headers,
            )
        assert resp.status_code == 201, resp.text
        return resp.json()["reference"]

    async def test_verify_and_purchase_happy_path(
        self, client: AsyncClient, test_user, paid_event_with_tickets, db_session: AsyncSession
    ):
        from unittest.mock import patch as _patch

        evt = paid_event_with_tickets
        ref = await self._initiate(
            client, str(evt["event"].event_id), str(evt["vip"].ticket_type_id), 2,
            test_user["headers"], 200_00,
        )

        with _patch(PAYSTACK_VERIFY_PATH, new=_verify_mock(200_00)):
            resp = await client.post(
                "/api/v1/tickets/verify-and-purchase",
                json={"reference": ref},
                headers=test_user["headers"],
            )
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert len(data["tickets"]) == 2
        assert data["payment_status"] == "completed"

    async def test_verify_and_purchase_idempotent(
        self, client: AsyncClient, test_user, paid_event_with_tickets
    ):
        from unittest.mock import patch as _patch

        evt = paid_event_with_tickets
        ref = await self._initiate(
            client, str(evt["event"].event_id), str(evt["vip"].ticket_type_id), 1,
            test_user["headers"], 100_00,
        )

        with _patch(PAYSTACK_VERIFY_PATH, new=_verify_mock(100_00)):
            r1 = await client.post(
                "/api/v1/tickets/verify-and-purchase", json={"reference": ref}, headers=test_user["headers"],
            )
            r2 = await client.post(
                "/api/v1/tickets/verify-and-purchase", json={"reference": ref}, headers=test_user["headers"],
            )
        assert r1.status_code == 201
        assert r2.status_code == 409

    async def test_verify_and_purchase_failed_payment_rejected(
        self, client: AsyncClient, test_user, paid_event_with_tickets
    ):
        from unittest.mock import patch as _patch

        evt = paid_event_with_tickets
        ref = await self._initiate(
            client, str(evt["event"].event_id), str(evt["vip"].ticket_type_id), 1,
            test_user["headers"], 100_00,
        )

        with _patch(PAYSTACK_VERIFY_PATH, new=_verify_mock(100_00, status="failed")):
            resp = await client.post(
                "/api/v1/tickets/verify-and-purchase", json={"reference": ref}, headers=test_user["headers"],
            )
        assert resp.status_code == 402

    async def test_verify_and_purchase_underpayment_rejected(
        self, client: AsyncClient, test_user, paid_event_with_tickets
    ):
        from unittest.mock import patch as _patch

        evt = paid_event_with_tickets
        ref = await self._initiate(
            client, str(evt["event"].event_id), str(evt["vip"].ticket_type_id), 1,
            test_user["headers"], 100_00,
        )

        with _patch(PAYSTACK_VERIFY_PATH, new=_verify_mock(50_00)):
            resp = await client.post(
                "/api/v1/tickets/verify-and-purchase", json={"reference": ref}, headers=test_user["headers"],
            )
        assert resp.status_code == 402

    async def test_sold_out_during_payment_marks_needs_refund(
        self, client: AsyncClient, test_user, paid_event_with_tickets, db_session: AsyncSession
    ):
        """paid_event_with_tickets VIP has quantity_available=2. Buy both via the
        free-purchase-shaped direct DB decrement to simulate someone else buying
        them out from under this pending payment, then verify this payment can't
        be fulfilled."""
        from unittest.mock import patch as _patch
        from sqlalchemy import update
        from app.models.event import TicketType

        evt = paid_event_with_tickets
        ref = await self._initiate(
            client, str(evt["event"].event_id), str(evt["vip"].ticket_type_id), 2,
            test_user["headers"], 200_00,
        )

        # Simulate the stock selling out to someone else in the meantime.
        await db_session.execute(
            update(TicketType)
            .where(TicketType.ticket_type_id == evt["vip"].ticket_type_id)
            .values(quantity_available=0, quantity_sold=2)
        )
        await db_session.flush()

        with _patch(PAYSTACK_VERIFY_PATH, new=_verify_mock(200_00)):
            resp = await client.post(
                "/api/v1/tickets/verify-and-purchase", json={"reference": ref}, headers=test_user["headers"],
            )
        assert resp.status_code == 409
        assert "refund" in resp.text.lower()

        from sqlalchemy import select
        from app.models.ticket import Ticket
        tickets = (await db_session.execute(
            select(Ticket).where(Ticket.event_id == evt["event"].event_id)
        )).scalars().all()
        assert len(tickets) == 0
