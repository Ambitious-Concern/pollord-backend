"""
Integration tests for the admin-controlled launch gate settings.

Covers:
  1. Admin PUT/GET /admin/platform-settings: launch_gate_enabled + launch_at
     round-trip, role-gated.
  2. (Task 2 adds TestPublicLaunchStatus to this same file.)

PUT /admin/platform-settings commits directly on the request's db session (see
admin.py::_set_platform_setting), which — like /waitlist/subscribe and
/waitlist/announce — permanently persists whatever the same session had
already flushed, including the admin_user/test_user fixtures. Purge those
fixture emails (and dependent rows) before/after every test here, same pattern
as tests/integration/test_waitlist.py.
"""

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AuditLog
from app.models.platform_setting import PlatformSetting
from app.models.user import User, UserRole

ADMIN_SETTINGS_URL = "/api/v1/admin/platform-settings"
PUBLIC_LAUNCH_URL = "/api/v1/settings/launch"

_SETTING_KEYS = ["launch_gate_enabled", "launch_at"]
_FIXTURE_EMAILS = ["admin@example.com", "test@example.com"]


async def _purge_fixture_users(session: AsyncSession) -> None:
    result = await session.execute(select(User.user_id).where(User.email.in_(_FIXTURE_EMAILS)))
    user_ids = [row[0] for row in result.all()]
    if not user_ids:
        return
    await session.execute(delete(AuditLog).where(AuditLog.user_id.in_(user_ids)))
    await session.execute(delete(UserRole).where(UserRole.user_id.in_(user_ids)))
    await session.execute(delete(User).where(User.user_id.in_(user_ids)))


@pytest_asyncio.fixture(autouse=True)
async def clean_state(db_session: AsyncSession):
    await db_session.execute(delete(PlatformSetting).where(PlatformSetting.key.in_(_SETTING_KEYS)))
    await _purge_fixture_users(db_session)
    await db_session.commit()
    yield
    await db_session.execute(delete(PlatformSetting).where(PlatformSetting.key.in_(_SETTING_KEYS)))
    await _purge_fixture_users(db_session)
    await db_session.commit()


@pytest.mark.asyncio
class TestAdminLaunchSettings:
    async def test_update_requires_admin_role(self, client: AsyncClient, test_user):
        response = await client.put(
            ADMIN_SETTINGS_URL,
            json={"launch_gate_enabled": False},
            headers=test_user["headers"],
        )
        assert response.status_code == 403

    async def test_defaults_before_any_update(self, client: AsyncClient, admin_user):
        response = await client.get(ADMIN_SETTINGS_URL, headers=admin_user["headers"])
        assert response.status_code == 200
        data = response.json()
        assert data["launch_gate_enabled"] is True
        assert data["launch_at"].startswith("2026-08-13")

    async def test_update_and_get_roundtrip(self, client: AsyncClient, admin_user):
        put_response = await client.put(
            ADMIN_SETTINGS_URL,
            json={"launch_gate_enabled": False, "launch_at": "2026-10-15T00:00:00+00:00"},
            headers=admin_user["headers"],
        )
        assert put_response.status_code == 200
        assert put_response.json()["launch_gate_enabled"] is False
        assert put_response.json()["launch_at"].startswith("2026-10-15")

        get_response = await client.get(ADMIN_SETTINGS_URL, headers=admin_user["headers"])
        assert get_response.status_code == 200
        assert get_response.json()["launch_gate_enabled"] is False
        assert get_response.json()["launch_at"].startswith("2026-10-15")

    async def test_naive_datetime_is_normalized_to_utc(self, client: AsyncClient, admin_user):
        # No timezone offset on this input — must be treated as UTC, not
        # silently stored naive (which would make every client parse it as
        # their own local time).
        put_response = await client.put(
            ADMIN_SETTINGS_URL,
            json={"launch_at": "2026-11-01T00:00:00"},
            headers=admin_user["headers"],
        )
        assert put_response.status_code == 200

        get_response = await client.get(ADMIN_SETTINGS_URL, headers=admin_user["headers"])
        assert get_response.status_code == 200
        launch_at = get_response.json()["launch_at"]
        assert launch_at.startswith("2026-11-01T00:00:00")
        assert launch_at.endswith("+00:00") or launch_at.endswith("Z")


@pytest.mark.asyncio
class TestPublicLaunchStatus:
    async def test_defaults_when_unset(self, client: AsyncClient):
        response = await client.get(PUBLIC_LAUNCH_URL)
        assert response.status_code == 200
        data = response.json()
        assert data["gate_enabled"] is True
        assert data["launch_at"].startswith("2026-08-13")

    async def test_reflects_admin_update_with_no_auth_required(
        self, client: AsyncClient, admin_user
    ):
        put_response = await client.put(
            ADMIN_SETTINGS_URL,
            json={"launch_gate_enabled": False, "launch_at": "2026-09-01T12:00:00+00:00"},
            headers=admin_user["headers"],
        )
        assert put_response.status_code == 200

        response = await client.get(PUBLIC_LAUNCH_URL)
        assert response.status_code == 200
        data = response.json()
        assert data["gate_enabled"] is False
        assert data["launch_at"].startswith("2026-09-01T12:00:00")
