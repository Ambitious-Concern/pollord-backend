"""What an invited organization member can see and change.

Accepting an invitation only granted the platform roles when the invite role
was exactly "admin". Anyone invited as editor or member kept whatever they
had — usually just Voter — and the organizer app gates its whole dashboard on
Election Administrator / Event Organizer / System Administrator. So an
invitee could sign in and then see nothing at all.

Granting those roles to every member fixes the view side but would hand a
plain member write access too, because the organizer endpoints use one role
set for both reading and writing. So write is separately gated on holding a
managing role in the organization.

The existing visibility suite never caught this: its helper grants both
platform roles to every test user, which is precisely what a real invitee
lacks. Users here are built without them, like a real invitation.
"""
from datetime import date, datetime, time, timedelta, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.election import Election
from app.models.event import Event
from app.models.organization import (
    Organization,
    OrganizationInvitation,
    OrganizationMember,
)
from app.models.user import Role, User, UserRole

VIEW_ROLES = ("Election Administrator", "Event Organizer")


async def _role(db: AsyncSession, name: str) -> Role:
    result = await db.execute(select(Role).where(Role.role_name == name))
    role = result.scalar_one_or_none()
    if not role:
        role = Role(role_name=name, permissions={})
        db.add(role)
        await db.flush()
    return role


async def _make_user(db: AsyncSession, email: str, *, roles=("Voter",)) -> dict:
    """A user with only the roles given — by default just Voter, like an invitee."""
    user = User(
        email=email,
        password_hash=hash_password("Passw0rd!"),
        full_name=email.split("@")[0].title(),
        email_verified=True,
        account_status="active",
    )
    db.add(user)
    await db.flush()
    for name in roles:
        role = await _role(db, name)
        db.add(UserRole(user_id=user.user_id, role_id=role.role_id))
    await db.flush()
    token = create_access_token(str(user.user_id))
    return {"user": user, "headers": {"Authorization": f"Bearer {token}"}}


async def _role_names(db: AsyncSession, user_id) -> set[str]:
    result = await db.execute(
        select(Role.role_name)
        .join(UserRole, UserRole.role_id == Role.role_id)
        .where(UserRole.user_id == user_id)
    )
    return {row[0] for row in result.all()}


async def _make_org(db: AsyncSession, name: str, owner) -> Organization:
    org = Organization(name=name, owner_id=owner["user"].user_id)
    db.add(org)
    await db.flush()
    db.add(OrganizationMember(
        org_id=org.org_id, user_id=owner["user"].user_id, role="owner"
    ))
    await db.flush()
    return org


async def _invitation(
    db: AsyncSession, org: Organization, email: str, role: str, inviter
) -> OrganizationInvitation:
    inv = OrganizationInvitation(
        org_id=org.org_id,
        email=email,
        role=role,
        token=uuid4().hex,
        status="pending",
        invited_by=inviter["user"].user_id,
        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
    )
    db.add(inv)
    await db.flush()
    return inv


@pytest_asyncio.fixture
async def org_with_work(db_session: AsyncSession) -> dict:
    """An org whose owner has already created an election and an event."""
    owner = await _make_user(
        db_session, f"owner-{uuid4().hex[:6]}@example.com", roles=VIEW_ROLES
    )
    org = await _make_org(db_session, "Acme Society", owner)

    now = datetime.now(timezone.utc)
    election = Election(
        title="Existing Election",
        start_datetime=now + timedelta(days=1),
        end_datetime=now + timedelta(days=2),
        status="draft",
        created_by=owner["user"].user_id,
    )
    event = Event(
        title="Existing Event",
        event_date=date(2026, 12, 1),
        event_time=time(19, 0),
        location="Accra",
        status="published",
        created_by=owner["user"].user_id,
    )
    db_session.add_all([election, event])
    await db_session.flush()
    return {"owner": owner, "org": org, "election": election, "event": event}


def _election_payload(title: str) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "title": title,
        "start_datetime": (now + timedelta(days=3)).isoformat(),
        "end_datetime": (now + timedelta(days=4)).isoformat(),
    }


