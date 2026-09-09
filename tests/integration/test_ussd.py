from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

import app.core.redis as redis_module
from app.core.config import settings
from app.models.election import Candidate, Category, Election
from app.services.paystack_service import PaystackService
from app.services.ussd_conversation_service import _detect_network

WEBHOOK_URL = "/api/v1/ussd/arkesel/callback"
TOKEN = "test-ussd-token"


class TestDetectNetwork:
    @pytest.mark.parametrize(
        "phone,expected_provider",
        [
            ("233241234567", "mtn"),
            ("0241234567", "mtn"),
            ("241234567", "mtn"),
            ("233201234567", "vod"),
            ("233501234567", "vod"),
            ("233261234567", "atl"),
            ("233571234567", "atl"),
        ],
    )
    def test_recognized_prefixes(self, phone, expected_provider):
        result = _detect_network(phone)
        assert result is not None
        assert result[0] == expected_provider

    def test_unrecognized_prefix_returns_none(self):
        assert _detect_network("233991234567") is None


@pytest.fixture(autouse=True)
def _set_webhook_token(monkeypatch):
    monkeypatch.setattr(settings, "ARKESEL_WEBHOOK_TOKEN", TOKEN)


@pytest.fixture(autouse=True)
async def _fresh_redis_client():
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


def _ussd_post(
    user_data: str,
    phone: str = "233241234567",
    session_id: str = "sess-1",
    new_session: bool = False,
):
    return {
        "sessionID": session_id,
        "userID": "user-1",
        "msisdn": phone,
        "newSession": new_session,
        "userData": user_data,
    }


@pytest.mark.asyncio
class TestUssdWebhookSecurity:
    async def test_missing_token_rejected(self, client: AsyncClient):
        response = await client.post(WEBHOOK_URL, json=_ussd_post("*928*928#", new_session=True))
        assert response.status_code == 403

    async def test_wrong_token_rejected(self, client: AsyncClient):
        response = await client.post(
            f"{WEBHOOK_URL}?token=wrong", json=_ussd_post("*928*928#", new_session=True)
        )
        assert response.status_code == 403

    async def test_correct_token_accepted(self, client: AsyncClient):
        response = await client.post(
            f"{WEBHOOK_URL}?token={TOKEN}", json=_ussd_post("*928*928#", new_session=True)
        )
        assert response.status_code == 200
        body = response.json()
        assert body["continueSession"] is True
        assert "Welcome" in body["message"]


@pytest.mark.asyncio
class TestUssdVotingFlow:
    async def test_free_vote_end_to_end(
        self, client: AsyncClient, active_election_with_candidate
    ):
        election, category, candidate = active_election_with_candidate

        r1 = await client.post(
            f"{WEBHOOK_URL}?token={TOKEN}", json=_ussd_post("*928*928#", new_session=True)
        )
        assert r1.status_code == 200
        assert r1.json()["continueSession"] is True

        r2 = await client.post(
            f"{WEBHOOK_URL}?token={TOKEN}", json=_ussd_post(election.ussd_code)
        )
        assert r2.status_code == 200
        body2 = r2.json()
        assert body2["continueSession"] is True
        assert candidate.short_code in body2["message"]

        r3 = await client.post(
            f"{WEBHOOK_URL}?token={TOKEN}", json=_ussd_post(candidate.short_code)
        )
        assert r3.status_code == 200
        body3 = r3.json()
        assert body3["continueSession"] is False
        assert "Vote cast" in body3["message"]

    async def test_duplicate_vote_rejected(
        self, client: AsyncClient, active_election_with_candidate
    ):
        election, category, candidate = active_election_with_candidate
        phone = "233241234568"

        await client.post(
            f"{WEBHOOK_URL}?token={TOKEN}",
            json=_ussd_post("*928*928#", phone, new_session=True),
        )
        await client.post(
            f"{WEBHOOK_URL}?token={TOKEN}", json=_ussd_post(election.ussd_code, phone)
        )
        first = await client.post(
            f"{WEBHOOK_URL}?token={TOKEN}", json=_ussd_post(candidate.short_code, phone)
        )
        assert "Vote cast" in first.json()["message"]

        await client.post(
            f"{WEBHOOK_URL}?token={TOKEN}",
            json=_ussd_post("*928*928#", phone, new_session=True),
        )
        second = await client.post(
            f"{WEBHOOK_URL}?token={TOKEN}", json=_ussd_post(election.ussd_code, phone)
        )
        body2 = second.json()
        assert body2["continueSession"] is False
        assert "already voted" in body2["message"]

    async def test_unknown_code_prompts_retry(self, client: AsyncClient):
        response = await client.post(
            f"{WEBHOOK_URL}?token={TOKEN}", json=_ussd_post("000000")
        )
        assert response.status_code == 200
        body = response.json()
        assert body["continueSession"] is True
        assert "not found" in body["message"].lower()

    async def test_unknown_candidate_code_reprompts(
        self, client: AsyncClient, active_election_with_candidate
    ):
        election, category, candidate = active_election_with_candidate
        await client.post(
            f"{WEBHOOK_URL}?token={TOKEN}", json=_ussd_post("*928*928#", new_session=True)
        )
        await client.post(
            f"{WEBHOOK_URL}?token={TOKEN}", json=_ussd_post(election.ussd_code)
        )
        response = await client.post(
            f"{WEBHOOK_URL}?token={TOKEN}", json=_ussd_post("ZZZZ")
        )
        assert response.status_code == 200
        assert response.json()["continueSession"] is True


