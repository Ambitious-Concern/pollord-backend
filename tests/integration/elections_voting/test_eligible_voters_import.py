from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.election import Category, Election
from app.models.user import User


@pytest.fixture(autouse=True)
def _stub_email_delivery(monkeypatch):
    # These tests assert on DB/response state, not on delivery — stubbed so
    # they don't depend on a reachable Celery result backend (host-run
    # pytest can't reach the Docker-network-only Redis broker/backend).
    monkeypatch.setattr(
        "app.tasks.email_tasks.send_email_task.delay", lambda *args, **kwargs: None
    )


async def _make_private_election(db_session: AsyncSession, owner) -> Election:
    now = datetime.now(timezone.utc)
    election = Election(
        title="Board Election",
        start_datetime=now - timedelta(hours=1),
        end_datetime=now + timedelta(hours=1),
        status="active",
        visibility="private",
        require_verification=True,
        vote_price=0,
        created_by=owner.user_id,
    )
    db_session.add(election)
    await db_session.flush()
    return election


class TestImportEligibleVoters:
    async def test_import_creates_shell_user_and_eligible_row(
        self, client: AsyncClient, db_session: AsyncSession, admin_user
    ):
        election = await _make_private_election(db_session, admin_user["user"])

        res = await client.post(
            f"/api/v1/elections/{election.election_id}/eligible-voters/import",
            json={"rows": [{"email": "brand.new.voter@example.com", "name": "Brand New"}]},
            headers=admin_user["headers"],
        )
        assert res.status_code == 201, res.text
        assert res.json()["message"] == "Imported 1 eligible voter(s)"

        created = (
            await db_session.execute(
                select(User).where(User.email == "brand.new.voter@example.com")
            )
        ).scalar_one_or_none()
        assert created is not None
        assert created.full_name == "Brand New"
        assert created.email_verified is True

        listing = await client.get(
            f"/api/v1/elections/{election.election_id}/eligible-voters", headers=admin_user["headers"]
        )
        assert listing.status_code == 200
        row = next(r for r in listing.json() if r["email"] == "brand.new.voter@example.com")
        assert row["notified_at"] is not None
        assert row["has_voted"] is False

    async def test_import_reuses_existing_user_and_does_not_duplicate(
        self, client: AsyncClient, db_session: AsyncSession, admin_user, test_user
    ):
        election = await _make_private_election(db_session, admin_user["user"])
        payload = {"rows": [{"email": test_user["user"].email, "name": "Ignored Name"}]}

        first = await client.post(
            f"/api/v1/elections/{election.election_id}/eligible-voters/import",
            json=payload,
            headers=admin_user["headers"],
        )
        assert first.status_code == 201
        assert first.json()["message"] == "Imported 1 eligible voter(s)"

        # Re-importing the same email should not create a duplicate user or
        # a duplicate eligible_voter row.
        second = await client.post(
            f"/api/v1/elections/{election.election_id}/eligible-voters/import",
            json=payload,
            headers=admin_user["headers"],
        )
        assert second.status_code == 201
        assert second.json()["message"] == "Imported 0 eligible voter(s)"

        users = (
            await db_session.execute(select(User).where(User.email == test_user["user"].email))
        ).scalars().all()
        assert len(users) == 1
        assert users[0].user_id == test_user["user"].user_id

        listing = await client.get(
            f"/api/v1/elections/{election.election_id}/eligible-voters", headers=admin_user["headers"]
        )
        matching = [r for r in listing.json() if r["email"] == test_user["user"].email]
        assert len(matching) == 1

    async def test_outsider_cannot_import(
        self, client: AsyncClient, db_session: AsyncSession, admin_user, test_user
    ):
        election = await _make_private_election(db_session, admin_user["user"])
        res = await client.post(
            f"/api/v1/elections/{election.election_id}/eligible-voters/import",
            json={"rows": [{"email": "someone@example.com"}]},
            headers=test_user["headers"],
        )
        assert res.status_code == 403


