from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Dict, List
from uuid import UUID

from sqlalchemy import Integer, and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.election import Candidate, Category
from app.models.transaction import Transaction
from app.models.vote import Vote

_CENTS = Decimal("100")


class ElectionSummaryStats:
    __slots__ = ("categories_count", "candidates_count", "votes_cast", "revenue_ghs", "revenue_growth_pct")

    def __init__(self) -> None:
        self.categories_count = 0
        self.candidates_count = 0
        self.votes_cast = 0
        self.revenue_ghs = "0.00"
        self.revenue_growth_pct = 0.0


class ElectionSummaryService:
    """Bulk stats for the organizer dashboard's elections grid — one query
    per metric across every requested election, not one query per card."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_summaries(self, election_ids: List[UUID]) -> Dict[UUID, ElectionSummaryStats]:
        stats: Dict[UUID, ElectionSummaryStats] = {eid: ElectionSummaryStats() for eid in election_ids}
        if not election_ids:
            return stats

        category_rows = (
            await self.db.execute(
                select(Category.election_id, func.count(Category.category_id))
                .where(Category.election_id.in_(election_ids))
                .group_by(Category.election_id)
            )
        ).all()
        for election_id, count in category_rows:
            stats[election_id].categories_count = count

        # Candidate.election_id is denormalized but kept in sync by the
        # service layer (see Candidate's model docstring) — fine for a
        # read-only dashboard count.
        candidate_rows = (
            await self.db.execute(
                select(Candidate.election_id, func.count(Candidate.candidate_id))
                .where(Candidate.election_id.in_(election_ids))
                .group_by(Candidate.election_id)
            )
        ).all()
        for election_id, count in candidate_rows:
            stats[election_id].candidates_count = count

        vote_rows = (
            await self.db.execute(
                select(Vote.election_id, func.count(Vote.vote_id))
                .where(Vote.election_id.in_(election_ids))
                .group_by(Vote.election_id)
            )
        ).all()
        for election_id, count in vote_rows:
            stats[election_id].votes_cast = count

        now = datetime.now(timezone.utc)
        week_ago = now - timedelta(days=7)
        two_weeks_ago = now - timedelta(days=14)
        revenue_rows = (
            await self.db.execute(
                select(
                    Transaction.election_id,
                    func.sum(Transaction.amount),
                    func.sum(
                        case((Transaction.created_at >= week_ago, Transaction.amount), else_=0)
                    ).cast(Integer),
                    func.sum(
                        case(
                            (
                                and_(
                                    Transaction.created_at >= two_weeks_ago,
                                    Transaction.created_at < week_ago,
                                ),
                                Transaction.amount,
                            ),
                            else_=0,
                        )
                    ).cast(Integer),
                )
                .where(Transaction.election_id.in_(election_ids), Transaction.status == "success")
                .group_by(Transaction.election_id)
            )
        ).all()
        for election_id, total_pesewas, recent_pesewas, prior_pesewas in revenue_rows:
            s = stats[election_id]
            s.revenue_ghs = str((Decimal(total_pesewas or 0) / _CENTS).quantize(Decimal("0.01")))
            recent = recent_pesewas or 0
            prior = prior_pesewas or 0
            if prior > 0:
                s.revenue_growth_pct = round((recent - prior) / prior * 100, 1)
            elif recent > 0:
                s.revenue_growth_pct = 100.0
            else:
                s.revenue_growth_pct = 0.0

        return stats
