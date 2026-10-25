"""Per-event switch for ticket check-in.

Turning scanning off must actually revoke access, not just hide buttons —
the point is being able to kill a leaked check-in link mid-event. Both the
public link scanner and the signed-in organizer scanner have to honour it,
or the switch is bypassable by using the other route.
"""
import json
from datetime import date, datetime, time, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import generate_secure_token
from app.models.event import Event, TicketType
from app.models.ticket import Ticket, TicketPurchase


@pytest_asyncio.fixture
async def scannable(db_session: AsyncSession, admin_user) -> dict:
    """A live event today with one valid ticket."""
    event = Event(
        title="Scan Toggle Event",
        event_date=datetime.now(timezone.utc).date(),
        event_time=time(20, 0),
        location="Accra",
        status="published",
        created_by=admin_user["user"].user_id,
    )
    db_session.add(event)
    await db_session.flush()

    tt = TicketType(
        event_id=event.event_id,
        type_name="GA",
        price=0,
        quantity_available=10,
    )
    db_session.add(tt)
    await db_session.flush()

    purchase = TicketPurchase(
        guest_name="Guest",
        guest_email="guest@example.com",
        event_id=event.event_id,
        total_amount=0,
        payment_status="completed",
    )
    db_session.add(purchase)
    await db_session.flush()

    code = generate_secure_token(16)
    ticket = Ticket(
        ticket_code=code,
        event_id=event.event_id,
        ticket_type_id=tt.ticket_type_id,
        guest_name="Guest",
        guest_email="guest@example.com",
        purchase_id=purchase.purchase_id,
        qr_code_data=json.dumps({"ticket_code": code}),
    )
    db_session.add(ticket)
    await db_session.flush()

    return {"event": event, "ticket": ticket, "code": code}


async def _scan_token(client: AsyncClient, admin_user, event) -> str:
    response = await client.get(
        f"/api/v1/events/{event.event_id}/scan-token", headers=admin_user["headers"]
    )
    assert response.status_code == 200
    return response.json()["scan_token"]


async def _set_scanning(client: AsyncClient, admin_user, event, enabled: bool):
    return await client.put(
        f"/api/v1/events/{event.event_id}",
        json={"scan_enabled": enabled},
        headers=admin_user["headers"],
    )


@pytest.mark.asyncio
class TestToggle:
    async def test_defaults_to_on(self, client: AsyncClient, admin_user, scannable):
        """Existing events must keep scanning; defaulting off would silently
        disable check-in for live events the moment this deploys."""
        response = await client.get(f"/api/v1/events/{scannable['event'].event_id}")
        assert response.status_code == 200
        assert response.json()["scan_enabled"] is True

    async def test_organizer_can_turn_it_off_and_on(
        self, client: AsyncClient, db_session, admin_user, scannable
    ):
        off = await _set_scanning(client, admin_user, scannable["event"], False)
        assert off.status_code == 200
        assert off.json()["scan_enabled"] is False

        on = await _set_scanning(client, admin_user, scannable["event"], True)
        assert on.json()["scan_enabled"] is True

    async def test_toggle_does_not_disturb_other_fields(
        self, client: AsyncClient, admin_user, scannable
    ):
        response = await _set_scanning(client, admin_user, scannable["event"], False)
        assert response.json()["title"] == "Scan Toggle Event"
        assert response.json()["location"] == "Accra"


