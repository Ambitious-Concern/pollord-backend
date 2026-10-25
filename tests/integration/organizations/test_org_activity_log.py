from datetime import datetime, timedelta, timezone

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.organization import Organization, OrganizationMember


async def _make_user(db: AsyncSession, email: str) -> dict:
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
    db.add(OrganizationMember(org_id=org.org_id, user_id=owner.user_id, role="owner"))
    await db.flush()
    return org


@pytest_asyncio.fixture
async def org_team(db_session: AsyncSession) -> dict:
    owner = await _make_user(db_session, "owner@activity.test")
    outsider = await _make_user(db_session, "outsider@activity.test")
    acme = await _make_org(db_session, "Acme Activity", owner["user"])
    rival = await _make_org(db_session, "Rival Activity", outsider["user"])
    return {"owner": owner, "outsider": outsider, "acme": acme, "rival": rival}


def _election_payload() -> dict:
    now = datetime.now(timezone.utc)
    return {
        "title": "Best Dressed",
        "start_datetime": (now + timedelta(hours=1)).isoformat(),
        "end_datetime": (now + timedelta(days=1)).isoformat(),
    }


class TestElectionActivityLogging:
    async def test_create_election_logs_activity(self, client: AsyncClient, org_team):
        res = await client.post(
            "/api/v1/elections", json=_election_payload(), headers=org_team["owner"]["headers"]
        )
        assert res.status_code == 201, res.text

        activity = await client.get(
            f"/api/v1/organizations/{org_team['acme'].org_id}/activity",
            headers=org_team["owner"]["headers"],
        )
        assert activity.status_code == 200
        entries = activity.json()
        assert any(
            e["category"] == "election" and e["verb"] == "created election" and e["target_name"] == "Best Dressed"
            for e in entries
        )
        created_entry = next(e for e in entries if e["verb"] == "created election")
        assert created_entry["actor"]["role"] == "owner"
        assert created_entry["actor"]["is_system"] is False

    async def test_update_election_logs_activity(self, client: AsyncClient, org_team):
        create_res = await client.post(
            "/api/v1/elections", json=_election_payload(), headers=org_team["owner"]["headers"]
        )
        election_id = create_res.json()["election_id"]

        res = await client.put(
            f"/api/v1/elections/{election_id}",
            json={"description": "Updated description"},
            headers=org_team["owner"]["headers"],
        )
        assert res.status_code == 200, res.text

        activity = await client.get(
            f"/api/v1/organizations/{org_team['acme'].org_id}/activity",
            headers=org_team["owner"]["headers"],
        )
        entries = activity.json()
        assert any(e["verb"] == "updated settings for" for e in entries)

    async def test_outsider_cannot_view_activity(self, client: AsyncClient, org_team):
        res = await client.get(
            f"/api/v1/organizations/{org_team['acme'].org_id}/activity",
            headers=org_team["outsider"]["headers"],
        )
        assert res.status_code == 403

    async def test_org_activity_isolated_from_other_org(self, client: AsyncClient, org_team):
        await client.post(
            "/api/v1/elections", json=_election_payload(), headers=org_team["owner"]["headers"]
        )
        rival_activity = await client.get(
            f"/api/v1/organizations/{org_team['rival'].org_id}/activity",
            headers=org_team["outsider"]["headers"],
        )
        assert rival_activity.status_code == 200
        assert rival_activity.json() == []


class TestActivityLogCapturesRequestMetadata:
    async def test_create_election_captures_ip_and_device(self, client: AsyncClient, org_team):
        res = await client.post(
            "/api/v1/elections", json=_election_payload(), headers=org_team["owner"]["headers"]
        )
        assert res.status_code == 201, res.text

        activity = await client.get(
            f"/api/v1/organizations/{org_team['acme'].org_id}/activity",
            headers=org_team["owner"]["headers"],
        )
        entries = activity.json()
        created_entry = next(e for e in entries if e["verb"] == "created election")
        assert created_entry["ip_address"] == "127.0.0.1"
        assert created_entry["user_agent"]


class TestTeamAndSettingsActivityLogging:
    async def test_update_organization_logs_settings_activity(self, client: AsyncClient, org_team):
        res = await client.put(
            f"/api/v1/organizations/{org_team['acme'].org_id}",
            json={"description": "A new description"},
            headers=org_team["owner"]["headers"],
        )
        assert res.status_code == 200, res.text

        activity = await client.get(
            f"/api/v1/organizations/{org_team['acme'].org_id}/activity",
            headers=org_team["owner"]["headers"],
        )
        entries = activity.json()
        assert any(
            e["category"] == "settings" and e["target_name"] == "Organization profile"
            for e in entries
        )

    async def test_invite_member_logs_team_activity(self, client: AsyncClient, org_team):
        res = await client.post(
            f"/api/v1/organizations/{org_team['acme'].org_id}/invite",
            json={"email": "newteammate@acme-activity-test.com", "role": "member"},
            headers=org_team["owner"]["headers"],
        )
        assert res.status_code == 201, res.text

        activity = await client.get(
            f"/api/v1/organizations/{org_team['acme'].org_id}/activity",
            headers=org_team["owner"]["headers"],
        )
        entries = activity.json()
        assert any(e["category"] == "team" and e["verb"] == "invited" for e in entries)

    async def test_category_filter(self, client: AsyncClient, org_team):
        await client.put(
            f"/api/v1/organizations/{org_team['acme'].org_id}",
            json={"description": "Filtered update"},
            headers=org_team["owner"]["headers"],
        )
        await client.post(
            "/api/v1/elections", json=_election_payload(), headers=org_team["owner"]["headers"]
        )

        activity = await client.get(
            f"/api/v1/organizations/{org_team['acme'].org_id}/activity",
            params={"category": "settings"},
            headers=org_team["owner"]["headers"],
        )
        entries = activity.json()
        assert len(entries) > 0
        assert all(e["category"] == "settings" for e in entries)
