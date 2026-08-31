"""Platform-admin drill-in detail for one event / one election."""
import json
from datetime import date, datetime, time, timedelta, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import generate_secure_token
from app.models.election import Candidate, Category, Election
from app.models.event import Event, TicketType
from app.models.ticket import Ticket, TicketPurchase
from app.models.transaction import Transaction
from app.models.vote import Vote
from app.services.cryptography_service import CryptographyService


# ── Event detail ─────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def detailed_event(db_session: AsyncSession, admin_user) -> dict:
    """An event with two tiers: 2 issued GA tickets (1 used) and 1 VIP."""
    event = Event(
        title="Detail Test Event",
        description="An event with real sales",
        event_date=date(2026, 11, 1),
        event_time=time(18, 30),
        location="Accra Arena",
        category="Concert",
        capacity=200,
        status="published",
        created_by=admin_user["user"].user_id,
    )
    db_session.add(event)
    await db_session.flush()

    ga = TicketType(
        event_id=event.event_id,
        type_name="General Admission",
        price=50,
        quantity_available=100,
        max_per_user=5,
    )
    vip = TicketType(
        event_id=event.event_id,
        type_name="VIP",
        price=150,
        quantity_available=20,
        max_per_user=2,
    )
    db_session.add_all([ga, vip])
    await db_session.flush()

    purchase = TicketPurchase(
        guest_name="Buyer One",
        guest_email="buyer.one@example.com",
        event_id=event.event_id,
        total_amount=250,
        payment_status="completed",
    )
    db_session.add(purchase)
    await db_session.flush()

    def make(ticket_type, status="valid"):
        code = generate_secure_token(16)
        return Ticket(
            ticket_code=code,
            event_id=event.event_id,
            ticket_type_id=ticket_type.ticket_type_id,
            guest_name="Buyer One",
            guest_email="buyer.one@example.com",
            purchase_id=purchase.purchase_id,
            qr_code_data=json.dumps({"ticket_code": code}),
            ticket_status=status,
        )

    db_session.add_all([make(ga), make(ga, "used"), make(vip)])
    # A cancelled ticket must not count toward issued or revenue.
    db_session.add(make(vip, "cancelled"))
    await db_session.flush()

    return {"event": event, "ga": ga, "vip": vip, "purchase": purchase}


@pytest.mark.asyncio
class TestEventDetail:
    async def test_requires_admin(self, client: AsyncClient, test_user, detailed_event):
        response = await client.get(
            f"/api/v1/admin/events/{detailed_event['event'].event_id}",
            headers=test_user["headers"],
        )
        assert response.status_code == 403

    async def test_unknown_event_returns_404(self, client: AsyncClient, admin_user):
        response = await client.get(
            f"/api/v1/admin/events/{uuid4()}", headers=admin_user["headers"]
        )
        assert response.status_code == 404

    async def test_returns_event_record_and_organizer(
        self, client: AsyncClient, admin_user, detailed_event
    ):
        response = await client.get(
            f"/api/v1/admin/events/{detailed_event['event'].event_id}",
            headers=admin_user["headers"],
        )
        assert response.status_code == 200
        body = response.json()
        assert body["title"] == "Detail Test Event"
        assert body["location"] == "Accra Arena"
        assert body["capacity"] == 200
        assert body["organizer_email"] == admin_user["user"].email

    async def test_ticket_type_breakdown_excludes_cancelled(
        self, client: AsyncClient, admin_user, detailed_event
    ):
        response = await client.get(
            f"/api/v1/admin/events/{detailed_event['event'].event_id}",
            headers=admin_user["headers"],
        )
        types = {t["type_name"]: t for t in response.json()["ticket_types"]}

        assert types["General Admission"]["tickets_issued"] == 2
        assert types["General Admission"]["tickets_used"] == 1
        assert float(types["General Admission"]["revenue"]) == 100.0

        # 2 VIP rows exist but one is cancelled, so only 1 counts.
        assert types["VIP"]["tickets_issued"] == 1
        assert float(types["VIP"]["revenue"]) == 150.0

    async def test_totals_roll_up(
        self, client: AsyncClient, admin_user, detailed_event
    ):
        body = (
            await client.get(
                f"/api/v1/admin/events/{detailed_event['event'].event_id}",
                headers=admin_user["headers"],
            )
        ).json()
        assert body["total_tickets_issued"] == 3
        assert body["total_tickets_used"] == 1
        assert body["total_purchases"] == 1
        assert float(body["total_revenue"]) == 250.0

    async def test_surfaces_undelivered_ticket_emails(
        self, client: AsyncClient, db_session, admin_user, detailed_event
    ):
        """The count an admin would act on from this page."""
        body = (
            await client.get(
                f"/api/v1/admin/events/{detailed_event['event'].event_id}",
                headers=admin_user["headers"],
            )
        ).json()
        # Never attempted, so unknown rather than failed.
        assert body["purchases_email_unknown"] == 1
        assert body["purchases_email_failed"] == 0

        detailed_event["purchase"].confirmation_email_status = "failed"
        await db_session.flush()

        body = (
            await client.get(
                f"/api/v1/admin/events/{detailed_event['event'].event_id}",
                headers=admin_user["headers"],
            )
        ).json()
        assert body["purchases_email_failed"] == 1
        assert body["purchases_email_unknown"] == 0

    async def test_event_with_no_sales_reports_zeros(
        self, client: AsyncClient, db_session, admin_user
    ):
        event = Event(
            title="Empty Event",
            event_date=date(2026, 12, 1),
            event_time=time(12, 0),
            location="Nowhere",
            status="draft",
            created_by=admin_user["user"].user_id,
        )
        db_session.add(event)
        await db_session.flush()

        body = (
            await client.get(
                f"/api/v1/admin/events/{event.event_id}", headers=admin_user["headers"]
            )
        ).json()
        assert body["total_tickets_issued"] == 0
        assert float(body["total_revenue"]) == 0.0
        assert body["ticket_types"] == []


