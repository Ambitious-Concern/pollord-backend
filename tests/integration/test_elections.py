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
                "start_datetime": "2026-06-01T08:00:00Z",
                "end_datetime": "2026-06-01T20:00:00Z",
            },
            headers=admin_user["headers"],
        )
        assert response.status_code == 201
        data = response.json()
        assert data["title"] == "Test Election"
        assert data["status"] == "draft"

    async def test_create_election_unauthorized(self, client: AsyncClient, test_user):
        response = await client.post(
            "/api/v1/elections/",
            json={
                "title": "Unauthorized Election",
                "start_datetime": "2026-06-01T08:00:00Z",
                "end_datetime": "2026-06-01T20:00:00Z",
            },
            headers=test_user["headers"],
        )
        assert response.status_code == 403

    async def test_add_category(self, client: AsyncClient, admin_user):
        create_resp = await client.post(
            "/api/v1/elections/",
            json={
                "title": "Category Test Election",
                "start_datetime": "2026-07-01T08:00:00Z",
                "end_datetime": "2026-07-01T20:00:00Z",
            },
            headers=admin_user["headers"],
        )
        election_id = create_resp.json()["election_id"]

        response = await client.post(
            f"/api/v1/elections/{election_id}/categories",
            json={"name": "President", "election_type": "single_choice"},
            headers=admin_user["headers"],
        )
        assert response.status_code == 201
        assert response.json()["name"] == "President"
        assert response.json()["election_type"] == "single_choice"

    async def test_add_candidate(self, client: AsyncClient, admin_user):
        # Create election
        create_resp = await client.post(
            "/api/v1/elections/",
            json={
                "title": "Candidate Test Election",
                "start_datetime": "2026-07-01T08:00:00Z",
                "end_datetime": "2026-07-01T20:00:00Z",
            },
            headers=admin_user["headers"],
        )
        election_id = create_resp.json()["election_id"]

        category_resp = await client.post(
            f"/api/v1/elections/{election_id}/categories",
            json={"name": "President", "election_type": "single_choice"},
            headers=admin_user["headers"],
        )
        category_id = category_resp.json()["category_id"]

        # Add candidate
        response = await client.post(
            f"/api/v1/elections/{election_id}/candidates",
            json={
                "category_id": category_id,
                "name": "Test Candidate",
                "description": "A test candidate",
            },
            headers=admin_user["headers"],
        )
        assert response.status_code == 201
        assert response.json()["name"] == "Test Candidate"
        assert response.json()["category_id"] == category_id

    async def test_add_candidate_invalid_category(self, client: AsyncClient, admin_user):
        create_resp = await client.post(
            "/api/v1/elections/",
            json={
                "title": "Invalid Category Election",
                "start_datetime": "2026-07-01T08:00:00Z",
                "end_datetime": "2026-07-01T20:00:00Z",
            },
            headers=admin_user["headers"],
        )
        election_id = create_resp.json()["election_id"]

        response = await client.post(
            f"/api/v1/elections/{election_id}/candidates",
            json={
                "category_id": "00000000-0000-0000-0000-000000000000",
                "name": "Test Candidate",
            },
            headers=admin_user["headers"],
        )
        assert response.status_code == 400

    async def test_list_candidates(self, client: AsyncClient, admin_user):
        # Create election with a category and two candidates
        create_resp = await client.post(
            "/api/v1/elections/",
            json={
                "title": "List Candidates Election",
                "start_datetime": "2026-08-01T08:00:00Z",
                "end_datetime": "2026-08-01T20:00:00Z",
            },
            headers=admin_user["headers"],
        )
        election_id = create_resp.json()["election_id"]

        category_resp = await client.post(
            f"/api/v1/elections/{election_id}/categories",
            json={"name": "President", "election_type": "single_choice"},
            headers=admin_user["headers"],
        )
        category_id = category_resp.json()["category_id"]

        await client.post(
            f"/api/v1/elections/{election_id}/candidates",
            json={"category_id": category_id, "name": "Candidate 1"},
            headers=admin_user["headers"],
        )
        await client.post(
            f"/api/v1/elections/{election_id}/candidates",
            json={"category_id": category_id, "name": "Candidate 2"},
            headers=admin_user["headers"],
        )

        response = await client.get(
            f"/api/v1/elections/{election_id}/candidates",
            headers=admin_user["headers"],
        )
        assert response.status_code == 200
        assert len(response.json()) == 2

    async def test_list_categories_with_candidates(self, client: AsyncClient, admin_user):
        create_resp = await client.post(
            "/api/v1/elections/",
            json={
                "title": "Nested Categories Election",
                "start_datetime": "2026-08-01T08:00:00Z",
                "end_datetime": "2026-08-01T20:00:00Z",
            },
            headers=admin_user["headers"],
        )
        election_id = create_resp.json()["election_id"]

        category_resp = await client.post(
            f"/api/v1/elections/{election_id}/categories",
            json={"name": "President", "election_type": "single_choice"},
            headers=admin_user["headers"],
        )
        category_id = category_resp.json()["category_id"]
        await client.post(
            f"/api/v1/elections/{election_id}/candidates",
            json={"category_id": category_id, "name": "Candidate 1"},
            headers=admin_user["headers"],
        )

        response = await client.get(
            f"/api/v1/elections/{election_id}/categories",
            headers=admin_user["headers"],
        )
        assert response.status_code == 200
        categories = response.json()
        assert len(categories) == 1
        assert len(categories[0]["candidates"]) == 1
