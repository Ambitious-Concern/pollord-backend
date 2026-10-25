from datetime import date, time

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.event import Event
from app.models.organization import Organization, OrganizationMember
from app.models.support_ticket import SupportMessage


async def _make_org(db: AsyncSession, owner) -> Organization:
    org = Organization(name="Priority Test Org", owner_id=owner.user_id)
    db.add(org)
    await db.flush()
    db.add(OrganizationMember(org_id=org.org_id, user_id=owner.user_id, role="owner"))
    await db.flush()
    return org


async def _make_premium_event(db: AsyncSession, org: Organization, creator) -> Event:
    event = Event(
        title="Priority Test Event",
        event_date=date(2026, 12, 1),
        event_time=time(19, 0),
        location="Accra",
        status="published",
        created_by=creator.user_id,
        org_id=org.org_id,
        plan="premium",
    )
    db.add(event)
    await db.flush()
    return event


class TestStartChat:
    async def test_start_chat_creates_waiting_ticket(self, client: AsyncClient, test_user):
        res = await client.post(
            "/api/v1/support/chat/start",
            json={"message": "Hi, I need help with my payout"},
            headers=test_user["headers"],
        )
        assert res.status_code == 201, res.text
        body = res.json()
        assert body["channel"] == "chat"
        assert body["status"] == "Waiting"
        assert body["assigned_agent_id"] is None
        assert len(body["messages"]) == 1
        assert body["messages"][0]["text"] == "Hi, I need help with my payout"

    async def test_message_encrypted_at_rest(self, client: AsyncClient, db_session: AsyncSession, test_user):
        secret = "my card number should never be visible in plaintext"
        res = await client.post(
            "/api/v1/support/chat/start", json={"message": secret}, headers=test_user["headers"],
        )
        message_id = res.json()["messages"][0]["message_id"]

        row = (
            await db_session.execute(
                select(SupportMessage.text_encrypted).where(SupportMessage.message_id == message_id)
            )
        ).scalar_one()
        assert secret.encode() not in row


class TestQueueAndClaim:
    async def test_agent_sees_waiting_chat_in_queue(self, client: AsyncClient, test_user, admin_user):
        await client.post(
            "/api/v1/support/chat/start", json={"message": "help"}, headers=test_user["headers"],
        )
        res = await client.get("/api/v1/support/chat/queue/list", headers=admin_user["headers"])
        assert res.status_code == 200
        assert any(t["status"] == "Waiting" for t in res.json())

    async def test_agent_claims_chat(self, client: AsyncClient, test_user, admin_user):
        start_res = await client.post(
            "/api/v1/support/chat/start", json={"message": "help"}, headers=test_user["headers"],
        )
        ticket_id = start_res.json()["ticket_id"]

        claim_res = await client.post(f"/api/v1/support/chat/{ticket_id}/claim", headers=admin_user["headers"])
        assert claim_res.status_code == 200, claim_res.text
        body = claim_res.json()
        assert body["status"] == "Active"
        assert body["assigned_agent_id"] == str(admin_user["user"].user_id)

    async def test_second_claim_rejected(self, client: AsyncClient, db_session, test_user, admin_user):
        from datetime import datetime, timezone

        from app.core.security import hash_password
        from app.models.user import Role, User, UserRole
        from app.core.security import create_access_token
        from sqlalchemy import select as sa_select

        start_res = await client.post(
            "/api/v1/support/chat/start", json={"message": "help"}, headers=test_user["headers"],
        )
        ticket_id = start_res.json()["ticket_id"]

        first_claim = await client.post(f"/api/v1/support/chat/{ticket_id}/claim", headers=admin_user["headers"])
        assert first_claim.status_code == 200

        # A second System Administrator trying to claim the same (now Active) chat.
        other_admin = User(
            email="other-admin@example.com", password_hash=hash_password("Passw0rd!"),
            full_name="Other Admin", email_verified=True, account_status="active",
        )
        db_session.add(other_admin)
        await db_session.flush()
        role = (await db_session.execute(sa_select(Role).where(Role.role_name == "System Administrator"))).scalar_one()
        db_session.add(UserRole(user_id=other_admin.user_id, role_id=role.role_id))
        await db_session.flush()
        other_headers = {"Authorization": f"Bearer {create_access_token(str(other_admin.user_id))}"}

        second_claim = await client.post(f"/api/v1/support/chat/{ticket_id}/claim", headers=other_headers)
        assert second_claim.status_code == 409

    async def test_non_agent_cannot_list_queue_or_claim(self, client: AsyncClient, test_user):
        start_res = await client.post(
            "/api/v1/support/chat/start", json={"message": "help"}, headers=test_user["headers"],
        )
        ticket_id = start_res.json()["ticket_id"]

        list_res = await client.get("/api/v1/support/chat/queue/list", headers=test_user["headers"])
        assert list_res.status_code == 403

        claim_res = await client.post(f"/api/v1/support/chat/{ticket_id}/claim", headers=test_user["headers"])
        assert claim_res.status_code == 403


