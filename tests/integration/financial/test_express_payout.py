from datetime import datetime, timedelta, timezone

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.election import Election
from app.models.transaction import Transaction


async def _make_paid_election(db_session: AsyncSession, admin_user, plan: str) -> Election:
    now = datetime.now(timezone.utc)
    election = Election(
        title="Express Payout Test Election",
        start_datetime=now - timedelta(hours=2),
        end_datetime=now - timedelta(hours=1),  # already ended, doesn't matter here
        status="active",
        visibility="public",
        require_verification=False,
        vote_price=100,
        plan=plan,
        created_by=admin_user["user"].user_id,
    )
    db_session.add(election)
    await db_session.flush()

    txn = Transaction(
        reference=f"txn-{election.election_id}",
        election_id=election.election_id,
        category_id=election.election_id,  # arbitrary, not FK-enforced on this table
        voter_hash="hash",
        candidate_ids=[],
        amount=10000,  # pesewas -> 100.00 cedis
        status="success",
    )
    db_session.add(txn)
    await db_session.flush()

    return election


class TestExpressPayout:
    """Express payout is open to every plan now — the old Standard/Premium
    split (and the admin queue's express-first sort, which only meant
    something when some payouts weren't express) no longer applies."""

    async def test_standard_plan_is_express(self, client: AsyncClient, db_session, admin_user):
        election = await _make_paid_election(db_session, admin_user, "standard")
        res = await client.post(
            f"/api/v1/payouts/elections/{election.election_id}",
            json={
                "payout_method": "mobile_money",
                "recipient_name": "Test Organizer",
                "destination_code": "MTN",
                "destination_number": "0241234567",
            },
            headers=admin_user["headers"],
        )
        assert res.status_code == 201
        assert res.json()["express"] is True

    async def test_premium_plan_is_express(self, client: AsyncClient, db_session, admin_user):
        election = await _make_paid_election(db_session, admin_user, "premium")
        res = await client.post(
            f"/api/v1/payouts/elections/{election.election_id}",
            json={
                "payout_method": "mobile_money",
                "recipient_name": "Test Organizer",
                "destination_code": "MTN",
                "destination_number": "0241234567",
            },
            headers=admin_user["headers"],
        )
        assert res.status_code == 201
        assert res.json()["express"] is True
