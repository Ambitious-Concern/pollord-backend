"""Notification-preference gating on real triggers — GET/PUT prefs
endpoint, plus end-to-end proof that a couple of representative triggers
(payout rejected, new member) actually create bell notifications for the
right people, and that OrgNotificationService's cooldown suppresses
repeats within the window.

IDs captured into local variables immediately after each DB write; see
test_org_roles.py's module docstring for why (shared-session rollback/
expiry gotchas under this fixture setup)."""
import uuid
from datetime import datetime, timedelta, timezone

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.election import Election
from app.models.notification import Notification
from app.models.organization import Organization, OrganizationMember
from app.models.payout_request import PayoutRequest
from app.models.user import User
from app.services.org_notification_service import OrgNotificationService


async def _make_user(db: AsyncSession, email: str) -> dict:
    user = User(
        email=email, password_hash=hash_password("Passw0rd!"),
        full_name=email.split("@")[0].title(), email_verified=True, account_status="active",
    )
    db.add(user)
    await db.flush()
    token = create_access_token(str(user.user_id))
    return {"user": user, "headers": {"Authorization": f"Bearer {token}"}}


async def _make_org(db: AsyncSession, owner: dict) -> str:
    org = Organization(name="Notif Prefs Test Org", owner_id=owner["user"].user_id)
    db.add(org)
    await db.flush()
    org_id = str(org.org_id)
    db.add(OrganizationMember(org_id=org.org_id, user_id=owner["user"].user_id, role="owner"))
    await db.flush()
    return org_id


async def _add_member(db: AsyncSession, org_id: str, user: dict, role: str) -> None:
    db.add(OrganizationMember(org_id=org_id, user_id=user["user"].user_id, role=role))
    await db.flush()
    await db.commit()


class TestNotificationPrefsEndpoint:
    async def test_get_defaults(self, client: AsyncClient, test_user):
        res = await client.get("/api/v1/users/me/notification-prefs", headers=test_user["headers"])
        assert res.status_code == 200
        prefs = res.json()["prefs"]
        assert prefs["vote_cast"] is True
        assert prefs["new_member"] is False

    async def test_put_partial_update_persists(self, client: AsyncClient, test_user):
        res = await client.put(
            "/api/v1/users/me/notification-prefs",
            json={"prefs": {"vote_cast": False}},
            headers=test_user["headers"],
        )
        assert res.status_code == 200
        assert res.json()["prefs"]["vote_cast"] is False
        assert res.json()["prefs"]["election_started"] is True  # untouched key still defaults

        get_res = await client.get("/api/v1/users/me/notification-prefs", headers=test_user["headers"])
        assert get_res.json()["prefs"]["vote_cast"] is False

    async def test_put_rejects_unknown_key(self, client: AsyncClient, test_user):
        res = await client.put(
            "/api/v1/users/me/notification-prefs",
            json={"prefs": {"not_a_real_key": True}},
            headers=test_user["headers"],
        )
        assert res.status_code == 422


class TestPayoutRejectedTrigger:
    async def test_rejected_payout_notifies_org_members(
        self, client: AsyncClient, db_session: AsyncSession, admin_user
    ):
        owner = await _make_user(db_session, "payout-owner@example.com")
        org_id = await _make_org(db_session, owner)

        now = datetime.now(timezone.utc)
        election = Election(
            title="Payout Notif Election", start_datetime=now, end_datetime=now + timedelta(days=1),
            status="completed", created_by=owner["user"].user_id, org_id=org_id,
        )
        db_session.add(election)
        await db_session.flush()
        election_id = str(election.election_id)

        payout = PayoutRequest(
            election_id=election_id, organizer_id=owner["user"].user_id, amount=100, status="pending",
        )
        db_session.add(payout)
        await db_session.flush()
        payout_id = str(payout.payout_request_id)
        owner_user_id = owner["user"].user_id
        await db_session.commit()

        res = await client.put(
            f"/api/v1/payouts/admin/{payout_id}",
            json={"status": "rejected", "admin_notes": "Missing details"},
            headers=admin_user["headers"],
        )
        assert res.status_code == 200, res.text

        result = await db_session.execute(
            select(Notification).where(Notification.user_id == owner_user_id, Notification.org_id == org_id)
        )
        notifications = result.scalars().all()
        assert any("rejected" in n.title.lower() for n in notifications)


class TestNewMemberTrigger:
    async def test_add_member_notifies_existing_members(
        self, client: AsyncClient, db_session: AsyncSession, admin_user
    ):
        org_id = await _make_org(db_session, admin_user)
        existing_admin = await _make_user(db_session, "existing-admin@example.com")
        await _add_member(db_session, org_id, existing_admin, role="admin")
        existing_admin_user_id = existing_admin["user"].user_id

        new_person = await _make_user(db_session, "new-person@example.com")
        new_user_id = new_person["user"].user_id
        await db_session.commit()

        res = await client.post(
            f"/api/v1/organizations/{org_id}/members",
            json={"user_id": str(new_user_id), "role": "member"},
            headers=admin_user["headers"],
        )
        assert res.status_code == 201, res.text

        result = await db_session.execute(
            select(Notification).where(
                Notification.user_id == existing_admin_user_id, Notification.org_id == org_id
            )
        )
        notifications = result.scalars().all()
        assert any("joined" in n.title.lower() for n in notifications)


class TestCooldown:
    async def test_second_call_within_window_is_suppressed(
        self, db_session: AsyncSession, admin_user
    ):
        org_id = await _make_org(db_session, admin_user)
        member_ids = [admin_user["user"].user_id]
        await db_session.commit()

        cooldown_key = f"test-cooldown-{uuid.uuid4()}"
        service = OrgNotificationService(db_session)

        await service.notify(
            org_id=org_id, member_ids=member_ids, pref_key="vote_cast", notif_type="vote",
            title="First", description="first", cooldown_key=cooldown_key, cooldown_seconds=60,
        )
        await service.notify(
            org_id=org_id, member_ids=member_ids, pref_key="vote_cast", notif_type="vote",
            title="Second", description="second", cooldown_key=cooldown_key, cooldown_seconds=60,
        )

        result = await db_session.execute(select(Notification).where(Notification.org_id == org_id))
        notifications = result.scalars().all()
        assert len(notifications) == 1
        assert notifications[0].title == "First"
