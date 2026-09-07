"""Platform-wide totals behind the admin console dashboard.

The dashboard renders five tiles (users / elections / events / tickets sold /
revenue) straight off this payload. The endpoint used to be an untyped `dict`
returning `active_elections`, `active_events` and `total_tickets_issued` with
no revenue at all, so four of the five tiles read `undefined` and rendered
"NaN". These tests pin the key names, the active-vs-total distinction, and the
pesewa normalisation of the two different revenue currencies.
"""
import json
from datetime import date, datetime, time, timedelta, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import generate_secure_token
from app.models.election import Election
from app.models.event import Event, TicketType
from app.models.ticket import Ticket, TicketPurchase
from app.models.transaction import Transaction
from app.models.vote import Vote


@pytest_asyncio.fixture
async def platform_activity(db_session: AsyncSession, admin_user) -> dict:
    """One active + one closed election, one published + one draft event.

    Revenue lands in both currencies the platform uses: election votes are
    paid in pesewas (Transaction.amount) and tickets in cedis
    (TicketPurchase.total_amount).
    """
    now = datetime.now(timezone.utc)
    creator = admin_user["user"].user_id

    active_election = Election(
        title="Live Election",
        election_type="single",
        start_datetime=now - timedelta(days=1),
        end_datetime=now + timedelta(days=1),
        status="active",
        created_by=creator,
    )
    closed_election = Election(
        title="Finished Election",
        election_type="single",
        start_datetime=now - timedelta(days=10),
        end_datetime=now - timedelta(days=5),
        status="closed",
        created_by=creator,
    )
    db_session.add_all([active_election, closed_election])
    await db_session.flush()

    # Weighted votes: 3 + 1 == 4 votes cast across 2 rows.
    db_session.add_all([
        Vote(
            election_id=active_election.election_id,
            voter_hash="hash-a",
            vote_data=b"encrypted-a",
            vote_signature="sig-a",
            count=3,
        ),
        Vote(
            election_id=closed_election.election_id,
            voter_hash="hash-b",
            vote_data=b"encrypted-b",
            vote_signature="sig-b",
            count=1,
        ),
    ])

    # Only successful transactions are revenue.
    db_session.add_all([
        Transaction(
            reference=f"ref-success-{uuid4().hex[:8]}",
            election_id=active_election.election_id,
            voter_hash="hash-a",
            candidate_ids=[str(uuid4())],
            amount=5000,
            status="success",
        ),
        Transaction(
            reference=f"ref-failed-{uuid4().hex[:8]}",
            election_id=active_election.election_id,
            voter_hash="hash-c",
            candidate_ids=[str(uuid4())],
            amount=2000,
            status="failed",
        ),
    ])

    published_event = Event(
        title="Published Event",
        event_date=date(2026, 12, 1),
        event_time=time(19, 0),
        location="Accra Arena",
        status="published",
        created_by=creator,
    )
    draft_event = Event(
        title="Draft Event",
        event_date=date(2026, 12, 20),
        event_time=time(19, 0),
        location="Kumasi Hall",
        status="draft",
        created_by=creator,
    )
    db_session.add_all([published_event, draft_event])
    await db_session.flush()

    ga = TicketType(
        event_id=published_event.event_id,
        type_name="General Admission",
        price=50,
        quantity_available=100,
    )
    db_session.add(ga)
    await db_session.flush()

    paid = TicketPurchase(
        guest_name="Buyer One",
        guest_email="buyer.one@example.com",
        event_id=published_event.event_id,
        total_amount=100,
        payment_status="completed",
    )
    unpaid = TicketPurchase(
        guest_name="Buyer Two",
        guest_email="buyer.two@example.com",
        event_id=published_event.event_id,
        total_amount=50,
        payment_status="pending",
    )
    db_session.add_all([paid, unpaid])
    await db_session.flush()

    def make(status="valid"):
        code = generate_secure_token(16)
        return Ticket(
            ticket_code=code,
            event_id=published_event.event_id,
            ticket_type_id=ga.ticket_type_id,
            guest_name="Buyer One",
            guest_email="buyer.one@example.com",
            purchase_id=paid.purchase_id,
            qr_code_data=json.dumps({"ticket_code": code}),
            ticket_status=status,
        )

    # 2 sold; the cancelled one must not count as sold.
    db_session.add_all([make(), make(), make("cancelled")])
    await db_session.flush()

    return {"active_election": active_election, "published_event": published_event}


@pytest.mark.asyncio
class TestSystemAnalytics:
    async def test_requires_admin(self, client: AsyncClient, test_user):
        response = await client.get(
            "/api/v1/analytics/system", headers=test_user["headers"]
        )
        assert response.status_code == 403

    async def test_counts_elections_and_events_as_totals_and_active(
        self, client: AsyncClient, admin_user, platform_activity
    ):
        body = (
            await client.get(
                "/api/v1/analytics/system", headers=admin_user["headers"]
            )
        ).json()

        assert body["total_elections"] == 2
        assert body["active_elections"] == 1
        assert body["total_events"] == 2
        assert body["active_events"] == 1

    async def test_tickets_sold_excludes_cancelled(
        self, client: AsyncClient, admin_user, platform_activity
    ):
        body = (
            await client.get(
                "/api/v1/analytics/system", headers=admin_user["headers"]
            )
        ).json()

        assert body["total_tickets_sold"] == 2

    async def test_votes_cast_sums_weighted_counts(
        self, client: AsyncClient, admin_user, platform_activity
    ):
        body = (
            await client.get(
                "/api/v1/analytics/system", headers=admin_user["headers"]
            )
        ).json()

        assert body["total_votes_cast"] == 4

    async def test_revenue_normalises_both_currencies_to_pesewas(
        self, client: AsyncClient, admin_user, platform_activity
    ):
        body = (
            await client.get(
                "/api/v1/analytics/system", headers=admin_user["headers"]
            )
        ).json()

        # Only the successful transaction counts: 5000 pesewas.
        assert body["total_election_revenue_pesewas"] == 5000
        # Only the completed purchase counts: GH¢100.00 -> 10000 pesewas.
        assert body["total_event_revenue_pesewas"] == 10000
        assert body["total_revenue_pesewas"] == 15000

    async def test_counts_users(
        self, client: AsyncClient, admin_user, platform_activity
    ):
        body = (
            await client.get(
                "/api/v1/analytics/system", headers=admin_user["headers"]
            )
        ).json()

        assert body["total_users"] >= 1

    async def test_empty_platform_reports_zeros_not_nulls(
        self, client: AsyncClient, admin_user
    ):
        """No activity must still yield numbers, so the UI never renders NaN."""
        body = (
            await client.get(
                "/api/v1/analytics/system", headers=admin_user["headers"]
            )
        ).json()

        for key in (
            "total_elections",
            "active_elections",
            "total_events",
            "active_events",
            "total_votes_cast",
            "total_tickets_sold",
            "total_election_revenue_pesewas",
            "total_event_revenue_pesewas",
            "total_revenue_pesewas",
        ):
            assert body[key] == 0, key

    async def test_response_is_documented_in_openapi(self, client: AsyncClient):
        """An untyped dict is what let the frontend contract drift unnoticed."""
        schema = (await client.get("/openapi.json")).json()
        ref = schema["paths"]["/api/v1/analytics/system"]["get"]["responses"]["200"][
            "content"
        ]["application/json"]["schema"]

        assert ref, "endpoint must declare a response_model"
