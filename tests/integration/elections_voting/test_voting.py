import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.election import Candidate, Category, Election, EligibleVoter


@pytest.fixture
async def active_election(db_session: AsyncSession, admin_user):
    """Create an active election with one category, candidates, and eligible voter."""
    now = datetime.now(timezone.utc)
    election = Election(
        title="Test Election",
        description="A test election",
        start_datetime=now - timedelta(hours=1),
        end_datetime=now + timedelta(hours=23),
        status="active",
        created_by=admin_user["user"].user_id,
        # Gated (not the default open/public election) so the eligible_voter
        # fixture's presence/absence actually exercises the eligibility check.
        require_verification=True,
    )
    db_session.add(election)
    await db_session.flush()

    category = Category(
        election_id=election.election_id,
        name="President",
        election_type="single_choice",
    )
    db_session.add(category)
    await db_session.flush()

    c1 = Candidate(
        category_id=category.category_id,
        election_id=election.election_id,
        name="Candidate A",
        display_order=1,
    )
    c2 = Candidate(
        category_id=category.category_id,
        election_id=election.election_id,
        name="Candidate B",
        display_order=2,
    )
    db_session.add_all([c1, c2])
    await db_session.flush()

    return {
        "election": election,
        "category": category,
        "candidates": [c1, c2],
    }


@pytest.fixture
async def eligible_voter(db_session: AsyncSession, active_election, test_user):
    """Make test_user eligible for the election."""
    ev = EligibleVoter(
        election_id=active_election["election"].election_id,
        user_id=test_user["user"].user_id,
    )
    db_session.add(ev)
    await db_session.flush()
    return ev


@pytest.mark.asyncio
class TestCastVote:
    async def test_cast_vote_success(
        self, client: AsyncClient, test_user, active_election, eligible_voter
    ):
        election = active_election["election"]
        category = active_election["category"]
        candidate = active_election["candidates"][0]

        response = await client.post(
            "/api/v1/voting/cast",
            json={
                "category_id": str(category.category_id),
                "candidate_ids": [str(candidate.candidate_id)],
            },
            headers=test_user["headers"],
        )
        assert response.status_code == 201
        data = response.json()
        assert "receipt_code" in data
        assert data["election_id"] == str(election.election_id)

    async def test_cast_vote_not_eligible(
        self, client: AsyncClient, test_user, active_election
    ):
        # test_user is NOT an eligible voter (no eligible_voter fixture)
        category = active_election["category"]
        candidate = active_election["candidates"][0]

        response = await client.post(
            "/api/v1/voting/cast",
            json={
                "category_id": str(category.category_id),
                "candidate_ids": [str(candidate.candidate_id)],
            },
            headers=test_user["headers"],
        )
        assert response.status_code == 403

    async def test_cast_vote_invalid_candidate(
        self, client: AsyncClient, test_user, active_election, eligible_voter
    ):
        category = active_election["category"]
        response = await client.post(
            "/api/v1/voting/cast",
            json={
                "category_id": str(category.category_id),
                "candidate_ids": [str(uuid.uuid4())],
            },
            headers=test_user["headers"],
        )
        assert response.status_code == 400

    async def test_cast_vote_twice_same_category_rejected(
        self, client: AsyncClient, test_user, active_election, eligible_voter
    ):
        category = active_election["category"]
        candidate = active_election["candidates"][0]
        payload = {
            "category_id": str(category.category_id),
            "candidate_ids": [str(candidate.candidate_id)],
        }
        first = await client.post(
            "/api/v1/voting/cast", json=payload, headers=test_user["headers"]
        )
        assert first.status_code == 201

        second = await client.post(
            "/api/v1/voting/cast", json=payload, headers=test_user["headers"]
        )
        assert second.status_code == 409


@pytest.mark.asyncio
class TestGetBallot:
    async def test_get_ballot(
        self, client: AsyncClient, test_user, active_election, eligible_voter
    ):
        election = active_election["election"]
        response = await client.get(
            f"/api/v1/voting/ballot/{election.election_id}",
            headers=test_user["headers"],
        )
        assert response.status_code == 200
        data = response.json()
        assert data["title"] == "Test Election"
        assert len(data["categories"]) == 1
        assert len(data["categories"][0]["candidates"]) == 2
