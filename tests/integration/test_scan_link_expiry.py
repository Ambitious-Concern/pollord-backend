"""Check-in links must outlive the event they belong to.

The expiry used to be midnight UTC on the event's own date, which killed the
link twice over: an event running past midnight lost its scanner mid-event
(Ghana is UTC+0, so local midnight is the cutoff), and any past event's link
was permanently dead — no late check-ins, no fixing the register the next
morning. Organizers saw "Invalid or expired check-in link" for events that,
to them, had barely finished.
"""
from datetime import date, datetime, time, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.endpoints.events import SCAN_LINK_GRACE_DAYS, scan_link_expiry
from app.models.event import Event


@pytest_asyncio.fixture
async def event_factory(db_session: AsyncSession, admin_user):
    async def make(event_date: date, status: str = "published") -> Event:
        event = Event(
            title=f"Event {event_date}",
            event_date=event_date,
            event_time=time(20, 0),
            location="Accra",
            status=status,
            created_by=admin_user["user"].user_id,
        )
        db_session.add(event)
        await db_session.flush()
        return event

    return make


def _today() -> date:
    return datetime.now(timezone.utc).date()


class TestExpiryPolicy:
    def test_grace_window_extends_past_the_event(self):
        expiry = scan_link_expiry(date(2026, 8, 12))
        # End of the 7th day after the event, i.e. midnight starting day 8.
        assert expiry == datetime(2026, 8, 20, tzinfo=timezone.utc)

    def test_an_event_running_past_midnight_keeps_its_scanner(self):
        """The old policy expired at midnight UTC on the event date, cutting
        off a 20:00-02:00 event exactly when guests were still arriving."""
        event_day = date(2026, 8, 12)
        just_after_midnight = datetime(2026, 8, 13, 1, 30, tzinfo=timezone.utc)
        assert scan_link_expiry(event_day) > just_after_midnight

    def test_yesterdays_event_is_still_scannable(self):
        assert scan_link_expiry(_today() - timedelta(days=1)) > datetime.now(timezone.utc)

    def test_links_do_eventually_expire(self):
        """The grace window is a backstop, not "forever" — a leaked link
        shouldn't work indefinitely."""
        long_past = _today() - timedelta(days=SCAN_LINK_GRACE_DAYS + 2)
        assert scan_link_expiry(long_past) < datetime.now(timezone.utc)


@pytest.mark.asyncio
class TestScanLinkEndToEnd:
    async def _mint(self, client: AsyncClient, admin_user, event) -> str:
        response = await client.get(
            f"/api/v1/events/{event.event_id}/scan-token",
            headers=admin_user["headers"],
        )
        assert response.status_code == 200, response.text
        return response.json()["scan_token"]

    async def test_future_event_link_resolves(
        self, client: AsyncClient, admin_user, event_factory
    ):
        event = await event_factory(_today() + timedelta(days=5))
        token = await self._mint(client, admin_user, event)

        response = await client.get(f"/api/v1/tickets/public/scan-info/{token}")
        assert response.status_code == 200
        assert response.json()["event_id"] == str(event.event_id)

    async def test_todays_event_link_resolves(
        self, client: AsyncClient, admin_user, event_factory
    ):
        event = await event_factory(_today())
        token = await self._mint(client, admin_user, event)
        response = await client.get(f"/api/v1/tickets/public/scan-info/{token}")
        assert response.status_code == 200

    async def test_yesterdays_event_link_still_resolves(
        self, client: AsyncClient, admin_user, event_factory
    ):
        """This is the reported bug: it used to 404 as expired."""
        event = await event_factory(_today() - timedelta(days=1))
        token = await self._mint(client, admin_user, event)

        response = await client.get(f"/api/v1/tickets/public/scan-info/{token}")
        assert response.status_code == 200, (
            "a link for an event that finished yesterday must still work — "
            "late check-ins and next-morning corrections depend on it"
        )

    async def test_link_within_the_grace_window_resolves(
        self, client: AsyncClient, admin_user, event_factory
    ):
        event = await event_factory(_today() - timedelta(days=SCAN_LINK_GRACE_DAYS - 1))
        token = await self._mint(client, admin_user, event)
        response = await client.get(f"/api/v1/tickets/public/scan-info/{token}")
        assert response.status_code == 200

    async def test_link_past_the_grace_window_is_rejected(
        self, client: AsyncClient, admin_user, event_factory
    ):
        event = await event_factory(_today() - timedelta(days=SCAN_LINK_GRACE_DAYS + 3))
        token = await self._mint(client, admin_user, event)

        response = await client.get(f"/api/v1/tickets/public/scan-info/{token}")
        assert response.status_code == 404

    async def test_reported_expiry_matches_the_token(
        self, client: AsyncClient, admin_user, event_factory
    ):
        """The UI shows expires_at, so it must not disagree with the token."""
        event = await event_factory(_today() + timedelta(days=2))
        response = await client.get(
            f"/api/v1/events/{event.event_id}/scan-token",
            headers=admin_user["headers"],
        )
        body = response.json()

        info = await client.get(
            f"/api/v1/tickets/public/scan-info/{body['scan_token']}"
        )
        assert info.status_code == 200
        assert info.json()["expires_at"][:19] == body["expires_at"][:19]
