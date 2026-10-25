import hashlib
import uuid as _uuid
from datetime import date, datetime, time, timedelta, timezone

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_ticket_scan_token
from app.models.election import Candidate, Category, Election, EligibleVoter
from app.models.event import Event, TicketType
from app.models.ticket import Ticket, TicketPurchase


async def _make_gated_election(db_session: AsyncSession, admin_user) -> tuple[Election, Category, Candidate]:
    now = datetime.now(timezone.utc)
    election = Election(
        title="Gated Voters Election",
        start_datetime=now - timedelta(hours=1),
        end_datetime=now + timedelta(hours=1),
        status="active",
        visibility="private",
        require_verification=True,
        vote_price=0,
        created_by=admin_user["user"].user_id,
    )
    db_session.add(election)
    await db_session.flush()
    category = Category(
        election_id=election.election_id, name="President",
        election_type="single_choice", display_order=0,
    )
    db_session.add(category)
    await db_session.flush()
    candidate = Candidate(
        category_id=category.category_id, election_id=election.election_id,
        name="Anne", short_code="ANNE", display_order=0,
    )
    db_session.add(candidate)
    await db_session.flush()
    return election, category, candidate


class TestNomineesVoteCounts:
    async def test_owner_sees_vote_counts_and_rank(self, client: AsyncClient, db_session, admin_user, test_user):
        election, category, candidate = await _make_gated_election(db_session, admin_user)
        db_session.add(
            EligibleVoter(election_id=election.election_id, user_id=test_user["user"].user_id)
        )
        await db_session.flush()

        vote_res = await client.post(
            "/api/v1/voting/cast",
            json={"category_id": str(category.category_id), "candidate_ids": [str(candidate.candidate_id)]},
            headers=test_user["headers"],
        )
        assert vote_res.status_code == 201, vote_res.text

        res = await client.get(
            f"/api/v1/elections/{election.election_id}/candidates", headers=admin_user["headers"]
        )
        assert res.status_code == 200
        row = next(c for c in res.json() if c["candidate_id"] == str(candidate.candidate_id))
        assert row["vote_count"] == 1
        assert row["category_rank"] == 1

    async def test_outsider_cannot_list_candidates(self, client: AsyncClient, db_session, admin_user, test_user):
        election, _category, _candidate = await _make_gated_election(db_session, admin_user)
        res = await client.get(
            f"/api/v1/elections/{election.election_id}/candidates", headers=test_user["headers"]
        )
        assert res.status_code == 403


class TestVotersShapeFix:
    async def test_eligible_voter_has_name_email_and_voted_flag(
        self, client: AsyncClient, db_session, admin_user, test_user
    ):
        election, category, candidate = await _make_gated_election(db_session, admin_user)
        add_res = await client.post(
            f"/api/v1/elections/{election.election_id}/eligible-voters",
            json={"user_ids": [str(test_user["user"].user_id)]},
            headers=admin_user["headers"],
        )
        assert add_res.status_code == 201, add_res.text

        before = await client.get(
            f"/api/v1/elections/{election.election_id}/eligible-voters", headers=admin_user["headers"]
        )
        assert before.status_code == 200
        row = before.json()[0]
        assert row["name"] == test_user["user"].full_name
        assert row["email"] == test_user["user"].email
        assert row["has_voted"] is False

        vote_res = await client.post(
            "/api/v1/voting/cast",
            json={"category_id": str(category.category_id), "candidate_ids": [str(candidate.candidate_id)]},
            headers=test_user["headers"],
        )
        assert vote_res.status_code == 201, vote_res.text

        after = await client.get(
            f"/api/v1/elections/{election.election_id}/eligible-voters", headers=admin_user["headers"]
        )
        assert after.json()[0]["has_voted"] is True


class TestAttendeesCheckInChannel:
    async def _make_ticket(self, db_session: AsyncSession, admin_user, buyer) -> tuple[Event, Ticket]:
        event = Event(
            title="Scan Channel Event",
            event_date=date(2026, 12, 1),
            event_time=time(19, 0),
            location="Accra",
            status="published",
            created_by=admin_user["user"].user_id,
            scan_enabled=True,
        )
        db_session.add(event)
        await db_session.flush()
        ticket_type = TicketType(
            event_id=event.event_id, type_name="Regular", price=0,
            quantity_available=100,
        )
        db_session.add(ticket_type)
        await db_session.flush()
        purchase = TicketPurchase(
            user_id=buyer.user_id, event_id=event.event_id,
            total_amount=0, payment_status="completed",
        )
        db_session.add(purchase)
        await db_session.flush()
        code = f"TCK-{hashlib.sha256(str(_uuid.uuid4()).encode()).hexdigest()[:8].upper()}"
        ticket = Ticket(
            ticket_code=code, event_id=event.event_id, ticket_type_id=ticket_type.ticket_type_id,
            user_id=buyer.user_id, purchase_id=purchase.purchase_id,
            qr_code_data=code, ticket_status="valid",
        )
        db_session.add(ticket)
        await db_session.flush()
        return event, ticket

    async def test_public_scan_sets_channel_scan(self, client: AsyncClient, db_session, admin_user, test_user):
        event, ticket = await self._make_ticket(db_session, admin_user, test_user["user"])
        scan_token = create_ticket_scan_token(
            str(event.event_id), datetime.now(timezone.utc) + timedelta(hours=1)
        )
        res = await client.post(
            "/api/v1/tickets/public/validate",
            json={"scan_token": scan_token, "ticket_code": ticket.ticket_code},
        )
        assert res.status_code == 200, res.text
        assert res.json()["valid"] is True
        assert res.json()["ticket"]["check_in_channel"] == "scan"

    async def test_organizer_manual_validate_sets_channel_manual_code(
        self, client: AsyncClient, db_session, admin_user, test_user
    ):
        event, ticket = await self._make_ticket(db_session, admin_user, test_user["user"])
        res = await client.post(
            "/api/v1/tickets/validate",
            json={"ticket_code": ticket.ticket_code},
            headers=admin_user["headers"],
        )
        assert res.status_code == 200, res.text
        assert res.json()["ticket"]["check_in_channel"] == "manual_code"

        sales = await client.get(
            "/api/v1/tickets/sales", params={"event_id": str(event.event_id)}, headers=admin_user["headers"]
        )
        assert sales.status_code == 200
        sold = next(t for t in sales.json() if t["ticket_id"] == str(ticket.ticket_id))
        assert sold["check_in_channel"] == "manual_code"
