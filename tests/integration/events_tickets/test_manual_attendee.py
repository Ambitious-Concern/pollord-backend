"""Issuing tickets for a Paystack payment our checkout never captured.

The buyer really paid, so the amount is recorded as genuine revenue. The
risk this code has to be careful about is issuing twice for one payment —
either by an admin submitting the same reference again, or by Paystack
retrying the webhook that failed in the first place.
"""
import json
from decimal import Decimal
from datetime import date, time
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import generate_secure_token
from app.models.event import Event, TicketType
from app.models.ticket import Ticket, TicketPurchase
from app.models.ticket_transaction import TicketTransaction


def paystack_ok(amount_pesewas=15000, email="ama@example.com", status="success"):
    return {
        "status": status,
        "amount": amount_pesewas,
        "currency": "GHS",
        "paid_at": "2026-08-21T10:00:00.000Z",
        "customer": {"email": email},
    }


@pytest_asyncio.fixture
async def manual_event(db_session: AsyncSession, admin_user) -> dict:
    event = Event(
        title="Manual Add Event",
        event_date=date(2026, 11, 20),
        event_time=time(19, 0),
        location="Accra",
        status="published",
        created_by=admin_user["user"].user_id,
    )
    db_session.add(event)
    await db_session.flush()

    vip = TicketType(
        event_id=event.event_id,
        type_name="VIP",
        price=150,
        quantity_available=5,
        max_per_user=2,
    )
    db_session.add(vip)
    await db_session.flush()
    return {"event": event, "vip": vip}


def _attendees_url(event_id):
    return f"/api/v1/admin/events/{event_id}/attendees"


def _verify_url(event_id):
    return f"/api/v1/admin/events/{event_id}/verify-payment"


def _body(vip, reference="ticket_ref_001", quantity=1, email="ama@example.com"):
    return {
        "reference": reference,
        "name": "Ama Mensah",
        "email": email,
        "ticket_type_id": str(vip.ticket_type_id),
        "quantity": quantity,
    }


@pytest.mark.asyncio
class TestVerifyPaymentLookup:
    async def test_requires_admin(self, client: AsyncClient, test_user, manual_event):
        response = await client.post(
            _verify_url(manual_event["event"].event_id),
            json={"reference": "ticket_ref_001"},
            headers=test_user["headers"],
        )
        assert response.status_code == 403

    async def test_returns_paystack_amount_and_payer(
        self, client: AsyncClient, admin_user, manual_event
    ):
        with patch(
            "app.services.paystack_service.PaystackService.verify_transaction",
            new=AsyncMock(return_value=paystack_ok()),
        ):
            response = await client.post(
                _verify_url(manual_event["event"].event_id),
                json={"reference": "ticket_ref_001"},
                headers=admin_user["headers"],
            )
        assert response.status_code == 200
        body = response.json()
        # Pesewas from Paystack, cedis on the wire.
        assert float(body["amount"]) == 150.0
        assert body["customer_email"] == "ama@example.com"
        assert body["paystack_status"] == "success"
        assert body["already_fulfilled"] is False

    async def test_flags_a_reference_already_used(
        self, client: AsyncClient, db_session, admin_user, manual_event
    ):
        db_session.add(
            TicketPurchase(
                guest_name="Ama",
                guest_email="ama@example.com",
                event_id=manual_event["event"].event_id,
                total_amount=150,
                payment_status="completed",
                payment_reference="ticket_ref_001",
            )
        )
        await db_session.flush()

        with patch(
            "app.services.paystack_service.PaystackService.verify_transaction",
            new=AsyncMock(return_value=paystack_ok()),
        ):
            response = await client.post(
                _verify_url(manual_event["event"].event_id),
                json={"reference": "ticket_ref_001"},
                headers=admin_user["headers"],
            )
        assert response.json()["already_fulfilled"] is True

    async def test_lookup_does_not_issue_anything(
        self, client: AsyncClient, db_session, admin_user, manual_event
    ):
        with patch(
            "app.services.paystack_service.PaystackService.verify_transaction",
            new=AsyncMock(return_value=paystack_ok()),
        ):
            await client.post(
                _verify_url(manual_event["event"].event_id),
                json={"reference": "ticket_ref_001"},
                headers=admin_user["headers"],
            )
        result = await db_session.execute(
            select(Ticket).where(Ticket.event_id == manual_event["event"].event_id)
        )
        assert result.scalars().all() == []