# ── Election detail ──────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def detailed_election(db_session: AsyncSession, admin_user) -> dict:
    """An election with 2 candidates, weighted votes, and mixed transactions."""
    now = datetime.now(timezone.utc)
    election = Election(
        title="Detail Test Election",
        description="An election with real votes",
        start_datetime=now - timedelta(days=1),
        end_datetime=now + timedelta(days=1),
        status="active",
        created_by=admin_user["user"].user_id,
        vote_price=200,
    )
    db_session.add(election)
    await db_session.flush()

    category = Category(
        election_id=election.election_id, name="Winner", election_type="single_choice"
    )
    db_session.add(category)
    await db_session.flush()

    alice = Candidate(
        category_id=category.category_id, election_id=election.election_id, name="Alice", display_order=0
    )
    bob = Candidate(
        category_id=category.category_id, election_id=election.election_id, name="Bob", display_order=1
    )
    db_session.add_all([alice, bob])
    await db_session.flush()

    crypto = CryptographyService()

    def make_vote(candidate_id, count, hash_seed):
        encrypted = crypto.encrypt_vote_data([str(candidate_id)])
        cast_at = now.isoformat()
        return Vote(
            category_id=category.category_id,
            election_id=election.election_id,
            voter_hash=hash_seed.ljust(64, "0"),
            vote_data=encrypted,
            vote_signature=crypto.sign_vote(encrypted, cast_at),
            count=count,
        )

    # Alice gets 5 (a weighted multi-vote) + 1; Bob gets 2. Totals: 8.
    db_session.add_all(
        [
            make_vote(alice.candidate_id, 5, "a1"),
            make_vote(alice.candidate_id, 1, "a2"),
            make_vote(bob.candidate_id, 2, "b1"),
        ]
    )

    db_session.add_all(
        [
            Transaction(
                reference="vote_ok_1",
                election_id=election.election_id,
                category_id=category.category_id,
                voter_hash="a1".ljust(64, "0"),
                email="voter1@example.com",
                candidate_ids=[str(alice.candidate_id)],
                amount=1000,
                status="success",
            ),
            Transaction(
                reference="vote_ok_2",
                election_id=election.election_id,
                category_id=category.category_id,
                voter_hash="b1".ljust(64, "0"),
                email="voter2@example.com",
                candidate_ids=[str(bob.candidate_id)],
                amount=400,
                status="success",
            ),
            Transaction(
                reference="vote_bad_1",
                election_id=election.election_id,
                category_id=category.category_id,
                voter_hash="c1".ljust(64, "0"),
                email="voter3@example.com",
                candidate_ids=[str(bob.candidate_id)],
                amount=200,
                status="failed",
            ),
        ]
    )
    await db_session.flush()

    return {"election": election, "alice": alice, "bob": bob}


