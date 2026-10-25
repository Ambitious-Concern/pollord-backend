from datetime import date, datetime, time, timedelta, timezone

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.event import Event


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


async def _make_event(db_session: AsyncSession, admin_user, *, future=True) -> Event:
    event_date = date.today() + timedelta(days=30) if future else date.today() - timedelta(days=30)
    event = Event(
        title="RSVP Test Event",
        event_date=event_date,
        event_time=time(18, 0),
        location="Accra",
        status="published",
        created_by=admin_user["user"].user_id,
    )
    db_session.add(event)
    await db_session.flush()
    return event


class TestCreateEventInvitation:
    async def test_organizer_creates_invitation(self, client: AsyncClient, db_session, admin_user):
        event = await _make_event(db_session, admin_user)
        res = await client.post(
            f"/api/v1/events/{event.event_id}/invitations",
            json={"email": "guest@example.com", "name": "Guest One"},
            headers=admin_user["headers"],
        )
        assert res.status_code == 201, res.text
        body = res.json()
        assert body["email"] == "guest@example.com"
        assert body["name"] == "Guest One"
        assert body["status"] == "pending"

    async def test_duplicate_pending_invite_rejected(self, client: AsyncClient, db_session, admin_user):
        event = await _make_event(db_session, admin_user)
        payload = {"email": "dupe@example.com"}
        first = await client.post(
            f"/api/v1/events/{event.event_id}/invitations", json=payload, headers=admin_user["headers"]
        )
        assert first.status_code == 201
        second = await client.post(
            f"/api/v1/events/{event.event_id}/invitations", json=payload, headers=admin_user["headers"]
        )
        assert second.status_code == 409

    async def test_non_owner_rejected(self, client: AsyncClient, db_session, admin_user):
        event = await _make_event(db_session, admin_user)
        outsider = await _make_organizer(db_session, "outsider@example.com")
        res = await client.post(
            f"/api/v1/events/{event.event_id}/invitations",
            json={"email": "guest@example.com"},
            headers=outsider["headers"],
        )
        assert res.status_code == 403

    async def test_list_invitations_for_event(self, client: AsyncClient, db_session, admin_user):
        event = await _make_event(db_session, admin_user)
        await client.post(
            f"/api/v1/events/{event.event_id}/invitations",
            json={"email": "one@example.com"},
            headers=admin_user["headers"],
        )
        res = await client.get(
            f"/api/v1/events/{event.event_id}/invitations", headers=admin_user["headers"]
        )
        assert res.status_code == 200
        assert len(res.json()) == 1


class TestPublicRSVP:
    async def test_lookup_by_token_and_rsvp_accept(self, client: AsyncClient, db_session, admin_user):
        event = await _make_event(db_session, admin_user)
        create_res = await client.post(
            f"/api/v1/events/{event.event_id}/invitations",
            json={"email": "rsvp@example.com", "name": "RSVP Guest"},
            headers=admin_user["headers"],
        )
        assert create_res.status_code == 201

        from sqlalchemy import select

        from app.models.event_invitation import EventInvitation

        result = await db_session.execute(
            select(EventInvitation).where(EventInvitation.email == "rsvp@example.com")
        )
        inv = result.scalar_one()

        lookup = await client.get(f"/api/v1/invitations/{inv.token}")
        assert lookup.status_code == 200
        body = lookup.json()
        assert body["event_title"] == "RSVP Test Event"
        assert body["status"] == "pending"

        rsvp = await client.post(f"/api/v1/invitations/{inv.token}/rsvp", json={"response": "accepted"})
        assert rsvp.status_code == 200
        assert rsvp.json()["status"] == "accepted"

        # Responding again is rejected — the invite is no longer pending.
        again = await client.post(f"/api/v1/invitations/{inv.token}/rsvp", json={"response": "declined"})
        assert again.status_code == 410

    async def test_unknown_token_404(self, client: AsyncClient):
        res = await client.get("/api/v1/invitations/not-a-real-token")
        assert res.status_code == 404

    async def test_accepting_grants_a_free_ticket(self, client: AsyncClient, db_session, admin_user):
        """Accepting an RSVP should reserve a real spot — a free ticket under
        the event's "Free Registration" type — not just flip a status flag,
        so the guest shows up in Attendees/check-in and ticket-holder
        broadcasts."""
        event = await _make_event(db_session, admin_user)
        create_res = await client.post(
            f"/api/v1/events/{event.event_id}/invitations",
            json={"email": "ticketed@example.com", "name": "Ticketed Guest"},
            headers=admin_user["headers"],
        )
        assert create_res.status_code == 201

        from sqlalchemy import select

        from app.models.event_invitation import EventInvitation
        from app.models.ticket import Ticket

        result = await db_session.execute(
            select(EventInvitation).where(EventInvitation.email == "ticketed@example.com")
        )
        inv = result.scalar_one()

        rsvp = await client.post(f"/api/v1/invitations/{inv.token}/rsvp", json={"response": "accepted"})
        assert rsvp.status_code == 200

        ticket_result = await db_session.execute(
            select(Ticket).where(Ticket.event_id == event.event_id, Ticket.guest_email == "ticketed@example.com")
        )
        ticket = ticket_result.scalar_one()
        assert ticket.origin == "rsvp_invite"
        assert ticket.guest_name == "Ticketed Guest"

    async def test_declining_does_not_grant_a_ticket(self, client: AsyncClient, db_session, admin_user):
        event = await _make_event(db_session, admin_user)
        create_res = await client.post(
            f"/api/v1/events/{event.event_id}/invitations",
            json={"email": "declined@example.com"},
            headers=admin_user["headers"],
        )
        assert create_res.status_code == 201

        from sqlalchemy import select

        from app.models.event_invitation import EventInvitation
        from app.models.ticket import Ticket

        result = await db_session.execute(
            select(EventInvitation).where(EventInvitation.email == "declined@example.com")
        )
        inv = result.scalar_one()

        rsvp = await client.post(f"/api/v1/invitations/{inv.token}/rsvp", json={"response": "declined"})
        assert rsvp.status_code == 200

        ticket_result = await db_session.execute(
            select(Ticket).where(Ticket.event_id == event.event_id, Ticket.guest_email == "declined@example.com")
        )
        assert ticket_result.scalar_one_or_none() is None

    async def test_expired_invitation_rejected_on_rsvp(self, client: AsyncClient, db_session, admin_user):
        event = await _make_event(db_session, admin_user, future=False)
        create_res = await client.post(
            f"/api/v1/events/{event.event_id}/invitations",
            json={"email": "late@example.com"},
            headers=admin_user["headers"],
        )
        assert create_res.status_code == 201

        from sqlalchemy import select

        from app.models.event_invitation import EventInvitation

        result = await db_session.execute(
            select(EventInvitation).where(EventInvitation.email == "late@example.com")
        )
        inv = result.scalar_one()
        # Force it into the past — the service floors expiry at "now + 1h" for
        # brand-new invites, so a past event alone isn't enough to test expiry.
        inv.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
        await db_session.flush()

        rsvp = await client.post(f"/api/v1/invitations/{inv.token}/rsvp", json={"response": "accepted"})
        assert rsvp.status_code == 410
