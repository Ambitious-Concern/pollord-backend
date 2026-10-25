"""Danger Zone — transfer ownership + delete (deactivate) organization.

IDs captured into local variables immediately after each DB write; see
test_org_roles.py's module docstring for why (shared-session rollback/
expiry gotchas under this fixture setup)."""
from datetime import datetime, timedelta, timezone

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.election import Election
from app.models.event import Event
from app.models.organization import Organization, OrganizationMember
from app.models.payout_request import PayoutRequest
from app.models.user import User


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
    org = Organization(name="Danger Zone Test Org", owner_id=owner["user"].user_id)
    db.add(org)
    await db.flush()
    org_id = str(org.org_id)
    db.add(OrganizationMember(org_id=org.org_id, user_id=owner["user"].user_id, role="owner"))
    await db.flush()
    return org_id


async def _add_member(db: AsyncSession, org_id: str, user: dict, role: str) -> str:
    member = OrganizationMember(org_id=org_id, user_id=user["user"].user_id, role=role)
    db.add(member)
    await db.flush()
    member_id = str(member.member_id)
    await db.commit()
    return member_id


async def _make_election(db: AsyncSession, org_id: str, owner: dict, status: str) -> str:
    now = datetime.now(timezone.utc)
    election = Election(
        title="Danger Zone Election",
        start_datetime=now,
        end_datetime=now + timedelta(days=1),
        status=status,
        created_by=owner["user"].user_id,
        org_id=org_id,
    )
    db.add(election)
    await db.flush()
    election_id = str(election.election_id)
    await db.commit()
    return election_id


async def _make_pending_payout(db: AsyncSession, election_id: str, owner: dict) -> str:
    payout = PayoutRequest(
        election_id=election_id,
        organizer_id=owner["user"].user_id,
        amount=100,
        status="pending",
    )
    db.add(payout)
    await db.flush()
    payout_id = str(payout.payout_request_id)
    await db.commit()
    return payout_id


class TestTransferOwnership:
    async def test_owner_transfers_to_member(self, client: AsyncClient, db_session, admin_user):
        org_id = await _make_org(db_session, admin_user)
        successor = await _make_user(db_session, "successor@example.com")
        member_id = await _add_member(db_session, org_id, successor, role="admin")

        res = await client.put(
            f"/api/v1/organizations/{org_id}/transfer-ownership",
            json={"new_owner_member_id": member_id},
            headers=admin_user["headers"],
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["owner_id"] == str(successor["user"].user_id)

        members_by_user = {m["user_id"]: m["role"] for m in body["members"]}
        assert members_by_user[str(successor["user"].user_id)] == "owner"
        assert members_by_user[str(admin_user["user"].user_id)] == "admin"

    async def test_non_owner_cannot_transfer(self, client: AsyncClient, db_session, admin_user):
        org_id = await _make_org(db_session, admin_user)
        admin_member = await _make_user(db_session, "admin-member@example.com")
        member_id = await _add_member(db_session, org_id, admin_member, role="admin")

        res = await client.put(
            f"/api/v1/organizations/{org_id}/transfer-ownership",
            json={"new_owner_member_id": member_id},
            headers=admin_member["headers"],
        )
        assert res.status_code == 403

    async def test_transfer_to_nonmember_rejected(self, client: AsyncClient, db_session, admin_user):
        org_id = await _make_org(db_session, admin_user)
        import uuid

        res = await client.put(
            f"/api/v1/organizations/{org_id}/transfer-ownership",
            json={"new_owner_member_id": str(uuid.uuid4())},
            headers=admin_user["headers"],
        )
        assert res.status_code == 404


class TestDeleteOrganization:
    async def test_owner_deletes_organization(self, client: AsyncClient, db_session, admin_user):
        org_id = await _make_org(db_session, admin_user)

        res = await client.delete(f"/api/v1/organizations/{org_id}", headers=admin_user["headers"])
        assert res.status_code == 204, res.text

        get_res = await client.get(f"/api/v1/organizations/{org_id}", headers=admin_user["headers"])
        assert get_res.status_code == 404

        listed = await client.get("/api/v1/organizations/my", headers=admin_user["headers"])
        assert listed.status_code == 200
        assert all(o["org_id"] != org_id for o in listed.json())

    async def test_non_owner_cannot_delete(self, client: AsyncClient, db_session, admin_user):
        org_id = await _make_org(db_session, admin_user)
        admin_member = await _make_user(db_session, "cannot-delete@example.com")
        await _add_member(db_session, org_id, admin_member, role="admin")

        res = await client.delete(f"/api/v1/organizations/{org_id}", headers=admin_member["headers"])
        assert res.status_code == 403

    async def test_blocked_by_active_election(self, client: AsyncClient, db_session, admin_user):
        org_id = await _make_org(db_session, admin_user)
        await _make_election(db_session, org_id, admin_user, status="active")

        res = await client.delete(f"/api/v1/organizations/{org_id}", headers=admin_user["headers"])
        assert res.status_code == 400

    async def test_blocked_by_pending_payout(self, client: AsyncClient, db_session, admin_user):
        org_id = await _make_org(db_session, admin_user)
        election_id = await _make_election(db_session, org_id, admin_user, status="closed")
        await _make_pending_payout(db_session, election_id, admin_user)

        res = await client.delete(f"/api/v1/organizations/{org_id}", headers=admin_user["headers"])
        assert res.status_code == 400

    async def test_draft_election_does_not_block_delete(self, client: AsyncClient, db_session, admin_user):
        org_id = await _make_org(db_session, admin_user)
        await _make_election(db_session, org_id, admin_user, status="draft")

        res = await client.delete(f"/api/v1/organizations/{org_id}", headers=admin_user["headers"])
        assert res.status_code == 204