@pytest.mark.asyncio
class TestEnforcement:
    async def test_public_link_works_while_scanning_is_on(
        self, client: AsyncClient, admin_user, scannable
    ):
        token = await _scan_token(client, admin_user, scannable["event"])
        info = await client.get(f"/api/v1/tickets/public/scan-info/{token}")
        assert info.status_code == 200

    async def test_turning_it_off_kills_an_outstanding_link(
        self, client: AsyncClient, admin_user, scannable
    ):
        """The link is minted first, then scanning is switched off — this is
        the revoke-a-leaked-link case."""
        token = await _scan_token(client, admin_user, scannable["event"])
        assert (
            await client.get(f"/api/v1/tickets/public/scan-info/{token}")
        ).status_code == 200

        await _set_scanning(client, admin_user, scannable["event"], False)

        blocked = await client.get(f"/api/v1/tickets/public/scan-info/{token}")
        assert blocked.status_code == 403
        # Distinct from "invalid or expired" so the organizer knows it's their
        # own switch and not a broken link.
        assert "turned off" in blocked.json()["detail"]

    async def test_public_validate_refuses_while_off(
        self, client: AsyncClient, admin_user, scannable
    ):
        token = await _scan_token(client, admin_user, scannable["event"])
        await _set_scanning(client, admin_user, scannable["event"], False)

        response = await client.post(
            "/api/v1/tickets/public/validate",
            json={"scan_token": token, "ticket_code": scannable["code"]},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["valid"] is False
        assert "turned off" in body["message"]

    async def test_authenticated_validate_also_refuses(
        self, client: AsyncClient, admin_user, scannable
    ):
        """The switch must not be bypassable by using the signed-in scanner
        instead of the public link."""
        await _set_scanning(client, admin_user, scannable["event"], False)

        response = await client.post(
            "/api/v1/tickets/validate",
            json={"ticket_code": scannable["code"]},
            headers=admin_user["headers"],
        )
        assert response.status_code == 200
        body = response.json()
        assert body["valid"] is False
        assert "turned off" in body["message"]

    async def test_ticket_is_not_consumed_while_scanning_is_off(
        self, client: AsyncClient, db_session, admin_user, scannable
    ):
        """A refused scan must not silently mark the ticket used, or the
        guest can't be checked in once scanning is switched back on."""
        await _set_scanning(client, admin_user, scannable["event"], False)
        await client.post(
            "/api/v1/tickets/validate",
            json={"ticket_code": scannable["code"]},
            headers=admin_user["headers"],
        )

        await db_session.refresh(scannable["ticket"])
        assert scannable["ticket"].ticket_status == "valid"
        assert scannable["ticket"].used_at is None

    async def test_turning_it_back_on_restores_check_in(
        self, client: AsyncClient, admin_user, scannable
    ):
        await _set_scanning(client, admin_user, scannable["event"], False)
        await _set_scanning(client, admin_user, scannable["event"], True)

        response = await client.post(
            "/api/v1/tickets/validate",
            json={"ticket_code": scannable["code"]},
            headers=admin_user["headers"],
        )
        assert response.json()["valid"] is True


@pytest.mark.asyncio
class TestAdminOverride:
    """A platform admin can shut down check-in when the organizer can't be
    reached — same field, so it takes effect on shared links immediately."""

    async def test_admin_can_turn_scanning_off(
        self, client: AsyncClient, admin_user, scannable
    ):
        token = await _scan_token(client, admin_user, scannable["event"])

        response = await client.patch(
            f"/api/v1/admin/events/{scannable['event'].event_id}/settings",
            json={"scan_enabled": False},
            headers=admin_user["headers"],
        )
        assert response.status_code == 200
        assert response.json()["scan_enabled"] is False

        blocked = await client.get(f"/api/v1/tickets/public/scan-info/{token}")
        assert blocked.status_code == 403

    async def test_admin_settings_leave_the_rest_of_the_event_alone(
        self, client: AsyncClient, admin_user, scannable
    ):
        response = await client.patch(
            f"/api/v1/admin/events/{scannable['event'].event_id}/settings",
            json={"scan_enabled": False},
            headers=admin_user["headers"],
        )
        body = response.json()
        assert body["title"] == "Scan Toggle Event"
        assert body["location"] == "Accra"
        # Untouched field keeps its value rather than being reset.
        assert body["show_ticket_counts"] is True

    async def test_requires_platform_admin(
        self, client: AsyncClient, test_user, scannable
    ):
        response = await client.patch(
            f"/api/v1/admin/events/{scannable['event'].event_id}/settings",
            json={"scan_enabled": False},
            headers=test_user["headers"],
        )
        assert response.status_code == 403

    async def test_writes_an_audit_log(
        self, client: AsyncClient, db_session, admin_user, scannable
    ):
        from sqlalchemy import select

        from app.models.audit_log import AuditLog

        await client.patch(
            f"/api/v1/admin/events/{scannable['event'].event_id}/settings",
            json={"scan_enabled": False},
            headers=admin_user["headers"],
        )
        result = await db_session.execute(
            select(AuditLog).where(AuditLog.action_type == "UPDATE_EVENT_SETTINGS")
        )
        log = result.scalars().first()
        assert log is not None
        assert log.changes == {"scan_enabled": False}
