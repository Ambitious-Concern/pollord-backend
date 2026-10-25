"""Admin ticket-purchase listing and confirmation-email resend.

Covers the support path for "I paid but never got my ticket": finding the
purchase across every event, seeing whether its email actually went out, and
resending it — optionally to a corrected address.
"""
import json
from datetime import date, time
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import generate_secure_token
from app.models.event import Event, TicketType
from app.models.ticket import Ticket, TicketPurchase


@pytest.fixture
async def event_for_purchases(db_session: AsyncSession, admin_user):
    event = Event(
        title="Admin Resend Test Event",
        description="An event for admin resend testing",
        event_date=date(2026, 9, 20),
        event_time=time(19, 0),
        location="Test Venue",
        category="Concert",
        capacity=100,
        status="published",
        created_by=admin_user["user"].user_id,
    )
    db_session.add(event)
    await db_session.flush()

    ticket_type = TicketType(
        event_id=event.event_id,
        type_name="General Admission",
        price=0,
        quantity_available=50,
        max_per_user=5,
    )
    db_session.add(ticket_type)
    await db_session.flush()

    return {"event": event, "ticket_type": ticket_type}


async def _make_purchase(
    db_session: AsyncSession,
    event_for_purchases,
    *,
    guest_email: str | None = None,
    guest_name: str | None = None,
    user_id=None,
    ticket_count: int = 1,
    payment_reference: str | None = None,
    email_status: str | None = None,
) -> TicketPurchase:
    """Build a completed purchase directly, so tests can set up the exact
    delivery state they care about without going through Paystack."""
    event = event_for_purchases["event"]
    ticket_type = event_for_purchases["ticket_type"]

    purchase = TicketPurchase(
        user_id=user_id,
        guest_name=guest_name,
        guest_email=guest_email,
        event_id=event.event_id,
        total_amount=0,
        payment_status="completed",
        payment_reference=payment_reference,
        confirmation_email_status=email_status,
    )
    db_session.add(purchase)
    await db_session.flush()

    for _ in range(ticket_count):
        code = generate_secure_token(16)
        db_session.add(
            Ticket(
                ticket_code=code,
                event_id=event.event_id,
                ticket_type_id=ticket_type.ticket_type_id,
                user_id=user_id,
                guest_name=guest_name,
                guest_email=guest_email,
                purchase_id=purchase.purchase_id,
                qr_code_data=json.dumps({"ticket_code": code}),
            )
        )
    await db_session.flush()
    return purchase