@pytest.fixture
async def paid_election_with_candidate(db_session: AsyncSession, admin_user):
    now = datetime.now(timezone.utc)
    election = Election(
        title="USSD Paid Election",
        ussd_code="713055",
        start_datetime=now - timedelta(hours=1),
        end_datetime=now + timedelta(hours=23),
        status="active",
        visibility="public",
        require_verification=False,
        vote_price=50,
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
        name="Bea",
        short_code="BEA1",
        display_order=0,
    )
    db_session.add(candidate)
    await db_session.flush()

    return election, category, candidate


@pytest.mark.asyncio
class TestUssdPaidVoteFlow:
    async def _dial_to_vote_count_prompt(self, client, election, candidate, phone):
        await client.post(
            f"{WEBHOOK_URL}?token={TOKEN}",
            json=_ussd_post("*928*928#", phone, new_session=True),
        )
        await client.post(
            f"{WEBHOOK_URL}?token={TOKEN}", json=_ussd_post(election.ussd_code, phone)
        )
        return await client.post(
            f"{WEBHOOK_URL}?token={TOKEN}", json=_ussd_post(candidate.short_code, phone)
        )

    async def _dial_to_network_prompt(self, client, election, candidate, phone, count="1"):
        await self._dial_to_vote_count_prompt(client, election, candidate, phone)
        return await client.post(
            f"{WEBHOOK_URL}?token={TOKEN}", json=_ussd_post(count, phone)
        )

    async def test_vote_count_prompt_appears_after_candidate_code(
        self, client: AsyncClient, paid_election_with_candidate
    ):
        election, category, candidate = paid_election_with_candidate
        phone = "233241234569"

        response = await self._dial_to_vote_count_prompt(
            client, election, candidate, phone
        )
        body = response.json()
        assert body["continueSession"] is True
        assert "how many votes" in body["message"].lower()

    async def test_vote_count_multiplies_charge_amount(
        self, client: AsyncClient, paid_election_with_candidate, monkeypatch
    ):
        election, category, candidate = paid_election_with_candidate
        phone = "233299234572"  # unrecognized prefix -> exercises the manual network menu
        charged = {}

        async def fake_charge(self, **kwargs):
            charged["amount"] = kwargs["amount"]
            return {"status": "pay_offline"}

        monkeypatch.setattr(PaystackService, "charge_mobile_money", fake_charge)

        network_prompt = await self._dial_to_network_prompt(
            client, election, candidate, phone, count="5"
        )
        body = network_prompt.json()
        assert "5 vote" in body["message"]

        await client.post(f"{WEBHOOK_URL}?token={TOKEN}", json=_ussd_post("1", phone))
        assert charged["amount"] == 250  # 5 votes * 50 pesewas

    async def test_invalid_vote_count_reprompts(
        self, client: AsyncClient, paid_election_with_candidate
    ):
        election, category, candidate = paid_election_with_candidate
        phone = "233241234573"

        await self._dial_to_vote_count_prompt(client, election, candidate, phone)
        response = await client.post(
            f"{WEBHOOK_URL}?token={TOKEN}", json=_ussd_post("0", phone)
        )
        body = response.json()
        assert body["continueSession"] is True
        assert "enter a number" in body["message"].lower()

    async def test_recognized_prefix_skips_network_menu(
        self, client: AsyncClient, paid_election_with_candidate, monkeypatch
    ):
        election, category, candidate = paid_election_with_candidate
        phone = "233241234599"  # 024 -> MTN
        charged = {}

        async def fake_charge(self, **kwargs):
            charged["provider"] = kwargs["provider"]
            return {"status": "pay_offline"}

        monkeypatch.setattr(PaystackService, "charge_mobile_money", fake_charge)

        response = await self._dial_to_network_prompt(client, election, candidate, phone)
        body = response.json()
        assert body["continueSession"] is False
        assert "Choose payment network" not in body["message"]
        assert charged["provider"] == "mtn"

    async def test_pay_offline_ends_session_immediately(
        self, client: AsyncClient, paid_election_with_candidate, monkeypatch
    ):
        election, category, candidate = paid_election_with_candidate
        phone = "233299234570"  # unrecognized prefix -> exercises the manual network menu

        async def fake_charge(self, **kwargs):
            return {"status": "pay_offline"}

        monkeypatch.setattr(PaystackService, "charge_mobile_money", fake_charge)

        network_prompt = await self._dial_to_network_prompt(
            client, election, candidate, phone
        )
        assert "Bea" in network_prompt.json()["message"]

        response = await client.post(
            f"{WEBHOOK_URL}?token={TOKEN}", json=_ussd_post("1", phone)
        )
        body = response.json()
        assert body["continueSession"] is False
        assert "approve" in body["message"].lower()

    async def test_send_otp_prompts_for_otp_then_submits_it(
        self, client: AsyncClient, paid_election_with_candidate, monkeypatch
    ):
        election, category, candidate = paid_election_with_candidate
        phone = "233299234571"  # unrecognized prefix -> exercises the manual network menu
        submitted = {}

        async def fake_charge(self, **kwargs):
            return {"status": "send_otp"}

        async def fake_submit_otp(self, otp, reference):
            submitted["otp"] = otp
            submitted["reference"] = reference
            return {"status": "success"}

        monkeypatch.setattr(PaystackService, "charge_mobile_money", fake_charge)
        monkeypatch.setattr(PaystackService, "submit_otp", fake_submit_otp)

        await self._dial_to_network_prompt(client, election, candidate, phone)

        otp_prompt = await client.post(
            f"{WEBHOOK_URL}?token={TOKEN}", json=_ussd_post("1", phone)
        )
        body = otp_prompt.json()
        assert body["continueSession"] is True
        assert "OTP" in body["message"]

        final = await client.post(
            f"{WEBHOOK_URL}?token={TOKEN}", json=_ussd_post("123456", phone)
        )
        body = final.json()
        assert body["continueSession"] is False
        assert "Payment submitted" in body["message"]
        assert submitted["otp"] == "123456"

    async def test_long_paystack_display_text_gets_truncated(
        self, client: AsyncClient, paid_election_with_candidate, monkeypatch
    ):
        election, category, candidate = paid_election_with_candidate
        phone = "233299234574"  # unrecognized prefix -> exercises the manual network menu

        async def fake_charge(self, **kwargs):
            return {
                "status": "send_otp",
                "display_text": "Please enter the One-Time-PIN sent to your "
                "registered mobile number to authorize and complete this "
                "mobile money transaction right away",
            }

        monkeypatch.setattr(PaystackService, "charge_mobile_money", fake_charge)

        await self._dial_to_network_prompt(client, election, candidate, phone)
        otp_prompt = await client.post(
            f"{WEBHOOK_URL}?token={TOKEN}", json=_ussd_post("1", phone)
        )
        body = otp_prompt.json()
        assert len(body["message"]) <= settings.USSD_MAX_MESSAGE_LENGTH
        assert body["message"].endswith("…")


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