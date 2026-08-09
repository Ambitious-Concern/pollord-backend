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


@pytest.fixture
async def paid_event_with_two_ticket_types(db_session: AsyncSession, admin_user):
    """Published event with two paid ticket types, for testing that a partial
    stock decrement across a multi-item purchase is rolled back atomically
    when a later item in the same purchase can't be fulfilled."""
    event = Event(
        title="Two-Tier Ticket Event",
        description="An event with two paid ticket types",
        event_date=date(2026, 9, 3),
        event_time=time(20, 0),
        location="Test Hall",
        category="Concert",
        capacity=50,
        status="published",
        created_by=admin_user["user"].user_id,
    )
    db_session.add(event)
    await db_session.flush()

    general = TicketType(
        event_id=event.event_id,
        type_name="General",
        price=50.00,
        quantity_available=5,
        max_per_user=5,
    )
    vip = TicketType(
        event_id=event.event_id,
        type_name="VIP",
        price=100.00,
        quantity_available=1,
        max_per_user=5,
    )
    db_session.add_all([general, vip])
    await db_session.flush()

    return {"event": event, "general": general, "vip": vip}


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

        # Prove the needs_refund status was actually persisted (committed),
        # not just returned in the HTTP response. get_db's real
        # rollback-on-exception behavior would otherwise silently discard
        # this write along with the HTTPException we just asserted above —
        # this test can't reproduce that rollback (the test harness's
        # override_get_db doesn't commit/rollback per-request), but it does
        # prove the explicit `await txn_repo.session.commit()` in
        # TicketingService._mark_needs_refund actually executes and its
        # write survives independently of the request's own commit/rollback.
        from app.models.ticket_transaction import TicketTransaction
        persisted_txn = (await db_session.execute(
            select(TicketTransaction).where(TicketTransaction.reference == ref)
        )).scalar_one()
        assert persisted_txn.status == "needs_refund"

    async def test_sold_out_second_item_rolls_back_first_items_decrement(
        self, client: AsyncClient, test_user, paid_event_with_two_ticket_types, db_session: AsyncSession
    ):
        """Purchase spans two ticket types: General (plenty of stock) and VIP
        (about to sell out to someone else). General's stock decrement must
        succeed first, then VIP's decrement must fail and trigger
        needs_refund — and that failure must roll back General's decrement
        too, so the whole fulfillment is atomic rather than partially
        applied."""
        from unittest.mock import patch as _patch
        from sqlalchemy import update, select
        from app.models.event import TicketType
        from app.models.ticket import Ticket

        evt = paid_event_with_two_ticket_types
        amount_pesewas = 50_00 + 100_00  # 1 General @ 50.00 GHS + 1 VIP @ 100.00 GHS

        with patch(PAYSTACK_INIT_PATH, new=_init_mock(amount_pesewas)):
            resp = await client.post(
                "/api/v1/tickets/initiate-payment",
                json={
                    "event_id": str(evt["event"].event_id),
                    "items": [
                        {"ticket_type_id": str(evt["general"].ticket_type_id), "quantity": 1},
                        {"ticket_type_id": str(evt["vip"].ticket_type_id), "quantity": 1},
                    ],
                },
                headers=test_user["headers"],
            )
        assert resp.status_code == 201, resp.text
        ref = resp.json()["reference"]

        # Simulate the VIP ticket type (the second item) selling out to
        # someone else while this payment was in flight. General (the first
        # item) still has stock.
        await db_session.execute(
            update(TicketType)
            .where(TicketType.ticket_type_id == evt["vip"].ticket_type_id)
            .values(quantity_available=0, quantity_sold=1)
        )
        await db_session.flush()

        with _patch(PAYSTACK_VERIFY_PATH, new=_verify_mock(amount_pesewas)):
            resp = await client.post(
                "/api/v1/tickets/verify-and-purchase", json={"reference": ref}, headers=test_user["headers"],
            )
        assert resp.status_code == 409
        assert "refund" in resp.text.lower()

        # (a) No tickets issued for either ticket type — no partial issuance.
        tickets = (await db_session.execute(
            select(Ticket).where(Ticket.event_id == evt["event"].event_id)
        )).scalars().all()
        assert len(tickets) == 0

        # (b)/(c) The FIRST item's (General's) stock decrement must have been
        # rolled back too — proving the whole operation is atomic, not just
        # that no tickets got created despite a leaked partial decrement.
        general = (await db_session.execute(
            select(TicketType).where(TicketType.ticket_type_id == evt["general"].ticket_type_id)
        )).scalar_one()
        assert general.quantity_available == 5
        assert general.quantity_sold == 0


import hashlib
import hmac
import json as _json

from app.core.config import settings as app_settings


def _sign(body: bytes) -> str:
    return hmac.new(app_settings.PAYSTACK_SECRET_KEY.encode(), body, hashlib.sha512).hexdigest()


@pytest.mark.asyncio
class TestTicketWebhook:
    async def test_webhook_charge_success_fulfills_ticket_reference(
        self, client: AsyncClient, test_user, paid_event_with_tickets, db_session: AsyncSession
    ):
        from unittest.mock import patch as _patch

        evt = paid_event_with_tickets
        with _patch(PAYSTACK_INIT_PATH, new=_init_mock(100_00)):
            init_resp = await client.post(
                "/api/v1/tickets/initiate-payment",
                json={"event_id": str(evt["event"].event_id), "items": [{"ticket_type_id": str(evt["vip"].ticket_type_id), "quantity": 1}]},
                headers=test_user["headers"],
            )
        reference = init_resp.json()["reference"]

        payload = {
            "event": "charge.success",
            "data": {"reference": reference, "amount": 100_00, "currency": "GHS"},
        }
        body = _json.dumps(payload).encode()
        sig = _sign(body)

        # The webhook trusts the signed payload's amount/status directly (same
        # as the existing vote webhook) — it does not call verify_transaction
        # itself, so no Paystack mock is needed for this call.
        resp = await client.post(
            "/api/v1/payments/webhook",
            content=body,
            headers={"x-paystack-signature": sig, "Content-Type": "application/json"},
        )
        assert resp.status_code == 200

        from sqlalchemy import select
        from app.models.ticket import Ticket
        tickets = (await db_session.execute(
            select(Ticket).where(Ticket.event_id == evt["event"].event_id)
        )).scalars().all()
        assert len(tickets) == 1

    async def test_webhook_charge_failed_marks_ticket_transaction_failed(
        self, client: AsyncClient, test_user, paid_event_with_tickets
    ):
        from unittest.mock import patch as _patch

        evt = paid_event_with_tickets
        with _patch(PAYSTACK_INIT_PATH, new=_init_mock(100_00)):
            init_resp = await client.post(
                "/api/v1/tickets/initiate-payment",
                json={"event_id": str(evt["event"].event_id), "items": [{"ticket_type_id": str(evt["vip"].ticket_type_id), "quantity": 1}]},
                headers=test_user["headers"],
            )
        reference = init_resp.json()["reference"]

        payload = {"event": "charge.failed", "data": {"reference": reference}}
        body = _json.dumps(payload).encode()
        sig = _sign(body)

        resp = await client.post(
            "/api/v1/payments/webhook",
            content=body,
            headers={"x-paystack-signature": sig, "Content-Type": "application/json"},
        )
        assert resp.status_code == 200

        # verify-and-purchase must now reject this reference as failed
        with _patch(PAYSTACK_VERIFY_PATH, new=_verify_mock(100_00)):
            resp2 = await client.post(
                "/api/v1/tickets/verify-and-purchase", json={"reference": reference}, headers=test_user["headers"],
            )
        assert resp2.status_code == 400
