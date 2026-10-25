"""
Integration tests for the new Event category-voting capability.

Events previously had zero voting concept (ticket sales only). This covers
the new /events/{id}/categories + /events/{id}/candidates CRUD and the
category_id-driven public voting/results endpoints working the same way for
an event-owned category as they already do for an election-owned one.
"""
from datetime import date, time

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
class TestEventCategoryCRUD:
    async def _create_draft_event(self, client: AsyncClient, admin_user) -> str:
        resp = await client.post(
            "/api/v1/events/",
            json={
                "title": "Awards Night",
                "description": "An event with voting categories",
                "event_date": str(date(2026, 12, 1)),
                "event_time": str(time(18, 0)),
                "location": "Accra",
                "capacity": 500,
            },
            headers=admin_user["headers"],
        )
        assert resp.status_code == 201, resp.text
        return resp.json()["event_id"]

    async def test_add_category_and_candidate(self, client: AsyncClient, admin_user):
        event_id = await self._create_draft_event(client, admin_user)

        cat_resp = await client.post(
            f"/api/v1/events/{event_id}/categories",
            json={"name": "Best Dressed", "election_type": "single_choice"},
            headers=admin_user["headers"],
        )
        assert cat_resp.status_code == 201, cat_resp.text
        category_id = cat_resp.json()["category_id"]

        cand_resp = await client.post(
            f"/api/v1/events/{event_id}/candidates",
            json={"category_id": category_id, "name": "Contestant A"},
            headers=admin_user["headers"],
        )
        assert cand_resp.status_code == 201, cand_resp.text
        assert cand_resp.json()["category_id"] == category_id

    async def test_publish_fails_with_empty_category(self, client: AsyncClient, admin_user):
        event_id = await self._create_draft_event(client, admin_user)
        await client.post(
            f"/api/v1/events/{event_id}/categories",
            json={"name": "Best Dressed", "election_type": "single_choice"},
            headers=admin_user["headers"],
        )

        resp = await client.post(
            f"/api/v1/events/{event_id}/publish", headers=admin_user["headers"]
        )
        assert resp.status_code == 400

    async def test_publish_succeeds_with_no_categories(self, client: AsyncClient, admin_user):
        """Events are ticket-only by default — zero categories is valid."""
        event_id = await self._create_draft_event(client, admin_user)
        resp = await client.post(
            f"/api/v1/events/{event_id}/publish", headers=admin_user["headers"]
        )
        assert resp.status_code == 200


@pytest.mark.asyncio
class TestEventPublicVoting:
    async def _create_published_event_with_category(
        self, client: AsyncClient, admin_user
    ) -> dict:
        # Voting on an event is only open from its own start time through the
        # end of that same calendar day, so it has to be "today" for these
        # tests to actually be able to cast a vote — a fixed future date
        # would make _assert_open_for_voting reject every cast below.
        resp = await client.post(
            "/api/v1/events/",
            json={
                "title": "Awards Night",
                "event_date": str(date.today()),
                "event_time": str(time(0, 0)),
                "location": "Accra",
            },
            headers=admin_user["headers"],
        )
        event_id = resp.json()["event_id"]

        cat_resp = await client.post(
            f"/api/v1/events/{event_id}/categories",
            json={"name": "Best Dressed", "election_type": "single_choice"},
            headers=admin_user["headers"],
        )
        category_id = cat_resp.json()["category_id"]

        cand_resp = await client.post(
            f"/api/v1/events/{event_id}/candidates",
            json={"category_id": category_id, "name": "Contestant A"},
            headers=admin_user["headers"],
        )
        candidate_id = cand_resp.json()["candidate_id"]

        publish_resp = await client.post(
            f"/api/v1/events/{event_id}/publish", headers=admin_user["headers"]
        )
        assert publish_resp.status_code == 200

        return {"event_id": event_id, "category_id": category_id, "candidate_id": candidate_id}

    async def test_public_ballot_no_auth_required(self, client: AsyncClient, admin_user):
        ctx = await self._create_published_event_with_category(client, admin_user)

        resp = await client.get(f"/api/v1/voting/public/events/{ctx['event_id']}/ballot")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["categories"]) == 1
        assert len(data["categories"][0]["candidates"]) == 1

    async def test_cast_free_vote_no_auth(self, client: AsyncClient, admin_user):
        ctx = await self._create_published_event_with_category(client, admin_user)

        resp = await client.post(
            "/api/v1/voting/public/cast",
            json={
                "category_id": ctx["category_id"],
                "candidate_ids": [ctx["candidate_id"]],
            },
        )
        assert resp.status_code == 201, resp.text
        assert "receipt_code" in resp.json()

    async def test_duplicate_vote_rejected(self, client: AsyncClient, admin_user):
        ctx = await self._create_published_event_with_category(client, admin_user)
        payload = {
            "category_id": ctx["category_id"],
            "candidate_ids": [ctx["candidate_id"]],
        }
        r1 = await client.post("/api/v1/voting/public/cast", json=payload)
        assert r1.status_code == 201
        r2 = await client.post("/api/v1/voting/public/cast", json=payload)
        assert r2.status_code == 409

    async def test_live_results_reflect_cast_vote(self, client: AsyncClient, admin_user):
        ctx = await self._create_published_event_with_category(client, admin_user)
        await client.post(
            "/api/v1/voting/public/cast",
            json={
                "category_id": ctx["category_id"],
                "candidate_ids": [ctx["candidate_id"]],
            },
        )

        resp = await client.get(
            f"/api/v1/voting/public/events/{ctx['event_id']}/live-results"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_votes"] == 1
        assert data["categories"][0]["results"][0]["vote_count"] == 1

    async def test_unpublished_event_rejects_votes(self, client: AsyncClient, admin_user):
        """Voting on a draft (not yet published) event's category must fail."""
        resp = await client.post(
            "/api/v1/events/",
            json={
                "title": "Draft Awards Night",
                "event_date": str(date(2026, 12, 1)),
                "event_time": str(time(18, 0)),
                "location": "Accra",
            },
            headers=admin_user["headers"],
        )
        event_id = resp.json()["event_id"]
        cat_resp = await client.post(
            f"/api/v1/events/{event_id}/categories",
            json={"name": "Best Dressed", "election_type": "single_choice"},
            headers=admin_user["headers"],
        )
        category_id = cat_resp.json()["category_id"]
        cand_resp = await client.post(
            f"/api/v1/events/{event_id}/candidates",
            json={"category_id": category_id, "name": "Contestant A"},
            headers=admin_user["headers"],
        )
        candidate_id = cand_resp.json()["candidate_id"]

        vote_resp = await client.post(
            "/api/v1/voting/public/cast",
            json={"category_id": category_id, "candidate_ids": [candidate_id]},
        )
        assert vote_resp.status_code == 400
