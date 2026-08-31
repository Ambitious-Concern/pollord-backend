import uuid
from datetime import date, datetime, time, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.election import Election
from app.models.event import Event
from app.models.ticket import TicketPurchase
from app.models.transaction import Transaction

RESOLVE_PATH = "app.api.v1.endpoints.payouts.PaystackService.resolve_account"
CREATE_RECIPIENT_PATH = "app.api.v1.endpoints.payouts.PaystackService.create_transfer_recipient"
TRANSFER_PATH = "app.api.v1.endpoints.payouts.PaystackService.initiate_transfer"
LIST_BANKS_PATH = "app.api.v1.endpoints.payouts.PaystackService.list_banks"

VALID_DESTINATION = {
    "payout_method": "mobile_money",
    "recipient_name": "Jane Organizer",
    "mobile_network": "MTN",
    "mobile_number": "0244000000",
}


@pytest.fixture
async def event_with_revenue(db_session: AsyncSession, admin_user):
    event = Event(
        title="Payout Test Event",
        event_date=date(2026, 8, 20),
        event_time=time(18, 0),
        location="Test Venue",
        status="published",
        created_by=admin_user["user"].user_id,
    )
    db_session.add(event)
    await db_session.flush()

    purchase = TicketPurchase(
        event_id=event.event_id,
        user_id=admin_user["user"].user_id,
        total_amount=500,
        payment_status="completed",
    )
    db_session.add(purchase)
    await db_session.flush()

    return event


@pytest.fixture
async def election_with_revenue(db_session: AsyncSession, admin_user):
    election = Election(
        title="Payout Test Election",
        start_datetime=datetime.now(timezone.utc) - timedelta(hours=1),
        end_datetime=datetime.now(timezone.utc) + timedelta(hours=1),
        status="active",
        created_by=admin_user["user"].user_id,
    )
    db_session.add(election)
    await db_session.flush()

    txn = Transaction(
        reference=f"vote_test_{uuid.uuid4().hex}",
        election_id=election.election_id,
        category_id=uuid.uuid4(),
        voter_hash="testhash",
        candidate_ids=[str(uuid.uuid4())],
        amount=50000,  # 500.00 cedis, in pesewas
        currency="GHS",
        status="success",
    )
    db_session.add(txn)
    await db_session.flush()

    return election


async def _request_payout(client: AsyncClient, event_id, headers, **overrides):
    payload = {**VALID_DESTINATION, **overrides}
    return await client.post(f"/api/v1/payouts/events/{event_id}", json=payload, headers=headers)


async def _request_election_payout(client: AsyncClient, election_id, headers, **overrides):
    payload = {**VALID_DESTINATION, **overrides}
    return await client.post(
        f"/api/v1/payouts/elections/{election_id}", json=payload, headers=headers
    )


@pytest.mark.asyncio
class TestRequestPayout:
    async def test_requires_payout_destination(
        self, client: AsyncClient, admin_user, event_with_revenue
    ):
        response = await client.post(
            f"/api/v1/payouts/events/{event_with_revenue.event_id}",
            json={},
            headers=admin_user["headers"],
        )
        assert response.status_code == 422

    async def test_succeeds_with_valid_destination(
        self, client: AsyncClient, admin_user, event_with_revenue
    ):
        response = await _request_payout(client, event_with_revenue.event_id, admin_user["headers"])
        assert response.status_code == 201
        data = response.json()
        assert data["status"] == "pending"
        assert data["mobile_network"] == "MTN"
        assert data["mobile_number"] == "0244000000"
        assert data["amount"] == "500.00"

    async def test_rejects_blank_recipient_name(
        self, client: AsyncClient, admin_user, event_with_revenue
    ):
        response = await _request_payout(
            client, event_with_revenue.event_id, admin_user["headers"], recipient_name="   ",
        )
        assert response.status_code == 422


