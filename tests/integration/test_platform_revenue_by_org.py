"""Platform revenue, and how it breaks down by organisation.

The dashboard's Revenue tile is a single platform-wide figure, but the only
place revenue was previously visible was one organisation at a time, via
/admin/organizations/{id}/analytics. That made the platform total impossible
to sanity-check against the per-org numbers an admin had already seen.

These tests pin two things: the platform total reconciles with the sum of the
per-org figures, and the payload carries a per-organisation breakdown so the
console can show which organisations are actually generating revenue.
"""
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.models.election import Category, Election
from app.models.event import Event
from app.models.organization import Organization, OrganizationMember
from app.models.ticket import TicketPurchase
from app.models.transaction import Transaction
from app.models.user import User


async def _make_org(
    db: AsyncSession,
    name: str,
    *,
    election_pesewas: int = 0,
    event_cedis: str = "0",
) -> Organization:
    """An organisation with one member who owns any revenue-bearing activity."""
    owner = User(
        email=f"owner-{uuid4().hex[:8]}@example.com",
        password_hash=hash_password("Owner1234!"),
        full_name=f"{name} Owner",
        email_verified=True,
        account_status="active",
    )
    db.add(owner)
    await db.flush()

    org = Organization(name=name, owner_id=owner.user_id)
    db.add(org)
    await db.flush()

    db.add(OrganizationMember(org_id=org.org_id, user_id=owner.user_id, role="owner"))
    await db.flush()

    now = datetime.now(timezone.utc)

    if election_pesewas:
        election = Election(
            title=f"{name} Election",
            start_datetime=now - timedelta(days=1),
            end_datetime=now + timedelta(days=1),
            status="active",
            created_by=owner.user_id,
        )
        db.add(election)
        await db.flush()
        # Transactions hang off a category now, not the election directly.
        category = Category(
            election_id=election.election_id,
            name="Best Overall",
            election_type="single_choice",
        )
        db.add(category)
        await db.flush()
        db.add(Transaction(
            reference=f"ref-{uuid4().hex[:10]}",
            election_id=election.election_id,
            category_id=category.category_id,
            voter_hash=f"hash-{uuid4().hex[:8]}",
            candidate_ids=[str(uuid4())],
            amount=election_pesewas,
            status="success",
        ))

    if Decimal(event_cedis):
        event = Event(
            title=f"{name} Event",
            event_date=date(2026, 12, 1),
            event_time=time(19, 0),
            location="Accra",
            status="published",
            created_by=owner.user_id,
        )
        db.add(event)
        await db.flush()
        db.add(TicketPurchase(
            guest_name="Buyer",
            guest_email=f"buyer-{uuid4().hex[:8]}@example.com",
            event_id=event.event_id,
            total_amount=Decimal(event_cedis),
            payment_status="completed",
        ))

    await db.flush()
    return org


@pytest_asyncio.fixture
async def two_orgs(db_session: AsyncSession) -> dict:
    """Org A earns from votes only, org B from tickets only, org C earns nothing."""
    a = await _make_org(db_session, "Org A Votes", election_pesewas=1500)
    b = await _make_org(db_session, "Org B Tickets", event_cedis="50.00")
    c = await _make_org(db_session, "Org C Idle")
    return {"a": a, "b": b, "c": c}


@pytest.mark.asyncio
class TestPlatformTotalReconciles:
    async def test_total_equals_sum_of_org_revenue(
        self, client: AsyncClient, admin_user, two_orgs
    ):
        """The headline figure must be the sum of what each org earned."""
        body = (
            await client.get(
                "/api/v1/analytics/system", headers=admin_user["headers"]
            )
        ).json()

        # Org A: 1500 pesewas of votes -> GH¢15.00. Org B: GH¢50.00 of tickets.
        assert body["total_election_revenue_ghs"] == 15.00
        assert body["total_event_revenue_ghs"] == 50.00
        assert body["total_revenue_ghs"] == 65.00

        summed = sum(o["total_revenue_ghs"] for o in body["organizations"])
        assert summed == body["total_revenue_ghs"]

    async def test_per_org_figures_match_the_org_analytics_endpoint(
        self, client: AsyncClient, admin_user, two_orgs
    ):
        """A figure here must agree with the org drill-in an admin already sees."""
        body = (
            await client.get(
                "/api/v1/analytics/system", headers=admin_user["headers"]
            )
        ).json()
        by_id = {o["org_id"]: o for o in body["organizations"]}

        org_a = str(two_orgs["a"].org_id)
        drill_in = (
            await client.get(
                f"/api/v1/admin/organizations/{org_a}/analytics",
                headers=admin_user["headers"],
            )
        ).json()

        assert by_id[org_a]["vote_revenue_ghs"] == (
            drill_in["total_election_revenue_pesewas"] / 100
        )
        assert by_id[org_a]["ticket_revenue_ghs"] == drill_in["total_event_revenue_ghs"]


@pytest.mark.asyncio
class TestOrganizationBreakdown:
    async def test_lists_every_organization_including_zero_revenue(
        self, client: AsyncClient, admin_user, two_orgs
    ):
        body = (
            await client.get(
                "/api/v1/analytics/system", headers=admin_user["headers"]
            )
        ).json()
        names = {o["name"] for o in body["organizations"]}

        assert {"Org A Votes", "Org B Tickets", "Org C Idle"} <= names
        idle = next(o for o in body["organizations"] if o["name"] == "Org C Idle")
        assert idle["total_revenue_ghs"] == 0.0

    async def test_sorted_by_revenue_descending(
        self, client: AsyncClient, admin_user, two_orgs
    ):
        body = (
            await client.get(
                "/api/v1/analytics/system", headers=admin_user["headers"]
            )
        ).json()
        totals = [o["total_revenue_ghs"] for o in body["organizations"]]

        assert totals == sorted(totals, reverse=True)
        # Org B (GH¢50) out-earns org A (GH¢15), so it must lead.
        assert body["organizations"][0]["name"] == "Org B Tickets"

    async def test_splits_vote_and_ticket_revenue_per_org(
        self, client: AsyncClient, admin_user, two_orgs
    ):
        body = (
            await client.get(
                "/api/v1/analytics/system", headers=admin_user["headers"]
            )
        ).json()
        by_name = {o["name"]: o for o in body["organizations"]}

        assert by_name["Org A Votes"]["vote_revenue_ghs"] == 15.00
        assert by_name["Org A Votes"]["ticket_revenue_ghs"] == 0.0
        assert by_name["Org B Tickets"]["vote_revenue_ghs"] == 0.0
        assert by_name["Org B Tickets"]["ticket_revenue_ghs"] == 50.00

    async def test_requires_admin(self, client: AsyncClient, test_user):
        response = await client.get(
            "/api/v1/analytics/system", headers=test_user["headers"]
        )
        assert response.status_code == 403
