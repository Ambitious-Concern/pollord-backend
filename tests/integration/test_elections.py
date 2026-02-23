import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
class TestElectionCRUD:
    async def test_create_election(self, client: AsyncClient, admin_user):
        response = await client.post(
            "/api/v1/elections/",
            json={
                "title": "Test Election",
                "description": "An election for testing",
                "election_type": "single_choice",
                "start_datetime": "2026-06-01T08:00:00Z",
                "end_datetime": "2026-06-01T20:00:00Z",
            },
            headers=admin_user["headers"],
        )
        assert response.status_code == 201
        data = response.json()
        assert data["title"] == "Test Election"
        assert data["status"] == "draft"
        assert data["election_type"] == "single_choice"

    async def test_create_election_unauthorized(self, client: AsyncClient, test_user):
        response = await client.post(
            "/api/v1/elections/",
            json={
                "title": "Unauthorized Election",
                "election_type": "single_choice",
                "start_datetime": "2026-06-01T08:00:00Z",
                "end_datetime": "2026-06-01T20:00:00Z",
            },
            headers=test_user["headers"],
        )
        assert response.status_code == 403

    async def test_add_candidate(self, client: AsyncClient, admin_user):
        # Create election
        create_resp = await client.post(
            "/api/v1/elections/",
            json={
                "title": "Candidate Test Election",
                "election_type": "single_choice",
                "start_datetime": "2026-07-01T08:00:00Z",
                "end_datetime": "2026-07-01T20:00:00Z",
            },
            headers=admin_user["headers"],
        )
        election_id = create_resp.json()["election_id"]

        # Add candidate
        response = await client.post(
            f"/api/v1/elections/{election_id}/candidates",
            json={"name": "Test Candidate", "description": "A test candidate"},
            headers=admin_user["headers"],
        )
        assert response.status_code == 201
        assert response.json()["name"] == "Test Candidate"

    async def test_list_candidates(self, client: AsyncClient, admin_user):
        # Create election with candidate
        create_resp = await client.post(
            "/api/v1/elections/",
            json={
                "title": "List Candidates Election",
                "election_type": "single_choice",
                "start_datetime": "2026-08-01T08:00:00Z",
                "end_datetime": "2026-08-01T20:00:00Z",
            },
            headers=admin_user["headers"],
        )
        election_id = create_resp.json()["election_id"]

        await client.post(
            f"/api/v1/elections/{election_id}/candidates",
            json={"name": "Candidate 1"},
            headers=admin_user["headers"],
        )
        await client.post(
            f"/api/v1/elections/{election_id}/candidates",
            json={"name": "Candidate 2"},
            headers=admin_user["headers"],
        )

        response = await client.get(
            f"/api/v1/elections/{election_id}/candidates",
            headers=admin_user["headers"],
        )
        assert response.status_code == 200
        assert len(response.json()) == 2
