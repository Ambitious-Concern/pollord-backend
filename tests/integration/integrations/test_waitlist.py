"""
Integration tests for the pre-launch waitlist feature.

Covers:
  1. Public subscribe: happy path, case/whitespace dedupe, invalid email
  2. Admin-only announce: auth required, role required
  3. Announce sends to pending subscribers and stamps notified_at
  4. Announce is re-runnable (no new emails once everyone is notified)
  5. Concurrent announce calls never double-send (Finding 1 regression test)

send_email is monkeypatched at the module level used by
app/api/v1/endpoints/waitlist.py so no real SMTP traffic happens.
"""

import asyncio
from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.db.base import get_db
from app.main import app
from app.models.audit_log import AuditLog
from app.models.user import Role, User, UserRole
from app.models.waitlist import WaitlistSubscriber
from tests.integration.conftest import test_session_maker

SUBSCRIBE_URL = "/api/v1/waitlist/subscribe"
ANNOUNCE_URL = "/api/v1/waitlist/announce"

SEND_EMAIL_PATH = "app.api.v1.endpoints.waitlist.send_email"

# Emails used by the shared `admin_user`/`test_user` conftest fixtures. Unlike
# most other endpoints under test elsewhere in this suite, /subscribe and
# /announce call `db.commit()` themselves, which permanently persists
# whatever the *same* session had flushed earlier in the test -- including
# these fixture users. That means a second test in this module that requests
# admin_user/test_user again would hit a duplicate-email IntegrityError. Purge
# them (and their dependent rows) before/after every test in this module so
# each test gets a clean insert, regardless of commits made by earlier tests.
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
async def clean_waitlist(db_session: AsyncSession):
    """
    /announce operates on the whole waitlist_subscribers table (by design),
    so leftover committed rows from other tests would break exact sent/
    skipped assertions. Keep the table empty, and the fixture users free of
    stale commits, on the way in and out of every test in this module.
    """
    await db_session.execute(delete(WaitlistSubscriber))
    await _purge_fixture_users(db_session)
    await db_session.commit()
    yield
    await db_session.execute(delete(WaitlistSubscriber))
    await _purge_fixture_users(db_session)
    await db_session.commit()


