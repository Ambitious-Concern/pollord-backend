"""Read-only access to the admin console.

The console was all-or-nothing: only System Administrator could open it, and
that role can change anything. There was no way to give someone visibility
without also giving them the ability to edit vote prices, suspend accounts
and rewrite platform settings.

Platform Viewer fills that gap. Reads on the admin endpoints accept it;
writes still require System Administrator, so this only ever widens read
access and never loosens a write. Hiding buttons in the console would not be
enough on its own — a viewer could call the API directly — so it is enforced
here.
"""
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.user import Role, User, UserRole

VIEWER_ROLE = "Platform Viewer"


async def _user_with_role(db: AsyncSession, role_name: str) -> dict:
    result = await db.execute(select(Role).where(Role.role_name == role_name))
    role = result.scalar_one_or_none()
    if not role:
        role = Role(role_name=role_name, permissions={})
        db.add(role)
        await db.flush()

    user = User(
        email=f"{role_name.lower().replace(' ', '-')}-{uuid4().hex[:6]}@example.com",
        password_hash=hash_password("Passw0rd!"),
        full_name=role_name,
        email_verified=True,
        account_status="active",
    )
    db.add(user)
    await db.flush()
    db.add(UserRole(user_id=user.user_id, role_id=role.role_id))
    await db.flush()

    token = create_access_token(str(user.user_id))
    return {"user": user, "headers": {"Authorization": f"Bearer {token}"}}


@pytest_asyncio.fixture
async def viewer(db_session: AsyncSession) -> dict:
    return await _user_with_role(db_session, VIEWER_ROLE)


@pytest.mark.asyncio
class TestViewerCanRead:
    @pytest.mark.parametrize(
        "path",
        [
            "/api/v1/admin/users",
            "/api/v1/admin/organizations",
            "/api/v1/admin/platform-settings",
            "/api/v1/admin/audit-logs",
            "/api/v1/admin/roles",
        ],
    )
    async def test_viewer_can_read_console_data(
        self, client: AsyncClient, viewer, path
    ):
        response = await client.get(path, headers=viewer["headers"])
        assert response.status_code == 200, f"{path}: {response.text}"


@pytest.mark.asyncio
class TestViewerCannotWrite:
    async def test_viewer_cannot_change_platform_settings(
        self, client: AsyncClient, viewer
    ):
        response = await client.put(
            "/api/v1/admin/platform-settings",
            json={"vote_price": 250},
            headers=viewer["headers"],
        )
        assert response.status_code == 403, response.text

    async def test_viewer_cannot_suspend_a_user(
        self, client: AsyncClient, db_session: AsyncSession, viewer
    ):
        target = await _user_with_role(db_session, "Voter")
        response = await client.put(
            f"/api/v1/admin/users/{target['user'].user_id}/status",
            json={"account_status": "suspended"},
            headers=viewer["headers"],
        )
        assert response.status_code == 403, response.text

    async def test_viewer_cannot_assign_roles(
        self, client: AsyncClient, db_session: AsyncSession, viewer
    ):
        target = await _user_with_role(db_session, "Voter")
        response = await client.put(
            f"/api/v1/admin/users/{target['user'].user_id}/roles",
            json={"role_ids": []},
            headers=viewer["headers"],
        )
        assert response.status_code == 403, response.text


@pytest.mark.asyncio
class TestSystemAdministratorUnchanged:
    """The whole point of a widening: the existing role must behave identically."""

    async def test_admin_can_still_read(self, client: AsyncClient, admin_user):
        response = await client.get(
            "/api/v1/admin/users", headers=admin_user["headers"]
        )
        assert response.status_code == 200, response.text

    async def test_admin_can_still_write(self, client: AsyncClient, admin_user):
        response = await client.put(
            "/api/v1/admin/platform-settings",
            json={"vote_price": 250},
            headers=admin_user["headers"],
        )
        assert response.status_code == 200, response.text


@pytest.mark.asyncio
class TestOtherRolesStillLockedOut:
    async def test_voter_cannot_read_console_data(
        self, client: AsyncClient, test_user
    ):
        response = await client.get(
            "/api/v1/admin/users", headers=test_user["headers"]
        )
        assert response.status_code == 403, response.text

    async def test_event_organizer_cannot_read_console_data(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        organizer = await _user_with_role(db_session, "Event Organizer")
        response = await client.get(
            "/api/v1/admin/users", headers=organizer["headers"]
        )
        assert response.status_code == 403, response.text
