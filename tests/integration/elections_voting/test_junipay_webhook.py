"""JuniPay webhook (USSD mobile money votes) — since JuniPay's callback has
no signature scheme, the route must reconcile via check_transaction_status
before ever casting a vote or marking a transaction failed. These tests
mock check_transaction_status only; nothing here calls the real JuniPay API.
"""
import hashlib
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.election import Candidate, Category, Election
from app.models.transaction import Transaction
from app.models.vote import Vote

CHECK_STATUS_PATH = "app.api.v1.endpoints.junipay_webhook.JunipayService.check_transaction_status"


def _now():
    return datetime.now(timezone.utc)


@pytest.fixture
async def junipay_vote_transaction(db_session: AsyncSession, admin_user):
    election = Election(
        title="JuniPay USSD Election",
        start_datetime=_now() - timedelta(hours=1),
        end_datetime=_now() + timedelta(hours=23),
        status="active",
        visibility="public",
        require_verification=False,
        allow_revoting=False,
        created_by=admin_user["user"].user_id,
    )
    db_session.add(election)
    await db_session.flush()

    category = Category(election_id=election.election_id, name="Winner", election_type="single_choice")
    db_session.add(category)
    await db_session.flush()

    candidate = Candidate(category_id=category.category_id, election_id=election.election_id, name="Alice", display_order=1)
    db_session.add(candidate)
    await db_session.flush()

    reference = "vote_ussd_testref123"
    txn = Transaction(
        reference=reference,
        election_id=election.election_id,
        category_id=category.category_id,
        voter_hash=hashlib.sha256(b"0557079838:junipay-test").hexdigest(),
        email="ussd123@pollord.vote",
        candidate_ids=[str(candidate.candidate_id)],
        amount=100,
        currency="GHS",
        status="pending",
    )
    db_session.add(txn)
    await db_session.flush()

    return {"election": election, "category": category, "candidate": candidate, "reference": reference}


@pytest.mark.asyncio
class TestJunipayWebhook:
    async def test_confirmed_success_casts_vote(
        self, client: AsyncClient, junipay_vote_transaction, db_session: AsyncSession
    ):
        reference = junipay_vote_transaction["reference"]
        with patch(CHECK_STATUS_PATH, new=AsyncMock(return_value={"status": "SUCCESS"})):
            resp = await client.post(
                "/api/v1/payments/junipay/webhook",
                json={"status": "success", "trans_id": "COL100002377810", "foreignID": reference, "amount": 1.00},
            )
        assert resp.status_code == 200

        votes = (await db_session.execute(
            select(Vote).where(Vote.category_id == junipay_vote_transaction["category"].category_id)
        )).scalars().all()
        assert len(votes) == 1

    async def test_confirmed_failed_marks_transaction_failed(
        self, client: AsyncClient, junipay_vote_transaction, db_session: AsyncSession
    ):
        reference = junipay_vote_transaction["reference"]
        with patch(CHECK_STATUS_PATH, new=AsyncMock(return_value={"status": "FAILED"})):
            resp = await client.post(
                "/api/v1/payments/junipay/webhook",
                json={"status": "success", "trans_id": "COL2", "foreignID": reference, "amount": 1.00},
            )
        assert resp.status_code == 200

        txn = (await db_session.execute(
            select(Transaction).where(Transaction.reference == reference)
        )).scalar_one()
        assert txn.status == "failed"

    async def test_unconfirmed_pending_does_not_cast_vote(
        self, client: AsyncClient, junipay_vote_transaction, db_session: AsyncSession
    ):
        """A forged POST claiming success must not be enough on its own —
        only check_transaction_status confirming it is."""
        reference = junipay_vote_transaction["reference"]
        with patch(CHECK_STATUS_PATH, new=AsyncMock(return_value={"status": "PENDING"})):
            resp = await client.post(
                "/api/v1/payments/junipay/webhook",
                json={"status": "success", "trans_id": "COL3", "foreignID": reference, "amount": 1.00},
            )
        assert resp.status_code == 200

        votes = (await db_session.execute(
            select(Vote).where(Vote.category_id == junipay_vote_transaction["category"].category_id)
        )).scalars().all()
        assert len(votes) == 0

    async def test_reconciliation_failure_returns_502_for_retry(
        self, client: AsyncClient, junipay_vote_transaction
    ):
        with patch(CHECK_STATUS_PATH, new=AsyncMock(side_effect=Exception("network error"))):
            resp = await client.post(
                "/api/v1/payments/junipay/webhook",
                json={"status": "success", "trans_id": "COL4", "foreignID": junipay_vote_transaction["reference"], "amount": 1.00},
            )
        assert resp.status_code == 502

    async def test_missing_identifiers_ignored(self, client: AsyncClient):
        resp = await client.post("/api/v1/payments/junipay/webhook", json={"status": "success"})
        assert resp.status_code == 200
        assert resp.json() == {"status": "ignored"}
