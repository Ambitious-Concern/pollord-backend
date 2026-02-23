import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
class TestEventCRUD:
    async def test_create_event(self, client: AsyncClient, admin_user):
        response = await client.post(
            "/api/v1/events/",
            json={
                "title": "Test Event",
                "description": "A test event",
                "event_date": "2026-06-15",
                "event_time": "18:00:00",
                "location": "Test Venue",
                "category": "Conference",
                "capacity": 500,
            },
            headers=admin_user["headers"],
        )
        assert response.status_code == 201
        data = response.json()
        assert data["title"] == "Test Event"
        assert data["status"] == "draft"

    async def test_create_event_unauthorized(self, client: AsyncClient, test_user):
        response = await client.post(
            "/api/v1/events/",
            json={
                "title": "Unauthorized Event",
                "event_date": "2026-06-15",
                "event_time": "18:00:00",
                "location": "Test Venue",
            },
            headers=test_user["headers"],
        )
        assert response.status_code == 403

    async def test_list_events(self, client: AsyncClient, test_user):
        response = await client.get(
            "/api/v1/events/",
            headers=test_user["headers"],
        )
        assert response.status_code == 200
        assert isinstance(response.json(), list)

    async def test_publish_event(self, client: AsyncClient, admin_user):
        # Create event
        create_resp = await client.post(
            "/api/v1/events/",
            json={
                "title": "Publish Test Event",
                "event_date": "2026-07-01",
                "event_time": "10:00:00",
                "location": "Publish Venue",
            },
            headers=admin_user["headers"],
        )
        event_id = create_resp.json()["event_id"]

        # Publish
        response = await client.post(
            f"/api/v1/events/{event_id}/publish",
            headers=admin_user["headers"],
        )
        assert response.status_code == 200

        # Verify
        get_resp = await client.get(
            f"/api/v1/events/{event_id}",
        )
        assert get_resp.json()["status"] == "published"