class TestPriority:
    """Priority support is open to every plan now — every chat queues as
    priority regardless of the org's plan."""

    async def test_standard_org_chat_is_priority(self, client: AsyncClient, test_user):
        res = await client.post(
            "/api/v1/support/chat/start", json={"message": "help"}, headers=test_user["headers"],
        )
        assert res.json()["priority"] is True

    async def test_premium_org_chat_is_priority(
        self, client: AsyncClient, db_session: AsyncSession, test_user
    ):
        org = await _make_org(db_session, test_user["user"])
        await _make_premium_event(db_session, org, test_user["user"])

        res = await client.post(
            "/api/v1/support/chat/start", json={"message": "help"}, headers=test_user["headers"],
        )
        assert res.json()["priority"] is True

    async def test_queue_is_fifo_when_every_chat_is_priority(
        self, client: AsyncClient, db_session: AsyncSession, test_user, admin_user
    ):
        first = await client.post(
            "/api/v1/support/chat/start", json={"message": "first in line"},
            headers=test_user["headers"],
        )
        second = await client.post(
            "/api/v1/support/chat/start", json={"message": "joined later"},
            headers=admin_user["headers"],
        )

        queue = await client.get("/api/v1/support/chat/queue/list", headers=admin_user["headers"])
        ids = [t["ticket_id"] for t in queue.json()]
        assert ids.index(first.json()["ticket_id"]) < ids.index(second.json()["ticket_id"])


class TestMessagingAndClose:
    async def test_message_exchange_and_close(self, client: AsyncClient, test_user, admin_user):
        start_res = await client.post(
            "/api/v1/support/chat/start", json={"message": "help"}, headers=test_user["headers"],
        )
        ticket_id = start_res.json()["ticket_id"]
        await client.post(f"/api/v1/support/chat/{ticket_id}/claim", headers=admin_user["headers"])

        agent_res = await client.post(
            f"/api/v1/support/chat/{ticket_id}/agent-messages",
            json={"text": "Sure, let me look into that"},
            headers=admin_user["headers"],
        )
        assert agent_res.status_code == 200, agent_res.text
        assert agent_res.json()["messages"][-1]["text"] == "Sure, let me look into that"
        assert agent_res.json()["messages"][-1]["sender_type"] == "agent"

        customer_res = await client.post(
            f"/api/v1/support/chat/{ticket_id}/messages",
            json={"text": "Thank you!"},
            headers=test_user["headers"],
        )
        assert customer_res.status_code == 200
        assert len(customer_res.json()["messages"]) == 3

        close_res = await client.post(f"/api/v1/support/chat/{ticket_id}/close", headers=admin_user["headers"])
        assert close_res.status_code == 200
        assert close_res.json()["status"] == "Closed"

    async def test_outsider_cannot_access_others_chat(self, client: AsyncClient, db_session, test_user, admin_user):
        from app.core.security import create_access_token, hash_password
        from app.models.user import Role, User, UserRole
        from sqlalchemy import select as sa_select

        start_res = await client.post(
            "/api/v1/support/chat/start", json={"message": "private stuff"}, headers=test_user["headers"],
        )
        ticket_id = start_res.json()["ticket_id"]

        outsider = User(
            email="chat-outsider@example.com", password_hash=hash_password("Passw0rd!"),
            full_name="Outsider", email_verified=True, account_status="active",
        )
        db_session.add(outsider)
        await db_session.flush()
        role = (await db_session.execute(sa_select(Role).where(Role.role_name == "Voter"))).scalar_one_or_none()
        if not role:
            role = Role(role_name="Voter", permissions={})
            db_session.add(role)
            await db_session.flush()
        db_session.add(UserRole(user_id=outsider.user_id, role_id=role.role_id))
        await db_session.flush()
        outsider_headers = {"Authorization": f"Bearer {create_access_token(str(outsider.user_id))}"}

        res = await client.get(f"/api/v1/support/chat/{ticket_id}", headers=outsider_headers)
        assert res.status_code == 404
