"""
Proves the vote-integrity guarantee under real concurrency, not just
reasoning about it: two genuinely simultaneous insert attempts for the same
(category_id, voter_hash) — as if two requests from the same voter raced
each other — and confirms exactly one succeeds, never both, never zero.

This deliberately bypasses the db_session/client fixtures used elsewhere in
this suite: those bind all of a test's activity to one connection + SAVEPOINT
that's rolled back at teardown, so nothing ever really commits — which is
exactly wrong for this test, since genuinely separate concurrent connections
need to see already-committed data to race against. Uses the test engine
directly instead, with real commits and explicit cleanup.
"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from tests.integration.conftest import test_session_maker
from app.models.election import Candidate, Category, Election
from app.models.vote import Vote
from app.services.cryptography_service import CryptographyService


@pytest.fixture
async def committed_election_with_candidate():
    """Real commits to the test DB (not a savepoint) — required so that
    independent concurrent connections can actually see this data."""
    now = datetime.now(timezone.utc)
    async with test_session_maker() as session:
        from app.models.user import User
        from app.core.security import hash_password

        # A throwaway creator user — Election.created_by is NOT NULL.
        user = User(
            email=f"concurrency-{uuid.uuid4().hex[:8]}@test.local",
            password_hash=hash_password("Password123!"),
            full_name="Concurrency Test User",
            email_verified=True,
        )
        session.add(user)
        await session.flush()

        election = Election(
            title="Concurrency Test Election",
            start_datetime=now - timedelta(hours=1),
            end_datetime=now + timedelta(hours=23),
            status="active",
            visibility="public",
            require_verification=False,
            created_by=user.user_id,
        )
        session.add(election)
        await session.flush()

        category = Category(
            election_id=election.election_id,
            name="President",
            election_type="single_choice",
            display_order=0,
        )
        session.add(category)
        await session.flush()

        candidate = Candidate(
            category_id=category.category_id,
            election_id=election.election_id,
            name="Anne",
            short_code="ANNE",
            display_order=0,
        )
        session.add(candidate)
        await session.flush()
        await session.commit()

        election_id, category_id, candidate_id, user_id = (
            election.election_id, category.category_id, candidate.candidate_id, user.user_id,
        )

    yield election_id, category_id, candidate_id

    # Manual cleanup since this bypassed the usual rollback-based isolation.
    async with test_session_maker() as session:
        await session.execute(delete(Vote).where(Vote.category_id == category_id))
        await session.execute(delete(Candidate).where(Candidate.candidate_id == candidate_id))
        await session.execute(delete(Category).where(Category.category_id == category_id))
        await session.execute(delete(Election).where(Election.election_id == election_id))
        await session.execute(delete(User).where(User.user_id == user_id))
        await session.commit()


async def _attempt_vote(
    category_id: uuid.UUID, election_id: uuid.UUID, candidate_id: uuid.UUID, voter_hash: str
) -> str:
    """Simulates one full request: its own session, its own connection —
    exactly how two concurrent HTTP/USSD requests would each get their own
    session from get_db(). Returns 'success' or 'rejected'."""
    crypto = CryptographyService()
    async with test_session_maker() as session:
        try:
            encrypted = crypto.encrypt_vote_data([str(candidate_id)])
            vote = Vote(
                category_id=category_id,
                election_id=election_id,
                event_id=None,
                voter_hash=voter_hash,
                vote_data=encrypted,
                vote_signature=crypto.sign_vote(encrypted, "2026-01-01T00:00:00+00:00"),
                count=1,
            )
            session.add(vote)
            await session.flush()
            await session.commit()
            return "success"
        except IntegrityError:
            await session.rollback()
            return "rejected"


@pytest.mark.asyncio
class TestVoteConcurrency:
    async def test_concurrent_double_vote_exactly_one_succeeds(
        self, committed_election_with_candidate
    ):
        election_id, category_id, candidate_id = committed_election_with_candidate
        voter_hash = f"concurrency-test-{uuid.uuid4().hex}"

        results = await asyncio.gather(
            _attempt_vote(category_id, election_id, candidate_id, voter_hash),
            _attempt_vote(category_id, election_id, candidate_id, voter_hash),
        )

        assert results.count("success") == 1, f"Results: {results}"
        assert results.count("rejected") == 1, f"Results: {results}"

        async with test_session_maker() as session:
            count_result = await session.execute(
                select(func.count()).select_from(Vote).where(
                    Vote.category_id == category_id,
                    Vote.voter_hash == voter_hash,
                )
            )
            assert count_result.scalar_one() == 1

    async def test_ten_concurrent_attempts_exactly_one_succeeds(
        self, committed_election_with_candidate
    ):
        """More adversarial: 10 simultaneous attempts, same voter — still
        exactly one winner, matching a realistic "double-tap from a flaky
        USSD/network retry" scenario rather than just two."""
        election_id, category_id, candidate_id = committed_election_with_candidate
        voter_hash = f"concurrency-test-10x-{uuid.uuid4().hex}"

        results = await asyncio.gather(
            *[
                _attempt_vote(category_id, election_id, candidate_id, voter_hash)
                for _ in range(10)
            ]
        )

        assert results.count("success") == 1, f"Results: {results}"
        assert results.count("rejected") == 9, f"Results: {results}"