@pytest.mark.asyncio
class TestMobileMoneyNetworks:
    async def test_lists_networks_from_paystack(self, client: AsyncClient, admin_user):
        mock = AsyncMock(return_value=[
            {"name": "MTN Mobile Money", "code": "MTN"},
            {"name": "Vodafone Cash", "code": "VOD"},
        ])
        with patch(LIST_BANKS_PATH, new=mock):
            response = await client.get(
                "/api/v1/payouts/mobile-money-networks", headers=admin_user["headers"],
            )
        assert response.status_code == 200
        data = response.json()
        assert data == [
            {"name": "MTN Mobile Money", "code": "MTN"},
            {"name": "Vodafone Cash", "code": "VOD"},
        ]


@pytest.mark.asyncio
class TestPayViaPaystack:
    async def test_successful_transfer_marks_paid(
        self, client: AsyncClient, admin_user, event_with_revenue
    ):
        created = await _request_payout(client, event_with_revenue.event_id, admin_user["headers"])
        payout_id = created.json()["payout_request_id"]

        resolve_mock = AsyncMock(return_value={"account_name": "Jane Organizer"})
        recipient_mock = AsyncMock(return_value={"recipient_code": "RCP_test123"})
        transfer_mock = AsyncMock(return_value={"status": "success", "reference": "payout_abc"})

        with patch(RESOLVE_PATH, new=resolve_mock), \
             patch(CREATE_RECIPIENT_PATH, new=recipient_mock), \
             patch(TRANSFER_PATH, new=transfer_mock):
            response = await client.post(
                f"/api/v1/payouts/admin/{payout_id}/pay", headers=admin_user["headers"],
            )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "paid"
        assert data["transfer_status"] == "success"

    async def test_failed_transfer_leaves_request_pending(
        self, client: AsyncClient, admin_user, event_with_revenue
    ):
        created = await _request_payout(client, event_with_revenue.event_id, admin_user["headers"])
        payout_id = created.json()["payout_request_id"]

        resolve_mock = AsyncMock(return_value={"account_name": "Jane Organizer"})
        recipient_mock = AsyncMock(return_value={"recipient_code": "RCP_test123"})
        transfer_mock = AsyncMock(return_value={"status": "failed", "reference": "payout_abc"})

        with patch(RESOLVE_PATH, new=resolve_mock), \
             patch(CREATE_RECIPIENT_PATH, new=recipient_mock), \
             patch(TRANSFER_PATH, new=transfer_mock):
            response = await client.post(
                f"/api/v1/payouts/admin/{payout_id}/pay", headers=admin_user["headers"],
            )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "pending"
        assert data["transfer_status"] == "failed"

    async def test_bad_account_number_never_reaches_transfer(
        self, client: AsyncClient, admin_user, event_with_revenue
    ):
        """resolve_account failing must abort before any money-moving call."""
        created = await _request_payout(client, event_with_revenue.event_id, admin_user["headers"])
        payout_id = created.json()["payout_request_id"]

        resolve_mock = AsyncMock(side_effect=Exception("should not be reached if this fails cleanly"))
        transfer_mock = AsyncMock(return_value={"status": "success", "reference": "payout_abc"})

        async def failing_resolve(*args, **kwargs):
            from fastapi import HTTPException
            raise HTTPException(status_code=400, detail="Could not verify that account")

        with patch(RESOLVE_PATH, new=failing_resolve), \
             patch(TRANSFER_PATH, new=transfer_mock):
            response = await client.post(
                f"/api/v1/payouts/admin/{payout_id}/pay", headers=admin_user["headers"],
            )

        assert response.status_code == 400
        transfer_mock.assert_not_called()

    async def test_cannot_pay_an_already_paid_request(
        self, client: AsyncClient, admin_user, event_with_revenue
    ):
        created = await _request_payout(client, event_with_revenue.event_id, admin_user["headers"])
        payout_id = created.json()["payout_request_id"]

        resolve_mock = AsyncMock(return_value={"account_name": "Jane Organizer"})
        recipient_mock = AsyncMock(return_value={"recipient_code": "RCP_test123"})
        transfer_mock = AsyncMock(return_value={"status": "success", "reference": "payout_abc"})
        with patch(RESOLVE_PATH, new=resolve_mock), \
             patch(CREATE_RECIPIENT_PATH, new=recipient_mock), \
             patch(TRANSFER_PATH, new=transfer_mock):
            await client.post(f"/api/v1/payouts/admin/{payout_id}/pay", headers=admin_user["headers"])

            second = await client.post(
                f"/api/v1/payouts/admin/{payout_id}/pay", headers=admin_user["headers"],
            )
        assert second.status_code == 409

    async def test_organizer_cannot_pay(self, client: AsyncClient, test_user, admin_user, event_with_revenue):
        created = await _request_payout(client, event_with_revenue.event_id, admin_user["headers"])
        payout_id = created.json()["payout_request_id"]

        response = await client.post(
            f"/api/v1/payouts/admin/{payout_id}/pay", headers=test_user["headers"],
        )
        assert response.status_code == 403