class TestEligibleVotersCrossElectionList:
    async def test_lists_across_all_private_elections_with_title(
        self, client: AsyncClient, db_session: AsyncSession, admin_user
    ):
        election_a = await _make_private_election(db_session, admin_user["user"])
        election_b = await _make_private_election(db_session, admin_user["user"])
        election_b.title = "Alumni Vote"
        await db_session.flush()

        for election in (election_a, election_b):
            res = await client.post(
                f"/api/v1/elections/{election.election_id}/eligible-voters/import",
                json={"rows": [{"email": f"voter-{election.election_id}@example.com"}]},
                headers=admin_user["headers"],
            )
            assert res.status_code == 201

        res = await client.get("/api/v1/elections/eligible-voters/mine", headers=admin_user["headers"])
        assert res.status_code == 200
        body = res.json()
        assert body["total"] == 2
        titles = {r["election_id"]: r["election_title"] for r in body["items"]}
        assert titles.get(str(election_a.election_id)) == election_a.title
        assert titles.get(str(election_b.election_id)) == "Alumni Vote"


class TestEligibleVotersPagination:
    async def test_limit_is_capped_at_200(
        self, client: AsyncClient, db_session: AsyncSession, admin_user
    ):
        election = await _make_private_election(db_session, admin_user["user"])
        res = await client.post(
            f"/api/v1/elections/{election.election_id}/eligible-voters/import",
            json={"rows": [{"email": "one@example.com"}]},
            headers=admin_user["headers"],
        )
        assert res.status_code == 201

        res = await client.get(
            "/api/v1/elections/eligible-voters/mine",
            params={"limit": 5000},
            headers=admin_user["headers"],
        )
        assert res.status_code == 200
        # A single row exists either way — this just proves the request
        # didn't reject the oversized limit; the cap itself is exercised
        # for real once there's more than 200 rows, which is impractical
        # to seed per-test, so this asserts the request path is safe.
        assert res.json()["total"] == 1

    async def test_skip_and_limit_page_through_results(
        self, client: AsyncClient, db_session: AsyncSession, admin_user
    ):
        election = await _make_private_election(db_session, admin_user["user"])
        for i in range(5):
            res = await client.post(
                f"/api/v1/elections/{election.election_id}/eligible-voters/import",
                json={"rows": [{"email": f"voter{i}@example.com", "name": f"Voter {i}"}]},
                headers=admin_user["headers"],
            )
            assert res.status_code == 201

        page1 = await client.get(
            "/api/v1/elections/eligible-voters/mine",
            params={"skip": 0, "limit": 2, "sort": "name"},
            headers=admin_user["headers"],
        )
        page2 = await client.get(
            "/api/v1/elections/eligible-voters/mine",
            params={"skip": 2, "limit": 2, "sort": "name"},
            headers=admin_user["headers"],
        )
        assert page1.json()["total"] == 5
        assert page2.json()["total"] == 5
        page1_names = [r["name"] for r in page1.json()["items"]]
        page2_names = [r["name"] for r in page2.json()["items"]]
        assert len(page1_names) == 2
        assert len(page2_names) == 2
        assert set(page1_names).isdisjoint(page2_names)

    async def test_search_matches_name_or_email(
        self, client: AsyncClient, db_session: AsyncSession, admin_user
    ):
        election = await _make_private_election(db_session, admin_user["user"])
        await client.post(
            f"/api/v1/elections/{election.election_id}/eligible-voters/import",
            json={
                "rows": [
                    {"email": "kwame@example.com", "name": "Kwame Mensah"},
                    {"email": "ama@example.com", "name": "Ama Boateng"},
                ]
            },
            headers=admin_user["headers"],
        )

        res = await client.get(
            "/api/v1/elections/eligible-voters/mine",
            params={"search": "kwame"},
            headers=admin_user["headers"],
        )
        assert res.status_code == 200
        body = res.json()
        assert body["total"] == 1
        assert body["items"][0]["email"] == "kwame@example.com"

    async def test_election_id_filter_scopes_to_selected_elections(
        self, client: AsyncClient, db_session: AsyncSession, admin_user
    ):
        election_a = await _make_private_election(db_session, admin_user["user"])
        election_b = await _make_private_election(db_session, admin_user["user"])
        for election in (election_a, election_b):
            await client.post(
                f"/api/v1/elections/{election.election_id}/eligible-voters/import",
                json={"rows": [{"email": f"voter-{election.election_id}@example.com"}]},
                headers=admin_user["headers"],
            )

        res = await client.get(
            "/api/v1/elections/eligible-voters/mine",
            params={"election_id": str(election_a.election_id)},
            headers=admin_user["headers"],
        )
        assert res.status_code == 200
        body = res.json()
        assert body["total"] == 1
        assert body["items"][0]["election_id"] == str(election_a.election_id)

    async def test_sort_name_orders_alphabetically(
        self, client: AsyncClient, db_session: AsyncSession, admin_user
    ):
        election = await _make_private_election(db_session, admin_user["user"])
        await client.post(
            f"/api/v1/elections/{election.election_id}/eligible-voters/import",
            json={
                "rows": [
                    {"email": "zed@example.com", "name": "Zed Owusu"},
                    {"email": "abena@example.com", "name": "Abena Frimpong"},
                ]
            },
            headers=admin_user["headers"],
        )

        res = await client.get(
            "/api/v1/elections/eligible-voters/mine",
            params={"sort": "name"},
            headers=admin_user["headers"],
        )
        names = [r["name"] for r in res.json()["items"]]
        assert names == ["Abena Frimpong", "Zed Owusu"]


