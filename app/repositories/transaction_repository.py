from typing import Optional
from uuid import UUID

from sqlalchemy import select
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