@pytest.mark.asyncio
class TestElectionPayouts:
    """Elections mirror the event payout flow, sourced from paid-vote
    Transaction rows instead of TicketPurchase."""

    async def test_available_reflects_paid_vote_revenue(
        self, client: AsyncClient, admin_user, election_with_revenue
    ):
        response = await client.get(
            f"/api/v1/payouts/elections/{election_with_revenue.election_id}/available",
            headers=admin_user["headers"],
        )
        assert response.status_code == 200
        data = response.json()
        assert data["gross_revenue"] == "500.00"
        assert data["available"] == "500.00"
        assert data["election_id"] == str(election_with_revenue.election_id)
        assert data["event_id"] is None

    async def test_succeeds_with_valid_destination(
        self, client: AsyncClient, admin_user, election_with_revenue
    ):
        response = await _request_election_payout(
            client, election_with_revenue.election_id, admin_user["headers"]
        )
        assert response.status_code == 201
        data = response.json()
        assert data["status"] == "pending"
        assert data["amount"] == "500.00"
        assert data["election_id"] == str(election_with_revenue.election_id)
        assert data["election_title"] == "Payout Test Election"
        assert data["event_id"] is None

    async def test_duplicate_pending_request_rejected(
        self, client: AsyncClient, admin_user, election_with_revenue
    ):
        first = await _request_election_payout(
            client, election_with_revenue.election_id, admin_user["headers"]
        )
        assert first.status_code == 201
        second = await _request_election_payout(
            client, election_with_revenue.election_id, admin_user["headers"]
        )
        assert second.status_code == 409

    async def test_no_revenue_rejected(self, client: AsyncClient, admin_user, db_session: AsyncSession):
        election = Election(
            title="Empty Election",
            start_datetime=datetime.now(timezone.utc) - timedelta(hours=1),
            end_datetime=datetime.now(timezone.utc) + timedelta(hours=1),
            status="active",
            created_by=admin_user["user"].user_id,
        )
        db_session.add(election)
        await db_session.flush()

        response = await _request_election_payout(client, election.election_id, admin_user["headers"])
        assert response.status_code == 400

    async def test_non_owner_forbidden(
        self, client: AsyncClient, test_user, election_with_revenue
    ):
        response = await client.get(
            f"/api/v1/payouts/elections/{election_with_revenue.election_id}/available",
            headers=test_user["headers"],
        )
        assert response.status_code == 403

    async def test_list_for_election(
        self, client: AsyncClient, admin_user, election_with_revenue
    ):
        await _request_election_payout(client, election_with_revenue.election_id, admin_user["headers"])
        response = await client.get(
            f"/api/v1/payouts/elections/{election_with_revenue.election_id}",
            headers=admin_user["headers"],
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["election_id"] == str(election_with_revenue.election_id)
