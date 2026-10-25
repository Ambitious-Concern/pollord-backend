"""Free votes (real voters, real encrypted ballots) for every category, plus
a global budget of paid bulk-vote Transactions confined to the elections
designated as payout-revenue sources (allow_revoting is only ever True on
those, see elections.py) — a fixed budget rather than an independent
per-category random draw is what keeps the platform-wide transaction total
controllable."""
import hashlib
import uuid as _uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.election import Candidate, Category, Election
from app.models.event import Event
from app.models.transaction import Transaction
from app.models.user import User
from app.models.vote import Vote
from app.services.cryptography_service import CryptographyService
from seeders import config
from seeders.config import rng
from seeders.data.distribute import distribute

_CHANNELS = ["mobile_money", "mobile_money", "mobile_money", "card", "bank"]
_TXN_STATUS = ["success"] * 17 + ["pending"] * 2 + ["failed"] * 1  # ~85/10/5 split


def _weighted_candidate(candidates: list[Candidate]) -> Candidate:
    """Exponential decay favoring earlier display_order — a realistic
    leaderboard shape instead of a flat random split."""
    weights = [1.5 ** (len(candidates) - i) for i in range(len(candidates))]
    return rng.choices(candidates, weights=weights, k=1)[0]


def _random_past_datetime(now: datetime, max_days: int) -> datetime:
    return now - timedelta(days=rng.randint(0, max_days), hours=rng.randint(0, 23), minutes=rng.randint(0, 59))


async def seed_votes(db: AsyncSession, elections: list[Election], events: list[Event], voters: list[User]) -> None:
    crypto = CryptographyService()
    now = datetime.now(timezone.utc)

    parents: list[tuple] = [(e, "election") for e in elections if e.status in ("active", "completed")]
    parents += [(e, "event") for e in events if e.status == "published"]

    # Idempotency guard: votes/transactions have no natural unique key to
    # check per-row like elections/events have title, so a re-run instead
    # skips any category that already has votes — safe to top up brand new
    # elections/events added by bumping N_ELECTIONS/N_EVENTS, without ever
    # risking a uq_vote_per_category collision on already-seeded ones.
    already_voted_category_ids = {
        row[0] for row in (await db.execute(select(Vote.category_id).distinct())).all()
    }

    # Free votes for every category — turnout should look real even on
    # elections/events that never charge for a vote.
    for parent, kind in parents:
        for category in parent.categories:
            if category.category_id in already_voted_category_ids:
                continue
            candidates = category.candidates
            if not candidates:
                continue

            election_id = parent.election_id if kind == "election" else None
            event_id = parent.event_id if kind == "event" else None
            max_days = min(config.DATA_SPREAD_DAYS, (now - parent.start_datetime).days) if kind == "election" else config.DATA_SPREAD_DAYS
            max_days = max(max_days, 1)

            n_free_voters = min(len(voters), rng.randint(8, 60))
            for voter in rng.sample(voters, n_free_voters):
                candidate = _weighted_candidate(candidates)
                voter_hash = crypto.generate_voter_hash(voter.user_id, category.category_id)
                cast_at = _random_past_datetime(now, max_days)
                encrypted = crypto.encrypt_vote_data([str(candidate.candidate_id)])
                signature = crypto.sign_vote(encrypted, cast_at.isoformat())
                db.add(Vote(
                    category_id=category.category_id,
                    election_id=election_id,
                    event_id=event_id,
                    voter_hash=voter_hash,
                    vote_data=encrypted,
                    vote_signature=signature,
                    count=1,
                    cast_at=cast_at,
                ))
            # Flush per category rather than batching every category's votes
            # into one flush — a single insertmanyvalues batch spanning
            # thousands of BYTEA-heavy rows across many categories trips an
            # asyncpg/SQLAlchemy edge case that surfaces as a misleading
            # MissingGreenlet error instead of a normal DBAPI one.
            await db.flush()

    # Budgeted paid bulk votes — only the elections designated as payout
    # sources ever charge for a vote (see elections.py's allow_revoting).
    paid_categories: list[tuple[Election, Category]] = [
        (election, category)
        for election in elections
        if election.allow_revoting and election.status in ("active", "completed")
        for category in election.categories
        if category.candidates and category.category_id not in already_voted_category_ids
    ]

    if paid_categories and config.N_VOTE_TRANSACTIONS > 0:
        counts = distribute(config.N_VOTE_TRANSACTIONS, len(paid_categories), minimum=1)
        for (election, category), n in zip(paid_categories, counts):
            vote_price = election.vote_price or 100
            max_days = max(min(config.DATA_SPREAD_DAYS, (now - election.start_datetime).days), 1)

            for _ in range(n):
                candidate = _weighted_candidate(category.candidates)
                vote_count = rng.randint(1, 20)
                amount = vote_count * vote_price
                status = rng.choice(_TXN_STATUS)
                cast_at = _random_past_datetime(now, max_days)
                reference = f"seed_vote_{_uuid.uuid4().hex[:16]}"
                voter_hash = hashlib.sha256(_uuid.uuid4().hex.encode()).hexdigest()
                voter = rng.choice(voters)
                paystack_response: Optional[dict] = None
                if status != "pending":
                    channel = rng.choice(_CHANNELS)
                    paystack_response = {"channel": channel, "fees": round(amount * 0.0195)}

                db.add(Transaction(
                    reference=reference,
                    election_id=election.election_id,
                    event_id=None,
                    category_id=category.category_id,
                    voter_hash=voter_hash,
                    email=voter.email,
                    candidate_ids=[str(candidate.candidate_id)],
                    amount=amount,
                    status=status,
                    vote_count=vote_count if status == "success" else None,
                    paystack_response=paystack_response,
                    created_at=cast_at,
                ))

                if status == "success":
                    encrypted = crypto.encrypt_vote_data([str(candidate.candidate_id)])
                    signature = crypto.sign_vote(encrypted, cast_at.isoformat())
                    db.add(Vote(
                        category_id=category.category_id,
                        election_id=election.election_id,
                        event_id=None,
                        voter_hash=voter_hash,
                        vote_data=encrypted,
                        vote_signature=signature,
                        count=vote_count,
                        cast_at=cast_at,
                    ))
            await db.flush()