@pytest.mark.asyncio
class TestSubscribe:
    async def test_subscribe_happy_path(self, client: AsyncClient, db_session: AsyncSession):
        with patch(SEND_EMAIL_PATH, return_value=True) as mock_send:
            response = await client.post(SUBSCRIBE_URL, json={"email": "newsub@example.com"})

        assert response.status_code == 200
        assert "on the list" in response.json()["message"].lower()
        mock_send.assert_called_once()
        assert mock_send.call_args[0][0] == "newsub@example.com"

        result = await db_session.execute(
            select(WaitlistSubscriber).where(WaitlistSubscriber.email == "newsub@example.com")
        )
        row = result.scalar_one()
        assert row.notified_at is None

    async def test_subscribe_dedupes_case_and_whitespace(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        db_session.add(WaitlistSubscriber(email="existing@example.com"))
        await db_session.flush()

        with patch(SEND_EMAIL_PATH, return_value=True) as mock_send:
            response = await client.post(
                SUBSCRIBE_URL, json={"email": "  Existing@Example.com  "}
            )

        assert response.status_code == 200
        assert "already on the list" in response.json()["message"].lower()
        mock_send.assert_not_called()

        result = await db_session.execute(
            select(WaitlistSubscriber).where(WaitlistSubscriber.email == "existing@example.com")
        )
        rows = result.scalars().all()
        assert len(rows) == 1

    async def test_subscribe_invalid_email(self, client: AsyncClient):
        response = await client.post(SUBSCRIBE_URL, json={"email": "not-an-email"})
        assert response.status_code == 422


@pytest.mark.asyncio
class TestAnnounceAuthorization:
    async def test_announce_requires_auth(self, client: AsyncClient):
        response = await client.post(ANNOUNCE_URL)
        assert response.status_code == 401

    async def test_announce_forbidden_for_non_admin(self, client: AsyncClient, test_user):
        response = await client.post(ANNOUNCE_URL, headers=test_user["headers"])
        assert response.status_code == 403


@pytest.mark.asyncio
class TestAnnounce:
    async def test_announce_sends_to_pending_and_stamps_notified_at(
        self, client: AsyncClient, db_session: AsyncSession, admin_user
    ):
        db_session.add_all(
            [
                WaitlistSubscriber(email="a@example.com"),
                WaitlistSubscriber(email="b@example.com"),
            ]
        )
        await db_session.flush()

        with patch(SEND_EMAIL_PATH, return_value=True) as mock_send:
            response = await client.post(ANNOUNCE_URL, headers=admin_user["headers"])

        assert response.status_code == 200
        data = response.json()
        assert data["sent"] == 2
        assert data["skipped"] == 0
        assert mock_send.call_count == 2

        result = await db_session.execute(
            select(WaitlistSubscriber).where(
                WaitlistSubscriber.email.in_(["a@example.com", "b@example.com"])
            )
        )
        rows = result.scalars().all()
        assert len(rows) == 2
        assert all(r.notified_at is not None for r in rows)

    async def test_announce_rerun_no_new_pending(
        self, client: AsyncClient, db_session: AsyncSession, admin_user
    ):
        db_session.add(WaitlistSubscriber(email="c@example.com"))
        await db_session.flush()

        with patch(SEND_EMAIL_PATH, return_value=True) as mock_send:
            first = await client.post(ANNOUNCE_URL, headers=admin_user["headers"])
        assert first.status_code == 200
        assert first.json()["sent"] == 1
        mock_send.assert_called_once()

        with patch(SEND_EMAIL_PATH, return_value=True) as mock_send_again:
            second = await client.post(ANNOUNCE_URL, headers=admin_user["headers"])

        assert second.status_code == 200
        assert second.json()["sent"] == 0
        assert second.json()["skipped"] == 1
        mock_send_again.assert_not_called()


@pytest.mark.asyncio
class TestAnnounceConcurrency:
    """
    Regression test for Finding 1: two concurrent admin POST /announce calls
    against the same pending set must never email the same subscriber twice.

    This needs two genuinely separate DB connections/transactions (not the
    single shared `db_session` fixture) so that `with_for_update(skip_locked=True)`
    actually has two competing transactions to arbitrate between. Test data is
    committed directly via a fresh session and cleaned up afterwards so it
    doesn't leak into other tests.
    """

    async def test_concurrent_announce_does_not_double_send(self):
        emails = [f"concurrent{i}@example.com" for i in range(3)]

        async with test_session_maker() as seed_session:
            role_result = await seed_session.execute(
                select(Role).where(Role.role_name == "System Administrator")
            )
            role = role_result.scalar_one_or_none()
            if role is None:
                role = Role(role_name="System Administrator", permissions={"admin": ["full"]})
                seed_session.add(role)
                await seed_session.flush()

            admin = User(
                email="concurrent-admin@example.com",
                password_hash=hash_password("Admin1234!"),
                full_name="Concurrent Admin",
                email_verified=True,
                account_status="active",
            )
            seed_session.add(admin)
            await seed_session.flush()
            seed_session.add(UserRole(user_id=admin.user_id, role_id=role.role_id))

            for email in emails:
                seed_session.add(WaitlistSubscriber(email=email))

            await seed_session.commit()
            admin_id = admin.user_id
            token = create_access_token(str(admin_id))

        headers = {"Authorization": f"Bearer {token}"}

        async def override_get_db():
            async with test_session_maker() as session:
                yield session

        app.dependency_overrides[get_db] = override_get_db

        sent_to: list[str] = []

        def fake_send_email(to, subject, html_body):
            sent_to.append(to)
            return True

        try:
            with patch(SEND_EMAIL_PATH, side_effect=fake_send_email):
                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test"
                ) as ac1, AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test"
                ) as ac2:
                    resp1, resp2 = await asyncio.gather(
                        ac1.post(ANNOUNCE_URL, headers=headers),
                        ac2.post(ANNOUNCE_URL, headers=headers),
                    )
        finally:
            app.dependency_overrides.clear()
            async with test_session_maker() as cleanup_session:
                await cleanup_session.execute(
                    delete(WaitlistSubscriber).where(WaitlistSubscriber.email.in_(emails))
                )
                await cleanup_session.execute(
                    delete(AuditLog).where(AuditLog.user_id == admin_id)
                )
                await cleanup_session.execute(
                    delete(UserRole).where(UserRole.user_id == admin_id)
                )
                await cleanup_session.execute(delete(User).where(User.user_id == admin_id))
                await cleanup_session.commit()

        assert resp1.status_code == 200
        assert resp2.status_code == 200

        total_sent = resp1.json()["sent"] + resp2.json()["sent"]
        assert total_sent == len(emails)
        assert len(sent_to) == len(emails)
        assert len(set(sent_to)) == len(emails)  # nobody emailed twice
