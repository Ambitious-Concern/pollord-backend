from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.election import Candidate, Category, Election
from app.services.arkesel_service import ArkeselService


@pytest.fixture(autouse=True)
def _stub_sms(monkeypatch):
    sent = []

    async def fake_send_sms(self, to, message):
        sent.append((to, message))

    monkeypatch.setattr(ArkeselService, "send_sms", fake_send_sms)
    return sent


async def _make_election(db_session: AsyncSession, admin_user, plan: str):
    now = datetime.now(timezone.utc)
    election = Election(
        title="SMS OTP Test Election",
        start_datetime=now - timedelta(hours=1),
        end_datetime=now + timedelta(hours=23),
        status="active",
        visibility="public",
        require_verification=False,
        vote_price=0,
        plan=plan,
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


class TestSmsVoterOtp:
    async def test_standard_plan_full_flow(self, client: AsyncClient, db_session, admin_user, _stub_sms):
        _, category, candidate = await _make_election(db_session, admin_user, "standard")

        request_res = await client.post(
            "/api/v1/voting/public/request-vote-sms-otp",
            json={"category_id": str(category.category_id), "phone": "0241234567"},
        )
        assert request_res.status_code == 200
        assert len(_stub_sms) == 1
        sent_to, sent_message = _stub_sms[0]
        assert sent_to == "233241234567"

        otp_code = sent_message.split("code is ")[1].split(".")[0]

        verify_res = await client.post(
            "/api/v1/voting/public/verify-vote-sms-otp-and-cast",
            json={
                "category_id": str(category.category_id),
                "phone": "0241234567",
                "otp": otp_code,
                "candidate_ids": [str(candidate.candidate_id)],
            },
        )
        assert verify_res.status_code == 201
        assert "receipt_code" in verify_res.json()

    async def test_wrong_code_rejected(self, client: AsyncClient, db_session, admin_user):
        _, category, candidate = await _make_election(db_session, admin_user, "premium")
        await client.post(
            "/api/v1/voting/public/request-vote-sms-otp",
            json={"category_id": str(category.category_id), "phone": "0241234567"},
        )
        res = await client.post(
            "/api/v1/voting/public/verify-vote-sms-otp-and-cast",
            json={
                "category_id": str(category.category_id),
                "phone": "0241234567",
                "otp": "000000",
                "candidate_ids": [str(candidate.candidate_id)],
            },
        )
        assert res.status_code == 400

    async def test_double_vote_rejected(self, client: AsyncClient, db_session, admin_user):
        _, category, candidate = await _make_election(db_session, admin_user, "premium")

        category_id = category.category_id
        candidate_id = candidate.candidate_id

        req = await client.post(
            "/api/v1/voting/public/request-vote-sms-otp",
            json={"category_id": str(category_id), "phone": "0241234567"},
        )
        assert req.status_code == 200

        from sqlalchemy import select
        from app.models.election import SmsVoterOTP

        result = await db_session.execute(
            select(SmsVoterOTP).where(
                SmsVoterOTP.category_id == category_id, SmsVoterOTP.used == False
            )
        )
        otp_code = result.scalars().first().otp_code

        first = await client.post(
            "/api/v1/voting/public/verify-vote-sms-otp-and-cast",
            json={
                "category_id": str(category_id),
                "phone": "0241234567",
                "otp": otp_code,
                "candidate_ids": [str(candidate_id)],
            },
        )
        assert first.status_code == 201

        # The vote is already recorded, so re-requesting a code for the same
        # phone+category is rejected before a new OTP is even generated.
        second_request = await client.post(
            "/api/v1/voting/public/request-vote-sms-otp",
            json={"category_id": str(category_id), "phone": "0241234567"},
        )
        assert second_request.status_code == 409
