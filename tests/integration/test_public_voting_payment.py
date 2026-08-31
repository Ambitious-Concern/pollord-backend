"""
Integration tests for public voting + Paystack payment flow.

Covers:
  1. allow_revoting saved via PUT /elections/{id} (SETTINGS_FIELDS fix)
  2. GET /voting/public/ballot returns allow_revoting field
  3. Single-vote public election: free cast, amount always ₵1
  4. Multi-vote (allow_revoting): initiate-payment creates correct vote_count
  5. verify-and-cast creates N vote records matching paid amount
  6. Duplicate vote prevention (single-vote election)
  7. Payment idempotency (already-used reference rejected)
  8. Failed/invalid payment rejected

All casting/payment endpoints are category_id-driven (a category always
belongs to exactly one election or event) — these fixtures create one
category per election to match.
"""

import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.election import Candidate, Category, Election
from app.models.transaction import Transaction
from app.models.vote import Vote

# Read from actual settings so tests stay correct regardless of .env overrides
VP = settings.VOTE_PRICE   # e.g. 100 or 1000 pesewas per vote


# ─────────────────────────────────────────────────────────────────────────────
# Shared fixtures
# ─────────────────────────────────────────────────────────────────────────────

def _now():
    return datetime.now(timezone.utc)


@pytest.fixture
async def public_single_election(db_session: AsyncSession, admin_user):
    """Active public single-choice election, NO auth required, NO revoting."""
    election = Election(
        title="Public Single Election",
        description="Single vote, no revoting",
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

    category = Category(
        election_id=election.election_id, name="Winner", election_type="single_choice"
    )
    db_session.add(category)
    await db_session.flush()

    c1 = Candidate(category_id=category.category_id, election_id=election.election_id, name="Alice", display_order=1)
    c2 = Candidate(category_id=category.category_id, election_id=election.election_id, name="Bob", display_order=2)
    db_session.add_all([c1, c2])
    await db_session.flush()
    return {"election": election, "category": category, "candidates": [c1, c2]}


@pytest.fixture
async def public_revoting_election(db_session: AsyncSession, admin_user):
    """Active public single-choice election with allow_revoting=True."""
    election = Election(
        title="Public Revoting Election",
        description="Multiple votes allowed",
        start_datetime=_now() - timedelta(hours=1),
        end_datetime=_now() + timedelta(hours=23),
        status="active",
        visibility="public",
        require_verification=False,
        allow_revoting=True,
        created_by=admin_user["user"].user_id,
    )
    db_session.add(election)
    await db_session.flush()

    category = Category(
        election_id=election.election_id, name="Winner", election_type="single_choice"
    )
    db_session.add(category)
    await db_session.flush()

    c1 = Candidate(category_id=category.category_id, election_id=election.election_id, name="Alice", display_order=1)
    db_session.add(c1)
    await db_session.flush()
    return {"election": election, "category": category, "candidates": [c1]}


@pytest.fixture
async def draft_election(db_session: AsyncSession, admin_user):
    """Draft election for testing settings update."""
    election = Election(
        title="Draft Election",
        start_datetime=_now() + timedelta(hours=1),
        end_datetime=_now() + timedelta(hours=25),
        status="draft",
        visibility="public",
        require_verification=False,
        allow_revoting=False,
        created_by=admin_user["user"].user_id,
    )
    db_session.add(election)
    await db_session.flush()

    category = Category(
        election_id=election.election_id, name="Winner", election_type="single_choice"
    )
    db_session.add(category)
    await db_session.flush()

    c1 = Candidate(category_id=category.category_id, election_id=election.election_id, name="Alice", display_order=1)
    db_session.add(c1)
    await db_session.flush()
    return {"election": election, "category": category, "candidates": [c1]}


# ─────────────────────────────────────────────────────────────────────────────
# 1.  allow_revoting saved correctly via PUT /elections/{id}
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestAllowRevotingSetting:

    async def test_allow_revoting_saved_on_update(
        self, client: AsyncClient, admin_user, draft_election, db_session: AsyncSession
    ):
        """PUT /elections/{id} must persist allow_revoting=True."""
        election = draft_election["election"]

        resp = await client.put(
            f"/api/v1/elections/{election.election_id}",
            json={
                "title": election.title,
                "start_datetime": election.start_datetime.isoformat(),
                "end_datetime": election.end_datetime.isoformat(),
                "settings": {
                    "visibility": "public",
                    "allow_result_viewing": "after_end",
                    "require_verification": False,
                    "anonymous_results": True,
                    "show_candidate_count": False,
                    "randomize_candidate_order": False,
                    "enable_notifications": True,
                    "allow_revoting": True,
                },
            },
            headers=admin_user["headers"],
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["allow_revoting"] is True

    async def test_allow_revoting_false_by_default(
        self, client: AsyncClient, admin_user, draft_election
    ):
        """allow_revoting must default to False when not supplied."""
        election = draft_election["election"]

        resp = await client.put(
            f"/api/v1/elections/{election.election_id}",
            json={
                "title": election.title,
                "start_datetime": election.start_datetime.isoformat(),
                "end_datetime": election.end_datetime.isoformat(),
                "settings": {
                    "visibility": "public",
                    "allow_result_viewing": "after_end",
                    "require_verification": False,
                    "anonymous_results": True,
                    "show_candidate_count": False,
                    "randomize_candidate_order": False,
                    "enable_notifications": True,
                    # allow_revoting intentionally omitted
                },
            },
            headers=admin_user["headers"],
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["allow_revoting"] is False


# ─────────────────────────────────────────────────────────────────────────────
# 2.  Public ballot returns allow_revoting
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestPublicBallot:

    async def test_ballot_includes_allow_revoting_false(
        self, client: AsyncClient, public_single_election
    ):
        election = public_single_election["election"]
        resp = await client.get(f"/api/v1/voting/public/ballot/{election.election_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert "allow_revoting" in data
        assert data["allow_revoting"] is False

    async def test_ballot_includes_allow_revoting_true(
        self, client: AsyncClient, public_revoting_election
    ):
        election = public_revoting_election["election"]
        resp = await client.get(f"/api/v1/voting/public/ballot/{election.election_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert "allow_revoting" in data
        assert data["allow_revoting"] is True

    async def test_ballot_not_found(self, client: AsyncClient):
        resp = await client.get(f"/api/v1/voting/public/ballot/{uuid.uuid4()}")
        assert resp.status_code == 404

    async def test_ballot_returns_candidates(
        self, client: AsyncClient, public_single_election
    ):
        election = public_single_election["election"]
        resp = await client.get(f"/api/v1/voting/public/ballot/{election.election_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["categories"]) == 1
        assert len(data["categories"][0]["candidates"]) == 2


# ─────────────────────────────────────────────────────────────────────────────
# 3.  Free public cast (no payment)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestPublicCastFree:

    async def test_cast_vote_success(
        self, client: AsyncClient, public_single_election
    ):
        election = public_single_election["election"]
        category = public_single_election["category"]
        candidate = public_single_election["candidates"][0]

        resp = await client.post(
            "/api/v1/voting/public/cast",
            json={
                "category_id": str(category.category_id),
                "candidate_ids": [str(candidate.candidate_id)],
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert "receipt_code" in data
        assert data["election_id"] == str(election.election_id)

    async def test_duplicate_vote_rejected(
        self, client: AsyncClient, public_single_election
    ):
        """Same IP/UA cannot vote twice on a single-vote election."""
        category = public_single_election["category"]
        candidate = public_single_election["candidates"][0]
        payload = {
            "category_id": str(category.category_id),
            "candidate_ids": [str(candidate.candidate_id)],
        }

        r1 = await client.post("/api/v1/voting/public/cast", json=payload)
        assert r1.status_code == 201

        r2 = await client.post("/api/v1/voting/public/cast", json=payload)
        assert r2.status_code == 409

    async def test_revoting_allows_multiple_casts(
        self, client: AsyncClient, public_revoting_election
    ):
        """allow_revoting=True: same voter can cast more than once."""
        category = public_revoting_election["category"]
        candidate = public_revoting_election["candidates"][0]
        payload = {
            "category_id": str(category.category_id),
            "candidate_ids": [str(candidate.candidate_id)],
        }

        r1 = await client.post("/api/v1/voting/public/cast", json=payload)
        r2 = await client.post("/api/v1/voting/public/cast", json=payload)
        assert r1.status_code == 201
        assert r2.status_code == 201

    async def test_cast_invalid_candidate(
        self, client: AsyncClient, public_single_election
    ):
        category = public_single_election["category"]
        resp = await client.post(
            "/api/v1/voting/public/cast",
            json={
                "category_id": str(category.category_id),
                "candidate_ids": [str(uuid.uuid4())],
            },
        )
        assert resp.status_code == 400


# ─────────────────────────────────────────────────────────────────────────────
# 4 & 5.  Payment initiation + verify-and-cast
# ─────────────────────────────────────────────────────────────────────────────

FAKE_ACCESS_CODE = "acc_test_abc123"
FAKE_REFERENCE   = "vote_testref123"

PAYSTACK_INIT_PATH   = "app.api.v1.endpoints.voting.PaystackService.initialize_transaction"
PAYSTACK_VERIFY_PATH = "app.api.v1.endpoints.voting.PaystackService.verify_transaction"


def _init_mock(amount: int = 100) -> AsyncMock:
    return AsyncMock(return_value={
        "access_code": FAKE_ACCESS_CODE,
        "reference": FAKE_REFERENCE,
        "authorization_url": "https://checkout.paystack.com/test",
    })


def _verify_mock(amount: int = 100, status: str = "success") -> AsyncMock:
    return AsyncMock(return_value={
        "status": status,
        "amount": amount,
        "reference": FAKE_REFERENCE,
        "currency": "GHS",
    })


@pytest.mark.asyncio
class TestInitiatePayment:

    async def test_single_vote_uses_vote_price(
        self, client: AsyncClient, public_single_election
    ):
        """Single-vote election: backend always charges exactly VOTE_PRICE."""
        category = public_single_election["category"]
        candidate = public_single_election["candidates"][0]

        with patch(PAYSTACK_INIT_PATH, new=_init_mock(VP)):
            resp = await client.post(
                "/api/v1/voting/public/initiate-payment",
                json={
                    "category_id": str(category.category_id),
                    "candidate_ids": [str(candidate.candidate_id)],
                    "email": "voter@example.com",
                    # amount_pesewas intentionally omitted for single-vote
                },
            )

        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["amount"] == VP
        assert data["vote_count"] == 1
        assert data["currency"] == "GHS"
        assert "reference" in data
        assert "access_code" in data
        assert "public_key" in data

    async def test_multi_vote_uses_provided_amount(
        self, client: AsyncClient, public_revoting_election
    ):
        """allow_revoting election: backend must use the supplied amount_pesewas."""
        category = public_revoting_election["category"]
        candidate = public_revoting_election["candidates"][0]
        n_votes = 11
        amount = VP * n_votes  # exactly 11 votes worth

        with patch(PAYSTACK_INIT_PATH, new=_init_mock(amount)):
            resp = await client.post(
                "/api/v1/voting/public/initiate-payment",
                json={
                    "category_id": str(category.category_id),
                    "candidate_ids": [str(candidate.candidate_id)],
                    "email": "voter@example.com",
                    "amount_pesewas": amount,
                },
            )

        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["amount"] == amount
        assert data["vote_count"] == n_votes

    async def test_amount_truncated_to_vote_price_multiple(
        self, client: AsyncClient, public_revoting_election
    ):
        """Amount not a multiple of VOTE_PRICE is floored to the nearest multiple."""
        category = public_revoting_election["category"]
        candidate = public_revoting_election["candidates"][0]
        # e.g. VP=1000: send 1500 → floored to 1000 → 1 vote
        amount_in = VP + VP // 2      # 1.5x VOTE_PRICE
        expected  = VP                # floored to 1x VOTE_PRICE

        with patch(PAYSTACK_INIT_PATH, new=_init_mock(expected)):
            resp = await client.post(
                "/api/v1/voting/public/initiate-payment",
                json={
                    "category_id": str(category.category_id),
                    "candidate_ids": [str(candidate.candidate_id)],
                    "email": "voter@example.com",
                    "amount_pesewas": amount_in,
                },
            )

        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["amount"] == expected
        assert data["vote_count"] == 1

    async def test_single_vote_election_ignores_custom_amount(
        self, client: AsyncClient, public_single_election
    ):
        """For single-vote elections, custom amount_pesewas is ignored; always charges VOTE_PRICE."""
        category = public_single_election["category"]
        candidate = public_single_election["candidates"][0]

        with patch(PAYSTACK_INIT_PATH, new=_init_mock(VP)):
            resp = await client.post(
                "/api/v1/voting/public/initiate-payment",
                json={
                    "category_id": str(category.category_id),
                    "candidate_ids": [str(candidate.candidate_id)],
                    "email": "voter@example.com",
                    "amount_pesewas": VP * 11,  # should be ignored — not allow_revoting
                },
            )

        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["amount"] == VP
        assert data["vote_count"] == 1


@pytest.mark.asyncio
class TestVerifyAndCast:

    async def _initiate(
        self, client: AsyncClient, category_id: str, candidate_id: str,
        email: str, amount_pesewas: int | None
    ) -> str:
        """Helper: initiate payment and return the reference."""
        payload = {
            "category_id": category_id,
            "candidate_ids": [candidate_id],
            "email": email,
        }
        if amount_pesewas is not None:
            payload["amount_pesewas"] = amount_pesewas

        with patch(PAYSTACK_INIT_PATH, new=_init_mock(amount_pesewas or 100)):
            resp = await client.post(
                "/api/v1/voting/public/initiate-payment", json=payload
            )
        assert resp.status_code == 201, resp.text
        return resp.json()["reference"]

    async def test_single_vote_creates_one_record_with_count_1(
        self, client: AsyncClient, db_session: AsyncSession, public_single_election
    ):
        election = public_single_election["election"]
        category = public_single_election["category"]
        candidate = public_single_election["candidates"][0]

        ref = await self._initiate(
            client, str(category.category_id), str(candidate.candidate_id),
            "voter1@example.com", None,
        )

        with patch(PAYSTACK_VERIFY_PATH, new=_verify_mock(VP)):
            resp = await client.post(
                "/api/v1/voting/public/verify-and-cast",
                json={"reference": ref},
            )

        assert resp.status_code == 201, resp.text
        assert "receipt_code" in resp.json()

        from sqlalchemy import select
        rows = (await db_session.execute(
            select(Vote).where(Vote.election_id == election.election_id)
        )).scalars().all()
        # Exactly 1 row with count=1
        assert len(rows) == 1
        assert rows[0].count == 1

    async def test_multi_vote_creates_one_record_with_correct_count(
        self, client: AsyncClient, db_session: AsyncSession, public_revoting_election
    ):
        """Paying N×VOTE_PRICE must create 1 vote record with count=N."""
        election = public_revoting_election["election"]
        category = public_revoting_election["category"]
        candidate = public_revoting_election["candidates"][0]
        n_votes = 11
        amount = VP * n_votes

        ref = await self._initiate(
            client, str(category.category_id), str(candidate.candidate_id),
            "voter2@example.com", amount,
        )

        with patch(PAYSTACK_VERIFY_PATH, new=_verify_mock(amount)):
            resp = await client.post(
                "/api/v1/voting/public/verify-and-cast",
                json={"reference": ref},
            )

        assert resp.status_code == 201, resp.text

        from sqlalchemy import select
        rows = (await db_session.execute(
            select(Vote).where(Vote.election_id == election.election_id)
        )).scalars().all()
        # 1 row, count=11
        assert len(rows) == 1
        assert rows[0].count == n_votes

    async def test_vote_count_accumulates_correctly(
        self, client: AsyncClient, db_session: AsyncSession, public_revoting_election
    ):
        """Two separate payments produce two records; sum of counts = total votes."""
        election = public_revoting_election["election"]
        category = public_revoting_election["category"]
        candidate = public_revoting_election["candidates"][0]

        # Payment 1: 3 votes
        ref1 = await self._initiate(
            client, str(category.category_id), str(candidate.candidate_id),
            "voter3a@example.com", VP * 3,
        )
        with patch(PAYSTACK_VERIFY_PATH, new=_verify_mock(VP * 3)):
            await client.post("/api/v1/voting/public/verify-and-cast", json={"reference": ref1})

        # Payment 2: 5 votes
        ref2 = await self._initiate(
            client, str(category.category_id), str(candidate.candidate_id),
            "voter3b@example.com", VP * 5,
        )
        with patch(PAYSTACK_VERIFY_PATH, new=_verify_mock(VP * 5)):
            await client.post("/api/v1/voting/public/verify-and-cast", json={"reference": ref2})

        from sqlalchemy import select
        rows = (await db_session.execute(
            select(Vote).where(Vote.election_id == election.election_id)
        )).scalars().all()
        assert len(rows) == 2
        assert sum(v.count for v in rows) == 8  # 3 + 5

    async def test_reference_not_found_returns_404(self, client: AsyncClient):
        resp = await client.post(
            "/api/v1/voting/public/verify-and-cast",
            json={"reference": "nonexistent_ref"},
        )
        assert resp.status_code == 404

    async def test_already_used_reference_rejected(
        self, client: AsyncClient, public_single_election
    ):
        """Calling verify-and-cast twice with the same reference must fail on 2nd call."""
        category = public_single_election["category"]
        candidate = public_single_election["candidates"][0]

        ref = await self._initiate(
            client, str(category.category_id), str(candidate.candidate_id),
            "voter4@example.com", None,
        )

        with patch(PAYSTACK_VERIFY_PATH, new=_verify_mock(VP)):
            r1 = await client.post(
                "/api/v1/voting/public/verify-and-cast", json={"reference": ref}
            )
            r2 = await client.post(
                "/api/v1/voting/public/verify-and-cast", json={"reference": ref}
            )

        assert r1.status_code == 201
        assert r2.status_code == 409

    async def test_failed_paystack_payment_rejected(
        self, client: AsyncClient, public_single_election
    ):
        """If Paystack reports status != success, vote must not be cast."""
        category = public_single_election["category"]
        candidate = public_single_election["candidates"][0]

        ref = await self._initiate(
            client, str(category.category_id), str(candidate.candidate_id),
            "voter5@example.com", None,
        )

        with patch(PAYSTACK_VERIFY_PATH, new=_verify_mock(VP, status="failed")):
            resp = await client.post(
                "/api/v1/voting/public/verify-and-cast", json={"reference": ref}
            )

        assert resp.status_code == 402

    async def test_underpayment_rejected(
        self, client: AsyncClient, public_single_election
    ):
        """Paystack amount less than charged amount must be rejected."""
        category = public_single_election["category"]
        candidate = public_single_election["candidates"][0]

        ref = await self._initiate(
            client, str(category.category_id), str(candidate.candidate_id),
            "voter6@example.com", None,
        )

        with patch(PAYSTACK_VERIFY_PATH, new=_verify_mock(VP // 2)):
            # Paystack says only half the required amount was paid
            resp = await client.post(
                "/api/v1/voting/public/verify-and-cast", json={"reference": ref}
            )

        assert resp.status_code == 402


# ─────────────────────────────────────────────────────────────────────────────
# 6.  Live results reflect correct vote counts
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestPublicLiveResults:

    async def test_results_count_multi_votes(
        self, client: AsyncClient, db_session: AsyncSession, public_revoting_election
    ):
        """After N paid votes, live results must show vote_count = N for the candidate."""
        election = public_revoting_election["election"]
        category = public_revoting_election["category"]
        candidate = public_revoting_election["candidates"][0]
        amount = VP * 7  # 7 votes

        with patch(PAYSTACK_INIT_PATH, new=_init_mock(amount)):
            init_resp = await client.post(
                "/api/v1/voting/public/initiate-payment",
                json={
                    "category_id": str(category.category_id),
                    "candidate_ids": [str(candidate.candidate_id)],
                    "email": "voter7@example.com",
                    "amount_pesewas": amount,
                },
            )
        ref = init_resp.json()["reference"]

        with patch(PAYSTACK_VERIFY_PATH, new=_verify_mock(amount)):
            await client.post(
                "/api/v1/voting/public/verify-and-cast", json={"reference": ref}
            )

        results_resp = await client.get(
            f"/api/v1/voting/public/live-results/{election.election_id}"
        )
        assert results_resp.status_code == 200
        data = results_resp.json()
        category_result = data["categories"][0]
        candidate_result = next(
            r for r in category_result["results"]
            if r["candidate_id"] == str(candidate.candidate_id)
        )
        # 1 vote record with count=7; results engine sums count → 7
        assert candidate_result["vote_count"] == 7
        assert data["total_votes"] == 7