@pytest.mark.asyncio
class TestListTicketPurchases:
    async def test_requires_admin(self, client: AsyncClient, test_user):
        response = await client.get(
            "/api/v1/admin/ticket-purchases", headers=test_user["headers"]
        )
        assert response.status_code == 403

    async def test_requires_authentication(self, client: AsyncClient):
        response = await client.get("/api/v1/admin/ticket-purchases")
        assert response.status_code == 401

    async def test_lists_purchase_with_buyer_and_email_status(
        self, client: AsyncClient, db_session, admin_user, event_for_purchases
    ):
        await _make_purchase(
            db_session,
            event_for_purchases,
            guest_email="buyer@example.com",
            guest_name="Guest Buyer",
            ticket_count=3,
        )

        response = await client.get(
            "/api/v1/admin/ticket-purchases", headers=admin_user["headers"]
        )
        assert response.status_code == 200
        body = response.json()
        assert body["total"] >= 1

        row = next(r for r in body["items"] if r["buyer_email"] == "buyer@example.com")
        assert row["buyer_name"] == "Guest Buyer"
        assert row["is_guest"] is True
        assert row["ticket_count"] == 3
        assert row["event_title"] == "Admin Resend Test Event"
        # Never attempted, so it must read as unknown rather than implying
        # the email went out.
        assert row["confirmation_email_status"] == "unknown"

    async def test_lists_registered_buyer_from_user_record(
        self, client: AsyncClient, db_session, admin_user, test_user, event_for_purchases
    ):
        await _make_purchase(
            db_session, event_for_purchases, user_id=test_user["user"].user_id
        )

        response = await client.get(
            "/api/v1/admin/ticket-purchases", headers=admin_user["headers"]
        )
        row = next(
            r
            for r in response.json()["items"]
            if r["buyer_email"] == test_user["user"].email
        )
        assert row["is_guest"] is False
        assert row["buyer_name"] == test_user["user"].full_name

    async def test_search_matches_guest_email(
        self, client: AsyncClient, db_session, admin_user, event_for_purchases
    ):
        await _make_purchase(
            db_session, event_for_purchases, guest_email="findme@example.com"
        )
        await _make_purchase(
            db_session, event_for_purchases, guest_email="other@example.com"
        )

        response = await client.get(
            "/api/v1/admin/ticket-purchases",
            params={"search": "findme"},
            headers=admin_user["headers"],
        )
        body = response.json()
        assert body["total"] == 1
        assert body["items"][0]["buyer_email"] == "findme@example.com"

    async def test_search_matches_payment_reference(
        self, client: AsyncClient, db_session, admin_user, event_for_purchases
    ):
        await _make_purchase(
            db_session,
            event_for_purchases,
            guest_email="ref@example.com",
            payment_reference="PSK_REF_12345",
        )

        response = await client.get(
            "/api/v1/admin/ticket-purchases",
            params={"search": "psk_ref_12345"},
            headers=admin_user["headers"],
        )
        body = response.json()
        assert body["total"] == 1
        assert body["items"][0]["payment_reference"] == "PSK_REF_12345"

    async def test_search_matches_registered_buyer_email(
        self, client: AsyncClient, db_session, admin_user, test_user, event_for_purchases
    ):
        await _make_purchase(
            db_session, event_for_purchases, user_id=test_user["user"].user_id
        )
        await _make_purchase(
            db_session, event_for_purchases, guest_email="nomatch@example.com"
        )

        response = await client.get(
            "/api/v1/admin/ticket-purchases",
            params={"search": test_user["user"].email},
            headers=admin_user["headers"],
        )
        body = response.json()
        assert body["total"] == 1
        assert body["items"][0]["buyer_email"] == test_user["user"].email

    async def test_filter_by_email_status_failed(
        self, client: AsyncClient, db_session, admin_user, event_for_purchases
    ):
        await _make_purchase(
            db_session,
            event_for_purchases,
            guest_email="failed@example.com",
            email_status="failed",
        )
        await _make_purchase(
            db_session,
            event_for_purchases,
            guest_email="sent@example.com",
            email_status="sent",
        )

        response = await client.get(
            "/api/v1/admin/ticket-purchases",
            params={"email_status": "failed"},
            headers=admin_user["headers"],
        )
        body = response.json()
        assert body["total"] == 1
        assert body["items"][0]["buyer_email"] == "failed@example.com"

    async def test_filter_by_email_status_unknown_matches_untracked_rows(
        self, client: AsyncClient, db_session, admin_user, event_for_purchases
    ):
        await _make_purchase(
            db_session, event_for_purchases, guest_email="untracked@example.com"
        )
        await _make_purchase(
            db_session,
            event_for_purchases,
            guest_email="tracked@example.com",
            email_status="sent",
        )

        response = await client.get(
            "/api/v1/admin/ticket-purchases",
            params={"email_status": "unknown"},
            headers=admin_user["headers"],
        )
        body = response.json()
        assert body["total"] == 1
        assert body["items"][0]["buyer_email"] == "untracked@example.com"

    async def test_total_reflects_filter_not_page_size(
        self, client: AsyncClient, db_session, admin_user, event_for_purchases
    ):
        for i in range(3):
            await _make_purchase(
                db_session, event_for_purchases, guest_email=f"page{i}@example.com"
            )

        response = await client.get(
            "/api/v1/admin/ticket-purchases",
            params={"limit": 2},
            headers=admin_user["headers"],
        )
        body = response.json()
        assert len(body["items"]) == 2
        assert body["total"] == 3