@pytest.mark.asyncio
class TestAcceptanceGrantsViewRoles:
    @pytest.mark.parametrize("invite_role", ["member", "editor", "admin"])
    async def test_every_accepted_member_can_reach_the_dashboard(
        self, client: AsyncClient, db_session: AsyncSession, org_with_work, invite_role
    ):
        """Whatever the org role, they need the platform roles to see anything."""
        invitee = await _make_user(db_session, f"inv-{uuid4().hex[:6]}@example.com")
        inv = await _invitation(
            db_session, org_with_work["org"], invitee["user"].email,
            invite_role, org_with_work["owner"],
        )

        response = await client.post(
            "/api/v1/organizations/invitations/accept",
            json={"token": inv.token},
            headers=invitee["headers"],
        )
        assert response.status_code == 200, response.text

        assert set(VIEW_ROLES) <= await _role_names(db_session, invitee["user"].user_id)

    @pytest.mark.parametrize("member_role", ["member", "editor"])
    async def test_direct_add_also_grants_view_roles(
        self, client: AsyncClient, db_session: AsyncSession, org_with_work, member_role
    ):
        newcomer = await _make_user(db_session, f"add-{uuid4().hex[:6]}@example.com")

        response = await client.post(
            f"/api/v1/organizations/{org_with_work['org'].org_id}/members",
            json={"user_id": str(newcomer["user"].user_id), "role": member_role},
            headers=org_with_work["owner"]["headers"],
        )
        assert response.status_code == 201, response.text

        assert set(VIEW_ROLES) <= await _role_names(db_session, newcomer["user"].user_id)


@pytest.mark.asyncio
class TestPlainMemberCanViewNotEdit:
    @pytest_asyncio.fixture
    async def member(self, db_session: AsyncSession, org_with_work) -> dict:
        m = await _make_user(db_session, f"m-{uuid4().hex[:6]}@example.com", roles=VIEW_ROLES)
        db_session.add(OrganizationMember(
            org_id=org_with_work["org"].org_id,
            user_id=m["user"].user_id,
            role="member",
        ))
        await db_session.flush()
        return m

    async def test_member_sees_the_orgs_existing_elections(
        self, client: AsyncClient, org_with_work, member
    ):
        response = await client.get("/api/v1/elections", headers=member["headers"])
        assert response.status_code == 200, response.text
        titles = [e["title"] for e in response.json()]
        assert "Existing Election" in titles

    async def test_member_sees_the_orgs_existing_events(
        self, client: AsyncClient, org_with_work, member
    ):
        response = await client.get("/api/v1/events", headers=member["headers"])
        assert response.status_code == 200, response.text
        titles = [e["title"] for e in response.json()]
        assert "Existing Event" in titles

    async def test_member_cannot_create_an_election(
        self, client: AsyncClient, org_with_work, member
    ):
        response = await client.post(
            "/api/v1/elections",
            json=_election_payload("Member's Election"),
            headers=member["headers"],
        )
        assert response.status_code == 403, response.text

    async def test_member_cannot_create_an_event(
        self, client: AsyncClient, org_with_work, member
    ):
        response = await client.post(
            "/api/v1/events",
            json={
                "title": "Member's Event",
                "event_date": "2026-12-20",
                "event_time": "19:00:00",
                "location": "Kumasi",
            },
            headers=member["headers"],
        )
        assert response.status_code == 403, response.text


