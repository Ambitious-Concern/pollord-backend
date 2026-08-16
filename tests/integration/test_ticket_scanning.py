from datetime import date, datetime, time, timedelta, timezone
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_ticket_scan_token
from app.models.event import Event, TicketType
from app.models.ticket import Ticket, TicketPurchase


@pytest.fixture
async def event_with_ticket(db_session: AsyncSession, admin_user):
    event = Event(
        title="Scan Test Event",
        event_date=date(2026, 8, 20),
        event_time=time(18, 0),
        location="Test Venue",
        status="published",
        created_by=admin_user["user"].user_id,
    )
    db_session.add(event)
    await db_session.flush()

    ticket_type = TicketType(
        event_id=event.event_id,
        type_name="General",
        price=0,
        quantity_available=50,
        max_per_user=5,
    )
    db_session.add(ticket_type)
    await db_session.flush()

    purchase = TicketPurchase(
        event_id=event.event_id,
        guest_name="Jane Guest",
        guest_email="jane@example.com",
        total_amount=0,
        payment_status="completed",
    )
    db_session.add(purchase)
    await db_session.flush()

    ticket = Ticket(
        ticket_code="SCANTEST123",
        event_id=event.event_id,
        ticket_type_id=ticket_type.ticket_type_id,
        guest_name="Jane Guest",
        guest_email="jane@example.com",
        purchase_id=purchase.purchase_id,
        qr_code_data="{}",
    )
    db_session.add(ticket)
    await db_session.flush()

    return {"event": event, "ticket_type": ticket_type, "ticket": ticket}


@pytest.mark.asyncio
class TestScanTokenIssuance:
    async def test_organizer_can_mint_scan_token(
        self, client: AsyncClient, admin_user, event_with_ticket
    ):
        event = event_with_ticket["event"]
        response = await client.get(
            f"/api/v1/events/{event.event_id}/scan-token",
            headers=admin_user["headers"],
        )
        assert response.status_code == 200
        data = response.json()
        assert "scan_token" in data
        assert "expires_at" in data

    async def test_scan_token_requires_organizer_role(
        self, client: AsyncClient, test_user, event_with_ticket
    ):
        event = event_with_ticket["event"]
        response = await client.get(
            f"/api/v1/events/{event.event_id}/scan-token",
            headers=test_user["headers"],
        )
        assert response.status_code == 403

    async def test_scan_token_requires_auth(self, client: AsyncClient, event_with_ticket):
        event = event_with_ticket["event"]
        response = await client.get(f"/api/v1/events/{event.event_id}/scan-token")
        assert response.status_code == 401


@pytest.mark.asyncio
class TestScanInfo:
    async def test_get_scan_info_no_auth_required(self, client: AsyncClient, event_with_ticket):
        event = event_with_ticket["event"]
        token = create_ticket_scan_token(
            str(event.event_id), datetime.now(timezone.utc) + timedelta(days=1)
        )
        response = await client.get(f"/api/v1/tickets/public/scan-info/{token}")
        assert response.status_code == 200
        data = response.json()
        assert data["event_id"] == str(event.event_id)
        assert data["event_title"] == "Scan Test Event"

    async def test_scan_info_rejects_expired_token(self, client: AsyncClient, event_with_ticket):
        event = event_with_ticket["event"]
        token = create_ticket_scan_token(
            str(event.event_id), datetime.now(timezone.utc) - timedelta(days=1)
        )
        response = await client.get(f"/api/v1/tickets/public/scan-info/{token}")
        assert response.status_code == 404

    async def test_scan_info_rejects_garbage_token(self, client: AsyncClient):
        response = await client.get("/api/v1/tickets/public/scan-info/not-a-real-token")
        assert response.status_code == 404


@pytest.mark.asyncio
class TestPublicValidate:
    async def test_valid_ticket_for_matching_event(
        self, client: AsyncClient, event_with_ticket
    ):
        event = event_with_ticket["event"]
        token = create_ticket_scan_token(
            str(event.event_id), datetime.now(timezone.utc) + timedelta(days=1)
        )
        response = await client.post(
            "/api/v1/tickets/public/validate",
            json={"scan_token": token, "ticket_code": "SCANTEST123"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["valid"] is True
        assert data["attendee_name"] == "Jane Guest"

    async def test_rejects_ticket_from_a_different_event(
        self, client: AsyncClient, event_with_ticket
    ):
        other_event_id = uuid4()
        token = create_ticket_scan_token(
            str(other_event_id), datetime.now(timezone.utc) + timedelta(days=1)
        )
        response = await client.post(
            "/api/v1/tickets/public/validate",
            json={"scan_token": token, "ticket_code": "SCANTEST123"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["valid"] is False

    async def test_rejects_expired_scan_token(self, client: AsyncClient, event_with_ticket):
        event = event_with_ticket["event"]
        token = create_ticket_scan_token(
            str(event.event_id), datetime.now(timezone.utc) - timedelta(days=1)
        )
        response = await client.post(
            "/api/v1/tickets/public/validate",
            json={"scan_token": token, "ticket_code": "SCANTEST123"},
        )
        assert response.status_code == 404

    async def test_second_scan_of_same_ticket_is_rejected(
        self, client: AsyncClient, event_with_ticket
    ):
        event = event_with_ticket["event"]
        token = create_ticket_scan_token(
            str(event.event_id), datetime.now(timezone.utc) + timedelta(days=1)
        )
        first = await client.post(
            "/api/v1/tickets/public/validate",
            json={"scan_token": token, "ticket_code": "SCANTEST123"},
        )
        assert first.json()["valid"] is True

        second = await client.post(
            "/api/v1/tickets/public/validate",
            json={"scan_token": token, "ticket_code": "SCANTEST123"},
        )
        assert second.json()["valid"] is False
        assert "already been used" in second.json()["message"]
