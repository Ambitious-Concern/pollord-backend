"""Organization members see the organization's work, not just their own.

Events and elections carry no org_id — they belong to the user who created
them — so listing scoped to `created_by == current_user`. A newly added
member had created nothing, so their dashboard was empty and they appeared
to be in an organization of one.

The risk in fixing this is the opposite failure: leaking one organization's
data to another. Every test below that proves a member CAN see something has
a sibling proving an outsider CANNOT.
"""
import json
from datetime import date, datetime, time, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, generate_secure_token, hash_password
from app.models.election import Election
from app.models.event import Event, TicketType
from app.models.organization import Organization, OrganizationMember
from app.models.ticket import Ticket, TicketPurchase


async def _make_user(db: AsyncSession, email: str, role_name: str = "Event Organizer") -> dict:
    from sqlalchemy import select

    from app.models.user import Role, User, UserRole

    user = User(
        email=email,
        password_hash=hash_password("Passw0rd!"),
        full_name=email.split("@")[0].title(),
        email_verified=True,
        account_status="active",
    )
    db.add(user)
    await db.flush()

    for name in ("Event Organizer", "Election Administrator"):
        result = await db.execute(select(Role).where(Role.role_name == name))
        role = result.scalar_one_or_none()
        if not role:
            role = Role(role_name=name, permissions={})
            db.add(role)
            await db.flush()
        db.add(UserRole(user_id=user.user_id, role_id=role.role_id))
    await db.flush()

    token = create_access_token(str(user.user_id))
    return {"user": user, "headers": {"Authorization": f"Bearer {token}"}}


async def _make_org(db: AsyncSession, name: str, owner) -> Organization:
    org = Organization(name=name, owner_id=owner.user_id)
    db.add(org)
    await db.flush()
    db.add(
        OrganizationMember(org_id=org.org_id, user_id=owner.user_id, role="owner")
    )
    await db.flush()
    return org


async def _add_member(db: AsyncSession, org: Organization, user, role: str) -> None:
    db.add(OrganizationMember(org_id=org.org_id, user_id=user.user_id, role=role))
    await db.flush()


async def _make_event(db: AsyncSession, creator, title: str) -> Event:
    event = Event(
        title=title,
        event_date=date(2026, 12, 1),
        event_time=time(19, 0),
        location="Accra",
        status="published",
        created_by=creator.user_id,
    )
    db.add(event)
    await db.flush()
    return event


async def _make_election(db: AsyncSession, creator, title: str) -> Election:
    # draft: the API refuses edits once voting has started, and the
    # management tests below need to reach the authorization check rather
    # than bounce off that unrelated guard.
    now = datetime.now(timezone.utc)
    election = Election(
        title=title,
        election_type="single_choice",
        start_datetime=now,
        end_datetime=now + timedelta(days=1),
        status="draft",
        created_by=creator.user_id,
    )
    db.add(election)
    await db.flush()
    return election


@pytest_asyncio.fixture
async def team(db_session: AsyncSession) -> dict:
    """Acme: an owner, a managing member, and a view-only member.
    Rival: an unrelated organization with its own event and election."""
    owner = await _make_user(db_session, "owner@acme.test")
    editor = await _make_user(db_session, "editor@acme.test")
    viewer = await _make_user(db_session, "viewer@acme.test")
    outsider = await _make_user(db_session, "outsider@rival.test")

    acme = await _make_org(db_session, "Acme", owner["user"])
    await _add_member(db_session, acme, editor["user"], "editor")
    await _add_member(db_session, acme, viewer["user"], "member")

    rival = await _make_org(db_session, "Rival", outsider["user"])

    acme_event = await _make_event(db_session, owner["user"], "Acme Gala")
    acme_election = await _make_election(db_session, owner["user"], "Acme Vote")
    rival_event = await _make_event(db_session, outsider["user"], "Rival Gala")
    await _make_election(db_session, outsider["user"], "Rival Vote")

    return {
        "owner": owner,
        "editor": editor,
        "viewer": viewer,
        "outsider": outsider,
        "acme": acme,
        "acme_event": acme_event,
        "acme_election": acme_election,
        "rival_event": rival_event,
    }


async def _titles(client: AsyncClient, path: str, who: dict) -> list[str]:
    response = await client.get(path, headers=who["headers"])
    assert response.status_code == 200, response.text
    return [item["title"] for item in response.json()]


