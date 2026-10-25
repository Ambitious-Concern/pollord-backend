from datetime import date, time, timedelta, timezone, datetime as dt
from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.election import Candidate, Category, Election
from app.models.event import Event, TicketType
from app.models.organization import Organization, OrganizationMember


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


async def _make_org(db_session: AsyncSession, name: str, owner) -> Organization:
    org = Organization(name=name, owner_id=owner.user_id)
    db_session.add(org)
    await db_session.flush()
    db_session.add(OrganizationMember(org_id=org.org_id, user_id=owner.user_id, role="owner"))
    await db_session.flush()
    return org


async def _make_election_with_categories(db_session: AsyncSession, creator, *, vote_price=None) -> Election:
    now = dt.now(timezone.utc)
    election = Election(
        title="Beauty Pageant 2026",
        start_datetime=now + timedelta(days=1),
        end_datetime=now + timedelta(days=2),
        status="draft",
        created_by=creator.user_id,
        vote_price=vote_price,
    )
    db_session.add(election)
    await db_session.flush()
    for name in ("Best Dressed", "Miss Congeniality"):
        db_session.add(Category(election_id=election.election_id, name=name, election_type="single_choice"))
    await db_session.flush()
    return election


async def _make_event_with_tickets_and_categories(db_session: AsyncSession, creator) -> Event:
    event = Event(
        title="Alumni Awards Night",
        event_date=date(2026, 12, 1),
        event_time=time(18, 0),
        location="Accra",
        status="draft",
        created_by=creator.user_id,
        vote_price=500,
    )
    db_session.add(event)
    await db_session.flush()
    db_session.add(TicketType(event_id=event.event_id, type_name="Regular", price=Decimal("150.00"), quantity_available=100))
    db_session.add(Category(event_id=event.event_id, name="Alumnus of the Year", election_type="single_choice"))
    await db_session.flush()
    return event


class TestTemplateCreate:
    async def test_save_election_template_snapshots_categories(self, client: AsyncClient, db_session, admin_user):
        election = await _make_election_with_categories(db_session, admin_user["user"], vote_price=200)
        res = await client.post(
            "/api/v1/templates",
            json={
                "kind": "election",
                "source_id": str(election.election_id),
                "title": "My Pageant Setup",
                "visibility": "private",
            },
            headers=admin_user["headers"],
        )
        assert res.status_code == 201, res.text
        body = res.json()
        assert set(body["categories"]) == {"Best Dressed", "Miss Congeniality"}
        assert body["ticket_tiers"] == []
        assert body["voting_type"] == "paid"
        assert body["vote_price_pesewas"] == 200
        assert body["is_mine"] is True

    async def test_save_event_template_snapshots_tickets_and_categories(self, client: AsyncClient, db_session, admin_user):
        event = await _make_event_with_tickets_and_categories(db_session, admin_user["user"])
        res = await client.post(
            "/api/v1/templates",
            json={"kind": "event", "source_id": str(event.event_id), "title": "Awards Night Setup"},
            headers=admin_user["headers"],
        )
        assert res.status_code == 201, res.text
        body = res.json()
        assert body["ticket_tiers"] == [
            {"name": "Regular", "price": "150.00", "description": "", "available": 100}
        ]
        assert body["categories"] == ["Alumnus of the Year"]
        assert body["voting_type"] == "paid"

    async def test_free_source_has_no_voting_type(self, client: AsyncClient, db_session, admin_user):
        election = await _make_election_with_categories(db_session, admin_user["user"], vote_price=None)
        res = await client.post(
            "/api/v1/templates",
            json={"kind": "election", "source_id": str(election.election_id), "title": "Free Election Setup"},
            headers=admin_user["headers"],
        )
        assert res.status_code == 201, res.text
        assert res.json()["voting_type"] == "free"

    async def test_cannot_save_template_from_others_election(self, client: AsyncClient, db_session, admin_user):
        election = await _make_election_with_categories(db_session, admin_user["user"])
        outsider = await _make_organizer(db_session, "outsider-template@example.com")
        res = await client.post(
            "/api/v1/templates",
            json={"kind": "election", "source_id": str(election.election_id), "title": "Stolen Setup"},
            headers=outsider["headers"],
        )
        assert res.status_code == 403

    async def test_missing_source_404(self, client: AsyncClient, admin_user):
        import uuid
        res = await client.post(
            "/api/v1/templates",
            json={"kind": "election", "source_id": str(uuid.uuid4()), "title": "Nothing"},
            headers=admin_user["headers"],
        )
        assert res.status_code == 404


class TestTemplateVisibility:
    async def test_private_template_not_visible_across_orgs(self, client: AsyncClient, db_session, admin_user):
        election = await _make_election_with_categories(db_session, admin_user["user"])
        await _make_org(db_session, "Acme", admin_user["user"])
        create_res = await client.post(
            "/api/v1/templates",
            json={"kind": "election", "source_id": str(election.election_id), "title": "Acme Private", "visibility": "private"},
            headers=admin_user["headers"],
        )
        assert create_res.status_code == 201

        outsider = await _make_organizer(db_session, "rival-org-user@example.com")
        await _make_org(db_session, "Rival Co", outsider["user"])
        list_res = await client.get("/api/v1/templates", headers=outsider["headers"])
        assert list_res.status_code == 200
        assert all(t["title"] != "Acme Private" for t in list_res.json())

    async def test_public_template_visible_across_orgs(self, client: AsyncClient, db_session, admin_user):
        election = await _make_election_with_categories(db_session, admin_user["user"])
        await _make_org(db_session, "Acme Public Co", admin_user["user"])
        create_res = await client.post(
            "/api/v1/templates",
            json={"kind": "election", "source_id": str(election.election_id), "title": "Acme Shared", "visibility": "public"},
            headers=admin_user["headers"],
        )
        assert create_res.status_code == 201

        outsider = await _make_organizer(db_session, "rival-org-user2@example.com")
        await _make_org(db_session, "Rival Co 2", outsider["user"])
        list_res = await client.get("/api/v1/templates", headers=outsider["headers"])
        assert list_res.status_code == 200
        match = next(t for t in list_res.json() if t["title"] == "Acme Shared")
        assert match["is_mine"] is False


class TestTemplateDelete:
    async def test_owner_can_delete(self, client: AsyncClient, db_session, admin_user):
        election = await _make_election_with_categories(db_session, admin_user["user"])
        create_res = await client.post(
            "/api/v1/templates",
            json={"kind": "election", "source_id": str(election.election_id), "title": "Deletable"},
            headers=admin_user["headers"],
        )
        template_id = create_res.json()["template_id"]
        del_res = await client.delete(f"/api/v1/templates/{template_id}", headers=admin_user["headers"])
        assert del_res.status_code == 204

    async def test_non_owner_cannot_delete_public_template(self, client: AsyncClient, db_session, admin_user):
        election = await _make_election_with_categories(db_session, admin_user["user"])
        create_res = await client.post(
            "/api/v1/templates",
            json={"kind": "election", "source_id": str(election.election_id), "title": "Public But Not Yours", "visibility": "public"},
            headers=admin_user["headers"],
        )
        template_id = create_res.json()["template_id"]

        outsider = await _make_organizer(db_session, "cant-delete-others@example.com")
        del_res = await client.delete(f"/api/v1/templates/{template_id}", headers=outsider["headers"])
        assert del_res.status_code == 403
