from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis import get_redis
from app.core.security import generate_voter_hash
from app.models.election import Candidate, Category, Election
from app.models.vote import Vote
from app.repositories.vote_repository import VoteRepository
from app.services.cryptography_service import CryptographyService


async def _make_open_election(db_session: AsyncSession, admin_user) -> tuple[Election, Category, Candidate]:
    now = datetime.now(timezone.utc)
    election = Election(
        title="Concurrency Test Election",
        start_datetime=now - timedelta(hours=1),
        end_datetime=now + timedelta(hours=1),
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


async def _insert_vote(db_session: AsyncSession, category: Category, election: Election, voter_hash: str) -> None:
    crypto = CryptographyService()
    now = datetime.now(timezone.utc)
    encrypted = crypto.encrypt_vote_data([])
    signature = crypto.sign_vote(encrypted, now.isoformat())
    await VoteRepository(Vote, db_session).create({
        "category_id": category.category_id,
        "election_id": election.election_id,
        "voter_hash": voter_hash,
        "vote_data": encrypted,
        "vote_signature": signature,
        "count": 1,
    })


class TestVoteRaceCondition:
    async def test_concurrent_duplicate_vote_returns_409_not_500(
        self, client: AsyncClient, db_session, admin_user, test_user, monkeypatch
    ):
        election, category, candidate = await _make_open_election(db_session, admin_user)

        # Simulate another request's vote landing between this request's
        # has_voted() check and its own insert.
        existing_hash = generate_voter_hash(test_user["user"].user_id, category.category_id)
        await _insert_vote(db_session, category, election, existing_hash)

        monkeypatch.setattr(VoteRepository, "has_voted", lambda self, *a, **kw: _false())

        res = await client.post(
            "/api/v1/voting/cast",
            json={"category_id": str(category.category_id), "candidate_ids": [str(candidate.candidate_id)]},
            headers=test_user["headers"],
        )
        assert res.status_code == 409
        assert "already voted" in res.json()["detail"]


async def _false():
    return False


class TestLiveResultsCache:
    async def test_second_call_within_ttl_is_cached(
        self, client: AsyncClient, db_session, admin_user
    ):
        election, category, candidate = await _make_open_election(db_session, admin_user)
        await _insert_vote(db_session, category, election, "voter-1")

        first = await client.get(f"/api/v1/voting/public/live-results/{election.election_id}")
        assert first.status_code == 200
        assert first.json()["total_votes"] == 1

        await _insert_vote(db_session, category, election, "voter-2")

        second = await client.get(f"/api/v1/voting/public/live-results/{election.election_id}")
        assert second.status_code == 200
        assert second.json()["total_votes"] == 1  # still cached

        redis = await get_redis()
        await redis.delete(f"live_results:election:{election.election_id}")

        third = await client.get(f"/api/v1/voting/public/live-results/{election.election_id}")
        assert third.status_code == 200
        assert third.json()["total_votes"] == 2  # cache miss, recomputed
