from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

import app.core.redis as redis_module
from app.core.config import settings
from app.models.election import Candidate, Category, Election

WEBHOOK_URL = "/api/v1/ussd/arkesel/callback"
TOKEN = "test-ussd-token"


@pytest.fixture(autouse=True)
def _set_webhook_token(monkeypatch):
    monkeypatch.setattr(settings, "ARKESEL_WEBHOOK_TOKEN", TOKEN)


@pytest.fixture(autouse=True)
async def _fresh_redis_client():
    """The module-level Redis client in app.core.redis is a singleton meant
    to live for one process's lifetime — it doesn't survive pytest-asyncio
    creating a new event loop per test. Force a fresh connection each test
    instead of reusing one bound to an already-closed loop. Also clear any
    ussd:* keys left over from a previous run — Redis isn't wrapped in the
    same per-test rollback the Postgres test DB gets, so stale session state
    for a reused phone number would otherwise leak across test runs."""
    redis_module._client = None
    redis = await redis_module.get_redis()
    keys = await redis.keys("ussd:*")
    if keys:
        await redis.delete(*keys)
    yield
    if redis_module._client is not None:
        keys = await redis_module._client.keys("ussd:*")
        if keys:
            await redis_module._client.delete(*keys)
        await redis_module._client.aclose()
        redis_module._client = None


@pytest.fixture
async def active_election_with_candidate(db_session: AsyncSession, admin_user):
    now = datetime.now(timezone.utc)
    election = Election(
        title="USSD Test Election",
        ussd_code="482913",
        start_datetime=now - timedelta(hours=1),
        end_datetime=now + timedelta(hours=23),
        status="active",
        visibility="public",
        require_verification=False,
        vote_price=0,
        created_by=admin_user["user"].user_id,
    )
    db_session.add(election)
    await db_session.flush()

    category = Category(
        election_id=election.election_id,
        name="President",
        election_type="single_choice",
        display_order=0,
    )
    db_session.add(category)
    await db_session.flush()

    candidate = Candidate(
        category_id=category.category_id,
        election_id=election.election_id,
        name="Anne",
        short_code="ANNE",
        display_order=0,
    )
    db_session.add(candidate)
    await db_session.flush()

    return election, category, candidate


def _ussd_post(text: str, phone: str = "233241234567", session_id: str = "sess-1"):
    return {
        "sessionId": session_id,
        "serviceCode": "*928*928#",
        "phoneNumber": phone,
        "text": text,
    }


@pytest.mark.asyncio
class TestUssdWebhookSecurity:
    async def test_missing_token_rejected(self, client: AsyncClient):
        response = await client.post(WEBHOOK_URL, data=_ussd_post(""))
        assert response.status_code == 403

    async def test_wrong_token_rejected(self, client: AsyncClient):
        response = await client.post(
            f"{WEBHOOK_URL}?token=wrong", data=_ussd_post("")
        )
        assert response.status_code == 403

    async def test_correct_token_accepted(self, client: AsyncClient):
        response = await client.post(f"{WEBHOOK_URL}?token={TOKEN}", data=_ussd_post(""))
        assert response.status_code == 200
        assert response.text.startswith("CON ")


@pytest.mark.asyncio
class TestUssdVotingFlow:
    async def test_free_vote_end_to_end(
        self, client: AsyncClient, active_election_with_candidate
    ):
        election, category, candidate = active_election_with_candidate

        # Step 1: dial in, no input yet
        r1 = await client.post(f"{WEBHOOK_URL}?token={TOKEN}", data=_ussd_post(""))
        assert r1.status_code == 200
        assert r1.text.startswith("CON ")

        # Step 2: enter the election's USSD code — single category, goes
        # straight to the ballot
        r2 = await client.post(
            f"{WEBHOOK_URL}?token={TOKEN}", data=_ussd_post(election.ussd_code)
        )
        assert r2.status_code == 200
        assert r2.text.startswith("CON ")
        assert candidate.short_code in r2.text

        # Step 3: enter the candidate's short code (Arkesel sends cumulative
        # text: "482913*ANNE")
        r3 = await client.post(
            f"{WEBHOOK_URL}?token={TOKEN}",
            data=_ussd_post(f"{election.ussd_code}*{candidate.short_code}"),
        )
        assert r3.status_code == 200
        assert r3.text.startswith("END ")
        assert "Vote cast" in r3.text

    async def test_duplicate_vote_rejected(
        self, client: AsyncClient, active_election_with_candidate
    ):
        election, category, candidate = active_election_with_candidate
        phone = "233241234568"

        await client.post(f"{WEBHOOK_URL}?token={TOKEN}", data=_ussd_post("", phone))
        await client.post(
            f"{WEBHOOK_URL}?token={TOKEN}", data=_ussd_post(election.ussd_code, phone)
        )
        first = await client.post(
            f"{WEBHOOK_URL}?token={TOKEN}",
            data=_ussd_post(f"{election.ussd_code}*{candidate.short_code}", phone),
        )
        assert "Vote cast" in first.text

        # Same phone, fresh session — should be rejected as already voted
        await client.post(f"{WEBHOOK_URL}?token={TOKEN}", data=_ussd_post("", phone))
        second = await client.post(
            f"{WEBHOOK_URL}?token={TOKEN}", data=_ussd_post(election.ussd_code, phone)
        )
        assert second.text.startswith("END ")
        assert "already voted" in second.text

    async def test_unknown_code_prompts_retry(self, client: AsyncClient):
        response = await client.post(
            f"{WEBHOOK_URL}?token={TOKEN}", data=_ussd_post("000000")
        )
        assert response.status_code == 200
        assert response.text.startswith("CON ")
        assert "not found" in response.text.lower()

    async def test_unknown_candidate_code_reprompts(
        self, client: AsyncClient, active_election_with_candidate
    ):
        election, category, candidate = active_election_with_candidate
        await client.post(f"{WEBHOOK_URL}?token={TOKEN}", data=_ussd_post(""))
        await client.post(
            f"{WEBHOOK_URL}?token={TOKEN}", data=_ussd_post(election.ussd_code)
        )
        response = await client.post(
            f"{WEBHOOK_URL}?token={TOKEN}",
            data=_ussd_post(f"{election.ussd_code}*ZZZZ"),
        )
        assert response.status_code == 200
        assert response.text.startswith("CON ")


@pytest.mark.asyncio
class TestUssdCodeGeneration:
    async def test_create_election_gets_ussd_code(self, client: AsyncClient, admin_user):
        response = await client.post(
            "/api/v1/elections/",
            json={
                "title": "Code Gen Test Election",
                "start_datetime": "2026-06-01T08:00:00Z",
                "end_datetime": "2026-06-01T20:00:00Z",
            },
            headers=admin_user["headers"],
        )
        assert response.status_code == 201
        code = response.json()["ussd_code"]
        assert code is not None
        assert code.isdigit()
        assert len(code) == 6

    async def test_create_event_gets_ussd_code(self, client: AsyncClient, admin_user):
        response = await client.post(
            "/api/v1/events/",
            json={
                "title": "Code Gen Test Event",
                "event_date": "2026-06-01",
                "event_time": "18:00:00",
                "location": "Accra",
            },
            headers=admin_user["headers"],
        )
        assert response.status_code == 201
        code = response.json()["ussd_code"]
        assert code is not None
        assert code.isdigit()
        assert len(code) == 6
