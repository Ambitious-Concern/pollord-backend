from typing import List, Optional
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.vote import Vote, VoteReceipt
from app.repositories.base import BaseRepository


class VoteRepository(BaseRepository[Vote]):
    def __init__(self, model, session: AsyncSession):
        super().__init__(model, session)

    async def has_voted(self, voter_hash: str, election_id: UUID) -> bool:
        result = await self.session.execute(
            select(Vote).where(
                Vote.election_id == election_id,
                Vote.voter_hash == voter_hash,
            )
        )
        return result.scalar_one_or_none() is not None

    async def get_votes_by_election(self, election_id: UUID) -> List[Vote]:
        result = await self.session.execute(
            select(Vote).where(Vote.election_id == election_id)
        )
        return list(result.scalars().all())

    async def count_by_election(self, election_id: UUID) -> int:
        result = await self.session.execute(
            select(func.count()).select_from(Vote).where(
                Vote.election_id == election_id
            )
        )
        return result.scalar_one()

    async def get_votes_timeline(self, election_id: UUID) -> List[dict]:
        result = await self.session.execute(
            select(
                func.date_trunc("hour", Vote.cast_at).label("hour"),
                func.count().label("count"),
            )
            .where(Vote.election_id == election_id)
            .group_by("hour")
            .order_by("hour")
        )
        return [{"hour": str(row.hour), "count": row.count} for row in result.all()]


class VoteReceiptRepository(BaseRepository[VoteReceipt]):
    def __init__(self, model, session: AsyncSession):
        super().__init__(model, session)

    async def get_by_receipt_code(self, code: str) -> Optional[VoteReceipt]:
        result = await self.session.execute(
            select(VoteReceipt).where(VoteReceipt.receipt_code == code)
        )
        return result.scalar_one_or_none()

    async def get_by_user_and_election(
        self, user_id: UUID, election_id: UUID
    ) -> Optional[VoteReceipt]:
        result = await self.session.execute(
            select(VoteReceipt).where(
                VoteReceipt.user_id == user_id,
                VoteReceipt.election_id == election_id,
            )
        )
        return result.scalar_one_or_none()