@pytest.mark.asyncio
class TestManagingRolesKeepWriteAccess:
    @pytest.mark.parametrize("managing_role", ["owner", "admin", "editor"])
    async def test_managing_member_can_create_an_election(
        self, client: AsyncClient, db_session: AsyncSession, org_with_work, managing_role
    ):
        u = await _make_user(db_session, f"mg-{uuid4().hex[:6]}@example.com", roles=VIEW_ROLES)
        db_session.add(OrganizationMember(
            org_id=org_with_work["org"].org_id,
            user_id=u["user"].user_id,
            role=managing_role,
        ))
        await db_session.flush()

        response = await client.post(
            "/api/v1/elections",
            json=_election_payload(f"{managing_role} Election"),
            headers=u["headers"],
        )
        assert response.status_code in (200, 201), response.text

    async def test_user_with_no_organization_can_still_create(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        """A solo organizer belongs to no org and must not be locked out."""
        solo = await _make_user(db_session, f"solo-{uuid4().hex[:6]}@example.com", roles=VIEW_ROLES)

        response = await client.post(
            "/api/v1/elections",
            json=_election_payload("Solo Election"),
            headers=solo["headers"],
        )
        assert response.status_code in (200, 201), response.text


@pytest.mark.asyncio
class TestOutsiderStillSeesNothing:
    async def test_non_member_does_not_see_the_orgs_election(
        self, client: AsyncClient, db_session: AsyncSession, org_with_work
    ):
        outsider = await _make_user(
            db_session, f"out-{uuid4().hex[:6]}@example.com", roles=VIEW_ROLES
        )
        response = await client.get("/api/v1/elections", headers=outsider["headers"])
        assert response.status_code == 200
        assert "Existing Election" not in [e["title"] for e in response.json()]


@pytest.mark.asyncio
class TestRoleChangesKeepViewAccess:
    """Demotion removes edit rights, not the ability to see the org at all."""

    async def _member(self, db, org_with_work, role):
        u = await _make_user(db, f"rc-{uuid4().hex[:6]}@example.com", roles=VIEW_ROLES)
        m = OrganizationMember(
            org_id=org_with_work["org"].org_id, user_id=u["user"].user_id, role=role
        )
        db.add(m)
        await db.flush()
        return u, m

    async def test_demoted_admin_keeps_view_roles(
        self, client: AsyncClient, db_session: AsyncSession, org_with_work
    ):
        u, m = await self._member(db_session, org_with_work, "admin")

        response = await client.put(
            f"/api/v1/organizations/{org_with_work['org'].org_id}/members/{m.member_id}",
            json={"role": "member"},
            headers=org_with_work["owner"]["headers"],
        )
        assert response.status_code == 200, response.text

        # Still a member of the org, so they must still be able to see its work.
        assert set(VIEW_ROLES) <= await _role_names(db_session, u["user"].user_id)

    async def test_demoted_admin_loses_create_access(
        self, client: AsyncClient, db_session: AsyncSession, org_with_work
    ):
        u, m = await self._member(db_session, org_with_work, "admin")

        await client.put(
            f"/api/v1/organizations/{org_with_work['org'].org_id}/members/{m.member_id}",
            json={"role": "member"},
            headers=org_with_work["owner"]["headers"],
        )

        response = await client.post(
            "/api/v1/elections",
            json=_election_payload("Demoted Election"),
            headers=u["headers"],
        )
        assert response.status_code == 403, response.text

    async def test_removed_member_loses_view_roles_when_in_no_other_org(
        self, client: AsyncClient, db_session: AsyncSession, org_with_work
    ):
        u, m = await self._member(db_session, org_with_work, "member")

        response = await client.delete(
            f"/api/v1/organizations/{org_with_work['org'].org_id}/members/{m.member_id}",
            headers=org_with_work["owner"]["headers"],
        )
        assert response.status_code in (200, 204), response.text

        assert not (set(VIEW_ROLES) & await _role_names(db_session, u["user"].user_id))

    async def test_removed_member_keeps_view_roles_when_still_in_another_org(
        self, client: AsyncClient, db_session: AsyncSession, org_with_work
    ):
        """Leaving one org must not strip access to a second one."""
        u, m = await self._member(db_session, org_with_work, "member")
        other_owner = await _make_user(
            db_session, f"oo-{uuid4().hex[:6]}@example.com", roles=VIEW_ROLES
        )
        other = await _make_org(db_session, "Other Org", other_owner)
        db_session.add(OrganizationMember(
            org_id=other.org_id, user_id=u["user"].user_id, role="member"
        ))
        await db_session.flush()

        await client.delete(
            f"/api/v1/organizations/{org_with_work['org'].org_id}/members/{m.member_id}",
            headers=org_with_work["owner"]["headers"],
        )

        assert set(VIEW_ROLES) <= await _role_names(db_session, u["user"].user_id)


@pytest.mark.asyncio
class TestAcceptWithSignup:
    """Joining from an invitation email needs a name and a password. Nothing else.

    The old flow bounced the invitee to the full signup page with their email
    pre-filled, so they retyped an address we already knew and verified it by
    OTP, then came back to click Join. The token already proves control of
    that inbox, so both steps are redundant.
    """

    async def test_creates_the_account_and_joins_in_one_call(
        self, client: AsyncClient, db_session: AsyncSession, org_with_work
    ):
        email = f"new-{uuid4().hex[:6]}@example.com"
        inv = await _invitation(
            db_session, org_with_work["org"], email, "editor", org_with_work["owner"]
        )

        response = await client.post(
            "/api/v1/organizations/invitations/accept-with-signup",
            json={"token": inv.token, "full_name": "Ada Lovelace", "password": "Passw0rd!"},
        )
        assert response.status_code in (200, 201), response.text
        body = response.json()

        # Signed in immediately — no second login step.
        assert body["access_token"]
        assert body["refresh_token"]

        result = await db_session.execute(select(User).where(User.email == email))
        user = result.scalar_one()
        assert user.full_name == "Ada Lovelace"
        # The emailed link already proved they control this inbox.
        assert user.email_verified is True

    async def test_grants_membership_with_the_invited_role(
        self, client: AsyncClient, db_session: AsyncSession, org_with_work
    ):
        email = f"ed-{uuid4().hex[:6]}@example.com"
        inv = await _invitation(
            db_session, org_with_work["org"], email, "editor", org_with_work["owner"]
        )

        await client.post(
            "/api/v1/organizations/invitations/accept-with-signup",
            json={"token": inv.token, "full_name": "Ed Member", "password": "Passw0rd!"},
        )

        user = (
            await db_session.execute(select(User).where(User.email == email))
        ).scalar_one()
        member = (
            await db_session.execute(
                select(OrganizationMember).where(
                    OrganizationMember.user_id == user.user_id
                )
            )
        ).scalar_one()
        assert member.role == "editor"
        assert member.org_id == org_with_work["org"].org_id
        assert set(VIEW_ROLES) <= await _role_names(db_session, user.user_id)

    async def test_new_member_immediately_sees_existing_org_work(
        self, client: AsyncClient, db_session: AsyncSession, org_with_work
    ):
        """The whole point: log in and the organization's work is already there."""
        email = f"see-{uuid4().hex[:6]}@example.com"
        inv = await _invitation(
            db_session, org_with_work["org"], email, "member", org_with_work["owner"]
        )

        tokens = (
            await client.post(
                "/api/v1/organizations/invitations/accept-with-signup",
                json={"token": inv.token, "full_name": "Seer", "password": "Passw0rd!"},
            )
        ).json()
        headers = {"Authorization": f"Bearer {tokens['access_token']}"}

        elections = await client.get("/api/v1/elections", headers=headers)
        assert elections.status_code == 200, elections.text
        assert "Existing Election" in [e["title"] for e in elections.json()]

    async def test_token_cannot_be_reused(
        self, client: AsyncClient, db_session: AsyncSession, org_with_work
    ):
        inv = await _invitation(
            db_session, org_with_work["org"], f"once-{uuid4().hex[:6]}@example.com",
            "member", org_with_work["owner"],
        )
        payload = {"token": inv.token, "full_name": "Once", "password": "Passw0rd!"}

        first = await client.post(
            "/api/v1/organizations/invitations/accept-with-signup", json=payload
        )
        assert first.status_code in (200, 201)

        second = await client.post(
            "/api/v1/organizations/invitations/accept-with-signup", json=payload
        )
        assert second.status_code == 410, second.text

    async def test_unknown_token_is_rejected(self, client: AsyncClient):
        response = await client.post(
            "/api/v1/organizations/invitations/accept-with-signup",
            json={"token": uuid4().hex, "full_name": "Nobody", "password": "Passw0rd!"},
        )
        assert response.status_code == 404, response.text

    async def test_expired_invitation_is_rejected(
        self, client: AsyncClient, db_session: AsyncSession, org_with_work
    ):
        inv = await _invitation(
            db_session, org_with_work["org"], f"exp-{uuid4().hex[:6]}@example.com",
            "member", org_with_work["owner"],
        )
        inv.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
        await db_session.flush()

        response = await client.post(
            "/api/v1/organizations/invitations/accept-with-signup",
            json={"token": inv.token, "full_name": "Late", "password": "Passw0rd!"},
        )
        assert response.status_code == 410, response.text

    async def test_existing_account_is_told_to_sign_in(
        self, client: AsyncClient, db_session: AsyncSession, org_with_work
    ):
        """Signing up would clobber a real account, so refuse and say why."""
        existing = await _make_user(db_session, f"dup-{uuid4().hex[:6]}@example.com")
        inv = await _invitation(
            db_session, org_with_work["org"], existing["user"].email,
            "member", org_with_work["owner"],
        )

        response = await client.post(
            "/api/v1/organizations/invitations/accept-with-signup",
            json={"token": inv.token, "full_name": "Dupe", "password": "Passw0rd!"},
        )
        assert response.status_code == 409, response.text

    async def test_weak_password_is_rejected(
        self, client: AsyncClient, db_session: AsyncSession, org_with_work
    ):
        inv = await _invitation(
            db_session, org_with_work["org"], f"weak-{uuid4().hex[:6]}@example.com",
            "member", org_with_work["owner"],
        )
        response = await client.post(
            "/api/v1/organizations/invitations/accept-with-signup",
            json={"token": inv.token, "full_name": "Weak", "password": "abc"},
        )
        assert response.status_code == 422, response.text