@pytest.mark.asyncio
class TestIssueTicketForUntrackedPayment:
    async def test_requires_admin(self, client: AsyncClient, test_user, manual_event):
        response = await client.post(
            _attendees_url(manual_event["event"].event_id),
            json=_body(manual_event["vip"]),
            headers=test_user["headers"],
        )
        assert response.status_code == 403

    async def test_issues_tickets_at_the_paystack_amount(
        self, client: AsyncClient, db_session, admin_user, manual_event
    ):
        with patch(
            "app.services.paystack_service.PaystackService.verify_transaction",
            new=AsyncMock(return_value=paystack_ok(amount_pesewas=30000)),
        ), patch("app.services.ticketing_service.send_email", return_value=True):
            response = await client.post(
                _attendees_url(manual_event["event"].event_id),
                json=_body(manual_event["vip"], quantity=2),
                headers=admin_user["headers"],
            )

        assert response.status_code == 201
        body = response.json()
        assert body["ticket_count"] == 2
        assert body["email_sent"] is True
        # Paystack's figure, not ticket price x quantity.
        assert float(body["amount"]) == 300.0

        purchase = await db_session.get(TicketPurchase, body["purchase_id"])
        assert purchase.payment_status == "completed"
        assert purchase.payment_reference == "ticket_ref_001"
        assert purchase.payment_method == "paystack"
        # Real revenue, so delivery tracking applies as with any purchase.
        assert purchase.confirmation_email_status == "sent"

    async def test_decrements_stock(
        self, client: AsyncClient, db_session, admin_user, manual_event
    ):
        with patch(
            "app.services.paystack_service.PaystackService.verify_transaction",
            new=AsyncMock(return_value=paystack_ok()),
        ), patch("app.services.ticketing_service.send_email", return_value=True):
            await client.post(
                _attendees_url(manual_event["event"].event_id),
                json=_body(manual_event["vip"], quantity=2),
                headers=admin_user["headers"],
            )
        await db_session.refresh(manual_event["vip"])
        assert manual_event["vip"].quantity_available == 3

    async def test_rejects_when_stock_exhausted(
        self, client: AsyncClient, admin_user, manual_event
    ):
        with patch(
            "app.services.paystack_service.PaystackService.verify_transaction",
            new=AsyncMock(return_value=paystack_ok()),
        ), patch("app.services.ticketing_service.send_email", return_value=True):
            response = await client.post(
                _attendees_url(manual_event["event"].event_id),
                json=_body(manual_event["vip"], quantity=9),
                headers=admin_user["headers"],
            )
        assert response.status_code == 409
        assert "no stock left" in response.json()["detail"]

    async def test_ignores_max_per_user(
        self, client: AsyncClient, admin_user, manual_event
    ):
        """max_per_user is a checkout guard; an admin fixing a real payment
        must not be blocked by it."""
        with patch(
            "app.services.paystack_service.PaystackService.verify_transaction",
            new=AsyncMock(return_value=paystack_ok()),
        ), patch("app.services.ticketing_service.send_email", return_value=True):
            response = await client.post(
                _attendees_url(manual_event["event"].event_id),
                # max_per_user is 2
                json=_body(manual_event["vip"], quantity=4),
                headers=admin_user["headers"],
            )
        assert response.status_code == 201
        assert response.json()["ticket_count"] == 4

    async def test_refuses_a_reference_already_issued(
        self, client: AsyncClient, admin_user, manual_event
    ):
        """An admin double-submitting must not produce two sets of tickets."""
        with patch(
            "app.services.paystack_service.PaystackService.verify_transaction",
            new=AsyncMock(return_value=paystack_ok()),
        ), patch("app.services.ticketing_service.send_email", return_value=True):
            first = await client.post(
                _attendees_url(manual_event["event"].event_id),
                json=_body(manual_event["vip"]),
                headers=admin_user["headers"],
            )
            second = await client.post(
                _attendees_url(manual_event["event"].event_id),
                json=_body(manual_event["vip"]),
                headers=admin_user["headers"],
            )

        assert first.status_code == 201
        assert second.status_code == 409
        assert "already been used" in second.json()["detail"]

    async def test_refuses_a_payment_paystack_says_failed(
        self, client: AsyncClient, db_session, admin_user, manual_event
    ):
        with patch(
            "app.services.paystack_service.PaystackService.verify_transaction",
            new=AsyncMock(return_value=paystack_ok(status="abandoned")),
        ), patch("app.services.ticketing_service.send_email") as mock_send:
            response = await client.post(
                _attendees_url(manual_event["event"].event_id),
                json=_body(manual_event["vip"]),
                headers=admin_user["headers"],
            )

        assert response.status_code == 402
        assert "abandoned" in response.json()["detail"]
        # Nothing was issued and no ticket reached them. Post-request DB state
        # isn't assertable here — get_db rolls back on the exception, which in
        # this harness also unwinds the fixture's own rows. The ordering in the
        # service is what guarantees stock is untouched: the Paystack status
        # check runs before decrement_available.
        mock_send.assert_not_called()

    async def test_marks_pending_transaction_success_to_block_late_webhook(
        self, client: AsyncClient, db_session, admin_user, manual_event
    ):
        """The original webhook may still retry. Once we've issued manually,
        it must find the transaction already successful and refuse."""
        db_session.add(
            TicketTransaction(
                reference="ticket_ref_001",
                user_id=None,
                guest_name="Ama",
                guest_email="ama@example.com",
                event_id=manual_event["event"].event_id,
                items=[
                    {
                        "ticket_type_id": str(manual_event["vip"].ticket_type_id),
                        "quantity": 1,
                    }
                ],
                amount=150,
                status="pending",
            )
        )
        await db_session.flush()

        with patch(
            "app.services.paystack_service.PaystackService.verify_transaction",
            new=AsyncMock(return_value=paystack_ok()),
        ), patch("app.services.ticketing_service.send_email", return_value=True):
            response = await client.post(
                _attendees_url(manual_event["event"].event_id),
                json=_body(manual_event["vip"]),
                headers=admin_user["headers"],
            )
        assert response.status_code == 201

        result = await db_session.execute(
            select(TicketTransaction).where(
                TicketTransaction.reference == "ticket_ref_001"
            )
        )
        assert result.scalar_one().status == "success"

    async def test_refuses_when_transaction_already_successful(
        self, client: AsyncClient, db_session, admin_user, manual_event
    ):
        db_session.add(
            TicketTransaction(
                reference="ticket_ref_001",
                guest_email="ama@example.com",
                event_id=manual_event["event"].event_id,
                items=[],
                amount=150,
                status="success",
            )
        )
        await db_session.flush()

        with patch(
            "app.services.paystack_service.PaystackService.verify_transaction",
            new=AsyncMock(return_value=paystack_ok()),
        ), patch("app.services.ticketing_service.send_email", return_value=True):
            response = await client.post(
                _attendees_url(manual_event["event"].event_id),
                json=_body(manual_event["vip"]),
                headers=admin_user["headers"],
            )
        assert response.status_code == 409
        assert "already been fulfilled" in response.json()["detail"]

    async def test_links_to_an_existing_account_when_one_matches(
        self, client: AsyncClient, db_session, admin_user, test_user, manual_event
    ):
        """So the tickets appear in their My Tickets, not just as guest links."""
        with patch(
            "app.services.paystack_service.PaystackService.verify_transaction",
            new=AsyncMock(return_value=paystack_ok()),
        ), patch("app.services.ticketing_service.send_email", return_value=True):
            response = await client.post(
                _attendees_url(manual_event["event"].event_id),
                json=_body(manual_event["vip"], email=test_user["user"].email),
                headers=admin_user["headers"],
            )
        assert response.status_code == 201

        purchase = await db_session.get(
            TicketPurchase, response.json()["purchase_id"]
        )
        assert purchase.user_id == test_user["user"].user_id
        assert purchase.guest_email is None

    async def test_rejects_ticket_type_from_another_event(
        self, client: AsyncClient, db_session, admin_user, manual_event
    ):
        other = Event(
            title="Other Event",
            event_date=date(2026, 12, 1),
            event_time=time(12, 0),
            location="Elsewhere",
            status="published",
            created_by=admin_user["user"].user_id,
        )
        db_session.add(other)
        await db_session.flush()
        foreign = TicketType(
            event_id=other.event_id,
            type_name="Foreign",
            price=10,
            quantity_available=5,
        )
        db_session.add(foreign)
        await db_session.flush()

        with patch(
            "app.services.paystack_service.PaystackService.verify_transaction",
            new=AsyncMock(return_value=paystack_ok()),
        ):
            response = await client.post(
                _attendees_url(manual_event["event"].event_id),
                json=_body(foreign),
                headers=admin_user["headers"],
            )
        assert response.status_code == 400
        assert "does not belong to this event" in response.json()["detail"]

    async def test_tickets_still_issued_when_email_fails(
        self, client: AsyncClient, db_session, admin_user, manual_event
    ):
        """A broken mail server must not cost them the ticket they paid for."""
        with patch(
            "app.services.paystack_service.PaystackService.verify_transaction",
            new=AsyncMock(return_value=paystack_ok()),
        ), patch("app.services.ticketing_service.send_email", return_value=False):
            response = await client.post(
                _attendees_url(manual_event["event"].event_id),
                json=_body(manual_event["vip"]),
                headers=admin_user["headers"],
            )

        assert response.status_code == 201
        body = response.json()
        assert body["email_sent"] is False
        assert "could not be sent" in body["message"]

        purchase = await db_session.get(TicketPurchase, body["purchase_id"])
        assert purchase.confirmation_email_status == "failed"

    async def test_writes_audit_log(
        self, client: AsyncClient, db_session, admin_user, manual_event
    ):
        from app.models.audit_log import AuditLog

        with patch(
            "app.services.paystack_service.PaystackService.verify_transaction",
            new=AsyncMock(return_value=paystack_ok()),
        ), patch("app.services.ticketing_service.send_email", return_value=True):
            await client.post(
                _attendees_url(manual_event["event"].event_id),
                json=_body(manual_event["vip"]),
                headers=admin_user["headers"],
            )

        result = await db_session.execute(
            select(AuditLog).where(AuditLog.action_type == "TICKET_ISSUED_MANUALLY")
        )
        log = result.scalars().first()
        assert log is not None
        assert log.user_id == admin_user["user"].user_id
        assert log.changes["reference"] == "ticket_ref_001"
        # Compare numerically — Decimal("150") and Decimal("150.00") are the
        # same money but not the same string.
        assert Decimal(log.changes["amount"]) == Decimal("150")
