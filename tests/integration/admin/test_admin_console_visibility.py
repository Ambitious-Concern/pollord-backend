"""Seeing who is on the platform, and who is in an organization.

Two gaps made an admin unable to find a person they had just added:

  - /admin/users paginated with OFFSET/LIMIT over an unordered query. Without
    an ORDER BY, Postgres may return rows in any order, so a new user could be
    absent from the first page entirely and rows could repeat across pages.
  - the org drill-in reported total_members as a count and nothing else, so
    there was nowhere in the console to see the members themselves.
"""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.models.organization import Organization, OrganizationMember
from app.models.user import Role, User, UserRole


async def _user(db: AsyncSession, email: str, *, created_at=None) -> User:
    user = User(
        email=email,
        password_hash=hash_password("Passw0rd!"),
        full_name=email.split("@")[0].title(),
        email_verified=True,
        account_status="active",
    )
    if created_at:
        user.created_at = created_at
    db.add(user)
    await db.flush()
    return user


@pytest.mark.asyncio
class TestUserListingIsDeterministic:
    async def test_newest_users_come_first(
        self, client: AsyncClient, db_session: AsyncSession, admin_user
    ):
        """A just-added person must be findable without paging through everyone."""
        now = datetime.now(timezone.utc)
        marker = uuid4().hex[:8]
        # Deliberately inserted oldest-last so insertion order can't be what
        # makes this pass.
        await _user(db_session, f"old-{marker}@example.com", created_at=now - timedelta(days=30))
        await _user(db_session, f"new-{marker}@example.com", created_at=now)
        await _user(db_session, f"mid-{marker}@example.com", created_at=now - timedelta(days=1))

        response = await client.get(
            "/api/v1/admin/users?limit=100", headers=admin_user["headers"]
        )
        assert response.status_code == 200, response.text
        emails = [u["email"] for u in response.json()]

        ours = [e for e in emails if marker in e]
        assert ours == [
            f"new-{marker}@example.com",
            f"mid-{marker}@example.com",
            f"old-{marker}@example.com",
        ]

    async def test_pages_do_not_overlap(
        self, client: AsyncClient, db_session: AsyncSession, admin_user
    ):
        """Unordered OFFSET/LIMIT can repeat a row on page 2 and drop another."""
        for i in range(6):
            await _user(db_session, f"page-{uuid4().hex[:8]}-{i}@example.com")

        first = await client.get(
            "/api/v1/admin/users?skip=0&limit=3", headers=admin_user["headers"]
        )
        second = await client.get(
            "/api/v1/admin/users?skip=3&limit=3", headers=admin_user["headers"]
        )
        assert first.status_code == 200 and second.status_code == 200

        ids_one = {u["user_id"] for u in first.json()}
        ids_two = {u["user_id"] for u in second.json()}
        assert not (ids_one & ids_two)


@pytest.mark.asyncio
class TestOrgAnalyticsListsMembers:
    @pytest_asyncio.fixture
    async def org(self, db_session: AsyncSession) -> dict:
        owner = await _user(db_session, f"own-{uuid4().hex[:6]}@example.com")
        viewer = await _user(db_session, f"vw-{uuid4().hex[:6]}@example.com")

        org = Organization(name="Jubilee Hostel", owner_id=owner.user_id)
        db_session.add(org)
        await db_session.flush()
        db_session.add_all([
            OrganizationMember(org_id=org.org_id, user_id=owner.user_id, role="owner"),
            OrganizationMember(org_id=org.org_id, user_id=viewer.user_id, role="member"),
        ])
        await db_session.flush()
        return {"org": org, "owner": owner, "viewer": viewer}

    async def test_members_are_listed_with_identity_and_role(
        self, client: AsyncClient, admin_user, org
    ):
        response = await client.get(
            f"/api/v1/admin/organizations/{org['org'].org_id}/analytics",
            headers=admin_user["headers"],
        )
        assert response.status_code == 200, response.text
        body = response.json()

        by_email = {m["user_email"]: m for m in body["members"]}
        assert org["owner"].email in by_email
        assert org["viewer"].email in by_email
        assert by_email[org["viewer"].email]["role"] == "member"
        assert by_email[org["owner"].email]["role"] == "owner"
        assert by_email[org["viewer"].email]["user_name"] == org["viewer"].full_name

    async def test_member_list_matches_the_reported_count(
        self, client: AsyncClient, admin_user, org
    ):
        body = (
            await client.get(
                f"/api/v1/admin/organizations/{org['org'].org_id}/analytics",
                headers=admin_user["headers"],
            )
        ).json()
        assert len(body["members"]) == body["total_members"]

    async def test_owner_is_listed_first(self, client: AsyncClient, admin_user, org):
        body = (
            await client.get(
                f"/api/v1/admin/organizations/{org['org'].org_id}/analytics",
                headers=admin_user["headers"],
            )
        ).json()
        assert body["members"][0]["role"] == "owner"

    async def test_requires_admin(self, client: AsyncClient, test_user, org):
        response = await client.get(
            f"/api/v1/admin/organizations/{org['org'].org_id}/analytics",
            headers=test_user["headers"],
        )
        assert response.status_code == 403
