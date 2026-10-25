from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from uuid import UUID

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.election import Category, Election
from app.models.event import Event
from app.models.transaction import Transaction
from app.models.ticket_transaction import TicketTransaction


async def _make_organizer(db_session: AsyncSession, email: str) -> dict:
    from sqlalchemy import select

    from app.models.user import Role, User, UserRole

    user = User(
        email=email, password_hash=hash_password("Passw0rd!"),
        full_name="Organizer", email_verified=True, account_status="active",
    )
    db_session.add(user)
    await db_session.flush()

    result = await db_session.execute(select(Role).where(Role.role_name == "Event Organizer"))
    role = result.scalar_one_or_none()
    if not role:
        role = Role(role_name="Event Organizer", permissions={})
        db_session.add(role)
        await db_session.flush()
    db_session.add(UserRole(user_id=user.user_id, role_id=role.role_id))
    await db_session.flush()

    token = create_access_token(str(user.user_id))
    return {"user": user, "headers": {"Authorization": f"Bearer {token}"}}


async def _make_election(db_session: AsyncSession, admin_user, *, plan="standard") -> dict:
    now = datetime.now(timezone.utc)
    election = Election(
        title="Finance Test Election",
        start_datetime=now - timedelta(days=1),
        end_datetime=now + timedelta(days=1),
        status="active",
        created_by=admin_user["user"].user_id,
        plan=plan,
    )
    db_session.add(election)
    await db_session.flush()
    category = Category(election_id=election.election_id, name="Overall Queen", election_type="single_choice")
    db_session.add(category)
    await db_session.flush()
    return {"election": election, "category": category}


async def _make_event(db_session: AsyncSession, admin_user) -> Event:
    event = Event(
        title="Finance Test Event",
        event_date=date(2026, 12, 1),
        event_time=time(18, 0),
        location="Accra",
        status="published",
        created_by=admin_user["user"].user_id,
    )
    db_session.add(event)
    await db_session.flush()
    return event


def _vote_txn(election, category, *, amount=10000, status="success", days_ago=0, paystack_response=None, vote_count=10):
    return Transaction(
        reference=f"vote_{election.election_id}_{days_ago}_{status}",
        election_id=election.election_id,
        category_id=category.category_id,
        voter_hash="hash",
        email="voter@example.com",
        candidate_ids=[],
        amount=amount,
        status=status,
        vote_count=vote_count,
        paystack_response=paystack_response,
        created_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
    )


def _ticket_txn(event, *, amount=Decimal("150.00"), status="success", days_ago=0, paystack_response=None):
    return TicketTransaction(
        reference=f"ticket_{event.event_id}_{days_ago}_{status}",
        guest_email="buyer@example.com",
        event_id=event.event_id,
        items=[{"ticket_type_id": "00000000-0000-0000-0000-000000000000", "quantity": 1}],
        amount=amount,
        status=status,
        paystack_response=paystack_response,
        created_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
    )


