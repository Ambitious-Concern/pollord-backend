from datetime import date, time

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.event import Event, TicketType


async def _make_organizer(db_session: AsyncSession, email: str) -> dict:
    from sqlalchemy import select

    from app.models.user import Role, User, UserRole

    user = User(
        email=email, password_hash=hash_password("Passw0rd!"),
        full_name="Organizer", email_verified=True, account_status="active",
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


async def _make_event(db_session: AsyncSession, admin_user, title: str) -> Event:
    event = Event(
        title=title, event_date=date(2026, 9, 1), event_time=time(19, 0),
        location="Test Venue", status="published", created_by=admin_user["user"].user_id,
    )
    db_session.add(event)
    await db_session.flush()
    return event


class TestCrossEventTicketTypes:
    async def test_lists_ticket_types_across_all_owned_events(
        self, client: AsyncClient, db_session, admin_user
    ):
        event_a = await _make_event(db_session, admin_user, "Event A")
        event_b = await _make_event(db_session, admin_user, "Event B")
        db_session.add(TicketType(event_id=event_a.event_id, type_name="Regular", price=25, quantity_available=100))
        db_session.add(TicketType(event_id=event_b.event_id, type_name="VIP", price=80, quantity_available=50))
        await db_session.flush()

        res = await client.get("/api/v1/ticket-types", headers=admin_user["headers"])
        assert res.status_code == 200, res.text
        by_name = {tt["type_name"]: tt["event_title"] for tt in res.json()}
        assert by_name["Regular"] == "Event A"
        assert by_name["VIP"] == "Event B"

    async def test_outsider_sees_none_of_it(self, client: AsyncClient, db_session, admin_user):
        event = await _make_event(db_session, admin_user, "Private Event")
        db_session.add(TicketType(event_id=event.event_id, type_name="Regular", price=25, quantity_available=100))
        await db_session.flush()

        outsider = await _make_organizer(db_session, "ticket-types-outsider@example.com")
        res = await client.get("/api/v1/ticket-types", headers=outsider["headers"])
        assert res.status_code == 200
        assert res.json() == []

    async def test_empty_when_no_ticket_types_exist(self, client: AsyncClient, admin_user):
        res = await client.get("/api/v1/ticket-types", headers=admin_user["headers"])
        assert res.status_code == 200
        assert res.json() == []
