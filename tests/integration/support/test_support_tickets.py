import pytest
from httpx import AsyncClient


class TestSupportTickets:
    async def test_create_and_list_own_ticket(self, client: AsyncClient, test_user):
        create_res = await client.post(
            "/api/v1/support/tickets",
            json={"category": "Technical issue", "subject": "Broken link", "message": "The results page 404s."},
            headers=test_user["headers"],
        )
        assert create_res.status_code == 201
        data = create_res.json()
        assert data["status"] == "Open"
        assert data["priority"] is True
        assert len(data["messages"]) == 1
        assert data["messages"][0]["sender_type"] == "customer"

        list_res = await client.get("/api/v1/support/tickets", headers=test_user["headers"])
        assert list_res.status_code == 200
        assert len(list_res.json()) == 1

    async def test_invalid_category_rejected(self, client: AsyncClient, test_user):
        res = await client.post(
            "/api/v1/support/tickets",
            json={"category": "Not a real category", "subject": "x", "message": "y"},
            headers=test_user["headers"],
        )
        assert res.status_code == 422

    async def test_customer_reply_reopens_ticket(self, client: AsyncClient, test_user):
        create_res = await client.post(
            "/api/v1/support/tickets",
            json={"category": "Other", "subject": "Question", "message": "Hi"},
            headers=test_user["headers"],
        )
        ticket_id = create_res.json()["ticket_id"]

        reply_res = await client.post(
            f"/api/v1/support/tickets/{ticket_id}/messages",
            json={"text": "Following up"},
            headers=test_user["headers"],
        )
        assert reply_res.status_code == 200
        data = reply_res.json()
        assert data["status"] == "Open"
        assert len(data["messages"]) == 2

    async def test_outsider_cannot_view_ticket(self, client: AsyncClient, test_user, admin_user):
        create_res = await client.post(
            "/api/v1/support/tickets",
            json={"category": "Other", "subject": "Private", "message": "Hi"},
            headers=test_user["headers"],
        )
        ticket_id = create_res.json()["ticket_id"]

        res = await client.get(f"/api/v1/support/tickets/{ticket_id}", headers=admin_user["headers"])
        assert res.status_code == 404

    async def test_non_admin_cannot_access_admin_routes(self, client: AsyncClient, test_user):
        res = await client.get("/api/v1/support/admin/tickets", headers=test_user["headers"])
        assert res.status_code == 403

    async def test_admin_can_list_and_reply(self, client: AsyncClient, test_user, admin_user):
        create_res = await client.post(
            "/api/v1/support/tickets",
            json={"category": "Other", "subject": "Needs help", "message": "Hi"},
            headers=test_user["headers"],
        )
        ticket_id = create_res.json()["ticket_id"]

        list_res = await client.get("/api/v1/support/admin/tickets", headers=admin_user["headers"])
        assert list_res.status_code == 200
        assert any(t["ticket_id"] == ticket_id for t in list_res.json())

        reply_res = await client.post(
            f"/api/v1/support/admin/tickets/{ticket_id}/messages",
            json={"text": "We're on it"},
            headers=admin_user["headers"],
        )
        assert reply_res.status_code == 200
        assert reply_res.json()["status"] == "Replied"

    async def test_admin_can_update_status(self, client: AsyncClient, test_user, admin_user):
        create_res = await client.post(
            "/api/v1/support/tickets",
            json={"category": "Other", "subject": "Needs help", "message": "Hi"},
            headers=test_user["headers"],
        )
        ticket_id = create_res.json()["ticket_id"]

        res = await client.patch(
            f"/api/v1/support/admin/tickets/{ticket_id}/status",
            json={"status": "Resolved"},
            headers=admin_user["headers"],
        )
        assert res.status_code == 200
        assert res.json()["status"] == "Resolved"
