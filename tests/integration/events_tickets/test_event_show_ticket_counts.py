"""Per-event toggle for showing remaining-ticket counts to buyers.

Sold-out state is deliberately not covered by this flag — a buyer needs to
know a tier is unavailable; that isn't a sales figure.
"""
from datetime import date, time

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.event import Event


@pytest.fixture
async def event(db_session: AsyncSession, admin_user) -> Event:
    e = Event(
        title="Toggle Test Event",
        event_date=date(2026, 12, 5),
        event_time=time(20, 0),
        location="Accra",
        status="published",
        created_by=admin_user["user"].user_id,
    )
    db_session.add(e)
    await db_session.flush()
    return e


@pytest.mark.asyncio
class TestShowTicketCounts:
    async def test_defaults_to_true(self, client: AsyncClient, admin_user, event):
        """Existing events must keep behaving exactly as they do now."""
        response = await client.get(
            f"/api/v1/events/{event.event_id}", headers=admin_user["headers"]
        )
        assert response.status_code == 200
        assert response.json()["show_ticket_counts"] is True

    async def test_organizer_can_turn_it_off(
        self, client: AsyncClient, db_session, admin_user, event
    ):
        """The False case specifically: a generic update that skips falsy
        values would silently drop this."""
        response = await client.put(
            f"/api/v1/events/{event.event_id}",
            json={"show_ticket_counts": False},
            headers=admin_user["headers"],
        )
        assert response.status_code == 200
        assert response.json()["show_ticket_counts"] is False

        await db_session.refresh(event)
        assert event.show_ticket_counts is False

    async def test_can_turn_it_back_on(
        self, client: AsyncClient, admin_user, event
    ):
        await client.put(
            f"/api/v1/events/{event.event_id}",
            json={"show_ticket_counts": False},
            headers=admin_user["headers"],
        )
        response = await client.put(
            f"/api/v1/events/{event.event_id}",
            json={"show_ticket_counts": True},
            headers=admin_user["headers"],
        )
        assert response.json()["show_ticket_counts"] is True

    async def test_other_fields_survive_the_toggle(
        self, client: AsyncClient, admin_user, event
    ):
        """exclude_unset means a toggle-only payload must not blank the rest."""
        response = await client.put(
            f"/api/v1/events/{event.event_id}",
            json={"show_ticket_counts": False},
            headers=admin_user["headers"],
        )
        body = response.json()
        assert body["title"] == "Toggle Test Event"
        assert body["location"] == "Accra"

    async def test_visible_on_the_public_event_payload(
        self, client: AsyncClient, admin_user, event
    ):
        """The public page needs the flag to know whether to render counts."""
        await client.put(
            f"/api/v1/events/{event.event_id}",
            json={"show_ticket_counts": False},
            headers=admin_user["headers"],
        )
        response = await client.get(f"/api/v1/events/{event.event_id}")
        assert response.status_code == 200
        assert response.json()["show_ticket_counts"] is False

    async def test_non_owner_cannot_toggle(
        self, client: AsyncClient, test_user, event
    ):
        response = await client.put(
            f"/api/v1/events/{event.event_id}",
            json={"show_ticket_counts": False},
            headers=test_user["headers"],
        )
        assert response.status_code in (401, 403)
