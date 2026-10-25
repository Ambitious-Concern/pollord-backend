from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import create_access_token, hash_password
from app.models.election import Candidate, Category, Election
from app.models.event import Event, TicketType

VP = settings.VOTE_PRICE


async def _make_organizer(db_session: AsyncSession, email: str) -> dict:
    """A real organizer role (not System Administrator, which bypasses
    ownership checks entirely) so scope-ownership rejection is actually
    exercised rather than short-circuited by the admin bypass."""
    from sqlalchemy import select

    from app.models.user import Role, User, UserRole

    user = User(
        email=email, password_hash=hash_password("Passw0rd!"),
        full_name="Outsider Organizer", email_verified=True, account_status="active",
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

TICKET_INIT_PATH = "app.api.v1.endpoints.tickets.PaystackService.initialize_transaction"
TICKET_VERIFY_PATH = "app.api.v1.endpoints.tickets.PaystackService.verify_transaction"
VOTE_INIT_PATH = "app.api.v1.endpoints.voting.PaystackService.initialize_transaction"
VOTE_VERIFY_PATH = "app.api.v1.endpoints.voting.PaystackService.verify_transaction"


def _init_mock(amount: int) -> AsyncMock:
    return AsyncMock(return_value={
        "access_code": "acc_test",
        "reference": "ref_test",
        "authorization_url": "https://checkout.paystack.com/test",
    })


def _verify_mock(amount: int, status: str = "success") -> AsyncMock:
    return AsyncMock(return_value={"status": status, "amount": amount, "reference": "ref_test", "currency": "GHS"})


async def _make_event(db_session: AsyncSession, admin_user, *, price="100.00", quantity=50) -> dict:
    event = Event(
        title="Promo Test Event",
        event_date=date(2026, 12, 1),
        event_time=time(18, 0),
        location="Accra",
        status="published",
        created_by=admin_user["user"].user_id,
    )
    db_session.add(event)
    await db_session.flush()
    ticket_type = TicketType(
        event_id=event.event_id, type_name="Regular", price=Decimal(price), quantity_available=quantity,
    )
    db_session.add(ticket_type)
    await db_session.flush()
    return {"event": event, "ticket_type": ticket_type}


async def _make_election_with_category(db_session: AsyncSession, admin_user, *, allow_revoting=False) -> dict:
    now = datetime.now(timezone.utc)
    election = Election(
        title="Promo Test Election",
        start_datetime=now - timedelta(hours=1),
        end_datetime=now + timedelta(hours=23),
        status="active",
        visibility="public",
        require_verification=False,
        allow_revoting=allow_revoting,
        created_by=admin_user["user"].user_id,
    )
    db_session.add(election)
    await db_session.flush()
    category = Category(election_id=election.election_id, name="Winner", election_type="single_choice")
    db_session.add(category)
    await db_session.flush()
    candidate = Candidate(category_id=category.category_id, election_id=election.election_id, name="Alice", display_order=1)
    db_session.add(candidate)
    await db_session.flush()
    return {"election": election, "category": category, "candidate": candidate}


class TestPromoCodeCrud:
    async def test_create_promo_code_unscoped(self, client: AsyncClient, admin_user):
        res = await client.post(
            "/api/v1/promo-codes",
            json={
                "code": "welcome10",
                "discount_type": "percentage",
                "discount_value": 10,
                "valid_from": str(date.today()),
            },
            headers=admin_user["headers"],
        )
        assert res.status_code == 201, res.text
        body = res.json()
        assert body["code"] == "WELCOME10"
        assert body["status"] == "active"
        assert body["uses_count"] == 0
        assert body["scope"] == []

    async def test_duplicate_code_rejected(self, client: AsyncClient, admin_user):
        payload = {
            "code": "DUPE1",
            "discount_type": "fixed_amount",
            "discount_value": 5,
            "valid_from": str(date.today()),
        }
        first = await client.post("/api/v1/promo-codes", json=payload, headers=admin_user["headers"])
        assert first.status_code == 201
        second = await client.post("/api/v1/promo-codes", json=payload, headers=admin_user["headers"])
        assert second.status_code == 409

    async def test_scoped_promo_code_reports_scope_name(self, client: AsyncClient, db_session, admin_user):
        evt = await _make_event(db_session, admin_user)
        res = await client.post(
            "/api/v1/promo-codes",
            json={
                "code": "EVENTONLY",
                "discount_type": "percentage",
                "discount_value": 15,
                "valid_from": str(date.today()),
                "scope": [{"event_id": str(evt["event"].event_id)}],
            },
            headers=admin_user["headers"],
        )
        assert res.status_code == 201, res.text
        assert res.json()["scope"] == [{"election_id": None, "event_id": str(evt["event"].event_id), "name": "Promo Test Event"}]

    async def test_delete_unused_promo_code(self, client: AsyncClient, admin_user):
        create_res = await client.post(
            "/api/v1/promo-codes",
            json={"code": "DELETEME", "discount_type": "percentage", "discount_value": 10, "valid_from": str(date.today())},
            headers=admin_user["headers"],
        )
        promo_id = create_res.json()["promo_code_id"]
        del_res = await client.delete(f"/api/v1/promo-codes/{promo_id}", headers=admin_user["headers"])
        assert del_res.status_code == 204

    async def test_outsider_cannot_scope_to_others_event(self, client: AsyncClient, db_session, admin_user):
        evt = await _make_event(db_session, admin_user)
        outsider = await _make_organizer(db_session, "outsider-promo@example.com")
        res = await client.post(
            "/api/v1/promo-codes",
            json={
                "code": "STEAL10",
                "discount_type": "percentage",
                "discount_value": 10,
                "valid_from": str(date.today()),
                "scope": [{"event_id": str(evt["event"].event_id)}],
            },
            headers=outsider["headers"],
        )
        assert res.status_code == 403


class TestPromoCodeTicketRedemption:
    async def test_percentage_discount_applied_at_initiate(self, client: AsyncClient, db_session, admin_user):
        evt = await _make_event(db_session, admin_user, price="100.00")
        await client.post(
            "/api/v1/promo-codes",
            json={"code": "TIX20", "discount_type": "percentage", "discount_value": 20, "valid_from": str(date.today())},
            headers=admin_user["headers"],
        )

        with patch(TICKET_INIT_PATH, new=_init_mock(80_00)):
            res = await client.post(
                "/api/v1/tickets/initiate-payment",
                json={
                    "event_id": str(evt["event"].event_id),
                    "items": [{"ticket_type_id": str(evt["ticket_type"].ticket_type_id), "quantity": 1}],
                    "promo_code": "tix20",
                },
                headers=admin_user["headers"],
            )
        assert res.status_code == 201, res.text
        assert res.json()["amount"] == 80.0

    async def test_invalid_code_rejected(self, client: AsyncClient, db_session, admin_user):
        evt = await _make_event(db_session, admin_user)
        res = await client.post(
            "/api/v1/tickets/initiate-payment",
            json={
                "event_id": str(evt["event"].event_id),
                "items": [{"ticket_type_id": str(evt["ticket_type"].ticket_type_id), "quantity": 1}],
                "promo_code": "NOPE",
            },
            headers=admin_user["headers"],
        )
        assert res.status_code == 404

    async def test_out_of_scope_code_rejected(self, client: AsyncClient, db_session, admin_user):
        evt = await _make_event(db_session, admin_user)
        other_evt = await _make_event(db_session, admin_user)
        await client.post(
            "/api/v1/promo-codes",
            json={
                "code": "OTHEREVENT",
                "discount_type": "percentage",
                "discount_value": 10,
                "valid_from": str(date.today()),
                "scope": [{"event_id": str(other_evt["event"].event_id)}],
            },
            headers=admin_user["headers"],
        )
        res = await client.post(
            "/api/v1/tickets/initiate-payment",
            json={
                "event_id": str(evt["event"].event_id),
                "items": [{"ticket_type_id": str(evt["ticket_type"].ticket_type_id), "quantity": 1}],
                "promo_code": "OTHEREVENT",
            },
            headers=admin_user["headers"],
        )
        assert res.status_code == 400

    async def test_usage_limit_enforced_after_successful_redemption(self, client: AsyncClient, db_session, admin_user, test_user):
        evt = await _make_event(db_session, admin_user, price="50.00")
        await client.post(
            "/api/v1/promo-codes",
            json={
                "code": "ONLYONE",
                "discount_type": "fixed_amount",
                "discount_value": 10,
                "valid_from": str(date.today()),
                "usage_limit": 1,
                "per_person_limit": 5,
            },
            headers=admin_user["headers"],
        )

        with patch(TICKET_INIT_PATH, new=_init_mock(40_00)):
            init_res = await client.post(
                "/api/v1/tickets/initiate-payment",
                json={
                    "event_id": str(evt["event"].event_id),
                    "items": [{"ticket_type_id": str(evt["ticket_type"].ticket_type_id), "quantity": 1}],
                    "promo_code": "ONLYONE",
                },
                headers=test_user["headers"],
            )
        assert init_res.status_code == 201, init_res.text
        ref = init_res.json()["reference"]

        with patch(TICKET_VERIFY_PATH, new=_verify_mock(40_00)):
            verify_res = await client.post(
                "/api/v1/tickets/verify-and-purchase", json={"reference": ref}, headers=test_user["headers"],
            )
        assert verify_res.status_code == 201, verify_res.text

        second_res = await client.post(
            "/api/v1/tickets/initiate-payment",
            json={
                "event_id": str(evt["event"].event_id),
                "items": [{"ticket_type_id": str(evt["ticket_type"].ticket_type_id), "quantity": 1}],
                "promo_code": "ONLYONE",
            },
            headers=admin_user["headers"],
        )
        assert second_res.status_code == 400

    async def test_redemption_recorded_and_visible_in_uses_count(self, client: AsyncClient, db_session, admin_user, test_user):
        evt = await _make_event(db_session, admin_user, price="50.00")
        create_res = await client.post(
            "/api/v1/promo-codes",
            json={"code": "COUNTME", "discount_type": "percentage", "discount_value": 10, "valid_from": str(date.today())},
            headers=admin_user["headers"],
        )
        promo_id = create_res.json()["promo_code_id"]

        with patch(TICKET_INIT_PATH, new=_init_mock(45_00)):
            init_res = await client.post(
                "/api/v1/tickets/initiate-payment",
                json={
                    "event_id": str(evt["event"].event_id),
                    "items": [{"ticket_type_id": str(evt["ticket_type"].ticket_type_id), "quantity": 1}],
                    "promo_code": "COUNTME",
                },
                headers=test_user["headers"],
            )
        ref = init_res.json()["reference"]
        with patch(TICKET_VERIFY_PATH, new=_verify_mock(45_00)):
            await client.post("/api/v1/tickets/verify-and-purchase", json={"reference": ref}, headers=test_user["headers"])

        # Deleting a used code must now be blocked.
        del_res = await client.delete(f"/api/v1/promo-codes/{promo_id}", headers=admin_user["headers"])
        assert del_res.status_code == 409

        list_res = await client.get("/api/v1/promo-codes", headers=admin_user["headers"])
        match = next(p for p in list_res.json() if p["promo_code_id"] == promo_id)
        assert match["uses_count"] == 1


class TestPromoCodeVoteRedemption:
    async def test_single_vote_discount_and_redemption(self, client: AsyncClient, db_session, admin_user):
        election_data = await _make_election_with_category(db_session, admin_user)
        category = election_data["category"]
        candidate = election_data["candidate"]

        await client.post(
            "/api/v1/promo-codes",
            json={"code": "VOTE50", "discount_type": "percentage", "discount_value": 50, "valid_from": str(date.today())},
            headers=admin_user["headers"],
        )
        expected_price = int(Decimal(VP) * Decimal("0.5"))

        with patch(VOTE_INIT_PATH, new=_init_mock(expected_price)):
            init_res = await client.post(
                "/api/v1/voting/public/initiate-payment",
                json={
                    "category_id": str(category.category_id),
                    "candidate_ids": [str(candidate.candidate_id)],
                    "email": "votepromo@example.com",
                    "promo_code": "vote50",
                },
            )
        assert init_res.status_code == 201, init_res.text
        assert init_res.json()["amount"] == expected_price
        assert init_res.json()["vote_count"] == 1
        ref = init_res.json()["reference"]

        with patch(VOTE_VERIFY_PATH, new=_verify_mock(expected_price)):
            verify_res = await client.post("/api/v1/voting/public/verify-and-cast", json={"reference": ref})
        assert verify_res.status_code == 201, verify_res.text

    async def test_per_person_limit_enforced(self, client: AsyncClient, db_session, admin_user):
        election_data = await _make_election_with_category(db_session, admin_user, allow_revoting=True)
        category = election_data["category"]
        candidate = election_data["candidate"]

        await client.post(
            "/api/v1/promo-codes",
            json={
                "code": "ONCEEACH",
                "discount_type": "percentage",
                "discount_value": 10,
                "valid_from": str(date.today()),
                "per_person_limit": 1,
            },
            headers=admin_user["headers"],
        )
        expected_price = int(Decimal(VP) * Decimal("0.9"))

        with patch(VOTE_INIT_PATH, new=_init_mock(expected_price)):
            init_res = await client.post(
                "/api/v1/voting/public/initiate-payment",
                json={
                    "category_id": str(category.category_id),
                    "candidate_ids": [str(candidate.candidate_id)],
                    "email": "repeatvoter@example.com",
                    "promo_code": "ONCEEACH",
                },
            )
        assert init_res.status_code == 201, init_res.text
        ref = init_res.json()["reference"]
        with patch(VOTE_VERIFY_PATH, new=_verify_mock(expected_price)):
            await client.post("/api/v1/voting/public/verify-and-cast", json={"reference": ref})

        second_res = await client.post(
            "/api/v1/voting/public/initiate-payment",
            json={
                "category_id": str(category.category_id),
                "candidate_ids": [str(candidate.candidate_id)],
                "email": "repeatvoter@example.com",
                "promo_code": "ONCEEACH",
            },
        )
        assert second_res.status_code == 400