@pytest.mark.asyncio
class TestElectionDetail:
    async def test_requires_admin(
        self, client: AsyncClient, test_user, detailed_election
    ):
        response = await client.get(
            f"/api/v1/admin/elections/{detailed_election['election'].election_id}",
            headers=test_user["headers"],
        )
        assert response.status_code == 403

    async def test_unknown_election_returns_404(self, client: AsyncClient, admin_user):
        response = await client.get(
            f"/api/v1/admin/elections/{uuid4()}", headers=admin_user["headers"]
        )
        assert response.status_code == 404

    async def test_candidate_breakdown_is_ranked_and_weighted(
        self, client: AsyncClient, admin_user, detailed_election
    ):
        response = await client.get(
            f"/api/v1/admin/elections/{detailed_election['election'].election_id}",
            headers=admin_user["headers"],
        )
        assert response.status_code == 200
        body = response.json()

        # Vote.count is a weight, not a row count — 5 + 1 for Alice, 2 for Bob.
        assert body["total_votes"] == 8
        assert body["total_candidates"] == 2

        candidates = body["candidates"]
        assert candidates[0]["name"] == "Alice"
        assert candidates[0]["vote_count"] == 6
        assert candidates[0]["rank"] == 1
        assert candidates[1]["name"] == "Bob"
        assert candidates[1]["vote_count"] == 2
        assert candidates[1]["rank"] == 2
        assert round(candidates[0]["percentage"]) == 75

    async def test_revenue_counts_only_successful_transactions(
        self, client: AsyncClient, admin_user, detailed_election
    ):
        body = (
            await client.get(
                f"/api/v1/admin/elections/{detailed_election['election'].election_id}",
                headers=admin_user["headers"],
            )
        ).json()
        # 1000 + 400 success; the 200 failed one is excluded.
        assert body["total_revenue_pesewas"] == 1400
        assert body["transactions_successful"] == 2
        assert body["transactions_failed"] == 1
        assert body["transactions_pending"] == 0

    async def test_transactions_omit_candidate_linkage(
        self, client: AsyncClient, admin_user, detailed_election
    ):
        """Pairing payer email with candidate choices would de-anonymise the
        ballot, so the response must not carry it."""
        body = (
            await client.get(
                f"/api/v1/admin/elections/{detailed_election['election'].election_id}",
                headers=admin_user["headers"],
            )
        ).json()

        assert len(body["transactions"]) == 3
        for txn in body["transactions"]:
            assert "candidate_ids" not in txn
            assert "voter_hash" not in txn

        by_ref = {t["reference"]: t for t in body["transactions"]}
        assert by_ref["vote_ok_1"]["email"] == "voter1@example.com"
        assert by_ref["vote_ok_1"]["amount_pesewas"] == 1000
        # 1000 pesewas at a 200 vote price.
        assert by_ref["vote_ok_1"]["vote_count"] == 5

    async def test_effective_vote_price_uses_election_override(
        self, client: AsyncClient, admin_user, detailed_election
    ):
        body = (
            await client.get(
                f"/api/v1/admin/elections/{detailed_election['election'].election_id}",
                headers=admin_user["headers"],
            )
        ).json()
        assert body["vote_price"] == 200
        assert body["effective_vote_price"] == 200

    async def test_election_with_no_votes_reports_zeros(
        self, client: AsyncClient, db_session, admin_user
    ):
        now = datetime.now(timezone.utc)
        election = Election(
            title="Empty Election",
            start_datetime=now,
            end_datetime=now + timedelta(days=1),
            status="draft",
            created_by=admin_user["user"].user_id,
        )
        db_session.add(election)
        await db_session.flush()

        body = (
            await client.get(
                f"/api/v1/admin/elections/{election.election_id}",
                headers=admin_user["headers"],
            )
        ).json()
        assert body["total_votes"] == 0
        assert body["total_revenue_pesewas"] == 0
        assert body["candidates"] == []
        assert body["transactions"] == []
        # Falls back to the global platform price when not overridden.
        assert body["vote_price"] is None
        assert body["effective_vote_price"] > 0