@pytest.mark.asyncio
class TestResendTicketEmail:
    async def test_requires_admin(
        self, client: AsyncClient, db_session, test_user, event_for_purchases
    ):
        purchase = await _make_purchase(
            db_session, event_for_purchases, guest_email="buyer@example.com"
        )
        response = await client.post(
            f"/api/v1/admin/ticket-purchases/{purchase.purchase_id}/resend-email",
            json={},
            headers=test_user["headers"],
        )
        assert response.status_code == 403

    async def test_resends_to_original_address(
        self, client: AsyncClient, db_session, admin_user, event_for_purchases
    ):
        purchase = await _make_purchase(
            db_session,
            event_for_purchases,
            guest_email="buyer@example.com",
            ticket_count=2,
        )

        with patch(
            "app.services.ticketing_service.send_email", return_value=True
        ) as mock_send:
            response = await client.post(
                f"/api/v1/admin/ticket-purchases/{purchase.purchase_id}/resend-email",
                json={},
                headers=admin_user["headers"],
            )

        assert response.status_code == 200
        body = response.json()
        assert body["sent"] is True
        assert body["email"] == "buyer@example.com"
        assert body["ticket_count"] == 2

        mock_send.assert_called_once()
        assert mock_send.call_args[0][0] == "buyer@example.com"

        await db_session.refresh(purchase)
        assert purchase.confirmation_email_status == "sent"
        assert purchase.confirmation_email_to == "buyer@example.com"
        assert purchase.confirmation_email_attempted_at is not None

    async def test_override_address_receives_the_email(
        self, client: AsyncClient, db_session, admin_user, event_for_purchases
    ):
        purchase = await _make_purchase(
            db_session, event_for_purchases, guest_email="typo@gmial.com"
        )

        with patch(
            "app.services.ticketing_service.send_email", return_value=True
        ) as mock_send:
            response = await client.post(
                f"/api/v1/admin/ticket-purchases/{purchase.purchase_id}/resend-email",
                json={"email": "correct@gmail.com"},
                headers=admin_user["headers"],
            )

        assert response.status_code == 200
        assert response.json()["email"] == "correct@gmail.com"
        assert mock_send.call_args[0][0] == "correct@gmail.com"

        await db_session.refresh(purchase)
        # The corrected address is what we last tried, and what the next
        # admin looking at this row needs to see.
        assert purchase.confirmation_email_to == "correct@gmail.com"
        # The buyer's own record is left alone — correcting an address is a
        # separate decision from getting this one ticket delivered.
        assert purchase.guest_email == "typo@gmial.com"

    async def test_failed_send_is_reported_and_recorded(
        self, client: AsyncClient, db_session, admin_user, event_for_purchases
    ):
        purchase = await _make_purchase(
            db_session, event_for_purchases, guest_email="buyer@example.com"
        )

        with patch("app.services.ticketing_service.send_email", return_value=False):
            response = await client.post(
                f"/api/v1/admin/ticket-purchases/{purchase.purchase_id}/resend-email",
                json={},
                headers=admin_user["headers"],
            )

        # A refused send is still a completed request — the admin needs the
        # answer, not an opaque 500.
        assert response.status_code == 200
        assert response.json()["sent"] is False

        await db_session.refresh(purchase)
        assert purchase.confirmation_email_status == "failed"

    async def test_purchase_without_tickets_is_rejected(
        self, client: AsyncClient, db_session, admin_user, event_for_purchases
    ):
        purchase = await _make_purchase(
            db_session,
            event_for_purchases,
            guest_email="unfulfilled@example.com",
            ticket_count=0,
        )

        with patch(
            "app.services.ticketing_service.send_email", return_value=True
        ) as mock_send:
            response = await client.post(
                f"/api/v1/admin/ticket-purchases/{purchase.purchase_id}/resend-email",
                json={},
                headers=admin_user["headers"],
            )

        assert response.status_code == 409
        assert "never fulfilled" in response.json()["detail"]
        mock_send.assert_not_called()

    async def test_unknown_purchase_returns_404(
        self, client: AsyncClient, admin_user
    ):
        response = await client.post(
            "/api/v1/admin/ticket-purchases/"
            "00000000-0000-0000-0000-000000000000/resend-email",
            json={},
            headers=admin_user["headers"],
        )
        assert response.status_code == 404

    async def test_writes_audit_log(
        self, client: AsyncClient, db_session, admin_user, event_for_purchases
    ):
        from app.models.audit_log import AuditLog

        purchase = await _make_purchase(
            db_session, event_for_purchases, guest_email="buyer@example.com"
        )

        with patch("app.services.ticketing_service.send_email", return_value=True):
            await client.post(
                f"/api/v1/admin/ticket-purchases/{purchase.purchase_id}/resend-email",
                json={"email": "elsewhere@example.com"},
                headers=admin_user["headers"],
            )

        result = await db_session.execute(
            select(AuditLog).where(AuditLog.action_type == "TICKET_EMAIL_RESENT")
        )
        log = result.scalars().first()
        assert log is not None
        assert log.entity_id == purchase.purchase_id
        assert log.user_id == admin_user["user"].user_id
        assert log.changes["email"] == "elsewhere@example.com"
        assert log.changes["overridden"] is True
        assert log.changes["sent"] is True


@pytest.mark.asyncio
class TestPurchaseRecordsDeliveryOutcome:
    """The listing is only useful if the purchase flow writes the status."""

    async def test_successful_purchase_records_sent(
        self, client: AsyncClient, db_session, test_user, event_for_purchases
    ):
        with patch("app.services.ticketing_service.send_email", return_value=True):
            response = await client.post(
                "/api/v1/tickets/purchase",
                json={
                    "event_id": str(event_for_purchases["event"].event_id),
                    "items": [
                        {
                            "ticket_type_id": str(
                                event_for_purchases["ticket_type"].ticket_type_id
                            ),
                            "quantity": 1,
                        }
                    ],
                },
                headers=test_user["headers"],
            )
        assert response.status_code == 201

        purchase = await db_session.get(
            TicketPurchase, response.json()["purchase_id"]
        )
        assert purchase.confirmation_email_status == "sent"
        assert purchase.confirmation_email_to == test_user["user"].email

    async def test_failed_send_leaves_purchase_marked_failed(
        self, client: AsyncClient, db_session, test_user, event_for_purchases
    ):
        with patch("app.services.ticketing_service.send_email", return_value=False):
            response = await client.post(
                "/api/v1/tickets/purchase",
                json={
                    "event_id": str(event_for_purchases["event"].event_id),
                    "items": [
                        {
                            "ticket_type_id": str(
                                event_for_purchases["ticket_type"].ticket_type_id
                            ),
                            "quantity": 1,
                        }
                    ],
                },
                headers=test_user["headers"],
            )
        # The purchase still succeeds — a broken mail server must not cost
        # the buyer their ticket.
        assert response.status_code == 201

        purchase = await db_session.get(
            TicketPurchase, response.json()["purchase_id"]
        )
        assert purchase.confirmation_email_status == "failed"