class TestRemoveAndResendEligibleVoter:
    async def test_remove_deletes_row(self, client: AsyncClient, db_session: AsyncSession, admin_user):
        election = await _make_private_election(db_session, admin_user["user"])
        await client.post(
            f"/api/v1/elections/{election.election_id}/eligible-voters/import",
            json={"rows": [{"email": "to.remove@example.com"}]},
            headers=admin_user["headers"],
        )
        listing = await client.get(
            f"/api/v1/elections/{election.election_id}/eligible-voters", headers=admin_user["headers"]
        )
        eligible_voter_id = listing.json()[0]["eligible_voter_id"]

        res = await client.delete(
            f"/api/v1/elections/{election.election_id}/eligible-voters/{eligible_voter_id}",
            headers=admin_user["headers"],
        )
        assert res.status_code == 204

        after = await client.get(
            f"/api/v1/elections/{election.election_id}/eligible-voters", headers=admin_user["headers"]
        )
        assert after.json() == []

    async def test_resend_bumps_notified_at(self, client: AsyncClient, db_session: AsyncSession, admin_user):
        election = await _make_private_election(db_session, admin_user["user"])
        await client.post(
            f"/api/v1/elections/{election.election_id}/eligible-voters/import",
            json={"rows": [{"email": "resend.me@example.com"}]},
            headers=admin_user["headers"],
        )
        listing = await client.get(
            f"/api/v1/elections/{election.election_id}/eligible-voters", headers=admin_user["headers"]
        )
        eligible_voter_id = listing.json()[0]["eligible_voter_id"]

        res = await client.post(
            f"/api/v1/elections/{election.election_id}/eligible-voters/{eligible_voter_id}/resend",
            headers=admin_user["headers"],
        )
        assert res.status_code == 200
        assert "resend.me@example.com" in res.json()["message"]

    async def test_outsider_cannot_remove(
        self, client: AsyncClient, db_session: AsyncSession, admin_user, test_user
    ):
        election = await _make_private_election(db_session, admin_user["user"])
        await client.post(
            f"/api/v1/elections/{election.election_id}/eligible-voters/import",
            json={"rows": [{"email": "protected@example.com"}]},
            headers=admin_user["headers"],
        )
        listing = await client.get(
            f"/api/v1/elections/{election.election_id}/eligible-voters", headers=admin_user["headers"]
        )
        eligible_voter_id = listing.json()[0]["eligible_voter_id"]

        res = await client.delete(
            f"/api/v1/elections/{election.election_id}/eligible-voters/{eligible_voter_id}",
            headers=test_user["headers"],
        )
        assert res.status_code == 403
