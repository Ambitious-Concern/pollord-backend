from datetime import date, time

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.event import Event, TicketType


@pytest.fixture
async def published_event_with_tickets(db_session: AsyncSession, admin_user):
    """Create a published event with ticket types."""
    event = Event(
        title="Ticket Test Event",
        description="An event for ticket testing",
        event_date=date(2026, 8, 15),
        event_time=time(18, 0),
        location="Test Venue",
        category="Concert",
        capacity=100,
        status="published",
        created_by=admin_user["user"].user_id,
    )
    db_session.add(event)
    await db_session.flush()

    general = TicketType(
        event_id=event.event_id,
        type_name="General Admission",
        price=0,
        quantity_available=50,
        max_per_user=5,
    )
    vip = TicketType(
        event_id=event.event_id,
        type_name="VIP",
        price=100.00,
        quantity_available=10,
        max_per_user=2,
    )
    db_session.add_all([general, vip])
    await db_session.flush()

    return {"event": event, "general": general, "vip": vip}


@pytest.mark.asyncio
class TestTicketPurchase:
    async def test_purchase_free_ticket(
        self, client: AsyncClient, test_user, published_event_with_tickets
    ):
        evt = published_event_with_tickets
        response = await client.post(
            "/api/v1/tickets/purchase",
            json={
                "event_id": str(evt["event"].event_id),
                "items": [
                    {
                        "ticket_type_id": str(evt["general"].ticket_type_id),
                        "quantity": 2,
                    }
                ],
            },
            headers=test_user["headers"],
        )
        assert response.status_code == 201
        data = response.json()
        assert len(data["tickets"]) == 2
        assert data["payment_status"] == "completed"

    async def test_purchase_exceeds_limit(
        self, client: AsyncClient, test_user, published_event_with_tickets
    ):
        evt = published_event_with_tickets
        response = await client.post(
            "/api/v1/tickets/purchase",
            json={
                "event_id": str(evt["event"].event_id),
                "items": [
                    {
                        "ticket_type_id": str(evt["vip"].ticket_type_id),
                        "quantity": 3,  # max_per_user is 2
                    }
                ],
            },
            headers=test_user["headers"],
        )
        assert response.status_code == 400

    async def test_get_my_tickets(
        self, client: AsyncClient, test_user, published_event_with_tickets
    ):
        evt = published_event_with_tickets
        # Purchase first
        await client.post(
            "/api/v1/tickets/purchase",
            json={
                "event_id": str(evt["event"].event_id),
                "items": [
                    {
                        "ticket_type_id": str(evt["general"].ticket_type_id),
                        "quantity": 1,
                    }
                ],
            },
            headers=test_user["headers"],
        )

        response = await client.get(
            "/api/v1/tickets/my-tickets",
            headers=test_user["headers"],
        )
        assert response.status_code == 200
        assert len(response.json()) >= 1

    async def test_purchase_sends_confirmation_email(
        self, client: AsyncClient, test_user, published_event_with_tickets
    ):
        from unittest.mock import patch

        evt = published_event_with_tickets
        with patch("app.services.ticketing_service.send_email", return_value=True) as mock_send:
            response = await client.post(
                "/api/v1/tickets/purchase",
                json={
                    "event_id": str(evt["event"].event_id),
                    "items": [
                        {
                            "ticket_type_id": str(evt["general"].ticket_type_id),
                            "quantity": 2,
                        }
                    ],
                },
                headers=test_user["headers"],
            )
        assert response.status_code == 201
        mock_send.assert_called_once()
        call_args = mock_send.call_args[0]
        assert call_args[0] == test_user["user"].email
        assert "Ticket Test Event" in call_args[1] or "Ticket Test Event" in call_args[2]

    async def test_purchase_rejects_priced_ticket(
        self, client: AsyncClient, test_user, published_event_with_tickets
    ):
        """VIP ticket type (price=100.00) must not be purchasable via the free endpoint."""
        evt = published_event_with_tickets
        response = await client.post(
            "/api/v1/tickets/purchase",
            json={
                "event_id": str(evt["event"].event_id),
                "items": [{"ticket_type_id": str(evt["vip"].ticket_type_id), "quantity": 1}],
            },
            headers=test_user["headers"],
        )
        assert response.status_code == 400
        assert "initiate-payment" in response.text.lower() or "payment" in response.text.lower()
