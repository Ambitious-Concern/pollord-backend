"""Sub-cedi vote prices.

Vote prices are stored in pesewas. The platform used to refuse anything under
₵1.00 and anything that wasn't a whole cedi (the global setting and the admin
override), while election create/update independently enforced a ₵0.50 floor
in ₵0.50 steps — four rules, three of them disagreeing. Organisers need to
charge less than a cedi per vote, so the floor is now ₵0.10 everywhere with
no step constraint, letting prices like ₵0.70 through.
"""
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.election import Election


@pytest_asyncio.fixture
async def election(db_session: AsyncSession, admin_user) -> Election:
    now = datetime.now(timezone.utc)
    e = Election(
        title="Priced Election",
        election_type="single_choice",
        start_datetime=now + timedelta(days=1),
        end_datetime=now + timedelta(days=2),
        status="draft",
        created_by=admin_user["user"].user_id,
    )
    db_session.add(e)
    await db_session.flush()
    return e


@pytest.mark.asyncio
class TestElectionOverrideMinimum:
    @pytest.mark.parametrize("pesewas", [10, 70, 99, 150, 250])
    async def test_accepts_sub_cedi_and_non_step_prices(
        self, client: AsyncClient, admin_user, election, pesewas
    ):
        response = await client.put(
            f"/api/v1/admin/elections/{election.election_id}/vote-price",
            json={"vote_price": pesewas},
            headers=admin_user["headers"],
        )
        assert response.status_code == 200, response.text
        assert response.json()["vote_price"] == pesewas
        assert response.json()["effective_vote_price"] == pesewas

    @pytest.mark.parametrize("pesewas", [0, 1, 9, -10])
    async def test_rejects_below_ten_pesewas(
        self, client: AsyncClient, admin_user, election, pesewas
    ):
        response = await client.put(
            f"/api/v1/admin/elections/{election.election_id}/vote-price",
            json={"vote_price": pesewas},
            headers=admin_user["headers"],
        )
        assert response.status_code == 422

    async def test_null_still_resets_to_global_default(
        self, client: AsyncClient, admin_user, election
    ):
        response = await client.put(
            f"/api/v1/admin/elections/{election.election_id}/vote-price",
            json={"vote_price": None},
            headers=admin_user["headers"],
        )
        assert response.status_code == 200
        assert response.json()["vote_price"] is None


@pytest.mark.asyncio
class TestGlobalSettingMinimum:
    @pytest.mark.parametrize("pesewas", [10, 70, 250])
    async def test_accepts_sub_cedi_global_price(
        self, client: AsyncClient, admin_user, pesewas
    ):
        response = await client.put(
            "/api/v1/admin/platform-settings",
            json={"vote_price": pesewas},
            headers=admin_user["headers"],
        )
        assert response.status_code == 200, response.text

    @pytest.mark.parametrize("pesewas", [0, 9])
    async def test_rejects_below_ten_pesewas(
        self, client: AsyncClient, admin_user, pesewas
    ):
        response = await client.put(
            "/api/v1/admin/platform-settings",
            json={"vote_price": pesewas},
            headers=admin_user["headers"],
        )
        assert response.status_code == 422


@pytest.mark.asyncio
class TestElectionCreateMinimum:
    """Election create/update carried its own ₵0.50-multiple rule."""

    async def test_accepts_seventy_pesewas_on_create(
        self, client: AsyncClient, admin_user
    ):
        now = datetime.now(timezone.utc)
        response = await client.post(
            "/api/v1/elections",
            json={
                "title": "Cheap Votes",
                "election_type": "single_choice",
                "start_datetime": (now + timedelta(days=1)).isoformat(),
                "end_datetime": (now + timedelta(days=2)).isoformat(),
                "settings": {"vote_price": 70},
            },
            headers=admin_user["headers"],
        )
        assert response.status_code in (200, 201), response.text

    async def test_rejects_nine_pesewas_on_create(
        self, client: AsyncClient, admin_user
    ):
        now = datetime.now(timezone.utc)
        response = await client.post(
            "/api/v1/elections",
            json={
                "title": "Too Cheap",
                "election_type": "single_choice",
                "start_datetime": (now + timedelta(days=1)).isoformat(),
                "end_datetime": (now + timedelta(days=2)).isoformat(),
                "settings": {"vote_price": 9},
            },
            headers=admin_user["headers"],
        )
        assert response.status_code in (400, 422), response.text
