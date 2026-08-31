from typing import Optional
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.transaction import Transaction
from app.repositories.base import BaseRepository


class TransactionRepository(BaseRepository[Transaction]):
    def __init__(self, session: AsyncSession):
        super().__init__(Transaction, session)

    async def get_by_reference(self, reference: str) -> Optional[Transaction]:
        result = await self.session.execute(
            select(Transaction).where(Transaction.reference == reference)
        )
        return result.scalar_one_or_none()

    async def get_revenue_by_election(self, election_id: UUID) -> float:
        """Gross revenue (cedis) from paid votes on this election. `amount`
        is stored in pesewas, matching PayoutRequest.amount's cedis unit
        requires the /100 — same convention as ticket_repository's
        get_revenue_by_event, which sums an already-cedis column."""
        result = await self.session.execute(
            select(func.sum(Transaction.amount)).where(
                Transaction.election_id == election_id,
                Transaction.status == "success",
            )
        )
        return float(result.scalar_one() or 0) / 100

    async def count_paid_votes_by_election(self, election_id: UUID) -> int:
        result = await self.session.execute(
            select(func.count()).select_from(Transaction).where(
                Transaction.election_id == election_id,
                Transaction.status == "success",
            )
        )
        return result.scalar_one()

    async def get_by_id(self, id: UUID, *, id_field: str = "transaction_id") -> Optional[Transaction]:
        return await super().get_by_id(id, id_field=id_field)

    async def update_status(
        self, reference: str, status: str, paystack_response: dict | None = None
    ) -> Optional[Transaction]:
        txn = await self.get_by_reference(reference)
        if not txn:
            return None
        txn.status = status
        if paystack_response is not None:
            txn.paystack_response = paystack_response
        await self.session.flush()
        await self.session.refresh(txn)
        return txn