@pytest.mark.asyncio
class TestEventVisibility:
    async def test_member_sees_events_they_did_not_create(
        self, client: AsyncClient, team
    ):
        """The reported bug: an added member saw an empty dashboard."""
        assert "Acme Gala" in await _titles(client, "/api/v1/events", team["editor"])

    async def test_view_only_member_also_sees_them(self, client: AsyncClient, team):
        assert "Acme Gala" in await _titles(client, "/api/v1/events", team["viewer"])

    async def test_owner_still_sees_their_own(self, client: AsyncClient, team):
        assert "Acme Gala" in await _titles(client, "/api/v1/events", team["owner"])

    async def test_other_organizations_are_not_leaked(
        self, client: AsyncClient, team
    ):
        """The failure mode this fix could easily introduce."""
        acme_titles = await _titles(client, "/api/v1/events", team["editor"])
        assert "Rival Gala" not in acme_titles

        rival_titles = await _titles(client, "/api/v1/events", team["outsider"])
        assert "Acme Gala" not in rival_titles
        assert "Rival Gala" in rival_titles

    async def test_user_with_no_organization_still_sees_own_events(
        self, client: AsyncClient, db_session, team
    ):
        """get_teammate_ids must never return empty and blank someone out."""
        loner = await _make_user(db_session, "loner@nowhere.test")
        await _make_event(db_session, loner["user"], "Solo Show")

        titles = await _titles(client, "/api/v1/events", loner)
        assert titles == ["Solo Show"]


@pytest.mark.asyncio
class TestElectionVisibility:
    async def test_member_sees_team_elections(self, client: AsyncClient, team):
        assert "Acme Vote" in await _titles(client, "/api/v1/elections", team["editor"])

    async def test_other_organizations_are_not_leaked(self, client: AsyncClient, team):
        assert "Rival Vote" not in await _titles(
            client, "/api/v1/elections", team["editor"]
        )


@pytest.mark.asyncio
class TestManagementAccess:
    async def test_managing_member_can_open_a_teammates_event(
        self, client: AsyncClient, team
    ):
        """Editing goes through the ownership check, not the listing query."""
        response = await client.put(
            f"/api/v1/events/{team['acme_event'].event_id}",
            json={"location": "Kumasi"},
            headers=team["editor"]["headers"],
        )
        assert response.status_code == 200
        assert response.json()["location"] == "Kumasi"

    async def test_view_only_member_cannot_edit(self, client: AsyncClient, team):
        """"member" is the read-only role — seeing the work isn't managing it."""
        response = await client.put(
            f"/api/v1/events/{team['acme_event'].event_id}",
            json={"location": "Kumasi"},
            headers=team["viewer"]["headers"],
        )
        assert response.status_code == 403

    async def test_outsider_cannot_edit(self, client: AsyncClient, team):
        response = await client.put(
            f"/api/v1/events/{team['acme_event'].event_id}",
            json={"location": "Kumasi"},
            headers=team["outsider"]["headers"],
        )
        assert response.status_code == 403

    async def test_managing_member_can_edit_a_teammates_election(
        self, client: AsyncClient, team
    ):
        response = await client.put(
            f"/api/v1/elections/{team['acme_election'].election_id}",
            json={"title": "Acme Vote 2027"},
            headers=team["editor"]["headers"],
        )
        assert response.status_code == 200

    async def test_outsider_cannot_edit_election(self, client: AsyncClient, team):
        response = await client.put(
            f"/api/v1/elections/{team['acme_election'].election_id}",
            json={"title": "Hijacked"},
            headers=team["outsider"]["headers"],
        )
        assert response.status_code == 403


@pytest.mark.asyncio
class TestTicketSalesVisibility:
    async def test_member_sees_team_ticket_sales(
        self, client: AsyncClient, db_session, team
    ):
        event = team["acme_event"]
        tt = TicketType(
            event_id=event.event_id,
            type_name="GA",
            price=50,
            quantity_available=10,
        )
        db_session.add(tt)
        await db_session.flush()

        purchase = TicketPurchase(
            guest_name="Buyer",
            guest_email="buyer@example.com",
            event_id=event.event_id,
            total_amount=50,
            payment_status="completed",
        )
        db_session.add(purchase)
        await db_session.flush()

        code = generate_secure_token(16)
        db_session.add(
            Ticket(
                ticket_code=code,
                event_id=event.event_id,
                ticket_type_id=tt.ticket_type_id,
                guest_name="Buyer",
                guest_email="buyer@example.com",
                purchase_id=purchase.purchase_id,
                qr_code_data=json.dumps({"ticket_code": code}),
            )
        )
        await db_session.flush()

        response = await client.get(
            "/api/v1/tickets/sales", headers=team["editor"]["headers"]
        )
        assert response.status_code == 200
        assert [s["attendee_email"] for s in response.json()] == ["buyer@example.com"]

    async def test_outsider_sees_no_sales_from_another_org(
        self, client: AsyncClient, team
    ):
        response = await client.get(
            "/api/v1/tickets/sales", headers=team["outsider"]["headers"]
        )
        assert response.status_code == 200
        assert response.json() == []