class TestFinancialStatsScope:
    async def test_vote_and_ticket_revenue_summed(self, client: AsyncClient, db_session, admin_user):
        election_data = await _make_election(db_session, admin_user, plan="standard")
        event = await _make_event(db_session, admin_user)
        db_session.add(_vote_txn(election_data["election"], election_data["category"], amount=10000))
        db_session.add(_ticket_txn(event, amount=Decimal("150.00")))
        await db_session.flush()

        res = await client.get("/api/v1/analytics/financial?range=all-time", headers=admin_user["headers"])
        assert res.status_code == 200, res.text
        body = res.json()
        # 100.00 GHS (vote) + 150.00 GHS (ticket) = 250.00 gross
        assert Decimal(body["summary"]["gross_ghs"]) == Decimal("250.00")
        assert body["summary"]["count"] == 2

    async def test_commission_rate_matches_plan(self, client: AsyncClient, db_session, admin_user):
        election_data = await _make_election(db_session, admin_user, plan="premium")
        db_session.add(_vote_txn(election_data["election"], election_data["category"], amount=10000))
        await db_session.flush()

        res = await client.get("/api/v1/analytics/financial?range=all-time", headers=admin_user["headers"])
        assert res.status_code == 200, res.text
        # Flat 10% commission on 100.00 GHS = 10.00, same as Standard.
        assert Decimal(res.json()["summary"]["platform_fee_ghs"]) == Decimal("10.00")

    async def test_outsiders_transactions_not_visible(self, client: AsyncClient, db_session, admin_user):
        election_data = await _make_election(db_session, admin_user)
        db_session.add(_vote_txn(election_data["election"], election_data["category"], amount=10000))
        await db_session.flush()

        outsider = await _make_organizer(db_session, "outsider-financial@example.com")
        res = await client.get("/api/v1/analytics/financial?range=all-time", headers=outsider["headers"])
        assert res.status_code == 200, res.text
        assert res.json()["summary"]["count"] == 0

    async def test_only_successful_status_counted_in_summary(self, client: AsyncClient, db_session, admin_user):
        election_data = await _make_election(db_session, admin_user)
        db_session.add(_vote_txn(election_data["election"], election_data["category"], amount=10000, status="success"))
        db_session.add(_vote_txn(election_data["election"], election_data["category"], amount=10000, status="failed"))
        db_session.add(_vote_txn(election_data["election"], election_data["category"], amount=10000, status="pending"))
        await db_session.flush()

        res = await client.get("/api/v1/analytics/financial?range=all-time", headers=admin_user["headers"])
        body = res.json()
        assert body["summary"]["count"] == 1
        # All three still show up in the detail table for visibility.
        assert len(body["transactions"]) == 3

    async def test_date_range_filters_old_transactions(self, client: AsyncClient, db_session, admin_user):
        election_data = await _make_election(db_session, admin_user)
        db_session.add(_vote_txn(election_data["election"], election_data["category"], amount=10000, days_ago=1))
        db_session.add(_vote_txn(election_data["election"], election_data["category"], amount=10000, days_ago=200))
        await db_session.flush()

        res = await client.get("/api/v1/analytics/financial?range=last-30", headers=admin_user["headers"])
        assert res.status_code == 200, res.text
        assert res.json()["summary"]["count"] == 1

        res_all = await client.get("/api/v1/analytics/financial?range=all-time", headers=admin_user["headers"])
        assert res_all.json()["summary"]["count"] == 2

    async def test_source_id_filter(self, client: AsyncClient, db_session, admin_user):
        election_data = await _make_election(db_session, admin_user)
        event = await _make_event(db_session, admin_user)
        db_session.add(_vote_txn(election_data["election"], election_data["category"], amount=10000))
        db_session.add(_ticket_txn(event, amount=Decimal("150.00")))
        await db_session.flush()

        res = await client.get(
            f"/api/v1/analytics/financial?range=all-time&source_id={election_data['election'].election_id}",
            headers=admin_user["headers"],
        )
        body = res.json()
        assert body["summary"]["count"] == 1
        assert Decimal(body["summary"]["gross_ghs"]) == Decimal("100.00")

    async def test_source_id_election_returns_top_categories(self, client: AsyncClient, db_session, admin_user):
        election_data = await _make_election(db_session, admin_user)
        election = election_data["election"]
        category_a = election_data["category"]
        category_b = Category(election_id=election.election_id, name="Overall King", election_type="single_choice")
        db_session.add(category_b)
        await db_session.flush()
        db_session.add(_vote_txn(election, category_a, amount=10000, days_ago=0))
        db_session.add(_vote_txn(election, category_b, amount=30000, days_ago=1))
        await db_session.flush()

        res = await client.get(
            f"/api/v1/analytics/financial?range=all-time&source_id={election.election_id}",
            headers=admin_user["headers"],
        )
        body = res.json()
        assert body["scoped_breakdown_kind"] == "categories"
        labels = [row["label"] for row in body["scoped_breakdown"]]
        assert labels[0] == "Overall King"
        assert labels[1] == "Overall Queen"
        assert Decimal(body["scoped_breakdown"][0]["value"]) == Decimal("300.00")

    async def test_source_id_event_returns_top_ticket_types_by_quantity(
        self, client: AsyncClient, db_session, admin_user
    ):
        event = await _make_event(db_session, admin_user)
        type_a = "11111111-1111-1111-1111-111111111111"
        type_b = "22222222-2222-2222-2222-222222222222"
        db_session.add(
            TicketTransaction(
                reference="ticket_mix_1",
                guest_email="buyer@example.com",
                event_id=event.event_id,
                items=[{"ticket_type_id": type_a, "quantity": 2}],
                amount=Decimal("100.00"),
                status="success",
            )
        )
        db_session.add(
            TicketTransaction(
                reference="ticket_mix_2",
                guest_email="buyer2@example.com",
                event_id=event.event_id,
                items=[{"ticket_type_id": type_b, "quantity": 5}],
                amount=Decimal("250.00"),
                status="success",
            )
        )
        await db_session.flush()
        from app.models.event import TicketType

        db_session.add_all(
            [
                TicketType(ticket_type_id=UUID(type_a), event_id=event.event_id, type_name="Regular", price=Decimal("50.00"), quantity_available=100),
                TicketType(ticket_type_id=UUID(type_b), event_id=event.event_id, type_name="VIP", price=Decimal("50.00"), quantity_available=100),
            ]
        )
        await db_session.flush()

        res = await client.get(
            f"/api/v1/analytics/financial?range=all-time&source_id={event.event_id}",
            headers=admin_user["headers"],
        )
        body = res.json()
        assert body["scoped_breakdown_kind"] == "ticket_types"
        assert body["scoped_breakdown"][0]["label"] == "VIP"
        assert Decimal(body["scoped_breakdown"][0]["value"]) == Decimal("5")
        assert body["scoped_breakdown"][1]["label"] == "Regular"

    async def test_channel_reflects_paystack_response(self, client: AsyncClient, db_session, admin_user):
        election_data = await _make_election(db_session, admin_user)
        db_session.add(
            _vote_txn(
                election_data["election"], election_data["category"], amount=10000,
                paystack_response={"channel": "mobile_money", "fees": 195},
            )
        )
        await db_session.flush()

        res = await client.get("/api/v1/analytics/financial?range=all-time", headers=admin_user["headers"])
        body = res.json()
        assert body["by_channel"] == [{"channel": "Mobile Money", "amount_ghs": "100.00"}]
        # Real Paystack fee (1.95 GHS) used instead of the estimate.
        assert Decimal(body["summary"]["processing_fee_ghs"]) == Decimal("1.95")
