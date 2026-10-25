"""Org-scoped custom roles (OrgRole) — CRUD, assignment to a member, and
real enforcement of the manage_team permission flag at the actual
invite/remove endpoints (not just a decorative CRUD surface).

IDs are captured into local variables immediately after each DB write and
reused from there — the shared db_session/app-session pattern means an app
request's own commit() expires every previously-loaded ORM object, and
touching an expired attribute afterwards triggers a synchronous lazy-load
that crashes under the async engine (MissingGreenlet)."""
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.organization import Organization, OrganizationMember
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
    org = Organization(name="Custom Roles Test Org", owner_id=owner["user"].user_id)
    db.add(org)
    await db.flush()
    org_id = str(org.org_id)
    db.add(OrganizationMember(org_id=org.org_id, user_id=owner["user"].user_id, role="owner"))
    await db.flush()
    return org_id


async def _add_member(db: AsyncSession, org_id: str, user: dict, role: str, org_role_id=None) -> str:
    member = OrganizationMember(org_id=org_id, user_id=user["user"].user_id, role=role, org_role_id=org_role_id)
    db.add(member)
    await db.flush()
    member_id = str(member.member_id)
    # A later request in the same test that raises (e.g. an expected 403)
    # triggers the app-side session's rollback(), which — under this
    # fixture's "join an outer SAVEPOINT" pattern — undoes everything since
    # the last commit boundary. Committing here protects this fixture row
    # from being wiped out by an unrelated failed request later in the test.
    await db.commit()
    return member_id


class TestOrgRoleCrud:
    async def test_create_list_role(self, client: AsyncClient, db_session, admin_user):
        org_id = await _make_org(db_session, admin_user)
        res = await client.post(
            f"/api/v1/organizations/{org_id}/roles",
            json={"name": "Finance Viewer", "permissions": ["view_financials"]},
            headers=admin_user["headers"],
        )
        assert res.status_code == 201, res.text
        body = res.json()
        assert body["name"] == "Finance Viewer"
        assert body["permissions"] == ["view_financials"]

        listed = await client.get(f"/api/v1/organizations/{org_id}/roles", headers=admin_user["headers"])
        assert listed.status_code == 200
        assert len(listed.json()) == 1

    async def test_duplicate_name_rejected(self, client: AsyncClient, db_session, admin_user):
        org_id = await _make_org(db_session, admin_user)
        payload = {"name": "Ops", "permissions": []}
        first = await client.post(f"/api/v1/organizations/{org_id}/roles", json=payload, headers=admin_user["headers"])
        assert first.status_code == 201
        second = await client.post(f"/api/v1/organizations/{org_id}/roles", json=payload, headers=admin_user["headers"])
        assert second.status_code == 409

    async def test_invalid_permission_rejected(self, client: AsyncClient, db_session, admin_user):
        org_id = await _make_org(db_session, admin_user)
        res = await client.post(
            f"/api/v1/organizations/{org_id}/roles",
            json={"name": "Bad Role", "permissions": ["delete_everything"]},
            headers=admin_user["headers"],
        )
        assert res.status_code == 422

    async def test_update_and_delete_role(self, client: AsyncClient, db_session, admin_user):
        org_id = await _make_org(db_session, admin_user)
        create_res = await client.post(
            f"/api/v1/organizations/{org_id}/roles",
            json={"name": "Support", "permissions": []},
            headers=admin_user["headers"],
        )
        role_id = create_res.json()["org_role_id"]

        update_res = await client.put(
            f"/api/v1/organizations/{org_id}/roles/{role_id}",
            json={"name": "Support Lead", "permissions": ["manage_team"]},
            headers=admin_user["headers"],
        )
        assert update_res.status_code == 200
        assert update_res.json()["name"] == "Support Lead"
        assert update_res.json()["permissions"] == ["manage_team"]

        delete_res = await client.delete(
            f"/api/v1/organizations/{org_id}/roles/{role_id}", headers=admin_user["headers"]
        )
        assert delete_res.status_code == 204

    async def test_non_member_cannot_manage_roles(self, client: AsyncClient, db_session, admin_user):
        org_id = await _make_org(db_session, admin_user)
        outsider = await _make_user(db_session, "outsider-roles@example.com")
        res = await client.post(
            f"/api/v1/organizations/{org_id}/roles",
            json={"name": "Sneaky", "permissions": []},
            headers=outsider["headers"],
        )
        assert res.status_code == 403


