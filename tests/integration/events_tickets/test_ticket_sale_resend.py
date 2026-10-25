"""Organizer-facing resend from the Tickets Sold table.

Same send path as the admin console's resend, but addressed by ticket and
scoped to the event's organizer rather than to platform admins.
"""
import json
from datetime import date, time
from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, generate_secure_token, hash_password
from app.models.event import Event, TicketType
from app.models.ticket import Ticket, TicketPurchase


@pytest_asyncio.fixture
async def organizer(db_session: AsyncSession) -> dict:
    from app.models.user import Role, User, UserRole

    user = User(
        email="organizer@example.com",
        password_hash=hash_password("Organizer1234!"),
        full_name="Organizer User",
        email_verified=True,
        account_status="active",
    )
    db_session.add(user)
    await db_session.flush()

    result = await db_session.execute(
        select(Role).where(Role.role_name == "Event Organizer")
    )
    role = result.scalar_one_or_none()
    if not role:
        role = Role(role_name="Event Organizer", permissions={"events": ["manage"]})
        db_session.add(role)
        await db_session.flush()

    db_session.add(UserRole(user_id=user.user_id, role_id=role.role_id))
    await db_session.flush()

    token = create_access_token(str(user.user_id))
    return {"user": user, "headers": {"Authorization": f"Bearer {token}"}}


@pytest_asyncio.fixture
async def sold_ticket(db_session: AsyncSession, organizer) -> dict:
    """An event owned by `organizer`, with one two-ticket guest order."""
    event = Event(
        title="Organizer Resend Event",
        description="An event for organizer resend testing",
        event_date=date(2026, 10, 4),
        event_time=time(20, 0),
        location="Test Venue",
        category="Concert",
        capacity=100,
        status="published",
        created_by=organizer["user"].user_id,
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

    purchase = TicketPurchase(
        guest_name="Ada Buyer",
        guest_email="ada@example.com",
        event_id=event.event_id,
        total_amount=0,
        payment_status="completed",
    )
    db_session.add(purchase)
    await db_session.flush()

    tickets = []
    for _ in range(2):
        code = generate_secure_token(16)
        ticket = Ticket(
            ticket_code=code,
            event_id=event.event_id,
            ticket_type_id=ticket_type.ticket_type_id,
            guest_name="Ada Buyer",
            guest_email="ada@example.com",
            purchase_id=purchase.purchase_id,
            qr_code_data=json.dumps({"ticket_code": code}),
        )
        db_session.add(ticket)
        tickets.append(ticket)
    await db_session.flush()

    return {"event": event, "purchase": purchase, "tickets": tickets}


def _url(ticket_id) -> str:
    return f"/api/v1/tickets/sales/{ticket_id}/resend-email"


@pytest.mark.asyncio
class TestOrganizerResend:
    async def test_organizer_can_resend_own_sale(
        self, client: AsyncClient, db_session, organizer, sold_ticket
    ):
        ticket = sold_ticket["tickets"][0]

        with patch(
            "app.services.ticketing_service.send_email", return_value=True
        ) as mock_send:
            response = await client.post(
                _url(ticket.ticket_id), json={}, headers=organizer["headers"]
            )

        assert response.status_code == 200
        body = response.json()
        assert body["sent"] is True
        assert body["email"] == "ada@example.com"
        # The order had two tickets, and the email covers the order.
        assert body["ticket_count"] == 2
        assert mock_send.call_args[0][0] == "ada@example.com"

        await db_session.refresh(sold_ticket["purchase"])
        assert sold_ticket["purchase"].confirmation_email_status == "sent"

    async def test_either_row_of_an_order_sends_the_same_email(
        self, client: AsyncClient, organizer, sold_ticket
    ):
        """Both rows resolve to the same purchase, so both send the full order."""
        with patch("app.services.ticketing_service.send_email", return_value=True):
            first = await client.post(
                _url(sold_ticket["tickets"][0].ticket_id),
                json={},
                headers=organizer["headers"],
            )
            second = await client.post(
                _url(sold_ticket["tickets"][1].ticket_id),
                json={},
                headers=organizer["headers"],
            )

        assert first.json()["ticket_count"] == second.json()["ticket_count"] == 2
        assert first.json()["email"] == second.json()["email"]

    async def test_organizer_can_correct_the_address(
        self, client: AsyncClient, db_session, organizer, sold_ticket
    ):
        with patch(
            "app.services.ticketing_service.send_email", return_value=True
        ) as mock_send:
            response = await client.post(
                _url(sold_ticket["tickets"][0].ticket_id),
                json={"email": "ada.fixed@example.com"},
                headers=organizer["headers"],
            )

        assert response.status_code == 200
        assert mock_send.call_args[0][0] == "ada.fixed@example.com"

        await db_session.refresh(sold_ticket["purchase"])
        assert sold_ticket["purchase"].confirmation_email_to == "ada.fixed@example.com"
        # Correcting one delivery must not silently rewrite the buyer's record.
        assert sold_ticket["purchase"].guest_email == "ada@example.com"

    async def test_other_organizer_cannot_resend(
        self, client: AsyncClient, test_user, sold_ticket
    ):
        """The authorization boundary: someone else's sale is off limits."""
        with patch(
            "app.services.ticketing_service.send_email", return_value=True
        ) as mock_send:
            response = await client.post(
                _url(sold_ticket["tickets"][0].ticket_id),
                json={},
                headers=test_user["headers"],
            )

        assert response.status_code == 403
        mock_send.assert_not_called()

    async def test_system_admin_can_resend_any_sale(
        self, client: AsyncClient, admin_user, sold_ticket
    ):
        """Admins keep the same escape hatch they have on the event routes."""
        with patch("app.services.ticketing_service.send_email", return_value=True):
            response = await client.post(
                _url(sold_ticket["tickets"][0].ticket_id),
                json={},
                headers=admin_user["headers"],
            )

        assert response.status_code == 200
        assert response.json()["sent"] is True

    async def test_requires_authentication(self, client: AsyncClient, sold_ticket):
        response = await client.post(
            _url(sold_ticket["tickets"][0].ticket_id), json={}
        )
        assert response.status_code == 401

    async def test_unknown_ticket_returns_404(self, client: AsyncClient, organizer):
        response = await client.post(
            _url("00000000-0000-0000-0000-000000000000"),
            json={},
            headers=organizer["headers"],
        )
        assert response.status_code == 404

    async def test_failed_send_is_reported_not_raised(
        self, client: AsyncClient, db_session, organizer, sold_ticket
    ):
        with patch("app.services.ticketing_service.send_email", return_value=False):
            response = await client.post(
                _url(sold_ticket["tickets"][0].ticket_id),
                json={},
                headers=organizer["headers"],
            )

        assert response.status_code == 200
        assert response.json()["sent"] is False

        await db_session.refresh(sold_ticket["purchase"])
        assert sold_ticket["purchase"].confirmation_email_status == "failed"
