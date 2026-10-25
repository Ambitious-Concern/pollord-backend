from datetime import date, time
from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.event import Event, TicketType
from app.models.ticket import Ticket, TicketPurchase


async def _make_organizer(db_session: AsyncSession, email: str) -> dict:
    """A real organizer role (not System Administrator, which bypasses
    ownership checks entirely) so scope-ownership rejection is actually
    exercised rather than short-circuited by the admin bypass."""
    from sqlalchemy import select

    from app.models.user import Role, User, UserRole

    user = User(
        email=email, password_hash=hash_password("Passw0rd!"),
        full_name="Outsider Organizer", email_verified=True, account_status="active",
    )
    db_session.add(user)
    await db_session.flush()

    result = await db_session.execute(select(Role).where(Role.role_name == "Event Organizer"))
    role = result.scalar_one_or_none()
    if not role:
        role = Role(role_name="Event Organizer", permissions={})
        db_session.add(role)
        await db_session.flush()
    db_session.add(UserRole(user_id=user.user_id, role_id=role.role_id))
    await db_session.flush()

    token = create_access_token(str(user.user_id))
    return {"user": user, "headers": {"Authorization": f"Bearer {token}"}}


async def _make_event(db_session: AsyncSession, admin_user) -> Event:
    event = Event(
        title="Broadcast Test Event",
        event_date=date(2026, 12, 1),
        event_time=time(18, 0),
        location="Accra",
        status="published",
        created_by=admin_user["user"].user_id,
    )
    db_session.add(event)
    await db_session.flush()
    return event


async def _make_ticket(
    db_session: AsyncSession, event: Event, *, guest_email: str, ticket_status: str = "valid"
) -> Ticket:
    ticket_type = TicketType(
        event_id=event.event_id, type_name="Regular", price=Decimal("10.00"), quantity_available=50,
    )
    db_session.add(ticket_type)
    await db_session.flush()
    purchase = TicketPurchase(
        event_id=event.event_id, guest_name="Guest", guest_email=guest_email,
        total_amount=Decimal("10.00"), payment_status="completed",
    )
    db_session.add(purchase)
    await db_session.flush()
    ticket = Ticket(
        ticket_code=f"code-{guest_email}",
        event_id=event.event_id,
        ticket_type_id=ticket_type.ticket_type_id,
        guest_name="Guest",
        guest_email=guest_email,
        purchase_id=purchase.purchase_id,
        qr_code_data="{}",
        ticket_status=ticket_status,
    )
    db_session.add(ticket)
    await db_session.flush()
    return ticket


class TestSendBroadcast:
    async def test_sends_to_all_ticket_holders(self, client: AsyncClient, db_session, admin_user):
        event = await _make_event(db_session, admin_user)
        await _make_ticket(db_session, event, guest_email="attendee1@example.com")
        await _make_ticket(db_session, event, guest_email="attendee2@example.com")

        res = await client.post(
            f"/api/v1/events/{event.event_id}/broadcast",
            json={"subject": "See you soon!", "body": "Doors open at 6pm."},
            headers=admin_user["headers"],
        )
        assert res.status_code == 201, res.text
        body = res.json()
        assert body["recipient_count"] == 2
        assert body["status"] == "sending"

    async def test_excludes_cancelled_tickets(self, client: AsyncClient, db_session, admin_user):
        event = await _make_event(db_session, admin_user)
        await _make_ticket(db_session, event, guest_email="valid@example.com")
        await _make_ticket(db_session, event, guest_email="cancelled@example.com", ticket_status="cancelled")

        res = await client.post(
            f"/api/v1/events/{event.event_id}/broadcast",
            json={"subject": "Update", "body": "Body"},
            headers=admin_user["headers"],
        )
        assert res.status_code == 201
        assert res.json()["recipient_count"] == 1

    async def test_excludes_walkin_synthetic_emails(self, client: AsyncClient, db_session, admin_user):
        event = await _make_event(db_session, admin_user)
        await _make_ticket(db_session, event, guest_email="real@example.com")
        await _make_ticket(db_session, event, guest_email="walkin-abcd@front-desk.pollord.internal")

        res = await client.post(
            f"/api/v1/events/{event.event_id}/broadcast",
            json={"subject": "Update", "body": "Body"},
            headers=admin_user["headers"],
        )
        assert res.status_code == 201
        assert res.json()["recipient_count"] == 1

    async def test_no_ticket_holders_rejected(self, client: AsyncClient, db_session, admin_user):
        event = await _make_event(db_session, admin_user)
        res = await client.post(
            f"/api/v1/events/{event.event_id}/broadcast",
            json={"subject": "Update", "body": "Body"},
            headers=admin_user["headers"],
        )
        assert res.status_code == 400

    async def test_non_owner_rejected(self, client: AsyncClient, db_session, admin_user):
        event = await _make_event(db_session, admin_user)
        await _make_ticket(db_session, event, guest_email="attendee@example.com")
        outsider = await _make_organizer(db_session, "outsider@example.com")

        res = await client.post(
            f"/api/v1/events/{event.event_id}/broadcast",
            json={"subject": "Update", "body": "Body"},
            headers=outsider["headers"],
        )
        assert res.status_code == 403

    async def test_list_broadcasts_for_event(self, client: AsyncClient, db_session, admin_user):
        event = await _make_event(db_session, admin_user)
        await _make_ticket(db_session, event, guest_email="attendee@example.com")
        await client.post(
            f"/api/v1/events/{event.event_id}/broadcast",
            json={"subject": "First", "body": "Body"},
            headers=admin_user["headers"],
        )

        res = await client.get(
            f"/api/v1/events/{event.event_id}/broadcasts", headers=admin_user["headers"]
        )
        assert res.status_code == 200
        assert len(res.json()) == 1
        assert res.json()[0]["subject"] == "First"