class TestOrgRoleAssignmentAndEnforcement:
    async def test_assign_role_to_member(self, client: AsyncClient, db_session, admin_user):
        org_id = await _make_org(db_session, admin_user)
        plain_member = await _make_user(db_session, "plain-member@example.com")
        member_id = await _add_member(db_session, org_id, plain_member, role="member")

        role_res = await client.post(
            f"/api/v1/organizations/{org_id}/roles",
            json={"name": "Team Manager", "permissions": ["manage_team"]},
            headers=admin_user["headers"],
        )
        role_id = role_res.json()["org_role_id"]

        assign_res = await client.put(
            f"/api/v1/organizations/{org_id}/members/{member_id}",
            json={"role": "member", "org_role_id": role_id},
            headers=admin_user["headers"],
        )
        assert assign_res.status_code == 200, assign_res.text
        assert assign_res.json()["org_role"]["name"] == "Team Manager"

    async def test_member_with_manage_team_can_invite(self, client: AsyncClient, db_session, admin_user):
        """The actual enforcement check — a plain 'member' with no custom
        role cannot invite; the same member granted a manage_team custom
        role can. Proves the permission flag isn't decorative."""
        org_id = await _make_org(db_session, admin_user)
        plain_member = await _make_user(db_session, "delegate@example.com")

        role_res = await client.post(
            f"/api/v1/organizations/{org_id}/roles",
            json={"name": "Recruiter", "permissions": ["manage_team"]},
            headers=admin_user["headers"],
        )
        role_id = role_res.json()["org_role_id"]

        member_id = await _add_member(db_session, org_id, plain_member, role="member")

        # Before assignment: a plain member cannot invite.
        denied = await client.post(
            f"/api/v1/organizations/{org_id}/invite",
            json={"email": "new-hire@example.com", "role": "member"},
            headers=plain_member["headers"],
        )
        assert denied.status_code == 403

        assign = await client.put(
            f"/api/v1/organizations/{org_id}/members/{member_id}",
            json={"role": "member", "org_role_id": role_id},
            headers=admin_user["headers"],
        )
        assert assign.status_code == 200

        allowed = await client.post(
            f"/api/v1/organizations/{org_id}/invite",
            json={"email": "new-hire@example.com", "role": "member"},
            headers=plain_member["headers"],
        )
        assert allowed.status_code == 201, allowed.text

    async def test_member_without_manage_team_still_rejected_for_other_permission(
        self, client: AsyncClient, db_session, admin_user
    ):
        org_id = await _make_org(db_session, admin_user)
        plain_member = await _make_user(db_session, "finance-only@example.com")

        role_res = await client.post(
            f"/api/v1/organizations/{org_id}/roles",
            json={"name": "Finance", "permissions": ["view_financials"]},
            headers=admin_user["headers"],
        )
        role_id = role_res.json()["org_role_id"]
        await _add_member(db_session, org_id, plain_member, role="member", org_role_id=role_id)

        res = await client.post(
            f"/api/v1/organizations/{org_id}/invite",
            json={"email": "someone@example.com", "role": "member"},
            headers=plain_member["headers"],
        )
        assert res.status_code == 403


class TestPendingInvitations:
    async def test_list_and_revoke(self, client: AsyncClient, db_session, admin_user):
        org_id = await _make_org(db_session, admin_user)
        invite_res = await client.post(
            f"/api/v1/organizations/{org_id}/invite",
            json={"email": "pending-guy@example.com", "role": "member"},
            headers=admin_user["headers"],
        )
        assert invite_res.status_code == 201

        listed = await client.get(f"/api/v1/organizations/{org_id}/invitations", headers=admin_user["headers"])
        assert listed.status_code == 200
        assert len(listed.json()) == 1
        invitation_id = listed.json()[0]["invitation_id"]

        revoke = await client.delete(
            f"/api/v1/organizations/{org_id}/invitations/{invitation_id}", headers=admin_user["headers"]
        )
        assert revoke.status_code == 204

        listed_again = await client.get(f"/api/v1/organizations/{org_id}/invitations", headers=admin_user["headers"])
        assert listed_again.json() == []
